"""Competitor tracking tests: mocked Etsy public endpoints, hand-built snapshots, migration. Fully offline."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import httpx
import pytest

from indigorepublica_etsy_mcp import competitors
from indigorepublica_etsy_mcp.client import EtsyClient
from indigorepublica_etsy_mcp.competitors import Competitors, CompetitorError, diff_snapshots, percentile
from indigorepublica_etsy_mcp.config import Settings
from indigorepublica_etsy_mcp.research import _V1, SCHEMA_VERSION, ResearchDB, _split, _split
from indigorepublica_etsy_mcp.server import build_server

from .test_server import call, env  # noqa: F401  (env fixture)


def _l(i, title="T", tags=("a",), price=5.0, images=3, created="2026-01-01T00:00:00+00:00"):
    return {"listing_id": i, "title": title, "tags": list(tags), "price": price, "image_count": images, "created": created,
            "currency": "USD", "updated": None, "views": None, "num_favorers": None, "url": None}


def _raw(i, title="T", tags=("a",), cents=500, images=2, created=1735689600):
    return {"listing_id": i, "title": title, "tags": list(tags), "price": {"amount": cents, "divisor": 100, "currency_code": "USD"},
            "images": [{"listing_image_id": k} for k in range(images)], "original_creation_timestamp": created,
            "last_modified_timestamp": created + 10, "url": f"https://etsy.example/l/{i}"}


class FakePublicEtsy:
    """Public endpoints only: asserts the key header is sent and no OAuth bearer is."""

    def __init__(self, listings=None, fail_429_once=False):
        self.shops = {"rival": 11, "other": 22}
        self.listings = listings or {11: [_raw(i) for i in range(1, 251)], 22: []}  # 250 listings -> 3 pages of 100
        self.calls: list[httpx.Request] = []
        self.fail_429_once = fail_429_once

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        assert req.headers["x-api-key"] == "KEY:SECRET"
        assert "authorization" not in req.headers
        p, q = req.url.path, dict(req.url.params)
        if self.fail_429_once:
            self.fail_429_once = False
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "slow down"})
        if p == "/v3/application/shops":
            name = q["shop_name"].lower()
            hits = [{"shop_id": v, "shop_name": k.title()} for k, v in self.shops.items() if name in k]
            return httpx.Response(200, json={"count": len(hits), "results": hits})
        if p.startswith("/v3/application/shops/") and p.endswith("/listings/active"):
            sid = int(p.split("/")[4])
            assert q["includes"] == "Images"
            allr = self.listings.get(sid)
            if allr is None:
                return httpx.Response(404, json={"error": "shop not found"})
            lim, off = int(q["limit"]), int(q["offset"])
            assert lim <= 100
            return httpx.Response(200, json={"count": len(allr), "results": allr[off:off + lim]})
        if p.startswith("/v3/application/shops/"):
            sid = int(p.rsplit("/", 1)[1])
            n = {v: k for k, v in self.shops.items()}.get(sid)
            return httpx.Response(200, json={"shop_id": sid, "shop_name": n.title()}) if n else httpx.Response(404, json={"error": "no"})
        return httpx.Response(404, json={"error": p})


@pytest.fixture()
def rig(tmp_path):
    fake = FakePublicEtsy()
    s = Settings(keystring="KEY", shared_secret="SECRET", token_file=tmp_path / "none.json", max_qps=1000)
    etsy = EtsyClient(s, transport=httpx.MockTransport(fake))
    db = ResearchDB(tmp_path / "r.db")
    return Competitors(etsy, db), fake, db


# ----------------------------------------------------------------------------- watchlist + snapshots
async def test_add_list_remove(rig):
    c, fake, _ = rig
    a = await c.add("Rival")
    assert a == {"shop_id": 11, "shop_name": "Rival", "already_watched": False}
    assert (await c.add("22"))["shop_name"] == "Other"  # by id
    assert (await c.add("rival"))["already_watched"] is True
    assert [s["shop_name"] for s in c.list()["shops"]] == ["Other", "Rival"]
    with pytest.raises(CompetitorError, match="No shop named"):
        await c.add("nonexistent")
    assert c.remove("rival")["removed"] == 11
    assert c.list()["count"] == 1
    with pytest.raises(CompetitorError, match="watchlist"):
        c.resolve("rival")


async def test_snapshot_paginates_and_stores(rig):
    c, fake, db = rig
    await c.add("rival")
    r = await c.snapshot("rival")
    assert r["listing_count"] == 250 and not r["empty"]
    pages = [dict(x.url.params) for x in fake.calls if x.url.path.endswith("/listings/active")]
    assert [p["offset"] for p in pages] == ["0", "100", "200"]
    snap = c._snapshots(11)[0]
    first = snap["listings"][0]
    assert snap["listing_count"] == 250 and first["price"] == 5.0 and first["image_count"] == 2
    assert first["created"].startswith("2025-01-01") and first["tags"] == ["a"]
    assert db.stats()["tables"]["competitor_snapshots"]["rows"] == 1


async def test_empty_shop(rig):
    c, _, _ = rig
    await c.add("other")
    r = await c.snapshot("other")
    assert r["listing_count"] == 0 and r["empty"]
    p = c.profile("other")
    assert p["listing_count"] == 0 and p["price"]["median"] is None and p["top_tags"] == [] and p["avg_listing_age_days"] is None
    await c.snapshot("other")
    d = c.diff("other")
    assert d["summary"]["total_changes"] == 0


async def test_429_retried_by_client_backoff(tmp_path):
    fake = FakePublicEtsy(fail_429_once=True)
    s = Settings(keystring="KEY", shared_secret="SECRET", token_file=tmp_path / "none.json", max_qps=1000)
    c = Competitors(EtsyClient(s, transport=httpx.MockTransport(fake)), ResearchDB(tmp_path / "r.db"))
    assert (await c.add("rival"))["shop_id"] == 11
    assert len(fake.calls) == 2


async def test_snapshot_all_continues_after_failure(rig):
    c, fake, _ = rig
    await c.add("rival")
    await c.add("other")
    fake.listings[22] = None  # -> 404 for this shop
    res = await c.snapshot_all(delay=0)
    assert (res["watched"], res["ok"], res["failed"]) == (2, 1, 1)
    assert res["errors"][0]["shop_name"] == "Other" and "404" in res["errors"][0]["error"]
    assert c._snapshots(22) == []  # failed fetch stores nothing


# ----------------------------------------------------------------------------- diff
def test_diff_snapshots_hand_built():
    old = [_l(1, "Old title", ["a", "b"], 10.0, 3), _l(2, "Same", ["x"], 20.0, 2), _l(3, "Gone", ["g"], 4.0, 1), _l(4, "Free", [], 0.0, 1)]
    new = [_l(1, "New title", ["b", "c"], 12.5, 5), _l(2, "Same", ["x"], 20.0, 2), _l(5, "Fresh", ["f"], 7.0, 4), _l(4, "Free", [], 1.0, 1)]
    d = diff_snapshots(old, new)
    s = d["summary"]
    assert (s["new_listings"], s["removed_listings"], s["title_changes"], s["tag_changes"], s["price_changes"],
            s["image_count_changes"]) == (1, 1, 1, 1, 2, 1)
    assert (s["listings_before"], s["listings_after"], s["total_changes"]) == (4, 4, 7)
    assert d["details"]["new_listings"][0]["listing_id"] == 5 and d["details"]["removed_listings"][0]["listing_id"] == 3
    assert d["details"]["title_changes"][0] == {"listing_id": 1, "title": "New title", "old": "Old title", "new": "New title"}
    assert d["details"]["tag_changes"][0]["added"] == ["c"] and d["details"]["tag_changes"][0]["removed"] == ["a"]
    by = {p["listing_id"]: p for p in d["details"]["price_changes"]}
    assert by[1]["change"] == 2.5 and by[1]["change_pct"] == 25.0
    assert by[4]["change_pct"] is None  # from 0: percentage undefined
    assert d["details"]["image_count_changes"][0] == {"listing_id": 1, "title": "New title", "old": 3, "new": 5}
    assert "truncated" not in d


def test_diff_no_changes_and_cap():
    same = [_l(i) for i in range(5)]
    assert diff_snapshots(same, same)["summary"]["total_changes"] == 0
    d = diff_snapshots([], same, max_details=2)
    assert d["summary"]["new_listings"] == 5 and len(d["details"]["new_listings"]) == 2 and d["truncated"]["totals"] == {"new_listings": 5}


def _store(db, shop_id, taken_at, listings):
    with closing(db._connect()) as con, con:
        con.execute("INSERT INTO competitor_snapshots VALUES (?,?,?,?)",
                    (shop_id, taken_at, len(listings), json.dumps({"shop_id": shop_id, "shop_name": "Rival", "listings": listings})))


def test_diff_latest_two_and_since(rig):
    c, _, db = rig
    with closing(db._connect()) as con, con:
        con.execute("INSERT INTO competitors VALUES (11,'Rival','2026-01-01T00:00:00+00:00')")
    _store(db, 11, "2026-03-01T00:00:00+00:00", [_l(1, price=10.0)])
    _store(db, 11, "2026-04-01T00:00:00+00:00", [_l(1, price=11.0), _l(2)])
    _store(db, 11, "2026-05-01T00:00:00+00:00", [_l(1, price=12.0), _l(2), _l(3)])
    d = c.diff("Rival")
    assert (d["from"], d["to"]) == ("2026-04-01T00:00:00+00:00", "2026-05-01T00:00:00+00:00")
    assert d["summary"]["new_listings"] == 1 and d["summary"]["price_changes"] == 1
    d = c.diff("rival", since="2026-03-15")  # newest snapshot on/before that date (excluding the latest)
    assert d["from"] == "2026-03-01T00:00:00+00:00" and d["summary"]["new_listings"] == 2
    with pytest.raises(CompetitorError, match="No snapshot taken on or before"):
        c.diff("rival", since="2026-01-01")
    with pytest.raises(CompetitorError, match="Bad since"):
        c.diff("rival", since="nonsense")
    assert c.diff("11")["shop_id"] == 11  # numeric id works too


def test_diff_needs_two_snapshots(rig):
    c, _, db = rig
    with closing(db._connect()) as con, con:
        con.execute("INSERT INTO competitors VALUES (11,'Rival','x')")
    with pytest.raises(CompetitorError, match="No snapshots"):
        c.diff("rival")
    _store(db, 11, "2026-03-01T00:00:00+00:00", [_l(1)])
    with pytest.raises(CompetitorError, match="Only one snapshot"):
        c.diff("rival")


# ----------------------------------------------------------------------------- profile
def test_percentile():
    assert percentile([], 0.5) is None
    assert percentile([4.0], 0.25) == 4.0
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.25) == 1.75


def test_profile(rig):
    c, _, db = rig
    with closing(db._connect()) as con, con:
        con.execute("INSERT INTO competitors VALUES (11,'Rival','x')")
    ls = [_l(i, price=float(i), tags=["Planner", "budget"] if i % 2 else ["planner"], created=f"2026-01-{i:02d}T00:00:00+00:00")
          for i in range(1, 13)]
    _store(db, 11, "2026-02-01T00:00:00+00:00", [_l(99, price=1.0)])  # older snapshot must be ignored
    _store(db, 11, "2026-03-01T00:00:00+00:00", ls)
    p = c.profile("rival")
    assert p["listing_count"] == 12 and p["snapshot_taken_at"] == "2026-03-01T00:00:00+00:00"
    assert (p["price"]["median"], p["price"]["p25"], p["price"]["p75"]) == (6.5, 3.75, 9.25)
    assert p["top_tags"] == [{"tag": "planner", "count": 12}, {"tag": "budget", "count": 6}]  # case-folded, sorted by count
    assert [n["listing_id"] for n in p["newest_listings"]] == [12, 11, 10, 9, 8, 7, 6, 5, 4, 3]
    assert p["avg_listing_age_days"] == 53.5  # Mar 1 minus Jan 1..12: mean of 59..48


def test_profile_top_tags_capped_at_30(rig):
    c, _, db = rig
    with closing(db._connect()) as con, con:
        con.execute("INSERT INTO competitors VALUES (11,'Rival','x')")
    _store(db, 11, "2026-03-01T00:00:00+00:00", [_l(1, tags=[f"t{i}" for i in range(40)])])
    assert len(c.profile("rival")["top_tags"]) == 30


def test_profile_reads_only_the_latest_snapshot(rig):
    """Every snapshot holds all of a shop's listings, and daily snapshots pile up: profiling must not parse the old ones."""
    c, _, db = rig
    with closing(db._connect()) as con, con:
        con.execute("INSERT INTO competitors VALUES (11,'Rival','x')")
        con.execute("INSERT INTO competitor_snapshots VALUES (11, '2026-01-01T00:00:00+00:00', 1, 'unreadable: parsing this would crash')")
    _store(db, 11, "2026-03-01T00:00:00+00:00", [_l(1, price=5.0), _l(2, price=7.0)])
    assert c.profile("rival")["listing_count"] == 2
    assert [s["taken_at"] for s in c._snapshots(11, limit=1)] == ["2026-03-01T00:00:00+00:00"]


# ----------------------------------------------------------------------------- migration
def test_shop_name_migration_on_existing_v1_database(tmp_path):
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(path)) as con:
        for stmt in _split(_V1):
            con.execute(stmt)
        con.execute("PRAGMA user_version = 1")
        con.execute("INSERT INTO market_listings (listing_id, keyword, shop_id, title, price, source, fetched_at) "
                    "VALUES (1,'kw',9,'Old row',3.5,'profittree','2026-01-01T00:00:00+00:00')")
        con.commit()
    db = ResearchDB(path)
    assert db.schema_version() == SCHEMA_VERSION >= 2
    with closing(sqlite3.connect(path)) as con:
        cols = [r[1] for r in con.execute("PRAGMA table_info(market_listings)")]
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "shop_name" in cols and "competitors" in tables
    old = db.get_market("kw", 365)["rows"][0]
    assert old["title"] == "Old row" and old["shop_name"] is None  # existing data kept
    db.save_market_listings([{"id": 2, "shop_id": 9, "shop_name": "  SomeShop ", "title": "New", "price": 1}], "kw", "profittree")
    db.save_market_listings([{"listing_id": 3, "title": "No shop"}], "kw", "profittree")
    rows = {r["listing_id"]: r for r in db.get_market("kw", 365)["rows"]}
    assert rows[2]["shop_name"] == "SomeShop" and rows[3]["shop_name"] is None
    ResearchDB(path)  # idempotent re-open


# ----------------------------------------------------------------------------- MCP wiring
async def test_tools_registered_and_read_only_flags(env):
    s, fake, _ = env
    from mcp import Client

    server = build_server(s, transport=httpx.MockTransport(fake))
    async with Client(server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    names = {"competitor_add", "competitor_remove", "competitor_list", "competitor_snapshot", "competitor_snapshot_all",
             "competitor_diff", "competitor_profile"}
    assert names <= tools.keys()
    for n in ("competitor_list", "competitor_diff", "competitor_profile"):
        assert tools[n].annotations.read_only_hint is True
    assert not any(tools[n].annotations.destructive_hint for n in names)


async def test_tool_flow_through_server(env):
    s, _, _ = env
    fake = FakePublicEtsy({11: [_raw(1), _raw(2)], 22: []})
    server = build_server(s, transport=httpx.MockTransport(fake))
    assert (await call(server, "competitor_add", {"shop_name_or_id": "rival"})).structured_content["shop_id"] == 11
    assert (await call(server, "competitor_snapshot", {"shop": "Rival"})).structured_content["listing_count"] == 2
    fake.listings[11] = [_raw(1, title="Changed"), _raw(3)]
    assert (await call(server, "competitor_snapshot_all", {"delay_seconds": 0})).structured_content["ok"] == 1
    d = (await call(server, "competitor_diff", {"shop": "rival"})).structured_content
    assert d["summary"]["new_listings"] == 1 and d["summary"]["removed_listings"] == 1 and d["summary"]["title_changes"] == 1
    assert (await call(server, "competitor_profile", {"shop": "rival"})).structured_content["listing_count"] == 2
    assert (await call(server, "competitor_list")).structured_content["shops"][0]["snapshots"] == 2
    bad = await call(server, "competitor_diff", {"shop": "nobody"})
    assert bad.is_error


def test_cli_exit_codes(monkeypatch, tmp_path, capsys):
    fake = FakePublicEtsy({11: [_raw(1)], 22: None})
    s = Settings(keystring="KEY", shared_secret="SECRET", token_file=tmp_path / "none.json", max_qps=1000, research_db=tmp_path / "cli.db")
    db = ResearchDB(s.research_db)
    with closing(db._connect()) as con, con:
        con.execute("INSERT INTO competitors VALUES (11,'Rival','x')")
    monkeypatch.setattr(competitors, "load_settings", lambda: s)
    real = competitors.EtsyClient
    monkeypatch.setattr(competitors, "EtsyClient", lambda st: real(st, transport=httpx.MockTransport(fake)))
    with pytest.raises(SystemExit) as e:
        competitors.main(["--delay", "0"])
    assert e.value.code == 0
    assert json.loads(capsys.readouterr().out)["ok"] == 1
    with closing(db._connect()) as con, con:
        con.execute("INSERT INTO competitors VALUES (22,'Other','x')")
    with pytest.raises(SystemExit) as e:
        competitors.main(["--delay", "0"])
    assert e.value.code == 1  # one shop failed -> non-zero for the scheduler

    bad = Settings(keystring="", shared_secret="")
    monkeypatch.setattr(competitors, "load_settings", lambda: bad)
    with pytest.raises(SystemExit) as e:
        competitors.main([])
    assert "ETSY_KEYSTRING" in str(e.value.code)
