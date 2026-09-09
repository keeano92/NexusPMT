"""Local-timezone timestamps for ledger/UI."""

from __future__ import annotations

from datetime import datetime


def local_now() -> datetime:
    return datetime.now().astimezone()


def local_iso() -> str:
    """ISO-8601 with local offset, e.g. 2026-09-09T03:30:00-04:00."""
    return local_now().isoformat(timespec="seconds")


def local_clock() -> str:
    """HH:MM:SS in local time for terminal lines."""
    return local_now().strftime("%H:%M:%S")


def format_ledger_ts(ts: str | None) -> str:
    """Render any ISO/ts string in local timezone for display."""
    if not ts:
        return ""
    try:
        raw = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.astimezone()  # assume already local-naive
        else:
            dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)[:19].replace("T", " ")
