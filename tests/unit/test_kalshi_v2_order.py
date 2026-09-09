from backend.app.kalshi.client import KalshiClient


def test_build_v2_buy_yes():
    body = KalshiClient.build_v2_order(
        ticker="FED-1",
        side="yes",
        count=3,
        yes_price_cents=42,
        client_order_id="11111111-1111-1111-1111-111111111111",
    )
    assert body["side"] == "bid"
    assert body["price"] == "0.4200"
    assert body["count"] == "3.00"


def test_build_v2_buy_no_is_ask_yes():
    body = KalshiClient.build_v2_order(
        ticker="FED-1",
        side="no",
        count=2,
        yes_price_cents=42,
        client_order_id="22222222-2222-2222-2222-222222222222",
    )
    assert body["side"] == "ask"
    assert body["price"] == "0.4200"
