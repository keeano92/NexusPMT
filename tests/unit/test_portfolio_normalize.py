from backend.app.kalshi.portfolio import normalize_balance, normalize_positions


def test_normalize_balance_cents():
    out = normalize_balance({"balance": 12500, "portfolio_value": 3400})
    assert out["cash_cents"] == 12500
    assert out["portfolio_value_cents"] == 3400
    assert out["equity_cents"] == 15900


def test_normalize_positions_filters_zero():
    payload = {
        "market_positions": [
            {
                "ticker": "DEMO-FED",
                "position_fp": "5.00",
                "market_exposure_dollars": "2.50",
                "realized_pnl_dollars": "0.10",
            },
            {
                "ticker": "FLAT",
                "position_fp": "0.00",
                "market_exposure_dollars": "0",
                "realized_pnl_dollars": "0",
            },
        ]
    }
    positions, realized = normalize_positions(payload)
    assert len(positions) == 1
    assert positions[0]["ticker"] == "DEMO-FED"
    assert positions[0]["side"] == "yes"
    assert positions[0]["qty"] == 5
    assert realized == 10
