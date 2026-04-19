You are running a **focused debug session** for **${AGENT_NAME}** (iteration ${ITERATION}).

## Problem

The following tests have failed for 2+ consecutive iterations. The full agent matrix has already been run — you need to diagnose and fix the root cause, not rewrite everything.

### Failing tests
${FAILING_TESTS}

### Error context from previous iterations
${CONSTRAINTS}

## Your files

### Implementation files you own:
${FILES}

### Test files you own:
${TEST_FILES}

## Spec context
${SPEC_EXCERPT}

## Strategy

1. **Read the failing test(s) first.** Understand exactly what they expect.
2. **Read the implementation file(s)** that the failing tests exercise.
3. **Identify the root cause.** Common patterns:
   - Test expects an interface that was renamed/moved by another agent
   - Missing import or circular dependency
   - Async/sync boundary issue (Celery tasks are sync, Graphiti is async)
   - Mock setup doesn't match actual function signature
   - Test fixture missing or misconfigured
4. **Fix the minimal code needed.** Do not refactor surrounding code.
5. **Run ONLY the failing tests** to verify your fix:
   ```bash
   uv run pytest ${FAILING_TESTS} -v --tb=long
   ```
6. **Then run all your tests** to make sure you didn't break anything:
   ```bash
   uv run pytest ${TEST_FILES} -v --tb=short
   ```
7. **Commit your fix:** `git add -A && git commit -m "forge: debug ${AGENT_NAME} iteration ${ITERATION}"`

## Rules

- Do NOT modify files outside your assignment.
- Do NOT rewrite tests that are already passing.
- Focus on the specific failure. This is a surgical fix, not a rewrite.
- If the failure is caused by code in another agent's files, create a minimal stub or adapter in YOUR files and add a comment: `# WORKAROUND: {other-agent} has incompatible interface, see constraint`
