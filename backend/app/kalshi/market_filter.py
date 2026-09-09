"""Market filter — everything allowed except sports/entertainment."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Literal

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
    r"touchdown|home\s*run|knockout|parlay|moneyline|"
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
    "science",
    "companies",
    "world",
    "other",
}

FilterMode = Literal["blocklist", "allowlist"]


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
    if cat in SPORTS_CATEGORY_NAMES or cat.startswith("sport"):
        return True

    for tag in tags or []:
        t = _norm(tag)
        if t in SPORTS_CATEGORY_NAMES or t.startswith("sport"):
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
    mode: FilterMode = "blocklist"  # everything except sports by default

    @classmethod
    def from_settings(
        cls,
        allowlist: Iterable[str],
        blocklist: Iterable[str],
        *,
        strict: bool = True,
        mode: FilterMode = "blocklist",
    ) -> MarketFilter:
        return cls(
            allowlist={_norm(x) for x in allowlist if _norm(x)},
            blocklist={_norm(x) for x in blocklist if _norm(x)} | set(SPORTS_CATEGORY_NAMES),
            strict=strict,
            mode=mode,
        )

    def category_allowed(self, category: str | None) -> bool:
        cat = _norm(category)
        if not cat:
            # Unknown category OK in blocklist mode (still sports-keyword checked)
            return True if self.mode == "blocklist" else (not self.strict)
        if cat in self.blocklist or cat.startswith("sport"):
            return False
        if self.mode == "allowlist" and self.allowlist:
            if cat not in self.allowlist and not any(a in cat or cat in a for a in self.allowlist):
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
        if d in {"sports", "entertainment", "odds"} or d.startswith("sport"):
            return False
        if self.mode == "blocklist":
            return True
        return d in FUNDAMENTAL_DOMAINS
