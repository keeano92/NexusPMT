"""Async Kalshi Trade API v2 client."""

from __future__ import annotations

from typing import Any, Literal

import httpx

from backend.app.kalshi.auth import build_auth_headers, load_private_key

# Business / market rejects that must NOT trip error_streak auto-kill.
_SOFT_CODES = {
    "insufficient_balance",
    "not_enough_balance",
    "market_not_found",
    "not_found",
    "market_closed",
    "invalid_order",
    "order_rejected",
    "rate_limit",
    "too_many_requests",
}
_SOFT_STATUS = {400, 404, 409, 429}


class KalshiAPIError(Exception):
    """Kalshi HTTP error with parsed JSON code/message when available."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str = "",
        message: str = "",
        details: str = "",
        body: Any = None,
    ) -> None:
        self.status_code = int(status_code)
        self.code = (code or "").strip()
        self.message = (message or "").strip()
        self.details = (details or "").strip()
        self.body = body
        super().__init__(self.friendly())

    def friendly(self) -> str:
        bits = [f"Kalshi {self.status_code}"]
        if self.code:
            bits.append(self.code)
        if self.message:
            bits.append(self.message)
        elif self.details:
            bits.append(self.details)
        return " — ".join(bits)[:220]

    @classmethod
    def from_http(cls, exc: httpx.HTTPStatusError) -> KalshiAPIError:
        status = exc.response.status_code if exc.response is not None else 0
        body: Any = None
        code = ""
        message = ""
        details = ""
        try:
            body = exc.response.json()
        except Exception:
            try:
                text = (exc.response.text or "")[:400]
                body = text
                message = text
            except Exception:
                message = str(exc)
        if isinstance(body, dict):
            # Some responses nest under "error"
            err = body.get("error") if isinstance(body.get("error"), dict) else body
            code = str(err.get("code") or err.get("error") or "")
            message = str(err.get("message") or err.get("msg") or "")
            details = str(err.get("details") or "")
        return cls(status_code=status, code=code, message=message, details=details, body=body)


def is_soft_order_reject(exc: BaseException) -> bool:
    """True for expected trading rejects that should not auto-kill the desk."""
    if isinstance(exc, KalshiAPIError):
        code = (exc.code or "").lower()
        if code in _SOFT_CODES or "insufficient" in code or "balance" in code:
            return True
        if exc.status_code in _SOFT_STATUS:
            # 400/404/409/429 without unknown catastrophic code → soft
            if exc.status_code == 400 and code and code not in _SOFT_CODES:
                # Unknown 400 codes: still soft (business reject) — hard only for auth/5xx
                return True
            return True
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        return is_soft_order_reject(KalshiAPIError.from_http(exc))
    text = str(exc).lower()
    if "insufficient_balance" in text or "insufficient balance" in text:
        return True
    if "429" in text and "rate" in text:
        return True
    return False


def classify_kalshi_error(exc: BaseException) -> Literal["soft", "hard"]:
    return "soft" if is_soft_order_reject(exc) else "hard"


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
        if resp.is_error:
            raise KalshiAPIError.from_http(
                httpx.HTTPStatusError(
                    f"{resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
            )
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
        exchange_index: int | None = None,
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
        body: dict[str, Any] = {
            "ticker": ticker,
            "side": book_side,
            "count": f"{int(count)}.00",
            "price": f"{yes_px / 100:.4f}",
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": client_order_id,
        }
        if exchange_index is not None:
            body["exchange_index"] = int(exchange_index)
        return body
