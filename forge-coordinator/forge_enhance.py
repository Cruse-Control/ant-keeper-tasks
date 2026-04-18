#!/usr/bin/env python3
"""Forge system enhancement loop.

Reads BUILD-STATE from all registered projects, identifies cross-project
failure patterns, dispatches a Claude session to patch the forge system
(templates, coordinator, config), commits changes, increments FORGE-VERSION.

Runs after all projects complete OR daily at 3am via ant-keeper.

Usage:
    python forge_enhance.py --projects projects.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def log(msg: str):
    print(msg, flush=True)


COORDINATOR_DIR = Path(__file__).parent


def load_projects(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def save_projects(path: str, data: dict):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_version() -> str:
    return (COORDINATOR_DIR / "FORGE-VERSION").read_text().strip()


def bump_version(part: str = "patch") -> str:
    """Bump version and write. Returns new version."""
    current = load_version()
    major, minor, patch = [int(x) for x in current.split(".")]
    if part == "major":
        major += 1; minor = 0; patch = 0
    elif part == "minor":
        minor += 1; patch = 0
    else:
        patch += 1
    new = f"{major}.{minor}.{patch}"
    (COORDINATOR_DIR / "FORGE-VERSION").write_text(new + "\n")
    return new


def collect_project_results(projects: list[dict]) -> list[dict]:
    """Read BUILD-STATE from each project and extract failure patterns."""
    results = []
    for proj in projects:
        config_path = COORDINATOR_DIR / proj["config"]
        if not config_path.exists():
            continue
        with open(config_path) as f:
            config = json.load(f)

        state_path = os.path.join(config["target_dir"], "_forge", "BUILD-STATE.json")
        if not os.path.exists(state_path):
            results.append({
                "project": proj["id"],
                "status": "no_build_state",
                "iterations": [],
                "constraints": [],
            })
            continue

        with open(state_path) as f:
            state = json.load(f)

        # Collect all constraints across iterations
        all_constraints = []
        all_failures = []
        for iteration in state.get("iterations", []):
            all_constraints.extend(iteration.get("constraints", []))
            # Collect agent failures
            for agent in iteration.get("agent_results", []):
                if agent.get("status") == "failed":
                    all_failures.append({
                        "agent": agent["name"],
                        "error": agent.get("error", "unknown"),
                        "iteration": iteration["iteration"],
                    })
            # Collect production eval failures
            prod = iteration.get("eval_report", {}).get("production", {})
            if prod:
                for phase in ("integration", "e2e", "security"):
                    output = prod.get(phase, {}).get("output", "")
                    if "FAILED" in output:
                        for line in output.splitlines():
                            if "FAILED" in line:
                                all_failures.append({
                                    "agent": f"production-{phase}",
                                    "error": line.strip()[:200],
                                    "iteration": iteration["iteration"],
                                })

        results.append({
            "project": proj["id"],
            "status": state.get("status", "unknown"),
            "forge_version": proj.get("current_forge_version", "unknown"),
            "iterations_count": len(state.get("iterations", [])),
            "constraints": all_constraints,
            "failures": all_failures,
        })

    return results


def identify_patterns(results: list[dict]) -> list[dict]:
    """Identify cross-project patterns that warrant system changes."""
    patterns = []

    # Collect all constraints and failures across projects
    all_constraints: dict[str, list[str]] = {}  # constraint text → [project_ids]
    all_failure_agents: dict[str, int] = {}  # agent name → failure count

    for r in results:
        for c in r.get("constraints", []):
            # Normalize constraint to pattern
            key = c.split(":")[0] if ":" in c else c[:50]
            all_constraints.setdefault(key, []).append(r["project"])

        for f in r.get("failures", []):
            agent = f["agent"]
            all_failure_agents[agent] = all_failure_agents.get(agent, 0) + 1

    # Pattern: same constraint type across multiple projects
    for key, projects in all_constraints.items():
        if len(projects) > 1 or all_constraints[key].count(projects[0]) >= 2:
            patterns.append({
                "type": "recurring_constraint",
                "key": key,
                "projects": projects,
                "occurrences": len(projects),
                "recommendation": f"Update template or spec to prevent: {key}",
            })

    # Pattern: same agent failing across iterations
    for agent, count in all_failure_agents.items():
        if count >= 2:
            patterns.append({
                "type": "agent_recurring_failure",
                "agent": agent,
                "count": count,
                "recommendation": f"Review and improve {agent} template or spec sections",
            })

    return patterns


def apply_enhancements(patterns: list[dict], projects_path: str) -> str | None:
    """Dispatch a Claude session to apply system enhancements based on patterns.

    Returns the enhancement summary or None if no enhancements needed.
    """
    if not patterns:
        log("  No patterns identified — no enhancements needed")
        return None

    # Build a prompt for Claude to analyze patterns and patch the system
    prompt_path = COORDINATOR_DIR / "_enhance_prompt.md"
    prompt = f"""You are the Forge system enhancement agent.

## Context

The Forge coordinator at `{COORDINATOR_DIR}` builds projects by dispatching Claude agents.
After reviewing build results across all projects, the following patterns were identified:

## Patterns

"""
    for i, p in enumerate(patterns, 1):
        prompt += f"### Pattern {i}: {p['type']}\n"
        prompt += f"- Key: {p.get('key', p.get('agent', 'unknown'))}\n"
        prompt += f"- Occurrences: {p.get('occurrences', p.get('count', 0))}\n"
        prompt += f"- Recommendation: {p['recommendation']}\n\n"

    prompt += f"""
## Your task

1. Read the current templates in `{COORDINATOR_DIR}/templates/`
2. Read the coordinator code at `{COORDINATOR_DIR}/forge_coordinator.py`
3. For each pattern, make a **minimal surgical fix** to prevent it in future builds
4. Only modify files in `{COORDINATOR_DIR}/` — never modify project code
5. Commit your changes: `git add -A && git commit -m "forge-enhance: <summary>"`

## Rules

- Fix the ROOT CAUSE in the template/coordinator, not the symptom in project code
- If a pattern is about mocked tests, fix the template instructions
- If a pattern is about Dockerfile ordering, add a rule to the infra-agent template
- If a pattern is about missing env vars, add defaults to the config schema
- Keep changes minimal — one fix per pattern
"""

    prompt_path.write_text(prompt)

    try:
        result = subprocess.run(
            ["claude", "-p", str(prompt_path),
             "--output-format", "stream-json", "--verbose",
             "--max-turns", "30", "--model", "sonnet",
             "--dangerously-skip-permissions"],
            capture_output=True, text=True,
            cwd=str(COORDINATOR_DIR),
            timeout=600,
        )
        # Save output
        log_path = COORDINATOR_DIR / "_enhance_output.log"
        log_path.write_text(result.stdout + "\n" + result.stderr)

        if result.returncode == 0:
            return f"Applied {len(patterns)} enhancements"
        else:
            return f"Enhancement agent failed: {result.stderr[-200:]}"
    except Exception as e:
        return f"Enhancement agent error: {e}"
    finally:
        prompt_path.unlink(missing_ok=True)


def run_enhancement(projects_path: str):
    """Main enhancement loop."""
    log(f"\n{'='*60}")
    log(f"  FORGE SYSTEM ENHANCEMENT")
    log(f"  Version: {load_version()}")
    log(f"  Time: {datetime.now(timezone.utc).isoformat()}")
    log(f"{'='*60}")

    proj_data = load_projects(projects_path)
    projects = proj_data["projects"]

    # Check trigger condition: all projects complete OR 24h since last run
    last_run = proj_data["system"].get("last_enhancement_run")
    all_complete = all(p["status"] in ("passed", "failed") for p in projects)
    hours_since = 999
    if last_run:
        from datetime import datetime as dt
        try:
            last_dt = dt.fromisoformat(last_run)
            hours_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
        except ValueError:
            pass

    if not all_complete and hours_since < 24:
        log(f"  Skip: not all projects complete ({sum(1 for p in projects if p['status'] in ('passed','failed'))}/{len(projects)}) "
            f"and only {hours_since:.1f}h since last run")
        return

    log(f"\n--- Collecting project results ---")
    results = collect_project_results(projects)
    for r in results:
        log(f"  {r['project']}: {r['status']}, "
            f"{r.get('iterations_count', 0)} iterations, "
            f"{len(r.get('constraints', []))} constraints, "
            f"{len(r.get('failures', []))} failures")

    log(f"\n--- Identifying patterns ---")
    patterns = identify_patterns(results)
    for p in patterns:
        log(f"  {p['type']}: {p.get('key', p.get('agent', '?'))} "
            f"({p.get('occurrences', p.get('count', 0))} occurrences)")

    if not patterns:
        log(f"\n  No patterns found — system is clean")
        proj_data["system"]["last_enhancement_run"] = datetime.now(timezone.utc).isoformat()
        save_projects(projects_path, proj_data)
        return

    log(f"\n--- Applying enhancements ---")
    summary = apply_enhancements(patterns, projects_path)
    log(f"  Result: {summary}")

    # Bump version
    new_version = bump_version()
    log(f"  New version: {new_version}")

    # Update projects.json
    proj_data["system"]["version"] = new_version
    proj_data["system"]["last_enhancement_run"] = datetime.now(timezone.utc).isoformat()
    proj_data["system"]["enhancement_count"] += 1
    for p in proj_data["projects"]:
        p["current_forge_version"] = new_version
    save_projects(projects_path, proj_data)

    # Append to enhancement log
    log_entry = f"""
## Enhancement {proj_data['system']['enhancement_count']} — v{new_version}

**Date:** {datetime.now(timezone.utc).isoformat()}
**Trigger:** {'all_projects_complete' if all_complete else f'24h_elapsed ({hours_since:.1f}h)'}
**Patterns:** {len(patterns)}

"""
    for p in patterns:
        log_entry += f"- **{p['type']}**: {p.get('key', p.get('agent', '?'))} — {p['recommendation']}\n"
    log_entry += f"\n**Result:** {summary}\n"

    with open(COORDINATOR_DIR / "ENHANCEMENT-LOG.md", "a") as f:
        f.write(log_entry)

    # Git tag
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(COORDINATOR_DIR),
    )
    subprocess.run(
        ["git", "commit", "-m", f"forge-enhance: v{new_version} — {len(patterns)} patterns"],
        cwd=str(COORDINATOR_DIR),
    )
    subprocess.run(
        ["git", "tag", f"forge-v{new_version}"],
        cwd=str(COORDINATOR_DIR),
    )

    log(f"\n✅ Enhancement complete: v{new_version}")
    log(f"  Tagged: forge-v{new_version}")


def main():
    parser = argparse.ArgumentParser(description="Forge system enhancement loop")
    parser.add_argument("--projects", default="projects.json",
                        help="Path to projects registry")
    args = parser.parse_args()

    run_enhancement(args.projects)


if __name__ == "__main__":
    main()
