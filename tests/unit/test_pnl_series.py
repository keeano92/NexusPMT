from backend.app.store.pnl_series import PnLSeriesStore


def test_horizons_change():
    store = PnLSeriesStore()
    now = 1_700_000_000.0
    store.record(100_000, ts=now)
    store.record(101_000, ts=now + 30)
    store.record(102_000, ts=now + 90)
    rt = store.series("rt", max_points=50)
    # Force window relative to last mark by monkeypatching _now via direct series with points already filtered
    # series() uses wall clock; inject by recording recent stamps near "now"
    store2 = PnLSeriesStore()
    import time

    t = time.time()
    store2.record(100_000, ts=t - 10)
    store2.record(110_000, ts=t)
    s = store2.series("5m")
    assert s["last_cents"] == 110_000
    assert s["change_cents"] == 10_000
    assert s["daily_pnl_cents"] == 10_000
