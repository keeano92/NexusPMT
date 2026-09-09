"""Start / inspect the SK AI WorldMap Docker container from NexusPMT."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")


async def _run(cmd: list[str], timeout: float = 120.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", "timeout"
    out = (out_b or b"").decode("utf-8", errors="replace").strip()
    err = (err_b or b"").decode("utf-8", errors="replace").strip()
    return proc.returncode or 0, out, err


def _socket_client() -> httpx.AsyncClient | None:
    if os.name == "nt":
        # Named pipe support varies; prefer CLI on Windows host
        return None
    if not os.path.exists(DOCKER_SOCK):
        return None
    transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCK)
    return httpx.AsyncClient(transport=transport, base_url="http://localhost", timeout=60.0)


async def docker_available() -> bool:
    client = _socket_client()
    if client is not None:
        try:
            async with client as c:
                r = await c.get("/_ping")
                if r.status_code == 200:
                    return True
        except Exception:
            logger.debug("docker socket ping failed", exc_info=True)
    if not shutil.which("docker"):
        return False
    code, _, _ = await _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=15)
    return code == 0


async def container_status(name: str) -> dict[str, Any]:
    client = _socket_client()
    if client is not None:
        try:
            async with client as c:
                r = await c.get(f"/containers/{name}/json")
                if r.status_code == 404:
                    return {"exists": False, "running": False, "status": "missing", "error": "not found"}
                r.raise_for_status()
                data = r.json()
                state = data.get("State") or {}
                return {
                    "exists": True,
                    "running": bool(state.get("Running")),
                    "status": state.get("Status") or "unknown",
                    "error": None,
                }
        except Exception as exc:
            logger.debug("socket inspect failed: %s", exc)

    code, out, err = await _run(
        ["docker", "inspect", "-f", "{{.State.Status}}|{{.State.Running}}", name],
        timeout=20,
    )
    if code != 0:
        return {"exists": False, "running": False, "status": "missing", "error": err or out}
    status, running = (out.split("|", 1) + ["false"])[:2]
    return {
        "exists": True,
        "running": running.strip().lower() == "true",
        "status": status.strip(),
        "error": None,
    }


async def start_worldmap(container_name: str, compose_project: str) -> dict[str, Any]:
    """Force-start WorldMap: Engine API start, then docker CLI / compose fallback."""
    if not await docker_available():
        return {
            "ok": False,
            "action": "none",
            "error": (
                "Docker is not available to NexusPMT. Start WorldMap manually "
                f"(`docker start {container_name}`), then click RECHECK."
            ),
        }

    before = await container_status(container_name)
    if before.get("running"):
        return {"ok": True, "action": "already_running", "container": before}

    client = _socket_client()
    if client is not None:
        try:
            async with client as c:
                r = await c.post(f"/containers/{container_name}/start")
                # 204 No Content = started; 304 = already started
                if r.status_code in (204, 304):
                    after = await container_status(container_name)
                    return {"ok": True, "action": "engine_api_start", "container": after}
                err = r.text
        except Exception as exc:
            err = str(exc)
    else:
        err = "no docker socket"

    code, out, cli_err = await _run(["docker", "start", container_name], timeout=90)
    if code == 0:
        after = await container_status(container_name)
        return {"ok": True, "action": "docker_start", "stdout": out, "container": after}

    code2, out2, err2 = await _run(
        ["docker", "compose", "-p", compose_project, "up", "-d", "app"],
        timeout=180,
    )
    if code2 == 0:
        after = await container_status(container_name)
        return {
            "ok": bool(after.get("running")),
            "action": "compose_up",
            "stdout": out2,
            "stderr": err2,
            "container": after,
            "prior_start_error": cli_err or err or out,
        }

    return {
        "ok": False,
        "action": "failed",
        "error": err2 or cli_err or err or out2 or out or "unable to start WorldMap",
        "docker_start": {"code": code, "stderr": cli_err, "stdout": out},
        "compose_up": {"code": code2, "stderr": err2, "stdout": out2},
        "container": before,
    }
