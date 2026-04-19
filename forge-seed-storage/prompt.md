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

## Phase 4: Runtime Validation (Gate 3)

Phase 3 asks "did it boot and pass smoke tests?" Phase 4 asks "does it actually work for users?"

Deploy the daemon as an ant-keeper task, then run journey stories against the live system.
If this phase fails, fix the specific issue and re-deploy — do not re-run Phase 2.

### Deploy the daemon

Register the deployment with ant-keeper:

```bash
HOST_IP=$(ip route show default | awk '{print $3}')
ANT_KEEPER_URL="http://${HOST_IP}:7070"

curl -s -X POST -H "Authorization: Bearer ${ANT_KEEPER_TOKEN}" \
  -H "Content-Type: application/json" \
  "${ANT_KEEPER_URL}/api/tasks" -d '{
  "id": "forge-seed-v2-test",
  "name": "Forge Test: Seed Storage v2",
  "type": "daemon",
  "owner": "wyler-zahm",
  "description": "Forge Gate 3 test deployment for seed-storage v2",
  "source": {"ref": "feat-v2-rebuild", "repo": "https://github.com/Cruse-Control/seed-storage.git", "type": "git"},
  "entry_point": "/usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf",
  "enabled": true,
  "resources": {"limits": {"cpu": "2", "memory": "6Gi"}, "requests": {"cpu": "1", "memory": "3Gi"}},
  "credentials": {"openai": "OPENAI_API_KEY", "discord-bot-ant-farm": "DISCORD_BOT_TOKEN"},
  "env": {"NEO4J_URI": "bolt://neo4j.ant-keeper.svc:7687", "REDIS_URL": "redis://redis.ant-keeper.svc:6379/2", "NEO4J_USER": "neo4j", "LLM_PROVIDER": "openai"},
  "dns_passthrough": ["gateway.discord.gg", "redis.ant-keeper.svc", "neo4j.ant-keeper.svc", "api.openai.com"],
  "health_check_path": "/health",
  "health_check_port": 8080
}'
```

Wait for the pod to start (check every 15s, timeout 5 minutes):
```bash
# Poll ant-keeper for task health
for i in $(seq 1 20); do
  STATUS=$(curl -s -H "Authorization: Bearer ${ANT_KEEPER_TOKEN}" "${ANT_KEEPER_URL}/api/tasks/forge-seed-v2-test" | python3 -c "import json,sys; print(json.load(sys.stdin).get('health','unknown'))")
  echo "Health: $STATUS"
  [ "$STATUS" = "healthy" ] && break
  sleep 15
done
```

### Pre-flight checks

Before running journey stories, verify the deployment is actually alive:

1. **Pod status**: No CrashLoopBackOff, zero restarts in last 5 minutes
2. **Health endpoint**: All component checks return 'ok' (redis, neo4j, celery, bot)
3. **Log scan**: No ImportError, ModuleNotFoundError, or tracebacks in last 100 lines

If any pre-flight fails, STOP. Read the logs, diagnose the root cause, fix it in the
code, commit, push, re-deploy, and re-check. Common failures:
- **ImportError**: Missing dependency in pyproject.toml, or module exports wrong name
- **Credential error**: Module using os.environ.get() instead of Settings singleton
- **Connection refused**: DNS passthrough missing, or service not in allowed_hosts

### Journey stories to verify

Run each story against the live deployment. For API stories, use curl. For log
verification, read ant-keeper run logs or kubectl logs.

```yaml
journey_stories:
  # === MANDATORY PATTERNS (catch forge agent integration failures) ===

  - id: infra-container-startup
    persona: "Operator"
    intent: "Deploy the container and confirm it stays running"
    steps:
      - action: "Check pod status via ant-keeper API"
        expect: "Pod is Running with zero restarts"
      - action: "Wait 3 minutes"
        expect: "Pod still Running, no CrashLoopBackOff"
      - action: "Read last 100 lines of pod logs"
        expect: "No ImportError, ModuleNotFoundError, or missing dependency tracebacks"
    verification: "api"
    severity: "critical"
    purpose_connection: "If the container can't start, nothing else matters"

  - id: infra-credential-injection
    persona: "Operator"
    intent: "Verify all credentials reach the code that uses them"
    steps:
      - action: "GET /health and check component statuses"
        expect: "All checks return 'ok' — especially neo4j, redis, and celery"
      - action: "Check logs for 'authentication failed' or 'empty key' warnings"
        expect: "No credential-related errors in logs"
      - action: "Verify Settings singleton resolved file-mode credentials"
        expect: "Health endpoint reports neo4j connected (not 'password empty' or 'auth failed')"
    verification: "api"
    severity: "critical"
    purpose_connection: "Forge agents often build credential resolution in one module but bypass it in another"

  - id: infra-cross-module-health
    persona: "Operator"
    intent: "Verify health endpoint reports real status for every component"
    steps:
      - action: "GET /health"
        expect: "Response includes checks for: redis, neo4j, celery workers, discord bot — all 'ok'"
      - action: "Verify each check is real (not hardcoded 'ok')"
        expect: "Celery check confirms active workers (not ImportError caught as 'ok')"
    verification: "api"
    severity: "critical"
    purpose_connection: "Health checks that always return 200 mask real failures"

  - id: e2e-discord-to-graph
    persona: "Community member"
    intent: "Share a link in Discord and see it appear in the knowledge graph"
    steps:
      - action: "Post a message with a URL to a monitored Discord channel"
        expect: "Bot reacts with 📥 (staged) within 5 seconds"
      - action: "Wait for processing (up to 60 seconds)"
        expect: "Bot adds ⚙️ (processed) reaction, then 🧠 (loaded) reaction"
      - action: "Query Neo4j for entities related to the URL content"
        expect: "At least one Entity node exists with content extracted from the URL"
      - action: "Check Redis dedup key"
        expect: "Dedup key exists for this message (no re-processing on replay)"
    verification: "api"
    severity: "critical"
    purpose_connection: "The single most valuable test — if Discord→Graph works end-to-end, most integration is correct"

  # === DOMAIN-SPECIFIC STORIES ===

  - id: pipeline-health-all-components
    persona: "Operator"
    intent: "Check that all pipeline components are running and connected"
    steps:
      - action: "GET /health"
        expect: "HTTP 200 with JSON containing status: healthy and all component checks ok"
      - action: "Check Celery worker count"
        expect: "At least 2 workers active (raw_messages pool + graph_ingest pool)"
      - action: "Check Discord bot status"
        expect: "Bot is connected (not 'disconnected' or 'reconnecting')"
    verification: "api"
    severity: "critical"
    purpose_connection: "Operator must be able to assess system health in under 30 seconds"

  - id: pipeline-dedup-prevents-duplicates
    persona: "Operator"
    intent: "Verify that re-processing the same message doesn't create duplicate graph entries"
    steps:
      - action: "Process a message with a known URL"
        expect: "Message is ingested and entities created"
      - action: "Re-submit the same message (same source_type:source_id)"
        expect: "Message is deduplicated — skipped with 'duplicate' log entry"
      - action: "Check Neo4j entity count"
        expect: "Entity count unchanged from first ingestion"
    verification: "api"
    severity: "critical"
    purpose_connection: "Dedup is a core promise — without it, the graph fills with noise"

  - id: pipeline-circuit-breaker-fires
    persona: "Operator"
    intent: "Verify circuit breakers protect against cascading failures"
    steps:
      - action: "Check circuit breaker states via Redis keys (seed:cb:*)"
        expect: "All circuit breakers in 'closed' state (healthy)"
      - action: "Verify circuit breaker is wired into graphiti_client (not bypassed)"
        expect: "graphiti_client.py imports and checks circuit breaker before calling add_episode()"
    verification: "api"
    severity: "major"
    purpose_connection: "Circuit breakers prevent one failed upstream from killing the whole pipeline"

  - id: pipeline-cost-guardrails
    persona: "Operator"
    intent: "Verify cost tracking prevents budget overruns"
    steps:
      - action: "Check Redis cost counter key (seed:cost:daily:YYYY-MM-DD)"
        expect: "Counter exists and increments with each add_episode() call"
      - action: "Verify DAILY_LLM_BUDGET is configured in Settings"
        expect: "Budget value is set (default $5.00)"
    verification: "api"
    severity: "major"
    purpose_connection: "Without cost guardrails, a burst of messages could drain the LLM budget"

  - id: pipeline-frontier-expansion
    persona: "Operator"
    intent: "Verify frontier discovers and queues URLs found within resolved content"
    steps:
      - action: "Process a message containing a URL that has embedded links"
        expect: "expansion_urls from the ResolvedContent are added to the frontier (seed:frontier)"
      - action: "Check frontier sorted set in Redis"
        expect: "Discovered URLs appear with priority scores"
    verification: "api"
    severity: "major"
    purpose_connection: "Frontier expansion is how the knowledge graph grows beyond directly shared links"

  - id: operator-batch-import
    persona: "Operator"
    intent: "Import historical Discord messages via batch import"
    steps:
      - action: "Run batch import CLI with a small JSON file (10 messages)"
        expect: "All 10 messages are enqueued to raw_messages"
      - action: "Wait for processing (up to 120 seconds)"
        expect: "Messages processed without errors, entities created in Neo4j"
    verification: "api"
    severity: "major"
    purpose_connection: "Batch import enables backfilling the graph with historical data"
```

### Grading

For each story, grade every step as PASS, FAIL, or SKIP. Write results to:
`/app/target/_forge/journey-acceptance-forge-20260415-seed-001.md`

Use this format:
```
# Journey Validation Report — forge-20260415-seed-001
Validated: {iso_timestamp}

## Summary
Stories: {total} | Passed: {N} | Failed: {N} | Skipped: {N}
Gate: {PASS|FAIL}

## Story Results
### ✅ {story_id} — PASS
...
### ❌ {story_id} — FAIL
Root cause: {analysis}
Fix target: {file or component}
```

### Gate decision

- ALL stories PASS → Gate 3 PASS, proceed to Phase 5 (Soak)
- ANY **critical** story FAIL → Gate 3 FAIL
  - Fix the root cause in the code
  - Commit and push to feat/v2-rebuild
  - Re-deploy (re-register with ant-keeper, wait for healthy)
  - Re-run ALL journey stories
- Only **major** stories FAIL → Gate 3 PASS with warnings (log for future improvement)
- Max 2 fix-and-retry cycles. Third failure → Discord escalation, STOP.

### Cleanup after Gate 3

After Gate 3 passes (or if you need to retry):
```bash
# Disable the test deployment
curl -s -X POST -H "Authorization: Bearer ${ANT_KEEPER_TOKEN}" \
  "${ANT_KEEPER_URL}/api/tasks/forge-seed-v2-test/disable" \
  -d '{"reason": "Gate 3 complete"}'
```

---

## Phase 5: Soak (non-blocking)

Register a soak monitoring task:
```bash
curl -s -X POST -H "Authorization: Bearer ${ANT_KEEPER_TOKEN}" \
  -H "Content-Type: application/json" \
  "${ANT_KEEPER_URL}/api/tasks" -d '{
  "id": "forge-soak-seed-v2",
  "name": "Forge Soak: Seed Storage v2",
  "type": "host",
  "owner": "wyler-zahm",
  "schedule": "*/10 * * * *",
  "entry_point": "curl -sf http://forge-seed-v2-test.ant-keeper.svc:8080/health | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['"'"'status'"'"']=='"'"'healthy'"'"', f'"'"'unhealthy: {d}'"'"'; print('"'"'Soak PASS'"'"')\"",
  "working_dir": "/tmp"
}'
```

Post Discord: "Gate 3 passed. Soak monitoring registered."
Update BUILD-STATE.

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

When Phase 4 (Gate 3) passes, the build is functionally complete. Phase 5 is non-blocking
soak monitoring. Write a final summary to BUILD-STATE and push.
