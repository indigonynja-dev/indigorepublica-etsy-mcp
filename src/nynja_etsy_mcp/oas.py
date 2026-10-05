"""Search Etsy's published OpenAPI spec so Claude can use etsy_api_request correctly."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

CACHE_TTL_S = 7 * 24 * 3600


class SpecIndex:
    def __init__(self, url: str, cache_file: Path):
        self.url = url
        self.cache_file = cache_file
        self._ops: list[dict[str, Any]] | None = None

    async def _load(self, http: httpx.AsyncClient) -> list[dict[str, Any]]:
        if self._ops is not None:
            return self._ops
        spec: dict[str, Any] | None = None
        if self.cache_file.exists() and time.time() - self.cache_file.stat().st_mtime < CACHE_TTL_S:
            spec = json.loads(self.cache_file.read_text())
        if spec is None:
            r = await http.get(self.url, timeout=60, follow_redirects=True)
            r.raise_for_status()
            spec = r.json()
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps(spec))
        self._ops = self._flatten(spec)
        return self._ops

    @staticmethod
    def _flatten(spec: dict[str, Any]) -> list[dict[str, Any]]:
        ops = []
        for path, item in spec.get("paths", {}).items():
            for method, op in item.items():
                if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                    continue
                params = [
                    {"name": p.get("name"), "in": p.get("in"), "required": p.get("required", False)}
                    for p in op.get("parameters", []) + item.get("parameters", [])
                    if isinstance(p, dict) and "name" in p
                ]
                body_fields: list[str] = []
                required_body: list[str] = []
                content = (op.get("requestBody") or {}).get("content", {})
                for ctype, c in content.items():
                    schema = c.get("schema", {})
                    body_fields = sorted(schema.get("properties", {}).keys())
                    required_body = schema.get("required", [])
                    break
                security = op.get("security") or []
                scopes = sorted({s for sec in security for v in sec.values() for s in v})
                ops.append(
                    {
                        "operation_id": op.get("operationId"),
                        "method": method.upper(),
                        "path": path.replace("/v3/application", "") or path,
                        "summary": (op.get("summary") or op.get("description") or "")[:300],
                        "params": params,
                        "body_fields": body_fields,
                        "required_body": required_body,
                        "body_type": next(iter(content), None),
                        "scopes": scopes,
                        "tags": op.get("tags", []),
                    }
                )
        return ops

    async def search(self, http: httpx.AsyncClient, query: str, limit: int = 8) -> list[dict[str, Any]]:
        ops = await self._load(http)
        terms = [t for t in query.lower().split() if t]

        def score(o: dict[str, Any]) -> int:
            hay = " ".join([o["operation_id"] or "", o["path"], o["summary"], " ".join(o["tags"])]).lower()
            s = sum(3 if t in (o["operation_id"] or "").lower() else 1 for t in terms if t in hay)
            return s

        ranked = sorted((o for o in ops if score(o) > 0), key=score, reverse=True)
        return ranked[:limit]
