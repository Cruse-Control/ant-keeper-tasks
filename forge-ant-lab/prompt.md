You are the Forge orchestrator. Read your full protocol from the skill loaded at:
`/app/skills/forge-orchestrator/SKILL.md`

Then execute it with these parameters:

---

**run_id**: `forge-20260419-antlab-001`
**goal**: Implement ANTLab v0 — the autonomous software laboratory for CruseControl. A FastAPI daemon with Discord bot integration, experiment management via filesystem folders + thin Postgres state tracking, a distiller, evaluator, cost tracking, and the full idea→build→review pipeline. This is a standalone Python application that runs as ant-keeper tasks.

---

## Setup: clone the target repo

Your working directory is `/app`. The target repo must be cloned here first:

```bash
git config --global user.email "forge@cruse-control.com"
git config --global user.name "Forge Orchestrator"
git clone https://${GITHUB_TOKEN}@github.com/Cruse-Control/ant-lab.git /app/target
cd /app/target
git checkout -b feat/v0-build
```

**target** = `/app/target` (all subsequent file paths use this)

---

## Critical context

### This is a greenfield build — the repo only has SPEC.md

The repo contains SPEC.md (the full specification) and ECOSYSTEM.md. No code exists yet. Everything must be built from scratch. The spec is your source of truth — read it thoroughly before Phase 1.

### Key architectural decisions in the spec

ANTLab uses a **files-first storage architecture**:
- **Filesystem folders** store all semi-structured content (ideas, prompts, evaluations, reasoning traces, mining intelligence). The nuances are in the words.
- **PostgreSQL** only tracks the state machine and the numbers (status, cost, scores, verdicts, timestamps). The tables are intentionally thin.

This is a core architectural decision. Do NOT put rich text content in Postgres columns. The experiment folder IS the experiment. The Postgres row is a cache/index.

### ANTLab runs as Ant Keeper tasks

| Task | Type | Purpose |
|------|------|---------|
| `antlab-api` | daemon | Always-on FastAPI + Discord bot + reaction watcher |
| `antlab-miner` | agent | Mine seed storage for ideas (manual trigger for v0) |
| `antforge` | host | The builder — dispatches agent tasks via Ant Keeper API |
| `antlab-evaluator` | agent | Evaluates completed builds |

### Infrastructure (already running in K3s on the host)

Access from inside the container via K8s NodePort addresses:
- **PostgreSQL**: host IP port 30433
- **Redis**: host IP port 30379
- **Neo4j**: `bolt://172.18.0.1:30687` (for seed storage queries during mining — v0 stubs this)
- **Ant-keeper API**: `${ANT_KEEPER_URL}` (injected by ant-keeper)

### Discord integration

Two channels, identical mechanics:
- **#antlab-proposals** — idea approval (bot posts idea, adds 👍/👎, watches for count 1→2)
- **#antlab-review** — final review (bot posts shipped experiment, same 👍/👎 mechanics)

Both use discord.py. The bot token will be injected as `DISCORD_BOT_TOKEN`.

### v0 scope (what to build)

Build everything needed for the manual loop pipeline:

1. **PostgreSQL schema** — `experiments`, `cost_ledger`, `mine_runs` tables (thin — see SPEC.md storage section)
2. **Experiment folder system** — Python module for creating/reading/writing experiment and mining folder trees
3. **antlab-api daemon** — FastAPI server + Discord bot + reaction watcher
4. **Distiller** — takes approved idea, produces actionable prompt with cost band assignment
5. **AntForge glue** — reads distilled prompt, dispatches build via Ant Keeper API, writes results back
6. **Basic evaluator** — automated checks only (builds? runs? tests pass?), verdict: ship or scrap
7. **Cost tracking** — parse cost events from Ant Keeper run stream, write to cost_ledger, enforce ceilings
8. **End-to-end wiring** — the full pipeline: nominate idea → Discord approval → distill → build → evaluate → Discord review → promote

### v0 scope (what NOT to build)

- Mining (humans nominate ideas via API for v0)
- LLM-judged evaluation (automated checks only)
- Iterate/scrap logic with prompt revision (ship or fail, no middle ground)
- Experiment management endpoints (stop, advise, redirect, fork)
- Full budget dashboard / breakdown / history
- MCP server
- Concurrent experiments (one at a time)

---

## Phase 1: Spec review

The spec already exists at `/app/target/SPEC.md`. Run the spec reviewer against it.

Run `/app/skills/forge-spec-reviewer/SKILL.md` against:
- `/app/target/SPEC.md`

PLAN intent: Autonomous software laboratory — idea nomination → Discord approval → distillation → AntForge build → automated evaluation → Discord review → project promotion, with filesystem-first storage and thin Postgres state tracking.

Write review to: `/app/target/_forge/spec-review-forge-20260419-antlab-001.md`
Commit: `git add -A && git commit -m "forge: Phase 1 spec review"`

---

## Phase 2: Implement

The architect decomposes the spec into agents and tiers. Follow the forge-orchestrator protocol for Phase 2 — read the spec, produce a PARALLEL-SPEC, assign agents, execute tiers sequentially.

After each tier:
```bash
cd /app/target && uv run pytest tests/ -q --tb=short 2>&1 | tail -10
```

---

## Phase 2 gate (impl-reviewer)

Run `/app/skills/forge-impl-reviewer/SKILL.md`. Target: `/app/target`, run_id: `forge-20260419-antlab-001`.

Gate: unit tests pass. Conventions pass. Push the branch:
```bash
cd /app/target
git push https://${GITHUB_TOKEN}@github.com/Cruse-Control/ant-lab.git feat/v0-build
```

---

## Phase 3: Integrate + accept

Push the branch and open a PR:

```bash
cd /app/target
GH_TOKEN=${GITHUB_TOKEN} gh pr create \
  --title "feat: ANTLab v0 — autonomous software laboratory" \
  --body "Implements the v0 pipeline: idea nomination → Discord approval → distillation → AntForge build → automated evaluation → Discord review → promotion. Files-first storage, thin Postgres state tracking. Built by Forge run forge-20260419-antlab-001." \
  --base main \
  --repo Cruse-Control/ant-lab
```

Run acceptance scenarios (from `/app/skills/forge-acceptance-gate/SKILL.md`):

```yaml
acceptance_scenarios:
  - name: unit_tests_pass
    steps:
      - cd /app/target && uv run pytest tests/ -q
    expect:
      - exit code 0
      - at least 50 tests passed

  - name: smoke_test_imports
    description: All modules importable without infra
    steps:
      - cd /app/target && uv run python -c "from antlab.config import Settings; from antlab.types import Experiment, CostBand; print('OK')"
    expect:
      - output contains "OK"
      - no ImportError

  - name: api_starts
    description: FastAPI app initializes
    steps:
      - cd /app/target && timeout 5 uv run python -c "from antlab.api.app import app; print(f'routes: {len(app.routes)}')" || true
    expect:
      - output contains "routes:"
      - no ImportError

  - name: schema_migration
    description: Alembic migrations apply cleanly
    steps:
      - cd /app/target && uv run alembic upgrade head --sql 2>&1 | head -20
    expect:
      - CREATE TABLE experiments
      - CREATE TABLE cost_ledger
      - no errors
```

Write acceptance report to: `/app/target/_forge/acceptance-forge-20260419-antlab-001.md`

---

## Phase 4: Runtime Validation (Gate 3)

Deploy the antlab-api daemon as an ant-keeper task, then verify it starts and responds.

### Deploy the daemon

```bash
HOST_IP=$(ip route show default | awk '{print $3}')
ANT_KEEPER_URL="http://${HOST_IP}:7070"

curl -s -X POST -H "Authorization: Bearer ${ANT_KEEPER_TOKEN}" \
  -H "Content-Type: application/json" \
  "${ANT_KEEPER_URL}/api/tasks" -d '{
  "id": "forge-antlab-v0-test",
  "name": "Forge Test: ANTLab v0",
  "type": "daemon",
  "owner": "wyler-zahm",
  "description": "Forge Gate 3 test deployment for ANTLab v0",
  "source": {"ref": "feat/v0-build", "repo": "https://github.com/Cruse-Control/ant-lab.git", "type": "git"},
  "entry_point": "/usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf",
  "enabled": true,
  "resources": {"limits": {"cpu": "2", "memory": "4Gi"}, "requests": {"cpu": "1", "memory": "2Gi"}},
  "credentials": {"discord-bot-antlab": "DISCORD_BOT_TOKEN"},
  "env": {"EXPERIMENTS_ROOT": "/data/experiments", "MINING_ROOT": "/data/mining"},
  "dns_passthrough": ["gateway.discord.gg"],
  "health_check_path": "/health",
  "health_check_port": 8080,
  "databases": ["antlab"]
}'
```

### Journey stories

```yaml
journey_stories:
  - id: infra-container-startup
    persona: "Operator"
    intent: "Deploy and confirm it stays running"
    steps:
      - action: "Check pod status via ant-keeper API"
        expect: "Pod is Running with zero restarts"
      - action: "Wait 3 minutes"
        expect: "Pod still Running, no CrashLoopBackOff"
      - action: "Read last 100 lines of pod logs"
        expect: "No ImportError, ModuleNotFoundError, or tracebacks"
    severity: "critical"

  - id: api-health
    persona: "Operator"
    intent: "Verify API is serving"
    steps:
      - action: "GET /health"
        expect: "HTTP 200 with JSON status"
      - action: "GET /api/experiments"
        expect: "HTTP 200 with empty list"
      - action: "GET /api/budget"
        expect: "HTTP 200 with zero spend"
    severity: "critical"

  - id: idea-nomination
    persona: "Engineer"
    intent: "Nominate an idea and see it in Discord"
    steps:
      - action: "POST /api/ideas with test idea payload"
        expect: "HTTP 201 with experiment ID"
      - action: "Check Discord #antlab-proposals"
        expect: "Bot posted idea with 👍/👎 reactions"
      - action: "GET /api/experiments/{id}"
        expect: "Status is 'proposed', experiment folder exists with idea.md"
    severity: "critical"

  - id: folder-structure
    persona: "Operator"
    intent: "Verify experiment folders are created correctly"
    steps:
      - action: "After idea nomination, check /data/experiments/{id}/"
        expect: "idea.md exists with correct content"
      - action: "Check Postgres experiments table"
        expect: "Row exists with experiment_dir pointing to correct path"
    severity: "critical"
```

### Gate decision

Same as seed-storage: ALL critical stories PASS → Gate 3 PASS. Max 2 fix-and-retry cycles.

### Cleanup

```bash
curl -s -X POST -H "Authorization: Bearer ${ANT_KEEPER_TOKEN}" \
  "${ANT_KEEPER_URL}/api/tasks/forge-antlab-v0-test/disable" \
  -d '{"reason": "Gate 3 complete"}'
```

---

## BUILD-STATE

Write to `/app/target/_forge/BUILD-STATE-antlab-001.md`. Update after every phase event.

```bash
cd /app/target
git add _forge/
git commit -m "forge: phase {N} complete"
git push https://${GITHUB_TOKEN}@github.com/Cruse-Control/ant-lab.git feat/v0-build
```

---

## Done

When Phase 4 (Gate 3) passes, the build is functionally complete. Write a final summary to BUILD-STATE and push.
