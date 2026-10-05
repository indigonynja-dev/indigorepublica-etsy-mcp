# Changelog

## Unreleased
- Research cache (SQLite, `ETSY_RESEARCH_DB`, default `~/.indigorepublica-etsy-mcp/research.db`) with schema versioning: new tools `research_save_keywords`, `research_save_market_listings`, `research_get_keywords`, `research_get_market`, `research_cache_stats`.
- Append-only write log: every write tool (create/update/publish/upload/delete/inventory/tracking/shop/api_request) records before/after, server mode, dry-run flag and result; read it with `etsy_write_log`.
- Renamed the project (package, console scripts, data folder `~/.indigorepublica-etsy-mcp`, systemd unit) from its previous name; the old data folder is moved automatically on first start.
