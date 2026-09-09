from backend.app.config import Settings


def test_worldmap_health_defaults_to_sidecar():
    s = Settings(
        WORLDMAP_BASE_URL="http://sk-ai-worldmap:8080",
        WORLDMAP_HEALTH_PATH="/api/sidecar-health",
    )
    assert s.worldmap_health_url == "http://sk-ai-worldmap:8080/api/sidecar-health"


def test_entrypoint_script_mentions_sidecar_health():
    from pathlib import Path

    text = Path("entrypoint.sh").read_text(encoding="utf-8")
    assert "/api/sidecar-health" in text
    assert "WorldMap" in text
