"""Integration tests: the whole server driven through real MCP sessions (in-process, Streamable HTTP, and a real stdio subprocess).

Everything is offline: Etsy is a httpx.MockTransport fake, the research cache is a temp SQLite file, no real keys.
"""

from __future__ import annotations

import json
import sys
import threading
import time

import httpx
from mcp import Client, StdioServerParameters, stdio_client

from indigorepublica_etsy_mcp import __version__, seo
from indigorepublica_etsy_mcp.research import ResearchDB
from indigorepublica_etsy_mcp.server import build_http_app, build_server

from .fakes import FullFake, ShopFake
from .test_resources import NOT_FOUND, read, read_error
from .test_server import _free_port, env  # noqa: F401  (env fixture)

# The 18 tools added after the original 35: research cache + write log + pruning, competitor tracking, SEO.
NEW_TOOLS = {
    "research_save_keywords", "research_save_market_listings", "research_get_keywords", "research_get_market", "research_cache_stats", "research_prune",
    "etsy_write_log",
    "competitor_add", "competitor_remove", "competitor_list", "competitor_snapshot", "competitor_snapshot_all", "competitor_diff",
    "competitor_profile",
    "seo_audit", "seo_tag_gaps", "seo_preview_update", "seo_apply_update",
}
VOLATILE_PROFILE = {"snapshot_age_days", "snapshot_age_hours", "snapshot_age", "refresh"}
RESOURCES = {"etsy://shop/listings", "etsy://writes/recent", "etsy://keywords/{seed}", "etsy://competitor/{shop}", "etsy://audit/{listing_id}"}


async def test_handshake_advertises_tools_prompts_and_resources(env):
    s, fake, _ = env
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        assert c.server_info.name == "indigorepublica-etsy" and c.server_info.version == __version__
        caps = c.server_capabilities
        assert caps.tools is not None and caps.prompts is not None and caps.resources is not None
        for must in ("etsy://shop/listings", "etsy_write_log", "research_save_*"):
            assert must in c.instructions, must
        names = {t.name for t in (await c.list_tools()).tools}
        listed = {str(r.uri) for r in (await c.list_resources()).resources}
        listed |= {t.uri_template for t in (await c.list_resource_templates()).resource_templates}
    assert NEW_TOOLS <= names and len(names - NEW_TOOLS) == 36, "35 original tools + etsy_upload_listing_video + the 18 newer ones"
    assert listed == RESOURCES


async def test_every_new_tool_and_resource_through_one_mcp_session(env):
    s, _, _ = env
    fake = FullFake()
    called: set[str] = set()

    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        async def tool(name: str, args: dict | None = None) -> dict:
            r = await c.call_tool(name, args or {})
            assert not r.is_error, (name, r.content)
            called.add(name)
            return r.structured_content

        # ---- research cache (ProfitTree data that Claude passes in)
        saved = await tool("research_save_keywords", {"rows": [{"keyword": "budget planner", "avg_monthly_searches": 12000, "niche_score": 71.5},
                                                              {"keyword": "bill tracker", "searches": 5000}]})
        assert saved["saved"] == 2
        saved = await tool("research_save_market_listings", {"keyword": "budget planner", "rows": [
            {"listing_id": 501, "shop_name": "Rival", "title": "Budget Planner", "price": 6.5, "est_monthly_revenue": 300,
             "tags": ["budget planner", "debt snowball", "bill tracker"]}]})
        assert saved["saved"] == 1
        kw = await tool("research_get_keywords", {"seed": "budget"})
        assert kw["status"] == "fresh" and kw["count"] == 1
        mk = await tool("research_get_market", {"keyword": "budget planner"})
        assert mk["count"] == 1 and mk["summary"]["total_est_monthly_revenue"] == 300

        # ---- competitor tracking (public Etsy endpoints)
        assert (await tool("competitor_add", {"shop_name_or_id": "rival"}))["shop_id"] == 11
        assert (await tool("competitor_snapshot", {"shop": "rival"}))["listing_count"] == 4
        all_ = await tool("competitor_snapshot_all", {"delay_seconds": 0})
        assert (all_["watched"], all_["ok"], all_["failed"]) == (1, 1, 0)
        assert (await tool("competitor_list"))["shops"][0]["snapshots"] == 2
        diff = await tool("competitor_diff", {"shop": "rival"})
        assert diff["summary"]["total_changes"] == 0 and diff["summary"]["listings_after"] == 4
        profile = await tool("competitor_profile", {"shop": "rival"})
        assert profile["listing_count"] == 4 and profile["top_tags"][0] == {"tag": "planner", "count": 4}
        assert {k: v for k, v in (await read(c, "etsy://competitor/rival")).items() if k not in VOLATILE_PROFILE} == {k: v for k, v in profile.items() if k not in VOLATILE_PROFILE}

        # ---- SEO: audit -> tag gaps -> preview -> apply
        audit = await tool("seo_audit", {"listing_id": 10})
        assert 0 < audit["score"] <= 100 and audit["fixes"]
        stored = await read(c, "etsy://audit/10")
        assert (stored["score"], stored["fixes"]) == (audit["score"], audit["fixes"]) and stored["audited_at"]
        gaps = await tool("seo_tag_gaps", {"listing_id": 10, "keyword": "budget planner"})
        assert [g["tag"] for g in gaps["gaps"][:4]] == ["planner", "budget", "bill tracker", "debt snowball"]
        assert gaps["listings_compared"] == 5 and gaps["gaps"][2]["search_volume"] == 5000  # cache + competitor snapshot, ranked by use then volume
        pv = await tool("seo_preview_update", {"listing_id": 10, "title": "Budget Planner Spreadsheet Template for Google Sheets"})
        assert pv["valid"] and pv["preview_id"] and pv["diff"]["title"]["changed"]
        applied = await tool("seo_apply_update", {"listing_id": 10, "preview_id": pv["preview_id"]})
        assert applied["applied"] is True and applied["fields"] == ["title"]

        # ---- write log + cache stats, and the resources that show the same data
        log = (await tool("etsy_write_log"))["entries"]
        assert [e["tool"] for e in log] == ["seo_apply_update"] and log[0]["result"] == "ok" and log[0]["listing_id"] == 10
        assert (await read(c, "etsy://writes/recent"))["entries"] == log
        assert (await tool("research_prune"))["dry_run"] is True
        stats = (await tool("research_cache_stats"))["tables"]
        assert (stats["keywords"]["rows"], stats["market_listings"]["rows"], stats["competitor_snapshots"]["rows"],
                stats["audits"]["rows"], stats["write_log"]["rows"]) == (2, 1, 2, 1, 1)
        assert (await read(c, "etsy://keywords/budget"))["rows"] == kw["rows"]
        shop = await read(c, "etsy://shop/listings")
        assert (shop["shop_id"], shop["returned"], shop["cached"]) == (4242, 2, False)

        # ---- un-watching keeps the snapshots: the watchlist name stops resolving, the numeric id still does
        removed = await tool("competitor_remove", {"shop": "rival"})
        assert removed["removed"] == 11 and "snapshots were kept" in removed["note"]
        assert (await read_error(c, "etsy://competitor/rival")).code == NOT_FOUND
        assert (await read(c, "etsy://competitor/11"))["listing_count"] == 4

    assert called == NEW_TOOLS, f"not exercised through MCP: {sorted(NEW_TOOLS - called)}"
    assert [m for m, _ in fake.shop.seen if m != "GET"] == ["PATCH"], "the only Etsy write is the approved SEO update"
    assert {r.method for r in fake.public.calls} == {"GET"}


# ----------------------------------------------------------------------------- Streamable HTTP (Claude.ai)
def test_resources_over_streamable_http_need_the_bearer_token(env):
    import uvicorn

    s, _, _ = env
    s.shop_id = "4242"
    ResearchDB(s.research_db).save_keywords([{"keyword": "budget planner", "searches": 10}], "profittree")
    port = _free_port()
    app = build_http_app(s, build_server(s, transport=httpx.MockTransport(ShopFake())))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    url = f"http://127.0.0.1:{port}/mcp"
    hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    auth = {**hdr, "Authorization": "Bearer t0ken"}

    def rpc(method: str, params: dict | None = None, headers: dict | None = None) -> httpx.Response:
        return httpx.post(url, headers=headers or auth, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})

    try:
        assert rpc("resources/list", headers=hdr).status_code == 401
        assert rpc("resources/read", {"uri": "etsy://writes/recent"}, headers={**hdr, "Authorization": "Bearer wrong"}).status_code == 401
        listed = rpc("resources/list")
        assert listed.status_code == 200
        assert {r["uri"] for r in listed.json()["result"]["resources"]} == {"etsy://shop/listings", "etsy://writes/recent"}
        templates = rpc("resources/templates/list").json()["result"]["resourceTemplates"]
        assert {t["uriTemplate"] for t in templates} == RESOURCES - {"etsy://shop/listings", "etsy://writes/recent"}
        body = rpc("resources/read", {"uri": "etsy://keywords/budget"}).json()["result"]["contents"][0]
        assert body["mimeType"] == "application/json" and json.loads(body["text"])["count"] == 1
        err = rpc("resources/read", {"uri": "etsy://audit/99"}).json()["error"]
        assert err["code"] == NOT_FOUND and "seo_audit" in err["message"]
        shop = rpc("resources/read", {"uri": "etsy://shop/listings"}).json()["result"]["contents"][0]
        assert json.loads(shop["text"])["returned"] == 2  # a stateless server keeps its in-memory cache between requests
        assert json.loads(rpc("resources/read", {"uri": "etsy://shop/listings"}).json()["result"]["contents"][0]["text"])["cached"] is True
    finally:
        server.should_exit = True
        th.join(timeout=5)


# ----------------------------------------------------------------------------- real stdio (Claude Code)
async def test_real_stdio_process_handshake_tools_and_resources(tmp_path):
    """Launch the actual entrypoint (python -m indigorepublica_etsy_mcp.server) and talk to it over its stdin/stdout.

    Catches what in-process tests can't: a stray print() corrupting the JSON-RPC stream, an import that only breaks outside pytest,
    or a resource that doesn't survive the wire. No credentials are configured and nothing here needs the network.
    """
    home = tmp_path / "home"
    home.mkdir()
    db_path = tmp_path / "research.db"
    db = ResearchDB(db_path)
    db.save_keywords([{"keyword": "budget planner", "avg_monthly_searches": 12000, "niche_score": 71.5}], "profittree")
    seo.store_audit(db, {"listing_id": 10, "score": 77.0, "fixes": []})
    db.log_write("etsy_update_listing", 10, "safe", False, {"title": "a"}, {"request": {"title": "b"}}, "ok")
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "indigorepublica_etsy_mcp.server"], cwd=str(tmp_path),
        env={"HOME": str(home), "ETSY_ENV_FILE": str(tmp_path / "no.env"), "ETSY_RESEARCH_DB": str(db_path),
             "ETSY_TOKEN_FILE": str(home / "tokens.json"), "ETSY_UPLOAD_DIRS": str(tmp_path), "ETSY_MCP_MODE": "safe", "LOG_LEVEL": "WARNING"})
    with open(tmp_path / "server.stderr", "w") as stderr:
        async with Client(stdio_client(params, errlog=stderr), read_timeout_seconds=60) as c:
            assert c.server_info.name == "indigorepublica-etsy"
            names = {t.name for t in (await c.list_tools()).tools}
            assert len(names) == 54 and NEW_TOOLS <= names
            assert {str(r.uri) for r in (await c.list_resources()).resources} == {"etsy://shop/listings", "etsy://writes/recent"}
            kw = await read(c, "etsy://keywords/budget")
            assert kw["count"] == 1 and kw["rows"][0]["searches"] == 12000
            assert (await read(c, "etsy://audit/10"))["score"] == 77.0
            assert [e["tool"] for e in (await read(c, "etsy://writes/recent"))["entries"]] == ["etsy_update_listing"]
            e = await read_error(c, "etsy://audit/99")
            assert e.code == NOT_FOUND and "seo_audit" in str(e)
            # a tool that needs credentials fails cleanly (no key configured, no network attempted) and the server keeps serving
            assert (await c.call_tool("etsy_whoami", {})).is_error
            assert (await c.call_tool("etsy_seo_check", {"title": "Monthly Budget Planner Spreadsheet Template"})).structured_content["ok"] is True
            assert len((await c.list_tools()).tools) == 54
