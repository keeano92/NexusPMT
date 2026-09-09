from backend.app.kalshi.market_filter import MarketFilter
from backend.app.workers.futures_wheel import FuturesWheelEngine, _extract_json


def test_extract_json_from_fenced_block():
    raw = """```json
{"nodes":[{"order":1,"title":"Fed cut","domain":"economics","direction":"up","confidence":0.7,"kalshi_keywords":["fed"],"evidence_refs":[]}]}
```"""
    data = _extract_json(raw)
    assert "nodes" in data
    assert data["nodes"][0]["title"] == "Fed cut"


def test_fallback_when_empty_nodes(monkeypatch):
    mf = MarketFilter.from_settings(["economics"], ["sports"], strict=True)
    eng = FuturesWheelEngine(api_key="missing", base_url="https://api.x.ai/v1", model="grok-4.6", market_filter=mf)
    result = eng._fallback("test query", ["intel a"])
    assert len(result.nodes) >= 3
    assert all(n.domain for n in result.nodes)
