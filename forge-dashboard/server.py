#!/usr/bin/env python3
"""Forge Dashboard — read-only visibility into the Forge build system.

Reads run data from ant-keeper API and project config from disk.
No database, no auth (proxies through ant-keeper token), no actions.

Usage:
    python server.py                    # default port 8090
    PORT=9000 python server.py          # custom port
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("PORT", "8090"))
ANT_KEEPER_URL = os.environ.get("ANT_KEEPER_URL", "http://127.0.0.1:7070")
ANT_KEEPER_TOKEN = os.environ.get("ANT_KEEPER_TOKEN", "")
COORDINATOR_DIR = Path(os.environ.get(
    "FORGE_COORDINATOR_DIR",
    "/home/wyler-zahm/Desktop/cruse-control/ant-keeper-tasks/forge-coordinator",
))
STATIC_DIR = Path(__file__).parent / "static"

# Map task IDs to project configs for enrichment
TASK_PROJECT_MAP = {
    "forge-seed-storage": "seed-storage.json",
}


def _load_token() -> str:
    """Load ant-keeper token from .env file, falling back to env var."""
    # Prefer .env file (authoritative) over shell env var (may be stale)
    env_path = Path("/opt/shared/ant-keeper/.env")
    if env_path.exists():
        try:
            for line in env_path.read_text().splitlines():
                if line.startswith("ANT_KEEPER_SYSTEM_TOKEN="):
                    return line.split("=", 1)[1].strip()
        except PermissionError:
            pass
    if ANT_KEEPER_TOKEN:
        return ANT_KEEPER_TOKEN
    return ""


TOKEN = _load_token()


def _headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def load_json(path: Path) -> dict | list | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Ant-keeper API proxy helpers
# ---------------------------------------------------------------------------

async def _ak_get(path: str, params: dict | None = None) -> dict | list | None:
    """GET from ant-keeper API."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{ANT_KEEPER_URL}{path}",
                headers=_headers(),
                params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# API handlers
# ---------------------------------------------------------------------------

async def api_status(request):
    """System overview — list forge tasks and their latest run status."""
    tasks_data = await _ak_get("/api/tasks")
    if not tasks_data:
        return web.json_response({"error": "Cannot reach ant-keeper"}, status=502)

    # Filter to forge tasks
    forge_tasks = []
    if isinstance(tasks_data, list):
        forge_tasks = [t for t in tasks_data if t.get("id", "").startswith("forge-")]
    elif isinstance(tasks_data, dict) and "tasks" in tasks_data:
        forge_tasks = [t for t in tasks_data["tasks"] if t.get("id", "").startswith("forge-")]

    projects = []
    for task in forge_tasks:
        task_id = task["id"]
        manifest = task.get("manifest", {})

        # Get latest runs for this task
        runs = await _ak_get("/api/runs", {"task_id": task_id, "limit": "50"}) or []

        latest = runs[0] if runs else None
        running_run = next((r for r in runs if r["status"] == "running"), None)

        # Count by status
        counts = {"success": 0, "failed": 0, "running": 0, "pending": 0}
        for r in runs:
            s = r.get("status", "unknown")
            if s in counts:
                counts[s] += 1

        # Load project config for enrichment
        config_file = TASK_PROJECT_MAP.get(task_id)
        config = load_json(COORDINATOR_DIR / config_file) if config_file else None

        projects.append({
            "id": task_id,
            "name": manifest.get("name", task_id),
            "description": manifest.get("description", ""),
            "status": "running" if running_run else (
                latest["status"] if latest else "unknown"
            ),
            "total_runs": len(runs),
            "counts": counts,
            "latest_run": _summarize_run(latest) if latest else None,
            "active_run": _summarize_run(running_run) if running_run else None,
            "repo": config.get("target_repo") if config else None,
            "branch": config.get("branch") if config else None,
        })

    return web.json_response({"projects": projects})


def _summarize_run(run: dict) -> dict:
    """Extract display-relevant fields from a run."""
    started = run.get("started_at")
    finished = run.get("finished_at")
    duration_s = None
    if started and finished:
        try:
            s = datetime.fromisoformat(started)
            f = datetime.fromisoformat(finished)
            duration_s = int((f - s).total_seconds())
        except (ValueError, TypeError):
            pass
    elif started:
        try:
            s = datetime.fromisoformat(started)
            duration_s = int((datetime.now(timezone.utc) - s).total_seconds())
        except (ValueError, TypeError):
            pass

    return {
        "id": run["id"],
        "status": run["status"],
        "trigger": run.get("trigger"),
        "started_at": started,
        "finished_at": finished,
        "duration_s": duration_s,
        "cost_usd": run.get("cost_usd"),
        "exit_code": run.get("exit_code"),
        "error_message": run.get("error_message"),
        "created_at": run.get("created_at"),
    }


async def api_runs(request):
    """All runs for a forge task."""
    task_id = request.match_info["task_id"]
    limit = request.query.get("limit", "50")
    runs = await _ak_get("/api/runs", {"task_id": task_id, "limit": limit}) or []
    return web.json_response([_summarize_run(r) for r in runs])


async def api_run_stream(request):
    """Stream events for a single run, parsed into display-ready format."""
    run_id = request.match_info["run_id"]

    # Stream endpoint returns NDJSON, not JSON — read as text
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{ANT_KEEPER_URL}/api/runs/{run_id}/stream",
                headers=_headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    return web.json_response({"error": "Run not found"}, status=404)
                text = await resp.text()
    except Exception:
        return web.json_response({"error": "Cannot reach ant-keeper"}, status=502)

    # Parse NDJSON lines
    events = []
    for line in text.strip().split("\n"):
        if line.strip():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    # Transform events into display format
    display_events = [_format_event(ev) for ev in events]
    return web.json_response({"events": display_events, "total": len(display_events)})


def _format_event(ev: dict) -> dict:
    """Transform a raw stream event into a display-ready dict."""
    event_type = ev.get("event_type", "unknown")
    content = ev.get("content", {})
    ts = ev.get("ts")
    seq = ev.get("sequence", 0)

    result = {
        "seq": seq,
        "ts": ts,
        "type": event_type,
    }

    if event_type == "agent":
        msg = content.get("message", {})
        blocks = msg.get("content", [])
        parts = []
        for block in blocks:
            if block.get("type") == "text":
                parts.append({"kind": "text", "text": block["text"]})
            elif block.get("type") == "tool_use":
                parts.append({
                    "kind": "tool_call",
                    "tool": block.get("name", "?"),
                    "input": block.get("input", {}),
                })
        usage = msg.get("usage", {})
        result["parts"] = parts
        result["model"] = msg.get("model")
        result["tokens"] = {
            "input": usage.get("input_tokens", 0),
            "output": usage.get("output_tokens", 0),
            "cache_read": usage.get("cache_read_input_tokens", 0),
        }

    elif event_type == "system":
        subtype = content.get("subtype", "")
        result["subtype"] = subtype
        if subtype == "task_progress":
            usage = content.get("usage", {})
            result["description"] = content.get("description", "")
            result["tool_count"] = usage.get("tool_uses", 0)
            result["total_tokens"] = usage.get("total_tokens", 0)
            result["duration_ms"] = usage.get("duration_ms", 0)
            result["last_tool"] = content.get("last_tool_name", "")
        elif subtype == "init":
            result["model"] = content.get("model")
            result["tools"] = content.get("tools", [])
        else:
            result["raw"] = _compact(content)

    elif event_type == "stdout":
        inner_type = content.get("type", "")
        if inner_type == "user":
            # Tool results
            msg = content.get("message", {})
            tool_results = []
            for block in msg.get("content", []):
                if block.get("type") == "tool_result":
                    text = block.get("content", "")
                    if isinstance(text, list):
                        text = " ".join(
                            b.get("text", "") for b in text if isinstance(b, dict)
                        )
                    tool_results.append({
                        "tool_use_id": block.get("tool_use_id", ""),
                        "content": str(text)[:2000],
                        "is_error": block.get("is_error", False),
                    })
            result["subtype"] = "tool_result"
            result["results"] = tool_results
        elif inner_type == "rate_limit_event":
            result["subtype"] = "rate_limit"
            info = content.get("rate_limit_info", {})
            result["rate_limit"] = {
                "status": info.get("status"),
                "resets_at": info.get("resetsAt"),
            }
        else:
            result["subtype"] = inner_type or "raw"
            result["raw"] = _compact(content)

    elif event_type == "done":
        result["result_text"] = content.get("result", "")
        result["cost_usd"] = content.get("cost_usd")
        result["num_turns"] = content.get("num_turns")
        result["total_tokens"] = content.get("total_tokens")
        result["duration_ms"] = content.get("duration_ms")

    elif event_type == "error":
        result["error"] = content.get("message", content.get("error", str(content)))

    else:
        result["raw"] = _compact(content)

    return result


def _compact(obj: dict, max_len: int = 500) -> str:
    """JSON-serialize, truncating long values."""
    try:
        s = json.dumps(obj, default=str)
        if len(s) > max_len:
            return s[:max_len] + "..."
        return s
    except (TypeError, ValueError):
        return str(obj)[:max_len]


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
app.router.add_get("/api/tasks/{task_id}/runs", api_runs)
app.router.add_get("/api/runs/{run_id}/stream", api_run_stream)
app.router.add_static("/static", STATIC_DIR)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)
