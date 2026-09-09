from backend.app.timeutil import format_ledger_ts, local_iso


def test_local_iso_has_offset_or_naive_local():
    ts = local_iso()
    assert "T" in ts
    # Local iso includes offset like -04:00 or +00:00
    assert ("+" in ts[10:] or "-" in ts[10:])


def test_format_ledger_ts_readable():
    out = format_ledger_ts("2026-09-09T15:30:00-04:00")
    assert "2026-09-09" in out
    assert "15:30:00" in out or "3:30" in out.lower()
