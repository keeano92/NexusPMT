"""Fundamentals-only market filter — sports and entertainment blocked."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

SPORTS_CATEGORY_NAMES = {
    "sports",
    "sport",
    "entertainment",
}

SPORTS_KEYWORDS = re.compile(
    r"\b("
    r"nba|nfl|mlb|nhl|mls|ncaa|ufc|mma|wwe|"
    r"soccer|football|basketball|baseball|hockey|tennis|golf|"
    r"premier\s*league|la\s*liga|serie\s*a|bundesliga|champions\s*league|"
    r"world\s*cup|super\s*bowl|march\s*madness|playoffs?|"
    r"touchdown|home\s*run|knockout|odds|parlay|moneyline|spread|"
    r"espn|fantasy\s*sports?"
    r")\b",
    re.IGNORECASE,
)

FUNDAMENTAL_DOMAINS = {
    "geopolitics",
    "economics",
    "energy",
    "climate",
    "tech",
    "crypto_macro",
    "politics",
    "trade",
}


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def is_sports_market(
    *,
    category: str | None = None,
    tags: Iterable[str] | None = None,
    title: str | None = None,
    ticker: str | None = None,
) -> bool:
    cat = _norm(category)
    if cat in SPORTS_CATEGORY_NAMES or "sport" in cat:
        return True

    for tag in tags or []:
        t = _norm(tag)
        if t in SPORTS_CATEGORY_NAMES or "sport" in t:
            return True
        if SPORTS_KEYWORDS.search(t):
            return True

    blob = f"{title or ''} {ticker or ''}"
    if SPORTS_KEYWORDS.search(blob):
        return True
    return False


@dataclass
class MarketFilter:
    allowlist: set[str]
    blocklist: set[str]
    strict: bool = True

    @classmethod
    def from_settings(
        cls,
        allowlist: Iterable[str],
        blocklist: Iterable[str],
        *,
        strict: bool = True,
    ) -> MarketFilter:
        return cls(
            allowlist={_norm(x) for x in allowlist if _norm(x)},
            blocklist={_norm(x) for x in blocklist if _norm(x)},
            strict=strict,
        )

    def category_allowed(self, category: str | None) -> bool:
        cat = _norm(category)
        if not cat:
            return not self.strict
        if cat in self.blocklist or "sport" in cat:
            return False
        if self.allowlist and cat not in self.allowlist:
            # Allow partial match e.g. "Climate and Weather" vs allowlist entry
            if not any(a in cat or cat in a for a in self.allowlist):
                return False
        return True

    def allow_series(self, series: dict[str, Any]) -> bool:
        category = series.get("category")
        tags = series.get("tags") or []
        title = series.get("title")
        ticker = series.get("ticker")
        if self.strict and is_sports_market(
            category=category, tags=tags, title=title, ticker=ticker
        ):
            return False
        if not self.category_allowed(category if isinstance(category, str) else None):
            return False
        return True

    def allow_market(self, market: dict[str, Any], series: dict[str, Any] | None = None) -> bool:
        if series and not self.allow_series(series):
            return False
        category = (series or {}).get("category") or market.get("category")
        tags = (series or {}).get("tags") or market.get("tags") or []
        title = market.get("title") or market.get("subtitle") or market.get("yes_sub_title")
        ticker = market.get("ticker") or market.get("event_ticker")
        if self.strict and is_sports_market(
            category=category if isinstance(category, str) else None,
            tags=tags,
            title=title if isinstance(title, str) else None,
            ticker=ticker if isinstance(ticker, str) else None,
        ):
            return False
        if category and not self.category_allowed(str(category)):
            return False
        return True

    def allow_wheel_domain(self, domain: str | None) -> bool:
        d = _norm(domain)
        if not d:
            return False
        if d in {"sports", "entertainment", "odds"}:
            return False
        return d in FUNDAMENTAL_DOMAINS
