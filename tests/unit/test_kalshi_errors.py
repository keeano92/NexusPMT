"""TDD: Kalshi API error parsing + soft vs hard reject classification."""

import httpx

from backend.app.kalshi.client import KalshiAPIError, classify_kalshi_error, is_soft_order_reject


def _http_error(status: int, body: dict | str) -> httpx.HTTPStatusError:
    content = body if isinstance(body, (bytes, bytearray)) else (
        __import__("json").dumps(body).encode() if isinstance(body, dict) else str(body).encode()
    )
    req = httpx.Request("POST", "https://example.test/trade-api/v2/portfolio/events/orders")
    resp = httpx.Response(status, request=req, content=content)
    return httpx.HTTPStatusError(f"{status}", request=req, response=resp)


def test_kalshi_api_error_from_http_parses_json_code():
    err = KalshiAPIError.from_http(_http_error(400, {"code": "insufficient_balance", "message": "not enough"}))
    assert err.status_code == 400
    assert err.code == "insufficient_balance"
    assert "not enough" in err.message
    assert is_soft_order_reject(err) is True


def test_insufficient_balance_is_soft():
    err = KalshiAPIError(status_code=400, code="insufficient_balance", message="no cash")
    assert is_soft_order_reject(err) is True
    assert classify_kalshi_error(err) == "soft"


def test_auth_401_is_hard():
    err = KalshiAPIError.from_http(_http_error(401, {"code": "unauthorized", "message": "bad key"}))
    assert is_soft_order_reject(err) is False
    assert classify_kalshi_error(err) == "hard"


def test_rate_limit_429_is_soft():
    err = KalshiAPIError.from_http(_http_error(429, {"code": "rate_limit", "message": "slow down"}))
    assert is_soft_order_reject(err) is True
    assert classify_kalshi_error(err) == "soft"


def test_market_404_is_soft():
    err = KalshiAPIError.from_http(_http_error(404, {"code": "not_found", "message": "gone"}))
    assert is_soft_order_reject(err) is True


def test_generic_exception_classified_hard():
    assert classify_kalshi_error(RuntimeError("boom")) == "hard"


def test_build_v2_order_includes_exchange_index():
    from backend.app.kalshi.client import KalshiClient

    body = KalshiClient.build_v2_order(
        ticker="KXBTCD-1",
        side="yes",
        count=1,
        yes_price_cents=50,
        client_order_id="11111111-1111-1111-1111-111111111111",
        exchange_index=2,
    )
    assert body["exchange_index"] == 2
    assert body["ticker"] == "KXBTCD-1"


def test_build_v2_order_omits_exchange_index_when_none():
    from backend.app.kalshi.client import KalshiClient

    body = KalshiClient.build_v2_order(
        ticker="FED-1",
        side="yes",
        count=1,
        yes_price_cents=50,
        client_order_id="11111111-1111-1111-1111-111111111111",
        exchange_index=None,
    )
    assert "exchange_index" not in body
