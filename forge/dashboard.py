#!/usr/bin/env python3
"""Forge Dashboard — read-only visibility into the Forge build system.

Reads all state from disk (BUILD-STATE.json, projects.json, log files).
No database, no auth, no actions. Just observe.

Usage:
    python server.py                    # default port 8090
    PORT=9000 python server.py          # custom port
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("PORT", "8090"))
# Everything lives in the same directory as this script
COORDINATOR_DIR = Path(__file__).parent
COORDINATOR_LOG = COORDINATOR_DIR / "coordinator.log"
STATIC_DIR = COORDINATOR_DIR / "static"


# ---------------------------------------------------------------------------
# Data loading (all from disk)
# ---------------------------------------------------------------------------

def load_json(path: Path) -> dict | list | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def load_version() -> str:
    try:
        return (COORDINATOR_DIR / "FORGE-VERSION").read_text().strip()
    except FileNotFoundError:
        return "unknown"


def load_projects() -> dict:
    return load_json(COORDINATOR_DIR / "projects.json") or {"projects": [], "system": {}}


def load_build_state(project: dict) -> dict | None:
    config_path = COORDINATOR_DIR / project["config"]
    config = load_json(config_path)
    if not config:
        return None
    state_path = os.path.join(config["target_dir"], "_forge", "BUILD-STATE.json")
    return load_json(Path(state_path))


def coordinator_running() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-f", "forge_coordinator.py"],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def get_current_agent(state: dict | None) -> str | None:
    """Infer which agent is currently running from BUILD-STATE."""
    if not state or state.get("status") != "in_progress":
        return None
    iterations = state.get("iterations", [])
    if not iterations:
        return None
    last = iterations[-1]
    agents = last.get("agent_results", [])
    for a in agents:
        if a.get("status") == "running":
            return a["name"]
    return None


def build_project_summary(project: dict, state: dict | None) -> dict:
    """Build summary for a single project."""
    if not state:
        return {
            "id": project["id"],
            "status": project.get("status", "unknown"),
            "current_iteration": 0,
            "max_iterations": 0,
            "started_at": None,
            "elapsed_seconds": 0,
            "current_agent": None,
            "gate1": None,
            "gate2": None,
            "constraints_count": 0,
        }

    iterations = state.get("iterations", [])
    last_iter = iterations[-1] if iterations else {}
    eval_report = last_iter.get("eval_report", {})
    prod = eval_report.get("production", {})

    # Gate 1
    gate1 = None
    if "tests" in eval_report:
        gate1 = {
            "passed": last_iter.get("eval_passed", False),
            "tests_passed": eval_report["tests"].get("passed", 0),
            "tests_failed": eval_report["tests"].get("failed", 0),
        }

    # Gate 2
    gate2 = None
    if prod:
        gate2 = {
            "passed": prod.get("passed", False),
            "deploy": "ok" if prod.get("deploy", {}).get("success") else "fail",
            "integration": f"{prod.get('integration', {}).get('passed', 0)}p/{prod.get('integration', {}).get('failed', 0)}f",
            "e2e": f"{prod.get('e2e', {}).get('passed', 0)}p/{prod.get('e2e', {}).get('failed', 0)}f",
            "security": f"{prod.get('security', {}).get('passed', 0)}p/{prod.get('security', {}).get('failed', 0)}f",
        }

    started = state.get("started_at")
    elapsed = 0
    if started:
        try:
            start_dt = datetime.fromisoformat(started)
            elapsed = (datetime.now(timezone.utc) - start_dt).total_seconds()
        except (ValueError, TypeError):
            pass

    # Load config for max_iterations
    config = load_json(COORDINATOR_DIR / project["config"]) or {}

    return {
        "id": project["id"],
        "status": state.get("status", "unknown"),
        "current_iteration": state.get("current_iteration", 0) + 1,
        "max_iterations": config.get("max_iterations", 5),
        "started_at": started,
        "elapsed_seconds": int(elapsed),
        "current_agent": get_current_agent(state),
        "gate1": gate1,
        "gate2": gate2,
        "constraints_count": len(last_iter.get("constraints", [])),
    }


# ---------------------------------------------------------------------------
# API handlers
# ---------------------------------------------------------------------------

async def api_status(request):
    """System overview."""
    proj_data = load_projects()
    projects = proj_data.get("projects", [])
    system = proj_data.get("system", {})

    summaries = []
    for p in projects:
        state = load_build_state(p)
        summaries.append(build_project_summary(p, state))

    return web.json_response({
        "forge_version": load_version(),
        "coordinator_running": coordinator_running(),
        "last_enhancement_run": system.get("last_enhancement_run"),
        "enhancement_count": system.get("enhancement_count", 0),
        "projects": summaries,
    })


async def api_project_detail(request):
    """Full project detail with all iterations."""
    project_id = request.match_info["id"]
    proj_data = load_projects()

    project = None
    for p in proj_data.get("projects", []):
        if p["id"] == project_id:
            project = p
            break
    if not project:
        return web.json_response({"error": "Project not found"}, status=404)

    state = load_build_state(project)
    if not state:
        return web.json_response({"error": "No build state"}, status=404)

    config = load_json(COORDINATOR_DIR / project["config"]) or {}

    # Build iteration summaries
    iterations = []
    for it in state.get("iterations", []):
        eval_report = it.get("eval_report", {})
        prod = eval_report.get("production", {})

        iter_summary = {
            "iteration": it["iteration"] + 1,
            "started_at": it.get("started_at"),
            "finished_at": it.get("finished_at"),
            "gate1_passed": it.get("eval_passed", False),
            "tests": eval_report.get("tests", {}),
            "imports": {
                "ok": len(eval_report.get("imports", {}).get("ok", [])),
                "failed": len(eval_report.get("imports", {}).get("failed", [])),
            },
            "conventions": len(eval_report.get("conventions", {}).get("violations", [])),
            "gate2": None,
            "agents": it.get("agent_results", []),
            "constraints": it.get("constraints", []),
        }

        if prod:
            iter_summary["gate2"] = {
                "passed": prod.get("passed", False),
                "deploy": prod.get("deploy", {}),
                "integration": {k: v for k, v in prod.get("integration", {}).items() if k != "output"},
                "e2e": {k: v for k, v in prod.get("e2e", {}).items() if k != "output"},
                "security": {k: v for k, v in prod.get("security", {}).items() if k != "output"},
            }

        iterations.append(iter_summary)

    return web.json_response({
        "id": project_id,
        "status": state.get("status"),
        "repo": config.get("target_repo"),
        "branch": config.get("branch"),
        "forge_version": project.get("current_forge_version"),
        "started_at": state.get("started_at"),
        "iterations": iterations,
    })


async def api_iteration_detail(request):
    """Single iteration with full eval output."""
    project_id = request.match_info["id"]
    iter_num = int(request.match_info["n"]) - 1  # 1-indexed in URL

    proj_data = load_projects()
    project = next((p for p in proj_data.get("projects", []) if p["id"] == project_id), None)
    if not project:
        return web.json_response({"error": "Project not found"}, status=404)

    state = load_build_state(project)
    if not state:
        return web.json_response({"error": "No build state"}, status=404)

    iterations = state.get("iterations", [])
    if iter_num < 0 or iter_num >= len(iterations):
        return web.json_response({"error": "Iteration not found"}, status=404)

    return web.json_response(iterations[iter_num])


async def api_agent_log(request):
    """Raw agent log file."""
    project_id = request.match_info["id"]
    agent_name = request.match_info["name"]

    proj_data = load_projects()
    project = next((p for p in proj_data.get("projects", []) if p["id"] == project_id), None)
    if not project:
        return web.json_response({"error": "Project not found"}, status=404)

    config = load_json(COORDINATOR_DIR / project["config"]) or {}
    target_dir = config.get("target_dir", "")

    # Find the log file (pattern: {agent}-iter{agent}.log)
    forge_dir = os.path.join(target_dir, "_forge")
    log_path = os.path.join(forge_dir, f"{agent_name}-iter{agent_name}.log")

    if not os.path.exists(log_path):
        # Try finding any matching log
        try:
            for f in os.listdir(forge_dir):
                if f.startswith(agent_name) and f.endswith(".log"):
                    log_path = os.path.join(forge_dir, f)
                    break
        except FileNotFoundError:
            pass

    if not os.path.exists(log_path):
        return web.json_response({"error": f"Log not found: {agent_name}"}, status=404)

    try:
        content = Path(log_path).read_text()[-50000:]  # Last 50KB
    except Exception as e:
        content = f"Error reading log: {e}"

    return web.json_response({"agent": agent_name, "log": content})


async def api_coordinator_log(request):
    """Tail of coordinator log."""
    tail = int(request.query.get("tail", "200"))
    try:
        lines = COORDINATOR_LOG.read_text().splitlines()
        return web.json_response({"lines": lines[-tail:]})
    except FileNotFoundError:
        return web.json_response({"lines": ["(no coordinator log found)"]})


async def api_enhancements(request):
    """Enhancement log markdown."""
    try:
        content = (COORDINATOR_DIR / "ENHANCEMENT-LOG.md").read_text()
    except FileNotFoundError:
        content = "(no enhancement log)"
    return web.json_response({"content": content})


async def health(request):
    return web.json_response({"status": "ok"})


async def index(request):
    return web.FileResponse(STATIC_DIR / "index.html")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = web.Application()
app.router.add_get("/", index)
app.router.add_get("/health", health)
app.router.add_get("/api/status", api_status)
app.router.add_get("/api/projects/{id}", api_project_detail)
app.router.add_get("/api/projects/{id}/iterations/{n}", api_iteration_detail)
app.router.add_get("/api/projects/{id}/agents/{name}/log", api_agent_log)
app.router.add_get("/api/coordinator/log", api_coordinator_log)
app.router.add_get("/api/enhancements", api_enhancements)
app.router.add_static("/static", STATIC_DIR)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)
