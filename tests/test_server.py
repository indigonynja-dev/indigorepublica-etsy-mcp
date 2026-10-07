"""Offline tests: a fake Etsy API via httpx.MockTransport. No network, no real shop."""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from mcp import Client

from indigorepublica_etsy_mcp.config import Settings
from indigorepublica_etsy_mcp.server import build_http_app, build_server, seo_check

SHOP = 4242
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class FakeEtsy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes, dict]] = []
        self.next_listing = 900

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = req.read()
        self.calls.append((req.method, req.url.path, body, dict(req.headers)))
        p, m = req.url.path, req.method
        if p != "/v3/public/oauth/token":
            assert req.headers.get("x-api-key") == "KEY:SECRET", "x-api-key must be keystring:shared_secret"
        if p == "/v3/public/oauth/token":
            form = parse_qs(body.decode())
            assert form["grant_type"] == ["refresh_token"]
            return httpx.Response(200, json={"access_token": "77.fresh", "refresh_token": "77.r2", "expires_in": 3600, "token_type": "Bearer"})
        assert req.headers.get("authorization", "").startswith("Bearer 77."), "missing bearer"
        if p == "/v3/application/users/me":
            return httpx.Response(200, json={"user_id": 77, "shop_id": SHOP})
        if p == f"/v3/application/shops/{SHOP}" and m == "GET":
            return httpx.Response(200, json={"shop_id": SHOP, "shop_name": "IndigoPrints", "currency_code": "USD", "listing_active_count": 3})
        if p == f"/v3/application/shops/{SHOP}/listings" and m == "POST":
            self.next_listing += 1
            f = parse_qs(body.decode())
            return httpx.Response(201, json={"listing_id": self.next_listing, "title": f["title"][0], "state": "draft",
                                             "price": {"amount": 499, "divisor": 100, "currency_code": "USD"},
                                             "tags": f.get("tags", [""])[0].split(","), "url": f"https://etsy.example/l/{self.next_listing}"})
        if p == f"/v3/application/shops/{SHOP}/listings" and m == "GET":
            return httpx.Response(200, json={"count": 2, "results": [
                {"listing_id": 1, "title": "BUDGET PLANNER SPREADSHEET", "state": "active", "tags": ["budget"], "views": 0,
                 "price": {"amount": 799, "divisor": 100, "currency_code": "USD"}},
                {"listing_id": 2, "title": "Wedding Seating Chart Template Editable Canva", "state": "active",
                 "tags": [f"tag phrase {i}" for i in range(13)], "views": 40, "shop_section_id": 5,
                 "price": {"amount": 1200, "divisor": 100, "currency_code": "USD"}}]})
        if p.endswith("/images") and m == "POST":
            assert b'name="image"' in body
            return httpx.Response(201, json={"listing_image_id": 5000 + len(self.calls), "rank": 1, "url_fullxfull": "https://img"})
        if p.endswith("/videos") and m == "POST":
            assert b'name="video"' in body
            return httpx.Response(201, json={"video_id": 7000 + len(self.calls), "video_state": "active", "video_url": "https://vid"})
        if p.endswith("/files") and m == "POST":
            assert b'name="file"' in body
            return httpx.Response(201, json={"listing_file_id": 6000 + len(self.calls), "filename": "x", "filesize": "1 KB"})
        if p.startswith(f"/v3/application/shops/{SHOP}/listings/") and m == "PATCH":
            return httpx.Response(200, json={"listing_id": int(p.rsplit("/", 1)[1]), "state": parse_qs(body.decode()).get("state", ["draft"])[0]})
        if p == f"/v3/application/shops/{SHOP}/receipts":
            return httpx.Response(200, json={"count": 2, "results": [
                {"receipt_id": 1, "name": "A", "is_paid": True, "grandtotal": {"amount": 1000, "divisor": 100, "currency_code": "USD"},
                 "transactions": [{"listing_id": 2, "title": "Seating", "quantity": 1, "price": {"amount": 1000, "divisor": 100}}]},
                {"receipt_id": 2, "name": "B", "is_paid": True, "grandtotal": {"amount": 500, "divisor": 100, "currency_code": "USD"},
                 "transactions": [{"listing_id": 1, "title": "Budget", "quantity": 1, "price": {"amount": 500, "divisor": 100}}]}]})
        if p == "/v3/application/seller-taxonomy/nodes":
            return httpx.Response(200, json={"results": [{"id": 1, "name": "Paper & Party Supplies", "children": [
                {"id": 11, "name": "Calendars & Planners", "children": [{"id": 111, "name": "Planners", "children": []}]}]}]})
        return httpx.Response(404, json={"error": f"no fake route for {m} {p}"})


@pytest.fixture()
def env(tmp_path: Path):
    products = tmp_path / "etsy-products" / "budget-kit"
    (products / "files").mkdir(parents=True)
    (products / "images").mkdir()
    (products / "files" / "Budget.xlsx").write_bytes(b"PK\x03\x04fake-xlsx")
    (products / "images" / "cover.png").write_bytes(PNG)
    (products / "listing.json").write_text(json.dumps({
        "title": "Monthly Budget Spreadsheet Template for Google Sheets and Excel",
        "description": "A clean monthly budget tracker. Includes an XLSX file that works in Excel and Google Sheets. " * 3,
        "price": 4.99, "taxonomy_id": 111,
        "tags": ["budget spreadsheet", "budget planner", "monthly budget"],
        "images": ["images/cover.png"], "files": ["files/Budget.xlsx"],
    }))
    tok = tmp_path / "tokens.json"
    tok.write_text(json.dumps({"access_token": "77.old", "refresh_token": "77.r1", "expires_at": time.time() + 3600, "user_id": "77", "shop_id": None}))
    s = Settings(keystring="KEY", shared_secret="SECRET", token_file=tok, upload_dirs=[(tmp_path / "etsy-products").resolve()],
                 max_qps=1000, mode="safe", auth_token="t0ken", public_hosts=["box.example.ts.net"])
    fake = FakeEtsy()
    return s, fake, products


async def call(server, name, args=None):
    async with Client(server) as c:
        return await c.call_tool(name, args or {})


async def test_tools_registered(env):
    s, fake, _ = env
    server = build_server(s, transport=httpx.MockTransport(fake))
    async with Client(server) as c:
        tools = (await c.list_tools()).tools
        prompts = (await c.list_prompts()).prompts
    names = {t.name for t in tools}
    assert len(names) >= 30
    for must in ["etsy_whoami", "etsy_create_digital_listing", "etsy_list_orders", "etsy_seo_check", "etsy_api_request"]:
        assert must in names
    deletes = [t.name for t in tools if t.annotations and t.annotations.destructive_hint]
    assert set(deletes) == {"etsy_delete_listing", "etsy_delete_listing_image", "etsy_delete_listing_file"}
    assert {p.name for p in prompts} == {"publish_digital_product", "weekly_shop_report", "listing_seo_audit"}


async def test_whoami_and_shop_id_cached(env):
    s, fake, _ = env
    server = build_server(s, transport=httpx.MockTransport(fake))
    r = await call(server, "etsy_whoami")
    assert not r.is_error, r
    assert r.structured_content["shop_name"] == "IndigoPrints"


async def test_digital_listing_from_manifest(env):
    s, fake, products = env
    server = build_server(s, transport=httpx.MockTransport(fake))
    r = await call(server, "etsy_create_digital_listing", {"manifest_path": str(products / "listing.json")})
    assert not r.is_error, r.content
    out = r.structured_content
    assert out["state"] == "draft" and len(out["images"]) == 1 and len(out["files"]) == 1 and not out["errors"]
    create = next(c for c in fake.calls if c[0] == "POST" and c[1].endswith("/listings"))
    form = parse_qs(create[2].decode())
    assert form["type"] == ["download"] and form["who_made"] == ["i_did"] and form["is_supply"] == ["false"]
    assert form["tags"] == ["budget spreadsheet,budget planner,monthly budget"]
    assert not any(c[0] == "PATCH" for c in fake.calls), "must not publish by default"


async def test_publish_requires_fee_confirmation(env):
    s, fake, _ = env
    server = build_server(s, transport=httpx.MockTransport(fake))
    r = await call(server, "etsy_publish_listing", {"listing_id": 901})
    assert r.is_error and "listing fee" in r.content[0].text
    r = await call(server, "etsy_publish_listing", {"listing_id": 901, "confirm_publish_fee": True})
    assert not r.is_error and r.structured_content["listing"]["state"] == "active"


async def test_safe_mode_blocks_deletes_and_readonly_blocks_writes(env):
    s, fake, _ = env
    server = build_server(s, transport=httpx.MockTransport(fake))
    r = await call(server, "etsy_delete_listing", {"listing_id": 1})
    assert r.is_error and "safe mode" in r.content[0].text
    s.mode = "readonly"
    server = build_server(s, transport=httpx.MockTransport(fake))
    r = await call(server, "etsy_create_shop_section", {"title": "X"})
    assert r.is_error and "readonly" in r.content[0].text


async def test_upload_path_jail(env, tmp_path):
    s, fake, _ = env
    secret = tmp_path / "id_rsa"
    secret.write_text("nope")
    server = build_server(s, transport=httpx.MockTransport(fake))
    r = await call(server, "etsy_upload_listing_file", {"listing_id": 1, "file_path": str(secret)})
    assert r.is_error and "outside ETSY_UPLOAD_DIRS" in r.content[0].text
    r = await call(server, "etsy_upload_listing_file", {"listing_id": 1, "file_path": str(s.upload_dirs[0] / ".." / "id_rsa")})
    assert r.is_error


async def test_api_request_ssrf_guard(env):
    s, fake, _ = env
    server = build_server(s, transport=httpx.MockTransport(fake))
    r = await call(server, "etsy_api_request", {"method": "GET", "path": "https://evil.example/x"})
    assert r.is_error
    r = await call(server, "etsy_api_request", {"method": "GET", "path": "/shops/{shop_id}"})
    assert not r.is_error and json.loads(r.content[0].text)["shop_name"] == "IndigoPrints"


async def test_token_refresh_rotates_and_persists(env):
    s, fake, _ = env
    data = json.loads(s.token_file.read_text())
    data["expires_at"] = 0
    s.token_file.write_text(json.dumps(data))
    server = build_server(s, transport=httpx.MockTransport(fake))
    r = await call(server, "etsy_get_shop")
    assert not r.is_error, r.content
    saved = json.loads(s.token_file.read_text())
    assert saved["access_token"] == "77.fresh" and saved["refresh_token"] == "77.r2" and saved["shop_id"] == str(SHOP)
    assert oct(s.token_file.stat().st_mode)[-3:] == "600"


async def test_sales_summary_and_audit_and_taxonomy(env):
    s, fake, _ = env
    server = build_server(s, transport=httpx.MockTransport(fake))
    r = await call(server, "etsy_sales_summary", {"since": "2026-09-01"})
    assert r.structured_content["gross"] == 15.0 and r.structured_content["orders"] == 2
    assert r.structured_content["top_listings"][0]["listing_id"] == 2
    r = await call(server, "etsy_listing_audit", {})
    flagged = r.structured_content["listings"]
    assert flagged[0]["listing_id"] == 1 and "0 views" in flagged[0]["issues"]
    r = await call(server, "etsy_search_taxonomy", {"query": "planners"})
    assert r.structured_content["matches"][0]["taxonomy_id"] == 111


def test_seo_rules():
    bad = seo_check("A" * 141 + " & & ", ["x" * 21, "dup", "Dup", "bad,tag"] + [f"t{i}" for i in range(10)])
    text = " ".join(bad["errors"])
    assert not bad["ok"]
    for frag in ["max is 140", "'&'", "max 20", "Duplicate", "characters Etsy rejects", "max is 13"]:
        assert frag in text, frag
    good = seo_check("Monthly Budget Spreadsheet Template, Google Sheets Budget Planner", ["budget spreadsheet"] * 1)
    assert good["ok"]


# ----------------------------------------------------------------------------- HTTP transport
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_http_bearer_and_host_checks(env):
    import uvicorn

    s, fake, _ = env
    port = _free_port()
    app = build_http_app(s, build_server(s, transport=httpx.MockTransport(fake)))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    base = f"http://127.0.0.1:{port}"
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    try:
        assert httpx.get(f"{base}/health").json()["ok"] is True
        assert httpx.post(f"{base}/mcp", json=init, headers=hdr).status_code == 401
        assert httpx.post(f"{base}/mcp", json=init, headers={**hdr, "Authorization": "Bearer wrong"}).status_code == 401
        ok = httpx.post(f"{base}/mcp", json=init, headers={**hdr, "Authorization": "Bearer t0ken"})
        assert ok.status_code == 200, ok.text
        assert "indigorepublica-etsy" in ok.text
        # Tunnel host allowed, random host rejected (DNS-rebinding guard)
        tunneled = httpx.post(f"{base}/mcp", json=init, headers={**hdr, "Authorization": "Bearer t0ken", "Host": "box.example.ts.net"})
        assert tunneled.status_code == 200, tunneled.text
        evil = httpx.post(f"{base}/mcp", json=init, headers={**hdr, "Authorization": "Bearer t0ken", "Host": "evil.example"})
        assert evil.status_code in (400, 403, 421)
    finally:
        server.should_exit = True
        th.join(timeout=5)


def test_migrate_legacy_data_dir(tmp_path):
    from indigorepublica_etsy_mcp.config import migrate_legacy_data_dir

    old = tmp_path / (".nynja" + "-etsy-mcp")
    old.mkdir()
    (old / "tokens.json").write_text("{}")
    assert migrate_legacy_data_dir(tmp_path) is True
    assert (tmp_path / ".indigorepublica-etsy-mcp" / "tokens.json").exists() and not old.exists()
    assert migrate_legacy_data_dir(tmp_path) is False


MP4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 32


async def test_upload_listing_video(env):
    s, fake, products = env
    (products / "demo.mp4").write_bytes(MP4)
    server = build_server(s, transport=httpx.MockTransport(fake))
    r = await call(server, "etsy_upload_listing_video", {"listing_id": 901, "file_path": str(products / "demo.mp4")})
    assert not r.is_error, r.content
    assert r.structured_content["video_id"] and r.structured_content["state"] == "active"
    post = next(c for c in fake.calls if c[1].endswith("/listings/901/videos"))
    assert b'name="video"' in post[2] and b'name="name"' in post[2]
    r = await call(server, "etsy_upload_listing_video", {"listing_id": 901, "file_base64": "aGk=", "filename": "notes.pdf"})
    assert r.is_error and "doesn't look like a video" in r.content[0].text


async def test_digital_listing_manifest_video_and_when_made(env):
    s, fake, products = env
    (products / "demo.mp4").write_bytes(MP4)
    manifest = json.loads((products / "listing.json").read_text())
    manifest["video"] = "demo.mp4"
    (products / "listing-video.json").write_text(json.dumps(manifest))
    server = build_server(s, transport=httpx.MockTransport(fake))
    r = await call(server, "etsy_create_digital_listing", {"manifest_path": str(products / "listing-video.json")})
    assert not r.is_error, r.content
    out = r.structured_content
    assert out["video"]["video_id"] and not out["errors"]
    create = next(c for c in fake.calls if c[0] == "POST" and c[1].endswith("/listings"))
    assert parse_qs(create[2].decode())["when_made"] == ["2020_2026"]
