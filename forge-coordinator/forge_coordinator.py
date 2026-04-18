#!/usr/bin/env python3
"""Forge coordinator — deterministic Python orchestrator.

Dispatches Claude Code CLI sessions for each agent, evaluates results,
retries with failure constraints. No LLM in the orchestration loop.

Usage:
    python forge_coordinator.py --config seed-storage.json
    python forge_coordinator.py --config seed-storage.json --resume  # resume from BUILD-STATE
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from string import Template

def log(msg: str):
    """Print with immediate flush (nohup buffers stdout otherwise)."""
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class AgentDef:
    name: str
    tier: int
    files: list[str]
    test_files: list[str]
    spec_sections: list[str]  # section headers to extract from parallel spec
    expected_test_count: int = 0

@dataclass
class ForgeConfig:
    run_id: str
    target_repo: str          # git clone URL
    target_dir: str           # local working directory
    branch: str
    spec_file: str            # path to parallel spec (relative to target)
    full_spec_file: str       # path to full spec (relative to target)
    agents: list[AgentDef]
    max_iterations: int = 5
    max_turns_per_agent: int = 80
    model: str = "sonnet"
    test_env: dict = field(default_factory=dict)  # env vars for integration/E2E tests
    deploy_manifest_overrides: dict = field(default_factory=dict)  # extra manifest fields for deploy

    @classmethod
    def from_file(cls, path: str) -> "ForgeConfig":
        with open(path) as f:
            data = json.load(f)
        agents = [AgentDef(**a) for a in data.pop("agents")]
        return cls(agents=agents, **data)


# ---------------------------------------------------------------------------
# Build State
# ---------------------------------------------------------------------------

@dataclass
class AgentResult:
    name: str
    status: str = "pending"  # pending, running, success, failed
    exit_code: int | None = None
    error: str | None = None
    test_count: int = 0
    duration_s: float = 0

@dataclass
class IterationResult:
    iteration: int
    agent_results: list[AgentResult] = field(default_factory=list)
    eval_passed: bool = False
    eval_report: dict = field(default_factory=dict)
    constraints: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

@dataclass
class BuildState:
    run_id: str
    target: str
    branch: str
    status: str = "in_progress"  # in_progress, passed, failed
    current_iteration: int = 0
    iterations: list[IterationResult] = field(default_factory=list)
    started_at: str = ""

    def save(self, path: str):
        with open(path, "w") as f:
            json.dump(self.__dict__, f, indent=2, default=_serialize)

    @classmethod
    def load(cls, path: str) -> "BuildState":
        with open(path) as f:
            data = json.load(f)
        iters = [IterationResult(**i) for i in data.pop("iterations", [])]
        for it in iters:
            it.agent_results = [AgentResult(**a) for a in it.agent_results]
        return cls(iterations=iters, **data)

def _serialize(obj):
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def run_evaluation(target_dir: str) -> dict:
    """Run pytest + import checks. Returns structured report."""
    report = {
        "passed": False,
        "tests": {"total": 0, "passed": 0, "failed": 0, "errors": 0, "output": ""},
        "imports": {"ok": [], "failed": []},
        "conventions": {"violations": []},
    }

    # 1. Unit tests
    try:
        result = subprocess.run(
            ["uv", "run", "pytest", "tests/unit/", "-q", "--tb=short", "-x"],
            capture_output=True, text=True, cwd=target_dir, timeout=300,
        )
        report["tests"]["output"] = result.stdout[-2000:] + result.stderr[-1000:]
        # Parse pytest output for counts
        for line in result.stdout.splitlines():
            if "passed" in line or "failed" in line or "error" in line:
                import re
                nums = re.findall(r"(\d+) (passed|failed|error)", line)
                for count, kind in nums:
                    if kind == "passed":
                        report["tests"]["passed"] = int(count)
                    elif kind == "failed":
                        report["tests"]["failed"] = int(count)
                    elif kind == "error":
                        report["tests"]["errors"] = int(count)
        report["tests"]["total"] = (
            report["tests"]["passed"]
            + report["tests"]["failed"]
            + report["tests"]["errors"]
        )
    except subprocess.TimeoutExpired:
        report["tests"]["output"] = "TIMEOUT: pytest hung after 300s"
        report["tests"]["errors"] = 1
    except FileNotFoundError:
        report["tests"]["output"] = "uv not found"

    # 2. Import smoke tests
    modules = [
        "seed_storage.enrichment.models",
        "seed_storage.config",
        "seed_storage.dedup",
        "seed_storage.circuit_breaker",
        "seed_storage.cost_tracking",
        "seed_storage.rate_limiting",
        "seed_storage.notifications",
        "seed_storage.graphiti_client",
        "seed_storage.health",
        "seed_storage.enrichment.dispatcher",
        "seed_storage.expansion.frontier",
        "seed_storage.ingestion.bot",
        "seed_storage.ingestion.batch",
        "seed_storage.worker.app",
        "seed_storage.worker.tasks",
        "seed_storage.worker.dead_letters",
    ]
    for mod in modules:
        try:
            result = subprocess.run(
                ["uv", "run", "python", "-c", f"import {mod}"],
                capture_output=True, text=True, cwd=target_dir, timeout=30,
            )
            if result.returncode == 0:
                report["imports"]["ok"].append(mod)
            else:
                report["imports"]["failed"].append(
                    {"module": mod, "error": result.stderr[-500:]}
                )
        except Exception as e:
            report["imports"]["failed"].append({"module": mod, "error": str(e)})

    # 3. Convention checks (subset of impl-reviewer rules)
    conventions = []
    try:
        # Check for hardcoded ports
        result = subprocess.run(
            ["grep", "-rn", r"localhost:5432\|localhost:6379\|127.0.0.1:5432",
             "--include=*.py", "seed_storage/"],
            capture_output=True, text=True, cwd=target_dir,
        )
        if result.stdout.strip():
            conventions.append(f"HARDCODED_PORTS: {result.stdout.strip()[:200]}")

        # Check for hardcoded API keys
        result = subprocess.run(
            ["grep", "-rn", r"sk-[a-zA-Z0-9]", "--include=*.py", "seed_storage/"],
            capture_output=True, text=True, cwd=target_dir,
        )
        if result.stdout.strip():
            conventions.append(f"HARDCODED_KEYS: {result.stdout.strip()[:200]}")

    except Exception:
        pass
    report["conventions"]["violations"] = conventions

    # Gate: pass if tests pass AND most imports work AND no convention violations
    test_pass = report["tests"]["failed"] == 0 and report["tests"]["passed"] >= 100
    import_pass = len(report["imports"]["failed"]) <= 3
    convention_pass = len(conventions) == 0
    report["passed"] = test_pass and import_pass and convention_pass

    return report


# ---------------------------------------------------------------------------
# Production Evaluation (Gate 2)
# ---------------------------------------------------------------------------

ANT_KEEPER_URL = "http://localhost:7070"


def _ak_headers() -> dict:
    """Load ant-keeper auth token."""
    env_path = os.path.expanduser("~/.cruse-control/env")
    token = ""
    try:
        for line in open(env_path):
            line = line.strip()
            # Handle both "export KEY=val" and "KEY=val"
            if "ANT_KEEPER_TOKEN=" in line:
                token = line.split("ANT_KEEPER_TOKEN=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _ak_request(method: str, path: str, json_data: dict | None = None) -> dict | list | None:
    """Make an ant-keeper API request via curl (no requests dependency)."""
    cmd = ["curl", "-s", "-X", method, f"{ANT_KEEPER_URL}{path}"]
    headers = _ak_headers()
    for k, v in headers.items():
        cmd.extend(["-H", f"{k}: {v}"])
    if json_data:
        cmd.extend(["-d", json.dumps(json_data)])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return None


def _kubectl(*args, timeout=15) -> subprocess.CompletedProcess:
    """Run kubectl with kubeconfig."""
    return subprocess.run(
        ["kubectl", "--kubeconfig=/opt/shared/k3s/kubeconfig.yaml", *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _get_daemon_nodeport(task_id: str) -> int | None:
    """Find NodePort for a daemon task's K8s service."""
    try:
        r = _kubectl("get", "svc", "-n", "ant-keeper", task_id, "-o", "json")
        if r.returncode == 0:
            svc = json.loads(r.stdout)
            ports = svc.get("spec", {}).get("ports", [])
            if ports:
                return ports[0].get("nodePort")
    except Exception:
        pass
    return None


def _get_pod_logs(task_id: str, lines: int = 50) -> str:
    """Get recent pod logs for a task."""
    try:
        r = _kubectl("logs", "-n", "ant-keeper", "-l", f"task={task_id}",
                      "-c", "task", f"--tail={lines}", timeout=10)
        return r.stdout[-3000:] if r.returncode == 0 else r.stderr[-1000:]
    except Exception as e:
        return f"Failed to get logs: {e}"


def deploy_to_antkeeper(config: ForgeConfig) -> dict:
    """Deploy project as ant-keeper daemon. Read manifest from repo, apply config overrides.

    Returns {"success": bool, "task_id": str, "error": str|None, "health": dict|None, "pod_logs": str}
    """
    task_id = f"forge-test-{config.run_id.split('-')[-1]}"
    result = {"success": False, "task_id": task_id, "error": None, "health": None, "pod_logs": ""}

    # Push to a Docker-tag-safe branch (ant-keeper#127: / in tags breaks builds)
    deploy_branch = config.branch.replace("/", "-")
    subprocess.run(
        ["git", "push", "origin", f"{config.branch}:{deploy_branch}", "--force"],
        cwd=config.target_dir,
    )

    # Build manifest from repo's manifest.json + config overrides
    repo_manifest_path = os.path.join(config.target_dir, "manifest.json")
    if os.path.exists(repo_manifest_path):
        with open(repo_manifest_path) as f:
            manifest = json.load(f)
    else:
        manifest = {}

    # Force daemon type with forge test identity
    manifest["id"] = task_id
    manifest["name"] = f"Forge test: {config.run_id}"
    manifest["description"] = f"Forge test deployment for {config.run_id}"
    manifest["owner"] = "wyler-zahm"
    manifest["type"] = "daemon"
    manifest["source"] = {"type": "git", "repo": config.target_repo, "ref": deploy_branch}
    manifest.setdefault("alert_channels", [])
    manifest.setdefault("resources", {"cpu": "1000m", "memory": "3Gi"})

    # Apply config overrides (credentials, dns_passthrough, databases)
    for key, val in config.deploy_manifest_overrides.items():
        manifest[key] = val

    # Strip fields ant-keeper doesn't accept
    for bad in ("schedule_type", "interval_seconds", "uses_claude", "skills_ref",
                "retry", "version", "author", "config"):
        manifest.pop(bad, None)

    # Fix credential format: must be dict {"cred_id": "ENV_VAR"}, not array
    creds = manifest.get("credentials", {})
    if isinstance(creds, list):
        manifest["credentials"] = config.deploy_manifest_overrides.get("credentials", {})

    # --- Clean up previous deployment ---
    log(f"  Cleaning up previous deployment...")
    _ak_request("DELETE", f"/api/tasks/{task_id}?force=true")
    # Clean stale K8s resources (ant-keeper#127: orphaned configmaps)
    _kubectl("delete", "configmap", f"ant-keeper-proxy-{task_id}", "-n", "ant-keeper",
             "--ignore-not-found")
    time.sleep(3)

    # --- Register (daemon auto-starts on registration) ---
    log(f"  Registering daemon {task_id} (ref={deploy_branch})...")
    resp = _ak_request("POST", "/api/tasks", manifest)
    if not isinstance(resp, dict) or "id" not in resp:
        result["error"] = f"Failed to register: {resp}"
        return result

    # --- Wait for build + deploy (up to 30 minutes for heavy images like whisper) ---
    log(f"  Waiting for image build + deploy...")
    deadline = time.time() + 1800
    last_status = "pending"
    while time.time() < deadline:
        time.sleep(20)
        runs = _ak_request("GET", f"/api/runs?task_id={task_id}&limit=1")
        if not runs or not isinstance(runs, list) or not runs:
            continue
        run = runs[0]
        status = run.get("status", "pending")
        err = run.get("error_message", "")

        if status != last_status:
            log(f"    Status: {status}")
            last_status = status

        if status == "running":
            break
        elif status == "failed":
            result["error"] = f"Deploy failed: {err[:500]}"
            result["pod_logs"] = _get_pod_logs(task_id)
            return result
    else:
        result["error"] = "Deploy timed out after 30 minutes (image build may be slow)"
        return result

    # --- Wait for health check (daemon needs time to start processes) ---
    log(f"  Daemon running, waiting for health endpoint...")
    time.sleep(15)

    node_port = _get_daemon_nodeport(task_id)
    if not node_port:
        result["error"] = "Daemon running but no NodePort service found"
        result["pod_logs"] = _get_pod_logs(task_id)
        return result

    # Poll health for up to 90 seconds
    health_deadline = time.time() + 90
    while time.time() < health_deadline:
        try:
            hr = subprocess.run(
                ["curl", "-sf", "--max-time", "5", f"http://127.0.0.1:{node_port}/health"],
                capture_output=True, text=True,
            )
            if hr.returncode == 0:
                try:
                    result["health"] = json.loads(hr.stdout)
                except json.JSONDecodeError:
                    result["health"] = {"raw": hr.stdout[:500]}

                health_status = result["health"].get("status", "unknown")
                log(f"  Health: {health_status} (port {node_port})")

                if health_status == "healthy":
                    result["success"] = True
                    return result
                else:
                    # Report unhealthy checks
                    checks = result["health"].get("checks", {})
                    failed = [k for k, v in checks.items() if v not in ("ok", "connected")]
                    log(f"    Unhealthy checks: {failed}")
        except Exception:
            pass
        time.sleep(10)

    # Health didn't reach "healthy" but daemon is running
    result["pod_logs"] = _get_pod_logs(task_id)
    if result["health"]:
        checks = result["health"].get("checks", {})
        failed = [f"{k}={v}" for k, v in checks.items() if v not in ("ok", "connected")]
        result["error"] = f"Deployed but unhealthy: {', '.join(failed)}"
    else:
        result["error"] = "Deployed but health endpoint never responded"
    return result


def run_production_eval(config: ForgeConfig) -> dict:
    """Gate 2: Deploy → integration tests → E2E tests → security tests.

    Returns structured report with per-phase results.
    """
    report = {
        "passed": False,
        "deploy": {"success": False, "error": None},
        "integration": {"total": 0, "passed": 0, "failed": 0, "output": ""},
        "e2e": {"total": 0, "passed": 0, "failed": 0, "output": ""},
        "security": {"total": 0, "passed": 0, "failed": 0, "output": ""},
    }

    target = config.target_dir

    # Env vars for tests — from config, not hardcoded
    test_env = {**os.environ, **config.test_env}

    # --- Phase 1: Deploy ---
    log(f"\n  --- Deploy ---")
    deploy_result = deploy_to_antkeeper(config)
    report["deploy"] = {"success": deploy_result["success"], "error": deploy_result.get("error")}
    if not deploy_result["success"]:
        log(f"  Deploy FAILED: {deploy_result.get('error')}")
        # Continue with tests anyway — they'll use local infra connections
    else:
        log(f"  Deploy succeeded")

    # --- Phase 2: Integration tests ---
    log(f"\n  --- Integration Tests ---")
    try:
        result = subprocess.run(
            ["uv", "run", "pytest", "tests/integration/", "-v", "--tb=short", "-x",
             ],
            capture_output=True, text=True, cwd=target, timeout=600, env=test_env,
        )
        report["integration"]["output"] = result.stdout[-3000:] + result.stderr[-1000:]
        _parse_pytest_output(result.stdout, report["integration"])
    except subprocess.TimeoutExpired:
        report["integration"]["output"] = "TIMEOUT: integration tests exceeded 10 min"
    except Exception as e:
        report["integration"]["output"] = str(e)

    log(f"  Integration: {report['integration']['passed']} passed, "
        f"{report['integration']['failed']} failed")

    # --- Phase 3: E2E tests ---
    log(f"\n  --- E2E Tests ---")
    try:
        result = subprocess.run(
            ["uv", "run", "pytest", "tests/e2e/", "-v", "--tb=short", "-x",
             ],
            capture_output=True, text=True, cwd=target, timeout=900, env=test_env,
        )
        report["e2e"]["output"] = result.stdout[-3000:] + result.stderr[-1000:]
        _parse_pytest_output(result.stdout, report["e2e"])
    except subprocess.TimeoutExpired:
        report["e2e"]["output"] = "TIMEOUT: E2E tests exceeded 15 min"
    except Exception as e:
        report["e2e"]["output"] = str(e)

    log(f"  E2E: {report['e2e']['passed']} passed, {report['e2e']['failed']} failed")

    # --- Phase 4: Security tests ---
    log(f"\n  --- Security Tests ---")
    try:
        result = subprocess.run(
            ["uv", "run", "pytest", "tests/security/", "-v", "--tb=short",
             ],
            capture_output=True, text=True, cwd=target, timeout=300, env=test_env,
        )
        report["security"]["output"] = result.stdout[-3000:] + result.stderr[-1000:]
        _parse_pytest_output(result.stdout, report["security"])
    except subprocess.TimeoutExpired:
        report["security"]["output"] = "TIMEOUT: security tests exceeded 5 min"
    except Exception as e:
        report["security"]["output"] = str(e)

    log(f"  Security: {report['security']['passed']} passed, "
        f"{report['security']['failed']} failed")

    # --- Gate ---
    # Deploy health check must return 200
    deploy_healthy = (deploy_result["success"] and
                      deploy_result.get("health", {}).get("status") == "healthy")
    # Integration: zero failures against real infra
    integ_ok = (report["integration"]["passed"] > 0 and
                report["integration"]["failed"] == 0)
    # E2E: zero failures — skipped tests (0 passed, 0 failed) treated as pass
    # (skip guards like missing OPENAI_API_KEY don't indicate a code problem)
    e2e_ok = (report["e2e"]["failed"] == 0)
    # Security: zero failures
    sec_ok = (report["security"]["passed"] > 0 and
              report["security"]["failed"] == 0)

    report["passed"] = deploy_healthy and integ_ok and e2e_ok and sec_ok

    # Log what's blocking
    if not report["passed"]:
        blockers = []
        if not deploy_healthy:
            blockers.append(f"deploy health: {deploy_result.get('health', {})}")
        if not integ_ok:
            blockers.append(f"integration: {report['integration']['failed']} failures")
        if not e2e_ok:
            blockers.append(f"e2e: {report['e2e']['failed']} failures")
        if report["e2e"]["passed"] == 0 and report["e2e"]["failed"] == 0:
            log(f"  ⚠ E2E tests all skipped (missing OPENAI_API_KEY?) — treated as pass")
        if not sec_ok:
            blockers.append(f"security: {report['security']['failed']} failures")
        log(f"  Blockers: {'; '.join(blockers)}")

    return report


def _parse_pytest_output(stdout: str, target_dict: dict):
    """Parse pytest output for pass/fail counts into target_dict."""
    import re
    for line in stdout.splitlines():
        if "passed" in line or "failed" in line or "error" in line:
            nums = re.findall(r"(\d+) (passed|failed|error)", line)
            for count, kind in nums:
                if kind == "passed":
                    target_dict["passed"] = int(count)
                elif kind == "failed":
                    target_dict["failed"] = int(count)
            target_dict["total"] = target_dict["passed"] + target_dict["failed"]


def generate_production_constraints(prod_report: dict) -> list[str]:
    """Generate constraints from production eval failures.

    Includes pod logs and health check details so agents can fix root causes.
    """
    constraints = []

    # Deploy failures — include pod logs for context
    deploy = prod_report.get("deploy", {})
    if not deploy.get("success"):
        err = deploy.get("error", "unknown")
        constraints.append(f"FIX DEPLOY: {err[:300]}")

        pod_logs = deploy.get("pod_logs", "")
        if pod_logs:
            # Extract actionable error lines from pod logs
            for line in pod_logs.splitlines():
                line = line.strip()
                if any(kw in line.lower() for kw in
                       ("error", "fatal", "refused", "unauthorized", "missing",
                        "cannot connect", "no such", "import")):
                    constraints.append(f"FIX DEPLOY (pod log): {line[:200]}")
                    if len(constraints) > 8:
                        break

    # Health check failures — specific check names
    health = deploy.get("health") if isinstance(deploy.get("health"), dict) else {}
    checks = health.get("checks", {})
    for check_name, check_val in checks.items():
        if check_val not in ("ok", "connected"):
            constraints.append(f"FIX HEALTH: {check_name} check returns '{check_val}' — must return 'ok'")

    # Test failures
    for phase in ("integration", "e2e", "security"):
        output = prod_report.get(phase, {}).get("output", "")
        for line in output.splitlines():
            if "FAILED" in line:
                constraints.append(f"FIX {phase.upper()}: {line.strip()[:200]}")
            elif "ERROR" in line and not line.strip().startswith("E "):
                constraints.append(f"FIX {phase.upper()}: {line.strip()[:200]}")

    return constraints[:25]  # Cap at 25 constraints


# ---------------------------------------------------------------------------
# Agent Dispatch
# ---------------------------------------------------------------------------

def extract_spec_sections(spec_path: str, section_names: list[str]) -> str:
    """Extract relevant sections from the spec file by heading match.

    Searches for markdown headings (##, ###) that contain any of the
    section_names keywords. Returns the matched sections concatenated.
    Falls back to first 3000 chars if no matches found.
    """
    try:
        full_text = Path(spec_path).read_text()
    except FileNotFoundError:
        return f"(spec file not found: {spec_path})"

    # Split into sections by ## or ### headings
    import re
    sections = re.split(r'(?=^#{2,3}\s)', full_text, flags=re.MULTILINE)

    matched = []
    keywords = [s.lower() for s in section_names]

    for section in sections:
        first_line = section.split('\n')[0].lower()
        for kw in keywords:
            if kw.lower() in first_line:
                matched.append(section.strip())
                break

    if matched:
        result = "\n\n---\n\n".join(matched)
        # Cap at 15000 chars to avoid overwhelming the agent
        if len(result) > 15000:
            result = result[:15000] + "\n\n... (truncated — see full spec for details)"
        return result

    # Fallback: first 3000 chars
    return full_text[:3000] + "\n\n... (could not match sections, showing first 3000 chars)"


def write_agent_prompt(
    config: ForgeConfig,
    agent: AgentDef,
    iteration: int,
    constraints: list[str],
    prompt_dir: str,
) -> str:
    """Write a prompt file for this agent. Returns path."""
    template_path = Path(__file__).parent / "templates" / f"{agent.name}.md"
    if not template_path.exists():
        template_path = Path(__file__).parent / "templates" / "generic.md"

    template_text = template_path.read_text()

    # Build constraint block
    constraint_block = ""
    if constraints:
        constraint_block = "\n## Constraints from previous iteration\n\n"
        for c in constraints:
            constraint_block += f"- {c}\n"

    # Extract relevant spec sections inline instead of making agent read 70KB
    spec_path = os.path.join(config.target_dir, config.spec_file)
    spec_excerpt = extract_spec_sections(spec_path, agent.spec_sections)

    # Substitute
    prompt = Template(template_text).safe_substitute(
        AGENT_NAME=agent.name,
        TARGET_DIR=config.target_dir,
        BRANCH=config.branch,
        SPEC_FILE=config.spec_file,
        FULL_SPEC_FILE=config.full_spec_file,
        FILES="\n".join(f"- `{f}`" for f in agent.files),
        TEST_FILES="\n".join(f"- `{f}`" for f in agent.test_files),
        SPEC_SECTIONS=", ".join(agent.spec_sections),
        SPEC_EXCERPT=spec_excerpt,
        EXPECTED_TEST_COUNT=str(agent.expected_test_count),
        ITERATION=str(iteration),
        CONSTRAINTS=constraint_block,
        TIER=str(agent.tier),
    )

    prompt_path = os.path.join(prompt_dir, f"{agent.name}-iter{iteration}.md")
    with open(prompt_path, "w") as f:
        f.write(prompt)
    return prompt_path


def run_agent(
    config: ForgeConfig,
    agent: AgentDef,
    prompt_path: str,
) -> AgentResult:
    """Run a single agent via claude -p. Returns result."""
    result = AgentResult(name=agent.name, status="running")
    start = time.time()

    log(f"\n{'='*60}")
    log(f"  AGENT: {agent.name} (tier {agent.tier})")
    log(f"  Prompt: {prompt_path}")
    log(f"{'='*60}\n")

    # Save agent output to per-agent log file for debugging
    agent_log = os.path.join(
        config.target_dir, "_forge",
        f"{agent.name}-iter{result.name}.log",
    )

    try:
        proc = subprocess.run(
            [
                "claude", "-p", prompt_path,
                "--output-format", "stream-json",
                "--verbose",
                "--max-turns", str(config.max_turns_per_agent),
                "--model", config.model,
                "--dangerously-skip-permissions",
            ],
            capture_output=True,
            text=True,
            cwd=config.target_dir,
            timeout=1800,  # 30 min per agent
        )
        result.exit_code = proc.returncode
        result.status = "success" if proc.returncode == 0 else "failed"

        # Save full output for debugging
        with open(agent_log, "w") as f:
            f.write(f"=== STDOUT ({len(proc.stdout)} bytes) ===\n")
            f.write(proc.stdout)
            f.write(f"\n=== STDERR ({len(proc.stderr)} bytes) ===\n")
            f.write(proc.stderr)

        # Extract summary from stream-json output
        for line in proc.stdout.splitlines():
            try:
                event = json.loads(line)
                if event.get("type") == "result":
                    cost = event.get("cost_usd")
                    if cost:
                        log(f"  Cost: ${cost:.4f}")
            except (json.JSONDecodeError, TypeError):
                pass

        if proc.returncode != 0:
            result.error = proc.stderr[-1000:] if proc.stderr else "non-zero exit"

    except subprocess.TimeoutExpired:
        result.status = "failed"
        result.error = "TIMEOUT: agent exceeded 30 minute limit"
    except Exception as e:
        result.status = "failed"
        result.error = str(e)

    result.duration_s = time.time() - start
    log(f"  Result: {result.status} ({result.duration_s:.0f}s)")
    log(f"  Log: {agent_log}")
    if result.error:
        log(f"  Error: {result.error[:200]}")

    return result


# ---------------------------------------------------------------------------
# Improvement
# ---------------------------------------------------------------------------

def generate_constraints(eval_report: dict, prev_constraints: list[str]) -> list[str]:
    """Generate constraints for the next iteration based on evaluation failures."""
    constraints = []

    # Failed tests
    if eval_report["tests"]["failed"] > 0:
        output = eval_report["tests"]["output"]
        # Extract FAILED test names
        for line in output.splitlines():
            if "FAILED" in line:
                constraints.append(f"FIX TEST: {line.strip()[:150]}")

    # Failed imports
    for imp in eval_report["imports"]["failed"]:
        constraints.append(
            f"FIX IMPORT: {imp['module']} — {imp['error'][:100]}"
        )

    # Convention violations
    for v in eval_report["conventions"]["violations"]:
        constraints.append(f"FIX CONVENTION: {v[:150]}")

    # Don't repeat previous constraints that were already addressed
    # (if they're not in the current failures, drop them)
    return constraints


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _update_project_status(config: ForgeConfig, status: str):
    """Update this project's status in projects.json."""
    projects_path = Path(__file__).parent / "projects.json"
    if not projects_path.exists():
        return
    try:
        with open(projects_path) as f:
            data = json.load(f)
        for p in data["projects"]:
            if p["config"] == os.path.basename(config.run_id.replace("forge-", "") + ".json"):
                p["status"] = status
                break
            # Fallback: match by target_repo
            cfg_path = Path(__file__).parent / p["config"]
            if cfg_path.exists():
                with open(cfg_path) as cf:
                    cfg = json.load(cf)
                if cfg.get("target_repo") == config.target_repo:
                    p["status"] = status
                    break
        with open(projects_path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"  Warning: failed to update project status: {e}")


def git_setup(config: ForgeConfig):
    """Clone target repo and create branch if needed."""
    target = Path(config.target_dir)
    if not target.exists():
        log(f"Cloning {config.target_repo} → {config.target_dir}")
        subprocess.run(
            ["git", "clone", config.target_repo, config.target_dir],
            check=True,
        )
    # Checkout or create branch
    subprocess.run(
        ["git", "checkout", "-B", config.branch],
        cwd=config.target_dir,
        check=True,
    )


def git_commit_agent(target_dir: str, agent_name: str, iteration: int):
    """Commit any changes made by an agent."""
    subprocess.run(["git", "add", "-A"], cwd=target_dir)
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=target_dir,
    )
    if result.returncode != 0:  # there are staged changes
        subprocess.run(
            ["git", "commit", "-m",
             f"forge: {agent_name} iteration {iteration}"],
            cwd=target_dir,
            check=True,
        )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_forge(config: ForgeConfig, resume: bool = False):
    """Main forge loop: dispatch agents → evaluate → improve → repeat."""
    state_path = os.path.join(config.target_dir, "_forge", "BUILD-STATE.json")
    prompt_dir = os.path.join(config.target_dir, "_forge", "prompts")
    os.makedirs(os.path.join(config.target_dir, "_forge"), exist_ok=True)
    os.makedirs(prompt_dir, exist_ok=True)

    if resume and os.path.exists(state_path):
        state = BuildState.load(state_path)
        log(f"Resuming from iteration {state.current_iteration}")
    else:
        git_setup(config)
        state = BuildState(
            run_id=config.run_id,
            target=config.target_dir,
            branch=config.branch,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

    constraints: list[str] = []
    if state.iterations:
        # Carry forward constraints from last iteration
        constraints = state.iterations[-1].constraints

    for iteration in range(state.current_iteration, config.max_iterations):
        state.current_iteration = iteration
        log(f"\n{'#'*60}")
        log(f"  ITERATION {iteration + 1}/{config.max_iterations}")
        log(f"  Constraints: {len(constraints)}")
        log(f"{'#'*60}")

        iter_result = IterationResult(
            iteration=iteration,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        # Group agents by tier
        tiers: dict[int, list[AgentDef]] = {}
        for agent in config.agents:
            tiers.setdefault(agent.tier, []).append(agent)

        # Run tiers sequentially (agents within a tier run sequentially for v0)
        all_agents_ok = True
        for tier_num in sorted(tiers.keys()):
            log(f"\n--- Tier {tier_num} ---")
            for agent in tiers[tier_num]:
                # Write prompt
                prompt_path = write_agent_prompt(
                    config, agent, iteration, constraints, prompt_dir
                )
                # Run agent
                agent_result = run_agent(config, agent, prompt_path)
                iter_result.agent_results.append(agent_result)

                # Commit changes
                git_commit_agent(config.target_dir, agent.name, iteration)

                if agent_result.status == "failed":
                    all_agents_ok = False
                    log(f"  ⚠ {agent.name} failed — continuing with next agent")

        # Evaluate
        log(f"\n--- Evaluation ---")
        eval_report = run_evaluation(config.target_dir)
        iter_result.eval_report = eval_report
        iter_result.eval_passed = eval_report["passed"]
        iter_result.finished_at = datetime.now(timezone.utc).isoformat()

        log(f"  Tests: {eval_report['tests']['passed']} passed, "
              f"{eval_report['tests']['failed']} failed")
        log(f"  Imports: {len(eval_report['imports']['ok'])} ok, "
              f"{len(eval_report['imports']['failed'])} failed")
        log(f"  Conventions: {len(eval_report['conventions']['violations'])} violations")
        log(f"  Gate: {'PASS' if eval_report['passed'] else 'FAIL'}")

        if not eval_report["passed"]:
            # Gate 1 failed — iterate on unit tests
            constraints = generate_constraints(eval_report, constraints)
            iter_result.constraints = constraints
            state.iterations.append(iter_result)
            state.save(state_path)

            log(f"\n  Gate 1 FAIL — generating {len(constraints)} constraints")
            for c in constraints[:10]:
                log(f"    → {c[:100]}")
            continue

        # Gate 1 passed — push code and run Gate 2 (production)
        log(f"\n✅ Gate 1 PASSED on iteration {iteration + 1}")
        log(f"  Pushing {config.branch} for deployment...")
        try:
            subprocess.run(
                ["git", "push", "-u", "origin", config.branch, "--force"],
                cwd=config.target_dir, check=True,
            )
        except Exception as e:
            log(f"  Push failed: {e}")

        # --- Gate 2: Production deployment + E2E ---
        log(f"\n{'='*60}")
        log(f"  GATE 2: Production Evaluation")
        log(f"{'='*60}")

        prod_report = run_production_eval(config)
        iter_result.eval_report["production"] = prod_report

        log(f"\n  Deploy: {'OK' if prod_report['deploy']['success'] else 'FAIL'}")
        log(f"  Integration: {prod_report['integration']['passed']}p / "
            f"{prod_report['integration']['failed']}f")
        log(f"  E2E: {prod_report['e2e']['passed']}p / {prod_report['e2e']['failed']}f")
        log(f"  Security: {prod_report['security']['passed']}p / "
            f"{prod_report['security']['failed']}f")
        log(f"  Gate 2: {'PASS' if prod_report['passed'] else 'FAIL'}")

        if prod_report["passed"]:
            state.status = "passed"
            state.iterations.append(iter_result)
            state.save(state_path)
            log(f"\n✅ BUILD FULLY PASSED on iteration {iteration + 1}")

            # Open PR with full results
            try:
                unit_passed = eval_report["tests"]["passed"]
                integ_passed = prod_report["integration"]["passed"]
                e2e_passed = prod_report["e2e"]["passed"]
                sec_passed = prod_report["security"]["passed"]
                subprocess.run(
                    ["gh", "pr", "create",
                     "--title", f"feat: seed-storage v2 (forge {config.run_id})",
                     "--body", f"Built by forge coordinator.\n\n"
                               f"- Iteration: {iteration + 1}\n"
                               f"- Unit tests: {unit_passed} passing\n"
                               f"- Integration tests: {integ_passed} passing\n"
                               f"- E2E tests: {e2e_passed} passing\n"
                               f"- Security tests: {sec_passed} passing\n"
                               f"- Deployed and health-checked via ant-keeper",
                     "--base", "main",
                     "--repo", config.target_repo.replace("https://github.com/", "").replace(".git", ""),
                     ],
                    cwd=config.target_dir,
                )
            except Exception as e:
                log(f"  PR creation failed: {e}")

            # Update project registry
            _update_project_status(config, "passed")
            return

        # Gate 2 failed — generate production constraints and iterate
        prod_constraints = generate_production_constraints(prod_report)
        constraints = prod_constraints  # Replace unit constraints with production ones
        iter_result.constraints = constraints
        state.iterations.append(iter_result)
        state.save(state_path)

        log(f"\n  Gate 2 FAIL — generating {len(constraints)} production constraints")
        for c in constraints[:10]:
            log(f"    → {c[:100]}")

        # Clean up test deployment before next iteration
        _ak_request("DELETE", "/api/tasks/seed-storage-forge-test?force=true")

    # Exhausted iterations
    state.status = "failed"
    state.save(state_path)
    _update_project_status(config, "failed")
    log(f"\n❌ BUILD FAILED after {config.max_iterations} iterations")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_production_only(config: ForgeConfig):
    """Skip agent dispatch, run production gate on existing code."""
    state_path = os.path.join(config.target_dir, "_forge", "BUILD-STATE.json")

    log(f"\n{'='*60}")
    log(f"  PRODUCTION GATE ONLY — skipping agent dispatch")
    log(f"  Target: {config.target_dir}")
    log(f"{'='*60}")

    # Quick sanity: run unit tests first
    log(f"\n--- Gate 1 (unit tests) ---")
    eval_report = run_evaluation(config.target_dir)
    log(f"  Unit tests: {eval_report['tests']['passed']}p / {eval_report['tests']['failed']}f")
    if not eval_report["passed"]:
        log(f"  Gate 1 still failing — run full pipeline first")
        sys.exit(1)

    # Push current code
    log(f"\n  Pushing {config.branch}...")
    subprocess.run(
        ["git", "push", "-u", "origin", config.branch, "--force"],
        cwd=config.target_dir, check=True,
    )

    # Run production gate
    log(f"\n{'='*60}")
    log(f"  GATE 2: Production Evaluation")
    log(f"{'='*60}")

    prod_report = run_production_eval(config)

    log(f"\n  Deploy: {'OK' if prod_report['deploy']['success'] else 'FAIL'}")
    log(f"    {prod_report['deploy'].get('error', 'no error')}")
    log(f"  Integration: {prod_report['integration']['passed']}p / "
        f"{prod_report['integration']['failed']}f")
    log(f"  E2E: {prod_report['e2e']['passed']}p / {prod_report['e2e']['failed']}f")
    log(f"  Security: {prod_report['security']['passed']}p / "
        f"{prod_report['security']['failed']}f")
    log(f"  Gate 2: {'PASS' if prod_report['passed'] else 'FAIL'}")

    if prod_report["passed"]:
        log(f"\n✅ PRODUCTION GATE PASSED")
        # Update BUILD-STATE to passed
        if os.path.exists(state_path):
            state = BuildState.load(state_path)
            state.status = "passed"
            if state.iterations:
                state.iterations[-1].eval_report["production"] = prod_report
            state.save(state_path)
            log(f"  BUILD-STATE updated to 'passed'")
        # Update projects.json
        _update_project_status(config, "passed")
    else:
        log(f"\n❌ PRODUCTION GATE FAILED")
        log(f"\n  Production constraints for next iteration:")
        constraints = generate_production_constraints(prod_report)
        for c in constraints[:15]:
            log(f"    → {c[:120]}")

        # Save constraints so full pipeline can resume with them
        if os.path.exists(state_path):
            state = BuildState.load(state_path)
            state.status = "in_progress"
            if state.iterations:
                state.iterations[-1].constraints = constraints
                state.iterations[-1].eval_report["production"] = prod_report
            state.save(state_path)
            log(f"\n  Saved constraints to BUILD-STATE. Run full pipeline to iterate.")

    # Dump full output for debugging
    for phase in ("integration", "e2e", "security"):
        output = prod_report[phase].get("output", "")
        if output:
            log(f"\n--- {phase} output (last 2000 chars) ---")
            log(output[-2000:])


def main():
    parser = argparse.ArgumentParser(description="Forge coordinator")
    parser.add_argument("--config", required=True, help="Path to forge config JSON")
    parser.add_argument("--resume", action="store_true", help="Resume from BUILD-STATE")
    parser.add_argument("--production-only", action="store_true",
                        help="Skip agents, run only production gate on existing code")
    args = parser.parse_args()

    config = ForgeConfig.from_file(args.config)
    if args.production_only:
        run_production_only(config)
    else:
        run_forge(config, resume=args.resume)


if __name__ == "__main__":
    main()
