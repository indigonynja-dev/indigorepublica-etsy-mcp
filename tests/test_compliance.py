"""Etsy API Terms compliance: retention pruning, snapshot freshness, buyer-data removal from the write log. Offline."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from mcp import Client

from indigorepublica_etsy_mcp import competitors
from indigorepublica_etsy_mcp.client import EtsyClient
from indigorepublica_etsy_mcp.competitors import Competitors, CompetitorError
from indigorepublica_etsy_mcp.config import Settings, load_settings
from indigorepublica_etsy_mcp.research import BUYER_REMOVED, ResearchDB, scrub_buyer_data
from indigorepublica_etsy_mcp.server import build_server

from .test_competitors import FakePublicEtsy, _raw
from .test_resources import read
from .test_server import env  # noqa: F401  (env fixture)


def _ago(**kw) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


def _seed_old_and_new(db: ResearchDB, base: int = 0) -> None:
    old, new = _ago(days=120), _ago(days=5)
    with closing(sqlite3.connect(db.path)) as con, con:
        for ts in (old, new):
            con.execute("INSERT INTO competitor_snapshots VALUES (11, ?, 1, '{}')", (ts,))
            con.execute("INSERT INTO market_listings (listing_id, keyword, source, fetched_at) VALUES (?, 'k', 'pt', ?)", (base + (1 if ts == old else 2), ts))
            con.execute("INSERT INTO keywords (keyword, source, fetched_at) VALUES (?, 'pt', ?)", (f"kw {base} {ts}", ts))
        con.execute("INSERT INTO audits VALUES (1, 50, '{}', ?)", (old,))


# ----------------------------------------------------------------------------- 1. retention
def test_prune_dry_run_counts_without_deleting(tmp_path):
    db = ResearchDB(tmp_path / "r.db")
    _seed_old_and_new(db)
    r = db.prune(90, dry_run=True)
    assert r["dry_run"] and r["total_rows"] == 3 and r["deleted"] == 0
    assert {t: v["rows"] for t, v in r["tables"].items()} == {"competitor_snapshots": 1, "market_listings": 1, "keywords": 1}
    assert [db.stats()["tables"][t]["rows"] for t in ("competitor_snapshots", "market_listings", "keywords")] == [2, 2, 2]


def test_prune_deletes_only_old_etsy_cache_rows_and_never_the_log_or_audits(tmp_path):
    db = ResearchDB(tmp_path / "r.db")
    _seed_old_and_new(db)
    with closing(sqlite3.connect(db.path)) as con, con:  # a 400-day-old write log row must survive
        con.execute("INSERT INTO write_log (ts, tool, mode, dry_run, result) VALUES (?, 'etsy_update_listing', 'safe', 0, 'ok')", (_ago(days=400),))
    r = db.prune(90)
    assert r["deleted"] == 3
    t = db.stats()["tables"]
    assert [t[x]["rows"] for x in ("competitor_snapshots", "market_listings", "keywords", "audits", "write_log")] == [1, 1, 1, 1, 1]
    assert db.prune(90)["total_rows"] == 0  # idempotent
    with pytest.raises(ValueError):
        db.prune(0)


def test_retention_setting_default_and_validation(monkeypatch, tmp_path):
    monkeypatch.setenv("ETSY_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ETSY_CACHE_RETENTION_DAYS", raising=False)
    assert load_settings().cache_retention_days == 90
    monkeypatch.setenv("ETSY_CACHE_RETENTION_DAYS", "30")
    assert load_settings().cache_retention_days == 30
    for bad in ("0", "-5", "soon"):
        monkeypatch.setenv("ETSY_CACHE_RETENTION_DAYS", bad)
        with pytest.raises(RuntimeError, match="ETSY_CACHE_RETENTION_DAYS"):
            load_settings()


async def test_startup_prunes_and_research_prune_tool_previews_then_runs(env):
    s, fake, _ = env
    s.cache_retention_days = 90
    db = ResearchDB(s.research_db)
    _seed_old_and_new(db)
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:  # building the server prunes
        assert db.stats()["tables"]["competitor_snapshots"]["rows"] == 1
        _seed_old_and_new(db, base=10)
        preview = (await c.call_tool("research_prune", {})).structured_content  # dry_run defaults to True
        assert preview["dry_run"] and preview["total_rows"] >= 3 and preview["retention_days"] == 90
        assert db.stats()["tables"]["market_listings"]["rows"] == 3, "a dry run deletes nothing"
        done = (await c.call_tool("research_prune", {"dry_run": False, "retention_days": 30})).structured_content
        assert done["deleted"] >= 3 and done["retention_days"] == 30
        bad = await c.call_tool("research_prune", {"retention_days": 0})
        assert bad.is_error


async def test_snapshot_cli_prunes_at_the_end(tmp_path, monkeypatch):
    fake = FakePublicEtsy({11: [_raw(1)], 22: []})
    s = Settings(keystring="KEY", shared_secret="SECRET", token_file=tmp_path / "none.json", max_qps=1000,
                 research_db=tmp_path / "r.db", cache_retention_days=90)
    db = ResearchDB(s.research_db)
    _seed_old_and_new(db)
    real = EtsyClient
    monkeypatch.setattr(competitors, "EtsyClient", lambda settings: real(settings, transport=httpx.MockTransport(fake)))
    await Competitors(real(s, transport=httpx.MockTransport(fake)), db).add("rival")
    result = await competitors._run_all(s, 0)
    assert result["ok"] == 1 and result["pruned"]["total_rows"] == 3 and result["pruned"]["deleted"] == 3


# ----------------------------------------------------------------------------- 2. freshness
def _age_snapshot(db: ResearchDB, hours: float) -> None:
    with closing(sqlite3.connect(db.path)) as con, con:
        con.execute("UPDATE competitor_snapshots SET taken_at=?", (_ago(hours=hours),))


@pytest.fixture()
def rig(tmp_path):
    fake = FakePublicEtsy({11: [_raw(i) for i in range(1, 4)], 22: []})
    s = Settings(keystring="KEY", shared_secret="SECRET", token_file=tmp_path / "none.json", max_qps=1000)
    db = ResearchDB(tmp_path / "r.db")
    c = Competitors(EtsyClient(s, transport=httpx.MockTransport(fake)), db)
    return c, fake, db


async def test_profile_fresh_snapshot_is_not_refreshed(rig):
    c, fake, db = rig
    await c.add("rival")
    await c.snapshot("rival")
    n = len(fake.calls)
    p = await c.profile_fresh("rival")
    assert p["refresh"] == {"attempted": False, "refreshed": False} and not p["stale"] and len(fake.calls) == n
    assert p["snapshot_age"].startswith("Snapshot taken ") and "STALE" not in p["snapshot_age"]


async def test_profile_stale_snapshot_is_refreshed_first(rig):
    c, fake, db = rig
    await c.add("rival")
    await c.snapshot("rival")
    _age_snapshot(db, 7)
    p = await c.profile_fresh("rival")
    assert p["refresh"]["refreshed"] and not p["stale"] and p["snapshot_age_hours"] < 0.1
    assert db.stats()["tables"]["competitor_snapshots"]["rows"] == 2


async def test_profile_refresh_failure_returns_old_data_labelled_with_its_age(rig):
    c, fake, db = rig
    await c.add("rival")
    await c.snapshot("rival")
    _age_snapshot(db, 30)
    fake.listings.pop(11)  # Etsy now answers 404 for the shop's listings
    p = await c.profile_fresh("rival")
    assert p["refresh"]["attempted"] and not p["refresh"]["refreshed"] and p["refresh"]["error"]
    assert p["stale"] and "STALE" in p["snapshot_age"] and "30h old" in p["snapshot_age"] and p["listing_count"] == 3
    calls = len(fake.calls)
    again = await c.profile_fresh("rival")  # a failed refresh is not retried straight away (rate limits)
    assert len(fake.calls) == calls and "not retrying" in again["refresh"]["note"] and again["listing_count"] == 3


async def test_profile_refresh_is_allowed_in_every_mode(env):
    """Like competitor_snapshot: only reads public Etsy data and writes the local cache, so readonly mode still refreshes."""
    s, fake, _ = env
    s.mode = "readonly"
    pub = FakePublicEtsy({11: [_raw(1)], 22: []})
    db = ResearchDB(s.research_db)
    comp = Competitors(EtsyClient(s, transport=httpx.MockTransport(pub)), db)
    await comp.add("rival")
    await comp.snapshot("rival")
    _age_snapshot(db, 50)
    async with Client(build_server(s, transport=httpx.MockTransport(lambda r: pub(r) if r.headers.get("authorization") is None else fake(r)))) as c:
        p = (await c.call_tool("competitor_profile", {"shop": "rival"})).structured_content
    assert p["refresh"]["refreshed"] and not p["stale"]
    assert db.stats()["tables"]["competitor_snapshots"]["rows"] == 2


async def test_diff_shows_both_snapshot_dates(rig):
    c, _, db = rig
    await c.add("rival")
    await c.snapshot("rival")
    await c.snapshot("rival")
    with closing(sqlite3.connect(db.path)) as con, con:
        con.execute("UPDATE competitor_snapshots SET taken_at=? WHERE rowid=1", (_ago(days=10),))
    d = c.diff("rival")
    older, newer = d["snapshot_dates"]["older"], d["snapshot_dates"]["newer"]
    assert older["date"] == d["from"][:10] and newer["date"] == d["to"][:10] and older["age"] == "10d old"
    assert d["from"] in d["comparison"] and d["to"] in d["comparison"]


async def test_competitor_resource_leads_with_snapshot_age(env):
    s, fake, _ = env
    pub = FakePublicEtsy({11: [_raw(1)], 22: []})
    db = ResearchDB(s.research_db)
    comp = Competitors(EtsyClient(s, transport=httpx.MockTransport(pub)), db)
    await comp.add("rival")
    await comp.snapshot("rival")
    _age_snapshot(db, 72)
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        p = await read(c, "etsy://competitor/rival")
    assert list(p)[0] == "snapshot_age" and "3d old" in p["snapshot_age"] and "STALE" in p["snapshot_age"]
    assert p["stale"] and p["snapshot_age_hours"] == pytest.approx(72, abs=0.1)
    assert not fake.calls, "the resource never refreshes: it stays local"


# ----------------------------------------------------------------------------- 3. buyer privacy
BUYER_STRINGS = ["Jane Q. Buyer", "Jane", "Buyer", "123 Secret Street", "Apt 4B", "Springfield", "Oregon", "97477", "New Zealand", "NZ",
                 "jane.buyer@example.com", "+1 555-010-1234", "987654321", "Happy birthday, Mom! Love, Jane", "Name: Emma Rose",
                 "Emma Rose"]


def receipt() -> dict:
    return {
        "receipt_id": 5551, "seller_user_id": 77, "buyer_user_id": 987654321, "buyer_email": "jane.buyer@example.com",
        "name": "Jane Q. Buyer", "first_line": "123 Secret Street", "second_line": "Apt 4B", "city": "Springfield", "state": "Oregon",
        "zip": "97477", "country_iso": "NZ", "formatted_address": "Jane Q. Buyer\n123 Secret Street\nApt 4B\nSpringfield, OR 97477\nNew Zealand",
        "phone": "+1 555-010-1234", "gift_message": "Happy birthday, Mom! Love, Jane", "message_from_buyer": "Please ship fast, Jane",
        "grandtotal": {"amount": 1299, "divisor": 100, "currency_code": "USD"}, "status": "paid", "is_gift": True,
        "transactions": [{"transaction_id": 9, "listing_id": 10, "title": "Budget Planner", "quantity": 1,
                          "variations": [{"property_id": 513, "formatted_name": "Personalization", "formatted_value": "Emma Rose"}]}],
        "shipments": [{"carrier_name": "USPS", "tracking_code": "9400111"}],
    }


def _row_text(db: ResearchDB) -> str:
    with closing(sqlite3.connect(db.path)) as con:
        return json.dumps(con.execute("SELECT * FROM write_log").fetchall(), ensure_ascii=False)


def test_scrub_removes_buyer_fields_and_keeps_the_rest():
    out = scrub_buyer_data(receipt())
    dumped = json.dumps(out)
    for s in BUYER_STRINGS:
        assert s not in dumped, s
    assert out["name"] == out["city"] == out["buyer_email"] == out["phone"] == BUYER_REMOVED
    assert out["receipt_id"] == 5551 and out["status"] == "paid" and out["grandtotal"]["amount"] == 1299
    assert out["transactions"][0]["title"] == "Budget Planner" and out["transactions"][0]["variations"][0]["formatted_value"] == BUYER_REMOVED
    assert out["shipments"][0]["tracking_code"] == "9400111"


def test_scrub_removes_phone_numbers_from_free_text_but_not_ids_dates_or_amounts():
    for phone in ("+1 555-010-1234", "(555) 010-1234", "555.010.1234", "+44 20 7946 0958", "+15550101234"):
        assert scrub_buyer_data(f"call {phone} now") == f"call {BUYER_REMOVED} now", phone
    safe = "listing 1234567890 at 2026-10-05 20:12:33, price 1,299.00 USD, receipt 5551, tracking 9400111, v1.2.3"
    assert scrub_buyer_data(safe) == safe


def test_scrub_leaves_listing_state_and_shop_names_alone():
    listing = {"listing_id": 1, "state": "active", "title": "Planner", "shop_name": "IndigoRepublica", "name": "kept"}
    assert scrub_buyer_data(listing) == listing
    assert scrub_buyer_data({"result": "failed for a@b.com"}) == {"result": f"failed for {BUYER_REMOVED}"}
    nested = scrub_buyer_data({"json": json.dumps({"first_line": "1 Road", "state": "active"})})
    assert "1 Road" not in nested["json"]


def test_log_write_never_stores_buyer_data(tmp_path):
    db = ResearchDB(tmp_path / "r.db")
    db.log_write("etsy_api_request", None, "safe", False, receipt(), {"request": {"form": {"first_name": "Jane", "city": "Springfield"}},
                                                                         "response": receipt()}, "error: bounced to jane.buyer@example.com")
    stored = _row_text(db)
    for s in BUYER_STRINGS:
        assert s not in stored, s
    assert BUYER_REMOVED in stored and "5551" in stored and "Budget Planner" in stored
    row = db.read_write_log()[0]
    assert row["after"]["response"]["transactions"][0]["variations"][0]["formatted_value"] == BUYER_REMOVED


async def test_receipt_response_through_a_real_write_is_not_stored(env):
    """etsy_add_tracking returns the updated receipt: the whole receipt-shaped response must be scrubbed before it is logged."""
    s, fake, _ = env
    s.mode = "safe"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/tracking"):
            return httpx.Response(200, json=receipt())
        return fake(req)

    async with Client(build_server(s, transport=httpx.MockTransport(handler))) as c:
        r = await c.call_tool("etsy_api_request", {"method": "POST", "path": "/shops/4242/receipts/5551/tracking",
                                                   "form": {"tracking_code": "9400111", "carrier_name": "usps", "first_name": "Jane"}})
        assert not r.is_error, r.content
        entries = (await c.call_tool("etsy_write_log", {"limit": 5})).structured_content["entries"]
    stored = _row_text(ResearchDB(s.research_db))
    for secret in BUYER_STRINGS:
        assert secret not in stored, secret
    assert entries and entries[0]["tool"] == "etsy_api_request" and BUYER_REMOVED in stored and "9400111" in stored
