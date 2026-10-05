"""SEO audit, tag-gap analysis and safe (preview -> apply) SEO updates.

Builds on the existing linter (`server.seo_check`) rather than replacing it: the linter's hard errors feed the
audit, and the same linter validates every proposed update. Pure scoring functions live at the top so they can be
tested on fixture listings; `register()` wires the four MCP tools into the server.

Data sources: Etsy Open API v3 (the user's own listings) and the local research cache only.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .research import ResearchDB, age_days, norm_keyword, now_iso

# Sub-score weights (must sum to 100). "digital" is dropped and the rest renormalised for physical listings.
WEIGHTS: dict[str, int] = {"title": 25, "tags": 25, "description": 15, "attributes": 10, "images": 15, "digital": 10}

TITLE_MAX, TAGS_MAX, TAG_CHARS_MAX, IMAGES_MAX = 140, 13, 20, 10
KEYWORD_PREVIEW_CHARS = 160
PREVIEW_TTL_HOURS = 24
PREVIEW_FIELDS = ("title", "tags", "description")

AUDIT_ANNOTATIONS = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

_BULLET_RE = re.compile(r"^\s*(?:[-*•✓✔►▪●]|\d+[.)])\s+\S")
_WORD_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)?", re.UNICODE)


# --------------------------------------------------------------------------------------------- text helpers
def _stem(w: str) -> str:
    w = w.lower()
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def _tokens(text: str) -> list[str]:
    return [_stem(w) for w in _WORD_RE.findall(text or "")]


def _tag_key(tag: str) -> tuple[str, ...]:
    return tuple(sorted(_tokens(tag)))


def _find_phrase(tokens: list[str], phrase: list[str]) -> int | None:
    """Index of the first token position where `phrase` appears contiguously, else None."""
    n = len(phrase)
    if not n:
        return None
    for i in range(len(tokens) - n + 1):
        if tokens[i:i + n] == phrase:
            return i
    return None


def infer_keyword(title: str, tags: list[str]) -> str | None:
    """Best guess at the listing's main keyword: the longest tag that appears in the title, else the first tag."""
    toks = _tokens(title)
    hits = [t for t in tags if _find_phrase(toks, _tokens(t)) is not None]
    if hits:
        return max(hits, key=lambda t: (len(_tokens(t)), -tags.index(t)))
    return tags[0] if tags else None


# --------------------------------------------------------------------------------------------- sub-scores
# Each returns (score 0-100, issues) where an issue is {"points": points lost within the sub-score, "message": str}.
Issue = dict[str, Any]


def _issue(points: float, message: str) -> Issue:
    return {"points": points, "message": message}


def _clamp(score: float) -> float:
    return round(max(0.0, min(100.0, score)), 1)


def score_title(title: str, keyword: str | None, lint_errors: list[str]) -> tuple[float, list[Issue]]:
    issues: list[Issue] = []
    title = (title or "").strip()
    if not title:
        return 0.0, [_issue(100, "Title is empty.")]
    n = len(title)
    if n > TITLE_MAX:
        issues.append(_issue(40, f"Title is {n} chars; Etsy max is {TITLE_MAX}."))
    elif n < 40:
        issues.append(_issue(25, f"Title is only {n} chars; use more of the {TITLE_MAX} available with buyer search phrases."))
    elif n < 70:
        issues.append(_issue(10, f"Title is {n} chars; there is room for more search phrases (max {TITLE_MAX})."))

    toks = _tokens(title)
    if keyword:
        pos = _find_phrase(toks, _tokens(keyword))
        if pos is None:
            issues.append(_issue(30, f"Main keyword '{keyword}' is not in the title."))
        else:
            start = len(" ".join(title.split()[:pos]))  # chars before the keyword (approx.; stems keep word count)
            if start > 40:
                issues.append(_issue(15, f"Main keyword '{keyword}' starts ~{start} chars in; move it to the first few words."))
            elif start > 20:
                issues.append(_issue(5, f"Main keyword '{keyword}' starts ~{start} chars in; earlier is better."))

    repeated = sorted({w for w in toks if len(w) > 3 and toks.count(w) > 1})
    if repeated:
        issues.append(_issue(min(24, 8 * len(repeated)), f"Repeated words in title: {', '.join(repeated)}. Use the slot for a new phrase."))

    letters = [c for c in title if c.isalpha()]
    caps = [w for w in re.findall(r"[^\W\d_]{4,}", title) if w.isupper()]
    if letters and all(c.isupper() for c in letters) and len(letters) > 3:
        issues.append(_issue(30, "Title is ALL CAPS; use Title Case."))
    elif caps:
        issues.append(_issue(min(30, 10 * len(caps)), f"ALL-CAPS words in title: {', '.join(caps)}."))

    for e in lint_errors:
        if e.startswith("Title") and not e.startswith("Title is"):
            issues.append(_issue(20, e))
    return _clamp(100 - sum(i["points"] for i in issues)), issues


def score_tags(tags: list[str], category_names: list[str], lint_errors: list[str]) -> tuple[float, list[Issue]]:
    issues: list[Issue] = []
    n = len(tags)
    if n == 0:
        return 0.0, [_issue(100, f"No tags; Etsy allows {TAGS_MAX}.")]
    if n > TAGS_MAX:
        issues.append(_issue(20, f"{n} tags; Etsy max is {TAGS_MAX}."))
    elif n < TAGS_MAX:
        issues.append(_issue(5 * (TAGS_MAX - n), f"Only {n}/{TAGS_MAX} tags used; fill the remaining {TAGS_MAX - n} slots."))

    for t in tags:
        if len(t) > TAG_CHARS_MAX:
            issues.append(_issue(8, f"Tag '{t}' is {len(t)} chars; max {TAG_CHARS_MAX}."))
    for e in lint_errors:
        if "characters Etsy rejects" in e:
            issues.append(_issue(8, e))

    keys = [_tag_key(t) for t in tags]
    flagged: set[int] = set()
    for i in range(n):
        for j in range(i + 1, n):
            if j in flagged or not keys[i] or not keys[j]:
                continue
            if tags[i].strip().lower() == tags[j].strip().lower():
                issues.append(_issue(8, f"Duplicate tag '{tags[j]}'."))
                flagged.add(j)
                continue
            a, b = set(keys[i]), set(keys[j])
            if keys[i] == keys[j] or len(a & b) / len(a | b) >= 0.75:
                issues.append(_issue(5, f"Near-duplicate tags '{tags[i]}' and '{tags[j]}'; replace one with a different phrase."))
                flagged.add(j)

    singles = [t for t in tags if len(_tokens(t)) < 2]
    if singles:
        issues.append(_issue(min(20, 3 * len(singles)), f"{len(singles)} single-word tag(s) ({', '.join(singles[:5])}); multi-word phrases match more searches."))

    cat_keys = {_tag_key(c) for c in category_names if c}
    wasted = [t for t, k in zip(tags, keys) if k and k in cat_keys]
    if wasted:
        issues.append(_issue(min(15, 5 * len(wasted)), f"Tag(s) {', '.join(wasted)} just repeat the category, which Etsy already indexes; use the slot for something new."))
    return _clamp(100 - sum(i["points"] for i in issues)), issues


def score_description(description: str, keyword: str | None) -> tuple[float, list[Issue]]:
    issues: list[Issue] = []
    desc = (description or "").strip()
    if not desc:
        return 0.0, [_issue(100, "Description is empty.")]
    if len(desc) < 160:
        issues.append(_issue(40, f"Description is {len(desc)} chars; explain what's included, format, and how delivery works."))
    elif len(desc) < 300:
        issues.append(_issue(20, f"Description is {len(desc)} chars; add what's included, sizes/formats and how to use it."))
    if keyword:
        head = _tokens(desc[:KEYWORD_PREVIEW_CHARS])
        if _find_phrase(head, _tokens(keyword)) is None:
            issues.append(_issue(35, f"Main keyword '{keyword}' is not in the first {KEYWORD_PREVIEW_CHARS} characters (the search-result snippet)."))
    lines = [ln for ln in desc.splitlines() if ln.strip()]
    structured = sum(1 for ln in lines if _BULLET_RE.match(ln) or (len(ln.strip()) <= 60 and (ln.strip().endswith(":") or (ln.strip().isupper() and len(ln.strip()) > 3))))
    if len(desc) >= 300 and (len(lines) < 4 or structured < 2):
        issues.append(_issue(25, "Description isn't scannable; break it into short sections with headers or bullet lists (what's included, how it works, sizes/formats)."))
    if any(len(ln) > 600 for ln in lines):
        issues.append(_issue(15, "Description has a wall-of-text paragraph over 600 chars; split it."))
    return _clamp(100 - sum(i["points"] for i in issues)), issues


def score_attributes(materials: list[str], properties: list[Any] | None, styles: list[str]) -> tuple[float, list[Issue]]:
    """properties is None when attribute data could not be fetched (that check is then skipped, not failed)."""
    issues: list[Issue] = []
    checks = [(bool(materials), "No materials set; add them (e.g. 'PDF', 'Google Sheets')."),
              (bool(styles), "No styles set; add up to two to help category browsing.")]
    if properties is not None:
        checks.append((bool(properties), "No category attributes filled (colour, occasion, etc.); attributes feed Etsy's search filters."))
    each = 100 / len(checks)
    for ok, msg in checks:
        if not ok:
            issues.append(_issue(round(each, 1), msg))
    return _clamp(100 - sum(i["points"] for i in issues)), issues


def score_images(count: int) -> tuple[float, list[Issue]]:
    if count > IMAGES_MAX:
        return 90.0, [_issue(10, f"{count} images; Etsy allows at most {IMAGES_MAX}.")]
    if count < IMAGES_MAX:
        return _clamp(count * 100 / IMAGES_MAX), [_issue(round(100 - count * 100 / IMAGES_MAX, 1), f"{count}/{IMAGES_MAX} images; use all {IMAGES_MAX} slots (mockups, what's included, sizes).")]
    return 100.0, []


def score_digital(listing_type: str | None, file_count: int | None, digital_flag: bool | None = None) -> tuple[float, list[Issue]]:
    issues: list[Issue] = []
    if listing_type != "download" and digital_flag is not True:
        issues.append(_issue(50, f"Listing type is '{listing_type}', not 'download'."))
    if file_count is None:
        pass  # couldn't fetch files; don't fail the check
    elif file_count == 0:
        issues.append(_issue(50, "No digital file attached; buyers receive nothing."))
    return _clamp(100 - sum(i["points"] for i in issues)), issues


# --------------------------------------------------------------------------------------------- audit
def audit_listing(listing: dict[str, Any], *, image_count: int | None = None, file_count: int | None = None,
                  properties: list[Any] | None = None, category_names: list[str] | None = None,
                  keyword: str | None = None, lint: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """Score one listing 0-100 from its Etsy record plus extras. Pure: no I/O."""
    if lint is None:
        from .server import seo_check as lint  # lazy: server imports this module
    title, tags = listing.get("title") or "", [str(t) for t in listing.get("tags") or []]
    description, materials = listing.get("description") or "", listing.get("materials") or []
    styles = listing.get("styles") or []
    if image_count is None:
        image_count = len(listing.get("images") or [])
    is_download = listing.get("type") == "download" or listing.get("is_digital") is True
    kw = norm_keyword(keyword) or infer_keyword(title, tags)
    lint_res = lint(title, tags, description, materials)

    parts: dict[str, tuple[float, list[Issue]]] = {
        "title": score_title(title, kw, lint_res["errors"]),
        "tags": score_tags(tags, category_names or [], lint_res["errors"]),
        "description": score_description(description, kw),
        "attributes": score_attributes(materials, properties, styles),
        "images": score_images(image_count),
    }
    if is_download:
        parts["digital"] = score_digital(listing.get("type"), file_count, listing.get("is_digital"))
    total_w = sum(WEIGHTS[k] for k in parts)
    score = round(sum(parts[k][0] * WEIGHTS[k] for k in parts) / total_w, 1)

    fixes = []
    for area, (_, issues) in parts.items():
        for i in issues:
            fixes.append({"area": area, "impact": round(i["points"] * WEIGHTS[area] / total_w, 1), "fix": i["message"]})
    fixes.sort(key=lambda f: -f["impact"])
    for rank, f in enumerate(fixes, 1):
        f["priority"] = rank
    return {
        "listing_id": listing.get("listing_id"),
        "score": score,
        "main_keyword": kw,
        "sub_scores": {k: {"score": s, "weight": WEIGHTS[k], "issues": len(iss)} for k, (s, iss) in parts.items()},
        "fixes": fixes,
        "notes": [n for n in (
            None if properties is not None else "Category attributes could not be checked (properties endpoint unavailable); that check was skipped.",
            "Digital-file count could not be fetched; file check skipped." if is_download and file_count is None else None,
            None if is_download else "Not a digital download: the digital-download sub-score is excluded and weights renormalised.",
        ) if n],
    }


# --------------------------------------------------------------------------------------------- local DB helpers
def _con(research: ResearchDB) -> sqlite3.Connection:
    con = sqlite3.connect(research.path, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def store_audit(research: ResearchDB, report: dict[str, Any]) -> None:
    with closing(_con(research)) as con, con:
        con.execute("INSERT INTO audits (listing_id, score, report_json, created_at) VALUES (?,?,?,?)",
                    (report["listing_id"], report["score"], json.dumps(report, ensure_ascii=False), now_iso()))


def _previews(research: ResearchDB) -> sqlite3.Connection:
    return _con(research)  # seo_previews is created by research.py migration v2


def store_preview(research: ResearchDB, listing_id: int, before: dict[str, Any], proposal: dict[str, Any]) -> tuple[str, str]:
    pid, now = secrets.token_hex(6), datetime.now(tz=timezone.utc)
    exp = (now + timedelta(hours=PREVIEW_TTL_HOURS)).isoformat(timespec="seconds")
    with closing(_previews(research)) as con, con:
        con.execute("INSERT INTO seo_previews VALUES (?,?,?,?,?,?,NULL)",
                    (pid, listing_id, now.isoformat(timespec="seconds"), exp, json.dumps(before), json.dumps(proposal)))
    return pid, exp


def load_preview(research: ResearchDB, listing_id: int, preview_id: str) -> dict[str, Any]:
    """Return the stored preview or raise ToolError (unknown, wrong listing, expired, already applied)."""
    with closing(_previews(research)) as con:
        row = con.execute("SELECT * FROM seo_previews WHERE preview_id=?", (str(preview_id).strip(),)).fetchone()
    if row is None:
        raise ToolError(f"Unknown preview_id '{preview_id}'. Run seo_preview_update first and apply its preview_id.")
    if row["listing_id"] != listing_id:
        raise ToolError(f"Preview {preview_id} belongs to listing {row['listing_id']}, not {listing_id}.")
    if row["applied_at"]:
        raise ToolError(f"Preview {preview_id} was already applied at {row['applied_at']}. Make a new preview for further changes.")
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(tz=timezone.utc):
        raise ToolError(f"Preview {preview_id} expired at {row['expires_at']} (previews last {PREVIEW_TTL_HOURS}h). Run seo_preview_update again.")
    return {"preview_id": row["preview_id"], "before": json.loads(row["before_json"]), "proposal": json.loads(row["proposal_json"])}


def mark_applied(research: ResearchDB, preview_id: str) -> None:
    with closing(_previews(research)) as con, con:
        con.execute("UPDATE seo_previews SET applied_at=? WHERE preview_id=?", (now_iso(), preview_id))


# --------------------------------------------------------------------------------------------- tag gaps
def _extract_tag_lists(obj: Any) -> list[list[str]]:
    """Find every 'tags' list/CSV inside an arbitrary JSON blob (competitor snapshot formats vary)."""
    found: list[list[str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "tags" and isinstance(v, (list, str)):
                tags = [t.strip() for t in (v.split(",") if isinstance(v, str) else v) if isinstance(t, str) and t.strip()]
                if tags:
                    found.append(tags)
            else:
                found.extend(_extract_tag_lists(v))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_extract_tag_lists(v))
    return found


def _snapshot_listings(data: Any) -> list[tuple[Any, list[str]]]:
    """(listing_id, tags) pairs from a competitor snapshot. Task 2 format: {"shop_id", "shop_name", "listings": [{"listing_id", "tags": [...]}]};
    anything else falls back to scanning for 'tags' keys."""
    if isinstance(data, dict) and isinstance(data.get("listings"), list):
        out = []
        for l in data["listings"]:
            if isinstance(l, dict):
                for tags in _extract_tag_lists({"tags": l.get("tags")}):
                    out.append((l.get("listing_id"), tags))
        return out
    return [(None, tags) for tags in _extract_tag_lists(data)]


def tag_gaps(research: ResearchDB, my_tags: list[str], keyword: str, max_age_days: float = 14, limit: int = 30) -> dict[str, Any]:
    kw = norm_keyword(keyword)
    now = datetime.now(tz=timezone.utc)
    sources: list[list[str]] = []
    seen_ids: set[Any] = set()  # a listing present in both market cache and a snapshot counts once
    provenance: dict[str, Any] = {}

    market = research.get_market(kw, max_age_days, 1000)
    for r in market["rows"]:
        if r["tags"]:
            sources.append([str(t) for t in r["tags"]])
            seen_ids.add(r["listing_id"])
    ages = [r["age_days"] for r in market["rows"]]
    provenance["market_listings"] = {"status": market["status"], "listings": market["count"],
                                     "with_tags": len([r for r in market["rows"] if r["tags"]]),
                                     "newest_age_days": min(ages) if ages else None, "oldest_age_days": max(ages) if ages else None,
                                     "message": market["message"]}

    with closing(_con(research)) as con:  # latest snapshot per competitor shop
        snaps = con.execute("SELECT shop_id, taken_at, data_json FROM competitor_snapshots s WHERE taken_at="
                            "(SELECT MAX(taken_at) FROM competitor_snapshots WHERE shop_id=s.shop_id)").fetchall()
        kw_rows = con.execute("SELECT keyword, searches, fetched_at FROM keywords WHERE searches IS NOT NULL").fetchall()
    snap_tag_lists = 0
    for s in snaps:
        try:
            pairs = _snapshot_listings(json.loads(s["data_json"] or "null"))
        except ValueError:
            pairs = []
        pairs = [(i, t) for i, t in pairs if i is None or i not in seen_ids]
        seen_ids.update(i for i, _ in pairs if i is not None)
        snap_tag_lists += len(pairs)
        sources.extend(t for _, t in pairs)
    snap_ages = [age_days(s["taken_at"], now) for s in snaps]
    provenance["competitor_snapshots"] = {
        "shops": len(snaps), "listings_with_tags": snap_tag_lists,
        "newest_age_days": min(snap_ages) if snap_ages else None, "oldest_age_days": max(snap_ages) if snap_ages else None,
        "stale": bool(snap_ages) and min(snap_ages) > max_age_days,
        "message": f"{len(snaps)} competitor snapshot(s)." if snaps else "No competitor snapshots cached.",
    }

    volume: dict[str, tuple[float, str]] = {}
    for r in kw_rows:
        k = norm_keyword(r["keyword"])
        if k not in volume or r["fetched_at"] > volume[k][1]:
            volume[k] = (r["searches"], r["fetched_at"])
    kw_ages = [age_days(v[1], now) for v in volume.values()]
    provenance["keyword_volume"] = {"keywords_cached": len(volume), "newest_age_days": min(kw_ages) if kw_ages else None,
                                    "oldest_age_days": max(kw_ages) if kw_ages else None}

    mine = {_tag_key(t) for t in my_tags}
    counts: dict[tuple[str, ...], dict[str, Any]] = {}
    for tags in sources:
        seen_in_listing: set[tuple[str, ...]] = set()
        for t in tags:
            key = _tag_key(t)
            if not key or key in mine or key in seen_in_listing:
                continue
            seen_in_listing.add(key)
            e = counts.setdefault(key, {"tag": t.strip().lower(), "listings": 0})
            e["listings"] += 1
    total = len(sources)
    gaps = []
    for e in counts.values():
        vol = volume.get(norm_keyword(e["tag"]))
        gaps.append({"tag": e["tag"], "used_by": e["listings"], "share": round(e["listings"] / total, 2) if total else 0,
                     "search_volume": vol[0] if vol else None, "fits_20_chars": len(e["tag"]) <= TAG_CHARS_MAX})
    gaps.sort(key=lambda g: (-g["used_by"], -(g["search_volume"] or 0), g["tag"]))
    notes = []
    if not sources:
        notes.append("No competitor tags cached. Ask ProfitTree (product_finder) for this keyword and save it with research_save_market_listings, then re-run.")
    if provenance["market_listings"]["status"] in ("stale", "partial"):
        notes.append(provenance["market_listings"]["message"])
    if not volume:
        notes.append("No cached keyword volume; gaps are ranked by frequency only. Save keyword_finder results with research_save_keywords.")
    return {"keyword": kw, "my_tags": my_tags, "listings_compared": total, "data": provenance, "notes": notes, "gaps": gaps[:max(1, min(limit, 200))]}


# --------------------------------------------------------------------------------------------- preview validation
def validate_proposal(listing: dict[str, Any], title: str | None, tags: list[str] | None, description: str | None,
                      lint: Callable[..., dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """Return (proposal containing only the fields being changed, errors). Etsy limits: title<=140, 13 tags, <=20 chars each."""
    errors: list[str] = []
    proposal: dict[str, Any] = {}
    if title is not None:
        t = " ".join(title.split())
        if not t:
            errors.append("Title can't be empty.")
        elif len(t) > TITLE_MAX:
            errors.append(f"Title is {len(t)} chars; Etsy max is {TITLE_MAX}.")
        proposal["title"] = t
    if tags is not None:
        if not isinstance(tags, list) or not all(isinstance(x, str) for x in tags):
            errors.append("tags must be a list of strings.")
        else:
            cleaned = [" ".join(x.split()) for x in tags if x.strip()]
            if len(cleaned) > TAGS_MAX:
                errors.append(f"{len(cleaned)} tags; Etsy max is {TAGS_MAX}.")
            errors += [f"Tag '{x}' is {len(x)} chars; max {TAG_CHARS_MAX}." for x in cleaned if len(x) > TAG_CHARS_MAX]
            proposal["tags"] = cleaned
    if description is not None:
        if not description.strip():
            errors.append("Description can't be empty.")
        proposal["description"] = description.strip()
    if not proposal:
        errors.append("Nothing to change: pass at least one of title, tags, description.")
    # linter catches the rest (bad characters, duplicate tags, repeated punctuation); skip errors already reported above
    res = lint(proposal.get("title", ""), proposal.get("tags"), proposal.get("description", ""), None)
    errors += [e for e in res["errors"] if e not in errors and not re.match(r"(Title is \d+|\d+ tags;|Tag '.*' is \d+ chars)", e)]
    return proposal, errors


def build_diff(before: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    diff: dict[str, Any] = {}
    for f, new in proposal.items():
        old = before.get(f)
        entry: dict[str, Any] = {"before": old, "after": new, "changed": old != new}
        if f == "tags":
            o, n = {t.lower() for t in old or []}, {t.lower() for t in new}
            entry.update(added=sorted(n - o), removed=sorted(o - n))
        elif f in ("title", "description"):
            entry.update(chars_before=len(old or ""), chars_after=len(new))
        diff[f] = entry
    return diff


# --------------------------------------------------------------------------------------------- tool registration
def register(mcp: Any, *, research: ResearchDB, call: Callable[..., Awaitable[Any]], sid: Callable[[], Awaitable[str]],
             guard: Callable[[str], None], update_listing: Callable[..., Awaitable[dict[str, Any]]],
             taxonomy_flat: Callable[[], Awaitable[list[dict[str, Any]]]], lint: Callable[..., dict[str, Any]],
             write: ToolAnnotations) -> None:
    """Register seo_audit, seo_tag_gaps, seo_preview_update, seo_apply_update on the server."""

    async def best_effort(coro: Awaitable[Any]) -> Any:
        try:
            return await coro
        except Exception:  # noqa: BLE001 - optional extras must never sink the audit
            return None

    async def context(listing_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
        listing = await call("GET", f"/listings/{listing_id}", params={"includes": ["Images"]})
        shop = await sid()
        files = None
        if listing.get("type") == "download" or listing.get("is_digital"):
            data = await best_effort(call("GET", f"/shops/{shop}/listings/{listing_id}/files"))
            files = len(data.get("results") or []) if isinstance(data, dict) else None
        # UNSURE endpoint: Etsy's getListingProperties path; failure just skips the attribute check.
        props = await best_effort(call("GET", f"/shops/{shop}/listings/{listing_id}/properties"))
        properties = [p for p in props.get("results") or [] if p.get("values")] if isinstance(props, dict) else None
        cats: list[str] = []
        flat = await best_effort(taxonomy_flat())
        if flat and listing.get("taxonomy_id"):
            node = next((n for n in flat if n["taxonomy_id"] == listing["taxonomy_id"]), None)
            cats = [c.strip() for c in (node["path"].split(" > ") if node else [])]
        return listing, {"image_count": len(listing.get("images") or []), "file_count": files, "properties": properties, "category_names": cats}

    @mcp.tool(annotations=AUDIT_ANNOTATIONS)
    async def seo_audit(listing_id: int, keyword: str | None = None) -> dict[str, Any]:
        """Score one of your listings 0-100 for SEO with weighted sub-scores (title, tags, description, attributes, images, digital-download fields) and a prioritized fix list. keyword defaults to the main phrase inferred from title+tags. The result is saved to the local audits table (no Etsy write)."""
        listing, extra = await context(listing_id)
        report = audit_listing(listing, keyword=keyword, lint=lint, **extra)
        report["listing_id"] = listing_id
        store_audit(research, report)
        return report

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def seo_tag_gaps(listing_id: int, keyword: str, max_age_days: float = 14, limit: int = 30) -> dict[str, Any]:
        """Tags competitors use for this keyword that your listing lacks, ranked by how many competitor listings use them and, if cached, search volume. Uses only the local cache (market_listings, competitor_snapshots, keywords) and reports what was cached and how old it is."""
        listing = await call("GET", f"/listings/{listing_id}")
        res = tag_gaps(research, [str(t) for t in listing.get("tags") or []], keyword, max_age_days, limit)
        res["listing_id"] = listing_id
        return res

    @mcp.tool(annotations=AUDIT_ANNOTATIONS)
    async def seo_preview_update(listing_id: int, title: str | None = None, tags: list[str] | None = None, description: str | None = None) -> dict[str, Any]:
        """Dry run of an SEO edit. Validates against Etsy limits (title <=140, <=13 tags, <=20 chars each), returns a before/after diff and the new audit score, and stores the proposal under a preview_id (valid 24h). Changes nothing on Etsy; apply it with seo_apply_update."""
        listing, extra = await context(listing_id)
        proposal, errors = validate_proposal(listing, title, tags, description, lint)
        before = {f: listing.get(f) for f in PREVIEW_FIELDS}
        current = audit_listing(listing, lint=lint, **extra)
        out: dict[str, Any] = {"listing_id": listing_id, "valid": not errors, "errors": errors, "diff": build_diff(before, proposal),
                               "score_before": current["score"]}
        if errors:
            out.update(preview_id=None, message="Not stored: fix the errors and preview again.")
            return out
        after = audit_listing({**listing, **proposal}, keyword=current["main_keyword"], lint=lint, **extra)
        pid, exp = store_preview(research, listing_id, before, proposal)
        out.update(preview_id=pid, expires_at=exp, score_after=after["score"], score_change=round(after["score"] - current["score"], 1),
                   remaining_fixes=after["fixes"][:5],
                   message=f"Nothing was changed. Show this diff to the user; if approved, call seo_apply_update(listing_id={listing_id}, preview_id='{pid}').")
        return out

    @mcp.tool(annotations=write)
    async def seo_apply_update(listing_id: int, preview_id: str) -> dict[str, Any]:
        """Apply a stored seo_preview_update exactly as previewed (nothing else can be written). Refuses unknown, expired, already-applied previews, and previews whose listing changed on Etsy since. Respects ETSY_MCP_MODE and is recorded in the write log."""
        guard("write")
        pv = load_preview(research, listing_id, preview_id)
        current = await call("GET", f"/listings/{listing_id}")
        drift = [f for f in pv["proposal"] if (current.get(f) or ([] if f == "tags" else "")) != (pv["before"].get(f) or ([] if f == "tags" else ""))]
        if drift:
            raise ToolError(f"Listing {listing_id} changed on Etsy since the preview ({', '.join(drift)}). Run seo_preview_update again so you approve the current diff.")
        res = await update_listing("seo_apply_update", listing_id, dict(pv["proposal"]), False)
        mark_applied(research, pv["preview_id"])
        return {"applied": True, "preview_id": pv["preview_id"], "fields": sorted(pv["proposal"]), **res}
