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

    async def collect_intel_snippets(self) -> list[str]:
        """Gather intel from endpoints that work in self-host open access."""
        snippets: list[str] = []

        try:
            compact = await self.health_compact()
            status = compact.get("status")
            summary = compact.get("summary") or {}
            snippets.append(
                f"WorldMap health={status} ok={summary.get('ok')} warn={summary.get('warn')} crit={summary.get('crit')}"
            )
            problems = compact.get("problems") or {}
            for name, meta in list(problems.items())[:12]:
                snippets.append(f"problem:{name}={meta}")
        except Exception as exc:
            snippets.append(f"health_compact unavailable: {exc}")

        try:
            markets = await self.list_prediction_markets(page_size=25)
            for m in (markets.get("markets") or [])[:20]:
                title = m.get("title") or m.get("id")
                cat = m.get("category")
                snippets.append(f"prediction_market[{cat}]: {title}")
        except Exception as exc:
            snippets.append(f"prediction markets unavailable: {exc}")

        try:
            resp = await self._client.get(self._url("/api/operator-me"))
            if resp.status_code == 200:
                me = resp.json()
                snippets.append(f"operator={me.get('email') or me.get('name') or 'local'}")
        except Exception:
            pass

        try:
            boot = await self.bootstrap()
            if isinstance(boot, dict):
                for key, val in list(boot.items())[:30]:
                    snippets.append(f"bootstrap:{key}: {str(val)[:160]}")
        except Exception as exc:
            # Bootstrap often requires a Pro/user API key from non-browser callers.
            snippets.append(f"bootstrap gated ({exc}); using health+prediction intel")

        return snippets