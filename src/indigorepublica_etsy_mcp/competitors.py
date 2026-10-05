"""Competitor tracking: a watchlist plus listing snapshots, diffs and profiles for other Etsy shops.

Data source is the Etsy Open API v3 PUBLIC endpoints only (API key, no OAuth, no scraping):
  GET /v3/application/shops?shop_name=...                 findShops
  GET /v3/application/shops/{shop_id}                     getShop
  GET /v3/application/shops/{shop_id}/listings/active     findAllActiveListingsByShop (includes=Images for image counts)
Snapshots live in the research DB's `competitor_snapshots` table; the watchlist in `competitors`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .client import EtsyClient, EtsyError
from .config import Settings, load_settings
from .research import ResearchDB, _loads, age_days, now_iso

log = logging.getLogger("indigorepublica_etsy_mcp.competitors")

MAX_LISTINGS = 20_000  # safety cap per shop snapshot
TOP_TAGS = 30
NEWEST = 10
REFRESH_AFTER_HOURS = 6  # competitor_profile re-snapshots when the latest snapshot is older than this
REFRESH_RETRY_SECONDS = 300  # after a failed refresh, serve the old data for this long instead of hammering Etsy
PRICE_EPS = 0.005

LOCAL = ToolAnnotations(read_only_hint=True, open_world_hint=False)
# Reads Etsy (public data) and stores the result locally; never writes to Etsy.
FETCH = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
WATCHLIST = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True)


class CompetitorError(Exception):
    """User-facing problem (unknown shop, no snapshots...)."""


def _iso(ts: Any) -> str | None:
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
    return None


def _money(m: Any) -> tuple[float | None, str | None]:
    if isinstance(m, dict) and isinstance(m.get("amount"), (int, float)):
        return round(m["amount"] / (m.get("divisor") or 100), 2), m.get("currency_code")
    return None, None


def slim_listing(l: dict[str, Any]) -> dict[str, Any]:
    price, cur = _money(l.get("price"))
    images = l.get("images")
    return {
        "listing_id": l.get("listing_id"),
        "title": l.get("title"),
        "tags": [str(t) for t in (l.get("tags") or [])],
        "price": price,
        "currency": cur,
        "image_count": len(images) if isinstance(images, list) else None,
        "created": _iso(l.get("original_creation_timestamp") or l.get("created_timestamp") or l.get("creation_timestamp")),
        "updated": _iso(l.get("last_modified_timestamp") or l.get("updated_timestamp")),
        "views": l.get("views"),
        "num_favorers": l.get("num_favorers"),
        "url": l.get("url"),
    }


def percentile(sorted_vals: list[float], q: float) -> float | None:
    """Linear-interpolated percentile (q in 0..1) of an ascending list."""
    if not sorted_vals:
        return None
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo), 2)


def diff_snapshots(old: list[dict[str, Any]], new: list[dict[str, Any]], max_details: int = 50) -> dict[str, Any]:
    """Compare two lists of slim listings. Returns {summary, details}."""
    o = {l["listing_id"]: l for l in old}
    n = {l["listing_id"]: l for l in new}
    new_l = [n[i] for i in n if i not in o]
    removed = [o[i] for i in o if i not in n]
    titles, tags, prices, images = [], [], [], []
    for i in n.keys() & o.keys():
        a, b = o[i], n[i]
        ref = {"listing_id": i, "title": b.get("title")}
        if a.get("title") != b.get("title"):
            titles.append({**ref, "old": a.get("title"), "new": b.get("title")})
        added = sorted(set(b.get("tags") or []) - set(a.get("tags") or []))
        gone = sorted(set(a.get("tags") or []) - set(b.get("tags") or []))
        if added or gone:
            tags.append({**ref, "added": added, "removed": gone})
        pa, pb = a.get("price"), b.get("price")
        if pa is not None and pb is not None and abs(pb - pa) > PRICE_EPS:
            prices.append({**ref, "old": pa, "new": pb, "change": round(pb - pa, 2),
                           "change_pct": round((pb - pa) / pa * 100, 1) if pa else None})
        ia, ib = a.get("image_count"), b.get("image_count")
        if ia is not None and ib is not None and ia != ib:
            images.append({**ref, "old": ia, "new": ib})

    def brief(l: dict[str, Any]) -> dict[str, Any]:
        return {k: l.get(k) for k in ("listing_id", "title", "price", "tags", "image_count", "created", "url")}

    groups = {
        "new_listings": [brief(l) for l in new_l], "removed_listings": [brief(l) for l in removed],
        "title_changes": titles, "tag_changes": tags, "price_changes": prices, "image_count_changes": images,
    }
    summary = {k: len(v) for k, v in groups.items()}
    summary["listings_before"], summary["listings_after"] = len(o), len(n)
    summary["total_changes"] = sum(len(v) for v in groups.values())
    cap = max(0, max_details)
    details = {k: v[:cap] for k, v in groups.items()}
    truncated = {k: len(v) for k, v in groups.items() if len(v) > cap}
    out: dict[str, Any] = {"summary": summary, "details": details}
    if truncated:
        out["truncated"] = {"note": f"details capped at {cap} per category; raise max_details", "totals": truncated}
    return out


class Competitors:
    """Watchlist + snapshot store on top of the research DB, fetching through the existing EtsyClient."""

    def __init__(self, etsy: EtsyClient, db: ResearchDB):
        self.etsy, self.db = etsy, db
        self._refresh_failed_at: dict[int, float] = {}

    # ------------------------------------------------------------- Etsy (public endpoints, API key only)
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            return await self.etsy.request("GET", path, params=params, auth=False)  # existing client: x-api-key, throttle, 429 backoff
        except EtsyError as e:
            raise CompetitorError(str(e)) from e

    async def lookup_shop(self, shop_name_or_id: str | int) -> dict[str, Any]:
        ref = str(shop_name_or_id).strip()
        if not ref:
            raise CompetitorError("shop_name_or_id is required.")
        if ref.isdigit():
            s = await self._get(f"/shops/{ref}")
            if not isinstance(s, dict) or not s.get("shop_id"):
                raise CompetitorError(f"No shop with id {ref}.")
            return {"shop_id": int(s["shop_id"]), "shop_name": s.get("shop_name") or ref}
        data = await self._get("/shops", {"shop_name": ref, "limit": 25})
        results = data.get("results", []) if isinstance(data, dict) else []
        exact = [s for s in results if str(s.get("shop_name", "")).lower() == ref.lower()]
        if not exact:
            names = ", ".join(str(s.get("shop_name")) for s in results[:5]) or "none"
            raise CompetitorError(f"No shop named {ref!r} found (closest: {names}).")
        return {"shop_id": int(exact[0]["shop_id"]), "shop_name": exact[0]["shop_name"]}

    async def fetch_listings(self, shop_id: int) -> list[dict[str, Any]]:
        try:
            page = await self.etsy.paginate(f"/shops/{shop_id}/listings/active", {"includes": "Images"}, MAX_LISTINGS, auth=False)
        except EtsyError as e:
            raise CompetitorError(str(e)) from e
        return [slim_listing(l) for l in page["results"] if isinstance(l, dict)]

    # ------------------------------------------------------------- watchlist
    def resolve(self, shop: str | int) -> dict[str, Any]:
        """Watchlist entry by id or (case-insensitive) name; a bare numeric id with stored snapshots also works."""
        ref = str(shop).strip()
        with closing(self.db._connect()) as con:
            row = con.execute("SELECT * FROM competitors WHERE shop_id=? OR LOWER(shop_name)=LOWER(?)",
                              (int(ref) if ref.isdigit() else -1, ref)).fetchone()
            if row:
                return dict(row)
            if ref.isdigit() and con.execute("SELECT 1 FROM competitor_snapshots WHERE shop_id=?", (int(ref),)).fetchone():
                return {"shop_id": int(ref), "shop_name": None, "added_at": None}
        raise CompetitorError(f"{ref!r} is not on the competitor watchlist. Add it first with competitor_add.")

    async def add(self, shop_name_or_id: str | int) -> dict[str, Any]:
        s = await self.lookup_shop(shop_name_or_id)
        with closing(self.db._connect()) as con, con:
            existed = con.execute("SELECT 1 FROM competitors WHERE shop_id=?", (s["shop_id"],)).fetchone() is not None
            con.execute("INSERT INTO competitors (shop_id, shop_name, added_at) VALUES (?,?,?) "
                        "ON CONFLICT(shop_id) DO UPDATE SET shop_name=excluded.shop_name", (s["shop_id"], s["shop_name"], now_iso()))
        return {**s, "already_watched": existed}

    def remove(self, shop: str | int) -> dict[str, Any]:
        e = self.resolve(shop)
        with closing(self.db._connect()) as con, con:
            con.execute("DELETE FROM competitors WHERE shop_id=?", (e["shop_id"],))
        return {"removed": e["shop_id"], "shop_name": e["shop_name"], "note": "Stored snapshots were kept."}

    def list(self) -> dict[str, Any]:
        with closing(self.db._connect()) as con:
            rows = con.execute(
                "SELECT c.shop_id, c.shop_name, c.added_at, COUNT(s.taken_at) AS snapshots, MAX(s.taken_at) AS last_snapshot_at "
                "FROM competitors c LEFT JOIN competitor_snapshots s ON s.shop_id=c.shop_id GROUP BY c.shop_id ORDER BY LOWER(c.shop_name)"
            ).fetchall()
        shops = [dict(r) for r in rows]
        return {"count": len(shops), "shops": shops}

    # ------------------------------------------------------------- snapshots
    def _snapshots(self, shop_id: int, limit: int | None = None) -> list[dict[str, Any]]:
        """Stored snapshots, newest first. Each one carries every listing, so pass `limit` when only the latest are needed."""
        sql, args = "SELECT rowid AS rid, * FROM competitor_snapshots WHERE shop_id=? ORDER BY taken_at DESC, rowid DESC", [shop_id]
        if limit:
            sql, args = sql + " LIMIT ?", [*args, limit]
        with closing(self.db._connect()) as con:
            rows = con.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = _loads(r["data_json"]) or {}
            out.append({"taken_at": r["taken_at"], "listing_count": r["listing_count"], "listings": d.get("listings", []),
                        "shop_name": d.get("shop_name")})
        return out

    async def snapshot(self, shop: str | int) -> dict[str, Any]:
        e = self.resolve(shop)
        listings = await self.fetch_listings(e["shop_id"])  # raises before storing, so a failed fetch never leaves a partial snapshot
        ts = now_iso()
        data = {"shop_id": e["shop_id"], "shop_name": e["shop_name"], "listings": listings}
        with closing(self.db._connect()) as con, con:
            con.execute("INSERT INTO competitor_snapshots (shop_id, taken_at, listing_count, data_json) VALUES (?,?,?,?)",
                        (e["shop_id"], ts, len(listings), json.dumps(data, ensure_ascii=False)))
        return {"shop_id": e["shop_id"], "shop_name": e["shop_name"], "taken_at": ts, "listing_count": len(listings),
                "empty": not listings}

    async def snapshot_all(self, delay: float = 1.0) -> dict[str, Any]:
        shops = self.list()["shops"]
        done, failed = [], []
        for i, s in enumerate(shops):
            if i and delay > 0:
                await asyncio.sleep(delay)
            try:
                done.append(await self.snapshot(s["shop_id"]))
            except CompetitorError as e:
                log.warning("snapshot of %s failed: %s", s["shop_name"], e)
                failed.append({"shop_id": s["shop_id"], "shop_name": s["shop_name"], "error": str(e)})
        return {"watched": len(shops), "ok": len(done), "failed": len(failed), "snapshots": done, "errors": failed}

    def diff(self, shop: str | int, since: str | None = None, max_details: int = 50) -> dict[str, Any]:
        e = self.resolve(shop)
        snaps = self._snapshots(e["shop_id"])
        if not snaps:
            raise CompetitorError(f"No snapshots for {e['shop_name'] or e['shop_id']} yet. Run competitor_snapshot first.")
        latest = snaps[0]
        if since:
            cutoff = _parse_since(since)
            older = [s for s in snaps[1:] if datetime.fromisoformat(s["taken_at"]) <= cutoff]
            if not older:
                oldest = snaps[-1]["taken_at"] if len(snaps) > 1 else None
                raise CompetitorError(f"No snapshot taken on or before {since}." + (f" Oldest earlier snapshot: {oldest}." if oldest else " Only one snapshot exists."))
            base = older[0]
        else:
            if len(snaps) < 2:
                raise CompetitorError("Only one snapshot exists; take another later to compare.")
            base = snaps[1]
        res = diff_snapshots(base["listings"], latest["listings"], max_details)
        older, newer = _snapshot_date(base["taken_at"]), _snapshot_date(latest["taken_at"])
        return {"shop_id": e["shop_id"], "shop_name": e["shop_name"], "from": base["taken_at"], "to": latest["taken_at"],
                "snapshot_dates": {"older": older, "newer": newer},
                "comparison": f"Older snapshot {older['taken_at']} ({older['age']}) vs newer snapshot {newer['taken_at']} ({newer['age']}).",
                **res,
                "note": "'removed' means no longer active (sold out, deactivated, expired or deleted); Etsy's public API doesn't say which."}

    def profile(self, shop: str | int) -> dict[str, Any]:
        e = self.resolve(shop)
        snaps = self._snapshots(e["shop_id"], limit=1)  # only the latest is profiled; don't parse months of older ones
        if not snaps:
            raise CompetitorError(f"No snapshots for {e['shop_name'] or e['shop_id']} yet. Run competitor_snapshot first.")
        snap = snaps[0]
        ls = snap["listings"]
        prices = sorted(l["price"] for l in ls if l.get("price") is not None)
        tag_counts: dict[str, int] = {}
        for l in ls:
            for t in {t.lower() for t in l.get("tags") or []}:
                tag_counts[t] = tag_counts.get(t, 0) + 1
        top = sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_TAGS]
        taken = datetime.fromisoformat(snap["taken_at"])
        ages = [(taken - datetime.fromisoformat(l["created"])).total_seconds() / 86400 for l in ls if l.get("created")]
        newest = sorted((l for l in ls if l.get("created")), key=lambda l: l["created"], reverse=True)[:NEWEST]
        return {
            **_freshness_fields(snap["taken_at"]),
            "shop_id": e["shop_id"], "shop_name": e["shop_name"], "snapshot_taken_at": snap["taken_at"],
            "listing_count": len(ls),
            "price": {"median": percentile(prices, 0.5), "p25": percentile(prices, 0.25), "p75": percentile(prices, 0.75),
                      "min": prices[0] if prices else None, "max": prices[-1] if prices else None},
            "top_tags": [{"tag": t, "count": c} for t, c in top],
            "newest_listings": [{k: l.get(k) for k in ("listing_id", "title", "price", "created", "url")} for l in newest],
            "avg_listing_age_days": round(sum(ages) / len(ages), 1) if ages else None,
        }

    async def profile_fresh(self, shop: str | int) -> dict[str, Any]:
        """Profile of the latest snapshot, re-snapshotting first when it is older than REFRESH_AFTER_HOURS. A refresh that is
        skipped (recent failure) or fails returns the old data, clearly labelled with its age."""
        e = self.resolve(shop)
        snaps = self._snapshots(e["shop_id"], limit=1)
        if not snaps:
            raise CompetitorError(f"No snapshots for {e['shop_name'] or e['shop_id']} yet. Run competitor_snapshot first.")
        refresh: dict[str, Any] = {"attempted": False, "refreshed": False}
        if _freshness_fields(snaps[0]["taken_at"])["stale"]:
            if time.monotonic() - self._refresh_failed_at.get(e["shop_id"], -1e9) < REFRESH_RETRY_SECONDS:
                refresh["note"] = "Not refreshed: a refresh failed a moment ago; not retrying yet."
            else:
                refresh["attempted"] = True
                try:
                    await self.snapshot(e["shop_id"])  # public API through the throttled client (429 backoff built in)
                    refresh["refreshed"] = True
                except CompetitorError as err:
                    self._refresh_failed_at[e["shop_id"]] = time.monotonic()
                    refresh["error"] = str(err)
                    refresh["note"] = "Refresh failed; showing the OLD snapshot (see snapshot_age)."
        return {**self.profile(e["shop_id"]), "refresh": refresh}


def _age_label(hours: float) -> str:
    return f"{round(hours)}h old" if hours < 48 else f"{round(hours / 24)}d old"


def _snapshot_date(taken_at: str) -> dict[str, Any]:
    hours = round(age_days(taken_at) * 24, 1)
    return {"taken_at": taken_at, "date": taken_at[:10], "age_hours": hours, "age": _age_label(hours)}


def _freshness_fields(taken_at: str) -> dict[str, Any]:
    hours = round(age_days(taken_at) * 24, 1)
    stale = hours > REFRESH_AFTER_HOURS
    return {"snapshot_age": f"Snapshot taken {taken_at} ({_age_label(hours)})" + (" - STALE, older than 6 hours" if stale else ""),
            "snapshot_age_hours": hours, "snapshot_age_days": round(hours / 24, 2), "stale": stale}


def _parse_since(value: str) -> datetime:
    v = value.strip()
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as e:
        raise CompetitorError(f"Bad since {value!r}; use YYYY-MM-DD or full ISO-8601.") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
        if len(v) == 10:  # a bare date means end of that day
            dt = dt.replace(hour=23, minute=59, second=59)
    return dt


# ----------------------------------------------------------------------------- MCP registration
def register_competitor_tools(mcp: MCPServer, etsy: EtsyClient, research: ResearchDB) -> Competitors:
    comp = Competitors(etsy, research)

    def wrap(fn):
        async def run(*a, **kw):
            try:
                r = fn(*a, **kw)
                return await r if asyncio.iscoroutine(r) else r
            except CompetitorError as e:
                raise ToolError(str(e)) from e
        return run

    @mcp.tool(annotations=WATCHLIST)
    async def competitor_add(shop_name_or_id: str) -> dict[str, Any]:
        """Add another Etsy shop to the competitor watchlist (looked up by name via findShops, or by numeric shop id). Local watchlist only; public Etsy data, no OAuth."""
        return await wrap(comp.add)(shop_name_or_id)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    async def competitor_remove(shop: str) -> dict[str, Any]:
        """Remove a shop (name or id) from the competitor watchlist. Its stored snapshots are kept."""
        return await wrap(comp.remove)(shop)

    @mcp.tool(annotations=LOCAL)
    async def competitor_list() -> dict[str, Any]:
        """The competitor watchlist with snapshot count and time of the latest snapshot per shop."""
        return await wrap(comp.list)()

    @mcp.tool(annotations=FETCH)
    async def competitor_snapshot(shop: str) -> dict[str, Any]:
        """Fetch ALL active listings of a watched shop (title, tags, price, image count, created/updated) from Etsy's public API and store a snapshot locally. Takes a while for big shops."""
        return await wrap(comp.snapshot)(shop)

    @mcp.tool(annotations=FETCH)
    async def competitor_snapshot_all(delay_seconds: float = 1.0) -> dict[str, Any]:
        """Snapshot every watched shop one after another (pausing delay_seconds between shops; 429s are retried with backoff). One shop failing doesn't stop the rest; failures are listed in 'errors'."""
        return await wrap(comp.snapshot_all)(delay_seconds)

    @mcp.tool(annotations=LOCAL)
    async def competitor_diff(shop: str, since: str | None = None, max_details: int = 50) -> dict[str, Any]:
        """What changed in a competitor's shop: compares the latest two snapshots (or the latest vs the newest one taken on/before `since`, YYYY-MM-DD or ISO). Summary counts first, then details: new/removed listings, title changes, tags added/removed, price changes (amount and %), image count changes."""
        return await wrap(comp.diff)(shop, since, max_details)

    @mcp.tool(annotations=FETCH)
    async def competitor_profile(shop: str) -> dict[str, Any]:
        """Profile from a shop's latest snapshot: listing count, price median/p25/p75, top 30 tags with counts, 10 newest listings, average listing age in days. If that snapshot is older than 6 hours it is refreshed first from Etsy's public API (like competitor_snapshot, allowed in every mode: it only reads public Etsy data and writes the local cache); if the refresh fails the old data is returned, labelled with its age (snapshot_age, refresh)."""
        return await wrap(comp.profile_fresh)(shop)

    return comp


# ----------------------------------------------------------------------------- CLI for schedulers
async def _run_all(settings: Settings, delay: float) -> dict[str, Any]:
    etsy = EtsyClient(settings)
    db = ResearchDB(settings.research_db)
    try:
        result = await Competitors(etsy, db).snapshot_all(delay)
    finally:
        await etsy.aclose()
    try:  # retention: age out old Etsy-sourced cache rows at the end of every run
        result["pruned"] = db.prune(settings.cache_retention_days)
    except Exception as e:  # noqa: BLE001 - pruning must not fail the snapshot run
        log.warning("cache pruning failed: %s", e)
        result["pruned"] = {"error": str(e)}
    return result


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Snapshot every watched competitor shop, then exit (for cron/systemd timers).")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds to pause between shops (default 1)")
    args = ap.parse_args(argv)
    logging.basicConfig(level="INFO", stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings()
        settings.require_credentials()
    except RuntimeError as e:
        sys.exit(f"error: {e}")
    result = asyncio.run(_run_all(settings, args.delay))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(1 if result["failed"] else 0)


if __name__ == "__main__":
    main()
