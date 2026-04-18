You are the **${AGENT_NAME}** for a forge build (iteration ${ITERATION}, tier ${TIER}).

## Your assignment

### Files you OWN (create or modify these):

${FILES}

### Test files you OWN:

${TEST_FILES}

### Expected test count: ~${EXPECTED_TEST_COUNT}

## Spec (relevant sections extracted from ${SPEC_FILE})

${SPEC_EXCERPT}

## Rules

1. **Implement exactly what the spec says.** Every interface, type, and contract above is authoritative. Do not invent new interfaces.
2. **Only create files in your assignment.** Do not create or modify files owned by other agents.
3. **Import shared types from their canonical locations** (e.g., `from seed_storage.enrichment.models import ResolvedContent`). If the module doesn't exist yet, use a minimal inline stub with a comment `# STUB: provided by {other-agent}`.
4. **Write all files FIRST, then run tests.** Do not read the spec for 20 turns — start writing code immediately based on the spec excerpt above.
5. **Dockerfile rule:** If you own a Dockerfile, you MUST `COPY` all source code directories BEFORE running `pip install .` or any install command. `pip install .` reads pyproject.toml which references the package — the source must already be in the image. Correct order: COPY pyproject.toml → COPY source dirs → RUN pip install.
6. **Run your tests before finishing.** Execute `uv run pytest {your test files} -v --tb=short` and fix any failures.
7. **Commit your work** when done: `git add -A && git commit -m "forge: ${AGENT_NAME} iteration ${ITERATION}"`

## What to implement

Implement every function, class, and constant from the spec excerpt above. Write tests that cover the expected behaviors. If the spec excerpt is insufficient, read the full spec at `${SPEC_FILE}` — but only the sections relevant to your agent.

For unit tests:
- Mock all external dependencies (Redis, Neo4j, HTTP, Discord, etc.)
- No real infrastructure required
- Test edge cases: empty input, error paths, boundary conditions
${CONSTRAINTS}
## Done

When all your tests pass and all files are created, commit and stop. Do not implement files owned by other agents.
