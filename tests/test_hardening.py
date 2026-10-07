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
from indigorepublica_etsy_mcp.client import EtsyClient, EtsyError
from indigorepublica_etsy_mcp.server import build_server

from .fakes import LISTINGS_PATH, SECTIONS_PATH, SHOP_PATH, FullFake, ShopFake
from .test_resources import INTERNAL, read, read_error
from .test_server import SHOP, env  # noqa: F401  (env fixture)

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


# ----------------------------------------------------------------------------- retries: reads vs. writes
# Reads (GET) keep retrying 429, 5xx and network errors with backoff. Writes (POST/PUT/PATCH/DELETE) are re-sent only on a 429
# (Etsy did not process it) or a failure to connect (nothing was sent). Anything that may have reached Etsy is attempted once and
# reported as "outcome unknown", because a retry could create a second listing, image or file.
SECTIONS = f"/shops/{SHOP}/sections"  # the path as the client takes it (SECTIONS_PATH is what the fake sees on the wire)
SHOP_ENDPOINT = f"/shops/{SHOP}"
UNKNOWN_PARTS = ("OUTCOME UNKNOWN", "may or may not have been applied", "not retried", "before retrying")


def etsy_client(settings, fake) -> EtsyClient:
    return EtsyClient(settings, transport=httpx.MockTransport(fake))


def waits(sleeps: list[float]) -> list[float]:
    return [d for d in sleeps if d >= 1]  # sub-second waits are the request throttle, not retry backoff


@pytest.mark.parametrize("method, status", [("POST", 500), ("POST", 502), ("POST", 503), ("POST", 504), ("PUT", 500), ("PATCH", 500), ("DELETE", 500)])
async def test_a_write_that_gets_a_5xx_is_attempted_exactly_once(env, sleeps, method, status):
    s, _, _ = env
    fake = ShopFake()
    fake.fault(method, SECTIONS_PATH, status, json={"error": "upstream exploded"})
    with pytest.raises(EtsyError) as e:
        await etsy_client(s, fake).request(method, SECTIONS, form={"title": "Budgets"})
    assert fake.count(method, SECTIONS_PATH) == 1, "a 5xx can follow a change Etsy already made: never re-send a write"
    assert waits(sleeps) == [], "and no backoff wait either"
    assert e.value.status == status and e.value.outcome_unknown is True
    for part in ("upstream exploded", *UNKNOWN_PARTS):
        assert part in str(e.value), part


async def test_a_post_that_gets_429_then_200_succeeds(env):
    s, _, _ = env
    fake = ShopFake()
    fake.fault("POST", SECTIONS_PATH, 429, times=1, headers=NOW_FREE, json=RATE_LIMITED)
    res = await etsy_client(s, fake).request("POST", SECTIONS, form={"title": "Budgets"})
    assert (res["shop_section_id"], res["title"]) == (7, "Budgets"), "the retry carried the body again"
    assert fake.count("POST", SECTIONS_PATH) == 2, "one 429 (not processed), then the attempt that went through"


async def test_a_get_that_gets_500_then_200_succeeds(env, sleeps):
    s, _, _ = env
    fake = ShopFake()
    fake.fault("GET", SHOP_PATH, 500, times=1)
    res = await etsy_client(s, fake).request("GET", SHOP_ENDPOINT)
    assert res["shop_name"] == "IndigoPrints"
    assert fake.count("GET", SHOP_PATH) == 2
    assert waits(sleeps) == [2], "reads keep the existing backoff"


@pytest.mark.parametrize("status, error", [
    pytest.param(500, None, id="500"), pytest.param(502, None, id="502"), pytest.param(503, None, id="503"),
    pytest.param(504, None, id="504"), pytest.param(429, None, id="429"),
    pytest.param(None, httpx.ReadTimeout, id="ReadTimeout"), pytest.param(None, httpx.ReadError, id="ReadError"),
    pytest.param(None, httpx.RemoteProtocolError, id="RemoteProtocolError"), pytest.param(None, httpx.ConnectError, id="ConnectError"),
    pytest.param(None, httpx.ConnectTimeout, id="ConnectTimeout"),
])
async def test_a_get_still_retries_everything_it_always_did(env, sleeps, status, error):
    s, _, _ = env
    fake = ShopFake()
    if error is None:
        fake.fault("GET", SHOP_PATH, status, times=1)
    else:
        fake.fault_raises("GET", SHOP_PATH, lambda: error("blip"), times=1)
    assert (await etsy_client(s, fake).request("GET", SHOP_ENDPOINT))["shop_name"] == "IndigoPrints"
    assert fake.count("GET", SHOP_PATH) == 2 and waits(sleeps) == [2]


async def test_a_get_that_keeps_failing_gives_up_after_five_attempts_without_the_write_warning(env, sleeps):
    s, _, _ = env
    fake = ShopFake()
    fake.fault("GET", SHOP_PATH, 503, json={"error": "unavailable"})
    with pytest.raises(EtsyError) as e:
        await etsy_client(s, fake).request("GET", SHOP_ENDPOINT)
    assert fake.count("GET", SHOP_PATH) == 5 and waits(sleeps) == [2, 4, 8, 16]
    assert e.value.status == 503 and e.value.outcome_unknown is False and "OUTCOME UNKNOWN" not in str(e.value)


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
async def test_a_write_is_retried_when_the_connection_could_not_be_made(env, sleeps, error):
    s, _, _ = env
    fake = ShopFake()
    fake.fault_raises("POST", SECTIONS_PATH, lambda: error("no route to host"), times=2)
    res = await etsy_client(s, fake).request("POST", SECTIONS, form={"title": "Budgets"})
    assert res["title"] == "Budgets" and fake.count("POST", SECTIONS_PATH) == 3
    assert waits(sleeps) == [2, 4]


async def test_a_write_that_never_connects_gives_up_and_says_nothing_was_changed(env, sleeps):
    s, _, _ = env
    fake = ShopFake()
    fake.fault_raises("POST", SECTIONS_PATH, lambda: httpx.ConnectError("connection refused"))
    with pytest.raises(EtsyError) as e:
        await etsy_client(s, fake).request("POST", SECTIONS, form={"title": "Budgets"})
    assert fake.count("POST", SECTIONS_PATH) == 5 and waits(sleeps) == [2, 4, 8, 16]
    text = str(e.value)
    assert e.value.outcome_unknown is False and "OUTCOME UNKNOWN" not in text, "nothing was sent, so there is no doubt"
    assert "ConnectError: connection refused" in text and "never sent" in text and "nothing was changed" in text


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ReadError, httpx.WriteTimeout, httpx.WriteError, httpx.RemoteProtocolError, httpx.ProxyError])
async def test_a_write_is_never_retried_after_a_timeout_or_read_error(env, sleeps, method, error):
    s, _, _ = env
    fake = ShopFake()
    fake.fault_raises(method, SECTIONS_PATH, lambda: error("dropped"))
    with pytest.raises(EtsyError) as e:
        await etsy_client(s, fake).request(method, SECTIONS, form={"title": "Budgets"})
    assert fake.count(method, SECTIONS_PATH) == 1 and waits(sleeps) == []
    assert e.value.status == 0 and e.value.outcome_unknown is True
    for part in (f"{error.__name__}: dropped", "after the request may have been sent", *UNKNOWN_PARTS):
        assert part in str(e.value), part


@pytest.mark.parametrize("status", [400, 403, 404, 409, 422])
async def test_a_4xx_on_a_write_is_a_definite_answer_not_an_unknown_outcome(env, sleeps, status):
    s, _, _ = env
    fake = ShopFake()
    fake.fault("POST", SECTIONS_PATH, status, json={"error": "rejected"})
    with pytest.raises(EtsyError) as e:
        await etsy_client(s, fake).request("POST", SECTIONS, form={"title": "Budgets"})
    assert fake.count("POST", SECTIONS_PATH) == 1 and waits(sleeps) == []
    assert e.value.status == status and e.value.outcome_unknown is False and "OUTCOME UNKNOWN" not in str(e.value)


async def test_a_write_rejected_with_401_is_re_sent_once_after_a_token_refresh(env):
    """The one other re-send: a 401 means Etsy refused the request before acting on it, so it is safe to send again with a fresh token."""
    s, _, _ = env
    fake = ShopFake()
    fake.fault("POST", SECTIONS_PATH, 401, times=1)
    res = await etsy_client(s, fake).request("POST", SECTIONS, form={"title": "Budgets"})
    assert res["shop_section_id"] == 7 and fake.count("POST", SECTIONS_PATH) == 2
    assert fake.count("POST", "/v3/public/oauth/token") == 1, "the token was refreshed between the two attempts"


async def test_a_5xx_on_a_write_is_attempted_once_logged_and_tells_the_model_to_check(env):
    s, _, _ = env
    fake = ShopFake()
    fake.fault("POST", SECTIONS_PATH, 500, json={"error": "upstream exploded"})
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        r = await c.call_tool("etsy_create_shop_section", {"title": "Budgets"})
        rows = await write_log(c)
    assert r.is_error
    for part in ("Etsy API 500", "upstream exploded", *UNKNOWN_PARTS):
        assert part in r.content[0].text, part
    assert fake.count("POST", SECTIONS_PATH) == 1
    assert len(rows) == 1 and (rows[0]["tool"], rows[0]["mode"]) == ("etsy_create_shop_section", "safe")
    assert rows[0]["result"].startswith("error: Etsy API 500") and "OUTCOME UNKNOWN" in rows[0]["result"]
    assert rows[0]["after"]["request"] == {"title": "Budgets"} and rows[0]["after"]["response"] is None


async def test_a_read_timeout_on_a_write_is_attempted_once_and_logged(env):
    s, _, _ = env
    fake = ShopFake()
    fake.fault_raises("PATCH", f"{LISTINGS_PATH}/901", lambda: httpx.ReadTimeout("slow"))
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        r = await c.call_tool("etsy_update_listing", {"listing_id": 901, "fields": {"title": "Budget Planner Spreadsheet Template for Google Sheets"}})
        rows = await write_log(c)
    assert r.is_error and "ReadTimeout: slow" in r.content[0].text and "OUTCOME UNKNOWN" in r.content[0].text
    assert fake.count("PATCH", f"{LISTINGS_PATH}/901") == 1
    assert len(rows) == 1 and rows[0]["tool"] == "etsy_update_listing" and rows[0]["listing_id"] == 901
    assert rows[0]["result"].startswith("error:") and "OUTCOME UNKNOWN" in rows[0]["result"]


async def test_an_upload_with_an_unknown_outcome_is_not_retried_and_blocks_publishing(env):
    s, _, products = env
    fake = ShopFake()
    images = f"{LISTINGS_PATH}/901/images"
    fake.fault("POST", images, 500)
    async with Client(build_server(s, transport=httpx.MockTransport(fake))) as c:
        r = await c.call_tool("etsy_create_digital_listing", {"manifest_path": str(products / "listing.json"), "publish": True, "confirm_publish_fee": True})
    assert not r.is_error, r.content  # partial failures are reported, not hidden
    report = r.structured_content
    assert report["images"] == [] and any("OUTCOME UNKNOWN" in x for x in report["errors"])
    assert report["state"] == "draft" and report["publish"].startswith("skipped")
    assert fake.count("POST", images) == 1, "the upload is not re-sent, so no duplicate image"
    assert fake.count("PATCH", f"{LISTINGS_PATH}/901") == 0, "and nothing was published"


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
    "etsy_upload_listing_video": {"listing_id": 1, "file_base64": "aGk=", "filename": "a.mp4"},
    "etsy_update_listing_inventory": {"listing_id": 1, "inventory": {"products": []}},
    "etsy_create_digital_listing": {"title": "Monthly Budget Planner Spreadsheet Template"},
    "etsy_add_tracking": {"receipt_id": 1, "tracking_code": "1Z999", "carrier_name": "ups"},
    "etsy_api_request": {"method": "POST", "path": "/shops/{shop_id}/sections", "form": {"title": "x"}},
    "seo_apply_update": {"listing_id": 10, "preview_id": "0123456789ab"},
}
LOCAL_WRITES = {  # change only this server's own SQLite cache, never Etsy: allowed in every mode.
    "competitor_add", "competitor_remove", "competitor_snapshot", "competitor_snapshot_all", "research_save_keywords",
    "research_save_market_listings", "seo_audit", "seo_preview_update", "competitor_profile", "research_prune",
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
        ("competitor_profile", {"shop": "rival"}),
        ("research_prune", {"dry_run": False}),
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
EXPECTED_TOOLS = 54
RESOURCE_URIS = ["etsy://shop/listings", "etsy://keywords/{seed}", "etsy://competitor/{shop}", "etsy://audit/{listing_id}", "etsy://writes/recent"]


async def test_tool_count_is_53_and_matches_the_readme(env):
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
