"""Photo alt-text/rank edits, listing attributes, video list/delete, and the 20-photo / listing_type audit fixes (offline)."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx

from indigorepublica_etsy_mcp import seo
from indigorepublica_etsy_mcp.server import build_server

from .test_server import SHOP, FakeEtsy, call, env  # noqa: F401  (env fixture)

L = 42
BASE = "/v3/application"


class MediaFake(FakeEtsy):
    def __init__(self) -> None:
        super().__init__()
        self.images = [{"listing_image_id": 1, "rank": 1, "alt_text": "Cover"}, {"listing_image_id": 2, "rank": 2, "alt_text": "Inside pages"}]
        self.props: dict[int, dict] = {}
        self.videos = [{"video_id": 9, "video_state": "active"}, {"video_id": 8, "video_state": "inactive"}]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path, req.method
        img_path = f"{BASE}/shops/{SHOP}/listings/{L}/images"
        if p == f"{BASE}/listings/{L}/images" and m == "GET":
            self.calls.append((m, p, b"", dict(req.headers)))
            return httpx.Response(200, json={"count": len(self.images), "results": self.images})
        if p == img_path and m == "POST" and b'name="image"' not in req.read():
            body = req.read()
            self.calls.append((m, p, body, dict(req.headers)))
            f = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            img = next(i for i in self.images if i["listing_image_id"] == int(f["listing_image_id"]))
            img["rank"] = int(f["rank"])
            img["alt_text"] = f.get("alt_text", "")  # Etsy wipes alt text when it is omitted
            return httpx.Response(201, json=img)
        if p == f"{BASE}/listings/{L}" and m == "GET":
            self.calls.append((m, p, b"", dict(req.headers)))
            return httpx.Response(200, json={"listing_id": L, "taxonomy_id": 12475, "listing_type": "download"})
        if p == f"{BASE}/seller-taxonomy/nodes/12475/properties":
            self.calls.append((m, p, b"", dict(req.headers)))
            return httpx.Response(200, json={"count": 1, "results": [{"property_id": 200, "name": "Primary color", "possible_values": [
                {"value_id": 2, "name": "Blue"}, {"value_id": 10, "name": "White"}]}]})
        if p.startswith(f"{BASE}/shops/{SHOP}/listings/") and "/properties/" in p and m == "PUT":
            body = req.read()
            self.calls.append((m, p, body, dict(req.headers)))
            f = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            pid = int(p.rsplit("/", 1)[1])
            self.props[pid] = {"property_id": pid, "property_name": "Primary color", "values": f["values"].split(","),
                               "value_ids": [int(x) for x in f.get("value_ids", "").split(",") if x]}
            return httpx.Response(200, json=self.props[pid])
        if p == f"{BASE}/shops/{SHOP}/listings/{L}/properties" and m == "GET":
            self.calls.append((m, p, b"", dict(req.headers)))
            return httpx.Response(200, json={"count": len(self.props), "results": list(self.props.values())})
        if p == f"{BASE}/listings/{L}/videos" and m == "GET":
            self.calls.append((m, p, b"", dict(req.headers)))
            return httpx.Response(200, json={"count": len(self.videos), "results": self.videos})
        if p.startswith(f"{BASE}/shops/{SHOP}/listings/{L}/videos/") and m == "DELETE":
            self.calls.append((m, p, b"", dict(req.headers)))
            vid = int(p.rsplit("/", 1)[1])
            self.videos = [v for v in self.videos if v["video_id"] != vid]
            return httpx.Response(204)
        return super().__call__(req)


def _server(env_, mode="safe"):
    s, _, _ = env_
    s.mode = mode
    fake = MediaFake()
    return build_server(s, transport=httpx.MockTransport(fake)), fake


async def test_new_tools_registered(env):
    from mcp import Client
    server, _ = _server(env)
    async with Client(server) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert {"etsy_update_listing_image", "etsy_get_listing_properties", "etsy_set_listing_property",
            "etsy_list_listing_videos", "etsy_delete_listing_video"} <= names


async def test_rank_change_keeps_alt_text(env):
    server, fake = _server(env)
    r = await call(server, "etsy_update_listing_image", {"listing_id": L, "listing_image_id": 2, "rank": 1})
    assert not r.is_error, r
    assert fake.images[1] == {"listing_image_id": 2, "rank": 1, "alt_text": "Inside pages"}


async def test_alt_text_change_keeps_rank(env):
    server, fake = _server(env)
    r = await call(server, "etsy_update_listing_image", {"listing_id": L, "listing_image_id": 1, "alt_text": "Botanical planner cover on iPad"})
    assert not r.is_error, r
    assert fake.images[0]["rank"] == 1 and fake.images[0]["alt_text"] == "Botanical planner cover on iPad"


async def test_image_edit_rejects_bad_rank_and_unknown_image(env):
    server, _ = _server(env)
    assert (await call(server, "etsy_update_listing_image", {"listing_id": L, "listing_image_id": 1, "rank": 21})).is_error
    r = await call(server, "etsy_update_listing_image", {"listing_id": L, "listing_image_id": 999, "alt_text": "x"})
    assert r.is_error and "not on listing" in r.content[0].text


async def test_set_property_looks_up_value_ids(env):
    server, fake = _server(env)
    r = await call(server, "etsy_set_listing_property", {"listing_id": L, "property_id": 200, "values": ["white"]})
    assert not r.is_error, r
    assert fake.props[200]["value_ids"] == [10]
    got = (await call(server, "etsy_get_listing_properties", {"listing_id": L})).structured_content
    assert got["properties"][0]["values"] == ["white"]
    bad = await call(server, "etsy_set_listing_property", {"listing_id": L, "property_id": 200, "values": ["Teal"]})
    assert bad.is_error and "not allowed" in bad.content[0].text


async def test_set_property_blocked_in_readonly(env):
    server, fake = _server(env, mode="readonly")
    r = await call(server, "etsy_set_listing_property", {"listing_id": L, "property_id": 200, "values": ["White"], "value_ids": [10]})
    assert r.is_error and "readonly" in r.content[0].text and not fake.props


async def test_video_list_and_delete_needs_full_mode(env):
    server, fake = _server(env)
    vids = (await call(server, "etsy_list_listing_videos", {"listing_id": L})).structured_content["videos"]
    assert [v["state"] for v in vids] == ["active", "inactive"]
    r = await call(server, "etsy_delete_listing_video", {"listing_id": L, "video_id": 8})
    assert r.is_error and "safe mode" in r.content[0].text and len(fake.videos) == 2
    server, fake = _server(env, mode="full")
    r = await call(server, "etsy_delete_listing_video", {"listing_id": L, "video_id": 8})
    assert not r.is_error, r
    assert [v["video_id"] for v in fake.videos] == [9]


async def test_manifest_alt_text_properties_and_photo_limit(env, tmp_path):
    s, _, products = env
    server, fake = _server(env)
    m = json.loads((products / "listing.json").read_text())
    m["images"] = [{"path": "images/cover.png", "alt": "Budget spreadsheet cover"}]
    m["properties"] = [{"property_id": 200, "values": ["White"], "value_ids": [10]}]
    (products / "listing.json").write_text(json.dumps(m))
    r = await call(server, "etsy_create_digital_listing", {"manifest_path": str(products / "listing.json")})
    assert not r.is_error, r
    rep = r.structured_content
    assert not rep["errors"], rep["errors"]
    up = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/images")]
    assert up and b"Budget spreadsheet cover" in up[0][2]
    assert any(c[0] == "PUT" and "/properties/200" in c[1] for c in fake.calls)
    m["images"] = ["images/cover.png"] * 21
    (products / "listing.json").write_text(json.dumps(m))
    r = await call(server, "etsy_create_digital_listing", {"manifest_path": str(products / "listing.json")})
    assert r.is_error and "Etsy allows 20" in r.content[0].text


# ------------------------------------------------------------------ audit fixes
LISTING = {
    "listing_id": 7, "listing_type": "download", "taxonomy_id": 1,
    "title": "2027 Digital Planner, Botanical Journal for GoodNotes and iPad, Hyperlinked with Moon Phases",
    "tags": ["planner digital 2027", "goodnotes planner", "2027 ipad planner", "botanical planner", "moon phase planner",
             "hyperlinked planner", "2027 planner journal", "2027 digital journal", "aesthetic planner", "2027 daily journal",
             "2027 planner weekly", "2027 planner monthly", "2026-2027 planner"],
    "description": "A botanical 2027 digital planner for GoodNotes and iPad.\n\nWHAT&#39;S INSIDE:\n- Monthly pages\n- Weekly pages\n\nHOW IT WORKS:\n- Download\n- Import\n" + "Details. " * 40,
    "materials": ["PDF"], "style": [], "images": [{}] * 18,
}


def test_twenty_photos_is_allowed_and_ten_is_full_marks():
    assert seo.score_images(18) == (100.0, [])
    assert seo.score_images(10)[0] == 100.0
    assert seo.score_images(20)[0] == 100.0
    assert seo.score_images(21)[0] < 100
    s, issues = seo.score_images(5)
    assert s == 50.0 and "at least 10" in issues[0]["message"]


def test_listing_type_download_is_digital_and_styles_not_required():
    r = seo.audit_listing(LISTING, file_count=1, properties=[{"values": ["Pink"]}])
    assert "digital" in r["sub_scores"] and r["sub_scores"]["digital"]["score"] == 100
    assert r["sub_scores"]["attributes"]["score"] == 100 and r["sub_scores"]["images"]["score"] == 100
    assert not any("styles" in f["fix"] for f in r["fixes"])
    r = seo.audit_listing(LISTING, file_count=0, properties=[])
    assert any("No digital file" in f["fix"] for f in r["fixes"])


def test_keyword_matches_in_any_word_order():
    assert seo.infer_keyword(LISTING["title"], LISTING["tags"]) == "planner digital 2027"
    r = seo.audit_listing(LISTING, file_count=1, properties=[{"values": ["Pink"]}], keyword="planner digital 2027")
    assert r["sub_scores"]["title"]["score"] == 100, r["fixes"]
    assert r["sub_scores"]["description"]["score"] == 100, r["fixes"]


def test_long_titles_flagged_by_word_count():
    s, issues = seo.score_title(" ".join(f"word{i}" for i in range(18)), None, [])
    assert any("18 words" in i["message"] for i in issues)
    assert seo.score_title("Botanical 2027 Digital Planner for GoodNotes", None, [])[0] == 100
