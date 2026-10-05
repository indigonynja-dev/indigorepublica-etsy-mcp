"""SQLite research cache + append-only write log (stdlib sqlite3 only).

Holds what Claude fetched from other sources (e.g. ProfitTree) so it can be reused and analysed, plus a log of
every write this server makes to Etsy. Nothing here talks to Etsy or the network.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

DEFAULT_DB = "~/.indigorepublica-etsy-mcp/research.db"
MAX_LOG_JSON_CHARS = 50_000

# Each migration takes the schema from version N to N+1 (index = N). Append new ones; never edit old ones.
_V1 = """
CREATE TABLE keywords (
    keyword TEXT NOT NULL, source TEXT NOT NULL, searches REAL, clicks REAL, competition REAL,
    digital_share REAL, trend, niche_score REAL, raw_json TEXT, fetched_at TEXT NOT NULL,
    PRIMARY KEY (keyword, source)
);
CREATE INDEX idx_keywords_fetched ON keywords(fetched_at);
CREATE TABLE market_listings (
    listing_id INTEGER NOT NULL, keyword TEXT NOT NULL, shop_id INTEGER, title TEXT, price REAL, tags_json TEXT,
    views INTEGER, favorites INTEGER, est_monthly_sales REAL, est_monthly_revenue REAL, source TEXT NOT NULL,
    fetched_at TEXT NOT NULL, PRIMARY KEY (listing_id, keyword, source)
);
CREATE INDEX idx_market_keyword ON market_listings(keyword, fetched_at);
CREATE TABLE competitor_snapshots (
    shop_id INTEGER NOT NULL, taken_at TEXT NOT NULL, listing_count INTEGER, data_json TEXT
);
CREATE INDEX idx_snapshots_shop ON competitor_snapshots(shop_id, taken_at);
CREATE TABLE audits (
    listing_id INTEGER NOT NULL, score REAL, report_json TEXT, created_at TEXT NOT NULL
);
CREATE INDEX idx_audits_listing ON audits(listing_id, created_at);
CREATE TABLE write_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, tool TEXT NOT NULL, listing_id INTEGER, mode TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0, before_json TEXT, after_json TEXT, result TEXT
);
CREATE INDEX idx_write_log_listing ON write_log(listing_id, id);
CREATE TRIGGER write_log_no_update BEFORE UPDATE ON write_log
BEGIN SELECT RAISE(ABORT, 'write_log is append-only'); END;
CREATE TRIGGER write_log_no_delete BEFORE DELETE ON write_log
BEGIN SELECT RAISE(ABORT, 'write_log is append-only'); END;
"""
# v2: competitor tracking (watchlist) + ProfitTree's shop_name on market rows.
_V2 = """
ALTER TABLE market_listings ADD COLUMN shop_name TEXT;
CREATE TABLE competitors (
    shop_id INTEGER PRIMARY KEY, shop_name TEXT NOT NULL, added_at TEXT NOT NULL
);
"""
# v3: stored SEO update previews (seo_preview_update -> seo_apply_update)
_V3 = """
CREATE TABLE seo_previews (
    preview_id TEXT PRIMARY KEY, listing_id INTEGER NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    before_json TEXT NOT NULL, proposal_json TEXT NOT NULL, applied_at TEXT
);
CREATE INDEX idx_seo_previews_listing ON seo_previews(listing_id, created_at);
"""
MIGRATIONS: list[str | Callable[[sqlite3.Connection], None]] = [_V1, _V2, _V3]
SCHEMA_VERSION = len(MIGRATIONS)

# table -> timestamp column used for oldest/newest in cache stats
TABLES = {
    "keywords": "fetched_at", "market_listings": "fetched_at", "competitor_snapshots": "taken_at",
    "audits": "created_at", "write_log": "ts",
}

# Cache tables that age out under ETSY_CACHE_RETENTION_DAYS (Etsy-sourced data, plus ProfitTree keyword rows).
PRUNABLE = {"competitor_snapshots": "taken_at", "market_listings": "fetched_at", "keywords": "fetched_at"}

# First name in each tuple is ProfitTree's (keyword_finder / product_finder); the rest are fallbacks.
KEYWORD_ALIASES = {
    "searches": ("avg_monthly_searches", "search_volume", "monthly_searches", "volume"),
    "clicks": ("avg_monthly_clicks", "est_clicks"),
    "trend": ("trend_direction",),
    "competition": ("competing_listings", "listings", "competitors"),
    "digital_share": ("digital_percent", "digital_pct"),
    "niche_score": ("score", "opportunity"),
}
LISTING_ALIASES = {
    "listing_id": ("id",),
    "favorites": ("num_favorers", "favorers", "favourites"),
    "est_monthly_sales": ("monthly_sales", "est_monthly_sales_count", "est_sales"),
    "est_monthly_revenue": ("monthly_revenue", "est_revenue"),
    "views": ("total_views",),
    "shop_name": ("shop",),
}


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def default_db_path() -> Path:
    return Path(os.environ.get("ETSY_RESEARCH_DB") or DEFAULT_DB).expanduser()


def norm_keyword(k: Any) -> str:
    return " ".join(str(k or "").lower().split())


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip().replace(",", "").rstrip("%"))
        except ValueError:
            return None
    return None


def _int(v: Any) -> int | None:
    n = _num(v)
    return int(n) if n is not None else None


def _pick(row: dict[str, Any], key: str, aliases: dict[str, tuple[str, ...]]) -> Any:
    for k in (key, *aliases.get(key, ())):
        if row.get(k) is not None:
            return row[k]
    return None


def _dumps(obj: Any) -> str | None:
    if obj is None:
        return None
    s = json.dumps(obj, default=str, ensure_ascii=False)
    if len(s) > MAX_LOG_JSON_CHARS:
        s = json.dumps({"truncated": True, "chars": len(s), "head": s[:MAX_LOG_JSON_CHARS]})
    return s


SENSITIVE_PARTS = ("auth", "token", "key", "secret")
REDACTED = "[REDACTED]"
BEARER_RE = re.compile(r"Bearer\s+[\w.\-~+/=]+", re.IGNORECASE)


def _sensitive_name(name: Any) -> bool:
    n = str(name).lower()
    # 'keyword(s)' is search data, not a credential; every other name containing "key" is redacted.
    return any(p in n for p in SENSITIVE_PARTS) and not n.startswith("keyword")


def redact(obj: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Copy of obj that is safe to persist: credential-like field names are masked, raw bytes are replaced by a size
    note, and any known secret value is scrubbed from every string."""
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return f"[{len(obj)} bytes omitted]"
    if isinstance(obj, dict):
        return {k: (REDACTED if _sensitive_name(k) else redact(v, secrets)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [redact(v, secrets) for v in obj]
    if isinstance(obj, str):
        for sec in secrets:
            if sec:
                obj = obj.replace(sec, REDACTED)
        return BEARER_RE.sub(f"Bearer {REDACTED}", obj)
    return obj


BUYER_REMOVED = "[BUYER_DATA_REMOVED]"
# Keys that only ever hold buyer personal data (Etsy receipts, transactions, shipping addresses), whatever they sit in.
_BUYER_KEYS = frozenset({
    "buyer_name", "buyer_email", "buyer_user_id", "buyer_id", "first_name", "last_name", "email", "phone", "phone_number",
    "first_line", "second_line", "formatted_address", "address", "address1", "address2", "address_line1", "address_line2",
    "street", "street_address", "city", "zip", "zipcode", "zip_code", "postal_code", "gift_message", "message_from_buyer",
    "personalization", "personalisation", "personalization_text", "customization",
    "ship_to_name", "shipping_address", "billing_address",
})
# Generic names that are buyer data only inside a dict that is clearly a buyer/address record (a listing's "state" is not).
_CONTEXT_KEYS = frozenset({"name", "state", "country", "country_iso", "country_id", "country_name", "region", "province"})
_ANCHOR_KEYS = frozenset({
    "first_line", "formatted_address", "zip", "city", "buyer_email", "buyer_user_id", "gift_message", "message_from_buyer",
    "buyer_name", "ship_to_name", "postal_code",
})
EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")


def scrub_buyer_data(obj: Any) -> Any:
    """Copy of obj with buyer personal data replaced by "[BUYER_DATA_REMOVED]": names, address lines, city/state/zip/country,
    emails, phone numbers, buyer user ids, gift messages and personalization text. Applied before anything reaches write_log."""
    if isinstance(obj, dict):
        anchored = any(str(k).lower() in _ANCHOR_KEYS for k in obj)
        label = str(obj.get("formatted_name") or obj.get("property_name") or "").lower()
        personalized = "personali" in label or "customi" in label
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in _BUYER_KEYS or (anchored and lk in _CONTEXT_KEYS):
                out[k] = BUYER_REMOVED if v not in (None, "", [], {}) else v
            elif personalized and lk in ("formatted_value", "value", "values"):
                out[k] = BUYER_REMOVED
            else:
                out[k] = scrub_buyer_data(v)
        return out
    if isinstance(obj, (list, tuple, set)):
        return [scrub_buyer_data(v) for v in obj]
    if isinstance(obj, str):
        t = obj.lstrip()
        if t[:1] in "{[":  # a JSON body that travelled as a string
            try:
                return json.dumps(scrub_buyer_data(json.loads(t)), ensure_ascii=False)
            except ValueError:
                pass
        return EMAIL_RE.sub(BUYER_REMOVED, obj)
    return obj


def _loads(s: str | None) -> Any:
    if s is None:
        return None
    try:
        return json.loads(s)
    except ValueError:
        return s


def age_days(ts: str, now: datetime | None = None) -> float:
    then = datetime.fromisoformat(ts)
    return round(((now or datetime.now(tz=timezone.utc)) - then).total_seconds() / 86400, 2)


class ResearchDB:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self.migrate()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists()
        with closing(self._connect()) as con:
            if fresh:
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"{self.path} has schema v{version}, newer than this server (v{SCHEMA_VERSION}). Upgrade the server.")
            for n in range(version, SCHEMA_VERSION):
                step = MIGRATIONS[n]
                con.execute("BEGIN")
                try:
                    if callable(step):
                        step(con)
                    else:
                        for stmt in _split(step):
                            con.execute(stmt)
                    con.execute(f"PRAGMA user_version = {n + 1}")
                    con.execute("COMMIT")
                except Exception:
                    con.execute("ROLLBACK")
                    raise

    def schema_version(self) -> int:
        with closing(self._connect()) as con:
            return con.execute("PRAGMA user_version").fetchone()[0]

    # ------------------------------------------------------------------ keywords
    def save_keywords(self, rows: list[dict[str, Any]], source: str) -> dict[str, Any]:
        ts, saved, skipped = now_iso(), 0, []
        with closing(self._connect()) as con, con:
            for i, row in enumerate(rows):
                kw = norm_keyword(row.get("keyword") if isinstance(row, dict) else None)
                if not kw:
                    skipped.append({"index": i, "reason": "missing 'keyword'"})
                    continue
                g = lambda k: _pick(row, k, KEYWORD_ALIASES)  # noqa: E731
                trend = g("trend")
                con.execute(
                    "INSERT OR REPLACE INTO keywords VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (kw, source, _num(g("searches")), _num(g("clicks")), _num(g("competition")), _num(g("digital_share")),
                     trend if isinstance(trend, (int, float, str)) or trend is None else _dumps(trend),
                     _num(g("niche_score")), _dumps(row), ts),
                )
                saved += 1
        return {"saved": saved, "skipped": skipped, "source": source, "fetched_at": ts}

    def get_keywords(self, seed: str | None, max_age_days: float, limit: int = 200) -> dict[str, Any]:
        sql, args = "SELECT * FROM keywords", []
        if seed:
            sql += " WHERE keyword LIKE ? ESCAPE '\\'"
            args.append("%" + norm_keyword(seed).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        sql += " ORDER BY niche_score IS NULL, niche_score DESC, searches DESC LIMIT ?"
        with closing(self._connect()) as con:
            rows = con.execute(sql, (*args, max(1, min(limit, 1000)))).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["raw"] = _loads(d.pop("raw_json"))
            out.append(d)
        return _freshness(out, max_age_days, "keyword rows" + (f" matching {seed!r}" if seed else ""),
                          "Ask ProfitTree (keyword_finder) and save with research_save_keywords.")

    # ------------------------------------------------------------------ market listings
    def save_market_listings(self, rows: list[dict[str, Any]], keyword: str, source: str) -> dict[str, Any]:
        ts, kw, saved, skipped = now_iso(), norm_keyword(keyword), 0, []
        if not kw:
            raise ValueError("keyword is required")
        with closing(self._connect()) as con, con:
            for i, row in enumerate(rows):
                g = lambda k: _pick(row, k, LISTING_ALIASES)  # noqa: E731
                lid = _int(g("listing_id")) if isinstance(row, dict) else None
                if lid is None:
                    skipped.append({"index": i, "reason": "missing numeric 'listing_id'"})
                    continue
                tags = row.get("tags")
                if tags is None and row.get("tags_json"):
                    tags = _loads(row["tags_json"]) if isinstance(row["tags_json"], str) else row["tags_json"]
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(",") if t.strip()]
                shop_name = str(g("shop_name") or "").strip() or None
                con.execute(
                    "INSERT OR REPLACE INTO market_listings (listing_id, keyword, shop_id, title, price, tags_json, views, favorites, "
                    "est_monthly_sales, est_monthly_revenue, source, fetched_at, shop_name) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (lid, kw, _int(row.get("shop_id")), row.get("title"), _num(row.get("price")), _dumps(tags),
                     _int(g("views")), _int(g("favorites")), _num(g("est_monthly_sales")), _num(g("est_monthly_revenue")),
                     source, ts, shop_name),
                )
                saved += 1
        return {"saved": saved, "skipped": skipped, "keyword": kw, "source": source, "fetched_at": ts}

    def get_market(self, keyword: str, max_age_days: float, limit: int = 200) -> dict[str, Any]:
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT * FROM market_listings WHERE keyword=? ORDER BY est_monthly_revenue IS NULL, est_monthly_revenue DESC LIMIT ?",
                (norm_keyword(keyword), max(1, min(limit, 1000)))).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["tags"] = _loads(d.pop("tags_json")) or []
            out.append(d)
        res = _freshness(out, max_age_days, f"market listings for {keyword!r}",
                         "Ask ProfitTree (product_finder) and save with research_save_market_listings.")
        prices = [r["price"] for r in out if r["price"] is not None]
        res["summary"] = {
            "listings": len(out),
            "avg_price": round(sum(prices) / len(prices), 2) if prices else None,
            "total_est_monthly_revenue": round(sum(r["est_monthly_revenue"] or 0 for r in out), 2),
        }
        return res

    # ------------------------------------------------------------------ retention
    def prune(self, retention_days: int = 90, dry_run: bool = False) -> dict[str, Any]:
        """Delete Etsy-sourced cache rows older than retention_days (competitor_snapshots, market_listings) plus ProfitTree
        keyword rows. The write log, audits and SEO previews are never touched. dry_run only counts."""
        if retention_days < 1:
            raise ValueError("retention_days must be 1 or more")
        cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=retention_days)).isoformat(timespec="seconds")
        out: dict[str, Any] = {}
        with closing(self._connect()) as con, con:
            for table, col in PRUNABLE.items():
                n, oldest = con.execute(f"SELECT COUNT(*), MIN({col}) FROM {table} WHERE {col} < ?", (cutoff,)).fetchone()
                if n and not dry_run:
                    con.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))
                out[table] = {"rows": n, "oldest": oldest}
        total = sum(v["rows"] for v in out.values())
        return {"dry_run": dry_run, "retention_days": retention_days, "cutoff": cutoff, "tables": out,
                "total_rows": total, "deleted": 0 if dry_run else total}

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        with closing(self._connect()) as con:
            for table, col in TABLES.items():
                n, old, new = con.execute(f"SELECT COUNT(*), MIN({col}), MAX({col}) FROM {table}").fetchone()
                out[table] = {"rows": n, "oldest": old, "newest": new,
                              "newest_age_days": age_days(new) if new else None}
        return {"db_path": str(self.path), "schema_version": self.schema_version(), "tables": out}

    # ------------------------------------------------------------------ write log
    def log_write(self, tool: str, listing_id: int | None, mode: str, dry_run: bool, before: Any, after: Any, result: str,
                  secrets: tuple[str, ...] = ()) -> int:
        before, after, result = (scrub_buyer_data(redact(x, secrets)) for x in (before, after, result or ""))
        with closing(self._connect()) as con, con:
            cur = con.execute(
                "INSERT INTO write_log (ts, tool, listing_id, mode, dry_run, before_json, after_json, result) VALUES (?,?,?,?,?,?,?,?)",
                (now_iso(), tool, listing_id, mode, int(dry_run), _dumps(before), _dumps(after), result[:2000]))
            return int(cur.lastrowid or 0)

    def read_write_log(self, limit: int = 50, listing_id: int | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM write_log", []
        if listing_id is not None:
            sql += " WHERE listing_id=?"
            args.append(listing_id)
        sql += " ORDER BY id DESC LIMIT ?"
        with closing(self._connect()) as con:
            rows = con.execute(sql, (*args, max(1, min(limit, 500)))).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["dry_run"] = bool(d["dry_run"])
            d["before"] = _loads(d.pop("before_json"))
            d["after"] = _loads(d.pop("after_json"))
            out.append(d)
        return out


def _split(script: str) -> list[str]:
    """Split a migration script into statements (handles CREATE TRIGGER ... END;)."""
    stmts, buf = [], ""
    for line in script.splitlines():
        buf += line + "\n"
        if sqlite3.complete_statement(buf):
            stmts.append(buf.strip())
            buf = ""
    if buf.strip():
        stmts.append(buf.strip())
    return stmts


def _freshness(rows: list[dict[str, Any]], max_age_days: float, what: str, hint: str) -> dict[str, Any]:
    now = datetime.now(tz=timezone.utc)
    for r in rows:
        r["age_days"] = age_days(r["fetched_at"], now)
        r["stale"] = r["age_days"] > max_age_days
    stale = sum(1 for r in rows if r["stale"])
    if not rows:
        status, msg = "missing", f"No cached {what}. {hint}"
    elif stale == len(rows):
        status, msg = "stale", f"All {len(rows)} cached {what} are older than {max_age_days} days (oldest {max(r['age_days'] for r in rows)}d). {hint}"
    elif stale:
        status, msg = "partial", f"{stale} of {len(rows)} cached {what} are older than {max_age_days} days; the rest are fresh. Refresh the stale ones if they matter."
    else:
        status, msg = "fresh", f"{len(rows)} cached {what}, all within {max_age_days} days."
    return {"status": status, "message": msg, "max_age_days": max_age_days, "count": len(rows), "rows": rows}
