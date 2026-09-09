"""Stop-loss / flip decision math for live position manager."""

from backend.app.kalshi.client import KalshiClient
from backend.app.workers.position_manager import (
    PositionDecision,
    decide_position_action,
    build_close_v2_order,
)


def test_stop_loss_yes_when_mid_drops():
    d = decide_position_action(
        side="yes",
        entry_yes_prob=0.40,
        mark_yes_prob=0.28,
        stop_loss_prob=0.10,
        take_profit_prob=0.15,
        flip_side=None,
        flip_min_edge=0.05,
        seconds_to_close=600,
        time_stop_sec=90,
    )
    assert d.action == "stop_loss"
    assert d.close_side == "yes"  # sell the YES we hold


def test_flip_no_when_yes_mid_rises_and_reeval_prefers_yes():
    # Held NO when YES was 0.57; YES mid → 0.68 means NO mark fell ~11¢
    d = decide_position_action(
        side="no",
        entry_yes_prob=0.57,
        mark_yes_prob=0.68,
        stop_loss_prob=0.10,
        take_profit_prob=0.15,
        flip_side="yes",
        flip_min_edge=0.05,
        seconds_to_close=600,
        time_stop_sec=90,
    )
    assert d.action == "flip"
    assert d.flip_to == "yes"


def test_take_profit_yes():
    d = decide_position_action(
        side="yes",
        entry_yes_prob=0.40,
        mark_yes_prob=0.58,
        stop_loss_prob=0.10,
        take_profit_prob=0.15,
        flip_side=None,
        flip_min_edge=0.05,
        seconds_to_close=600,
        time_stop_sec=90,
    )
    assert d.action == "take_profit"


def test_hold_when_inside_bands():
    d = decide_position_action(
        side="yes",
        entry_yes_prob=0.50,
        mark_yes_prob=0.52,
        stop_loss_prob=0.10,
        take_profit_prob=0.15,
        flip_side=None,
        flip_min_edge=0.05,
        seconds_to_close=600,
        time_stop_sec=90,
    )
    assert d.action == "hold"


def test_time_stop_when_underwater_near_expiry():
    d = decide_position_action(
        side="yes",
        entry_yes_prob=0.50,
        mark_yes_prob=0.45,
        stop_loss_prob=0.10,
        take_profit_prob=0.15,
        flip_side=None,
        flip_min_edge=0.05,
        seconds_to_close=60,
        time_stop_sec=90,
    )
    assert d.action == "time_stop"


def test_max_hold_forces_exit_for_paper():
    d = decide_position_action(
        side="yes",
        entry_yes_prob=0.37,
        mark_yes_prob=0.375,
        stop_loss_prob=0.18,
        take_profit_prob=0.20,
        flip_side=None,
        flip_min_edge=0.20,
        seconds_to_close=3600,
        time_stop_sec=120,
        held_sec=901,
        max_hold_sec=900,
    )
    assert d.action == "time_stop"


def test_build_close_sells_yes_with_ask():
    body = build_close_v2_order(
        ticker="KXSOL15M-1",
        position_side="yes",
        count=5,
        mark_yes_prob=0.40,
        client_order_id="11111111-1111-1111-1111-111111111111",
        exchange_index=2,
    )
    # Selling YES → ask on YES book
    assert body["side"] == "ask"
    assert body["exchange_index"] == 2
    assert body["count"] == "5.00"


def test_build_close_covers_no_with_bid():
    body = build_close_v2_order(
        ticker="KXSOL15M-1",
        position_side="no",
        count=5,
        mark_yes_prob=0.60,
        client_order_id="11111111-1111-1111-1111-111111111111",
        exchange_index=2,
    )
    # Covering NO = buy YES → bid
    assert body["side"] == "bid"
