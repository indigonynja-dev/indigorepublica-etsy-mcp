"""Shared fake Etsy for the end-to-end tests: httpx.MockTransport handlers only, no network, no real keys.

ShopFake      the OAuth-authenticated shop API (FakeEtsy + SeoFake routes) plus what the end-to-end tests need on top:
              a paginated active-listings route, DELETE routes, a request log, and fault injection (429s, 403s...).
FullFake      everything the server can call: ShopFake for bearer-token requests, FakePublicEtsy (competitor lookups)
              for key-only requests, routed the same way the real client sends them.
"""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import parse_qs

import httpx

from .test_competitors import FakePublicEtsy, _raw
from .test_seo import SeoFake
from .test_server import SHOP

SHOP_PATH = f"/v3/application/shops/{SHOP}"
LISTINGS_PATH = f"{SHOP_PATH}/listings"
SECTIONS_PATH = f"{SHOP_PATH}/sections"


def listing_row(i: int, ts: int = 1_760_000_000, state: str = "active") -> dict[str, Any]:
    return {"listing_id": i, "title": f"Listing {i}", "state": state, "tags": ["x"], "views": 5,
            "price": {"amount": 100 * i + 99, "divisor": 100, "currency_code": "USD"}, "last_modified_timestamp": ts}


class ShopFake(SeoFake):
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.rows = [listing_row(1, 1_760_000_000), listing_row(2, 1_770_000_000)] if rows is None else rows
        self.seen: list[tuple[str, str]] = []  # every request that reached the fake, faulted or not: (method, path)
        self._faults: list[dict[str, Any]] = []

    # ---- inspection
    def count(self, method: str, path: str) -> int:
        return sum(1 for m, p in self.seen if (m, p) == (method, path))

    @property
    def methods(self) -> set[str]:
        return {m for m, _ in self.seen}

    # ---- fault injection
    def fault(self, method: str, path: str, status: int, *, times: int | None = None, headers: dict[str, str] | None = None,
              json: Any = None) -> None:
        """Answer `method path` with `status` the next `times` requests (forever when None)."""
        self._faults.append({"method": method, "path": path, "left": times, "status": status, "headers": headers or {},
                             "json": {"error": f"injected {status}"} if json is None else json})

    def fault_raises(self, method: str, path: str, make_error: Callable[[], Exception], *, times: int | None = None) -> None:
        """Make `method path` raise a transport error (ConnectError, ReadTimeout...) the next `times` requests (forever when None)."""
        self._faults.append({"method": method, "path": path, "left": times, "raises": make_error})

    def clear_faults(self) -> None:
        self._faults.clear()

    def __call__(self, req: httpx.Request) -> httpx.Response:
        m, p = req.method, req.url.path
        self.seen.append((m, p))
        for f in self._faults:
            if (f["method"], f["path"]) == (m, p) and (f["left"] is None or f["left"] > 0):
                if f["left"] is not None:
                    f["left"] -= 1
                if "raises" in f:
                    raise f["raises"]()
                return httpx.Response(f["status"], headers=f["headers"], json=f["json"])
        if m == "DELETE" and (p.startswith("/v3/application/listings/") or p.startswith(f"{LISTINGS_PATH}/")):
            return httpx.Response(204)
        if m == "POST" and p == SECTIONS_PATH:
            return httpx.Response(201, json={"shop_section_id": 7, "title": parse_qs(req.read().decode()).get("title", [""])[0], "active_listing_count": 0})
        if m == "GET" and p == LISTINGS_PATH:
            assert req.headers["x-api-key"] == "KEY:SECRET" and req.headers["authorization"].startswith("Bearer 77.")
            q = dict(req.url.params)
            assert q["state"] == "active" and int(q["limit"]) <= 100
            lim, off = int(q["limit"]), int(q["offset"])
            return httpx.Response(200, json={"count": len(self.rows), "results": self.rows[off:off + lim]})
        return super().__call__(req)


class FullFake:
    """Bearer-token requests go to ShopFake; key-only requests (competitor lookups, auth=False) go to FakePublicEtsy."""

    def __init__(self, competitor_listings: dict[int, list[dict[str, Any]] | None] | None = None, rows: list[dict[str, Any]] | None = None) -> None:
        self.shop = ShopFake(rows)
        self.public = FakePublicEtsy(competitor_listings if competitor_listings is not None else
                                     {11: [_raw(i, tags=("planner", "budget") if i % 2 else ("planner",), cents=500 + i) for i in range(1, 5)], 22: []})

    def __call__(self, req: httpx.Request) -> httpx.Response:
        return (self.shop if "authorization" in req.headers else self.public)(req)

    @property
    def methods(self) -> set[str]:
        """HTTP methods of every request that reached Etsy, public and authenticated."""
        return self.shop.methods | {r.method for r in self.public.calls}
