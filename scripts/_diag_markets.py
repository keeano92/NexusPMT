import asyncio
import json

from backend.app.config import get_settings
from backend.app.kalshi.client import KalshiClient
from backend.app.kalshi.market_filter import MarketFilter
from backend.app.workers.edge_engine import _liquidity, _mid_prob, _spread
from backend.app.workers.opportunity_eval import collect_candidate_markets


async def main() -> None:
    s = get_settings()
    c = KalshiClient(
        api_key_id=s.kalshi_key_id,
        private_key_pem=s.private_key_bytes(),
        base_url=s.kalshi_base_url,
    )
    markets = (await c.list_markets(status="open", limit=5)).get("markets") or []
    print("sample_n", len(markets))
    if markets:
        sample = markets[0]
        print("keys", sorted(sample.keys())[:50])
        print(
            "mid/spread/liq",
            _mid_prob(sample),
            _spread(sample),
            _liquidity(sample),
        )
        print(
            json.dumps(
                {
                    k: sample.get(k)
                    for k in [
                        "ticker",
                        "event_ticker",
                        "series_ticker",
                        "title",
                        "yes_sub_title",
                        "yes_bid_dollars",
                        "yes_ask_dollars",
                        "yes_bid",
                        "yes_ask",
                        "last_price_dollars",
                        "volume_fp",
                        "category",
                    ]
                },
                indent=2,
            )
        )

    mf = MarketFilter.from_settings(s.allowlist, s.blocklist, strict=True)
    series_by = {}
    cats = [x.strip() for x in s.kalshi_category_allowlist.split(",") if x.strip()]
    for cat in cats[:8]:
        r = await c.list_series(category=cat)
        print("cat", cat, "series", len(r.get("series") or []))
        for ser in r.get("series") or []:
            series_by[str(ser.get("ticker"))] = ser
    allm = []
    seen = set()
    for st in list(series_by.keys())[:30]:
        try:
            r = await c.list_markets(status="open", series_ticker=st, limit=20, mve_filter="exclude")
        except Exception:
            r = await c.list_markets(status="open", series_ticker=st, limit=20)
        for m in r.get("markets") or []:
            t = str(m.get("ticker") or "")
            if t and t not in seen:
                if not m.get("series_ticker"):
                    m = {**m, "series_ticker": st}
                seen.add(t)
                allm.append(m)
    hit = sum(1 for x in allm if str(x.get("series_ticker") or "") in series_by)
    print("markets", len(allm), "series_map", len(series_by), "hit", hit)

    drops = {"no_series_reject": 0, "with_series_reject": 0, "mid": 0, "spread": 0, "ok": 0}
    for market in allm:
        series = series_by.get(str(market.get("series_ticker") or ""))
        if series is None:
            if not mf.allow_market(market, None):
                drops["no_series_reject"] += 1
                continue
        elif not mf.allow_market(market, series):
            drops["with_series_reject"] += 1
            continue
        mid = _mid_prob(market)
        if mid is None or mid <= 0.02 or mid >= 0.98:
            drops["mid"] += 1
            continue
        spread = _spread(market)
        if spread is not None and spread > s.kalshi_max_spread:
            drops["spread"] += 1
            continue
        drops["ok"] += 1
    print("drops", drops)
    cand = collect_candidate_markets(
        allm, series_by, mf, max_spread=0.25, min_liquidity=0, limit=20
    )
    print("candidates", len(cand), [c["ticker"] for c in cand[:8]])
    await c.aclose()


if __name__ == "__main__":
    asyncio.run(main())
