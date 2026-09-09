"""SK AI WorldMap HTTP client (internal Docker network)."""

from __future__ import annotations

from typing import Any

import httpx


class WorldMapClient:
    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return f"{self.base_url}{path if path.startswith('/') else '/' + path}"

    async def sidecar_health(self) -> tuple[int, Any]:
        resp = await self._client.get(self._url("/api/sidecar-health"))
        try:
            body: Any = resp.json()
        except Exception:
            body = resp.text
        return resp.status_code, body

    async def health_compact(self) -> dict[str, Any]:
        resp = await self._client.get(self._url("/api/health"), params={"compact": "1"})
        resp.raise_for_status()
        return resp.json()

    async def bootstrap(self) -> dict[str, Any]:
        resp = await self._client.get(self._url("/api/bootstrap"))
        resp.raise_for_status()
        return resp.json()

    async def future_wheel(self, query: str, horizon_hours: float = 24 * 7) -> dict[str, Any]:
        resp = await self._client.post(
            self._url("/api/analyst/future-wheel"),
            json={"query": query, "horizonHours": horizon_hours},
        )
        resp.raise_for_status()
        return resp.json()

    async def list_prediction_markets(self, **params: Any) -> dict[str, Any]:
        resp = await self._client.get(
            self._url("/api/prediction/v1/list-prediction-markets"),
            params=params or None,
        )
        resp.raise_for_status()
        return resp.json()
