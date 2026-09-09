"""Cheap intel briefs via official search/Gemini APIs — never scrape Gemini AI Mode UI."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

Bias = Literal["yes", "no", "neutral"]


@dataclass
class IntelBrief:
    bias: Bias
    confidence: float
    bullets: list[str]
    provider: str
    cached: bool = False


_CACHE: dict[str, tuple[float, IntelBrief]] = {}
_CACHE_TTL = 600.0


def fetch_intel_brief(
    *,
    title: str,
    series: str,
    provider: str,
    api_key: str = "",
    gemini_api_key: str = "",
    gemini_model: str = "gemini-2.0-flash",
) -> IntelBrief | None:
    """Return a short directional brief or None if provider disabled/unavailable."""
    provider = (provider or "none").lower()
    if provider in {"", "none"}:
        return None
    cache_key = f"{provider}:{series}:{title[:80]}"
    hit = _CACHE.get(cache_key)
    now = time.time()
    if hit and now - hit[0] < _CACHE_TTL:
        b = hit[1]
        return IntelBrief(b.bias, b.confidence, b.bullets, b.provider, cached=True)

    try:
        if provider == "gemini" and gemini_api_key:
            brief = _gemini_brief(title, gemini_api_key, gemini_model)
        elif provider in {"brave", "serp"} and api_key:
            brief = _search_then_heuristic(title, provider, api_key)
        else:
            return None
        if brief:
            _CACHE[cache_key] = (now, brief)
        return brief
    except Exception:
        logger.exception("intel brief failed provider=%s", provider)
        return None


def _gemini_brief(title: str, api_key: str, model: str) -> IntelBrief | None:
    """Official Google Generative Language API (Gemini Flash) — not UI scraping."""
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    prompt = (
        "You are a prediction-market research assistant. "
        f"For the market: {title!r}\n"
        "Reply JSON only: "
        '{"bias":"yes"|"no"|"neutral","confidence":0.0-1.0,"bullets":["...","..."]}. '
        "bias=yes means YES side more likely than market-neutral; be conservative."
    )
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    with httpx.Client(timeout=20.0) as client:
        resp = client.post(url, json=body)
        resp.raise_for_status()
        data = resp.json()
    text = (
        data.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [{}])[0]
        .get("text", "")
    )
    return _parse_brief_json(text, provider="gemini")


def _search_then_heuristic(title: str, provider: str, api_key: str) -> IntelBrief | None:
    snippets: list[str] = []
    if provider == "brave":
        with httpx.Client(timeout=15.0) as client:
            r = client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": f"{title} prediction odds", "count": 5},
                headers={"Accept": "application/json", "X-Subscription-Token": api_key},
            )
            r.raise_for_status()
            for item in (r.json().get("web", {}) or {}).get("results", [])[:5]:
                snippets.append(f"{item.get('title')}: {item.get('description')}")
    elif provider == "serp":
        with httpx.Client(timeout=15.0) as client:
            r = client.get(
                "https://serpapi.com/search.json",
                params={"q": f"{title} prediction", "api_key": api_key, "num": 5},
            )
            r.raise_for_status()
            for item in r.json().get("organic_results", [])[:5]:
                snippets.append(f"{item.get('title')}: {item.get('snippet')}")
    if not snippets:
        return IntelBrief("neutral", 0.2, [], provider=provider)
    blob = " ".join(snippets).lower()
    yes_hits = sum(1 for w in ("surge", "rally", "higher", "beats", "hotter", "wins") if w in blob)
    no_hits = sum(1 for w in ("falls", "cooler", "miss", "lower", "drop", "weak") if w in blob)
    if yes_hits > no_hits + 1:
        return IntelBrief("yes", min(0.7, 0.3 + 0.1 * yes_hits), snippets[:3], provider)
    if no_hits > yes_hits + 1:
        return IntelBrief("no", min(0.7, 0.3 + 0.1 * no_hits), snippets[:3], provider)
    return IntelBrief("neutral", 0.25, snippets[:3], provider)


def _parse_brief_json(text: str, provider: str) -> IntelBrief | None:
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    data = json.loads(text[start : end + 1])
    bias = str(data.get("bias") or "neutral").lower()
    if bias not in {"yes", "no", "neutral"}:
        bias = "neutral"
    conf = float(data.get("confidence") or 0.3)
    bullets = [str(b) for b in (data.get("bullets") or [])][:5]
    return IntelBrief(bias, max(0.0, min(1.0, conf)), bullets, provider)  # type: ignore[arg-type]
