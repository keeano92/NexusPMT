"""Manual and automatic trading fail-safes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from threading import Lock
from typing import Any


class FailSafeState(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    KILLED = "killed"


@dataclass
class AuditEvent:
    ts: str
    action: str
    actor: str
    detail: dict[str, Any] = field(default_factory=dict)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FailSafeController:
    """Thread-safe gate for autonomous order placement."""

    def __init__(
        self,
        *,
        max_daily_loss_cents: int = 5000,
        max_drawdown_pct: float = 15.0,
        auto_kill_on_errors: int = 5,
    ) -> None:
        self._lock = Lock()
        self.state = FailSafeState.RUNNING
        self.max_daily_loss_cents = max_daily_loss_cents
        self.max_drawdown_pct = max_drawdown_pct
        self.auto_kill_on_errors = auto_kill_on_errors
        self._error_streak = 0
        self._peak_equity_cents: int | None = None
        self._day_start_equity_cents: int | None = None
        self.audit: list[AuditEvent] = []
        self.pending_approvals: list[dict[str, Any]] = []

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self.state.value,
                "can_place_orders": self._can_place_unlocked(),
                "error_streak": self._error_streak,
                "pending_approvals": list(self.pending_approvals),
                "audit_tail": [e.__dict__ for e in self.audit[-20:]],
            }

    def _can_place_unlocked(self) -> bool:
        return self.state == FailSafeState.RUNNING

    def can_place_orders(self) -> bool:
        with self._lock:
            return self._can_place_unlocked()

    def _audit(self, action: str, actor: str, **detail: Any) -> None:
        self.audit.append(AuditEvent(ts=_now(), action=action, actor=actor, detail=detail))

    def kill(self, actor: str = "operator", reason: str = "") -> FailSafeState:
        with self._lock:
            self.state = FailSafeState.KILLED
            self._audit("kill", actor, reason=reason)
            return self.state

    def pause(self, actor: str = "operator") -> FailSafeState:
        with self._lock:
            if self.state == FailSafeState.KILLED:
                self._audit("pause_ignored", actor, reason="killed")
                return self.state
            self.state = FailSafeState.PAUSED
            self._audit("pause", actor)
            return self.state

    def resume(self, actor: str = "operator") -> FailSafeState:
        with self._lock:
            if self.state == FailSafeState.KILLED:
                self._audit("resume_blocked", actor, reason="clear_kill_first")
                return self.state
            self.state = FailSafeState.RUNNING
            self._audit("resume", actor)
            return self.state

    def clear_kill(self, actor: str = "operator") -> FailSafeState:
        """Move from KILLED to PAUSED; requires explicit resume to trade again."""
        with self._lock:
            if self.state != FailSafeState.KILLED:
                return self.state
            self.state = FailSafeState.PAUSED
            self._error_streak = 0
            self._audit("clear_kill", actor)
            return self.state

    def record_api_error(self, actor: str = "system") -> FailSafeState:
        with self._lock:
            self._error_streak += 1
            self._audit("api_error", actor, streak=self._error_streak)
            if self._error_streak >= self.auto_kill_on_errors:
                self.state = FailSafeState.KILLED
                self._audit("auto_kill", "circuit_breaker", reason="error_streak")
            return self.state

    def record_api_success(self) -> None:
        with self._lock:
            self._error_streak = 0

    def update_equity(self, equity_cents: int, actor: str = "system") -> FailSafeState | None:
        """Update equity marks; may auto-kill on loss/drawdown."""
        with self._lock:
            if self._day_start_equity_cents is None:
                self._day_start_equity_cents = equity_cents
            if self._peak_equity_cents is None or equity_cents > self._peak_equity_cents:
                self._peak_equity_cents = equity_cents

            daily_pnl = equity_cents - self._day_start_equity_cents
            if daily_pnl <= -abs(self.max_daily_loss_cents):
                self.state = FailSafeState.KILLED
                self._audit(
                    "auto_kill",
                    actor,
                    reason="max_daily_loss",
                    daily_pnl_cents=daily_pnl,
                )
                return self.state

            peak = self._peak_equity_cents or equity_cents
            if peak > 0:
                drawdown_pct = ((peak - equity_cents) / peak) * 100.0
                if drawdown_pct >= self.max_drawdown_pct:
                    self.state = FailSafeState.KILLED
                    self._audit(
                        "auto_kill",
                        actor,
                        reason="max_drawdown",
                        drawdown_pct=drawdown_pct,
                    )
                    return self.state
            return None

    def enqueue_approval(self, order: dict[str, Any]) -> str:
        with self._lock:
            oid = order.get("client_order_id") or f"appr-{len(self.pending_approvals)+1}"
            payload = {**order, "client_order_id": oid, "queued_at": _now()}
            self.pending_approvals.append(payload)
            self._audit("approval_queued", "system", order=payload)
            return oid

    def approve(self, client_order_id: str, actor: str = "operator") -> dict[str, Any] | None:
        with self._lock:
            for i, item in enumerate(self.pending_approvals):
                if item.get("client_order_id") == client_order_id:
                    approved = self.pending_approvals.pop(i)
                    self._audit("approval_granted", actor, order=approved)
                    return approved
            return None

    def reject(self, client_order_id: str, actor: str = "operator") -> dict[str, Any] | None:
        with self._lock:
            for i, item in enumerate(self.pending_approvals):
                if item.get("client_order_id") == client_order_id:
                    rejected = self.pending_approvals.pop(i)
                    self._audit("approval_rejected", actor, order=rejected)
                    return rejected
            return None
