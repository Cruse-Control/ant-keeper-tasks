# Seed Storage — Parallel Implementation Spec

> Derived from [seed-storage-spec-v2.md](seed-storage-spec-v2.md) using the [Parallel Agent Implementation Guide](personas/parallel-impl-guide-merged-created-2026-04-12.md).
> Created: 2026-04-14.
> Source spec: seed-storage-spec-v2.md (v2 revision 7, 2026-04-12).
> Goal: decompose the seed-storage replacement build into parallel agent work — preventing drift, incompatible code, and coordination failures.

---

## Section 1: Module Decomposition

Every module listed here has a one-sentence responsibility, typed Python interfaces, and explicit file ownership. Agents reading only this document should implement their module without reading any other agent's source.

### Shared Types — `seed_storage/enrichment/models.py`

**Responsibility:** Canonical location for all shared data types used across modules.

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

ContentType = Literal["webpage", "youtube", "video", "image", "pdf", "github", "tweet", "unknown"]

@dataclass
class ResolvedContent:
    source_url: str
    content_type: ContentType
    title: str | None
    text: str                       # clean extracted text; empty string on failure
    transcript: str | None          # for video/audio content
    summary: str | None             # populated by vision LLM for images
    expansion_urls: list[str]       # secondary URLs found within this content
    metadata: dict[str, Any]        # source-specific extras
    extraction_error: str | None    # None on success, error message on failure
    resolved_at: datetime           # UTC, set by dispatcher after resolution completes

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict. datetime → ISO 8601 string."""
        ...

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResolvedContent":
        """Deserialize from dict. Ignores unknown keys (forward compatibility)."""
        ...

    @classmethod
    def error_result(cls, url: str, error: str) -> "ResolvedContent":
        """Factory for failed resolutions. text='', extraction_error=error, resolved_at=utcnow()."""
        ...
```

**URL canonicalization** lives in `seed_storage/dedup.py` as `canonicalize_url(url: str) -> str`. Used for URL dedup key generation.

```python
def canonicalize_url(url: str) -> str:
    """Normalize URL for dedup. Strips utm_*, fbclid, ref, si, t, s params.
    Lowercases scheme+host. Preserves path case. Sorts remaining query params.
    Removes trailing slash and fragment. Returns original on malformed input."""
    ...

def url_hash(url: str) -> str:
    """SHA256 hex digest of canonicalize_url(url)."""
    ...
```

### Module List

| Module | Responsibility | Key Interface |
|--------|---------------|---------------|
| `enrichment/models.py` | Shared types: `ResolvedContent`, `ContentType` | Dataclass with `to_dict()`, `from_dict()`, `error_result()` |
| `config.py` | All configuration via pydantic-settings `Settings` class | `Settings` singleton with validators for credentials, providers, constants |
| `dedup.py` | Redis-backed dedup (messages + URLs) and URL canonicalization | `DedupStore.seen_or_mark(key) -> bool`, `canonicalize_url()`, `url_hash()` |
| `circuit_breaker.py` | Redis-backed per-service circuit breaker | `CircuitBreaker.record_success()`, `.record_failure()`, `.is_open() -> bool` |
| `cost_tracking.py` | Redis-backed daily LLM cost counter | `CostTracker.increment()`, `.is_budget_exceeded() -> bool`, `.is_warning_threshold() -> bool`, `.get_current_spend() -> float` |
| `rate_limiting.py` | Redis-backed sliding window rate limiter | `RateLimiter.allow() -> bool` |
| `notifications.py` | Fire-and-forget Discord webhook alerts with debounce | `send_alert(message, debounce_key=None)` — sync, never raises |
| `worker/dead_letters.py` | Dead-letter storage and replay logic | `dead_letter(task_name, payload, exc, retries)`, `list_dead_letters()`, `replay_one()`, `replay_all()` |
| `worker/replay.py` | CLI for dead-letter replay | `python -m seed_storage.worker.replay --list/--all/--one` |
| `enrichment/resolvers/base.py` | Abstract base for all content resolvers | `BaseResolver.can_handle(url) -> bool`, `async resolve(url) -> ResolvedContent` |
| `enrichment/resolvers/webpage.py` | trafilatura + readability-lxml fallback | Truncate at 8000 tokens |
| `enrichment/resolvers/youtube.py` | yt-dlp metadata + transcript extraction | Truncate transcript at 12000 tokens |
| `enrichment/resolvers/image.py` | Vision LLM description | Provider-agnostic via `VISION_PROVIDER` config |
| `enrichment/resolvers/pdf.py` | docling + unstructured fallback | Truncate at 10000 tokens |
| `enrichment/resolvers/github.py` | GitHub REST API metadata + README | Authenticated if `GITHUB_TOKEN` present |
| `enrichment/resolvers/video.py` | Download → ffmpeg → transcription | Temp file cleanup in `finally` block |
| `enrichment/resolvers/twitter.py` | **TODO stub** — returns `error_result()` | URL pattern matching only |
| `enrichment/resolvers/fallback.py` | Best-effort HTML extraction | Never raises |
| `enrichment/dispatcher.py` | Routes URLs to resolvers by priority order | `ContentDispatcher.dispatch(url) -> ResolvedContent` |
| `graphiti_client.py` | Graphiti singleton with provider branching + vision client | `get_graphiti() -> Graphiti`, `get_vision_client()` |
| `query/search.py` | Graphiti search wrapper | `async search(query, num_results=10) -> list[EntityEdge]` |
| `expansion/frontier.py` | Redis frontier operations (add, pick, remove, metadata) | `add_to_frontier()`, `pick_top()`, `remove_from_frontier()` |
| `expansion/policies.py` | Per-resolver depth policies and priority scoring | `compute_priority()`, `DEPTH_POLICIES` dict |
| `expansion/scanner.py` | Celery beat task: scan frontier, enqueue expansions | `scan_frontier()` task |
| `expansion/cli.py` | CLI wrapper for manual expansion | `python -m seed_storage.expansion.cli expand <url>` |
| `ingestion/bot.py` | Discord bot real-time ingestion | `raw_payload` → `enrich_message.delay()` + reaction pubsub |
| `ingestion/batch.py` | DiscordChatExporter JSON import | `raw_payload` → `enrich_message.delay()`, cap 5000/run |
| `worker/app.py` | Celery app + queue routing + beat schedule | Two queues: `raw_messages`, `graph_ingest` |
| `worker/tasks.py` | All Celery tasks: `enrich_message`, `ingest_episode`, `expand_from_frontier`, `scan_frontier` | Central integration point |
| `health.py` | HTTP health endpoint on :8080 | `GET /health` → 200/503 JSON |
| `smoke_test.py` | Post-deploy verification | `python -m seed_storage.smoke_test` |
| `scripts/query.py` | CLI query interface | `python scripts/query.py "query" --limit N` |
| `scripts/rollback.py` | Graph rollback by timestamp | `python scripts/rollback.py --after <timestamp>` |

### Additional files (no Python logic)

| File | Purpose |
|------|---------|
| `Dockerfile` | Python 3.12 + ffmpeg + supervisord + whisper model pre-download |
| `supervisord.conf` | 5 processes: bot + worker-raw + worker-graph + beat + health |
| `manifest.json` | Ant-keeper daemon task manifest |
| `docker-compose.yml` | Local dev: Redis + Neo4j |
| `infra/k8s/neo4j.yaml` | Neo4j StatefulSet + Service for ant-keeper namespace |
| `pyproject.toml` | Dependencies + dev extras + pytest config |
| `.env.example` | All config vars with descriptions and defaults |
| `.gitignore` | Standard Python + .env |

---

## Section 2: Dependency DAG

### Tier 0 — Foundation (10 agents, all start in parallel)

Every Tier 0 agent can implement using only this PARALLEL-SPEC. No agent needs another agent's source code. Where cross-module imports are needed, agents use the typed interfaces defined in Section 1 and stub any not-yet-available modules.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              TIER 0 (parallel)                               │
│                                                                              │
│  types    config    infra    redis-utils    resolvers    graphiti             │
│  agent    agent     agent    agent          agent        agent               │
│                                                                              │
│  frontier    ingestion    alerts    health                                   │
│  agent       agent        agent     agent                                    │
└──────────────────────────────────────┬───────────────────────────────────────┘
                                       │ merge + unit test gate
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                              TIER 1 (sequential)                             │
│                                                                              │
│                            worker-agent                                      │
│                  (Celery app + all 4 task implementations)                   │
└──────────────────────────────────────┬───────────────────────────────────────┘
                                       │ merge + unit test gate
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                              TIER 2 (parallel)                               │
│                                                                              │
│              integration-test-agent        docs-agent                        │
│           (integration + e2e + security)   (README, CLAUDE.md, docs/)        │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Named Work Streams

| Stream | Agent(s) | Tier | Rationale |
|--------|----------|------|-----------|
| Shared types | types-agent | 0 | Simplest module. Completes first, replacing all stubs. |
| Configuration | config-agent | 0 | pydantic-settings; all other agents stub `from seed_storage.config import settings` |
| Infrastructure | infra-agent | 0 | No Python imports; pure config files |
| Redis utilities | redis-utils-agent | 0 | Dedup, circuit breaker, cost tracking, rate limiting — all Redis-backed, independent |
| Content resolution | resolvers-agent | 0 | 8 resolvers + dispatcher. Depends on models.py interface only (stub it). |
| Graph client | graphiti-agent | 0 | Graphiti singleton + query. Depends on config interface only (stub it). |
| Frontier | frontier-agent | 0 | Expansion pipeline. Depends on config + dedup interfaces (stub them). |
| Ingestion | ingestion-agent | 0 | Discord bot + batch. Depends on config + worker task name (stub `enrich_message.delay`). |
| Alerts | alerts-agent | 0 | Notifications + dead letters + replay. Depends on config interface only (stub it). |
| Health | health-agent | 0 | Health endpoint + smoke test. Depends on config + subsystem interfaces (stub them). |
| Worker integration | worker-agent | 1 | Central integration: calls dedup, resolvers, graphiti, frontier, notifications, cost, rate limit. Needs real interfaces. |
| Test integration | integration-test-agent | 2 | Needs all modules merged and working. Real Redis + Neo4j. |
| Documentation | docs-agent | 2 | Needs final architecture to document accurately. |

### Validation check

For each Tier 0 agent: "Can this agent implement with zero information beyond the PARALLEL-SPEC?"

| Agent | External deps needed | How resolved |
|-------|---------------------|--------------|
| types-agent | None | Self-contained dataclass |
| config-agent | None | pydantic-settings, no imports from other seed_storage modules |
| infra-agent | None | Config files only |
| redis-utils-agent | `config.Settings` | Stub: `REDIS_URL` constant |
| resolvers-agent | `enrichment/models.ResolvedContent` | Stub: copy dataclass from Section 1 |
| graphiti-agent | `config.Settings` | Stub: provider constants |
| frontier-agent | `config.Settings`, `dedup.url_hash` | Stub both from Section 1 interfaces |
| ingestion-agent | `config.Settings`, `worker.tasks.enrich_message` | Stub: `enrich_message.delay(payload)` |
| alerts-agent | `config.Settings` | Stub: webhook URL constant |
| health-agent | `config.Settings`, all subsystem clients | Stub: mock all checks |

---

## Section 3: Interface Contracts

### Contract 1: Ingestion → Enrichment (`raw_payload`)

All ingestion sources produce this exact shape. `bot.py`, `batch.py`, and future ingestion modules.

```python
raw_payload: dict = {
    "source_type": str,       # "discord", "slack", "email", "rss", ...
    "source_id": str,         # unique ID within source (Discord snowflake, etc.)
    "source_channel": str,    # channel/folder/feed name
    "author": str,            # display name
    "content": str,           # raw text including URLs
    "timestamp": str,         # ISO 8601 with timezone
    "attachments": list[str], # direct URLs to attached files
    "metadata": dict,         # source-specific extras
}
```

**Error contract:** If `content` is empty AND `attachments` is empty → skip (log DEBUG). If `author` is a bot → skip (log DEBUG).

### Contract 2: Enrichment → Graph ingest (`enriched_payload`)

```python
enriched_payload: dict = {
    "message": raw_payload,                     # original raw_payload, unmodified
    "resolved_contents": list[dict[str, Any]],  # [rc.to_dict() for rc in resolved]
}
```

### Contract 3: Expansion → Graph ingest (`build_content_payload`)

```python
def build_content_payload(resolved: ResolvedContent, meta: dict) -> dict:
    """Build enriched_payload for expansion-discovered content."""
    return {
        "message": {
            "source_type": "expansion",
            "source_id": f"frontier_{meta['url_hash']}",
            "source_channel": meta["source_channel"],
            "author": "system",
            "content": f"Expanded from {meta['discovered_from_url']}",
            "timestamp": meta["discovered_at"],
            "attachments": [],
            "metadata": {
                "frontier_depth": meta["depth"],
                "discovered_from_url": meta["discovered_from_url"],
                "discovered_from_source_id": meta["discovered_from_source_id"],
            },
        },
        "resolved_contents": [resolved.to_dict()],
    }
```

### Contract 4: Worker → Discord bot (reaction events via Redis pubsub)

Channel: `seed:reactions`

```python
reaction_event: dict = {
    "message_id": str,    # Discord snowflake
    "channel_id": str,    # Discord channel ID
    "emoji": str,         # Unicode emoji character
}
```

Workers publish. Bot subscribes. If bot is disconnected, events are dropped silently.

### Contract 5: DedupStore interface

```python
class DedupStore:
    def __init__(self, redis_client: redis.Redis, set_key: str): ...
    def is_seen(self, key: str) -> bool: ...
    def mark_seen(self, key: str) -> None: ...
    def seen_or_mark(self, key: str) -> bool:
        """Atomic SADD. Returns True if already seen."""
        ...
```

Three separate Redis SETs:
- `seed:seen_messages` — member = `{source_type}:{source_id}`
- `seed:seen_urls` — member = SHA256 hex of canonical URL
- `seed:ingested_content` — member = URL hash (tracks graph ingestion, not just resolution)

### Contract 6: CircuitBreaker interface

```python
class CircuitBreaker:
    def __init__(self, redis_client: redis.Redis, service_name: str,
                 failure_threshold: int = 5, cooldown_seconds: int = 300): ...

    def record_success(self) -> None: ...
    def record_failure(self) -> None: ...
    def is_open(self) -> bool: ...
    @property
    def state(self) -> Literal["closed", "open", "half-open"]: ...
```

Redis key: `seed:circuit:{service_name}`. State shared across all workers.

When circuit opens → call `send_alert(...)`. When circuit closes → call `send_alert(...)`.

### Contract 7: CostTracker interface

```python
class CostTracker:
    def __init__(self, redis_client: redis.Redis, daily_budget: float,
                 cost_per_call: float, warning_threshold: float = 0.8): ...

    def increment(self) -> None:
        """Increment daily counter by cost_per_call. Key: seed:cost:daily:YYYY-MM-DD, TTL 48h."""
        ...
    def is_budget_exceeded(self) -> bool: ...
    def is_warning_threshold(self) -> bool: ...
    def get_current_spend(self) -> float: ...
```

### Contract 8: RateLimiter interface

```python
class RateLimiter:
    def __init__(self, redis_client: redis.Redis, key: str, max_per_minute: int): ...
    def allow(self) -> bool:
        """Sliding window check. Returns True if under limit."""
        ...
```

Redis key: `seed:ratelimit:graphiti`.

### Contract 9: Frontier interface

```python
# seed_storage/expansion/frontier.py

def add_to_frontier(redis_client, url_hash: str, priority: float, meta: dict) -> None:
    """ZADD NX to seed:frontier + HSET seed:frontier:meta:{url_hash}."""
    ...

def pick_top(redis_client, batch_size: int, min_threshold: float,
             depth_policies: dict) -> list[dict]:
    """Top N URLs from frontier where score >= threshold and depth within policy."""
    ...

def remove_from_frontier(redis_client, url_hash: str) -> None:
    """ZREM + DEL metadata hash."""
    ...

def get_frontier_meta(redis_client, url_hash: str) -> dict | None:
    """HGETALL seed:frontier:meta:{url_hash}."""
    ...
```

Frontier metadata shape:
```python
frontier_meta: dict = {
    "url": str,
    "discovered_from_url": str,
    "discovered_from_source_id": str,
    "source_channel": str,
    "depth": int,                   # 0 = direct link from message
    "resolver_hint": str,           # expected resolver type
    "discovered_at": str,           # ISO 8601
}
```

### Contract 10: Notifications interface

```python
def send_alert(message: str, debounce_key: str | None = None) -> None:
    """Fire-and-forget Discord webhook alert. Sync httpx.Client. Never raises.
    Empty DISCORD_ALERTS_WEBHOOK_URL → silently skipped (alerts disabled)."""
    ...
```

### Contract 11: Dead letter interface

```python
def dead_letter(task_name: str, payload: dict, exc: Exception, retries: int) -> None:
    """RPUSH to seed:dead_letters. Sanitize traceback (strip credential paths, mask API keys)."""
    ...

def list_dead_letters(redis_client) -> tuple[int, list[dict]]:
    """LRANGE — count + preview without consuming."""
    ...

def replay_one(redis_client) -> tuple[str, dict] | None:
    """LPOP oldest entry. Returns (task_name, payload) or None."""
    ...

def replay_all(redis_client) -> list[tuple[str, dict]]:
    """Pop all entries. Returns list of (task_name, payload)."""
    ...
```

### Contract 12: Graphiti client interface

```python
def get_graphiti() -> Graphiti:
    """Singleton. Calls build_indices_and_constraints() on first init.
    Provider branching: openai→OpenAIClient, anthropic→AnthropicClient, groq→GroqClient.
    Embedder: always OpenAIEmbedder (requires OPENAI_API_KEY regardless of LLM_PROVIDER)."""
    ...

def get_vision_client():
    """Returns SDK client for VISION_PROVIDER (defaults to LLM_PROVIDER).
    Used by image resolver. Separate from Graphiti LLM client."""
    ...
```

All `add_episode()` calls MUST use `group_id="seed-storage"`. Never per-channel.

### Contract 13: Health endpoint

```python
# GET /health → 200 (healthy) or 503 (unhealthy)
# Response body:
{
    "status": "healthy" | "unhealthy",
    "checks": {
        "redis": "ok" | "error",
        "neo4j": "ok" | "error",
        "celery": "ok" | "error",
        "bot": "connected" | "disconnected"
    },
    "details": {
        "raw_messages_queue_depth": int,
        "graph_ingest_queue_depth": int,
        "frontier_size": int,
        "dead_letter_count": int,
        "daily_cost_usd": float,
        "daily_budget_usd": float,
        "messages_seen_total": int,
        "urls_seen_total": int,
        "open_circuit_breakers": list[str]
    }
}
```

### Async/sync boundary

- Resolvers: `async def resolve()` (non-blocking HTTP via httpx).
- Celery tasks: synchronous. Bridge with `asyncio.run()` per-task invocation.
- Do NOT use `--pool=gevent`. Keep default prefork pool.
- `send_alert()`: synchronous `httpx.Client` — no `asyncio.run()`.

---

## Section 4: Graph Schema (Neo4j via Graphiti)

No traditional DDL. Graphiti manages the Neo4j schema via `build_indices_and_constraints()`, called once at startup (idempotent).

**Node types produced by Graphiti's `add_episode()`:**
- `Entity` — named entities extracted by LLM (e.g., "Wyler", "Project Alpha")
- `Episodic` — individual episodes (messages, content) with `source_description` metadata

**Edge types:**
- `RELATES_TO` — between Entity nodes
- `MENTIONS` — from Episodic to Entity

**Key indexes (created by Graphiti):**
- Vector index on Entity embeddings (3072 dim, OpenAI embeddings)
- Fulltext index on Entity names
- Uniqueness constraints on node IDs

**`source_description` format (on Episodic nodes):**
- Message episodes: `"{source_type.title()} #{source_channel}"` → e.g., `"Discord #imessages"`
- Content episodes: `"content_from_{source_type.title()}_#{source_channel}:{content_type}"` → e.g., `"content_from_Discord_#general:youtube"`

**`group_id`:** Always `"seed-storage"` — single unified graph. Never per-channel.

**Anti-fallback rule:** If `add_episode()` fails, do NOT fall back to direct Cypher. Report the failure. Graphiti's entity resolution is the core value — bypassing it produces a graph without cross-episode entity linking.

---

## Section 5: Test Specification

### Test hierarchy — 4 levels

| Level | Dir | Infrastructure | Gate |
|-------|-----|---------------|------|
| Unit | `tests/unit/` | None — zero external dependencies | Tier 0 merge gate |
| Integration | `tests/integration/` | Real Redis + Neo4j (docker-compose) | Tier 1 merge gate |
| E2E | `tests/e2e/` | Full stack (all processes) | Tier 2 gate |
| Security | `tests/security/` | Mixed (some need infra) | Tier 2 gate |

### Unit tests by agent (~185–205 total)

**types-agent:**
- `tests/unit/test_models.py` (~15 tests) — `to_dict`/`from_dict` round-trip, `error_result` factory, datetime serialization, None optionals, JSON-serializable output, missing field errors, extra field tolerance

**config-agent:**
- `tests/unit/test_config.py` (~15 tests) — default values, file-mode credential loading (Discord, Neo4j, webhook), missing credentials → ValueError, LLM_API_KEY resolution per provider, DISCORD_CHANNEL_IDS parsing, TRANSCRIPTION_BACKEND validation, VISION_PROVIDER defaults, env precedence over .env
- `tests/unit/test_logging.py` (~5 tests) — JSON format, required fields present, API keys masked, duration_ms in task logs, no raw secrets at any level

**redis-utils-agent:**
- `tests/unit/test_dedup.py` (~12 tests) — seen_or_mark atomicity, key formats, set isolation, idempotent mark_seen, empty/long key handling
- `tests/unit/test_url_canonicalization.py` (~20 tests) — strip tracking params, normalize scheme/host, preserve path case, sort params, remove fragment/trailing slash, YouTube/Twitter normalization, idempotent, unicode
- `tests/unit/test_circuit_breaker.py` (~12 tests) — state transitions (closed→open→half-open→closed), threshold counting, cooldown timing, success resets counter, independent breakers, Redis key format
- `tests/unit/test_cost_tracking.py` (~10 tests) — increment, daily key format, budget exceeded check, 80% warning threshold, TTL 48h, configurable values, get_current_spend
- `tests/unit/test_rate_limiting.py` (~8 tests) — under/at/over limit, sliding window expiry, config-driven limit, concurrent callers

**resolvers-agent:**
- `tests/unit/resolvers/test_webpage.py` (~8 tests) — trafilatura success, readability fallback, both fail → error_result, truncation at 8000 tokens, expansion_urls, encoding detection, timeout, SSL error
- `tests/unit/resolvers/test_youtube.py` (~8 tests) — metadata extraction, manual captions, auto-caption fallback, transcription fallback, truncation at 12000, timeout, metadata fields, Shorts URL
- `tests/unit/resolvers/test_github.py` (~6 tests) — repo metadata + README, text format, unauth/auth, private repo error, rate limit error
- `tests/unit/resolvers/test_twitter.py` (~2 tests) — stub returns error_result for twitter.com and x.com
- `tests/unit/resolvers/test_image.py` (~5 tests) — vision LLM called, summary+text populated, inaccessible URL, timeout, wrong content-type
- `tests/unit/resolvers/test_pdf.py` (~5 tests) — docling success, unstructured fallback, both fail, truncation at 10000, large PDF timeout
- `tests/unit/resolvers/test_video.py` (~5 tests) — full path (download→ffmpeg→transcribe), temp cleanup, whisper timeout, download fail, unsupported codec
- `tests/unit/resolvers/test_fallback.py` (~4 tests) — HTTP GET + BS4, never raises, timeout → minimal result, malformed HTML
- `tests/unit/test_dispatcher.py` (~15 tests) — URL→resolver routing (all 8 types), priority ordering, multiple URLs independent, exception → error_result

**graphiti-agent:**
- `tests/unit/test_graphiti_client.py` (~9 tests) — provider branching (openai/anthropic/groq), build_indices on init, singleton, group_id enforcement, vision client per provider, VISION_PROVIDER default
- `tests/unit/test_query.py` (~5 tests) — group_ids forwarded, num_results forwarded, EntityEdge→JSON transformation, empty results, error propagation

**frontier-agent:**
- `tests/unit/test_frontier.py` (~15 tests) — add_to_frontier stores correctly, priority scoring (depth penalty, resolver bonus, domain bonus, channel bonus, floor at 0), pick_top sorted/filtered, depth policies respected, ZADD NX semantics, metadata round-trip, remove cleanup, empty frontier, hash consistency, MAX_EXPANSION_BREADTH

**ingestion-agent:**
- `tests/unit/ingestion/test_bot.py` (~10 tests) — configured channel processed, non-configured ignored, empty → skipped, bot author → skipped, raw_payload shape matches contract, source_type/id/channel correct, attachments extracted, metadata includes channel_id/author_id/guild_id
- `tests/unit/ingestion/test_batch.py` (~10 tests) — JSON parsed, raw_payload shape matches, --offset skips, 5000 cap, progress logged, failure log, summary format, empty file, malformed entry → skip+continue, source_type="discord"

**alerts-agent:**
- `tests/unit/test_notifications.py` (~8 tests) — correct POST body, HTTP failure → log WARNING, timeout → log WARNING, debounce (skip within window, send after expiry), no debounce_key → always send, empty webhook URL → skip silently
- `tests/unit/test_dead_letters.py` (~8 tests) — RPUSH stores entry, all required fields, source_id extraction with fallback, list without consuming, replay_one LPOP, replay_all, empty list → None, unknown task_name → WARNING

**health-agent:**
- `tests/unit/test_health.py` (~8 tests) — all pass → 200, Redis/Neo4j/Celery down → 503, response body includes queue depths/frontier/cost/circuit breakers, partial failure → 503, 5s timeout per check

**worker-agent (Tier 1):**
- `tests/unit/tasks/test_enrich_message.py` (~12 tests) — dedup skip, URL extraction, canonicalization, per-URL dedup+cache, enriched_payload shape, reaction events, no URLs (plain text), multiple URLs, partial failure, empty/bot → skip, asyncio.run wrapping
- `tests/unit/tasks/test_ingest_episode.py` (~10 tests) — source_description format (message + content), group_id enforcement, expansion_urls → frontier, cost increment, budget exceeded → sleep+retry, multiple contents, reaction events, empty resolved → message-only, cost_limit_exceeded → dead-letter
- `tests/unit/tasks/test_expand_frontier.py` (~8 tests) — depth ceiling, build_content_payload shape, ingested_content dedup, cache hit, child URLs added, priority computed, breadth limit, remove processed
- `tests/unit/tasks/test_scan_frontier.py` (~5 tests) — FRONTIER_AUTO_ENABLED=false → no-op, picks top batch, respects depth policies, enqueues expand tasks, logs count

### Integration tests (~69 total) — integration-test-agent (Tier 2)

Require real Redis + Neo4j. Marker: `pytest.mark.integration`.

- `test_dedup_redis.py` (~6) — real SADD/SISMEMBER, concurrent access, atomicity, large set, persistence, isolation
- `test_circuit_breaker_redis.py` (~5) — cross-worker state, concurrent failures, cooldown timing, reconnect recovery, KEYS listing
- `test_cost_tracking_redis.py` (~4) — concurrent workers, TTL, parseable float, midnight boundary
- `test_rate_limiting_redis.py` (~4) — real timing, concurrent requests, window expiry, accuracy
- `test_frontier_redis.py` (~6) — ZADD NX, ZRANGEBYSCORE, metadata hash, cleanup, large frontier, score update
- `test_content_cache_redis.py` (~4) — SET+TTL, round-trip, expired → None, miss → None
- `test_reaction_pubsub.py` (~3) — publish→receive, disconnected → dropped, multiple subscribers
- `test_graphiti.py` (~8) — add_episode creates nodes, entity merging (3 episodes → 1 Entity), MENTIONS edges, idempotency, source_description persisted, group_id scoping, build_indices idempotent, search returns results
- `test_celery_tasks.py` (~8) — enrich end-to-end, ingest writes to Neo4j, retry on transient error, dead-letter after max, reject_on_worker_lost, expand task, beat fires, queue routing
- `test_enrichment_pipeline.py` (~6) — full dispatch, multiple URLs, mixed success/failure, cache hit, cache populated, truncation
- `test_notifications_integration.py` (~4) — real POST to mock server, debounce in Redis, debounce expired, connection refused
- `test_dead_letters_redis.py` (~4) — RPUSH+LLEN+LPOP FIFO, concurrent, LRANGE listing, replay round-trip
- `test_health_endpoint.py` (~4) — real HTTP 200, queue depth reflects actual, cost reflects actual, circuit breaker reflects actual
- `test_config_loading.py` (~3) — real env vars, real file credential, .env fallback

### E2E tests (~38 total) — integration-test-agent (Tier 2)

Full stack. All clean up after themselves (test-specific `source_description` prefix + yield teardown).

- `test_message_to_graph.py` (~6) — YouTube, GitHub, image, PDF, multi-URL, plain text
- `test_batch_import.py` (~4) — fixture file, --offset, 5000 cap, mixed types
- `test_query.py` (~3) — search→results, no matches→empty, source_description filtering
- `test_dedup.py` (~4) — same message twice, same URL in two messages, bot+batch overlap, canonical URL matching
- `test_graceful_degradation.py` (~3) — dead URL, all URLs fail, resolver timeout
- `test_source_tracking.py` (~3) — multi-channel source_description, Cypher filter, cross-channel entity merge
- `test_reactions.py` (~3) — pubsub event order, platform emoji, dedup emoji
- `test_frontier_expansion.py` (~4) — expansion_urls appear, auto-scanner processes, manual expansion, depth limit
- `test_circuit_breaker_e2e.py` (~3) — trip→skip→alert, recover→alert, open→error_result+message still ingests
- `test_cost_ceiling.py` (~3) — budget exceeded→pause, retry after delay, 80% warning
- `test_pipeline_restart.py` (~2) — restart→tasks re-queued, dedup survives

### Security tests (~20 total) — integration-test-agent (Tier 2)

- `test_injection.py` (~5) — SQL, XSS, SSTI, oversized payload, unicode edge cases
- `test_credential_isolation.py` (~4) — no keys in startup logs, no keys in task logs, masking format, bot token absent
- `test_dedup_key_isolation.py` (~3) — separate SETs, no message↔URL collision, no URL↔ingested collision
- `test_egress_boundary.py` (~3) — allowlisted domain succeeds, non-allowlisted blocked, internal services accessible
- `test_input_validation.py` (~5) — missing source_type, wrong timestamp type, null content, non-URL attachments, deep metadata

### Test count expectations

| Category | Expected | Gate |
|----------|----------|------|
| Unit | ~185–205 | Tier 0 merge |
| Integration | ~69 | Tier 1 merge |
| E2E | ~38 | Tier 2 |
| Security | ~20 | Tier 2 |
| **Total** | **~310–335** | If <250, investigate silent deselection |

---

## Section 6: Infrastructure

### Dockerfile

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .
RUN python -c "import whisper; whisper.load_model('base')"

COPY seed_storage/ seed_storage/
COPY scripts/ scripts/
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

EXPOSE 8080

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
```

### supervisord.conf

5 processes — bot + worker-raw + worker-graph + beat + health. All log to stdout/stderr. Concurrency read from env vars: `%(ENV_WORKER_CONCURRENCY_RAW)s`, `%(ENV_WORKER_CONCURRENCY_GRAPH)s`.

### manifest.json (ant-keeper daemon)

Task ID: `seed-storage`. Type: `daemon`. Owner: `wyler-zahm`. Health check: `:8080/health`.

Credentials:
- `openai` → `OPENAI_API_KEY` (env-mode, proxy-enabled)
- `discord-bot-seed-storage` → `DISCORD_BOT_TOKEN_PATH` (file-mode)
- `neo4j-seed-storage` → `NEO4J_PASSWORD_PATH` (file-mode)
- `github-pat` → `GITHUB_TOKEN` (env-mode, optional)
- `discord-alerts-webhook` → `DISCORD_ALERTS_WEBHOOK_PATH` (file-mode)

Resources: 1 CPU request / 2 CPU limit, 3Gi memory request / 6Gi limit.

### docker-compose.yml (local dev only)

Redis 7 (port 6379, appendonly) + Neo4j 5 (ports 7474, 7687, auth neo4j/localdev, APOC plugin).

### infra/k8s/neo4j.yaml

StatefulSet + NodePort Service in `ant-keeper` namespace. Bolt 7687 (NodePort 30687), HTTP 7474 (NodePort 30474). PVC 5Gi. APOC plugin. Liveness/readiness probes on HTTP 7474.

### Deploy steps (fresh install)

1. `kubectl apply -f infra/k8s/neo4j.yaml` + wait for ready
2. Change Neo4j default password + store as ant-keeper file-mode credential
3. Store all credentials (openai, discord-bot, neo4j, github-pat, alerts-webhook)
4. Enable proxy targets (openai, github-pat)
5. Register daemon: `POST /api/tasks` with `manifest.json`
6. Verify pod running + check logs
7. Run smoke test
8. Verify real ingestion + emoji reactions

### Rollback path

1. Disable daemon via ant-keeper API
2. Verify pod terminated
3. Optional: `scripts/rollback.py --after <timestamp>` to remove recent episodes
4. Optional: flush dedup sets for re-ingestion

---

## Section 7: Database Migrations

No Alembic. No traditional SQL migrations. Graphiti manages the Neo4j schema:

```python
# In graphiti_client.py, during initialization:
await graphiti.build_indices_and_constraints()
```

This is idempotent — safe to call on every startup. Creates vector indexes, fulltext indexes, and uniqueness constraints. No migration chain, no version identifiers, no upgrade/downgrade functions needed.

**If Neo4j schema changes are needed in the future:** they happen through Graphiti version upgrades, not manual Cypher. The `build_indices_and_constraints()` call handles it.

---

## Section 8: Agent Assignment Matrix

**Every file mapped to exactly one agent. Test files assigned to the same agent as implementation.**

### Tier 0 Agents

#### types-agent

| File | Action |
|------|--------|
| `seed_storage/enrichment/__init__.py` | Create |
| `seed_storage/enrichment/models.py` | Create |
| `tests/unit/test_models.py` | Create |

#### config-agent

| File | Action |
|------|--------|
| `seed_storage/__init__.py` | Create |
| `seed_storage/config.py` | Create |
| `pyproject.toml` | Create |
| `.env.example` | Create |
| `tests/unit/test_config.py` | Create |
| `tests/unit/test_logging.py` | Create |

#### infra-agent

| File | Action |
|------|--------|
| `Dockerfile` | Create |
| `supervisord.conf` | Create |
| `manifest.json` | Create |
| `docker-compose.yml` | Create |
| `infra/k8s/neo4j.yaml` | Create |
| `.gitignore` | Create |

#### redis-utils-agent

| File | Action |
|------|--------|
| `seed_storage/dedup.py` | Create |
| `seed_storage/circuit_breaker.py` | Create |
| `seed_storage/cost_tracking.py` | Create |
| `seed_storage/rate_limiting.py` | Create |
| `tests/unit/test_dedup.py` | Create |
| `tests/unit/test_url_canonicalization.py` | Create |
| `tests/unit/test_circuit_breaker.py` | Create |
| `tests/unit/test_cost_tracking.py` | Create |
| `tests/unit/test_rate_limiting.py` | Create |

#### resolvers-agent

| File | Action |
|------|--------|
| `seed_storage/enrichment/resolvers/__init__.py` | Create |
| `seed_storage/enrichment/resolvers/base.py` | Create |
| `seed_storage/enrichment/resolvers/webpage.py` | Create |
| `seed_storage/enrichment/resolvers/youtube.py` | Create |
| `seed_storage/enrichment/resolvers/image.py` | Create |
| `seed_storage/enrichment/resolvers/pdf.py` | Create |
| `seed_storage/enrichment/resolvers/github.py` | Create |
| `seed_storage/enrichment/resolvers/video.py` | Create |
| `seed_storage/enrichment/resolvers/twitter.py` | Create |
| `seed_storage/enrichment/resolvers/fallback.py` | Create |
| `seed_storage/enrichment/dispatcher.py` | Create |
| `tests/unit/resolvers/test_webpage.py` | Create |
| `tests/unit/resolvers/test_youtube.py` | Create |
| `tests/unit/resolvers/test_image.py` | Create |
| `tests/unit/resolvers/test_pdf.py` | Create |
| `tests/unit/resolvers/test_github.py` | Create |
| `tests/unit/resolvers/test_video.py` | Create |
| `tests/unit/resolvers/test_twitter.py` | Create |
| `tests/unit/resolvers/test_fallback.py` | Create |
| `tests/unit/test_dispatcher.py` | Create |

#### graphiti-agent

| File | Action |
|------|--------|
| `seed_storage/graphiti_client.py` | Create |
| `seed_storage/query/__init__.py` | Create |
| `seed_storage/query/search.py` | Create |
| `scripts/query.py` | Create |
| `tests/unit/test_graphiti_client.py` | Create |
| `tests/unit/test_query.py` | Create |

#### frontier-agent

| File | Action |
|------|--------|
| `seed_storage/expansion/__init__.py` | Create |
| `seed_storage/expansion/frontier.py` | Create |
| `seed_storage/expansion/policies.py` | Create |
| `seed_storage/expansion/scanner.py` | Create |
| `seed_storage/expansion/cli.py` | Create |
| `tests/unit/test_frontier.py` | Create |

#### ingestion-agent

| File | Action |
|------|--------|
| `seed_storage/ingestion/__init__.py` | Create |
| `seed_storage/ingestion/bot.py` | Create |
| `seed_storage/ingestion/batch.py` | Create |
| `tests/unit/ingestion/__init__.py` | Create |
| `tests/unit/ingestion/test_bot.py` | Create |
| `tests/unit/ingestion/test_batch.py` | Create |

#### alerts-agent

| File | Action |
|------|--------|
| `seed_storage/notifications.py` | Create |
| `seed_storage/worker/__init__.py` | Create |
| `seed_storage/worker/dead_letters.py` | Create |
| `seed_storage/worker/replay.py` | Create |
| `tests/unit/test_notifications.py` | Create |
| `tests/unit/test_dead_letters.py` | Create |

#### health-agent

| File | Action |
|------|--------|
| `seed_storage/health.py` | Create |
| `seed_storage/smoke_test.py` | Create |
| `tests/unit/test_health.py` | Create |

### Tier 1 Agent

#### worker-agent

| File | Action |
|------|--------|
| `seed_storage/worker/app.py` | Create |
| `seed_storage/worker/tasks.py` | Create |
| `tests/unit/tasks/__init__.py` | Create |
| `tests/unit/tasks/test_enrich_message.py` | Create |
| `tests/unit/tasks/test_ingest_episode.py` | Create |
| `tests/unit/tasks/test_expand_frontier.py` | Create |
| `tests/unit/tasks/test_scan_frontier.py` | Create |

### Tier 2 Agents

#### integration-test-agent

| File | Action |
|------|--------|
| `tests/__init__.py` | Create |
| `tests/conftest.py` | Create |
| `tests/unit/__init__.py` | Create |
| `tests/unit/resolvers/__init__.py` | Create |
| `tests/integration/__init__.py` | Create |
| `tests/integration/conftest.py` | Create |
| `tests/integration/test_dedup_redis.py` | Create |
| `tests/integration/test_circuit_breaker_redis.py` | Create |
| `tests/integration/test_cost_tracking_redis.py` | Create |
| `tests/integration/test_rate_limiting_redis.py` | Create |
| `tests/integration/test_frontier_redis.py` | Create |
| `tests/integration/test_content_cache_redis.py` | Create |
| `tests/integration/test_reaction_pubsub.py` | Create |
| `tests/integration/test_graphiti.py` | Create |
| `tests/integration/test_celery_tasks.py` | Create |
| `tests/integration/test_enrichment_pipeline.py` | Create |
| `tests/integration/test_notifications_integration.py` | Create |
| `tests/integration/test_dead_letters_redis.py` | Create |
| `tests/integration/test_health_endpoint.py` | Create |
| `tests/integration/test_config_loading.py` | Create |
| `tests/e2e/__init__.py` | Create |
| `tests/e2e/conftest.py` | Create |
| `tests/e2e/test_message_to_graph.py` | Create |
| `tests/e2e/test_batch_import.py` | Create |
| `tests/e2e/test_query.py` | Create |
| `tests/e2e/test_dedup.py` | Create |
| `tests/e2e/test_graceful_degradation.py` | Create |
| `tests/e2e/test_source_tracking.py` | Create |
| `tests/e2e/test_reactions.py` | Create |
| `tests/e2e/test_frontier_expansion.py` | Create |
| `tests/e2e/test_circuit_breaker_e2e.py` | Create |
| `tests/e2e/test_cost_ceiling.py` | Create |
| `tests/e2e/test_pipeline_restart.py` | Create |
| `tests/security/__init__.py` | Create |
| `tests/security/test_injection.py` | Create |
| `tests/security/test_credential_isolation.py` | Create |
| `tests/security/test_dedup_key_isolation.py` | Create |
| `tests/security/test_egress_boundary.py` | Create |
| `tests/security/test_input_validation.py` | Create |

#### docs-agent

| File | Action |
|------|--------|
| `README.md` | Create |
| `CLAUDE.md` | Create |
| `docs/architecture.md` | Create |
| `docs/resolvers.md` | Create |
| `scripts/rollback.py` | Create |

### Explicit prohibition

**Do not create, modify, or stub files owned by other agents.** If you need a type from another agent's module, use a minimal inline stub documented as `# STUB: replace with {agent-name} implementation` — but ONLY for types listed in Section 1. Do not add new types.

---

## Section 9: Coordination Protocol

### Stub protocol

Tier 0 agents that need cross-module imports create minimal stubs:

```python
# STUB: replace with types-agent implementation
# This stub exists only for development. At merge, types-agent's real
# enrichment/models.py replaces this.
```

**Stubs must replicate the exact interface from Section 3.** Do not add methods, fields, or behaviors not in the spec.

### Shared files

No shared files at Tier 0. Each agent owns its files exclusively (Section 8).

The following files are created at merge time by the integration process, not by any individual agent:
- `tests/__init__.py`, `tests/unit/__init__.py` — created by integration-test-agent at Tier 2
- `tests/conftest.py` — created by integration-test-agent at Tier 2

### Interface freeze

After Tier 0 merge, the interfaces in Section 3 are frozen. Tier 1 (worker-agent) implements against them. If a Tier 0 agent discovers that an interface needs to change, it must:
1. Document the change in its branch README
2. Flag it during the merge review
3. The change is applied to all affected modules during merge

### Merge order (Tier 0)

1. **types-agent** merges first (replaces all stubs)
2. **config-agent** merges second (replaces all config stubs)
3. Remaining Tier 0 agents merge in any order
4. Run `pytest tests/unit/` — must pass 100% before proceeding to Tier 1

### Merge conflict resolution

- **Agent-owned files:** keep agent's version (no overlap by design)
- **`__init__.py` files:** if multiple agents create the same `__init__.py`, keep the one with more content, or merge imports
- **`pyproject.toml`:** config-agent owns this. If other agents need dependencies, note them in their branch README — config-agent's version is canonical

### Forbidden actions

- Do not push to `origin` until after merging to the integration branch
- Do not create files owned by other agents
- Do not add types not defined in Section 1
- Do not hardcode config values — import from `seed_storage.config`
- Do not use `group_id` other than `"seed-storage"` in any Graphiti call
- Do not bypass iron-proxy by hardcoding secrets
- Do not use `--pool=gevent` for Celery workers

### Duplicate dispatch prevention

The orchestration script tracks which agents are assigned via a `CLAIMED_BY` column in the state file. Each agent name maps to exactly one worktree. Do not launch the same agent twice.

---

## Section 10: Orchestration Prompt

### Worktree setup

```bash
# Initialize repo
cd /home/wyler-zahm
git clone https://github.com/Cruse-Control/seed-storage.git seed-storage-v2
cd seed-storage-v2

# Create integration branch
git checkout -b v2/integration

# Per agent: create branch + worktree in sibling directory
for agent in types config infra redis-utils resolvers graphiti frontier ingestion alerts health; do
  git checkout -b "v2/${agent}-agent" v2/integration
  git worktree add "../seed-storage-v2-worktrees/${agent}-agent" "v2/${agent}-agent"
done

# CRITICAL: Copy spec files into each worktree
for agent in types config infra redis-utils resolvers graphiti frontier ingestion alerts health; do
  mkdir -p "../seed-storage-v2-worktrees/${agent}-agent/docs"
  cp docs/PARALLEL-SPEC.md "../seed-storage-v2-worktrees/${agent}-agent/docs/"
done

# Return to integration branch
git checkout v2/integration
```

### Agent launch commands (Tier 0 — all 10 in parallel)

```bash
# types-agent
cd /home/wyler-zahm/seed-storage-v2-worktrees/types-agent
claude --dangerously-skip-permissions \
  --append-system-prompt-file "docs/PARALLEL-SPEC.md" \
  "You are types-agent. Implement enrichment/models.py (ResolvedContent dataclass) and tests/unit/test_models.py (~15 tests). Read Section 1 'Shared Types' and Section 8 'types-agent' for your file list. Follow interfaces exactly. Run pytest tests/unit/test_models.py before committing."

# config-agent
cd /home/wyler-zahm/seed-storage-v2-worktrees/config-agent
claude --dangerously-skip-permissions \
  --append-system-prompt-file "docs/PARALLEL-SPEC.md" \
  "You are config-agent. Implement seed_storage/config.py (pydantic-settings Settings class), pyproject.toml, .env.example, and tests. Read Section 1 'config.py' row, Section 3 for infrastructure constants, and seed-storage-spec-v2.md Appendix C for the full Settings class. Create tests/unit/test_config.py (~15 tests) and tests/unit/test_logging.py (~5 tests). Run pytest tests/unit/ before committing."

# infra-agent
cd /home/wyler-zahm/seed-storage-v2-worktrees/infra-agent
claude --dangerously-skip-permissions \
  --append-system-prompt-file "docs/PARALLEL-SPEC.md" \
  "You are infra-agent. Create all infrastructure files: Dockerfile, supervisord.conf, manifest.json, docker-compose.yml, infra/k8s/neo4j.yaml, .gitignore. Read Section 6 for exact contents. No Python tests — validate YAML/JSON syntax only."

# redis-utils-agent
cd /home/wyler-zahm/seed-storage-v2-worktrees/redis-utils-agent
claude --dangerously-skip-permissions \
  --append-system-prompt-file "docs/PARALLEL-SPEC.md" \
  "You are redis-utils-agent. Implement seed_storage/dedup.py (DedupStore + canonicalize_url + url_hash), seed_storage/circuit_breaker.py (CircuitBreaker), seed_storage/cost_tracking.py (CostTracker), seed_storage/rate_limiting.py (RateLimiter). Create unit tests: test_dedup.py (~12), test_url_canonicalization.py (~20), test_circuit_breaker.py (~12), test_cost_tracking.py (~10), test_rate_limiting.py (~8). Mock Redis in all tests. Run pytest tests/unit/ before committing."

# resolvers-agent
cd /home/wyler-zahm/seed-storage-v2-worktrees/resolvers-agent
claude --dangerously-skip-permissions \
  --append-system-prompt-file "docs/PARALLEL-SPEC.md" \
  "You are resolvers-agent. Implement all files under seed_storage/enrichment/resolvers/ (base.py + 8 resolvers) and seed_storage/enrichment/dispatcher.py. Stub enrichment/models.py with ResolvedContent from Section 1 (comment: STUB). Create tests under tests/unit/resolvers/ (~43 tests) and tests/unit/test_dispatcher.py (~15 tests). Mock all HTTP calls. Twitter resolver is a TODO stub returning error_result(). Run pytest tests/unit/ before committing."

# graphiti-agent
cd /home/wyler-zahm/seed-storage-v2-worktrees/graphiti-agent
claude --dangerously-skip-permissions \
  --append-system-prompt-file "docs/PARALLEL-SPEC.md" \
  "You are graphiti-agent. Implement seed_storage/graphiti_client.py (Graphiti singleton, provider branching, vision client), seed_storage/query/search.py, and scripts/query.py (CLI). Stub config.py with provider constants. Create tests/unit/test_graphiti_client.py (~9 tests) and tests/unit/test_query.py (~5 tests). Mock Graphiti in all tests. Run pytest tests/unit/ before committing."

# frontier-agent
cd /home/wyler-zahm/seed-storage-v2-worktrees/frontier-agent
claude --dangerously-skip-permissions \
  --append-system-prompt-file "docs/PARALLEL-SPEC.md" \
  "You are frontier-agent. Implement seed_storage/expansion/ — frontier.py (Redis frontier ops), policies.py (depth policies + priority scoring), scanner.py (scan_frontier Celery beat task), cli.py (manual expansion CLI). Stub config.py and dedup.url_hash. Create tests/unit/test_frontier.py (~15 tests). Mock Redis. Run pytest tests/unit/ before committing."

# ingestion-agent
cd /home/wyler-zahm/seed-storage-v2-worktrees/ingestion-agent
claude --dangerously-skip-permissions \
  --append-system-prompt-file "docs/PARALLEL-SPEC.md" \
  "You are ingestion-agent. Implement seed_storage/ingestion/bot.py (discord.py bot → raw_payload → enrich_message.delay + reaction pubsub listener) and seed_storage/ingestion/batch.py (DiscordChatExporter JSON import → raw_payload → enrich_message.delay, 5000 cap). Stub worker.tasks.enrich_message.delay(). Create tests/unit/ingestion/test_bot.py (~10) and test_batch.py (~10). Mock discord.py and file I/O. Run pytest tests/unit/ before committing."

# alerts-agent
cd /home/wyler-zahm/seed-storage-v2-worktrees/alerts-agent
claude --dangerously-skip-permissions \
  --append-system-prompt-file "docs/PARALLEL-SPEC.md" \
  "You are alerts-agent. Implement seed_storage/notifications.py (send_alert with sync httpx.Client + Redis debounce), seed_storage/worker/dead_letters.py (dead_letter storage + list + replay logic), seed_storage/worker/replay.py (CLI: --list, --all, --one). Stub config.py. Create tests/unit/test_notifications.py (~8) and test_dead_letters.py (~8). Mock httpx and Redis. Run pytest tests/unit/ before committing."

# health-agent
cd /home/wyler-zahm/seed-storage-v2-worktrees/health-agent
claude --dangerously-skip-permissions \
  --append-system-prompt-file "docs/PARALLEL-SPEC.md" \
  "You are health-agent. Implement seed_storage/health.py (HTTP :8080 health endpoint checking Redis, Neo4j, Celery, bot status + queue depths, frontier, cost, circuit breakers) and seed_storage/smoke_test.py (10-step post-deploy verification). Stub all subsystem clients. Create tests/unit/test_health.py (~8 tests). Run pytest tests/unit/ before committing."
```

### Tier 0 merge sequence

```bash
cd /home/wyler-zahm/seed-storage-v2
git checkout v2/integration

# 1. Merge types-agent first (replaces all stubs)
git merge v2/types-agent --no-edit
pytest tests/unit/test_models.py

# 2. Merge config-agent second
git merge v2/config-agent --no-edit
pytest tests/unit/test_config.py tests/unit/test_logging.py

# 3. Merge remaining agents (any order — no file conflicts by design)
for agent in infra redis-utils resolvers graphiti frontier ingestion alerts health; do
  git merge "v2/${agent}-agent" --no-edit
done

# 4. Remove all STUB comments (find and verify none remain)
grep -rn "# STUB:" seed_storage/ && echo "STUBS REMAIN — resolve before proceeding" || echo "All stubs replaced"

# 5. Full unit test gate
pytest tests/unit/ -v
# Expected: ~185-205 tests passing
```

### Tier 1 launch (worker-agent)

```bash
git checkout -b v2/worker-agent v2/integration
git worktree add "../seed-storage-v2-worktrees/worker-agent" v2/worker-agent
cp docs/PARALLEL-SPEC.md "../seed-storage-v2-worktrees/worker-agent/docs/"

cd /home/wyler-zahm/seed-storage-v2-worktrees/worker-agent
claude --dangerously-skip-permissions \
  --append-system-prompt-file "docs/PARALLEL-SPEC.md" \
  "You are worker-agent (Tier 1). All Tier 0 modules are merged. Implement seed_storage/worker/app.py (Celery app + queue routing + beat schedule) and seed_storage/worker/tasks.py (enrich_message, ingest_episode, expand_from_frontier, scan_frontier). Import real modules — do not stub. Read Section 3 contracts and seed-storage-spec-v2.md Appendix C worker sections. Create tests/unit/tasks/test_enrich_message.py (~12), test_ingest_episode.py (~10), test_expand_frontier.py (~8), test_scan_frontier.py (~5). Run pytest tests/unit/ before committing."
```

### Tier 1 merge

```bash
cd /home/wyler-zahm/seed-storage-v2
git checkout v2/integration
git merge v2/worker-agent --no-edit
pytest tests/unit/ -v
# Expected: all unit tests passing
```

### Tier 2 launch (parallel: integration-test-agent + docs-agent)

```bash
# Integration tests
git checkout -b v2/integration-test-agent v2/integration
git worktree add "../seed-storage-v2-worktrees/integration-test-agent" v2/integration-test-agent
cp docs/PARALLEL-SPEC.md "../seed-storage-v2-worktrees/integration-test-agent/docs/"

cd /home/wyler-zahm/seed-storage-v2-worktrees/integration-test-agent
claude --dangerously-skip-permissions \
  --append-system-prompt-file "docs/PARALLEL-SPEC.md" \
  "You are integration-test-agent (Tier 2). All modules are merged. Create tests/conftest.py (shared fixtures, no infra deps), tests/integration/conftest.py (Redis+Neo4j fixtures, SEPARATE), and all integration (~69), e2e (~38), and security (~20) tests per Section 5. Integration tests need real Redis+Neo4j (docker-compose up). E2E tests need full stack. Security tests validate boundaries. Run pytest tests/unit/ first to confirm baseline, then pytest tests/integration/ -m integration."

# Docs
git checkout -b v2/docs-agent v2/integration
git worktree add "../seed-storage-v2-worktrees/docs-agent" v2/docs-agent
cp docs/PARALLEL-SPEC.md "../seed-storage-v2-worktrees/docs-agent/docs/"

cd /home/wyler-zahm/seed-storage-v2-worktrees/docs-agent
claude --dangerously-skip-permissions \
  --append-system-prompt-file "docs/PARALLEL-SPEC.md" \
  "You are docs-agent (Tier 2). All modules are merged. Create README.md (setup, usage, architecture, cost, ant-keeper deployment, local dev, batch import, querying), CLAUDE.md (agent instructions, architecture decisions, conventions, known limitations), docs/architecture.md (detailed architecture with diagrams), docs/resolvers.md (how to add a new resolver), and scripts/rollback.py (--after timestamp removes recent episodes). Read the full codebase to document accurately."
```

### Tier 2 merge + final validation

```bash
cd /home/wyler-zahm/seed-storage-v2
git checkout v2/integration
git merge v2/integration-test-agent --no-edit
git merge v2/docs-agent --no-edit

# Final validation
ruff check .
ruff format .
pytest tests/unit/ -v                        # ~185-205 passing
pytest tests/integration/ -m integration -v  # ~69 passing (needs docker-compose up)
pytest tests/e2e/ -v                         # ~38 passing (needs full stack)
pytest tests/security/ -v                    # ~20 passing
```

---

## Section 11: Execution Environment

- **Runs on:** K8s pod in `ant-keeper` namespace (production) or local host (development)
- **Runtime:** Python 3.12 inside Docker container
- **User:** root (Docker default) in production; `wyler-zahm` in local dev
- **Working directory:** `/app` (in container) or project root (local dev)
- **Network context:** K8s cluster networking. Services accessible via `*.ant-keeper.svc` DNS. External HTTP via iron-proxy sidecar.
- **sudo:** Not needed. Docker group access on host for local dev (`docker compose`).
- **Manual steps requiring human:**
  1. Discord bot creation + token (Discord Developer Portal)
  2. Discord channel ID collection (Developer Mode → Copy Channel ID)
  3. Discord webhook creation for alerts (Server Settings → Integrations)
  4. Neo4j password change after initial deployment
  5. Credential storage in ant-keeper (curl commands)
  6. Proxy target enablement (proxy-enable.sh)

**Agents are running on the development host.** Do NOT SSH anywhere. All kubectl commands target the local k3s cluster.

---

## Section 12: Preconditions

### Verify before launching any agent

| Precondition | Verify command | Expected |
|-------------|---------------|----------|
| Python 3.12+ available | `python3 --version` | `3.12.x` |
| uv installed | `uv --version` | Any version |
| Git configured | `git config user.name && git config user.email` | Non-empty |
| GitHub remote accessible | `git ls-remote https://github.com/Cruse-Control/seed-storage.git` | Lists refs |
| k3s running | `kubectl get nodes` | Node in Ready state |
| ant-keeper running | `curl -s http://127.0.0.1:7070/health` | 200 OK |
| Redis running (K8s) | `redis-cli -h 127.0.0.1 -p 30679 PING` | `PONG` |
| Docker available | `docker info` | No errors |
| Claude Code installed | `claude --version` | Any version |

### Verify before Tier 2 (integration tests)

| Precondition | Verify command | Expected |
|-------------|---------------|----------|
| docker-compose Redis + Neo4j running | `docker compose -p seed-storage-dev ps` | Both running |
| Neo4j accessible | `curl -s http://localhost:7474` | 200 |
| Redis accessible | `redis-cli PING` | `PONG` |

### Verify before deployment

| Precondition | Verify command | Expected |
|-------------|---------------|----------|
| Neo4j StatefulSet running | `kubectl get pods -n ant-keeper -l app=neo4j` | Running |
| Neo4j password changed from default | Attempt login with default fails | Auth error |
| Discord bot token stored | `curl -s http://127.0.0.1:7070/api/credentials/discord-bot-seed-storage -H "Authorization: Bearer $ANT_KEEPER_TOKEN"` | 200 |
| OpenAI credential stored + proxy enabled | `curl -s http://127.0.0.1:7070/api/credentials/openai -H "Authorization: Bearer $ANT_KEEPER_TOKEN"` | 200 |
| Neo4j credential stored | `curl -s http://127.0.0.1:7070/api/credentials/neo4j-seed-storage -H "Authorization: Bearer $ANT_KEEPER_TOKEN"` | 200 |

---

## Section 13: Scope Guard

### IN SCOPE (deliverables)

- [ ] `seed_storage/` package — all modules per Section 8
- [ ] `tests/` — unit, integration, e2e, security per Section 5
- [ ] `scripts/` — query.py, rollback.py
- [ ] Infrastructure files — Dockerfile, supervisord.conf, manifest.json, docker-compose.yml, neo4j.yaml
- [ ] Documentation — README.md, CLAUDE.md, docs/architecture.md, docs/resolvers.md
- [ ] Configuration — pyproject.toml, .env.example, .gitignore
- [ ] Linting clean: `ruff check .` and `ruff format .`

### OUT OF SCOPE

- Graph query enrichment (temporal filtering, PageRank, community detection) — Phase B
- MCP server — Phase B
- Richer node types (Source, Concept, Theme, Domain, Question, Gap) — Phase B
- Ant-keeper auto-disable integration — separate work
- Instagram ingestion — separate daemon
- Cross-encoder reranking — not addressed
- Web UI or API server — CLI only
- Conversation threading — Graphiti handles cross-message linking
- Real X/Twitter content extraction — stub only
- CI/CD pipeline — minimum viable (lint + unit on push) noted, not required for Phase A completion
- Migration from existing seed-storage v1 data — not addressed

### Stop condition

Implementation is complete when:
1. All files from Section 8 exist on the integration branch
2. Unit tests pass (~185–205)
3. Integration tests pass with docker-compose (~69)
4. E2E tests pass with full stack (~38)
5. Security tests pass (~20)
6. `ruff check .` clean
7. Smoke test passes inside deployed pod
8. Discord bot connects and processes real messages with emoji reactions

---

## Section 14: Cost Budget

### LLM costs during implementation

| Operation | Model | Estimated calls | Estimated cost |
|-----------|-------|-----------------|----------------|
| 13 agent sessions (Tier 0-2) | Claude (via Claude Code) | ~13 sessions × ~200 msgs | Included in Claude Code subscription |
| Integration test: Graphiti add_episode | gpt-4o-mini | ~50 test episodes | ~$0.02 |
| Integration test: Vision LLM | gpt-4o | ~5 test images | ~$0.05 |
| E2E tests | gpt-4o-mini + gpt-4o | ~30 episodes | ~$0.05 |
| Smoke test | gpt-4o-mini | ~3 episodes | ~$0.01 |
| **Total external API cost** | | | **~$0.15** |

### Runtime costs (post-deployment)

| Component | Daily calls | Cost at gpt-4o-mini |
|-----------|------------|---------------------|
| Entity extraction (messages) | ~500 | ~$0.15 |
| Entity extraction (URLs) | ~750 | ~$0.25 |
| Vision (images) | ~50 est. | ~$0.50 (varies) |
| Embeddings | ~1,250 | ~$0.02 |
| **Total** | **~2,550** | **~$0.92/day** |

### Hard stops

- `DAILY_LLM_BUDGET`: $5.00/day default. Tracked via Redis counter. Exceeded → graph_ingest pauses.
- Batch import: max 5,000 messages per run.
- Expansion depth: max 5 hops (HARD_DEPTH_CEILING).
- Expansion breadth: max 20 child URLs per resolution.
- Rate limit: max 100 `add_episode()` calls per minute.

---

## Section 15: Cross-User Considerations

- **Who runs it:** Deployed by `wyler-zahm`. Both `wyler-zahm` and `flynn-cruse` can query.
- **Owner name format:** Always `wyler-zahm` in manifest, credentials, logs, documentation. Never shortened.
- **File locations:** Repo at `/home/wyler-zahm/seed-storage-v2/`. Worktrees at `/home/wyler-zahm/seed-storage-v2-worktrees/`.
- **K8s namespace:** `ant-keeper` (shared).
- **Redis DB isolation:** Seed-storage uses **DB 2**. Ant-keeper uses DB 0. Critical — without the `/2` suffix, keys collide.
- **Neo4j:** Dedicated StatefulSet, but shares the `ant-keeper` namespace and PVC storage class.
- **Resource impact on flynn-cruse's workloads:** The pod requests 1 CPU / 3Gi memory. On a shared server, this reduces available resources. The Neo4j StatefulSet requests 250m CPU / 512Mi. Combined with existing ant-keeper workloads, verify total resource usage is within server capacity.
- **Credential storage:** All credentials owned by `wyler-zahm` in ant-keeper.
- **git safe.directory:** Not needed — worktrees are under `/home/wyler-zahm/`.

---

## Section 16: Channel and Integration Map

### Discord

| Channel | ID | Bot permission | Purpose |
|---------|----|----|---------|
| (user-specified) | `DISCORD_CHANNEL_IDS` env var | Read Messages, Read History, Add Reactions | Ingestion source channels |
| Alerts channel | Webhook URL via `discord-alerts-webhook` credential | N/A (webhook, not bot) | Circuit breaker, budget, dead-letter alerts |

**Bot:** `AntFarm#7792` (or new bot created per Section 6 operator workflow). Requires `Message Content Intent` enabled.

### External APIs

| Service | Base URL | Credential | Proxy |
|---------|----------|-----------|-------|
| OpenAI | `https://api.openai.com` | `openai` (env-mode) | iron-proxy |
| Anthropic (if used) | `https://api.anthropic.com` | `anthropic` (env-mode) | iron-proxy |
| Groq (if used) | `https://api.groq.com` | `groq` (env-mode) | iron-proxy |
| GitHub API | `https://api.github.com` | `github-pat` (env-mode, optional) | iron-proxy |
| AssemblyAI (if used) | `https://api.assemblyai.com` | `assemblyai` (env-mode) | iron-proxy |
| Discord Gateway | `gateway.discord.gg` | `discord-bot-seed-storage` (file-mode) | Passes through iron-proxy for HTTP upgrade |
| Discord API | `discord.com/api/*` | Same bot token | iron-proxy for REST calls |

### Internal services

| Service | Address | Protocol | Auth |
|---------|---------|----------|------|
| Redis | `redis://redis.ant-keeper.svc:6379/2` | Redis protocol | None |
| Neo4j (Bolt) | `bolt://neo4j.ant-keeper.svc:7687` | Bolt | neo4j + file-mode password |
| Neo4j (HTTP) | `http://neo4j.ant-keeper.svc:7474` | HTTP | Same |
| Ant-keeper API | `http://127.0.0.1:7070` | HTTP | Bearer token |

### MCP servers

None required for Phase A. MCP server integration is Phase B.

---

## Section 17: Documentation Updates

| Document | Specific changes | Agent |
|----------|-----------------|-------|
| `README.md` | Full project README: setup (ant-keeper + local dev), architecture overview, batch import usage, query CLI, configuration reference, cost information, troubleshooting | docs-agent |
| `CLAUDE.md` | Agent instructions: architecture decisions, credential model, resolver quirks, Celery tuning, config deviations, known limitations, anti-fallback rule | docs-agent |
| `docs/architecture.md` | Detailed architecture: component diagram, data flow, queue design, frontier expansion, dedup model, circuit breakers, cost tracking | docs-agent |
| `docs/resolvers.md` | How to add a new content resolver: BaseResolver interface, registration in dispatcher, test expectations, allowed_hosts update | docs-agent |
| `.env.example` | Every config var with description and default value | config-agent |

Documentation updates are a **required implementation phase** (Tier 2), not a follow-up task.

---

## Post-Implementation Checklist

Before declaring implementation complete:

- [ ] All files from Section 8 exist on the integration branch
- [ ] Unit tests pass: `pytest tests/unit/` (~185–205 tests)
- [ ] Integration tests pass: `pytest tests/integration/ -m integration` (~69 tests, needs docker-compose)
- [ ] E2E tests pass: `pytest tests/e2e/` (~38 tests, needs full stack)
- [ ] Security tests pass: `pytest tests/security/` (~20 tests)
- [ ] Linting clean: `ruff check . && ruff format --check .`
- [ ] No STUB comments remaining: `grep -rn "# STUB:" seed_storage/` returns nothing
- [ ] Smoke test passes in deployed pod
- [ ] Discord bot connects and processes real messages
- [ ] Emoji reactions appear (📥 → ⚙️ → 🏷️ → 🧠 + platform emojis)
- [ ] Health endpoint responds 200 at `:8080/health`
- [ ] All 5 supervisord processes running
- [ ] Cost within budget: `redis-cli -n 2 GET seed:cost:daily:$(date +%Y-%m-%d)` < $5.00
- [ ] Documentation complete: README, CLAUDE.md, docs/architecture.md, docs/resolvers.md
- [ ] Git state clean: feature branches merged, integration branch pushed

### What this spec typically misses (fill before implementation)

1. **DISCORD_CHANNEL_IDS** — must be provided by operator before deployment
2. **LLM provider choice** — default is OpenAI; if Anthropic/Groq, need dual credentials
3. **Existing Neo4j data** — this is a clean-room build; no migration from v1
4. **`ruff` baseline** — run `ruff check .` early; fix before it accumulates
5. **GitHub Actions** — minimum viable CI (lint + unit on push) noted but not required for Phase A completion

---

## Appendix: The 14 Anti-Patterns (applied to seed-storage)

| # | Anti-Pattern | Prevention in this spec |
|---|-------------|----------------------|
| 1 | Spec files missing from worktrees | Orchestration script explicitly copies PARALLEL-SPEC.md into each worktree |
| 2 | Async/sync driver mismatch | Section 3 documents: resolvers are async, Celery tasks bridge with `asyncio.run()`, `send_alert()` is sync |
| 3 | Deployment not specced | Section 6 has full Dockerfile, supervisord.conf, manifest.json, deploy steps, rollback |
| 4 | Breaking existing functionality | This is a clean-room build — no existing v2 functionality to break |
| 5 | Production hotfixes not backported | Coordination protocol: no direct-to-production edits |
| 6 | `patch_deployment_image` destroying specs | Single Dockerfile, no partial patches |
| 7 | Multiple agents stubbing shared types | Section 9: only stub types from Section 1; types-agent merges first |
| 8 | Module-level imports blocking mocking | Config via pydantic-settings (env override); Redis clients injected |
| 9 | Tests passing green while testing nothing | Section 5 specifies exact test assertions per file |
| 10 | Agent SSH-ing to the server it's already on | Section 11: "Agents are running on the development host. Do NOT SSH anywhere." |
| 11 | Pushing before merging | Section 9: "Do not push to origin until after merging to integration branch" |
| 12 | Truncating output without summary | Agent prompts: report test counts explicitly |
| 13 | Pausing for confirmation on spec-defined decisions | Agent prompts run with `--dangerously-skip-permissions` |
| 14 | Doc updates not in spec phases | Section 17: docs-agent is a required Tier 2 agent |
