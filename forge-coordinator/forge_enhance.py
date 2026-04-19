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


def analyze_and_enhance(results: list[dict]) -> str | None:
    """Dispatch a Claude task to analyze all project results and patch forge.

    Claude reads the raw build results (constraints, failures, pod logs),
    identifies root causes in the forge templates/coordinator/config,
    and makes surgical fixes. No programmatic pattern matching — Claude
    does the analysis.

    Returns enhancement summary or None if nothing to improve.
    """
    if not results or all(r["status"] == "passed" and not r.get("failures") for r in results):
        log("  All projects clean — no enhancements needed")
        return None

    # Write project results to a file Claude can read
    results_path = COORDINATOR_DIR / "_enhance_results.json"
    results_path.write_text(json.dumps(results, indent=2))

    prompt_path = COORDINATOR_DIR / "_enhance_prompt.md"
    prompt = f"""You are the Forge system enhancement agent. Your job is to analyze build results from all projects, identify root causes of failures in the forge system itself, and patch the system to prevent recurrence.

## Build results

Read the full results at `{results_path}`. Each project entry contains:
- `status`: did the build pass or fail?
- `iterations_count`: how many iterations it took
- `constraints`: failure constraints generated during the build (these are symptoms)
- `failures`: specific agent/test failures with error details

## Forge system files (these are what you can modify)

- `{COORDINATOR_DIR}/templates/generic.md` — default agent prompt template
- `{COORDINATOR_DIR}/templates/integration-test-agent.md` — template for integration/E2E test agent
- `{COORDINATOR_DIR}/forge_coordinator.py` — the coordinator script (deploy, evaluation, constraint generation)
- `{COORDINATOR_DIR}/forge_enhance.py` — this enhancement loop (you can improve it too)
- `{COORDINATOR_DIR}/seed-storage.json` — project config (test_env, deploy_manifest_overrides)

## Your analysis process

1. Read `{results_path}` to understand what went wrong across all projects
2. For each failure pattern, trace it to a ROOT CAUSE in the forge system:
   - Is an agent template missing critical instructions? → Patch the template
   - Is the coordinator's deploy function misconfigured? → Patch the coordinator
   - Is the project config missing infra details? → Patch the config
   - Is the evaluation gate too lenient or checking the wrong things? → Patch the gate
3. Make minimal, surgical fixes to the forge system files
4. Write a summary of what you changed and why to `{COORDINATOR_DIR}/_enhance_summary.md`
5. Commit: `git add -A && git commit -m "forge-enhance: <one-line summary>"`

## Rules

- **Analyze, don't guess.** Read the actual error messages and constraints before deciding what to fix.
- **Fix root causes, not symptoms.** "Dockerfile COPY order wrong" means the infra-agent template should teach COPY ordering — don't just add a constraint.
- **Only modify forge system files** in `{COORDINATOR_DIR}/`. Never modify project code.
- **One commit with all fixes.** Don't make separate commits per fix.
- **If a failure is a one-off** (only happened once, in one project), note it but don't change the system — it may be project-specific.
- **If a failure is systemic** (happened across iterations or projects), that's a system bug. Fix it.
"""

    prompt_path.write_text(prompt)

    try:
        result = subprocess.run(
            ["claude", "-p", str(prompt_path),
             "--output-format", "stream-json", "--verbose",
             "--max-turns", "40", "--model", "sonnet",
             "--dangerously-skip-permissions"],
            capture_output=True, text=True,
            cwd=str(COORDINATOR_DIR),
            timeout=900,
        )
        # Save full output for debugging
        (COORDINATOR_DIR / "_enhance_output.log").write_text(
            result.stdout[-5000:] + "\n---STDERR---\n" + result.stderr[-2000:]
        )

        # Read summary if Claude wrote one
        summary_path = COORDINATOR_DIR / "_enhance_summary.md"
        if summary_path.exists():
            summary = summary_path.read_text().strip()
            return summary[:500]

        return f"Enhancement agent exited {result.returncode}" if result.returncode != 0 else "Enhancements applied (no summary written)"
    except subprocess.TimeoutExpired:
        return "Enhancement agent timed out after 15 minutes"
    except Exception as e:
        return f"Enhancement agent error: {e}"
    finally:
        prompt_path.unlink(missing_ok=True)
        results_path.unlink(missing_ok=True)


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

    log(f"\n--- Dispatching Claude to analyze and enhance ---")
    summary = analyze_and_enhance(results)
    if not summary:
        proj_data["system"]["last_enhancement_run"] = datetime.now(timezone.utc).isoformat()
        save_projects(projects_path, proj_data)
        return
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
    total_failures = sum(len(r.get("failures", [])) for r in results)
    total_constraints = sum(len(r.get("constraints", [])) for r in results)
    log_entry = f"""
## Enhancement {proj_data['system']['enhancement_count']} — v{new_version}

**Date:** {datetime.now(timezone.utc).isoformat()}
**Trigger:** {'all_projects_complete' if all_complete else f'24h_elapsed ({hours_since:.1f}h)'}
**Projects analyzed:** {len(results)}
**Total failures:** {total_failures}, **Total constraints:** {total_constraints}

"""
    for r in results:
        log_entry += f"- **{r['project']}**: {r['status']}, {r.get('iterations_count', 0)} iterations, {len(r.get('failures', []))} failures\n"
    log_entry += f"\n**Result:** {summary}\n"

    with open(COORDINATOR_DIR / "ENHANCEMENT-LOG.md", "a") as f:
        f.write(log_entry)

    # Create a PR for auditability instead of committing directly to main
    branch_name = f"forge-enhance/v{new_version}"
    pr_title = f"enhancements-run – v{new_version}: {total_failures} failures analyzed"
    # Build PR body from the summary
    pr_body = f"## Enhancement v{new_version}\n\n"
    pr_body += f"**Trigger:** {'all_projects_complete' if all_complete else f'24h_elapsed ({hours_since:.1f}h)'}\n"
    pr_body += f"**Projects analyzed:** {len(results)}\n"
    pr_body += f"**Total failures:** {total_failures} | **Total constraints:** {total_constraints}\n\n"
    for r in results:
        pr_body += f"- **{r['project']}**: {r['status']}, {r.get('iterations_count', 0)} iterations, {len(r.get('failures', []))} failures\n"
    pr_body += f"\n---\n\n{summary}\n"

    repo_root = str(COORDINATOR_DIR.parent)
    subprocess.run(["git", "checkout", "-b", branch_name], cwd=repo_root)
    subprocess.run(["git", "add", "-A"], cwd=repo_root)
    subprocess.run(
        ["git", "commit", "-m", f"forge-enhance: v{new_version} — {total_failures} failures analyzed"],
        cwd=repo_root,
    )
    subprocess.run(["git", "tag", f"forge-v{new_version}"], cwd=repo_root)
    subprocess.run(["git", "push", "-u", "origin", branch_name], cwd=repo_root)

    # Create PR via gh CLI
    try:
        subprocess.run(
            ["gh", "pr", "create",
             "--title", pr_title,
             "--body", pr_body,
             "--base", "main",
             ],
            cwd=repo_root,
        )
        log(f"  PR created: {pr_title}")
    except Exception as e:
        log(f"  PR creation failed: {e}")

    # Return to main so the coordinator keeps running from main
    subprocess.run(["git", "checkout", "main"], cwd=repo_root)

    log(f"\n✅ Enhancement complete: v{new_version}")
    log(f"  Branch: {branch_name}")
    log(f"  Tagged: forge-v{new_version}")


def main():
    parser = argparse.ArgumentParser(description="Forge system enhancement loop")
    parser.add_argument("--projects", default="projects.json",
                        help="Path to projects registry")
    args = parser.parse_args()

    run_enhancement(args.projects)


if __name__ == "__main__":
    main()
