"""Append-only trade / control ledger."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Deque, Literal


LedgerKind = Literal["order", "fill", "control", "filter", "system"]


@dataclass
class LedgerEntry:
    id: str
    ts: str
    kind: LedgerKind
    ticker: str | None = None
    side: str | None = None
    qty: int | None = None
    price_cents: int | None = None
    mode: str | None = None
    status: str = "info"
    realized_delta_cents: int | None = None
    message: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


class TradeLedger:
    def __init__(self, max_entries: int = 5_000) -> None:
        self._lock = Lock()
        self._entries: Deque[LedgerEntry] = deque(maxlen=max_entries)
        self._seq = 0

    def append(self, **kwargs: Any) -> LedgerEntry:
        with self._lock:
            self._seq += 1
            entry = LedgerEntry(
                id=kwargs.pop("id", f"led-{self._seq}"),
                ts=kwargs.pop("ts", datetime.now(timezone.utc).isoformat()),
                kind=kwargs.pop("kind", "system"),
                **kwargs,
            )
            self._entries.append(entry)
            return entry

    def list(
        self,
        *,
        limit: int = 200,
        kind: str | None = None,
        ticker: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._entries)
        items.reverse()
        out: list[dict[str, Any]] = []
        for e in items:
            if kind and e.kind != kind:
                continue
            if ticker and e.ticker != ticker:
                continue
            out.append(asdict(e))
            if len(out) >= limit:
                break
        return out
