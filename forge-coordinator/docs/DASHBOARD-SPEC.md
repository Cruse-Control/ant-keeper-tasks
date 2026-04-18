# Forge Dashboard Spec

Read-only visibility into the Forge system. No actions — just observe what Forge is doing, has done, and what's broken.

## Data sources

All state is on disk. The dashboard reads files, no database needed.

| Source | Path | Content |
|--------|------|---------|
| System version | `FORGE-VERSION` | Current semver |
| Project registry | `projects.json` | All projects + system metadata |
| Enhancement log | `ENHANCEMENT-LOG.md` | System improvement history |
| Per-project state | `{project_target_dir}/_forge/BUILD-STATE.json` | Iterations, agent results, eval reports, constraints |
| Coordinator log | `/tmp/forge-coordinator.log` | Live build output |
| Per-agent logs | `{project_target_dir}/_forge/{agent}-iter{agent}.log` | Claude session output per agent |
| Enhancement output | `_enhance_output.log`, `_enhance_summary.md` | Last enhancement run details |

## Views

### 1. System Overview (landing page)

```
┌─────────────────────────────────────────────────────────────┐
│  FORGE v0.1.0                                    ● Running  │
│  Enhancement loop: last ran 2026-04-18T03:00 (12h ago)      │
│  Enhancements applied: 3                                    │
├─────────────────────────────────────────────────────────────┤
│  PROJECTS                                                   │
│                                                             │
│  ● seed-storage-v2    iter 5/5    gate1: PASS  gate2: FAIL  │
│    Building since 2026-04-18 02:48 (10h 44m)                │
│    Current: worker-agent (tier 1)                           │
│    Unit: 390p/0f  Integration: 0p/1f  E2E: 0p/0f           │
│                                                             │
│  (no other projects registered)                             │
└─────────────────────────────────────────────────────────────┘
```

Fields:
- Forge version from `FORGE-VERSION`
- System status: Running (coordinator process alive) / Idle / Enhancing
- Per project: id, current iteration, gate results, elapsed time, current agent, test counts
- Project status derived from `BUILD-STATE.json`: building, passed, failed

### 2. Project Detail (click a project)

```
┌─────────────────────────────────────────────────────────────┐
│  seed-storage-v2                              Status: FAIL  │
│  Repo: Cruse-Control/seed-storage                           │
│  Branch: feat/v2-rebuild                                    │
│  Forge version: 0.1.0                                       │
│  Started: 2026-04-18 02:48:45 UTC                           │
├─────────────────────────────────────────────────────────────┤
│  ITERATIONS                                                 │
│                                                             │
│  #1  gate1: FAIL (279p/1f)    gate2: —                      │
│      Constraints: 2                                         │
│      Duration: 1h 12m                                       │
│                                                             │
│  #2  gate1: PASS (390p/0f)    gate2: FAIL                   │
│      Deploy: FAIL (trigger error)                           │
│      Integration: 19p/0f  E2E: 6p/0f  Security: 9p/0f      │
│      Constraints: 1                                         │
│      Duration: 58m                                          │
│                                                             │
│  #3  gate1: PASS (390p/0f)    gate2: FAIL                   │
│      Deploy: FAIL (health never responded)                  │
│      Integration: 19p/0f  E2E: 0p/0f  Security: 9p/0f      │
│      Constraints: 4                                         │
│      Duration: 45m                                          │
│                                                             │
│  #4  gate1: PASS (390p/0f)    gate2: FAIL                   │
│      Deploy: FAIL (health never responded)                  │
│      Integration: 0p/1f  E2E: 0p/0f  Security: 9p/0f       │
│      Constraints: 4                                         │
│      Duration: 38m                                          │
│                                                             │
│  #5  gate1: ...    gate2: ...         ← CURRENT             │
│      agents: types ✓ config ✓ infra ✓ redis ✓ resolvers ✓  │
│              graphiti ✓ frontier ✓ ingestion ✓ alerts ✓     │
│              health ✓ worker ● integration-test ...         │
│              docs ...                                       │
└─────────────────────────────────────────────────────────────┘
```

Fields per iteration:
- Gate 1 result: test counts
- Gate 2 result: deploy status, integration/e2e/security counts
- Constraints generated (expandable)
- Agent results: name, status (✓/✗/●/...), duration
- Total iteration duration

### 3. Iteration Detail (click an iteration)

```
┌─────────────────────────────────────────────────────────────┐
│  Iteration #4                          Duration: 38m        │
├─────────────────────────────────────────────────────────────┤
│  AGENTS                                                     │
│                                                             │
│  Tier 0:                                                    │
│    types-agent       ✓  33s    0 files changed              │
│    config-agent      ✓  128s   148 lines changed            │
│    infra-agent       ✓  303s   118 lines changed            │
│    redis-utils-agent ✓  157s   0 files changed              │
│    resolvers-agent   ✓  325s   0 files changed              │
│    graphiti-agent    ✓  67s    65 lines changed              │
│    frontier-agent    ✓  35s    0 files changed              │
│    ingestion-agent   ✓  219s   123 lines changed            │
│    alerts-agent      ✓  73s    0 files changed              │
│    health-agent      ✓  315s   74 lines changed             │
│                                                             │
│  Tier 1:                                                    │
│    worker-agent      ✓  60s    0 files changed              │
│                                                             │
│  Tier 2:                                                    │
│    integration-test  ✓  146s   0 files changed              │
│    docs-agent        ✓  157s   120 lines changed            │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│  GATE 1: PASS                                               │
│    Unit tests: 390 passed, 0 failed                         │
│    Imports: 16/16 ok                                        │
│    Conventions: 0 violations                                │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│  GATE 2: FAIL                                               │
│    Deploy: FAIL — health endpoint never responded            │
│    Health: {"redis":"ok","neo4j":"error","celery":"ok"}      │
│    Integration: 0 passed, 1 failed                          │
│    E2E: 0 passed, 0 failed (skipped: no OPENAI_API_KEY)    │
│    Security: 9 passed, 0 failed                             │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│  CONSTRAINTS → iteration #5                                 │
│    • FIX DEPLOY: Deployed but health endpoint never responded│
│    • FIX INTEGRATION: test_enrich_end_to_end FAILED          │
│    • FIX INTEGRATION: enrich_message: failed source_id=ms... │
│    • FIX INTEGRATION: FAILED test_enrich_end_to_end - Assert │
└─────────────────────────────────────────────────────────────┘
```

### 4. Agent Log (click an agent row)

Raw Claude session output for that agent's run. Streamed from `_forge/{agent}-iter{agent}.log`. Syntax-highlighted code blocks, tool use events, thinking blocks if present.

### 5. Live Coordinator Log

Tail of `/tmp/forge-coordinator.log`. Auto-scrolling. Shows real-time output from the running coordinator.

### 6. System Enhancements (link from header)

Rendered markdown of `ENHANCEMENT-LOG.md`. Shows version history, what changed, why, which projects triggered it.

## Architecture

```
forge-dashboard/
├── server.py          # FastAPI, single file
├── templates/
│   └── index.html     # Single-page app, vanilla JS + fetch polling
└── manifest.json      # ant-keeper daemon task
```

- **FastAPI** app, single file, no database
- Reads all state from disk (file mtime for caching)
- **Daemon task** in ant-keeper, port 8090
- **No auth** (read-only, internal network, Tailscale)
- **Poll-based updates** — frontend fetches `/api/status` every 5 seconds
- **No WebSocket** for v0 — polling is simpler and sufficient

## API endpoints

| Method | Path | Response |
|--------|------|----------|
| GET | `/api/status` | System overview: version, project list with current state |
| GET | `/api/projects/{id}` | Project detail: all iterations with agent results + eval reports |
| GET | `/api/projects/{id}/iterations/{n}` | Single iteration detail |
| GET | `/api/projects/{id}/agents/{name}/log` | Raw agent log file content |
| GET | `/api/coordinator/log?tail=200` | Last N lines of coordinator log |
| GET | `/api/enhancements` | Enhancement log markdown |
| GET | `/health` | `{"status": "ok"}` |

## `/api/status` response shape

```json
{
  "forge_version": "0.1.0",
  "coordinator_running": true,
  "last_enhancement_run": "2026-04-18T03:00:00Z",
  "enhancement_count": 3,
  "projects": [
    {
      "id": "seed-storage-v2",
      "status": "building",
      "current_iteration": 5,
      "max_iterations": 5,
      "started_at": "2026-04-18T02:48:45Z",
      "elapsed_seconds": 38700,
      "current_agent": "worker-agent",
      "gate1": {"passed": true, "tests_passed": 390, "tests_failed": 0},
      "gate2": {"passed": false, "deploy": "fail", "integration": "0p/1f", "e2e": "0p/0f", "security": "9p/0f"},
      "constraints_count": 4
    }
  ]
}
```

## Deployment

```json
{
  "id": "forge-dashboard",
  "type": "daemon",
  "owner": "wyler-zahm",
  "source": {
    "type": "git",
    "repo": "https://github.com/Cruse-Control/ant-keeper-tasks.git",
    "ref": "main",
    "working_dir": "forge-dashboard/"
  },
  "entry_point": "python server.py",
  "health_check_port": 8090,
  "health_check_path": "/health",
  "credentials": {},
  "databases": [],
  "resources": {"cpu": "200m", "memory": "256Mi"}
}
```

## Non-goals

- No actions (no trigger/stop/restart buttons)
- No authentication
- No persistent storage
- No notifications (ant-keeper handles alerts)
- No edit capabilities
- No agent-level streaming (just log files after completion)
