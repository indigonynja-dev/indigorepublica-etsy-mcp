"""Configuration loaded from environment / .env."""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_SCOPES = (
    "listings_r listings_w listings_d shops_r shops_w "
    "transactions_r transactions_w profile_r feedback_r billing_r"
)
API_BASE = "https://api.etsy.com/v3/application"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
AUTH_URL = "https://www.etsy.com/oauth/connect"
OAS_URL = "https://www.etsy.com/openapi/generated/oas/3.0.0.json"
MODES = ("readonly", "safe", "full")

log = logging.getLogger("indigorepublica_etsy_mcp")


def migrate_legacy_data_dir(home: Path | None = None) -> bool:
    """One-time move of the pre-rename data folder to the new name."""
    base = home or Path.home()
    old, new = base / (".nynja" + "-etsy-mcp"), base / ".indigorepublica-etsy-mcp"
    if old.is_dir() and not new.exists():
        shutil.move(str(old), str(new))
        log.info("Moved data folder %s -> %s", old, new)
        return True
    return False


def _load_env() -> None:
    explicit = os.environ.get("ETSY_ENV_FILE")
    if explicit:
        load_dotenv(Path(explicit).expanduser(), override=False)
        return
    # Project root (two levels above this file when run from a checkout), then CWD.
    root_env = Path(__file__).resolve().parents[2] / ".env"
    if root_env.exists():
        load_dotenv(root_env, override=False)
    load_dotenv(Path.cwd() / ".env", override=False)


def _paths(value: str) -> list[Path]:
    return [Path(p).expanduser().resolve() for p in value.split(":") if p.strip()]


def _research_db_path() -> Path:
    return Path(os.environ.get("ETSY_RESEARCH_DB") or "~/.indigorepublica-etsy-mcp/research.db").expanduser()


@dataclass
class Settings:
    keystring: str = ""
    shared_secret: str = ""
    redirect_uri: str = "http://localhost:3003/oauth/redirect"
    scopes: str = DEFAULT_SCOPES
    shop_id: str = ""
    token_file: Path = field(default_factory=lambda: Path("~/.indigorepublica-etsy-mcp/tokens.json").expanduser())
    mode: str = "safe"
    upload_dirs: list[Path] = field(default_factory=list)
    max_qps: float = 4.0
    http_host: str = "127.0.0.1"
    http_port: int = 8765
    auth_token: str = ""
    public_hosts: list[str] = field(default_factory=list)
    oas_url: str = OAS_URL
    max_upload_mb: int = 25
    research_db: Path = field(default_factory=lambda: _research_db_path())

    @property
    def api_key_header(self) -> str:
        # Since Feb 9, 2026 Etsy requires "keystring:shared_secret" on every request.
        return f"{self.keystring}:{self.shared_secret}"

    def require_credentials(self) -> None:
        missing = [n for n, v in (("ETSY_KEYSTRING", self.keystring), ("ETSY_SHARED_SECRET", self.shared_secret)) if not v]
        if missing:
            raise RuntimeError(
                f"Missing {', '.join(missing)}. Copy .env.example to .env and fill in the values "
                "from etsy.com/developers/your-apps -> 'See API Key Details'."
            )


def load_settings() -> Settings:
    _load_env()
    migrate_legacy_data_dir()
    env = os.environ.get
    mode = (env("ETSY_MCP_MODE") or "safe").strip().lower()
    if mode not in MODES:
        raise RuntimeError(f"ETSY_MCP_MODE must be one of {MODES}, got {mode!r}")
    return Settings(
        keystring=(env("ETSY_KEYSTRING") or "").strip(),
        shared_secret=(env("ETSY_SHARED_SECRET") or "").strip(),
        redirect_uri=(env("ETSY_REDIRECT_URI") or "http://localhost:3003/oauth/redirect").strip(),
        scopes=" ".join((env("ETSY_SCOPES") or DEFAULT_SCOPES).split()),
        shop_id=(env("ETSY_SHOP_ID") or "").strip(),
        token_file=Path(env("ETSY_TOKEN_FILE") or "~/.indigorepublica-etsy-mcp/tokens.json").expanduser(),
        mode=mode,
        upload_dirs=_paths(env("ETSY_UPLOAD_DIRS") or "~/etsy-products"),
        max_qps=float(env("ETSY_MAX_QPS") or 4),
        http_host=env("MCP_HTTP_HOST") or "127.0.0.1",
        http_port=int(env("MCP_HTTP_PORT") or 8765),
        auth_token=(env("MCP_AUTH_TOKEN") or "").strip(),
        public_hosts=[h.strip() for h in (env("MCP_PUBLIC_HOSTS") or "").split(",") if h.strip()],
        oas_url=env("ETSY_OAS_URL") or OAS_URL,
        max_upload_mb=int(env("ETSY_MAX_UPLOAD_MB") or 25),
        research_db=_research_db_path(),
    )
