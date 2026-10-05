"""OAuth token storage shared by every server process (stdio + HTTP).

Etsy access tokens last ~1 hour; refresh tokens ~90 days. Each refresh returns a new
refresh token, so two processes refreshing at once could race. We serialize refreshes
with an advisory file lock and always re-read the file inside the lock.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any, Iterator

try:  # POSIX (WSL/Linux/macOS)
    import fcntl
except ImportError:  # pragma: no cover - native Windows
    fcntl = None  # type: ignore[assignment]

REFRESH_MARGIN_S = 300  # refresh when < 5 minutes remain


class TokenStore:
    def __init__(self, path: Path):
        self.path = path
        self._cache: dict[str, Any] | None = None
        self._mtime: float = 0.0

    # ---------- file helpers
    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> dict[str, Any]:
        """Return tokens, reloading if another process rewrote the file."""
        if not self.path.exists():
            raise FileNotFoundError(
                f"No Etsy tokens at {self.path}. Run `uv run indigorepublica-etsy-auth` once to connect your shop."
            )
        mtime = self.path.stat().st_mtime
        if self._cache is None or mtime != self._mtime:
            self._cache = json.loads(self.path.read_text())
            self._mtime = mtime
        return self._cache

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.path.parent, 0o700)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        self._cache = data
        self._mtime = self.path.stat().st_mtime

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(".lock")
        with open(lock_path, "w") as fh:
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl:
                    fcntl.flock(fh, fcntl.LOCK_UN)

    # ---------- token semantics
    @staticmethod
    def from_token_response(resp: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
        access = resp["access_token"]
        user_id = access.split(".", 1)[0] if "." in access else (previous or {}).get("user_id")
        return {
            "access_token": access,
            "refresh_token": resp.get("refresh_token") or (previous or {}).get("refresh_token"),
            "token_type": resp.get("token_type", "Bearer"),
            "expires_at": time.time() + int(resp.get("expires_in", 3600)),
            "user_id": user_id,
            "shop_id": (previous or {}).get("shop_id"),
            "scopes": (previous or {}).get("scopes"),
            "obtained_at": time.time(),
        }

    @staticmethod
    def needs_refresh(data: dict[str, Any]) -> bool:
        return float(data.get("expires_at", 0)) - time.time() < REFRESH_MARGIN_S
