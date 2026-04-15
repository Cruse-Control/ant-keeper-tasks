You are the Forge orchestrator. Read your full protocol from the skill loaded at:
`/app/skills/forge-orchestrator/SKILL.md`

Then execute it with these parameters:

---

**run_id**: `forge-20260415-seed-001`
**goal**: Implement seed-storage v2 — a clean-room rebuild of the Discord→Neo4j knowledge graph ingestion pipeline. This is a standalone Python application that runs as ant-keeper tasks, not a feature of ant-keeper itself.

---

## Setup: clone the target repo

Your working directory is `/app`. The target repo must be cloned here first:

```bash
git config --global user.email "forge@cruse-control.com"
git config --global user.name "Forge Orchestrator"
git clone https://${GITHUB_TOKEN}@github.com/Cruse-Control/seed-storage.git /app/target
cd /app/target
git checkout -b feat/v2-rebuild
```

**target** = `/app/target` (all subsequent file paths use this)

---

## Critical context: what already exists

### v1 is live in production — do not break it

Existing `main` branch has v1 running. The entire v2 build goes on branch `feat/v2-rebuild`. Never modify files under `ingestion/`, `analysis/`, or `k8s/` (these are v1). v2 code goes under the new `seed_storage/` package.

### Specs already exist — copy them in and validate

```bash
# These specs are committed in this task repo — copy into target
cp /app/skills/../spec-v2.md /app/target/docs/SPEC-v2.md
cp /app/skills/../parallel-spec-v2.md /app/target/docs/PARALLEL-SPEC-v2.md
```

Phase 1 = run the spec reviewer against these. Fix any FAILs inline. Proceed to Phase 2 once all 17 checks pass.

### Infrastructure (already running in K3s on the host)

Access from inside the container via K8s NodePort addresses:
- **Neo4j**: `bolt://172.18.0.1:30687` (host gateway — use `ip route show default | awk '{print $3}'` to get host IP)
- **Redis**: check `ANT_KEEPER_URL` env var for host IP, then use port 30379 or query ant-keeper
- **PostgreSQL**: host IP port 30433
- **Ant-keeper API**: `${ANT_KEEPER_URL}` (injected by ant-keeper)

---

## Phase 1: Spec review

Run `/app/skills/forge-spec-reviewer/SKILL.md` against:
- `/app/target/docs/SPEC-v2.md`
- `/app/target/docs/PARALLEL-SPEC-v2.md`

PLAN intent: Discord ingestion → content enrichment (Celery) → Graphiti/Neo4j storage, with circuit breakers, frontier expansion, cost guardrails, and operational alerting.

Write review to: `/app/target/_forge/spec-review-forge-20260415-seed-001.md`
Commit: `git add -A && git commit -m "forge: Phase 1 spec review"`

---

## Phase 2: Implement (run agents sequentially)

All work on branch `feat/v2-rebuild`. Each agent reads PARALLEL-SPEC-v2.md for their section. Commit after each agent completes.

**Tier-0 agents (run sequentially):**

1. **types-agent** — `seed_storage/enrichment/models.py`, `tests/unit/test_models.py`
2. **config-agent** — `seed_storage/config.py`, `seed_storage/__init__.py`, `pyproject.toml` (add deps), `.env.example`, `tests/unit/test_config.py`
3. **infra-agent** — `Dockerfile`, `supervisord.conf`, `manifest.json`, `docker-compose.yml`
4. **redis-utils-agent** — `seed_storage/dedup.py`, `seed_storage/circuit_breaker.py`, `seed_storage/cost_tracking.py`, `seed_storage/rate_limiting.py`, tests
5. **resolvers-agent** — `seed_storage/enrichment/resolvers/` (8 resolvers + dispatcher), tests
6. **graphiti-agent** — `seed_storage/graphiti_client.py`, `seed_storage/query/search.py`, tests
7. **frontier-agent** — `seed_storage/expansion/frontier.py`, `policies.py`, `scanner.py`, `cli.py`, tests
8. **ingestion-agent** — `seed_storage/ingestion/bot.py`, `seed_storage/ingestion/batch.py`, tests
9. **alerts-agent** — `seed_storage/notifications.py`, `seed_storage/worker/dead_letters.py`, `seed_storage/worker/replay.py`, tests
10. **health-agent** — `seed_storage/health.py`, `seed_storage/smoke_test.py`, tests

**Tier-1 agent:**

11. **worker-agent** — `seed_storage/worker/app.py`, `seed_storage/worker/tasks.py`, `tests/unit/test_tasks.py`
    Replace all stubs with real imports from Tier-0 modules. This is the integration point.

**Tier-2 agents:**

12. **integration-test-agent** — `tests/integration/`, `tests/e2e/`, `tests/security/`
13. **docs-agent** — `README.md`, `CLAUDE.md`, `docs/RUNBOOK.md`

After each tier, run:
```bash
cd /app/target && uv run pytest tests/unit/ -q --tb=short 2>&1 | tail -10
```

---

## Phase 2 gate (impl-reviewer)

Run `/app/skills/forge-impl-reviewer/SKILL.md`. Target: `/app/target`, run_id: `forge-20260415-seed-001`.

Gate: unit tests pass (~185-205 expected). Conventions pass. Push the branch:
```bash
cd /app/target
git push https://${GITHUB_TOKEN}@github.com/Cruse-Control/seed-storage.git feat/v2-rebuild
```

---

## Phase 3: Integrate + accept

"Deploy" for this project means: push the branch and open a PR.

```bash
cd /app/target
GH_TOKEN=${GITHUB_TOKEN} gh pr create \
  --title "feat: seed-storage v2 — Celery pipeline rebuild" \
  --body "Implements the v2 architecture: two-queue Celery, circuit breakers, frontier expansion, cost guardrails. Built by Forge run forge-20260415-seed-001." \
  --base main \
  --repo Cruse-Control/seed-storage
```

Run acceptance scenarios (from `/app/skills/forge-acceptance-gate/SKILL.md`):

```yaml
acceptance_scenarios:
  - name: unit_tests_pass
    steps:
      - cd /app/target && uv run pytest tests/unit/ -q
    expect:
      - exit code 0
      - at least 185 tests passed

  - name: smoke_test_imports
    description: All modules importable without infra
    steps:
      - cd /app/target && uv run python -c "from seed_storage.enrichment.models import ResolvedContent; from seed_storage.config import Settings; from seed_storage.worker.app import app; print('OK')"
    expect:
      - output contains "OK"
      - no ImportError

  - name: types_contract
    steps:
      - cd /app/target && uv run python -c "from seed_storage.enrichment.models import ResolvedContent; r = ResolvedContent.error_result('https://example.com', 'test'); print(r.to_dict()['extraction_error'])"
    expect:
      - output contains "test"
```

Write acceptance report to: `/app/target/_forge/acceptance-forge-20260415-seed-001.md`

---

## BUILD-STATE

Write to `/app/target/_forge/BUILD-STATE-seed-001.md`. Update after every phase event.

Commit and push all forge artifacts with each phase:
```bash
cd /app/target
git add _forge/
git commit -m "forge: phase {N} complete"
git push https://${GITHUB_TOKEN}@github.com/Cruse-Control/seed-storage.git feat/v2-rebuild
```

---

## Done

When Phase 3 acceptance gate passes, write a final summary to `/app/target/_forge/BUILD-STATE-seed-001.md` and push. The build is complete.
