# Changelog

## Unreleased (2026-10-12)

### Fixed
- `seo_audit` scored every download as a physical item: it read `type`, but Etsy's API returns `listing_type`. Digital-file checks now run.
- Photo scoring capped at 10 and flagged anything above it; Etsy allows 20. Full marks now from 10 photos up to 20.
- Dropped the "No styles set" penalty (Etsy retired listing styles; the API ignores them).
- Main keyword matching ignores word order, as Etsy search does ("planner digital 2027" matches "2027 Digital Planner").
- HTML entities from Etsy (`&#39;`, `&quot;`) are unescaped before scoring.
- Title rule follows Etsy's current guidance: flag titles over 15 words instead of short-but-fine titles under 70 chars.

### Added
- `etsy_update_listing_image`: change a photo's rank and/or alt text without re-uploading; keeps the fields you leave out (Etsy wipes alt text otherwise).
- `etsy_get_listing_properties` / `etsy_set_listing_property`: category attributes (colour, occasion, holiday), value ids looked up automatically.
- `etsy_list_listing_videos` / `etsy_delete_listing_video` (delete needs `ETSY_MCP_MODE=full`).
- `etsy_create_digital_listing` manifests: per-image alt text (`{"path", "alt"}` or `image_alts`), `properties`, and up-front 20-photo / 5-file checks.

## Unreleased
- Listing videos: new `etsy_upload_listing_video` (Etsy's uploadListingVideo) from a file in ETSY_UPLOAD_DIRS, an https URL or
  base64, up to 100 MB. `etsy_create_digital_listing` takes `video_path`/`video_url` and a `video` manifest key.
- Digital drafts now default to `when_made=2020_2026` (a finished item) instead of `made_to_order`; pass `when_made` to override.
- Write retries tightened (`client.py`): a write (POST/PUT/PATCH/DELETE) is re-sent only on a 429 or when the connection could not be made (connect error or timeout, pool timeout), because then Etsy certainly did not act on it. A 5xx, a read/write timeout or a dropped connection is attempted once and raised as `OUTCOME UNKNOWN: ... may or may not have been applied ... check the listing before retrying`, which also lands in the write log. Reads (GET) retry exactly as before. A 401 still triggers one token refresh and re-send (Etsy refused the request before acting on it).
- Read-only MCP resources (`resources.py`): `etsy://shop/listings` (your active listings; one Etsy read cached 5 minutes in memory and dropped on every write), `etsy://keywords/{seed}`, `etsy://competitor/{shop}`, `etsy://audit/{listing_id}` and `etsy://writes/recent`. They never write; missing data returns a message naming the tool to run first.
- CI (`.github/workflows/tests.yml`): the offline suite on every push and pull request, Python 3.11, no secrets.
- Hardening tests: `test_integration.py` (every newer tool and resource through MCP in-process, resources over Streamable HTTP with bearer auth, and a real stdio subprocess), `test_hardening.py` (429 retry/backoff and clear errors, every write tool in readonly/safe/full mode with write-log checks, tool count vs the README), `test_resources.py`.
- Fix: the write log now records refused `seo_apply_update` attempts (readonly mode, unknown/expired/already-applied/drifted preview) and refused `etsy_api_request` writes (absolute URL or bad path); previously these were rejected without a log row. A successful apply still logs exactly one row.
- `competitor_profile` parses only the latest snapshot instead of every stored one (matters after months of daily snapshots).
- README: tools by area, resources, the snapshot CLI, cache location, the mode matrix, and a "what this server deliberately does NOT do" section.
- SEO tools (`seo.py`): `seo_audit`, `seo_tag_gaps`, `seo_preview_update`, `seo_apply_update` (preview-then-apply; research.db schema v3 adds `seo_previews`).
- Competitor tracking (public Etsy API only): `competitor_add/remove/list/snapshot/snapshot_all/diff/profile`, plus the `indigorepublica-etsy-snapshot` CLI for schedulers. Schema v2 adds the `competitors` watchlist and `market_listings.shop_name` (filled from ProfitTree's `shop_name`).
- Research cache (SQLite, `ETSY_RESEARCH_DB`, default `~/.indigorepublica-etsy-mcp/research.db`) with schema versioning: new tools `research_save_keywords`, `research_save_market_listings`, `research_get_keywords`, `research_get_market`, `research_cache_stats`.
- Append-only write log: every write tool (create/update/publish/upload/delete/inventory/tracking/shop/api_request) records before/after, server mode, dry-run flag and result; read it with `etsy_write_log`.
- Renamed the project (package, console scripts, data folder `~/.indigorepublica-etsy-mcp`, systemd unit) from its previous name; the old data folder is moved automatically on first start.
