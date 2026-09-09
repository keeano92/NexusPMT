"""Async Kalshi Trade API v2 client."""

from __future__ import annotations

from typing import Any

import httpx

from backend.app.kalshi.auth import build_auth_headers, load_private_key


class KalshiClient:
    def __init__(
        self,
        *,
        api_key_id: str,
        private_key_pem: bytes,
        base_url: str,
        timeout: float = 30.0,
    ) -> None:
        self.api_key_id = api_key_id
        self.base_url = base_url.rstrip("/")
        self._key = load_private_key(private_key_pem)
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self, method: str, relative_path: str) -> dict[str, str]:
        return dict(
            build_auth_headers(
                api_key_id=self.api_key_id,
                private_key=self._key,
                method=method,
                base_url=self.base_url,
                relative_path=relative_path,
            )
        )

    async def request(
        self,
        method: str,
        relative_path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> Any:
        rel = relative_path if relative_path.startswith("/") else f"/{relative_path}"
        url = f"{self.base_url}{rel}"
        headers = self._headers(method, rel) if auth else {"Accept": "application/json"}
        resp = await self._client.request(method, url, headers=headers, params=params, json=json)
        resp.raise_for_status()
        if resp.content:
            return resp.json()
        return None

    async def get_balance(self) -> dict[str, Any]:
        return await self.request("GET", "/portfolio/balance")

    async def get_positions(self) -> dict[str, Any]:
        return await self.request("GET", "/portfolio/positions")

    async def get_orders(self, **params: Any) -> dict[str, Any]:
        return await self.request("GET", "/portfolio/orders", params=params or None)

    async def list_series(self, **params: Any) -> dict[str, Any]:
        # Public market data often works without auth; still sign if we have keys
        return await self.request("GET", "/series", params=params or None, auth=bool(self.api_key_id))

    async def list_markets(self, **params: Any) -> dict[str, Any]:
        return await self.request("GET", "/markets", params=params or None, auth=bool(self.api_key_id))

    async def create_order(self, body: dict[str, Any]) -> dict[str, Any]:
        """Place order via Create Order V2 (`/portfolio/events/orders`).

        Legacy `/portfolio/orders` returns HTTP 410 Gone.
        """
        return await self.request("POST", "/portfolio/events/orders", json=body)

    @staticmethod
    def build_v2_order(
        *,
        ticker: str,
        side: str,
        count: int,
        yes_price_cents: int,
        client_order_id: str,
    ) -> dict[str, Any]:
        """Map yes/no buy intent → V2 bid/ask + fixed-point dollar price."""
        # V2 quotes the YES book only: bid=buy YES, ask=sell YES (= buy NO).
        side_l = (side or "yes").lower()
        px = max(1, min(99, int(yes_price_cents)))
        if side_l == "no":
            # Buying NO at (100-px)¢ YES-equivalent → ask YES at px
            book_side = "ask"
            yes_px = px  # YES limit when selling YES / buying NO
        else:
            book_side = "bid"
            yes_px = px
        return {
            "ticker": ticker,
            "side": book_side,
            "count": f"{int(count)}.00",
            "price": f"{yes_px / 100:.4f}",
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": client_order_id,
        }
