"""Hardening tests: Etsy rate limits, safe/readonly mode for every write tool, write-log coverage, and the tool count."""

from __future__ import annotations

import asyncio
import re
import types
from pathlib import Path

import httpx
import pytest
from mcp import Client

from indigorepublica_etsy_mcp import client as client_module
from indigorepublica_etsy_mcp.server import build_server

from .fakes import LISTINGS_PATH, SHOP_PATH, FullFake, ShopFake
from .test_resources import INTERNAL, read, read_error
from .test_server import env  # noqa: F401  (env fixture)

RATE_LIMITED = {"error": "You have exceeded your rate limit."}
NOW_FREE = {"retry-after": "0"}  # retried instantly: keeps these tests fast without touching the client's real retry policy


async def write_log(c: Client) -> list[dict]:
    return (await c.call_tool("etsy_write_log", {"limit": 500})).structured_content["entries"]


@pytest.fixture()
def sleeps(monkeypatch):
    """Record the client's backoff sleeps instead of waiting. Only the client module sees the stand-in."""
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *args, **kwargs):
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(client_module, "asyncio", types.SimpleNamespace(sleep=fake_sleep, Lock=asyncio.Lock))
    return delays


# ----------------------------------------------------------------------------- rate limits
async def test_429_then_success_is_retried(env):
    s, _, _ = env
    fake = ShopFake()
    fake.fault("GET", SHOP_PATH, 429, times=1, headers=NOW_FREE, json=RATE_LIMITED)
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        r = await c.call_tool("etsy_get_shop")
    assert not r.is_error, r.content
    assert r.structured_content["shop_name"] == "IndigoPrints"
    assert fake.count("GET", SHOP_PATH) == 2, "one 429, then the retry that succeeded"


async def test_repeated_429s_return_a_clear_error(env):
    s, _, _ = env
    fake = ShopFake()
    fake.fault("GET", SHOP_PATH, 429, headers=NOW_FREE, json=RATE_LIMITED)  # never stops
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        r = await c.call_tool("etsy_get_shop")
    assert r.is_error
    text = r.content[0].text
    # The SDK frames a deliberate ToolError as "Error executing tool <name>: <message>"; a crash would carry no message at all.
    for part in ("429", SHOP_PATH.removeprefix("/v3/application"), "exceeded your rate limit", "Rate limited by Etsy", "ETSY_MAX_QPS"):
        assert part in text, part
    assert fake.count("GET", SHOP_PATH) == 5, "the first try plus 4 retries, then it gives up instead of looping"


@pytest.mark.parametrize("headers, expected", [
    ({"retry-after": "3"}, [3.0, 3.0, 3.0, 3.0]),  # Etsy's own instruction wins
    ({}, [2, 4, 8, 16]),  # no header: exponential backoff
    ({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, [2, 4, 8, 16]),  # an HTTP-date isn't parsed: falls back to backoff
])
async def test_backoff_schedule(env, sleeps, headers, expected):
    s, _, _ = env
    fake = ShopFake()
    fake.fault("GET", SHOP_PATH, 429, headers=headers, json=RATE_LIMITED)
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        assert (await c.call_tool("etsy_get_shop")).is_error
    assert [d for d in sleeps if d >= 1] == expected  # (sub-second waits are the request throttle, not the 429 backoff)


async def test_a_429_on_a_write_is_retried_and_logged_as_one_attempt(env):
    s, _, _ = env
    fake = ShopFake()
    fake.fault("PATCH", f"{LISTINGS_PATH}/901", 429, times=1, headers=NOW_FREE, json=RATE_LIMITED)
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        r = await c.call_tool("etsy_update_listing", {"listing_id": 901, "fields": {"title": "Budget Planner Spreadsheet Template for Google Sheets"}})
        rows = await write_log(c)
    assert not r.is_error, r.content
    assert fake.count("PATCH", f"{LISTINGS_PATH}/901") == 2
    assert [(e["tool"], e["result"]) for e in rows] == [("etsy_update_listing", "ok")], "retries are inside one logged attempt"


async def test_repeated_429s_on_a_write_fail_clearly_and_are_logged(env):
    s, _, _ = env
    fake = ShopFake()
    fake.fault("PATCH", f"{LISTINGS_PATH}/901", 429, headers=NOW_FREE, json=RATE_LIMITED)
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        r = await c.call_tool("etsy_update_listing", {"listing_id": 901, "fields": {"title": "Budget Planner Spreadsheet Template for Google Sheets"}})
        rows = await write_log(c)
    assert r.is_error and "429" in r.content[0].text and "Rate limited by Etsy" in r.content[0].text
    assert fake.count("PATCH", f"{LISTINGS_PATH}/901") == 5
    assert len(rows) == 1 and rows[0]["tool"] == "etsy_update_listing" and rows[0]["result"].startswith("error: Etsy API 429")


async def test_listings_resource_survives_one_429_and_reports_repeated_ones(env):
    s, _, _ = env
    fake = ShopFake()
    fake.fault("GET", LISTINGS_PATH, 429, times=1, headers=NOW_FREE, json=RATE_LIMITED)
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        assert (await read(c, "etsy://shop/listings"))["returned"] == 2
    assert fake.count("GET", LISTINGS_PATH) == 2

    fake = ShopFake()
    fake.fault("GET", LISTINGS_PATH, 429, headers=NOW_FREE, json=RATE_LIMITED)
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        e = await read_error(c, "etsy://shop/listings")
        assert e.code == INTERNAL and "429" in str(e) and "Rate limited by Etsy" in str(e)
        fake.clear_faults()
        assert (await read(c, "etsy://shop/listings"))["cached"] is False, "a rate-limit error must not be cached"
    assert fake.count("GET", LISTINGS_PATH) == 6


# ----------------------------------------------------------------------------- write tools vs. server mode
# Every tool that is not read-only, sorted by what it can do. test_every_write_tool_is_classified fails when a write tool is
# added without being listed here, so each new one gets an explicit decision about readonly and safe mode.
DELETES = {  # id -> (tool, arguments): permanently remove something on Etsy. Refused unless ETSY_MCP_MODE=full.
    "etsy_delete_listing": ("etsy_delete_listing", {"listing_id": 1}),
    "etsy_delete_listing_image": ("etsy_delete_listing_image", {"listing_id": 1, "listing_image_id": 2}),
    "etsy_delete_listing_file": ("etsy_delete_listing_file", {"listing_id": 1, "listing_file_id": 3}),
    "etsy_api_request[DELETE]": ("etsy_api_request", {"method": "DELETE", "path": "/listings/1"}),
}
ETSY_WRITES = {  # tool -> arguments: change something on Etsy without deleting it. Refused in readonly, allowed in safe.
    "etsy_update_shop": {"title": "New shop headline"},
    "etsy_create_shop_section": {"title": "Budgets"},
    "etsy_create_draft_listing": {"title": "Monthly Budget Planner Spreadsheet Template", "description": "d", "price": 4.99, "taxonomy_id": 111},
    "etsy_update_listing": {"listing_id": 1, "fields": {"title": "New title"}},
    "etsy_publish_listing": {"listing_id": 1, "confirm_publish_fee": True},
    "etsy_upload_listing_image": {"listing_id": 1, "file_base64": "aGk=", "filename": "a.png"},
    "etsy_upload_listing_file": {"listing_id": 1, "file_base64": "aGk=", "filename": "a.pdf"},
    "etsy_update_listing_inventory": {"listing_id": 1, "inventory": {"products": []}},
    "etsy_create_digital_listing": {"title": "Monthly Budget Planner Spreadsheet Template"},
    "etsy_add_tracking": {"receipt_id": 1, "tracking_code": "1Z999", "carrier_name": "ups"},
    "etsy_api_request": {"method": "POST", "path": "/shops/{shop_id}/sections", "form": {"title": "x"}},
    "seo_apply_update": {"listing_id": 10, "preview_id": "0123456789ab"},
}
LOCAL_WRITES = {  # change only this server's own SQLite cache, never Etsy: allowed in every mode.
    "competitor_add", "competitor_remove", "competitor_snapshot", "competitor_snapshot_all", "research_save_keywords",
    "research_save_market_listings", "seo_audit", "seo_preview_update",
}
DELETE_CASES = [pytest.param(tool, args, id=case) for case, (tool, args) in DELETES.items()]
ETSY_WRITE_CASES = [pytest.param(tool, args, id=tool) for tool, args in ETSY_WRITES.items()]


async def test_every_write_tool_is_classified(env):
    s, fake, _ = env
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        tools = (await c.list_tools()).tools
    writers = {t.name for t in tools if not (t.annotations and t.annotations.read_only_hint)}
    classified = set(ETSY_WRITES) | {tool for tool, _ in DELETES.values()} | LOCAL_WRITES
    assert writers == classified, f"write tools missing from the table: {sorted(writers - classified)}; stale entries: {sorted(classified - writers)}"
    assert not set(ETSY_WRITES) & LOCAL_WRITES
    destructive = {t.name for t in tools if t.annotations and t.annotations.destructive_hint}
    assert destructive == {"etsy_delete_listing", "etsy_delete_listing_image", "etsy_delete_listing_file"}


@pytest.mark.parametrize("name, args", DELETE_CASES)
async def test_safe_mode_refuses_every_delete_and_logs_the_attempt(env, name, args):
    s, _, _ = env
    assert s.mode == "safe"
    fake = ShopFake()
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        r = await c.call_tool(name, args)
        rows = await write_log(c)
    assert r.is_error and "safe mode" in r.content[0].text and "ETSY_MCP_MODE=full" in r.content[0].text
    assert fake.seen == [], "refused before any request reached Etsy"
    assert len(rows) == 1, "exactly one log row for the attempt"
    row = rows[0]
    assert (row["tool"], row["mode"], row["dry_run"]) == (name, "safe", False)
    assert row["result"].startswith("error:") and "safe mode" in row["result"]
    assert row["listing_id"] == args.get("listing_id")


@pytest.mark.parametrize("name, args", DELETE_CASES)
async def test_full_mode_lets_the_same_delete_through(env, name, args):
    """The control for the test above: the refusal comes from the mode guard, not from a missing fake route."""
    s, _, _ = env
    s.mode = "full"
    fake = ShopFake()
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        r = await c.call_tool(name, args)
        rows = await write_log(c)
    assert not r.is_error, r.content
    assert sum(1 for m, _ in fake.seen if m == "DELETE") == 1
    assert (rows[0]["tool"], rows[0]["mode"], rows[0]["result"]) == (name, "full", "ok")


@pytest.mark.parametrize("name, args", [*ETSY_WRITE_CASES, *DELETE_CASES])
async def test_readonly_mode_refuses_every_etsy_write_and_logs_the_attempt(env, name, args):
    s, _, _ = env
    s.mode = "readonly"
    fake = ShopFake()
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        r = await c.call_tool(name, args)
        rows = await write_log(c)
    assert r.is_error and "readonly" in r.content[0].text
    assert fake.seen == [], "refused before any request reached Etsy"
    assert len(rows) == 1, "exactly one log row for the attempt"
    assert (rows[0]["tool"], rows[0]["mode"]) == (name, "readonly")
    assert rows[0]["result"].startswith("error:") and "readonly" in rows[0]["result"]


@pytest.mark.parametrize("mode", ["readonly", "safe", "full"])
async def test_local_tools_work_in_every_mode_and_never_write_to_etsy(env, mode):
    s, _, _ = env
    s.mode = mode
    fake = FullFake()
    calls = [
        ("research_save_keywords", {"rows": [{"keyword": "budget planner", "searches": 10}]}),
        ("research_save_market_listings", {"rows": [{"listing_id": 1, "tags": ["a b"]}], "keyword": "budget planner"}),
        ("competitor_add", {"shop_name_or_id": "rival"}),
        ("competitor_snapshot", {"shop": "rival"}),
        ("competitor_snapshot_all", {"delay_seconds": 0}),
        ("seo_audit", {"listing_id": 10}),
        ("seo_preview_update", {"listing_id": 10, "title": "Budget Planner Spreadsheet"}),
        ("competitor_remove", {"shop": "rival"}),
    ]
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        for name, args in calls:
            r = await c.call_tool(name, args)
            assert not r.is_error, (name, r.content)
    assert {name for name, _ in calls} == LOCAL_WRITES
    assert fake.methods == {"GET"}, "a local-only tool sent something other than a read to Etsy"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", ["https://evil.example/steal", "listings/1", "/shops/../users/me"])
async def test_api_request_path_refusals_are_logged_for_writes(env, method, path):
    s, _, _ = env
    s.mode = "full"  # so only the path guard can refuse it
    fake = ShopFake()
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        r = await c.call_tool("etsy_api_request", {"method": method, "path": path})
        rows = await write_log(c)
    assert r.is_error and "relative API path" in r.content[0].text
    assert fake.seen == []
    assert len(rows) == 1 and rows[0]["tool"] == "etsy_api_request" and rows[0]["result"].startswith("error:")
    assert rows[0]["after"]["request"]["method"] == method and rows[0]["after"]["request"]["path"] == path


async def test_api_request_get_with_a_bad_path_is_refused_but_reads_are_not_logged(env):
    s, _, _ = env
    fake = ShopFake()
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        r = await c.call_tool("etsy_api_request", {"method": "GET", "path": "https://evil.example/steal"})
        rows = await write_log(c)
    assert r.is_error and "relative API path" in r.content[0].text and fake.seen == [] and rows == []


async def test_seo_apply_update_logs_every_refusal_once_and_a_success_once(env):
    s, _, _ = env
    fake = ShopFake()
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        r = await c.call_tool("seo_apply_update", {"listing_id": 10, "preview_id": "nope"})
        assert r.is_error and "Unknown preview_id" in r.content[0].text
        rows = await write_log(c)
        assert len(rows) == 1 and rows[0]["tool"] == "seo_apply_update" and rows[0]["result"].startswith("error:")
        assert rows[0]["after"]["request"] == {"preview_id": "nope"} and rows[0]["listing_id"] == 10

        pv = (await c.call_tool("seo_preview_update", {"listing_id": 10, "title": "Budget Planner Spreadsheet"})).structured_content
        fake.listing["title"] = "Edited elsewhere"  # drift
        r = await c.call_tool("seo_apply_update", {"listing_id": 10, "preview_id": pv["preview_id"]})
        assert r.is_error and "changed on Etsy" in r.content[0].text
        assert len(await write_log(c)) == 2

        pv = (await c.call_tool("seo_preview_update", {"listing_id": 10, "title": "Budget Planner Spreadsheet Template"})).structured_content
        r = await c.call_tool("seo_apply_update", {"listing_id": 10, "preview_id": pv["preview_id"]})
        assert not r.is_error, r.content
        rows = await write_log(c)
    assert len(rows) == 3, "a successful apply adds exactly one row, not a pre-flight row plus a write row"
    assert rows[0]["tool"] == "seo_apply_update" and rows[0]["result"] == "ok" and rows[0]["after"]["response"]["listing_id"] == 10
    assert fake.count("PATCH", f"{LISTINGS_PATH}/10") == 1


# ----------------------------------------------------------------------------- the README's numbers stay true
EXPECTED_TOOLS = 52
RESOURCE_URIS = ["etsy://shop/listings", "etsy://keywords/{seed}", "etsy://competitor/{shop}", "etsy://audit/{listing_id}", "etsy://writes/recent"]


async def test_tool_count_is_52_and_matches_the_readme(env):
    s, fake, _ = env
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        tools = (await c.list_tools()).tools
        prompts = (await c.list_prompts()).prompts
        resource_count = len((await c.list_resources()).resources) + len((await c.list_resource_templates()).resource_templates)
    names = [t.name for t in tools]
    assert len(names) == len(set(names)) == EXPECTED_TOOLS, "a tool was added or removed: update EXPECTED_TOOLS here and the counts in README.md"
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    m = re.search(r"\b(\d+) tools, (\d+) prompts, (\d+) resources\b", readme)
    assert m, "README.md must state '<n> tools, <n> prompts, <n> resources' near the top"
    assert tuple(int(x) for x in m.groups()) == (len(names), len(prompts), resource_count)
    assert not [n for n in names if f"`{n}`" not in readme], "tools missing from the README tables"
    assert not [u for u in RESOURCE_URIS if f"`{u}`" not in readme], "resources missing from the README table"
