# indigorepublica-etsy-mcp

MCP server that gives Claude full control of **your own** Etsy shop: listings, digital-download files,
images, orders, sales, fees, reviews, plus a local research cache, competitor tracking and SEO audits. Runs over **stdio**
for Claude Code and **Streamable HTTP** for Claude.ai (web, desktop, mobile) behind an HTTPS tunnel.
52 tools, 3 prompts, 5 resources, offline test suite.

> The term 'Etsy' is a trademark of Etsy, Inc. This application uses the Etsy API but is not endorsed or certified by Etsy, Inc.

## Quickstart (WSL2 / Linux / macOS)

```bash
# 0. Register an app at https://www.etsy.com/developers/your-apps (name must NOT contain "Etsy").
#    Callback URL: http://localhost:3003/oauth/redirect   -> wait for "Personal Approval".

# 1. Install
cd ~/mcp-servers/indigorepublica-etsy-mcp
uv sync --all-extras
cp .env.example .env && chmod 600 .env     # fill ETSY_KEYSTRING + ETSY_SHARED_SECRET
mkdir -p ~/etsy-products
uv run --all-extras pytest -q              # offline tests (the same command CI runs)

# 2. Connect your shop (once; tokens auto-refresh afterwards)
uv run indigorepublica-etsy-auth                     # or: --manual  |  --status

# 3a. Claude Code (stdio)
claude mcp add --scope user etsy -- uv run --directory ~/mcp-servers/indigorepublica-etsy-mcp indigorepublica-etsy-mcp
mkdir -p ~/.claude/skills && cp -r skills/etsy-shop-ops ~/.claude/skills/

# 3b. Claude.ai (remote)
#   .env: MCP_AUTH_TOKEN=$(openssl rand -hex 32)   MCP_PUBLIC_HOSTS=<machine>.<tailnet>.ts.net
sudo tailscale funnel --bg localhost:8765
uv run indigorepublica-etsy-mcp --transport http     # or the systemd unit in deploy/
#   Claude.ai -> Customize -> Connectors -> + Add -> Add custom connector
#   URL https://<machine>.<tailnet>.ts.net/mcp | Authentication: No sign in
#   Request header: Authorization = Bearer <MCP_AUTH_TOKEN>
```

## Tools (52)

Tools marked **write** change something on Etsy and are refused in `readonly` mode; **delete** tools are refused unless
`ETSY_MCP_MODE=full`. Every other tool only reads (see [Safety model](#safety-model)).

### Shop, listings, files and images

| Tool | What it does |
|---|---|
| `etsy_whoami` | Check the connection: authorized user, shop, server mode, token health. Call this first if anything fails. |
| `etsy_get_shop` | Full shop record: policies, counts, vacation status. |
| `etsy_update_shop` | **write** Update shop headline, announcement and sale messages. |
| `etsy_list_shop_sections` / `etsy_create_shop_section` | List sections (**write**: create one). |
| `etsy_list_listings` | Listings by state with views, favorites, price, tags (auto-paginates up to 500). |
| `etsy_get_listing` | Full listing record, optionally with images and inventory. |
| `etsy_create_draft_listing` | **write** Create a DRAFT listing (no fee until published). |
| `etsy_update_listing` | **write** Patch listing fields; `state=active` needs `confirm_publish_fee=true`. |
| `etsy_publish_listing` | **write** Publish a draft (charges Etsy's listing fee; needs `confirm_publish_fee=true`). |
| `etsy_delete_listing` | **delete** Permanently delete a listing. |
| `etsy_create_digital_listing` | **write** One call: draft + images + files (+ optional publish) from a `listing.json` manifest. |
| `etsy_list_listing_images` / `etsy_upload_listing_image` / `etsy_delete_listing_image` | List photos; **write** upload one; **delete** remove one. |
| `etsy_list_listing_files` / `etsy_upload_listing_file` / `etsy_delete_listing_file` | List download files; **write** attach one; **delete** remove one. |
| `etsy_get_listing_inventory` / `etsy_update_listing_inventory` | Variations, SKUs, per-option prices (**write**: replace the inventory). |

### Market, orders and money

| Tool | What it does |
|---|---|
| `etsy_search_active_listings` | Public marketplace search through the official API, for quick competitive checks. |
| `etsy_search_taxonomy` / `etsy_get_taxonomy_properties` | Find a `taxonomy_id`; list a category's attributes. |
| `etsy_list_orders` / `etsy_get_order` | Orders with items and totals. |
| `etsy_add_tracking` | **write** Mark a physical order shipped with tracking. |
| `etsy_sales_summary` | Revenue report for a window: orders, gross, average order, top listings. |
| `etsy_list_ledger_entries` | Payment-account ledger: sales, Etsy fees, ad charges, deposits, refunds. |
| `etsy_list_reviews` | Recent reviews with rating, text, listing, date. |
| `etsy_list_shipping_profiles` / `etsy_list_return_policies` | Shipping profiles (ids for physical listings); return policies. |

### Utilities

| Tool | What it does |
|---|---|
| `etsy_seo_check` | Lint a title/tags/description before writing: lengths, once-only characters, 13x20 tag rules. No API call. |
| `etsy_listing_audit` | Scan the shop's listings and flag fixable problems, worst first. |
| `etsy_find_endpoint` | Search Etsy's published OpenAPI spec for an endpoint with no dedicated tool. |
| `etsy_api_request` | **write / delete** Escape hatch for any v3 endpoint. Relative paths only; the mode applies to the HTTP method. |

### Research cache and write log

Claude passes in what it learned elsewhere (for example from ProfitTree); this server stores it and reports how old it is.
Nothing here calls Etsy, so all of it works in every server mode.

| Tool | What it does |
|---|---|
| `research_save_keywords` | Store keyword rows from a research source (default `profittree`); the same keyword+source replaces the old row. |
| `research_save_market_listings` | Store competitor/market listings found for a keyword. |
| `research_get_keywords` | Cached keyword rows (best `niche_score` first) with `age_days`, a stale flag and a fresh / partial / stale / missing status. |
| `research_get_market` | Cached market listings for a keyword, with an average-price and estimated-revenue summary. |
| `research_cache_stats` | Row counts and oldest/newest timestamps for every cache table, plus the database path and schema version. |
| `etsy_write_log` | Read the append-only log of writes (newest first), including blocked and failed attempts. |

### Competitor tracking (public Etsy data only)

Other shops are read through Etsy's public API endpoints with your API key. Nothing is ever written to another shop.

| Tool | What it does |
|---|---|
| `competitor_add` / `competitor_remove` / `competitor_list` | Maintain the watchlist (by shop name or numeric id). Removing keeps the stored snapshots. |
| `competitor_snapshot` | Fetch all active listings of a watched shop (title, tags, price, image count, dates) and store a snapshot. |
| `competitor_snapshot_all` | Snapshot every watched shop; one failing shop does not stop the rest. |
| `competitor_diff` | What changed between snapshots: new/removed listings, title, tag, price and image-count changes. |
| `competitor_profile` | From the latest snapshot: price median/quartiles, top 30 tags, 10 newest listings, average listing age. |

### SEO audit and safe edits

| Tool | What it does |
|---|---|
| `seo_audit` | Score a listing 0-100 (title, tags, description, attributes, images, digital fields) with a prioritized fix list; stored locally. |
| `seo_tag_gaps` | Tags competitors use for a keyword that your listing lacks, ranked by use and cached search volume. Cache only. |
| `seo_preview_update` | Dry run of a title/tags/description edit: validation, before/after diff, new score. Stores a `preview_id` (valid 24 h); changes nothing on Etsy. |
| `seo_apply_update` | **write** Apply a stored preview exactly as previewed. Refuses unknown, expired, already-applied or drifted previews. |

Suggested loop: save ProfitTree data with `research_save_*` -> `competitor_add` + `competitor_snapshot` -> `seo_audit` ->
`seo_tag_gaps` -> `seo_preview_update` -> review the diff -> `seo_apply_update`.

## Resources (5, read-only)

Resources are data Claude (or you, with an `@` mention in Claude Code) can attach to a conversation. They **never write**:
no Etsy write, no database write, no write-log row. They work in every server mode and over both transports.

| URI | Returns | Source |
|---|---|---|
| `etsy://shop/listings` | Your active listings, newest update first: `listing_id`, `title`, `price`, `currency`, `state`, `updated`. Up to 500; `truncated` says if there are more. | One Etsy read, cached in memory for 5 minutes and dropped whenever this server writes (`cached` / `cache_age_seconds` say which you got). |
| `etsy://keywords/{seed}` | Cached keyword rows containing the seed, with `age_days`, `stale` (older than 7 days) and a `status` of fresh / partial / stale / missing. | Research cache. |
| `etsy://competitor/{shop}` | Latest competitor profile (price quartiles, top tags, newest listings, average age) plus `snapshot_age_days`. `{shop}` is the shop name or id. | Latest stored snapshot. |
| `etsy://audit/{listing_id}` | The most recent stored `seo_audit` report for a listing, with `audited_at` and `age_days`. | Research cache. |
| `etsy://writes/recent` | The last 50 write-log rows, newest first. | Write log. |

URL-encode spaces in a seed: `etsy://keywords/budget%20planner`. A seed with nothing cached returns `status: "missing"` with the
next step; a competitor that is not watched, a shop with no snapshot, or a listing with no stored audit is an MCP error that says
which tool to run first.

## Competitor snapshot CLI

`indigorepublica-etsy-snapshot` snapshots every watched shop and exits, for cron or a systemd timer. It uses public endpoints, so
it needs only `ETSY_KEYSTRING` and `ETSY_SHARED_SECRET` (no OAuth tokens), reads the same `.env`, and writes to the same research cache.

```bash
uv run indigorepublica-etsy-snapshot                 # --delay 1.0 seconds between shops (default)
```

It prints a JSON report (`watched`, `ok`, `failed`, per-shop `errors`) on stdout, logs to stderr, and exits **1 if any shop failed**
so a scheduler can alert. For example, daily at 06:30:

```cron
30 6 * * *  cd ~/mcp-servers/indigorepublica-etsy-mcp && ~/.local/bin/uv run indigorepublica-etsy-snapshot >> ~/.indigorepublica-etsy-mcp/snapshot.log 2>&1
```

Add shops first with `competitor_add`. Snapshots accumulate (nothing deletes them); `competitor_diff` compares the latest one with the
previous one, or with the newest one taken on or before a date you give.

## Cache location

Everything the server remembers lives in one SQLite file (standard-library `sqlite3`, no extra service):

| | |
|---|---|
| Default path | `~/.indigorepublica-etsy-mcp/research.db` |
| Override | `ETSY_RESEARCH_DB=/path/to/research.db` |
| Created | chmod 600; the parent folder is created if missing |
| Tables | `keywords`, `market_listings`, `competitors`, `competitor_snapshots`, `audits`, `seo_previews`, `write_log` |
| Upgrades | Versioned with `PRAGMA user_version` (currently 3); older files are migrated in place on start, a file from a newer server is refused |
| Write log | Append-only: database triggers reject `UPDATE` and `DELETE` on `write_log` |

The same folder holds `tokens.json` (OAuth tokens) and `etsy-oas.json` (a cached copy of Etsy's OpenAPI spec). To reset the cache,
stop the server and delete `research.db`; that also deletes the write log, so copy it first if you want to keep the history.

## Safety model

| Control | Default | Effect |
|---|---|---|
| `ETSY_MCP_MODE` | `safe` | `readonly` = GET only, `safe` = no deletes, `full` = everything |
| Publish gate | on | `state=active` needs `confirm_publish_fee=true` (Etsy charges a listing fee) |
| Preview before apply | on | SEO edits are stored as a preview first; `seo_apply_update` writes exactly the preview and nothing else |
| Upload jail | `~/etsy-products` | `file_path` uploads must resolve inside `ETSY_UPLOAD_DIRS` (symlinks/`..` resolved) |
| Remote auth | required | HTTP mode refuses to start without `MCP_AUTH_TOKEN`; constant-time bearer check (resources included) |
| Host check | on | Only localhost + `MCP_PUBLIC_HOSTS` accepted (DNS-rebinding guard) |
| Token file | `~/.indigorepublica-etsy-mcp/tokens.json` | chmod 600, atomic writes, file lock around refresh |
| Write log | always on | Every Etsy write, including blocked and failed attempts, is appended to `research.db` -> `etsy_write_log` (credentials and file bytes redacted); triggers forbid UPDATE/DELETE |
| Resources | read-only | They read the cache, or make one cached Etsy read; they never write |
| `etsy_api_request` | relative paths only | No absolute URLs, so your API key can't be sent to other hosts; a refused write is logged too |

What each mode allows:

| Mode | Reads | Local cache tools (`research_save_*`, `competitor_*`, `seo_audit`, `seo_preview_update`) | Etsy writes | Etsy deletes |
|---|---|---|---|---|
| `readonly` | yes | yes | no | no |
| `safe` (default) | yes | yes | yes | no |
| `full` | yes | yes | yes | yes |

The local cache tools only touch `research.db` (and read public Etsy data), so they are allowed in every mode. Removing a competitor
from the watchlist is one of them: it deletes a row in your own cache, not anything on Etsy.

## What this server deliberately does NOT do

- **No scraping.** It never fetches etsy.com pages, search results or autocomplete. The only Etsy data source is the Etsy Open API
  v3. The only other things it touches on etsy.com are the OAuth consent page you open yourself during login and Etsy's published
  OpenAPI spec (`etsy_find_endpoint`).
- **No market data of its own.** Search volume, competitor sales and revenue estimates come from ProfitTree, which Claude calls
  separately; this server only stores and analyzes what it is given. Tag-gap ranking uses only cached competitor tags and cached search volume.
- **No ads, coupons or stats endpoints.** Etsy Ads / promoted listings, coupons and discount codes, and shop traffic or conversion
  statistics are not exposed, because none has been verified to exist in Etsy Open API v3. (Ad charges and fees still appear in
  `etsy_list_ledger_entries`; per-listing `views` and favorites come with the listing record.) Before adding any such tool, confirm
  the endpoint in Etsy's spec with `etsy_find_endpoint`; do not guess paths.
- **No writes to other shops.** Competitor tools read public data only: no messages, favorites or follows.
- **No deletes unless you opt in.** Deletes need `ETSY_MCP_MODE=full`, and publishing needs `confirm_publish_fee=true`. A blocked attempt is refused and logged.
- **No paid services.** No embeddings or LLM calls inside the server. SEO scoring, tag gaps and competitor diffs are plain Python over your own data.
- **No ranking promises.** An SEO score measures what is on the page against Etsy's limits and common practice; it does not predict Etsy's ranking or sales.

## Layout

```
src/indigorepublica_etsy_mcp/
  server.py   tools, prompts, stdio + HTTP entrypoint, bearer middleware
  client.py   Etsy v3 client: keystring:secret header, refresh, throttle, retries, error hints
  auth.py     PKCE login CLI (listener or --manual paste)
  tokens.py   shared token store with refresh lock
  oas.py      search Etsy's OpenAPI spec (powers etsy_find_endpoint)
  config.py   .env loading
  competitors.py competitor watchlist/snapshots/diff/profile tools + `indigorepublica-etsy-snapshot` CLI
  seo.py      SEO audit, tag gaps, preview->apply updates
  research.py SQLite research cache (ProfitTree data etc.) + append-only write log (`ETSY_RESEARCH_DB`)
  resources.py read-only etsy:// resources (cache reads + one cached listings read)
skills/etsy-shop-ops/SKILL.md   Claude Code skill: product folders, SEO rules, draft->publish
examples/sample-product/        listing.json manifest convention
deploy/indigorepublica-etsy-mcp.service   systemd --user unit for 24/7 remote mode
.github/workflows/tests.yml     CI: the offline suite on every push and pull request (Python 3.11, no secrets)
tests/                          offline suite: mocked Etsy, temp SQLite
  test_server.py                tools, upload jail, token refresh, live HTTP auth/host checks
  test_research.py / test_competitors.py / test_seo.py   cache + write log, competitor tracking, SEO audit and preview/apply
  test_resources.py             every resource through an MCP client; the "never writes" guarantee
  test_hardening.py             429 retry/backoff, every write tool in readonly/safe/full mode, tool count vs this README
  test_integration.py           the whole server over MCP: in-process, Streamable HTTP, and a real stdio subprocess
  fakes.py                      shared fake Etsy (paginated listings, DELETE routes, fault injection)
```

Requires Python 3.11+ and the MCP Python SDK 2.x (`MCPServer`, formerly `FastMCP`).

## Tests

```bash
uv sync --all-extras
uv run --all-extras pytest -q
```

No network, no real keys: Etsy is a mocked HTTP transport and the cache is a temp file. A test asserts the registered tool count (52)
and that the "52 tools, 3 prompts, 5 resources" line at the top of this README matches the server, so adding a tool means updating both.
