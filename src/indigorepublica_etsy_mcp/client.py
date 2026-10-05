"""Async Etsy Open API v3 client: auth headers, token refresh, throttling, retries."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .config import API_BASE, TOKEN_URL, Settings
from .tokens import TokenStore

RETRY_STATUSES = {429, 500, 502, 503, 504}  # reads (GET): rate limits and server errors are retried with backoff
# Writes (POST/PUT/PATCH/DELETE) are re-sent only when Etsy certainly did not act on them: a 429 (rate limited, not processed)
# or a connection that could not be made. A 5xx, a timeout or a read error can arrive AFTER Etsy applied the change, so those
# are reported once as "outcome unknown" rather than risking a duplicate listing, image or file.
WRITE_RETRY_STATUSES = {429}
NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)  # all raised before any request byte is written
OUTCOME_UNKNOWN = (
    "OUTCOME UNKNOWN: this write may or may not have been applied, and it was not retried automatically. "
    "Check the listing (etsy_get_listing) or the Etsy UI before retrying, so it is not applied twice."
)

HINTS = {
    "shared secret": "Set ETSY_SHARED_SECRET in .env (Etsy needs 'keystring:shared_secret' in x-api-key since Feb 2026).",
    "not found or not active": "Your Etsy app key is not active yet. Check etsy.com/developers/your-apps; 'Pending Personal Approval' usually clears in 1-7 days.",
    "invalid_grant": "The refresh token is expired or revoked. Run `uv run indigorepublica-etsy-auth` again.",
    "insufficient_scope": "Token lacks a scope this call needs. Add it to ETSY_SCOPES and re-run `uv run indigorepublica-etsy-auth`.",
}


class EtsyError(Exception):
    def __init__(self, status: int, message: str, path: str = "", body: Any = None, outcome_unknown: bool = False):
        self.status, self.message, self.path, self.body = status, message, path, body
        self.outcome_unknown = outcome_unknown  # a write that failed in a way that may still have been applied
        hint = next((h for k, h in HINTS.items() if k in message.lower()), "")
        if not hint and status == 401:
            hint = "Token rejected. Run `uv run indigorepublica-etsy-auth --status`; if it persists, re-run `uv run indigorepublica-etsy-auth`."
        if not hint and status == 403:
            hint = "Forbidden. The token may lack a scope, or this resource isn't yours."
        if not hint and status == 404:
            hint = "Not found. Double-check the id (listing_id vs listing_image_id vs receipt_id)."
        if not hint and status == 429:
            hint = "Rate limited by Etsy. Lower ETSY_MAX_QPS or wait; daily quotas reset on a rolling window."
        self.hint = hint
        super().__init__(f"Etsy API {status} on {path}: {message}" + (f" | Hint: {hint}" if hint else "")
                         + (f" | {OUTCOME_UNKNOWN}" if outcome_unknown else ""))


def _describe(e: Exception) -> str:
    return type(e).__name__ + (f": {e}" if str(e) else "")


class RateLimiter:
    def __init__(self, qps: float):
        self.interval = 1.0 / max(qps, 0.1)
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval


def _form(data: dict[str, Any]) -> dict[str, str]:
    """Etsy form bodies take arrays as comma-separated strings and booleans as true/false."""
    out: dict[str, str] = {}
    for k, v in data.items():
        if v is None:
            continue
        if isinstance(v, bool):
            out[k] = "true" if v else "false"
        elif isinstance(v, (list, tuple)):
            out[k] = ",".join(str(x) for x in v)
        else:
            out[k] = str(v)
    return out


def _query(params: dict[str, Any] | None) -> dict[str, Any]:
    if not params:
        return {}
    q: dict[str, Any] = {}
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            q[k] = "true" if v else "false"
        elif isinstance(v, (list, tuple)):
            q[k] = ",".join(str(x) for x in v)
        else:
            q[k] = v
    return q


class EtsyClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings
        self.tokens = TokenStore(settings.token_file)
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15), transport=transport)
        self.limiter = RateLimiter(settings.max_qps)
        self._refresh_lock = asyncio.Lock()
        self._shop_id: str | None = settings.shop_id or None

    async def aclose(self) -> None:
        await self.http.aclose()

    # ---------------- auth
    async def _access_token(self) -> str:
        data = self.tokens.load()
        if not TokenStore.needs_refresh(data):
            return data["access_token"]
        async with self._refresh_lock:
            with self.tokens.locked():
                data = self.tokens.load()  # another process may have refreshed already
                if not TokenStore.needs_refresh(data):
                    return data["access_token"]
                resp = await self.http.post(
                    TOKEN_URL,
                    data={"grant_type": "refresh_token", "client_id": self.s.keystring, "refresh_token": data["refresh_token"]},
                )
                if resp.status_code != 200:
                    raise EtsyError(resp.status_code, f"token refresh failed: {resp.text}", "oauth/token")
                new = TokenStore.from_token_response(resp.json(), previous=data)
                self.tokens.save(new)
                return new["access_token"]

    async def shop_id(self) -> str:
        if self._shop_id:
            return self._shop_id
        with_file = self.tokens.load().get("shop_id")
        if with_file:
            self._shop_id = str(with_file)
            return self._shop_id
        me = await self.request("GET", "/users/me")
        if not me.get("shop_id"):
            raise EtsyError(404, "This Etsy account has no shop.", "/users/me")
        self._shop_id = str(me["shop_id"])
        data = self.tokens.load()
        data["shop_id"] = self._shop_id
        self.tokens.save(data)
        return self._shop_id

    # ---------------- core request
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
        json: Any = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        auth: bool = True,
        max_retries: int = 4,
    ) -> Any:
        """One Etsy call. Reads (GET) retry on 429, 5xx and any network error, with backoff. Writes retry only on a 429 or a
        failure to connect (nothing was sent); a 5xx, timeout or read error is raised once as an outcome-unknown error."""
        self.s.require_credentials()
        url = path if path.startswith("http") else f"{API_BASE}{path if path.startswith('/') else '/' + path}"
        is_read = method.upper() == "GET"
        retry_statuses = RETRY_STATUSES if is_read else WRITE_RETRY_STATUSES
        attempt = 0
        while True:
            headers = {"x-api-key": self.s.api_key_header, "Accept": "application/json"}
            if auth:
                headers["Authorization"] = f"Bearer {await self._access_token()}"
            await self.limiter.wait()
            kwargs: dict[str, Any] = {"params": _query(params), "headers": headers}
            if files is not None:
                kwargs["files"] = files
                if form:
                    kwargs["data"] = _form(form)
            elif form is not None:
                kwargs["data"] = _form(form)
            elif json is not None:
                kwargs["json"] = json
            try:
                resp = await self.http.request(method.upper(), url, **kwargs)
            except httpx.TransportError as e:
                if not is_read and not isinstance(e, NOT_SENT_ERRORS):
                    # Timeout, read/write error, dropped connection: the request may already have reached Etsy. Never re-send a write.
                    raise EtsyError(0, f"connection lost or timed out after the request may have been sent ({_describe(e)})", path,
                                    outcome_unknown=True) from e
                if attempt >= max_retries:
                    if is_read:
                        raise EtsyError(0, f"network error: {e}", path) from e
                    raise EtsyError(0, f"could not connect to Etsy ({_describe(e)}); the request was never sent, so nothing was changed", path) from e
                attempt += 1
                await asyncio.sleep(min(2**attempt, 20))
                continue

            if resp.status_code in retry_statuses and attempt < max_retries:
                attempt += 1
                retry_after = resp.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else min(2**attempt, 30)
                await asyncio.sleep(delay)
                continue
            if resp.status_code == 401 and auth and attempt == 0:
                # Token may have been revoked/rotated elsewhere: force a refresh once.
                data = self.tokens.load()
                data["expires_at"] = 0
                self.tokens.save(data)
                attempt += 1
                continue
            if resp.status_code >= 400:
                try:
                    body = resp.json()
                    msg = body.get("error") or body.get("error_description") or str(body)
                except ValueError:
                    body, msg = resp.text, resp.text[:500]
                raise EtsyError(resp.status_code, msg, path, body, outcome_unknown=not is_read and resp.status_code >= 500)
            if resp.status_code == 204 or not resp.content:
                return {"ok": True, "status": resp.status_code}
            try:
                return resp.json()
            except ValueError:
                return {"ok": True, "status": resp.status_code, "text": resp.text[:2000]}

    async def paginate(self, path: str, params: dict[str, Any], limit: int, offset: int = 0, max_page: int = 100, auth: bool = True) -> dict[str, Any]:
        """Fetch up to `limit` results across pages. Returns {count, results, next_offset}."""
        results: list[Any] = []
        count = None
        cur = offset
        while len(results) < limit:
            page = min(max_page, limit - len(results))
            data = await self.request("GET", path, params={**params, "limit": page, "offset": cur}, auth=auth)
            batch = data.get("results", [])
            count = data.get("count", count)
            results.extend(batch)
            cur += len(batch)
            if len(batch) < page or (count is not None and cur >= count):
                break
        next_offset = cur if count is not None and cur < count else None
        return {"count": count, "results": results, "next_offset": next_offset}
