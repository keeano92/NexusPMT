# NexusPMT

Autonomous **fundamentals-only** Kalshi desk with a Back-to-the-Future cyberpunk **web** operator UI. Ingests SK AI WorldMap telemetry over a shared Docker network, builds a Futures Wheel via SpaceXAI (xAI), scores edges on non-sports Kalshi markets, and trades in **paper** mode by default — with kill switch, pause, flatten, and approval fail-safes.

## Architecture

| Piece | Role |
|-------|------|
| `nexus-pmt` | FastAPI + autonomous workers + web UI (`:8088` → `:8080`) |
| `sk-ai-worldmap` | Existing aggregator image on `somykida_net` |
| `somykida_net` | Shared internal bridge — `http://sk-ai-worldmap:8080` |

Entrypoint blocks until `GET http://sk-ai-worldmap:8080/api/sidecar-health` returns **200** (same probe WorldMap uses in its HEALTHCHECK).

## Quick start

```powershell
# 1) Credentials (gitignored)
python scripts/setup_credentials.py

# 2) Shared network + attach WorldMap (from WorldMap repo)
docker network create somykida_net
# merge docker-compose.worldmap-net.override.yml into WorldMap compose, then up WorldMap

# 3) Run NexusPMT
docker compose up -d --build
# UI: http://localhost:8088
```

Local API without Docker gate:

```powershell
$env:SKIP_WORLDMAP_WAIT="true"   # only for local uvicorn experiments
pip install -r requirements.txt
uvicorn backend.app.main:app --host 0.0.0.0 --port 8088
```

Paste your **operator token** (from `.env`) into the UI header to use fail-safes.

## Git branches

`dev` → `staging` → `prod`. Push `dev` often after green tests. Real funds only after [`docs/SANDBOX_CHECKLIST.md`](docs/SANDBOX_CHECKLIST.md).

## Tests

```powershell
pip install -r requirements.txt
pytest tests/unit -q
pytest tests/smoke -q
```

## Market policy

Sports and entertainment odds are **hard-blocked**. Economics, politics, geopolitics, energy, climate, tech, and crypto-macro only.
