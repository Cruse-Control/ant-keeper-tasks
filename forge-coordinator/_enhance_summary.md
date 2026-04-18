# Enhancement Summary

## Changes made

### 1. `forge_coordinator.py` — Fix false-positive HARDCODED_KEYS convention check

**Problem:** The `run_evaluation()` convention check grepped for `sk-[a-zA-Z0-9]` and flagged `re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}")` in `dead_letters.py` and `config.py` as hardcoded API keys. These are regex patterns that *detect* leaked keys (security code), not actual hardcoded secrets. This caused a false convention violation that blocked Gate 1 and forced an unnecessary iteration.

**Fix:** Post-filter grep output in Python to exclude lines containing `re.compile(` before raising the violation. The pattern-detecting code now passes the check correctly.

### 2. `templates/generic.md` — Add Celery async/sync bridging rule (Rule 6b)

**Problem:** Integration test `test_enrich_end_to_end` failed with `a coroutine was expected, got <function _async_return.<locals>._inner at 0x...>`. The worker-agent wrote an `_async_return` helper and called it as `asyncio.run(_async_return(func))` instead of `asyncio.run(_async_return(func)())` — passing the wrapper function where asyncio expected a coroutine object. The generic template had no guidance on this pattern.

**Fix:** Added Rule 6b to `generic.md` explaining the correct pattern: `asyncio.run(_do_async_work(args))` where `_do_async_work` is an `async def` function called to produce a coroutine, with a CORRECT vs WRONG code example.

## What was NOT changed

- Dockerfile COPY order constraint: Already covered by Rule 5. One-off.
- supervisord.conf `%(ENV_*)s` constraint: Already covered by Rule 5b. One-off.
- Neo4j auth=None constraint: One-off deploy config fix.
- Deploy trigger/health failures: Transient deploy issues from early iterations, not systemic.
