#!/usr/bin/env python3
"""Forge daemon — unified entry point.

Runs the dashboard (HTTP server on port 8090) and exposes endpoints
to trigger builds and the enhancement loop. All state lives in this
directory (projects/, templates/, BUILD-STATE, logs).

This is the single process that ant-keeper manages as a daemon.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

# Ensure we're running from the forge directory
os.chdir(Path(__file__).parent)

# Import the dashboard app
from dashboard import app, web

FORGE_DIR = Path(__file__).parent


def run_coordinator(config_name: str):
    """Run forge coordinator in a subprocess. Non-blocking (thread)."""
    log_path = FORGE_DIR / "coordinator.log"
    with open(log_path, "a") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "forge_coordinator.py", "--config", config_name],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=str(FORGE_DIR),
        )
    return proc


def run_enhancer():
    """Run forge enhancement loop in a subprocess. Non-blocking (thread)."""
    log_path = FORGE_DIR / "enhance.log"
    with open(log_path, "a") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "forge_enhance.py", "--projects", "projects.json"],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=str(FORGE_DIR),
        )
    return proc


# --- Extra API endpoints for triggering ---

async def api_trigger_build(request):
    """POST /api/trigger/build?project=seed-storage.json"""
    config_name = request.query.get("project", "seed-storage.json")
    config_path = FORGE_DIR / config_name
    if not config_path.exists():
        return web.json_response({"error": f"Config not found: {config_name}"}, status=404)

    def _run():
        run_coordinator(config_name)

    threading.Thread(target=_run, daemon=True).start()
    return web.json_response({"status": "triggered", "config": config_name})


async def api_trigger_enhance(request):
    """POST /api/trigger/enhance"""
    def _run():
        run_enhancer()

    threading.Thread(target=_run, daemon=True).start()
    return web.json_response({"status": "triggered"})


async def api_build_processes(request):
    """GET /api/processes — list running coordinator/enhancer processes."""
    result = subprocess.run(
        ["pgrep", "-af", "forge_coordinator|forge_enhance"],
        capture_output=True, text=True,
    )
    lines = [l for l in result.stdout.strip().splitlines() if l] if result.returncode == 0 else []
    return web.json_response({"processes": lines})


# Add trigger endpoints to the dashboard app
app.router.add_post("/api/trigger/build", api_trigger_build)
app.router.add_post("/api/trigger/enhance", api_trigger_enhance)
app.router.add_get("/api/processes", api_build_processes)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8090"))
    print(f"Forge daemon starting on port {port}", flush=True)
    print(f"  Directory: {FORGE_DIR}", flush=True)
    print(f"  Projects: {FORGE_DIR / 'projects.json'}", flush=True)
    web.run_app(app, host="0.0.0.0", port=port)
