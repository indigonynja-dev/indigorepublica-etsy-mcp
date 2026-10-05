"""Resource tests: every etsy:// resource read through a real MCP client session. Fully offline (mocked Etsy, temp SQLite)."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from mcp import Client, MCPError
from mcp.server.mcpserver.exceptions import ResourceError

from indigorepublica_etsy_mcp import resources, seo
from indigorepublica_etsy_mcp.client import EtsyClient
from indigorepublica_etsy_mcp.competitors import Competitors
from indigorepublica_etsy_mcp.research import ResearchDB
from indigorepublica_etsy_mcp.server import build_server

from .fakes import LISTINGS_PATH, ShopFake, listing_row
from .test_competitors import FakePublicEtsy, _raw
from .test_seo import SeoFake
from .test_server import SHOP, call, env  # noqa: F401  (env fixture)

STATIC_URIS = {"etsy://shop/listings", "etsy://writes/recent"}
TEMPLATE_URIS = {"etsy://keywords/{seed}", "etsy://competitor/{shop}", "etsy://audit/{listing_id}"}
NOT_FOUND = -32602  # JSON-RPC invalid params: the SDK's code for "no such resource"
INTERNAL = -32603


async def read(client: Client, uri: str) -> dict:
    res = await client.read_resource(uri)
    assert len(res.contents) == 1 and res.contents[0].mime_type == "application/json"
    return json.loads(res.contents[0].text)


async def read_error(client: Client, uri: str) -> MCPError:
    with pytest.raises(MCPError) as e:
        await client.read_resource(uri)
    return e.value


def _age(db: ResearchDB, table: str, col: str, days: float) -> None:
    ts = (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with closing(sqlite3.connect(db.path)) as con, con:
        con.execute(f"UPDATE {table} SET {col}=?", (ts,))


async def _watched_rival(settings) -> ResearchDB:
    """Watch + snapshot a competitor with the real competitors module (what competitor_add/competitor_snapshot do)."""
    fake = FakePublicEtsy({11: [_raw(i, tags=("planner", "budget") if i % 2 else ("planner",), cents=500 + i) for i in range(1, 5)], 22: []})
    db = ResearchDB(settings.research_db)
    comp = Competitors(EtsyClient(settings, transport=httpx.MockTransport(fake)), db)
    await comp.add("rival")
    await comp.add("other")
    await comp.snapshot("rival")
    return db


# ----------------------------------------------------------------------------- inventory
async def test_resource_inventory(env):
    s, fake, _ = env
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        listed = (await c.list_resources()).resources
        templates = (await c.list_resource_templates()).resource_templates
        assert c.server_capabilities.resources is not None
    assert {str(r.uri) for r in listed} == STATIC_URIS
    assert {t.uri_template for t in templates} == TEMPLATE_URIS
    assert all(x.mime_type == "application/json" and x.description and x.title for x in [*listed, *templates])
    assert not fake.calls, "listing resources must not touch Etsy"


@pytest.mark.parametrize("uri", ["etsy://nope", "etsy://shop/other", "etsy://writes/recent/extra", "etsy://competitor/..", "etsy://keywords/%2E%2E",
                                 "etsy://keywords/a/b"])
async def test_unknown_uris_are_not_found(env, uri):
    s, fake, _ = env
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        assert (await read_error(c, uri)).code == NOT_FOUND


# ----------------------------------------------------------------------------- etsy://shop/listings
async def test_shop_listings_shape_order_and_cache(env):
    s, _, _ = env
    fake = ShopFake([listing_row(1, 1_760_000_000), listing_row(2, 1_780_000_000), listing_row(3, 1_770_000_000)])
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        first = await read(c, "etsy://shop/listings")
        second = await read(c, "etsy://shop/listings")
    assert list(first["listings"][0]) == list(resources.LISTING_FIELDS)  # id, title, price, state, last updated (+ currency)
    assert [x["listing_id"] for x in first["listings"]] == [2, 3, 1]  # most recently updated first
    top = first["listings"][0]
    assert top == {"listing_id": 2, "title": "Listing 2", "price": 2.99, "currency": "USD", "state": "active",
                   "updated": "2026-05-28T20:26:40+00:00"}  # epoch 1_780_000_000, checked against `date -u -d @1780000000`
    assert (first["shop_id"], first["count"], first["returned"], first["truncated"]) == (SHOP, 3, 3, False)
    assert first["cached"] is False and second["cached"] is True
    assert {k: v for k, v in first.items() if k not in ("cached", "cache_age_seconds")} == \
           {k: v for k, v in second.items() if k not in ("cached", "cache_age_seconds")}
    assert fake.count("GET", LISTINGS_PATH) == 1, "two reads must cost one Etsy page"


async def test_shop_listings_is_capped_and_says_so(env):
    s, _, _ = env
    fake = ShopFake([listing_row(i) for i in range(1, 1201)])
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        r = await read(c, "etsy://shop/listings")
    assert (r["count"], r["returned"], r["truncated"]) == (1200, resources.MAX_LISTINGS, True)
    assert fake.count("GET", LISTINGS_PATH) == 5  # 500 listings at 100 per page


async def test_shop_listings_cache_is_dropped_by_a_write(env):
    s, _, _ = env
    fake = ShopFake()
    server = build_server(s, transport=httpx.MockTransport(fake))
    async with Client(server) as c:
        assert (await read(c, "etsy://shop/listings"))["cached"] is False
        assert (await read(c, "etsy://shop/listings"))["cached"] is True
        r = await c.call_tool("etsy_update_listing", {"listing_id": 1, "fields": {"title": "Budget Planner Spreadsheet Template for Google Sheets"}})
        assert not r.is_error, r.content
        again = await read(c, "etsy://shop/listings")
    assert again["cached"] is False and fake.count("GET", LISTINGS_PATH) == 2, "the next read after a write must go back to Etsy"


async def test_shop_listings_cache_is_dropped_even_by_a_refused_write(env):
    s, _, _ = env
    fake = ShopFake()
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        await read(c, "etsy://shop/listings")
        assert (await c.call_tool("etsy_delete_listing", {"listing_id": 1})).is_error  # blocked in safe mode
        assert (await read(c, "etsy://shop/listings"))["cached"] is False  # cheap and safe: refetch rather than guess


@pytest.mark.parametrize("status, headers, expect", [
    (403, {}, ["Etsy API 403", "Hint"]),
    (500, {"retry-after": "0"}, ["Etsy API 500"]),  # retried by the client (instantly, thanks to retry-after: 0), then reported
])
async def test_shop_listings_reports_etsy_errors_and_does_not_cache_them(env, status, headers, expect):
    s, _, _ = env
    fake = ShopFake()
    fake.fault("GET", LISTINGS_PATH, status, headers=headers)
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        e = await read_error(c, "etsy://shop/listings")
        assert e.code == INTERNAL and all(x in str(e) for x in expect)
        fake.clear_faults()
        ok = await read(c, "etsy://shop/listings")
    assert ok["cached"] is False and ok["returned"] == 2, "a failed read must not be cached"


async def test_shop_listings_explains_missing_tokens(env):
    s, fake, _ = env
    s.token_file.unlink()
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        e = await read_error(c, "etsy://shop/listings")
    assert e.code == INTERNAL and "indigorepublica-etsy-auth" in str(e)


async def test_shop_listings_explains_missing_credentials(env):
    s, fake, _ = env
    nokeys = dataclasses.replace(s, keystring="", shared_secret="")
    async with Client(build_server(nokeys, transport=httpx.MockTransport(fake))) as c:
        e = await read_error(c, "etsy://shop/listings")
    assert e.code == INTERNAL and "ETSY_KEYSTRING" in str(e) and "ETSY_SHARED_SECRET" in str(e)  # not a generic "error reading resource"
    assert not fake.calls


# ----------------------------------------------------------------------------- ListingsCache
async def test_listings_cache_ttl_invalidate_and_shared_fetch():
    now = [0.0]
    cache = resources.ListingsCache(ttl=60, clock=lambda: now[0])
    fetched: list[float] = []

    async def fetch():
        fetched.append(now[0])
        await asyncio.sleep(0)
        return {"n": len(fetched)}

    assert await cache.get(fetch) == {"n": 1, "cached": False, "cache_age_seconds": 0}
    now[0] = 59
    assert await cache.get(fetch) == {"n": 1, "cached": True, "cache_age_seconds": 59}
    now[0] = 60  # age == ttl is expired
    assert (await cache.get(fetch))["n"] == 2
    cache.invalidate()
    assert (await cache.get(fetch))["n"] == 3
    cache.invalidate()
    results = await asyncio.gather(*(cache.get(fetch) for _ in range(5)))
    assert len(fetched) == 4 and {r["n"] for r in results} == {4}, "concurrent readers must share one fetch"


async def test_listings_cache_drops_a_read_that_a_write_overtook():
    cache = resources.ListingsCache(ttl=60)
    started, release = asyncio.Event(), asyncio.Event()

    async def slow():
        started.set()
        await release.wait()
        return {"v": "before the write"}

    task = asyncio.create_task(cache.get(slow))
    await started.wait()
    cache.invalidate()  # a write lands while the read is in flight
    release.set()
    assert (await task)["v"] == "before the write"  # the reader still gets its answer...

    async def fresh():
        return {"v": "after the write"}

    assert (await cache.get(fresh))["v"] == "after the write"  # ...but it was not kept


async def test_listings_cache_never_keeps_a_failure():
    cache = resources.ListingsCache(ttl=60)

    async def boom():
        raise ResourceError("down")

    with pytest.raises(ResourceError):
        await cache.get(boom)

    async def ok():
        return {"v": 1}

    assert (await cache.get(ok))["cached"] is False


# ----------------------------------------------------------------------------- etsy://keywords/{seed}
async def test_keywords_resource(env):
    s, fake, _ = env
    ResearchDB(s.research_db).save_keywords([
        {"keyword": "budget planner", "avg_monthly_searches": 12000, "niche_score": 71.5, "trend_direction": "up"},
        {"keyword": "budget tracker", "searches": 800, "niche_score": 40},
        {"keyword": "wedding seating chart", "searches": 900}], "profittree")
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        r = await read(c, "etsy://keywords/budget")
        assert (r["seed"], r["status"], r["count"]) == ("budget", "fresh", 2)
        assert [x["keyword"] for x in r["rows"]] == ["budget planner", "budget tracker"]  # best niche_score first
        top = r["rows"][0]
        assert top["searches"] == 12000 and top["trend"] == "up" and top["stale"] is False and top["age_days"] < 0.01
        tool = (await c.call_tool("research_get_keywords", {"seed": "budget"})).structured_content
        assert tool["rows"] == r["rows"], "the resource and the tool must agree"
        assert (await read(c, "etsy://keywords/Budget%20Planner"))["count"] == 1  # percent-encoding and case
        assert (await read(c, "etsy://keywords/budget planner"))["count"] == 1
        miss = await read(c, "etsy://keywords/zzz")  # nothing cached is a normal state: in-band status, with the next step
        assert miss["status"] == "missing" and miss["rows"] == [] and "keyword_finder" in miss["message"]
        assert (await read(c, "etsy://keywords/100%25"))["count"] == 0  # LIKE wildcards are data, not patterns
    assert not fake.calls, "keyword resource must not touch Etsy"


async def test_keywords_resource_flags_stale_rows(env):
    s, fake, _ = env
    db = ResearchDB(s.research_db)
    db.save_keywords([{"keyword": "old one"}], "profittree")
    _age(db, "keywords", "fetched_at", 10)
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        r = await read(c, "etsy://keywords/old")
    assert r["status"] == "stale" and r["rows"][0]["stale"] is True and r["rows"][0]["age_days"] >= 9.9


@pytest.mark.parametrize("uri", ["etsy://keywords/", "etsy://keywords/%20", "etsy://keywords/%20%20"])
async def test_keywords_resource_needs_a_seed(env, uri):
    s, fake, _ = env
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        e = await read_error(c, uri)
    assert e.code == NOT_FOUND and "seed" in str(e)


# ----------------------------------------------------------------------------- etsy://competitor/{shop}
async def test_competitor_resource(env):
    s, fake, _ = env
    await _watched_rival(s)
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        p = await read(c, "etsy://competitor/Rival")
        assert (p["shop_id"], p["shop_name"], p["listing_count"]) == (11, "Rival", 4)
        assert p["top_tags"][0] == {"tag": "planner", "count": 4} and p["price"]["min"] == 5.01 and p["price"]["max"] == 5.04
        assert 0 <= p["snapshot_age_days"] < 0.01
        assert await read(c, "etsy://competitor/rival") == p  # case-insensitive name
        assert (await read(c, "etsy://competitor/11"))["shop_name"] == "Rival"  # or numeric id
        tool = (await c.call_tool("competitor_profile", {"shop": "rival"})).structured_content
        assert {k: v for k, v in p.items() if k != "snapshot_age_days"} == tool, "the resource and the tool must agree"
        e = await read_error(c, "etsy://competitor/other")  # watched but never snapshotted
        assert e.code == NOT_FOUND and "No snapshots" in str(e) and "competitor_snapshot" in str(e)
        e = await read_error(c, "etsy://competitor/nobody")
        assert e.code == NOT_FOUND and "watchlist" in str(e) and "competitor_add" in str(e)
    assert not fake.calls, "competitor resource must not touch Etsy"


# ----------------------------------------------------------------------------- etsy://audit/{listing_id}
async def test_audit_resource_returns_the_latest_report(env):
    s, fake, _ = env
    db = ResearchDB(s.research_db)
    long_ago = (datetime.now(tz=timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
    with closing(sqlite3.connect(db.path)) as con, con:
        con.execute("INSERT INTO audits VALUES (10, 40, ?, ?)", (json.dumps({"listing_id": 10, "score": 40, "fixes": []}), long_ago))
    seo.store_audit(db, {"listing_id": 10, "score": 82.5, "main_keyword": "budget planner", "sub_scores": {},
                         "fixes": [{"area": "tags", "impact": 5, "fix": "Fill all 13 tags", "priority": 1}], "notes": []})
    seo.store_audit(db, {"listing_id": 11, "score": 10, "sub_scores": {}, "fixes": [], "notes": []})
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        a = await read(c, "etsy://audit/10")
        assert (a["listing_id"], a["score"], a["main_keyword"]) == (10, 82.5, "budget planner")
        assert a["fixes"][0]["fix"] == "Fill all 13 tags" and a["audited_at"] and 0 <= a["age_days"] < 0.01
        e = await read_error(c, "etsy://audit/99")
        assert e.code == NOT_FOUND and "seo_audit" in str(e) and "99" in str(e)
    assert not fake.calls, "audit resource must not touch Etsy"


async def test_audit_resource_serves_what_the_seo_audit_tool_stored(env):
    s, _, _ = env
    async with Client(build_server(s, transport=httpx.MockTransport(SeoFake()))) as c:
        assert (await read_error(c, "etsy://audit/10")).code == NOT_FOUND  # nothing stored yet
        report = (await c.call_tool("seo_audit", {"listing_id": 10})).structured_content
        a = await read(c, "etsy://audit/10")
    assert a["score"] == report["score"] and a["fixes"] == report["fixes"] and a["sub_scores"] == report["sub_scores"]
    assert a["main_keyword"] == report["main_keyword"] == "budget planner"


@pytest.mark.parametrize("bad", ["abc", "1.5", "-3", "%20", "1e3", "%D9%A1%D9%A2", "%C2%B2", "9" * 19])
async def test_audit_resource_rejects_malformed_ids_cleanly(env, bad):
    s, fake, _ = env
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        e = await read_error(c, f"etsy://audit/{bad}")
    assert e.code == NOT_FOUND and "listing_id must be a number" in str(e)  # a clear message, not "Error creating resource"


def test_latest_audit_helper(tmp_path):
    db = ResearchDB(tmp_path / "r.db")
    assert seo.latest_audit(db, 5) is None
    with closing(sqlite3.connect(db.path)) as con, con:
        con.execute("INSERT INTO audits VALUES (5, 50, '{}', '2026-01-01T00:00:00+00:00')")  # partial legacy row
    legacy = seo.latest_audit(db, 5)
    assert legacy["listing_id"] == 5 and legacy["score"] == 50 and legacy["audited_at"] == "2026-01-01T00:00:00+00:00"
    seo.store_audit(db, {"listing_id": 5, "score": 70})
    seo.store_audit(db, {"listing_id": 5, "score": 90})  # same second: the later insert still wins
    assert seo.latest_audit(db, 5)["score"] == 90


# ----------------------------------------------------------------------------- etsy://writes/recent
async def test_recent_writes_is_the_last_50_newest_first(env):
    s, fake, _ = env
    db = ResearchDB(s.research_db)
    server = build_server(s, transport=httpx.MockTransport(fake))
    async with Client(server) as c:
        empty = await read(c, "etsy://writes/recent")
        assert (empty["count"], empty["entries"]) == (0, [])
        for i in range(1, 56):
            db.log_write("etsy_update_listing", i, "safe", False, {"n": i}, {"request": {"n": i}}, "ok")
        w = await read(c, "etsy://writes/recent")
        assert (w["limit"], w["count"]) == (50, 50)
        assert [e["listing_id"] for e in w["entries"]] == list(range(55, 5, -1))
        assert w["entries"][0]["before"] == {"n": 55} and w["entries"][0]["dry_run"] is False and w["entries"][0]["mode"] == "safe"
        assert w["entries"] == (await c.call_tool("etsy_write_log", {})).structured_content["entries"], "same rows as the tool"


async def test_recent_writes_shows_blocked_attempts_and_never_credentials(env):
    s, fake, _ = env
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        r = await c.call_tool("etsy_api_request", {"method": "POST", "path": "/shops/{shop_id}/sections",
                                                   "form": {"title": "X", "api_key": "KEY:SECRET", "note": "Bearer 77.old and SECRET"}})
        assert r.is_error  # the fake has no route; the failed attempt is logged
        assert (await c.call_tool("etsy_delete_listing", {"listing_id": 1})).is_error  # blocked in safe mode: logged too
        text = (await c.read_resource("etsy://writes/recent")).contents[0].text
    entries = json.loads(text)["entries"]
    assert [e["tool"] for e in entries] == ["etsy_delete_listing", "etsy_api_request"]
    assert "safe mode" in entries[0]["result"] and entries[0]["mode"] == "safe"
    assert "[REDACTED]" in text
    for secret in ("KEY:SECRET", "SECRET", "77.old", "Bearer 77"):
        assert secret not in text, secret


# ----------------------------------------------------------------------------- read-only guarantee
def _fingerprint(*paths) -> list[tuple[str, int]]:
    return [(hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in paths]


async def test_reading_resources_never_writes_anything_in_any_mode(env):
    s, _, _ = env
    s.shop_id = str(SHOP)  # resolved up front, so the first read has no reason to touch the token file either
    db = await _watched_rival(s)
    db.save_keywords([{"keyword": "budget planner", "searches": 10}], "profittree")
    db.save_market_listings([{"listing_id": 1, "tags": ["a b"]}], "budget planner", "profittree")
    seo.store_audit(db, {"listing_id": 10, "score": 80})
    db.log_write("etsy_update_listing", 10, "safe", False, None, None, "ok")
    uris = [*STATIC_URIS, "etsy://keywords/budget", "etsy://keywords/nothing", "etsy://competitor/rival", "etsy://competitor/other",
            "etsy://competitor/nobody", "etsy://audit/10", "etsy://audit/99", "etsy://audit/abc", "etsy://keywords/"]
    for mode in ("readonly", "safe", "full"):
        s.mode = mode
        fake = ShopFake()
        server = build_server(s, transport=httpx.MockTransport(fake))
        before = _fingerprint(s.research_db, s.token_file)
        async with Client(server) as c:
            for uri in uris * 2:  # twice: the second pass is served from caches
                try:
                    await c.read_resource(uri)
                except MCPError:
                    pass
        assert _fingerprint(s.research_db, s.token_file) == before, f"a resource wrote to disk in {mode} mode"
        assert fake.seen == [("GET", LISTINGS_PATH)], "exactly one Etsy request, and it is a GET"
        assert len(db.read_write_log(500)) == 1, "no new write-log rows"


async def test_resources_are_not_exposed_as_tools(env):
    s, fake, _ = env
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert not any(n.startswith(("resource", "etsy_resource")) for n in names)
    assert len(names) == 52, "adding resources must not change the tool count"
