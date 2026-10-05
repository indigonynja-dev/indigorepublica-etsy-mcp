"""Read-only MCP resources: cached data exposed as etsy:// URIs.

  etsy://shop/listings          your active listings: id, title, price, state, last updated
  etsy://keywords/{seed}        cached keyword rows containing a seed (research cache)
  etsy://competitor/{shop}      latest competitor profile (from the newest snapshot)
  etsy://audit/{listing_id}     latest stored SEO audit for a listing
  etsy://writes/recent          last 50 rows of the append-only write log

Resources never write: no Etsy write, no database write, no write-log row. Four of them only read the local SQLite
cache. The fifth (the shop's own listings) makes one Etsy read that is kept in memory for a few minutes and dropped
whenever this server writes, so a resource read right after an edit shows the edit.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError, ToolError

from . import seo
from .client import EtsyClient, EtsyError
from .competitors import CompetitorError, Competitors
from .research import ResearchDB, age_days, norm_keyword, now_iso

JSON = "application/json"

LISTINGS_URI = "etsy://shop/listings"
KEYWORDS_URI = "etsy://keywords/{seed}"
COMPETITOR_URI = "etsy://competitor/{shop}"
AUDIT_URI = "etsy://audit/{listing_id}"
WRITES_URI = "etsy://writes/recent"

LISTINGS_TTL_SECONDS = 300
MAX_LISTINGS = 500  # same cap as the etsy_list_listings tool
LISTING_FIELDS = ("listing_id", "title", "price", "currency", "state", "updated")
RECENT_WRITES = 50
KEYWORD_MAX_AGE_DAYS = 7  # same default as the research_get_keywords tool
KEYWORD_LIMIT = 200
MAX_ID_DIGITS = 18  # keeps the id inside SQLite's 64-bit INTEGER


class ListingsCache:
    """In-memory, time-limited cache for the one Etsy read behind etsy://shop/listings. Never persisted."""

    def __init__(self, ttl: float = LISTINGS_TTL_SECONDS, clock: Callable[[], float] = time.monotonic):
        self.ttl, self._clock = ttl, clock
        self._entry: tuple[float, dict[str, Any]] | None = None
        self._generation = 0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        """Forget the cached read (the server calls this on every write attempt) so the next read sees the change."""
        self._generation += 1
        self._entry = None

    async def get(self, fetch: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        """Cached read if it is younger than ttl, else call fetch() once. Concurrent readers share one fetch."""
        async with self._lock:
            now = self._clock()
            if self._entry is not None and now - self._entry[0] < self.ttl:
                return {**self._entry[1], "cached": True, "cache_age_seconds": int(now - self._entry[0])}
            generation = self._generation
            data = await fetch()
            if generation == self._generation:  # a write landed mid-fetch: this read is already stale, so don't keep it
                self._entry = (self._clock(), data)
            return {**data, "cached": False, "cache_age_seconds": 0}


def _listing_id(value: str) -> int:
    v = value.strip()
    if not (v.isascii() and v.isdigit()) or len(v) > MAX_ID_DIGITS:
        raise ResourceNotFoundError(f"listing_id must be a number like etsy://audit/1234567890, got {value!r}.")
    return int(v)


def register(mcp: Any, *, research: ResearchDB, competitors: Competitors, etsy: EtsyClient,
             sid: Callable[[], Awaitable[str]], summarize: Callable[[dict[str, Any]], dict[str, Any]],
             cache: ListingsCache) -> None:
    """Register the five resources on the server. `summarize` is the server's listing_summary (same price/date format as the tools)."""

    async def fetch_listings() -> dict[str, Any]:
        try:
            shop = await sid()
            page = await etsy.paginate(f"/shops/{shop}/listings", {"state": "active"}, MAX_LISTINGS)
        # RuntimeError is Settings.require_credentials(); its message tells the user exactly what to configure.
        except (ToolError, EtsyError, FileNotFoundError, RuntimeError) as e:
            raise ResourceError(str(e)) from e
        rows = [{k: s.get(k) for k in LISTING_FIELDS} for s in map(summarize, page["results"])]
        rows.sort(key=lambda r: r["updated"] or "", reverse=True)  # ISO-8601 UTC strings sort chronologically
        return {"shop_id": int(shop) if str(shop).isdigit() else shop, "count": page["count"], "returned": len(rows),
                "truncated": page["next_offset"] is not None, "fetched_at": now_iso(), "listings": rows}

    @mcp.resource(LISTINGS_URI, name="shop_listings", title="Active listings", mime_type=JSON,
                  description="Your shop's active listings (newest update first): listing_id, title, price, currency, state, updated. "
                              "One Etsy read, kept in memory for 5 minutes and dropped whenever this server writes (see cached / "
                              "cache_age_seconds). Capped at 500 listings; truncated says if there are more. Read-only.")
    async def shop_listings() -> dict[str, Any]:
        return await cache.get(fetch_listings)

    @mcp.resource(KEYWORDS_URI, name="cached_keywords", title="Cached keyword rows", mime_type=JSON,
                  description="Cached keyword rows containing the seed, best niche_score first, each with age_days and a stale flag (older than "
                              "7 days). status is fresh / partial / stale / missing. Local research cache only, no Etsy call. "
                              "URL-encode the seed: etsy://keywords/budget%20planner.")
    def cached_keywords(seed: str) -> dict[str, Any]:
        if not norm_keyword(seed):
            raise ResourceNotFoundError("Put a seed keyword after the slash, e.g. etsy://keywords/budget%20planner.")
        return {"seed": norm_keyword(seed), **research.get_keywords(seed, KEYWORD_MAX_AGE_DAYS, KEYWORD_LIMIT)}

    @mcp.resource(COMPETITOR_URI, name="latest_competitor_profile", title="Competitor profile", mime_type=JSON,
                  description="Profile of a watched competitor from its latest stored snapshot: listing count, price median/p25/p75, top tags, "
                              "newest listings, average listing age. {shop} is the shop name or id. Local cache only, no Etsy call; "
                              "add the shop with competitor_add and snapshot it with competitor_snapshot first.")
    def latest_competitor_profile(shop: str) -> dict[str, Any]:
        try:
            profile = competitors.profile(shop)
        except CompetitorError as e:
            raise ResourceNotFoundError(str(e)) from e
        profile["snapshot_age_days"] = age_days(profile["snapshot_taken_at"])
        return profile

    @mcp.resource(AUDIT_URI, name="latest_seo_audit", title="Latest SEO audit", mime_type=JSON,
                  description="The most recent stored seo_audit report for a listing: score, sub-scores, prioritized fixes, audited_at and "
                              "age_days. Local cache only, no Etsy call; run the seo_audit tool to create or refresh it.")
    def latest_seo_audit(listing_id: str) -> dict[str, Any]:
        lid = _listing_id(listing_id)
        report = seo.latest_audit(research, lid)
        if report is None:
            raise ResourceNotFoundError(f"No SEO audit stored for listing {lid}. Run the seo_audit tool with listing_id={lid} first.")
        return report

    @mcp.resource(WRITES_URI, name="recent_writes", title="Recent writes", mime_type=JSON,
                  description="The last 50 rows of the append-only write log, newest first: tool, listing_id, server mode, dry_run, before/after, "
                              "result. Includes blocked and failed attempts. Credentials are redacted before they are stored.")
    def recent_writes() -> dict[str, Any]:
        entries = research.read_write_log(RECENT_WRITES)
        return {"limit": RECENT_WRITES, "count": len(entries), "entries": entries}
