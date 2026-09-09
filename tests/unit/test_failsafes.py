from backend.app.risk.failsafes import FailSafeController, FailSafeState


def test_kill_blocks_orders():
    fs = FailSafeController()
    assert fs.can_place_orders()
    fs.kill(actor="op")
    assert fs.state == FailSafeState.KILLED
    assert not fs.can_place_orders()


def test_resume_blocked_while_killed():
    fs = FailSafeController()
    fs.kill()
    assert fs.resume() == FailSafeState.KILLED
    assert fs.clear_kill() == FailSafeState.PAUSED
    assert fs.resume() == FailSafeState.RUNNING


def test_auto_kill_on_errors():
    fs = FailSafeController(auto_kill_on_errors=3)
    fs.record_api_error()
    fs.record_api_error()
    assert fs.state == FailSafeState.RUNNING
    fs.record_api_error()
    assert fs.state == FailSafeState.KILLED


def test_daily_loss_circuit_breaker():
    fs = FailSafeController(max_daily_loss_cents=1000)
    fs.update_equity(10_000)
    fs.update_equity(8_500)  # -1500
    assert fs.state == FailSafeState.KILLED
