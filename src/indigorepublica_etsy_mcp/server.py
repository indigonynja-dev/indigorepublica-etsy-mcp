"""IndigoRepublica Etsy MCP server: your own Etsy shop as Claude tools.

Run:  uv run indigorepublica-etsy-mcp                      # stdio (Claude Code)
      uv run indigorepublica-etsy-mcp --transport http     # Streamable HTTP (Claude.ai via tunnel)
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import logging
import mimetypes
import os
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .client import EtsyClient, EtsyError
from .config import Settings, load_settings
from .oas import SpecIndex
from .research import ResearchDB

log = logging.getLogger("indigorepublica_etsy_mcp")

RO = ToolAnnotations(read_only_hint=True, open_world_hint=True)
LOCAL = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
DELETE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True)

LOCAL_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

LISTING_STATES = ("active", "inactive", "sold_out", "draft", "expired")
UPDATABLE_LISTING_FIELDS = {
    "title", "description", "price", "quantity", "tags", "materials", "taxonomy_id", "shop_section_id",
    "who_made", "when_made", "is_supply", "type", "should_auto_renew", "is_taxable", "shipping_profile_id",
    "return_policy_id", "featured_rank", "is_personalizable", "personalization_is_required",
    "personalization_char_count_max", "personalization_instructions", "production_partner_ids", "styles",
    "item_weight", "item_length", "item_width", "item_height", "item_weight_unit", "item_dimensions_unit",
    "processing_min", "processing_max", "readiness_state_id", "state",
}
TAG_RE = re.compile(r"^[\w\s\-'™©®]+$", re.UNICODE)

INSTRUCTIONS = """Tools for managing the user's OWN Etsy shop (Etsy Open API v3).
Rules of the road:
- Create listings as drafts. Publishing (state=active) charges Etsy's listing fee; only publish when the user explicitly says so, and pass the confirm flag.
- Deletions are permanent. Confirm with the user first; the server may block them (ETSY_MCP_MODE).
- Run etsy_seo_check before creating or retitling a listing and fix any errors it reports.
- Money comes back as decimals in the shop currency. Timestamps are ISO-8601 UTC.
- Prefer the specific tools; use etsy_find_endpoint + etsy_api_request only for gaps.
- Every write to Etsy is recorded in a local append-only log (etsy_write_log).
- Market data comes from other tools (e.g. ProfitTree); cache it with research_save_* and reuse it via research_get_* before re-fetching.
- File uploads read from the server's allowed folders (ETSY_UPLOAD_DIRS) or from https URLs.
The term 'Etsy' is a trademark of Etsy, Inc. This tool uses the Etsy API but is not endorsed or certified by Etsy, Inc."""


# ----------------------------------------------------------------------------- helpers
def money(m: Any) -> float | None:
    if isinstance(m, dict) and "amount" in m:
        return round(m["amount"] / (m.get("divisor") or 100), 2)
    return None


def iso(ts: Any) -> str | None:
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
    return None


def to_epoch(value: str | None) -> int | None:
    if not value:
        return None
    v = value.strip()
    if v.isdigit():
        return int(v)
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as e:
        raise ToolError(f"Bad date {value!r}; use YYYY-MM-DD or full ISO-8601.") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def listing_summary(l: dict[str, Any]) -> dict[str, Any]:
    return {
        "listing_id": l.get("listing_id"),
        "title": l.get("title"),
        "state": l.get("state"),
        "type": l.get("listing_type") or ("download" if l.get("is_digital") else None),
        "price": money(l.get("price")),
        "currency": (l.get("price") or {}).get("currency_code"),
        "quantity": l.get("quantity"),
        "views": l.get("views"),
        "favorites": l.get("num_favorers"),
        "tags": l.get("tags"),
        "taxonomy_id": l.get("taxonomy_id"),
        "shop_section_id": l.get("shop_section_id"),
        "url": l.get("url"),
        "updated": iso(l.get("last_modified_timestamp") or l.get("updated_timestamp")),
        "expires": iso(l.get("ending_timestamp")),
    }


def receipt_summary(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "receipt_id": r.get("receipt_id"),
        "buyer": r.get("name"),
        "status": r.get("status"),
        "paid": r.get("is_paid", r.get("was_paid")),
        "shipped": r.get("is_shipped", r.get("was_shipped")),
        "created": iso(r.get("create_timestamp") or r.get("created_timestamp")),
        "total": money(r.get("grandtotal")),
        "subtotal": money(r.get("subtotal")),
        "shipping": money(r.get("total_shipping_cost")),
        "tax": money(r.get("total_tax_cost")),
        "discount": money(r.get("discount_amt")),
        "currency": (r.get("grandtotal") or {}).get("currency_code"),
        "message_from_buyer": r.get("message_from_buyer") or None,
        "items": [
            {
                "transaction_id": t.get("transaction_id"),
                "listing_id": t.get("listing_id"),
                "title": t.get("title"),
                "quantity": t.get("quantity"),
                "price": money(t.get("price")),
                "digital": t.get("is_digital"),
            }
            for t in r.get("transactions", []) or []
        ],
    }


def seo_check(title: str = "", tags: list[str] | None = None, description: str = "", materials: list[str] | None = None) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    tags = tags or []
    materials = materials or []
    if title:
        if len(title) > 140:
            errors.append(f"Title is {len(title)} chars; Etsy max is 140.")
        for ch in "%:&+":
            if title.count(ch) > 1:
                errors.append(f"Title uses '{ch}' {title.count(ch)} times; Etsy allows it once.")
        caps = [w for w in re.findall(r"[A-Za-z]{2,}", title) if w.isupper()]
        if len(caps) > 3:
            warnings.append(f"{len(caps)} ALL-CAPS words in title; Etsy may reject more than 3.")
        words = [w.lower() for w in re.findall(r"\w+", title)]
        dupes = sorted({w for w in words if len(w) > 3 and words.count(w) > 2})
        if dupes:
            warnings.append(f"Keyword stuffing: {', '.join(dupes)} repeated 3+ times.")
        if len(title) < 30:
            warnings.append("Short title; lead with the exact phrase a buyer would search.")
    elif title == "":
        pass
    if len(tags) > 13:
        errors.append(f"{len(tags)} tags; Etsy max is 13.")
    if 0 < len(tags) < 13:
        warnings.append(f"Only {len(tags)}/13 tags used; unused tags are free search reach.")
    seen = set()
    for t in tags:
        if len(t) > 20:
            errors.append(f"Tag '{t}' is {len(t)} chars; max 20.")
        if not TAG_RE.match(t):
            errors.append(f"Tag '{t}' has characters Etsy rejects (allowed: letters, numbers, spaces, - ' ™ © ®).")
        if t.lower() in seen:
            errors.append(f"Duplicate tag '{t}'.")
        seen.add(t.lower())
        if " " not in t.strip() and len(t) < 12:
            warnings.append(f"Tag '{t}' is a single short word; multi-word phrases usually match more searches.")
    if len(materials) > 13:
        errors.append(f"{len(materials)} materials; Etsy max is 13.")
    if description is not None and description != "":
        if len(description) < 160:
            warnings.append("Description under 160 chars; explain what's included, format, and how delivery works.")
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "stats": {"title_chars": len(title), "tags": len(tags), "description_chars": len(description or "")},
    }


# ----------------------------------------------------------------------------- server factory
def build_server(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> MCPServer:
    etsy = EtsyClient(settings, transport=transport)
    spec = SpecIndex(settings.oas_url, settings.token_file.parent / "etsy-oas.json")
    taxonomy_cache: dict[str, Any] = {}
    mcp = MCPServer(
        name="indigorepublica-etsy",
        title="IndigoRepublica Etsy",
        description="Manage your own Etsy shop: listings, digital files, orders, money, reviews.",
        instructions=INSTRUCTIONS,
        version=__version__,
    )

    # ---- guards
    def guard(kind: Literal["write", "delete"]) -> None:
        if settings.mode == "readonly":
            raise ToolError("Server is in readonly mode (ETSY_MCP_MODE=readonly). Ask the user to change it if this write is intended.")
        if kind == "delete" and settings.mode != "full":
            raise ToolError(
                "Deletes are blocked in safe mode. Do it in the Etsy UI, or set ETSY_MCP_MODE=full and restart the server."
            )

    async def call(method: str, path: str, **kw: Any) -> Any:
        try:
            return await etsy.request(method, path, **kw)
        except EtsyError as e:
            raise ToolError(str(e)) from e
        except FileNotFoundError as e:
            raise ToolError(str(e)) from e

    research = ResearchDB(settings.research_db)

    class WriteEntry:
        def __init__(self, request: Any) -> None:
            self.request, self.before, self.response = request, None, None

    @asynccontextmanager
    async def wlog(tool: str, listing_id: int | None = None, request: Any = None, dry_run: bool = False):
        """Record one write attempt (success, rejection or failure) in the append-only write log. Never masks the real error."""
        entry = WriteEntry(request)
        result = "ok"
        try:
            yield entry
        except Exception as e:
            result = f"error: {e}"
            raise
        finally:
            try:
                research.log_write(tool, listing_id, settings.mode, dry_run, entry.before,
                                   {"request": entry.request, "response": entry.response}, result)
            except Exception:  # noqa: BLE001 - logging must never break a tool
                log.exception("write log failed for %s", tool)

    async def peek(path: str, keys: list[str] | None = None, shape: Any = None) -> Any:
        """Best-effort read of current state for the log's 'before'. Returns None on any failure."""
        try:
            data = await etsy.request("GET", path)
        except Exception:  # noqa: BLE001
            return None
        if shape:
            return shape(data)
        return {k: data.get(k) for k in keys} if keys else data

    async def sid() -> str:
        try:
            return await etsy.shop_id()
        except (EtsyError, FileNotFoundError) as e:
            raise ToolError(str(e)) from e

    def allowed_path(p: str) -> Path:
        path = Path(p).expanduser().resolve()
        if not any(path == root or root in path.parents for root in settings.upload_dirs):
            roots = ", ".join(str(r) for r in settings.upload_dirs) or "(none configured)"
            raise ToolError(f"{path} is outside ETSY_UPLOAD_DIRS ({roots}). Move the file there or add the folder to ETSY_UPLOAD_DIRS.")
        if not path.is_file():
            raise ToolError(f"File not found: {path}")
        return path

    async def load_blob(file_path: str | None, file_url: str | None, file_base64: str | None, filename: str | None) -> tuple[str, bytes, str]:
        cap = settings.max_upload_mb * 1024 * 1024
        if sum(x is not None and x != "" for x in (file_path, file_url, file_base64)) != 1:
            raise ToolError("Provide exactly one of file_path, file_url, file_base64.")
        if file_path:
            p = allowed_path(file_path)
            if p.stat().st_size > cap:
                raise ToolError(f"{p.name} is over {settings.max_upload_mb} MB.")
            data, name = p.read_bytes(), filename or p.name
        elif file_url:
            if not file_url.lower().startswith("https://"):
                raise ToolError("file_url must be https://")
            async with httpx.AsyncClient(follow_redirects=True, timeout=120) as h:
                r = await h.get(file_url)
                if r.status_code != 200:
                    raise ToolError(f"Download failed ({r.status_code}) for {file_url}")
                data = r.content
            if len(data) > cap:
                raise ToolError(f"Downloaded file is over {settings.max_upload_mb} MB.")
            name = filename or Path(httpx.URL(file_url).path).name or "upload.bin"
        else:
            if not filename:
                raise ToolError("filename is required with file_base64.")
            try:
                data = base64.b64decode(file_base64 or "", validate=True)
            except ValueError as e:
                raise ToolError("file_base64 is not valid base64.") from e
            name = filename
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return name, data, mime

    # ======================================================================= ACCOUNT & SHOP
    @mcp.tool(annotations=RO)
    async def etsy_whoami() -> dict[str, Any]:
        """Check the connection: the authorized Etsy user, their shop, server mode, and token health. Call this first if anything fails."""
        me = await call("GET", "/users/me")
        shop_id = me.get("shop_id")
        shop = await call("GET", f"/shops/{shop_id}") if shop_id else {}
        tok = etsy.tokens.load()
        return {
            "user_id": me.get("user_id"),
            "shop_id": shop_id,
            "shop_name": shop.get("shop_name"),
            "shop_url": shop.get("url"),
            "currency": shop.get("currency_code"),
            "active_listings": shop.get("listing_active_count"),
            "digital_listings": shop.get("digital_listing_count"),
            "sales": shop.get("transaction_sold_count"),
            "server_mode": settings.mode,
            "scopes": tok.get("scopes"),
            "token_expires": iso(tok.get("expires_at")),
            "upload_dirs": [str(p) for p in settings.upload_dirs],
        }

    @mcp.tool(annotations=RO)
    async def etsy_get_shop() -> dict[str, Any]:
        """Full shop record: name, title, announcement, sale messages, policies, counts, vacation status."""
        return await call("GET", f"/shops/{await sid()}")

    @mcp.tool(annotations=WRITE)
    async def etsy_update_shop(
        title: str | None = None,
        announcement: str | None = None,
        sale_message: str | None = None,
        digital_sale_message: str | None = None,
        policy_additional: str | None = None,
    ) -> dict[str, Any]:
        """Update shop text: title (headline), announcement, sale_message (sent to buyers after purchase), digital_sale_message (sent with digital downloads), policy_additional. Only passed fields change."""
        fields = {k: v for k, v in dict(title=title, announcement=announcement, sale_message=sale_message,
                                        digital_sale_message=digital_sale_message, policy_additional=policy_additional).items() if v is not None}
        async with wlog("etsy_update_shop", None, fields) as w:
            guard("write")
            if not fields:
                raise ToolError("Pass at least one field to change.")
            shop = await sid()
            w.before = await peek(f"/shops/{shop}", keys=list(fields))
            w.response = await call("PUT", f"/shops/{shop}", form=fields)
            return w.response

    @mcp.tool(annotations=RO)
    async def etsy_list_shop_sections() -> dict[str, Any]:
        """List shop sections (id, title, active listing count). Use the ids as shop_section_id on listings."""
        data = await call("GET", f"/shops/{await sid()}/sections")
        return {"sections": [{"shop_section_id": s.get("shop_section_id"), "title": s.get("title"),
                              "active_listings": s.get("active_listing_count")} for s in data.get("results", [])]}

    @mcp.tool(annotations=WRITE)
    async def etsy_create_shop_section(title: str) -> dict[str, Any]:
        """Create a shop section (e.g. 'Budget Spreadsheets'). Returns its shop_section_id."""
        async with wlog("etsy_create_shop_section", None, {"title": title}) as w:
            guard("write")
            w.response = await call("POST", f"/shops/{await sid()}/sections", form={"title": title})
            return w.response

    # ======================================================================= LISTINGS
    @mcp.tool(annotations=RO)
    async def etsy_list_listings(
        state: Literal["active", "inactive", "sold_out", "draft", "expired"] = "active",
        limit: int = 25,
        offset: int = 0,
        sort_on: Literal["created", "price", "updated", "score"] = "created",
        sort_order: Literal["asc", "desc"] = "desc",
        detail: Literal["summary", "full"] = "summary",
    ) -> dict[str, Any]:
        """List the shop's listings by state with views, favorites, price and tags. limit up to 500 (auto-paginates). Use next_offset to continue."""
        page = await etsy_paginate(f"/shops/{await sid()}/listings",
                                   {"state": state, "sort_on": sort_on, "sort_order": sort_order}, limit, offset)
        if detail == "summary":
            page["results"] = [listing_summary(l) for l in page["results"]]
        return page

    async def etsy_paginate(path: str, params: dict[str, Any], limit: int, offset: int) -> dict[str, Any]:
        try:
            return await etsy.paginate(path, params, max(1, min(limit, 500)), max(0, offset))
        except EtsyError as e:
            raise ToolError(str(e)) from e

    @mcp.tool(annotations=RO)
    async def etsy_get_listing(listing_id: int, includes: list[Literal["Images", "Inventory", "Shipping", "Videos", "Translations"]] | None = None) -> dict[str, Any]:
        """Full listing record (description, tags, materials, state, digital flag). Add includes=['Images','Inventory'] for photos and variations."""
        return await call("GET", f"/listings/{listing_id}", params={"includes": includes or None})

    @mcp.tool(annotations=WRITE)
    async def etsy_create_draft_listing(
        title: str,
        description: str,
        price: float,
        taxonomy_id: int,
        quantity: int = 999,
        type: Literal["download", "physical", "both"] = "download",
        who_made: Literal["i_did", "someone_else", "collective"] = "i_did",
        when_made: str = "made_to_order",
        is_supply: bool = False,
        tags: list[str] | None = None,
        materials: list[str] | None = None,
        shop_section_id: int | None = None,
        shipping_profile_id: int | None = None,
        readiness_state_id: int | None = None,
        extra_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a DRAFT listing (no fee until published). Defaults suit digital downloads: type=download, who_made=i_did, when_made=made_to_order, quantity=999.
        Physical items also need shipping_profile_id (and readiness_state_id). Find taxonomy_id with etsy_search_taxonomy. Up to 13 tags, 20 chars each."""
        body: dict[str, Any] = {
            "title": title, "description": description, "price": price, "quantity": quantity,
            "taxonomy_id": taxonomy_id, "who_made": who_made, "when_made": when_made, "is_supply": is_supply,
            "type": type, "tags": tags, "materials": materials, "shop_section_id": shop_section_id,
            "shipping_profile_id": shipping_profile_id, "readiness_state_id": readiness_state_id,
        }
        body.update(extra_fields or {})
        async with wlog("etsy_create_draft_listing", None, body) as w:
            guard("write")
            check = seo_check(title, tags or [], description, materials or [])
            if not check["ok"]:
                raise ToolError("Fix before creating: " + " | ".join(check["errors"]))
            if type != "download" and not shipping_profile_id:
                raise ToolError("Physical listings need shipping_profile_id (see etsy_list_shipping_profiles).")
            res = await call("POST", f"/shops/{await sid()}/listings", form=body)
            w.response = res
            return {"listing": listing_summary(res), "seo_warnings": check["warnings"]}

    async def update_listing(tool: str, listing_id: int, fields: dict[str, Any], confirm_publish_fee: bool) -> dict[str, Any]:
        async with wlog(tool, listing_id, fields) as w:
            guard("write")
            unknown = set(fields) - UPDATABLE_LISTING_FIELDS
            if unknown:
                raise ToolError(f"Unknown fields {sorted(unknown)}. Allowed: {sorted(UPDATABLE_LISTING_FIELDS)}")
            if fields.get("state") == "active" and not confirm_publish_fee:
                raise ToolError("Publishing charges Etsy's listing fee. Get the user's OK, then retry with confirm_publish_fee=true.")
            if any(k in fields for k in ("title", "tags", "materials")):
                check = seo_check(fields.get("title", ""), fields.get("tags"), "", fields.get("materials"))
                if not check["ok"]:
                    raise ToolError("Fix before updating: " + " | ".join(check["errors"]))
            w.before = await peek(f"/listings/{listing_id}", keys=list(fields))
            res = await call("PATCH", f"/shops/{await sid()}/listings/{listing_id}", form=fields)
            w.response = res
            return {"listing": listing_summary(res)}

    @mcp.tool(annotations=WRITE)
    async def etsy_update_listing(listing_id: int, fields: dict[str, Any], confirm_publish_fee: bool = False) -> dict[str, Any]:
        """Patch a listing. fields may include title, description, price, quantity, tags, materials, taxonomy_id, shop_section_id, who_made, when_made, should_auto_renew, state, etc.
        Setting state='active' publishes and charges Etsy's listing fee: requires confirm_publish_fee=true. state='inactive' deactivates."""
        return await update_listing("etsy_update_listing", listing_id, fields, confirm_publish_fee)

    @mcp.tool(annotations=WRITE)
    async def etsy_publish_listing(listing_id: int, confirm_publish_fee: bool = False) -> dict[str, Any]:
        """Publish a draft or inactive listing (state=active). Charges Etsy's listing fee, so only call after the user explicitly approves; pass confirm_publish_fee=true.
        Etsy requires at least one image (and, for downloads, at least one file) before a listing can go live."""
        return await update_listing("etsy_publish_listing", listing_id, {"state": "active"}, confirm_publish_fee)

    @mcp.tool(annotations=DELETE)
    async def etsy_delete_listing(listing_id: int) -> dict[str, Any]:
        """Permanently delete a listing. Blocked unless ETSY_MCP_MODE=full. Prefer etsy_update_listing state='inactive' to hide it instead."""
        async with wlog("etsy_delete_listing", listing_id, {"listing_id": listing_id}) as w:
            guard("delete")
            w.before = await peek(f"/listings/{listing_id}", shape=listing_summary)
            w.response = await call("DELETE", f"/listings/{listing_id}")
            return w.response

    # ---- images
    @mcp.tool(annotations=RO)
    async def etsy_list_listing_images(listing_id: int) -> dict[str, Any]:
        """List a listing's photos (listing_image_id, rank, alt text, full-size URL)."""
        data = await call("GET", f"/listings/{listing_id}/images")
        return {"images": [{"listing_image_id": i.get("listing_image_id"), "rank": i.get("rank"), "alt_text": i.get("alt_text"),
                            "url": i.get("url_fullxfull"), "width": i.get("full_width"), "height": i.get("full_height")}
                           for i in data.get("results", [])]}

    @mcp.tool(annotations=WRITE)
    async def etsy_upload_listing_image(
        listing_id: int,
        file_path: str | None = None,
        file_url: str | None = None,
        file_base64: str | None = None,
        filename: str | None = None,
        rank: int | None = None,
        alt_text: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Add a photo to a listing from ONE source: file_path (inside ETSY_UPLOAD_DIRS), file_url (https), or file_base64 (+filename). rank 1 = primary image."""
        req = {"listing_id": listing_id, "source": file_path or file_url or "base64", "filename": filename, "rank": rank,
               "alt_text": alt_text, "overwrite": overwrite}
        async with wlog("etsy_upload_listing_image", listing_id, req) as w:
            guard("write")
            name, data, mime = await load_blob(file_path, file_url, file_base64, filename)
            req["bytes"] = len(data)
            form = {"rank": rank, "alt_text": alt_text, "overwrite": overwrite if rank else None}
            res = await call("POST", f"/shops/{await sid()}/listings/{listing_id}/images", form=form, files={"image": (name, data, mime)})
            w.response = out = {"listing_image_id": res.get("listing_image_id"), "rank": res.get("rank"), "url": res.get("url_fullxfull")}
            return out

    @mcp.tool(annotations=DELETE)
    async def etsy_delete_listing_image(listing_id: int, listing_image_id: int) -> dict[str, Any]:
        """Remove a photo from a listing. Blocked unless ETSY_MCP_MODE=full."""
        req = {"listing_id": listing_id, "listing_image_id": listing_image_id}
        async with wlog("etsy_delete_listing_image", listing_id, req) as w:
            guard("delete")
            w.response = await call("DELETE", f"/shops/{await sid()}/listings/{listing_id}/images/{listing_image_id}")
            return w.response

    # ---- digital files
    @mcp.tool(annotations=RO)
    async def etsy_list_listing_files(listing_id: int) -> dict[str, Any]:
        """List the downloadable files attached to a digital listing."""
        data = await call("GET", f"/shops/{await sid()}/listings/{listing_id}/files")
        return {"files": [{"listing_file_id": f.get("listing_file_id"), "filename": f.get("filename"),
                           "size": f.get("filesize"), "rank": f.get("rank")} for f in data.get("results", [])]}

    @mcp.tool(annotations=WRITE)
    async def etsy_upload_listing_file(
        listing_id: int,
        file_path: str | None = None,
        file_url: str | None = None,
        file_base64: str | None = None,
        filename: str | None = None,
        name: str | None = None,
        rank: int | None = None,
    ) -> dict[str, Any]:
        """Attach a downloadable file (PDF, XLSX, ZIP...) to a digital listing from ONE source: file_path, file_url (https) or file_base64 (+filename).
        name = filename buyers see. Etsy caps digital files per listing (5 at time of writing) and size per file."""
        req = {"listing_id": listing_id, "source": file_path or file_url or "base64", "filename": filename, "name": name, "rank": rank}
        async with wlog("etsy_upload_listing_file", listing_id, req) as w:
            guard("write")
            fname, data, mime = await load_blob(file_path, file_url, file_base64, filename)
            req["bytes"] = len(data)
            res = await call("POST", f"/shops/{await sid()}/listings/{listing_id}/files",
                             form={"name": name or fname, "rank": rank}, files={"file": (fname, data, mime)})
            w.response = out = {"listing_file_id": res.get("listing_file_id"), "filename": res.get("filename"), "size": res.get("filesize")}
            return out

    @mcp.tool(annotations=DELETE)
    async def etsy_delete_listing_file(listing_id: int, listing_file_id: int) -> dict[str, Any]:
        """Remove a downloadable file from a listing. Blocked unless ETSY_MCP_MODE=full."""
        req = {"listing_id": listing_id, "listing_file_id": listing_file_id}
        async with wlog("etsy_delete_listing_file", listing_id, req) as w:
            guard("delete")
            w.response = await call("DELETE", f"/shops/{await sid()}/listings/{listing_id}/files/{listing_file_id}")
            return w.response

    # ---- inventory
    @mcp.tool(annotations=RO)
    async def etsy_get_listing_inventory(listing_id: int) -> dict[str, Any]:
        """Variations/SKUs/per-option prices and quantities for a listing (raw Etsy inventory object)."""
        return await call("GET", f"/listings/{listing_id}/inventory")

    @mcp.tool(annotations=WRITE)
    async def etsy_update_listing_inventory(listing_id: int, inventory: dict[str, Any]) -> dict[str, Any]:
        """Replace a listing's inventory. Pass the full object Etsy expects: {products:[{sku, property_values, offerings:[{price, quantity, is_enabled}]}], price_on_property, quantity_on_property, sku_on_property}.
        Read it first with etsy_get_listing_inventory, edit, then send it back whole."""
        async with wlog("etsy_update_listing_inventory", listing_id, inventory) as w:
            guard("write")
            if "products" not in inventory:
                raise ToolError("inventory must include 'products'. Start from etsy_get_listing_inventory output.")
            w.before = await peek(f"/listings/{listing_id}/inventory")
            w.response = await call("PUT", f"/listings/{listing_id}/inventory", json=inventory)
            return w.response

    # ---- workflow: one-shot digital product
    @mcp.tool(annotations=WRITE)
    async def etsy_create_digital_listing(
        manifest_path: str | None = None,
        title: str | None = None,
        description: str | None = None,
        price: float | None = None,
        taxonomy_id: int | None = None,
        tags: list[str] | None = None,
        materials: list[str] | None = None,
        shop_section_id: int | None = None,
        image_paths: list[str] | None = None,
        image_urls: list[str] | None = None,
        file_paths: list[str] | None = None,
        file_urls: list[str] | None = None,
        publish: bool = False,
        confirm_publish_fee: bool = False,
    ) -> dict[str, Any]:
        """One call: create a digital-download draft, upload images in order (first = primary), attach the files, optionally publish.
        Easiest with manifest_path -> a listing.json inside ETSY_UPLOAD_DIRS (paths in it resolve relative to its folder). Explicit args override the manifest.
        Leaves the listing as a draft unless publish=true AND confirm_publish_fee=true. Partial failures are reported, not hidden."""
        # The sub-calls below each write their own log entries; this one is the summary.
        async with wlog("etsy_create_digital_listing", None, {"manifest_path": manifest_path, "title": title, "publish": publish}) as w:
            guard("write")
            report = await _create_digital_listing(
                manifest_path, title, description, price, taxonomy_id, tags, materials, shop_section_id,
                image_paths, image_urls, file_paths, file_urls, publish, confirm_publish_fee)
            w.response = report
            lid = report.get("listing_id")
            w.request["listing_id"] = lid
            return report

    async def _create_digital_listing(manifest_path, title, description, price, taxonomy_id, tags, materials, shop_section_id,
                                      image_paths, image_urls, file_paths, file_urls, publish, confirm_publish_fee) -> dict[str, Any]:
        m: dict[str, Any] = {}
        base: Path | None = None
        if manifest_path:
            mp = allowed_path(manifest_path)
            try:
                m = json.loads(mp.read_text())
            except json.JSONDecodeError as e:
                raise ToolError(f"{mp.name} is not valid JSON: {e}") from e
            base = mp.parent

        def pick(key: str, val: Any) -> Any:
            return val if val is not None else m.get(key)

        def rel(paths: list[str] | None) -> list[str]:
            return [str((base / p) if base and not Path(p).expanduser().is_absolute() else Path(p).expanduser()) for p in (paths or [])]

        title_, desc_, price_, tax_ = pick("title", title), pick("description", description), pick("price", price), pick("taxonomy_id", taxonomy_id)
        missing = [k for k, v in (("title", title_), ("description", desc_), ("price", price_), ("taxonomy_id", tax_)) if v in (None, "")]
        if missing:
            raise ToolError(f"Missing {missing} (pass them or put them in the manifest).")
        imgs = rel(image_paths if image_paths is not None else m.get("images"))
        files = rel(file_paths if file_paths is not None else m.get("files"))
        img_urls = image_urls if image_urls is not None else m.get("image_urls", [])
        f_urls = file_urls if file_urls is not None else m.get("file_urls", [])
        # Validate local paths up front so we don't leave a half-built draft for a typo.
        for p in imgs + files:
            allowed_path(p)
        if not (imgs or img_urls):
            raise ToolError("At least one image is required (Etsy won't publish without one).")
        if not (files or f_urls):
            raise ToolError("At least one downloadable file is required for a digital listing.")

        created = await etsy_create_draft_listing(
            title=title_, description=desc_, price=float(price_), taxonomy_id=int(tax_),
            quantity=int(m.get("quantity", 999)), type="download", who_made=m.get("who_made", "i_did"),
            when_made=m.get("when_made", "made_to_order"), tags=pick("tags", tags), materials=pick("materials", materials),
            shop_section_id=pick("shop_section_id", shop_section_id), extra_fields=m.get("extra_fields"),
        )
        lid = created["listing"]["listing_id"]
        report: dict[str, Any] = {"listing_id": lid, "url": created["listing"].get("url"), "state": "draft",
                                  "seo_warnings": created["seo_warnings"], "images": [], "files": [], "errors": []}
        rank = 1
        for p in imgs:
            try:
                report["images"].append(await etsy_upload_listing_image(lid, file_path=p, rank=rank)); rank += 1
            except ToolError as e:
                report["errors"].append(f"image {p}: {e}")
        for u in img_urls:
            try:
                report["images"].append(await etsy_upload_listing_image(lid, file_url=u, rank=rank)); rank += 1
            except ToolError as e:
                report["errors"].append(f"image {u}: {e}")
        for i, p in enumerate(files, 1):
            try:
                report["files"].append(await etsy_upload_listing_file(lid, file_path=p, rank=i))
            except ToolError as e:
                report["errors"].append(f"file {p}: {e}")
        for i, u in enumerate(f_urls, len(files) + 1):
            try:
                report["files"].append(await etsy_upload_listing_file(lid, file_url=u, rank=i))
            except ToolError as e:
                report["errors"].append(f"file {u}: {e}")
        if publish:
            if report["errors"]:
                report["publish"] = "skipped: fix upload errors first"
            elif not confirm_publish_fee:
                report["publish"] = "skipped: needs confirm_publish_fee=true (listing fee applies)"
            else:
                await etsy_publish_listing(lid, confirm_publish_fee=True)
                report["state"] = "active"
        return report

    # ======================================================================= MARKET & TAXONOMY
    @mcp.tool(annotations=RO)
    async def etsy_search_active_listings(
        keywords: str,
        limit: int = 25,
        offset: int = 0,
        sort_on: Literal["created", "price", "updated", "score"] = "score",
        sort_order: Literal["asc", "desc"] = "desc",
        min_price: float | None = None,
        max_price: float | None = None,
        taxonomy_id: int | None = None,
    ) -> dict[str, Any]:
        """Search all active Etsy listings (public marketplace) for quick competitive checks: titles, prices, tags, favorites. Max 100 per call. For deep market research use a dedicated research tool."""
        params = {"keywords": keywords, "sort_on": sort_on, "sort_order": sort_order, "min_price": min_price,
                  "max_price": max_price, "taxonomy_id": taxonomy_id, "limit": max(1, min(limit, 100)), "offset": offset}
        data = await call("GET", "/listings/active", params=params)
        return {"count": data.get("count"), "results": [listing_summary(l) for l in data.get("results", [])]}

    async def taxonomy_flat() -> list[dict[str, Any]]:
        if "flat" in taxonomy_cache:
            return taxonomy_cache["flat"]
        data = await call("GET", "/seller-taxonomy/nodes")
        flat: list[dict[str, Any]] = []

        def walk(nodes: list[dict[str, Any]], trail: list[str]) -> None:
            for n in nodes:
                path = trail + [n.get("name", "")]
                kids = n.get("children") or []
                flat.append({"taxonomy_id": n.get("id"), "path": " > ".join(path), "leaf": not kids})
                walk(kids, path)

        walk(data.get("results", []), [])
        taxonomy_cache["flat"] = flat
        return flat

    @mcp.tool(annotations=RO)
    async def etsy_search_taxonomy(query: str, limit: int = 15, leaves_only: bool = True) -> dict[str, Any]:
        """Find the taxonomy_id (category) for a listing, e.g. 'planner', 'spreadsheet template', 'wall art printable'. Leaf categories are the most specific and usually best."""
        terms = [t for t in query.lower().split() if t]
        hits = []
        for n in await taxonomy_flat():
            if leaves_only and not n["leaf"]:
                continue
            p = n["path"].lower()
            s = sum(1 for t in terms if t in p)
            if s:
                hits.append((s + (2 if terms and terms[-1] in p.split(" > ")[-1] else 0), n))
        hits.sort(key=lambda x: (-x[0], len(x[1]["path"])))
        return {"matches": [h[1] for h in hits[:limit]]}

    @mcp.tool(annotations=RO)
    async def etsy_get_taxonomy_properties(taxonomy_id: int) -> dict[str, Any]:
        """Attributes a category supports (e.g. occasion, holiday, file type) and their allowed values, for richer listings."""
        data = await call("GET", f"/seller-taxonomy/nodes/{taxonomy_id}/properties")
        return {"properties": [{"property_id": p.get("property_id"), "name": p.get("display_name") or p.get("name"),
                                "required": p.get("is_required"), "multi": p.get("is_multivalued"),
                                "values": [v.get("name") for v in (p.get("possible_values") or [])][:60]}
                               for p in data.get("results", [])]}

    # ======================================================================= ORDERS
    @mcp.tool(annotations=RO)
    async def etsy_list_orders(
        since: str | None = None,
        until: str | None = None,
        paid: bool | None = None,
        shipped: bool | None = None,
        limit: int = 25,
        offset: int = 0,
        detail: Literal["summary", "full"] = "summary",
    ) -> dict[str, Any]:
        """List orders (receipts) newest first, with items and totals. since/until accept YYYY-MM-DD or ISO-8601. Digital orders deliver automatically; physical ones may need tracking."""
        params = {"min_created": to_epoch(since), "max_created": to_epoch(until), "was_paid": paid, "was_shipped": shipped,
                  "sort_on": "created", "sort_order": "desc"}
        page = await etsy_paginate(f"/shops/{await sid()}/receipts", params, limit, offset)
        if detail == "summary":
            page["results"] = [receipt_summary(r) for r in page["results"]]
        return page

    @mcp.tool(annotations=RO)
    async def etsy_get_order(receipt_id: int) -> dict[str, Any]:
        """One order with its line items, totals and shipping status."""
        shop = await sid()
        r = await call("GET", f"/shops/{shop}/receipts/{receipt_id}")
        if not r.get("transactions"):
            tx = await call("GET", f"/shops/{shop}/receipts/{receipt_id}/transactions")
            r["transactions"] = tx.get("results", [])
        out = receipt_summary(r)
        out["shipments"] = r.get("shipments")
        return out

    @mcp.tool(annotations=WRITE)
    async def etsy_add_tracking(receipt_id: int, tracking_code: str, carrier_name: str, note_to_buyer: str | None = None, send_bcc: bool = False) -> dict[str, Any]:
        """Mark a physical order shipped with tracking (sends Etsy's shipping notification to the buyer). carrier_name like 'usps', 'ups', 'fedex'."""
        form = {"tracking_code": tracking_code, "carrier_name": carrier_name, "note_to_buyer": note_to_buyer, "send_bcc": send_bcc}
        async with wlog("etsy_add_tracking", None, {"receipt_id": receipt_id, **form}) as w:
            guard("write")
            w.response = await call("POST", f"/shops/{await sid()}/receipts/{receipt_id}/tracking", form=form)
            return w.response

    @mcp.tool(annotations=RO)
    async def etsy_sales_summary(since: str, until: str | None = None, max_orders: int = 500) -> dict[str, Any]:
        """Revenue report for a window: paid orders, gross, average order value, and top listings by revenue. since/until as YYYY-MM-DD."""
        page = await etsy_paginate(f"/shops/{await sid()}/receipts",
                                   {"min_created": to_epoch(since), "max_created": to_epoch(until), "was_paid": True},
                                   max_orders, 0)
        orders = [receipt_summary(r) for r in page["results"]]
        gross = round(sum(o["total"] or 0 for o in orders), 2)
        by_listing: dict[Any, dict[str, Any]] = {}
        for o in orders:
            for it in o["items"]:
                row = by_listing.setdefault(it["listing_id"], {"listing_id": it["listing_id"], "title": it["title"], "units": 0, "revenue": 0.0})
                row["units"] += it["quantity"] or 0
                row["revenue"] = round(row["revenue"] + (it["price"] or 0) * (it["quantity"] or 0), 2)
        top = sorted(by_listing.values(), key=lambda r: r["revenue"], reverse=True)[:15]
        return {
            "window": {"since": since, "until": until or "now"},
            "orders": len(orders),
            "gross": gross,
            "currency": next((o["currency"] for o in orders if o["currency"]), None),
            "avg_order_value": round(gross / len(orders), 2) if orders else 0,
            "top_listings": top,
            "truncated": page["next_offset"] is not None,
            "note": "Gross = order totals incl. shipping/tax, before Etsy fees. Use etsy_list_ledger_entries for fees and net.",
        }

    # ======================================================================= MONEY, REVIEWS, POLICIES
    @mcp.tool(annotations=RO)
    async def etsy_list_ledger_entries(since: str, until: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """Payment-account ledger: sales credits, Etsy fees, ad charges, deposits, refunds. since/until as YYYY-MM-DD (until defaults to now). Needs billing_r scope."""
        until_epoch = to_epoch(until) or int(datetime.now(tz=timezone.utc).timestamp())
        page = await etsy_paginate(f"/shops/{await sid()}/payment-account/ledger-entries",
                                   {"min_created": to_epoch(since), "max_created": until_epoch}, limit, offset)
        page["results"] = [{"entry_id": e.get("entry_id"), "created": iso(e.get("create_date") or e.get("created_timestamp")),
                            "type": e.get("ledger_type") or e.get("description"), "description": e.get("description"),
                            "amount": (e.get("amount") or 0) / 100 if isinstance(e.get("amount"), int) else e.get("amount"),
                            "currency": e.get("currency"), "balance": (e.get("balance") or 0) / 100 if isinstance(e.get("balance"), int) else e.get("balance"),
                            "reference_type": e.get("reference_type"), "reference_id": e.get("reference_id")}
                           for e in page["results"]]
        return page

    @mcp.tool(annotations=RO)
    async def etsy_list_reviews(limit: int = 25, offset: int = 0, since: str | None = None) -> dict[str, Any]:
        """Recent shop reviews: rating, text, listing, date."""
        page = await etsy_paginate(f"/shops/{await sid()}/reviews", {"min_created": to_epoch(since)}, limit, offset)
        page["results"] = [{"listing_id": r.get("listing_id"), "rating": r.get("rating"), "review": r.get("review"),
                            "created": iso(r.get("create_timestamp") or r.get("created_timestamp")), "language": r.get("language")}
                           for r in page["results"]]
        ratings = [r["rating"] for r in page["results"] if r["rating"]]
        page["avg_rating_in_page"] = round(sum(ratings) / len(ratings), 2) if ratings else None
        return page

    @mcp.tool(annotations=RO)
    async def etsy_list_shipping_profiles() -> dict[str, Any]:
        """Shipping profiles (ids needed for physical listings)."""
        data = await call("GET", f"/shops/{await sid()}/shipping-profiles")
        return {"profiles": [{"shipping_profile_id": p.get("shipping_profile_id"), "title": p.get("title"),
                              "origin_country": p.get("origin_country_iso"), "processing_days": [p.get("min_processing_days"), p.get("max_processing_days")]}
                             for p in data.get("results", [])]}

    @mcp.tool(annotations=RO)
    async def etsy_list_return_policies() -> dict[str, Any]:
        """Return policies defined for the shop."""
        return await call("GET", f"/shops/{await sid()}/policies/return")

    # ======================================================================= UTILITIES
    @mcp.tool(name="etsy_seo_check", annotations=LOCAL)
    async def etsy_seo_check_tool(title: str = "", tags: list[str] | None = None, description: str = "", materials: list[str] | None = None) -> dict[str, Any]:
        """Lint a listing before you create or edit it: title length and once-only characters, 13x20-char tag rules, duplicates, weak tags, thin descriptions. No API call."""
        return seo_check(title, tags, description, materials)

    @mcp.tool(annotations=RO)
    async def etsy_listing_audit(state: Literal["active", "draft", "inactive", "expired"] = "active", limit: int = 100) -> dict[str, Any]:
        """Scan the shop's listings and flag fixable problems: unused tag slots, over-long titles, zero views, expiring soon, missing section. Sorted worst first."""
        page = await etsy_paginate(f"/shops/{await sid()}/listings", {"state": state}, limit, 0)
        now = datetime.now(tz=timezone.utc).timestamp()
        rows = []
        for l in page["results"]:
            issues = []
            chk = seo_check(l.get("title") or "", l.get("tags") or [])
            issues += chk["errors"] + [w for w in chk["warnings"] if "tags used" in w or "ALL-CAPS" in w or "stuffing" in w]
            if state == "active" and not l.get("views"):
                issues.append("0 views")
            if l.get("ending_timestamp") and l["ending_timestamp"] - now < 14 * 86400:
                issues.append("expires within 14 days")
            if not l.get("shop_section_id"):
                issues.append("no shop section")
            if issues:
                rows.append({"listing_id": l.get("listing_id"), "title": l.get("title"), "views": l.get("views"),
                             "favorites": l.get("num_favorers"), "issues": issues})
        rows.sort(key=lambda r: len(r["issues"]), reverse=True)
        return {"scanned": len(page["results"]), "flagged": len(rows), "listings": rows}

    @mcp.tool(annotations=RO)
    async def etsy_find_endpoint(query: str, limit: int = 8) -> dict[str, Any]:
        """Search Etsy's official OpenAPI spec for an endpoint not covered by a dedicated tool (e.g. 'shop production partners', 'listing translation'). Returns method, path, params, body fields, scopes."""
        try:
            return {"endpoints": await spec.search(etsy.http, query, limit)}
        except httpx.HTTPError as e:
            raise ToolError(f"Couldn't load the Etsy spec from {settings.oas_url}: {e}. Set ETSY_OAS_URL if it moved.") from e

    @mcp.tool(annotations=WRITE)
    async def etsy_api_request(
        method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"],
        path: str,
        query: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
        json_body: dict[str, Any] | list[Any] | None = None,
    ) -> Any:
        """Escape hatch: call any Etsy Open API v3 endpoint on this shop. path is relative to /v3/application, e.g. '/shops/{shop_id}/listings' ({shop_id} is filled in).
        Use form for x-www-form-urlencoded bodies (most POST/PATCH), json_body for JSON (inventory). Writes respect ETSY_MCP_MODE; DELETE needs full."""
        if "://" in path or not path.startswith("/") or ".." in path:
            raise ToolError("path must be a relative API path like '/shops/{shop_id}/sections'.")
        if method == "GET":
            if "{shop_id}" in path:
                path = path.replace("{shop_id}", await sid())
            return await call(method, path, params=query)
        async with wlog("etsy_api_request", None, {"method": method, "path": path, "query": query, "form": form, "json": json_body}) as w:
            guard("delete" if method == "DELETE" else "write")
            if "{shop_id}" in path:
                path = path.replace("{shop_id}", await sid())
            w.response = await call(method, path, params=query, form=form, json=json_body)
            return w.response

    # ======================================================================= RESEARCH CACHE & WRITE LOG
    def _rows(rows: Any) -> list[dict[str, Any]]:
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ToolError("rows must be a list of objects.")
        return rows

    @mcp.tool(annotations=LOCAL_WRITE)
    async def research_save_keywords(rows: list[dict[str, Any]], source: str = "profittree") -> dict[str, Any]:
        """Store keyword rows Claude got from a research source (default 'profittree') in the local cache. Each row needs 'keyword'; optional searches, clicks, competition, digital_share, trend, niche_score (the full row is kept as raw_json).
        Saving the same keyword+source again replaces it and refreshes its age. Local only: no Etsy call, allowed in every server mode."""
        return research.save_keywords(_rows(rows), source.strip() or "profittree")

    @mcp.tool(annotations=LOCAL_WRITE)
    async def research_save_market_listings(rows: list[dict[str, Any]], keyword: str, source: str = "profittree") -> dict[str, Any]:
        """Store competitor/market listings found for a keyword. Each row needs 'listing_id'; optional shop_id, title, price, tags (list), views, favorites, est_monthly_sales, est_monthly_revenue.
        Same listing+keyword+source is replaced. Local only: no Etsy call, allowed in every server mode."""
        try:
            return research.save_market_listings(_rows(rows), keyword, source.strip() or "profittree")
        except ValueError as e:
            raise ToolError(str(e)) from e

    @mcp.tool(annotations=LOCAL)
    async def research_get_keywords(seed: str | None = None, max_age_days: float = 7, limit: int = 200) -> dict[str, Any]:
        """Cached keyword rows (optionally those containing `seed`), best niche_score first, each with age_days and stale flag. status is fresh / partial / stale / missing; when stale or missing, re-fetch from the source and save again."""
        return research.get_keywords(seed, max_age_days, limit)

    @mcp.tool(annotations=LOCAL)
    async def research_get_market(keyword: str, max_age_days: float = 7, limit: int = 200) -> dict[str, Any]:
        """Cached market listings for an exact keyword with age_days/stale per row, plus a summary (avg price, total est. revenue). status is fresh / partial / stale / missing."""
        return research.get_market(keyword, max_age_days, limit)

    @mcp.tool(annotations=LOCAL)
    async def research_cache_stats() -> dict[str, Any]:
        """Row counts and oldest/newest timestamps for every research table (keywords, market_listings, competitor_snapshots, audits, write_log), plus db path and schema version."""
        return research.stats()

    @mcp.tool(annotations=LOCAL)
    async def etsy_write_log(limit: int = 50, listing_id: int | None = None) -> dict[str, Any]:
        """Read the append-only log of writes this server made (newest first): tool, listing_id, server mode, dry_run, before/after, result. Filter by listing_id. Nothing can delete rows."""
        return {"entries": research.read_write_log(limit, listing_id)}

    # ======================================================================= PROMPTS
    @mcp.prompt()
    def publish_digital_product(product_folder: str, price: str = "") -> str:
        """Turn a product folder into a reviewed Etsy draft listing."""
        return (
            f"Prepare the digital product in {product_folder} for Etsy.\n"
            "1. Read listing.json if it exists; otherwise inspect files/ and images/ and draft one.\n"
            "2. Find the best leaf category with etsy_search_taxonomy.\n"
            "3. Write a buyer-first title, 13 tags (multi-word, <=20 chars), and a description covering what's included, "
            "file formats, how to open them, and that it's an instant digital download.\n"
            f"4. Price: {price or 'suggest one after checking 10-20 comparable listings with etsy_search_active_listings'}.\n"
            "5. Run etsy_seo_check and fix every error.\n"
            "6. Show me the full listing for approval. Only after I approve, call etsy_create_digital_listing (draft only).\n"
            "7. Report the draft URL. Do not publish unless I say 'publish'."
        )

    @mcp.prompt()
    def weekly_shop_report(days: str = "7") -> str:
        """Sales, fees, reviews and listing health for the last N days."""
        return (
            f"Build my Etsy shop report for the last {days} days.\n"
            "Use etsy_sales_summary, etsy_list_ledger_entries (fees vs sales), etsy_list_reviews, and etsy_listing_audit.\n"
            "Give me: revenue and orders vs the previous equal period, net after fees, best and worst listings, "
            "new reviews, and the 3 highest-leverage fixes for this week."
        )

    @mcp.prompt()
    def listing_seo_audit(limit: str = "50") -> str:
        """Audit and rewrite weak listings."""
        return (
            f"Run etsy_listing_audit on up to {limit} active listings. For the 5 worst, propose a new title and 13 tags, "
            "validated with etsy_seo_check, shown as before/after. Apply nothing until I approve each one."
        )

    mcp._etsy_client = etsy  # type: ignore[attr-defined]  # exposed for tests
    return mcp


# ----------------------------------------------------------------------------- HTTP wrapper
class BearerAuth:
    """Pure-ASGI guard: /mcp requires Authorization: Bearer <MCP_AUTH_TOKEN>; /health is open."""

    def __init__(self, app: Any, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") == "/health" or not self.token:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"")
        ok = auth.startswith(b"Bearer ") and hmac.compare_digest(auth[7:].strip(), self.token)
        if not ok:
            body = b'{"error":"unauthorized"}'
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body", "body": body})
            return
        return await self.app(scope, receive, send)


def build_http_app(settings: Settings, mcp: MCPServer) -> Any:
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import JSONResponse

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Any) -> Any:
        return JSONResponse({"ok": True, "server": "indigorepublica-etsy", "version": __version__})

    hosts = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"]
    for h in settings.public_hosts:
        hosts += [h, f"{h}:*"]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=["https://claude.ai", "https://claude.com", "http://localhost:*", "http://127.0.0.1:*"],
    )
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=security,
        max_request_body_size=(settings.max_upload_mb + 10) * 1024 * 1024,
        host=settings.http_host,
    )
    return BearerAuth(app, settings.auth_token)


# ----------------------------------------------------------------------------- entrypoint
def main() -> None:
    ap = argparse.ArgumentParser(description="IndigoRepublica Etsy MCP server")
    ap.add_argument("--transport", choices=["stdio", "http"], default=os.environ.get("MCP_TRANSPORT", "stdio"))
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    args = ap.parse_args()

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    if args.host:
        settings.http_host = args.host
    if args.port:
        settings.http_port = args.port
    mcp = build_server(settings)

    if args.transport == "stdio":
        log.info("indigorepublica-etsy %s on stdio (mode=%s)", __version__, settings.mode)
        mcp.run("stdio")
        return

    if not settings.auth_token and os.environ.get("MCP_ALLOW_NO_AUTH") != "1":
        sys.exit("Refusing to start HTTP mode without MCP_AUTH_TOKEN (anyone with the URL could run your shop). "
                 "Generate one: openssl rand -hex 32")
    if not settings.public_hosts:
        log.warning("MCP_PUBLIC_HOSTS is empty: requests through a tunnel will be rejected (Host header check).")
    import uvicorn

    log.info("indigorepublica-etsy %s on http://%s:%s/mcp (mode=%s, public hosts=%s)", __version__,
             settings.http_host, settings.http_port, settings.mode, settings.public_hosts)
    uvicorn.run(build_http_app(settings, mcp), host=settings.http_host, port=settings.http_port,
                log_level="info", proxy_headers=True, forwarded_allow_ips="127.0.0.1")


if __name__ == "__main__":
    main()
