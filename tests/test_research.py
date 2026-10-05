"""Research cache + write log tests (offline, temp-dir SQLite)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from indigorepublica_etsy_mcp.research import SCHEMA_VERSION, ResearchDB, default_db_path
from indigorepublica_etsy_mcp.server import build_server

from .test_server import FakeEtsy, call, env  # noqa: F401  (env fixture)


@pytest.fixture()
def db(tmp_path):
    return ResearchDB(tmp_path / "sub" / "research.db")


def _age(db, table, col, days):
    ts = (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with closing(sqlite3.connect(db.path)) as con, con:
        con.execute(f"UPDATE {table} SET {col}=?", (ts,))


def test_schema_creation_and_idempotent_migration(db):
    with closing(sqlite3.connect(db.path)) as con:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cols = [r[1] for r in con.execute("PRAGMA table_info(keywords)")]
    assert {"keywords", "market_listings", "competitor_snapshots", "audits", "write_log"} <= tables
    assert cols == ["keyword", "source", "searches", "clicks", "competition", "digital_share", "trend", "niche_score", "raw_json", "fetched_at"]
    assert db.schema_version() == SCHEMA_VERSION == 1
    db.save_keywords([{"keyword": "x"}], "profittree")
    again = ResearchDB(db.path)  # re-open: migration is a no-op, data kept
    assert again.get_keywords(None, 7)["count"] == 1


def test_newer_schema_is_refused(db):
    with closing(sqlite3.connect(db.path)) as con:
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    with pytest.raises(RuntimeError, match="newer"):
        ResearchDB(db.path)


def test_env_var_overrides_path(monkeypatch, tmp_path):
    monkeypatch.setenv("ETSY_RESEARCH_DB", str(tmp_path / "custom.db"))
    assert default_db_path() == tmp_path / "custom.db"


def test_keywords_round_trip_and_replace(db):
    r = db.save_keywords([
        {"keyword": "  Budget  Planner ", "search_volume": "1,200", "clicks": 300, "competition": 5000, "digital_share": "80%",
         "trend": "rising", "niche_score": 7.5, "extra": {"a": 1}},
        {"keyword": "wedding seating chart", "searches": 900},
        {"nope": 1},
    ], "profittree")
    assert r["saved"] == 2 and r["skipped"][0]["index"] == 2
    out = db.get_keywords(None, 7)
    assert out["status"] == "fresh" and [x["keyword"] for x in out["rows"]] == ["budget planner", "wedding seating chart"]
    top = out["rows"][0]
    assert top["searches"] == 1200 and top["digital_share"] == 80 and top["trend"] == "rising" and top["raw"]["extra"] == {"a": 1}
    assert db.get_keywords("PLANNER", 7)["count"] == 1
    assert db.get_keywords("100%_", 7)["count"] == 0  # LIKE wildcards are escaped
    db.save_keywords([{"keyword": "budget planner", "searches": 5}], "profittree")
    assert db.get_keywords("budget", 7)["rows"][0]["searches"] == 5 and db.get_keywords(None, 7)["count"] == 2


def test_market_round_trip(db):
    r = db.save_market_listings([
        {"listing_id": 1, "shop_id": 9, "title": "A", "price": "4.99", "tags": "a, b", "views": 10, "num_favorers": 3,
         "est_monthly_sales": 20, "est_monthly_revenue": 100},
        {"listing_id": 2, "title": "B", "price": 10, "tags": ["x"], "est_monthly_revenue": 50},
        {"title": "no id"},
    ], "Budget Planner", "profittree")
    assert r["saved"] == 2 and len(r["skipped"]) == 1
    out = db.get_market("budget  planner", 7)
    assert out["status"] == "fresh" and out["count"] == 2
    first = out["rows"][0]
    assert first["listing_id"] == 1 and first["tags"] == ["a", "b"] and first["favorites"] == 3
    assert out["summary"] == {"listings": 2, "avg_price": 7.5, "total_est_monthly_revenue": 150.0}
    assert db.get_market("other", 7)["status"] == "missing"


def test_staleness_and_missing_messages(db):
    missing = db.get_keywords("zzz", 7)
    assert missing["status"] == "missing" and "No cached" in missing["message"] and "ProfitTree" in missing["message"]
    db.save_keywords([{"keyword": "old one"}], "profittree")
    _age(db, "keywords", "fetched_at", 10)
    db.save_keywords([{"keyword": "new one"}], "profittree")
    mixed = db.get_keywords(None, 7)
    assert mixed["status"] == "partial"
    assert {r["keyword"]: r["stale"] for r in mixed["rows"]} == {"old one": True, "new one": False}
    _age(db, "keywords", "fetched_at", 10)
    stale = db.get_keywords(None, 7)
    assert stale["status"] == "stale" and "older than 7" in stale["message"] and stale["rows"][0]["age_days"] >= 9.9
    assert db.get_keywords(None, 30)["status"] == "fresh"


def test_stats(db):
    assert db.stats()["tables"]["keywords"] == {"rows": 0, "oldest": None, "newest": None, "newest_age_days": None}
    db.save_keywords([{"keyword": "a"}], "profittree")
    st = db.stats()
    assert st["tables"]["keywords"]["rows"] == 1 and st["tables"]["keywords"]["oldest"] and st["schema_version"] == 1


def test_write_log_is_append_only(db):
    i = db.log_write("t", 5, "safe", False, {"a": 1}, {"b": 2}, "ok")
    db.log_write("t", None, "safe", True, None, None, "ok")
    rows = db.read_write_log(10)
    assert [r["id"] for r in rows] == [i + 1, i] and rows[1]["before"] == {"a": 1} and rows[0]["dry_run"] is True
    assert [r["id"] for r in db.read_write_log(10, listing_id=5)] == [i]
    with closing(sqlite3.connect(db.path)) as con:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute("DELETE FROM write_log")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute("UPDATE write_log SET result='x'")


# ----------------------------------------------------------------------------- through the MCP server
async def test_research_tools_via_mcp(env):  # noqa: F811
    s, fake, _ = env
    server = build_server(s, transport=httpx.MockTransport(fake))
    r = await call(server, "research_get_keywords", {"seed": "budget"})
    assert r.structured_content["status"] == "missing"
    r = await call(server, "research_save_keywords", {"rows": [{"keyword": "budget planner", "searches": 10}]})
    assert r.structured_content["saved"] == 1
    r = await call(server, "research_save_market_listings", {"rows": [{"listing_id": 1, "price": 5}], "keyword": "budget planner"})
    assert r.structured_content["saved"] == 1
    assert (await call(server, "research_get_keywords", {"seed": "budget"})).structured_content["status"] == "fresh"
    assert (await call(server, "research_get_market", {"keyword": "budget planner"})).structured_content["count"] == 1
    assert (await call(server, "research_cache_stats")).structured_content["tables"]["market_listings"]["rows"] == 1
    assert (await call(server, "research_save_keywords", {"rows": "bad"})).is_error
    assert not fake.calls, "research tools must not touch Etsy"


async def test_mocked_writes_are_logged(env):  # noqa: F811
    s, fake, products = env
    server = build_server(s, transport=httpx.MockTransport(fake))
    r = await call(server, "etsy_update_listing", {"listing_id": 901, "fields": {"title": "Budget Planner Spreadsheet Template for Google Sheets"}})
    assert not r.is_error, r.content
    r = await call(server, "etsy_publish_listing", {"listing_id": 901})  # rejected: no fee confirmation
    assert r.is_error
    r = await call(server, "etsy_delete_listing", {"listing_id": 901})  # blocked in safe mode
    assert r.is_error
    r = await call(server, "etsy_upload_listing_image", {"listing_id": 901, "file_path": str(products / "images" / "cover.png"), "rank": 1})
    assert not r.is_error
    log = (await call(server, "etsy_write_log", {})).structured_content["entries"]
    by_tool = {e["tool"]: e for e in reversed(log)}
    upd = by_tool["etsy_update_listing"]
    assert upd["result"] == "ok" and upd["mode"] == "safe" and upd["dry_run"] is False and upd["listing_id"] == 901
    assert upd["after"]["request"]["title"].startswith("Budget Planner") and upd["after"]["response"]["listing_id"] == 901
    assert upd["before"] is None  # fake Etsy has no GET /listings/901: logging degrades, doesn't fail
    assert by_tool["etsy_publish_listing"]["result"].startswith("error:") and "listing fee" in by_tool["etsy_publish_listing"]["result"]
    assert "safe mode" in by_tool["etsy_delete_listing"]["result"]
    img = by_tool["etsy_upload_listing_image"]
    assert img["after"]["request"]["bytes"] > 0 and img["after"]["response"]["rank"] == 1
    only = (await call(server, "etsy_write_log", {"listing_id": 999})).structured_content["entries"]
    assert only == []


async def test_digital_listing_workflow_logged_and_before_captured(env):  # noqa: F811
    s, fake, products = env
    server = build_server(s, transport=httpx.MockTransport(fake))
    r = await call(server, "etsy_create_digital_listing", {"manifest_path": str(products / "listing.json")})
    assert not r.is_error, r.content
    tools = [e["tool"] for e in reversed((await call(server, "etsy_write_log", {})).structured_content["entries"])]
    assert tools == ["etsy_create_draft_listing", "etsy_upload_listing_image", "etsy_upload_listing_file", "etsy_create_digital_listing"]


async def test_log_failure_never_breaks_tool(env, monkeypatch):  # noqa: F811
    s, fake, _ = env
    server = build_server(s, transport=httpx.MockTransport(fake))
    monkeypatch.setattr(ResearchDB, "log_write", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))
    r = await call(server, "etsy_create_shop_section", {"title": "X"})
    assert r.is_error  # fake has no route -> real error surfaces, not the logging error
    assert "disk full" not in r.content[0].text


async def test_readonly_mode_blocked_write_is_logged_and_cache_still_works(env):  # noqa: F811
    s, fake, _ = env
    s.mode = "readonly"
    server = build_server(s, transport=httpx.MockTransport(fake))
    assert (await call(server, "etsy_create_shop_section", {"title": "X"})).is_error
    e = (await call(server, "etsy_write_log", {})).structured_content["entries"][0]
    assert e["mode"] == "readonly" and "readonly" in e["result"]
    assert not (await call(server, "research_save_keywords", {"rows": [{"keyword": "k"}]})).is_error


async def test_before_state_captured_when_available(env):  # noqa: F811
    s, fake, _ = env

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/v3/application/listings/901":
            return httpx.Response(200, json={"listing_id": 901, "title": "Old title", "tags": ["old"], "views": 3})
        return fake(req)

    server = build_server(s, transport=httpx.MockTransport(handler))
    r = await call(server, "etsy_update_listing", {"listing_id": 901, "fields": {"title": "Budget Planner Spreadsheet Template for Google Sheets"}})
    assert not r.is_error, r.content
    e = (await call(server, "etsy_write_log", {"listing_id": 901})).structured_content["entries"][0]
    assert e["before"] == {"title": "Old title"}  # only the fields being changed


# ----------------------------------------------------------------------------- log hygiene
def _dump_log(path) -> str:
    with closing(sqlite3.connect(path)) as con:
        return "\n".join(str(tuple(r)) for r in con.execute("SELECT * FROM write_log"))


async def test_write_log_never_stores_credentials(env):  # noqa: F811
    s, fake, products = env
    server = build_server(s, transport=httpx.MockTransport(fake))
    r = await call(server, "etsy_api_request", {
        "method": "POST", "path": "/shops/{shop_id}/sections",
        "form": {"title": "X", "api_key": "KEY:SECRET", "Authorization": "Bearer 77.old", "client_token": "tok-123",
                 "note": "leaks Bearer 77.fresh and SECRET in text"},
        "json_body": {"nested": {"x-api-key": "KEY:SECRET", "shared_secret": "SECRET"}}})
    assert r.is_error  # fake has no route; the failure result goes into the log too
    await call(server, "etsy_update_listing", {"listing_id": 901, "fields": {"title": "Budget Planner Spreadsheet Template for Google Sheets"}})
    stored = _dump_log(s.research_db)
    assert "[REDACTED]" in stored
    for secret in ("KEY:SECRET", "SECRET", "77.old", "77.fresh", "77.r1", "77.r2", "tok-123", "t0ken"):
        assert secret not in stored, secret
    for secret in ("Bearer 77",):
        assert secret not in stored, secret
    assert "x-api-key" not in stored.lower() or "[REDACTED]" in stored


async def test_upload_log_holds_only_filename_size_type(env):  # noqa: F811
    s, fake, products = env
    server = build_server(s, transport=httpx.MockTransport(fake))
    f = products / "files" / "Budget.xlsx"
    r = await call(server, "etsy_upload_listing_file", {"listing_id": 901, "file_path": str(f), "name": "Shown name", "rank": 2})
    assert not r.is_error, r.content
    img = products / "images" / "cover.png"
    r = await call(server, "etsy_upload_listing_image", {"listing_id": 901, "file_path": str(img), "alt_text": "alt", "rank": 1})
    assert not r.is_error, r.content
    entries = {e["tool"]: e for e in (await call(server, "etsy_write_log", {})).structured_content["entries"]}
    file_row, img_row = entries["etsy_upload_listing_file"], entries["etsy_upload_listing_image"]
    assert file_row["after"]["request"] == {"filename": "Budget.xlsx", "bytes": len(f.read_bytes()),
                                            "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    assert img_row["after"]["request"] == {"filename": "cover.png", "bytes": len(img.read_bytes()), "mime": "image/png"}
    stored = _dump_log(s.research_db)
    for needle in ("fake-xlsx", "PK\\x03", "PNG", str(products), "0000000000"):
        assert needle not in stored, needle
    # base64 uploads: content never logged either
    import base64
    b64 = base64.b64encode(b"SECRETPAYLOAD" * 10).decode()
    await call(server, "etsy_upload_listing_file", {"listing_id": 901, "file_base64": b64, "filename": "x.pdf"})
    stored = _dump_log(s.research_db)
    assert b64[:20] not in stored and "SECRETPAYLOAD" not in stored and "x.pdf" in stored


def test_redact_helper_masks_names_bytes_and_known_secrets():
    from indigorepublica_etsy_mcp.research import redact

    out = redact({"Authorization": "x", "a": {"X-Api-Key": "k", "refresh_token": "t", "client_secret": "s"}, "data": b"abc",
                  "keywords": "kept", "msg": "has TOPSECRET inside", "list": [{"auth_header": 1}]}, ("TOPSECRET",))
    assert out == {"Authorization": "[REDACTED]", "a": {"X-Api-Key": "[REDACTED]", "refresh_token": "[REDACTED]", "client_secret": "[REDACTED]"},
                   "data": "[3 bytes omitted]", "keywords": "kept", "msg": "has [REDACTED] inside", "list": [{"auth_header": "[REDACTED]"}]}


# ----------------------------------------------------------------------------- ProfitTree field names
def test_profittree_shaped_rows(db):
    r = db.save_keywords([{"keyword": "Budget Planner", "avg_monthly_searches": 12000, "avg_monthly_clicks": 4300, "competition": 8100,
                           "digital_share": 0.82, "trend_direction": "up", "niche_score": 71.5}], "profittree")
    assert r["saved"] == 1
    k = db.get_keywords(None, 7)["rows"][0]
    assert (k["searches"], k["clicks"], k["competition"], k["digital_share"], k["trend"], k["niche_score"]) == (12000, 4300, 8100, 0.82, "up", 71.5)
    assert k["raw"]["trend_direction"] == "up"
    db.save_keywords([{"keyword": "fallback", "search_volume": 5, "trend": "flat"}], "profittree")  # old aliases still work
    fb = [x for x in db.get_keywords("fallback", 7)["rows"]][0]
    assert fb["searches"] == 5 and fb["trend"] == "flat"
    db.save_market_listings([{"listing_id": 123, "title": "Budget Planner", "price": 6.5, "views": 800, "favorites": 40,
                              "monthly_sales": 55, "monthly_revenue": 357.5, "shop_id": 77, "shop_name": "SomeShop"}], "budget planner", "profittree")
    m = db.get_market("budget planner", 7)["rows"][0]
    assert (m["listing_id"], m["shop_id"], m["price"], m["views"], m["favorites"], m["est_monthly_sales"], m["est_monthly_revenue"]) == \
        (123, 77, 6.5, 800, 40, 55, 357.5)
