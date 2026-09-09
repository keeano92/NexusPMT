from backend.app.config import Settings
from backend.app.state import build_app_state


def test_worldmap_down_blocks_trading_flag():
    s = Settings(WORLDMAP_REQUIRED=True)
    state = build_app_state(s)
    assert state.worldmap_ready is False
    state.set_worldmap_ready(True)
    assert state.worldmap_ready is True
    state.set_worldmap_ready(False, "down")
    assert state.worldmap_ready is False
    assert "WorldMap" in state.worldmap_block_reason or "down" in state.worldmap_block_reason
    assert state.failsafes.snapshot()["state"] in {"paused", "running", "killed"}
