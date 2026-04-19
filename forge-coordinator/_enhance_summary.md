# Enhancement Summary

## Changes made (v0.1.2 → v0.1.3)

**Project analyzed:** seed-storage-v2 (9 iterations, in_progress)

### 1. `forge_coordinator.py` — Abort on repeated credential errors in Gate 2

**Problem:** The `openai` credential missing `proxy_target` in ant-keeper blocked Gate 2 deployment in 4 consecutive iterations (5, 6, 7, 8). Each iteration, production constraints were forwarded to implementation agents who cannot fix ant-keeper credentials. The forge burned 4 full agent iterations doing nothing useful — implementation agents can't run `proxy-enable.sh`.

**Fix:** After each Gate 2 failure, detect "missing proxy_target" in the generated constraints. On first occurrence, print clear user action instructions (run `proxy-enable.sh`, then `--resume`). On second consecutive occurrence with the same error, abort the forge and exit rather than burning another agent iteration.

### 2. `templates/integration-test-agent.md` — Add async test patterns for graphiti/Neo4j

**Problem:** `test_add_episode_creates_nodes` failed across 3 iterations (5, 6, 8) with different async errors each time: event loop RuntimeError → AssertionError → `TypeError: execute_query() got multiple values for keyword argument 'parameters_'`. The integration-test-agent had detailed Redis/Neo4j fixture guidance but zero async test guidance. Graphiti's API is async — integration tests calling `add_episode` must use `@pytest.mark.asyncio` + `async def`. Without this, the agent wrote sync tests that failed with event loop errors or called graphiti incorrectly.

**Fix:** Added "## Async test patterns" section with concrete examples: use `@pytest.mark.asyncio` + `async def` for graphiti tests, do NOT use `asyncio.run()` inside pytest test functions (causes re-entry errors), ensure `pytest-asyncio` is in test dependencies, and use async Neo4j driver fixtures.

## What was NOT changed

- Dockerfile COPY order: already in `generic.md` rule 5, one-off occurrence
- supervisord.conf `%(ENV_*)s`: already in `generic.md` rule 5b, one-off
- Celery async/sync bridge: already in `generic.md` rule 6b, one-off
- Neo4j `parameters_` error in graphiti internals: dependency version mismatch, project-specific, not forge-fixable
- Neo4j `auth=None` when password empty: project-specific config issue, one-off
- HARDCODED_KEYS false positive fix: already applied in v0.1.2
