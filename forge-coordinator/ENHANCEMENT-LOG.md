# Forge System Enhancement Log

Tracks system-level improvements applied by the enhancement loop.
Each entry records what changed, why, which projects triggered it, and the resulting forge version.

---

## Enhancement 1 — v0.1.1

**Date:** 2026-04-18T21:42:37.464580+00:00
**Trigger:** all_projects_complete
**Projects analyzed:** 1
**Total failures:** 2, **Total constraints:** 13

- **seed-storage-v2**: failed, 5 iterations, 2 failures

**Result:** # Enhancement Summary

## Changes made

### 1. `forge_coordinator.py` — Fix false-positive HARDCODED_KEYS convention check

**Problem:** The `run_evaluation()` convention check grepped for `sk-[a-zA-Z0-9]` and flagged `re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}")` in `dead_letters.py` and `config.py` as hardcoded API keys. These are regex patterns that *detect* leaked keys (security code), not actual hardcoded secrets. This caused a false convention violation that blocked Gate 1 and forced an unnec

## Enhancement 2 — v0.1.2

**Date:** 2026-04-19T00:04:08.051066+00:00
**Trigger:** all_projects_complete
**Projects analyzed:** 1
**Total failures:** 10, **Total constraints:** 27

- **seed-storage-v2**: in_progress, 9 iterations, 10 failures

**Result:** # Enhancement Summary

## Changes made

### 1. `forge_coordinator.py` — Fix false-positive HARDCODED_KEYS convention check

**Problem:** The `run_evaluation()` convention check grepped for `sk-[a-zA-Z0-9]` and flagged `re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}")` in `dead_letters.py` and `config.py` as hardcoded API keys. These are regex patterns that *detect* leaked keys (security code), not actual hardcoded secrets. This caused a false convention violation that blocked Gate 1 and forced an unnec

## Enhancement 3 — v0.1.3

**Date:** 2026-04-19T01:57:24.632476+00:00
**Trigger:** all_projects_complete
**Projects analyzed:** 1
**Total failures:** 10, **Total constraints:** 27

- **seed-storage-v2**: in_progress, 9 iterations, 10 failures

**Result:** # Enhancement Summary

## Changes made (v0.1.2 → v0.1.3)

**Project analyzed:** seed-storage-v2 (9 iterations, in_progress)

### 1. `forge_coordinator.py` — Abort on repeated credential errors in Gate 2

**Problem:** The `openai` credential missing `proxy_target` in ant-keeper blocked Gate 2 deployment in 4 consecutive iterations (5, 6, 7, 8). Each iteration, production constraints were forwarded to implementation agents who cannot fix ant-keeper credentials. The forge burned 4 full agent iterat
