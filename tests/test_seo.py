"""SEO audit / tag gaps / preview+apply tests (offline)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from indigorepublica_etsy_mcp import seo
from indigorepublica_etsy_mcp.research import ResearchDB
from indigorepublica_etsy_mcp.server import build_server

from .test_server import SHOP, FakeEtsy, call, env  # noqa: F401  (env fixture)

GOOD = {
    "listing_id": 10, "type": "download", "taxonomy_id": 111,
    "title": "Monthly Budget Planner Spreadsheet Template for Google Sheets, Excel Finance Tracker Printable",
    "tags": ["budget planner", "monthly budget", "finance tracker", "google sheets", "excel template", "debt payoff",
             "savings tracker", "budget spreadsheet", "money planner", "expense tracker", "bill organizer", "paycheck budget", "digital download"],
    "description": "Monthly budget planner spreadsheet that works in Google Sheets and Excel.\n\nWHAT'S INCLUDED:\n- 12 monthly tabs\n- Debt payoff tracker\n- Savings goals\n\nHOW IT WORKS:\n1. Purchase and download\n2. Open the link in Google Sheets\n3. Make a copy and start budgeting\n\nPlease note this is a digital product; no physical item will be shipped.",
    "materials": ["Google Sheets"], "styles": ["minimalist"], "images": [{}] * 10,
}
BAD = {
    "listing_id": 11, "type": "download", "taxonomy_id": 111,
    "title": "PLANNER PLANNER PLANNER BUDGET",
    "tags": ["planner", "Planner", "planners", "budget"], "description": "Nice planner.", "materials": [], "images": [{}] * 2,
}


def test_weights_sum_to_100():
    assert sum(seo.WEIGHTS.values()) == 100


def test_good_listing_scores_high_bad_scores_low():
    good = seo.audit_listing(GOOD, file_count=1, properties=[{"values": ["x"]}], category_names=["Calendars & Planners"])
    bad = seo.audit_listing(BAD, file_count=0, properties=[], category_names=["Calendars & Planners"])
    assert good["score"] >= 90, good["fixes"]
    assert bad["score"] <= 35
    assert good["main_keyword"] == "budget planner"
    assert set(good["sub_scores"]) == set(seo.WEIGHTS)
    impacts = [f["impact"] for f in bad["fixes"]]
    assert impacts == sorted(impacts, reverse=True) and bad["fixes"][0]["priority"] == 1


def test_title_rules():
    s, issues = seo.score_title("PLANNER PLANNER PLANNER BUDGET", "budget", [])
    msgs = " ".join(i["message"] for i in issues)
    assert "ALL CAPS" in msgs and "Repeated" in msgs and s < 50
    _, issues = seo.score_title("A very long preamble of words that comes before the main phrase Budget Planner Template", "budget planner", [])
    assert any("starts" in i["message"] for i in issues)
    assert seo.score_title("", None, [])[0] == 0


def test_tag_rules():
    s, issues = seo.score_tags(["budget planner", "planner budget", "budget planners", "planners", "x" * 21], ["Planners"], [])
    msgs = " ".join(i["message"] for i in issues)
    assert "Near-duplicate" in msgs and "repeat the category" in msgs and "21 chars" in msgs and "Only 5/13" in msgs
    assert seo.score_tags([], [], [])[0] == 0


def test_description_rules():
    _, issues = seo.score_description("x" * 200 + " budget planner", "budget planner")
    assert any("first 160" in i["message"] for i in issues)
    wall = "Budget planner. " + "word " * 120
    assert any("scannable" in i["message"] for i in seo.score_description(wall, "budget planner")[1])


def test_physical_listing_drops_digital_weight():
    r = seo.audit_listing({**GOOD, "type": "physical"}, properties=None)
    assert "digital" not in r["sub_scores"] and any("renormalised" in n for n in r["notes"])


def test_validate_proposal_limits():
    from indigorepublica_etsy_mcp.server import seo_check
    _, errs = seo.validate_proposal(GOOD, "x" * 141, [f"t{i}" for i in range(14)] + ["y" * 21], None, seo_check)
    joined = " ".join(errs)
    assert "141 chars" in joined and "15 tags" in joined and "21 chars" in joined
    _, errs = seo.validate_proposal(GOOD, None, None, None, seo_check)
    assert "Nothing to change" in errs[0]
    prop, errs = seo.validate_proposal(GOOD, " Fine  title ", ["a b", "c d"], None, seo_check)
    assert not errs and prop == {"title": "Fine title", "tags": ["a b", "c d"]}
    assert seo.validate_proposal(GOOD, None, ["Dup", "dup"], None, seo_check)[1]


def _db(tmp_path):
    return ResearchDB(tmp_path / "r.db")


def test_tag_gaps_ranking_and_provenance(tmp_path):
    db = _db(tmp_path)
    db.save_market_listings([
        {"listing_id": 1, "tags": ["budget planner", "debt snowball", "bill tracker"]},
        {"listing_id": 2, "tags": ["debt snowball", "bill tracker", "finance"]},
        {"listing_id": 3, "tags": ["debt snowball", "Budget Planners"]},
    ], "budget planner", "profittree")
    db.save_keywords([{"keyword": "bill tracker", "searches": 5000}, {"keyword": "finance", "searches": 10}], "profittree")
    with closing(sqlite3.connect(db.path)) as con, con:
        con.execute("INSERT INTO competitor_snapshots VALUES (9, ?, 1, ?)", (seo.now_iso(), '{"listings": [{"tags": "bill tracker, zen"}]}'))
    r = seo.tag_gaps(db, ["budget planner"], "Budget Planner")
    tags = [g["tag"] for g in r["gaps"]]
    assert tags[:2] == ["bill tracker", "debt snowball"] and "budget planner" not in tags and "budget planners" not in tags
    # tie on frequency (3 each) is broken by cached search volume
    assert r["gaps"][0]["used_by"] == 3 and r["gaps"][0]["search_volume"] == 5000 and r["gaps"][1]["used_by"] == 3
    assert r["data"]["market_listings"]["listings"] == 3 and r["data"]["competitor_snapshots"]["shops"] == 1
    assert r["data"]["keyword_volume"]["keywords_cached"] == 2 and r["listings_compared"] == 4


def test_tag_gaps_without_data_says_so(tmp_path):
    r = seo.tag_gaps(_db(tmp_path), ["a b"], "nothing")
    assert r["gaps"] == [] and any("product_finder" in n for n in r["notes"])


# ------------------------------------------------------------------ tools end to end
class SeoFake(FakeEtsy):
    def __init__(self) -> None:
        super().__init__()
        self.listing = {**GOOD, "listing_id": 10, "state": "active"}
        self.listing.pop("images")
        self.listing["images"] = [{"listing_image_id": i} for i in range(3)]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path, req.method
        if p == "/v3/application/listings/10" and m == "GET":
            self.calls.append((m, p, b"", dict(req.headers)))
            return httpx.Response(200, json=self.listing)
        if p == f"/v3/application/shops/{SHOP}/listings/10/files":
            self.calls.append((m, p, b"", dict(req.headers)))
            return httpx.Response(200, json={"count": 1, "results": [{"listing_file_id": 1}]})
        if p == f"/v3/application/shops/{SHOP}/listings/10" and m == "PATCH":
            f = parse_qs(req.read().decode())
            for k, v in f.items():
                self.listing[k] = v[0].split(",") if k == "tags" else v[0]
        return super().__call__(req)


def _server(env_, mode="safe"):
    s, _, _ = env_
    s.mode = mode
    fake = SeoFake()
    return build_server(s, transport=httpx.MockTransport(fake)), fake


async def test_seo_tools_registered_and_annotated(env):
    from mcp import Client
    server, _ = _server(env)
    async with Client(server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    for n in ("seo_audit", "seo_tag_gaps", "seo_preview_update", "seo_apply_update"):
        assert n in tools
    assert tools["seo_tag_gaps"].annotations.read_only_hint is True
    assert tools["seo_apply_update"].annotations.read_only_hint is False


async def test_audit_is_stored(env):
    server, _ = _server(env)
    r = await call(server, "seo_audit", {"listing_id": 10})
    assert not r.is_error, r
    assert 0 < r.structured_content["score"] <= 100 and r.structured_content["fixes"]
    st = await call(server, "research_cache_stats")
    assert st.structured_content["tables"]["audits"]["rows"] == 1


async def test_preview_then_apply_writes_exactly_the_preview(env):
    server, fake = _server(env)
    new_tags = ["budget planner", "debt payoff plan"]
    pv = (await call(server, "seo_preview_update", {"listing_id": 10, "tags": new_tags, "title": "Budget Planner Spreadsheet"})).structured_content
    assert pv["valid"] and pv["preview_id"] and "score_after" in pv and pv["diff"]["tags"]["added"] == ["debt payoff plan"]
    assert not [c for c in fake.calls if c[0] == "PATCH"], "preview must not write"
    r = await call(server, "seo_apply_update", {"listing_id": 10, "preview_id": pv["preview_id"]})
    assert not r.is_error, r
    patches = [c for c in fake.calls if c[0] == "PATCH"]
    assert len(patches) == 1
    form = parse_qs(patches[0][2].decode())
    assert set(form) == {"title", "tags"} and form["tags"] == ["budget planner,debt payoff plan"]
    log = (await call(server, "etsy_write_log", {})).structured_content["entries"]
    assert log[0]["tool"] == "seo_apply_update" and log[0]["listing_id"] == 10
    again = await call(server, "seo_apply_update", {"listing_id": 10, "preview_id": pv["preview_id"]})
    assert again.is_error and "already applied" in again.content[0].text


async def test_invalid_preview_is_not_stored(env):
    server, _ = _server(env)
    pv = (await call(server, "seo_preview_update", {"listing_id": 10, "title": "x" * 150})).structured_content
    assert not pv["valid"] and pv["preview_id"] is None and "150 chars" in pv["errors"][0]


async def test_apply_refuses_unknown_and_expired(env, tmp_path):
    server, fake = _server(env)
    r = await call(server, "seo_apply_update", {"listing_id": 10, "preview_id": "nope"})
    assert r.is_error and "Unknown preview_id" in r.content[0].text
    pv = (await call(server, "seo_preview_update", {"listing_id": 10, "title": "Budget Planner Spreadsheet"})).structured_content
    past = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    with closing(sqlite3.connect(env[0].research_db)) as con, con:
        con.execute("UPDATE seo_previews SET expires_at=?", (past,))
    r = await call(server, "seo_apply_update", {"listing_id": 10, "preview_id": pv["preview_id"]})
    assert r.is_error and "expired" in r.content[0].text
    assert not [c for c in fake.calls if c[0] == "PATCH"]


async def test_apply_refuses_when_listing_drifted(env):
    server, fake = _server(env)
    pv = (await call(server, "seo_preview_update", {"listing_id": 10, "title": "Budget Planner Spreadsheet"})).structured_content
    fake.listing["title"] = "Edited elsewhere"
    r = await call(server, "seo_apply_update", {"listing_id": 10, "preview_id": pv["preview_id"]})
    assert r.is_error and "changed on Etsy" in r.content[0].text


async def test_apply_blocked_in_readonly_mode(env):
    server, fake = _server(env)
    pv = (await call(server, "seo_preview_update", {"listing_id": 10, "title": "Budget Planner Spreadsheet"})).structured_content
    s = env[0]
    s.mode = "readonly"
    r = await call(server, "seo_apply_update", {"listing_id": 10, "preview_id": pv["preview_id"]})
    assert r.is_error and "readonly" in r.content[0].text
    assert not [c for c in fake.calls if c[0] == "PATCH"]


# ------------------------------------------------------------------ migrations
def _tables(path):
    with closing(sqlite3.connect(path)) as con:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_v1_database_upgrades_to_v3(tmp_path):
    from indigorepublica_etsy_mcp.research import _V1, SCHEMA_VERSION, _split
    path = tmp_path / "v1.db"
    with closing(sqlite3.connect(path)) as con:
        for stmt in _split(_V1):
            con.execute(stmt)
        con.execute("PRAGMA user_version = 1")
        con.execute("INSERT INTO audits VALUES (1, 50, '{}', '2026-01-01T00:00:00+00:00')")
        con.commit()
    db = ResearchDB(path)
    assert SCHEMA_VERSION >= 3 and db.schema_version() == SCHEMA_VERSION
    assert {"competitors", "seo_previews"} <= _tables(path)
    assert db.stats()["tables"]["audits"]["rows"] == 1  # data kept
    pid, _ = seo.store_preview(db, 5, {"title": "a"}, {"title": "b"})
    assert seo.load_preview(db, 5, pid)["proposal"] == {"title": "b"}


def test_v2_database_upgrades_to_v3(tmp_path):
    from indigorepublica_etsy_mcp.research import _V1, _V2, SCHEMA_VERSION, _split
    path = tmp_path / "v2.db"
    with closing(sqlite3.connect(path)) as con:
        for stmt in _split(_V1) + _split(_V2):
            con.execute(stmt)
        con.execute("PRAGMA user_version = 2")
        con.execute("INSERT INTO competitors VALUES (11, 'Rival', 'x')")
        con.commit()
    assert "seo_previews" not in _tables(path)
    db = ResearchDB(path)
    assert db.schema_version() == SCHEMA_VERSION >= 3 and "seo_previews" in _tables(path)
    with closing(sqlite3.connect(path)) as con:
        assert con.execute("SELECT shop_name FROM competitors").fetchone()[0] == "Rival"
    ResearchDB(path)  # idempotent re-open


# ------------------------------------------------------------------ task 2's real snapshot format
async def test_tag_gaps_reads_snapshot_made_by_competitors_module(tmp_path):
    from indigorepublica_etsy_mcp.client import EtsyClient
    from indigorepublica_etsy_mcp.competitors import Competitors
    from indigorepublica_etsy_mcp.config import Settings
    from .test_competitors import FakePublicEtsy, _raw

    fake = FakePublicEtsy(listings={11: [_raw(1, tags=("debt snowball", "budget planner")), _raw(2, tags=("debt snowball", "bill tracker"))], 22: []})
    s = Settings(keystring="KEY", shared_secret="SECRET", token_file=tmp_path / "none.json", max_qps=1000)
    db = ResearchDB(tmp_path / "r.db")
    comp = Competitors(EtsyClient(s, transport=httpx.MockTransport(fake)), db)
    await comp.add("rival")
    await comp.snapshot("rival")
    db.save_market_listings([{"listing_id": 1, "tags": ["debt snowball", "budget planner"]}], "budget planner", "profittree")  # same listing as snapshot's #1

    r = seo.tag_gaps(db, ["budget planner"], "budget planner")
    assert r["data"]["competitor_snapshots"]["shops"] == 1 and r["data"]["competitor_snapshots"]["listings_with_tags"] == 1  # listing 1 not double counted
    assert r["listings_compared"] == 2
    got = {g["tag"]: g["used_by"] for g in r["gaps"]}
    assert got == {"debt snowball": 2, "bill tracker": 1}
