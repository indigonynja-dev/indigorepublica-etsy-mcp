# indigorepublica-etsy-mcp

MCP server that gives Claude full control of **your own** Etsy shop: listings, digital-download files,
images, orders, sales, fees, reviews. Runs over **stdio** for Claude Code and **Streamable HTTP** for
Claude.ai (web, desktop, mobile) behind an HTTPS tunnel. 41 tools, 3 prompts, offline test suite.

> The term 'Etsy' is a trademark of Etsy, Inc. This application uses the Etsy API but is not endorsed or certified by Etsy, Inc.

## Quickstart (WSL2 / Linux / macOS)

```bash
# 0. Register an app at https://www.etsy.com/developers/your-apps (name must NOT contain "Etsy").
#    Callback URL: http://localhost:3003/oauth/redirect   -> wait for "Personal Approval".

# 1. Install
cd ~/mcp-servers/indigorepublica-etsy-mcp
uv sync --extra dev
cp .env.example .env && chmod 600 .env     # fill ETSY_KEYSTRING + ETSY_SHARED_SECRET
mkdir -p ~/etsy-products
uv run pytest -q                           # offline tests

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

## Safety model

| Control | Default | Effect |
|---|---|---|
| `ETSY_MCP_MODE` | `safe` | `readonly` = GET only, `safe` = no deletes, `full` = everything |
| Publish gate | on | `state=active` needs `confirm_publish_fee=true` (Etsy charges a listing fee) |
| Upload jail | `~/etsy-products` | `file_path` uploads must resolve inside `ETSY_UPLOAD_DIRS` (symlinks/`..` resolved) |
| Remote auth | required | HTTP mode refuses to start without `MCP_AUTH_TOKEN`; constant-time bearer check |
| Host check | on | Only localhost + `MCP_PUBLIC_HOSTS` accepted (DNS-rebinding guard) |
| Token file | `~/.indigorepublica-etsy-mcp/tokens.json` | chmod 600, atomic writes, file lock around refresh |
| Write log | always on | Every write (incl. blocked/failed attempts) is appended to `research.db` → `etsy_write_log`; triggers forbid UPDATE/DELETE |
| `etsy_api_request` | relative paths only | No absolute URLs, so your API key can't be sent to other hosts |

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
  research.py SQLite research cache (ProfitTree data etc.) + append-only write log (`ETSY_RESEARCH_DB`)
skills/etsy-shop-ops/SKILL.md   Claude Code skill: product folders, SEO rules, draft->publish
examples/sample-product/        listing.json manifest convention
deploy/indigorepublica-etsy-mcp.service   systemd --user unit for 24/7 remote mode
tests/test_server.py            mocked-Etsy tests incl. live HTTP auth/host checks
```

Requires Python 3.11+ and the MCP Python SDK 2.x (`MCPServer`, formerly `FastMCP`).
