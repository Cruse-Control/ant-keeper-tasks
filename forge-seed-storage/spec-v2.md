# Seed Storage Replacement — Implementation Spec

> Rebuilt from the original agent build prompt, structured against the [Spec-Writing Guide v2](personas/spec-guide-global-v2-created-2026-04-12.md).
> Created: 2026-04-12. Last updated: 2026-04-12.

---

## 1. Problem & Scope

### System boundary statement

**Seed-storage ingests messages from Discord, resolves linked content (URLs, video, images, PDFs), and stores extracted entities and relationships in a knowledge graph.** It does NOT answer user questions, run agentic workflows, manage external subscriptions, or send notifications. The frontier-based expansion system discovers and expands links found within resolved content to enrich the graph — not to perform external actions. Query CLI supports Graphiti's full-text and vector search for validation only — graph algorithms, temporal filtering, and cross-source reasoning belong in the MCP server layer (Phase B).

### Problem statement

Seed-storage needs a robust pipeline for ingesting messages, resolving linked content, and building a knowledge graph. Key requirements that drive the design:

- **Neo4j must be used as a graph database, not just a vector store.** Entity resolution, cross-source linking, and relationship extraction are the core value — not just embedding storage.
- **X/Twitter extraction must work.** Readability-based scraping fails against X's aggressive blocking. A dedicated resolver with fallback strategies is needed.
- **The enrichment pipeline must be resilient.** Circuit breakers, dedup, and graceful degradation when upstream APIs fail are non-negotiable.
- **The codebase must be modular and testable.** Decoupled stages, comprehensive tests, easy to extend with new content types.

This spec defines a clean-room build using open-source tooling: discord.py for ingestion, Celery+Redis for queuing, a modular content enrichment layer, and Graphiti for LLM-powered knowledge graph construction over Neo4j.

### In scope (Phase A — this spec)

- Real-time Discord message ingestion via bot
- Batch import from DiscordChatExporter JSON
- Content enrichment for: webpages, YouTube, images, PDFs, GitHub repos, generic video, fallback HTML. **X/Twitter: TODO** — stub resolver only (URL detection + platform emoji, no content extraction). X's anti-scraping makes reliable extraction a separate effort.
- Deduplication (message-level and URL-level)
- Graphiti-based knowledge graph ingestion (Entity + Episodic nodes via `add_episode()`)
- **Channel-based source tracking** via `source_description` on Episodic nodes — enables queries like "show me iMessage data discussing X" vs "show me Granola meeting notes about X"
- **Discord reaction emoji status** — bot adds reactions to messages to indicate pipeline stage (📥 staged, ⚙️ processed, 🏷️ enriched, 🧠 loaded, ❌ failed, 🔁 deduped) and detected platform (📸 Instagram, 🎬 YouTube, 🐦 X/Twitter, 📦 GitHub, 🌐 Web)
- Two-queue Celery architecture with independent worker pools
- Query interface for searching the graph
- **Frontier-based expansion** — discovered URLs stored in a priority-scored frontier; expanded on-demand (auto-policy, manual trigger, or future MCP tool) without blocking ingestion
- **Source-agnostic ingestion contract** — `raw_payload` is source-independent. Discord is the first source; the same pipeline supports future sources (Slack, email, RSS) by adding new ingestion modules that produce the same `raw_payload` shape.

### Out of scope

- **Graph query enrichment.** Temporal filtering, graph algorithms (PageRank, community detection), cross-source reasoning, APOC/GDS — these are the features that justify a graph database over pgvector. They belong in a Phase B focused on query capabilities, not ingestion.
- **Richer node types.** Graphiti's `add_episode()` produces Entity, Episodic, RELATES_TO, and MENTIONS. The epistemic schema (Source, Concept, Theme, Domain, Question, Gap, etc.) from the original design was never wired in. Don't re-attempt it here — resolve at the Graphiti layer or in Phase B.
- **Ant-keeper integration.** The circuit-breaker-to-ant-keeper auto-disable feature (task health → auto-disable + notify) is an ant-keeper-level feature, not a seed-storage one. This spec implements internal circuit breakers; ant-keeper integration is separate work.
- **Instagram ingestion.** The video analyzer daemon (instagram-video-analyzer-mcp) is separate infrastructure. This spec's `VideoResolver` handles generic video files but does not replace the Instagram-specific flow.
- **Cross-encoder reranking.** Not addressed here.
- **Web UI or API server.** Query is CLI-only in Phase A.
- **Conversation threading as explicit grouping.** This system relies on Graphiti's automatic entity resolution to link related content across messages. Explicit thread grouping is not needed — Graphiti's 3-tier entity dedup (embedding similarity → fuzzy string → LLM) handles cross-message linking.

### Phase naming

- **Phase A** — This spec. Ingestion pipeline + enrichment + graph storage. "Data in."
- **Phase A-ops** — This spec. Operational alerting: circuit breaker events, budget warnings, and dead-letter notifications pushed to Discord via webhook. "Operations." Ships with Phase A — not a separate phase.
- **Phase B** — Graph query capabilities, temporal filtering, graph algorithms, MCP server updates. "Data out." **Out of scope for this spec.** Depends on Phase A being stable.

### Feature optionality

- **Expansion**: The frontier auto-scanner is optional, controlled by `FRONTIER_AUTO_ENABLED` (default: true). Manual/programmatic expansion always works regardless. Per-resolver depth policies control how deep auto-expansion goes.
- **Batch import**: Optional mode alongside real-time. Both must work independently.
- **Graph backend**: Neo4j is the graph backend. No alternative backends are supported.

### Cost envelope

Graphiti makes ~1 LLM call per episode for entity extraction. Estimated daily volume at ~500 Discord messages/day with ~1.5 URLs each:

| Component | Daily calls | Cost at gpt-4o-mini |
|---|---|---|
| Entity extraction (messages) | ~500 | ~$0.15 |
| Entity extraction (content) | ~750 | ~$0.25 |
| Vision (images, per `VISION_PROVIDER`/`VISION_MODEL`) | ~50 (est.) | ~$0.50 (varies by provider) |
| Embeddings | ~1,250 | ~$0.02 |
| **Total** | **~2,550** | **~$0.92/day** |

Local Whisper transcription is free but CPU-intensive (~2–5 min per hour of audio on modern CPU).

**Cost guardrails are mandatory** — see Section 5.

### Overkill check

- **Two Celery queues:** At ~500 messages/day, a single queue could handle the load. However, the separation exists because `graph_ingest` is LLM-bound (slow, expensive) and `raw_messages` is lightweight routing (fast, cheap). Expansion results feed into `graph_ingest` — no separate queue needed since expansion is triggered by a periodic scanner, not recursive task chaining. A single queue would force both profiles to share concurrency settings. Justified.
- **Supervisord inside a single container:** An alternative is separate K8s Deployments per process. At this scale, a single container with supervisord is simpler to deploy and debug. If scaling becomes necessary (e.g., graph_ingest becomes a bottleneck), split the workers into separate Deployments then — not preemptively.
- **Frontier-based expansion:** The frontier is a Redis sorted set — near-zero overhead to write during ingestion. The expansion scanner is a periodic Celery beat task — one config line. The real cost is resolver HTTP calls and LLM ingestion, both of which are rate-limited. Low infrastructure cost; high option value (can expand any URL at any future time).
- **Redis-backed circuit breakers:** Could be in-memory per-worker instead. But with multiple workers, in-memory state doesn't propagate — worker A could keep hitting a dead API while worker B's circuit is open. Redis sharing is necessary for correctness.

### Daily cost ceiling

**`DAILY_LLM_BUDGET`**: Maximum dollar spend on LLM calls per day. Default: `$5.00`. Tracked via Redis counter (`seed:cost:daily:{YYYY-MM-DD}`) incremented by `ESTIMATED_COST_PER_CALL` (default: `$0.0004`, tunable) after each `add_episode()` call. Counter TTL: 48 hours. Resets naturally at midnight UTC (new date key).

**Budget-exceeded behavior:** When budget exceeded, `ingest_episode` tasks sleep for `min(900, seconds_until_midnight_utc)` then retry. After `max_retries` (5), task is dead-lettered with error `cost_limit_exceeded`. Log at WARNING when 80% of budget consumed; log at CRITICAL and pause when exceeded.

**Batch imports share the daily budget.** Running batch import with `--offset` multiple times in one day accumulates against the same counter. Estimate before starting: `message_count × avg_urls × ESTIMATED_COST_PER_CALL`.

---

## 2. Architecture & Interfaces

### Component diagram

```
Discord / Future Sources
  └── ingestion module (bot.py, batch.py, future: slack.py, ...)
        │
        ├──[raw_messages queue]──► enrich_message() worker
        │                                │
        │                          ContentDispatcher
        │                          (per URL/attachment)
        │                                │
        │                          Resolution Cache
        │                          (seed:content:{hash})
        │                                │
        │                          ResolvedContent
        │                                │
        │                  ┌─────────────┴──────────────┐
        │                  │                            │
        │        [graph_ingest queue]           Frontier
        │                  │                  (seed:frontier)
        │        ingest_episode() worker    priority-scored URLs
        │                  │                      │
        │             Graphiti              ┌─────┴──────┐
        │        add_episode() × N         │             │
        │                  │          auto-scanner   on-demand
        │            Neo4j          (celery beat)  (task/CLI)
        │                                  │             │
        │                                  └──────┬──────┘
        │                                         │
        │                              expand_from_frontier()
        │                              resolve → ingest → discover
        │                              └──► [graph_ingest queue]
        │
  Batch import (ingestion/batch.py)
        └── same raw_payload → enrich_message.delay()
```

Two Celery queues with separate worker pools:
- `raw_messages` — high throughput, lightweight validation and routing
- `graph_ingest` — slower, LLM calls via Graphiti. Both ingestion and expansion feed into this queue.

Expansion is NOT a separate queue. The frontier scanner and on-demand expansion tasks resolve content and enqueue results into `graph_ingest` — the same path as primary ingestion.

**Async/sync boundary:** Resolvers are `async def resolve()` (for non-blocking HTTP via httpx). Celery tasks are synchronous. Each Celery task must bridge the gap with `asyncio.run()` to call async resolvers. Do NOT use `--pool=gevent` (gevent monkey-patching conflicts with asyncio). Keep the default prefork pool and use `asyncio.run()` per-task invocation. This is the simplest correct approach at this scale.

### Shared types — canonical location

**All shared types live in `seed_storage/enrichment/models.py`.** Every other module imports from here. No module recreates these types.

```python
# seed_storage/enrichment/models.py — canonical source for all shared types

@dataclass
class ResolvedContent:
    source_url: str
    content_type: str           # Literal["webpage", "youtube", "video", "image", "pdf", "github", "tweet", "unknown"]
    title: str | None
    text: str                   # clean extracted text, always populated if resolution succeeded; empty string on failure
    transcript: str | None      # for video/audio content
    summary: str | None         # populated by vision LLM for images
    expansion_urls: list[str]   # secondary URLs found within this content
    metadata: dict[str, Any]    # source-specific extras (author, duration, publish_date, etc.)
    extraction_error: str | None  # None on success, error message on failure
    resolved_at: datetime       # UTC, set by dispatcher after resolution completes

    def to_dict(self) -> dict[str, Any]: ...

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResolvedContent": ...

    @classmethod
    def error_result(cls, url: str, error: str) -> "ResolvedContent":
        """Factory for failed resolutions. text='', extraction_error=error."""
        ...
```

### Data shapes at integration boundaries

**Ingestion → Celery (`raw_payload`):** This is the contract between ingestion and enrichment. All ingestion sources must produce this exact shape. `bot.py`, `batch.py`, and any future ingestion modules (Slack, email, RSS) produce this contract.

```python
raw_payload: dict = {
    "source_type": str,       # "discord", "slack", "email", "rss", ...
    "source_id": str,         # unique ID within the source (Discord snowflake, Slack ts, email message-id)
    "source_channel": str,    # channel/folder/feed name — used to build source_description for Graphiti
    "author": str,            # display name
    "content": str,           # raw text including URLs
    "timestamp": str,         # ISO 8601 with timezone
    "attachments": list[str], # direct URLs to attached files
    "metadata": dict,         # source-specific extras (e.g., discord: {channel_id, author_id, guild_id}, slack: {thread_ts, team_id})
}
```

**Enrichment → Graph ingest (`enriched_payload`):** This is the contract between the enrichment worker and the graph ingest worker.

```python
enriched_payload: dict = {
    "message": raw_payload,                          # original raw_payload, unmodified — source-agnostic
    "resolved_contents": list[dict[str, Any]],       # [rc.to_dict() for rc in resolved]
}
```

**Redis cache shape:** Serialized `ResolvedContent` stored at `seed:content:{url_hash}` with 7-day TTL. Value is JSON (via `ResolvedContent.to_dict()`). Consumers must use `ResolvedContent.from_dict()` to deserialize — never construct manually from cached JSON.

**Graphiti return types:** `graphiti.add_episode()` returns `AddEpisodeResults` (created/updated nodes and edges). The return value is logged at DEBUG but not used — Graphiti handles entity resolution internally. `graphiti.search()` returns `list[EntityEdge]`. The `query/search.py` module passes these through directly.

**DedupStore keys — three separate Redis SETs with distinct purposes (do not conflate):**
- **Message dedup:** `seed:seen_messages` — member = `{source_type}:{source_id}` (e.g., `discord:123456789012345678`). Checked by `enrich_message` to skip duplicate messages. Prevents re-enriching the same message from bot + batch overlap.
- **URL resolution dedup:** `seed:seen_urls` — member = SHA256 hex digest of canonical URL. Checked by `enrich_message` to skip re-resolving URLs already seen. A URL in `seen_urls` means it has been resolved (cached in `seed:content:{hash}`) but does NOT guarantee it was ingested into the graph.
- **Content ingestion dedup:** `seed:ingested_content` — member = URL hash. Checked by `expand_from_frontier` to skip content already ingested into the graph. A URL can be in `seen_urls` (resolved during enrichment) but NOT in `ingested_content` if the `ingest_episode` task failed. The expansion path catches this — it re-ingests orphaned content.

**Expansion content payload shape (`build_content_payload`):**

When `expand_from_frontier` needs to ingest resolved content, it constructs an `enriched_payload` that matches the same contract as primary ingestion:

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

This produces a synthetic `raw_payload` with `source_type="expansion"` so expanded content is distinguishable from primary ingestion in logs and graph queries. The `source_channel` is inherited from the original message that triggered discovery.

### Data quality requirements

- **Empty messages:** Skip messages where `content` is empty AND `attachments` is empty. Log at DEBUG.
- **Bot/automated messages:** For Discord, skip bot accounts (`message.author.bot == True`). Other sources define their own skip logic in their ingestion module. Log at DEBUG.
- **Minimum content length:** `ResolvedContent.text` shorter than 50 characters (after stripping whitespace) is still ingested but logged at WARNING as low-quality. Do not silently drop it — Graphiti may still extract entities.
- **Truncation:** Each resolver has a token limit (webpage: 8000, youtube transcript: 12000, pdf: 10000). Truncated content must append `\n[Content truncated at {limit} tokens]` so downstream consumers know.
- **Encoding:** All text must be UTF-8. Resolvers must handle encoding detection (chardet/charset-normalizer) and convert. Mojibake is a data quality bug.

### Channel source tracking via `source_description`

Graphiti's `group_id` creates **fully isolated namespaces** — entities in different groups never merge. This is NOT what we want. We want a unified knowledge graph where "Wyler" merges across all sources.

Instead, use a **single `group_id`** (e.g., `"seed-storage"`) for all episodes, and differentiate source via `source_description`:

```python
# Message episode
await graphiti.add_episode(
    name=f"{source_type}_{source_id}",
    episode_body=message_text,
    source_description=f"{source_type.title()} #{source_channel}",  # e.g., "Discord #imessages", "Slack #engineering", "Email inbox"
    reference_time=message_timestamp,
    source=EpisodeType.message,
    group_id="seed-storage",
)

# Content episode (resolved URL)
await graphiti.add_episode(
    name=f"content_{url_hash[:12]}",
    episode_body=resolved_text,
    source_description=f"content_from_{source_type.title()}_#{source_channel}:{content_type}",  # e.g., "content_from_Discord_#general:youtube"
    reference_time=message_timestamp,
    source=EpisodeType.text,
    group_id="seed-storage",
)
```

**Querying by source:** `source_description` is stored as metadata on Episodic nodes. To query "iMessage data discussing X":
```python
# Search all sources, then filter by source_description
results = await graphiti.search("X", group_ids=["seed-storage"])
imessage_results = [r for r in results if "imessages" in r.source_description]
```

Or via Cypher for more precise filtering:
```cypher
MATCH (ep:Episodic)-[:MENTIONS]->(e:Entity)
WHERE ep.source_description STARTS WITH 'Discord #imessages'
AND e.name CONTAINS 'X'
RETURN ep, e
```

**Why not `group_id` per channel:** Using separate `group_id` per channel would prevent entity merging across channels. If "Wyler" is mentioned in both `#imessages` and `#granola-meeting-notes`, they'd be two separate Entity nodes. The whole point of the knowledge graph is cross-source entity resolution.

### Discord reaction emojis

The bot adds reaction emojis to Discord messages to indicate pipeline status and detected content types. This provides visual feedback in Discord.

**Pipeline stage reactions (added progressively):**

| Emoji | Meaning | When added |
|---|---|---|
| 📥 | Message staged (received by bot) | Immediately on message receipt, before `enrich_message.delay()` |
| ⚙️ | Processing (enrichment started) | `enrich_message` task begins |
| 🏷️ | Enriched (content resolved) | `enrich_message` task completes |
| 🧠 | Loaded (ingested into graph) | `ingest_episode` task completes |
| ❌ | Failed | Any task permanently fails (dead-lettered) |
| 🔁 | Deduped (already seen) | `enrich_message` detects duplicate `source_type:source_id` |

**Platform detection reactions (added alongside 🏷️):**

| Emoji | Platform | Trigger |
|---|---|---|
| 📸 | Instagram | URL matches `instagram.com` |
| 🎬 | YouTube | URL matches `youtube.com`, `youtu.be` |
| 🐦 | X/Twitter | URL matches `twitter.com`, `x.com` |
| 📦 | GitHub | URL matches `github.com` |
| 🌐 | Web (generic) | Any other resolved URL |

**Implementation:** The bot holds a reference to the `discord.Message` object. Celery tasks cannot add reactions directly (they don't have Discord API access). Instead, use a callback mechanism:
- `enrich_message` publishes reaction events to a Redis pubsub channel (`seed:reactions`).
- The bot process subscribes to this channel and adds reactions via `message.add_reaction()`.
- If the bot is disconnected when a reaction event arrives, the reaction is dropped silently (non-critical).

**Reaction pubsub message format (JSON):**
```python
reaction_event: dict = {
    "message_id": str,    # Discord snowflake of the original message
    "channel_id": str,    # Discord channel ID (needed to fetch the message object)
    "emoji": str,         # Unicode emoji character (e.g., "📥", "⚙️", "🎬")
}
```

**Permissions required:** The bot needs `Add Reactions` permission in addition to the read permissions from Section 6.

**Source-specificity:** Reaction emojis are Discord-specific feedback. Other sources will need their own feedback mechanisms (e.g., Slack thread replies, email read receipts). The reaction pubsub pattern (`seed:reactions`) can be extended per-source, but only Discord is implemented in Phase A.

### Frontier-based expansion

Expansion is decoupled from ingestion. During ingestion, discovered `expansion_urls` are written to the frontier — they are NOT automatically resolved or ingested.

**Frontier data model:**

```python
# Redis sorted set: seed:frontier
# Member = canonical URL hash (same as resolution cache key)
# Score = priority (float, higher = process sooner)

# Redis hash per URL: seed:frontier:meta:{url_hash}
frontier_meta: dict = {
    "url": str,                    # original URL
    "discovered_from_url": str,    # parent URL that contained this link
    "discovered_from_source_id": str,  # source_id of the message that started the chain
    "source_channel": str,         # channel where the original message appeared
    "depth": int,                  # hops from original message (0 = direct link, 1 = link within resolved content, ...)
    "resolver_hint": str,          # expected resolver type based on URL pattern
    "discovered_at": str,          # ISO 8601
}
```

**Priority scoring:**

```python
def compute_priority(url: str, depth: int, source_channel: str, resolver_hint: str) -> float:
    base = 10.0
    # Depth penalty: deeper = lower priority
    base -= depth * 2.0
    # Resolver type bonus
    resolver_bonus = {"github": 5, "youtube": 3, "twitter": 3, "webpage": 1, "pdf": 0}
    base += resolver_bonus.get(resolver_hint, 0)
    # Domain reputation (extensible)
    domain_bonus = {"arxiv.org": 4, "github.com": 3, "nature.com": 3, "medium.com": 1}
    base += domain_bonus.get(extract_domain(url), 0)
    # Source channel bonus (configurable)
    # e.g., links from #research get +2, #random gets +0
    base += channel_priority.get(source_channel, 0)
    return max(base, 0.0)  # floor at 0
```

**Per-resolver depth policies:**

| Resolver type | Auto-expand max depth | Rationale |
|---|---|---|
| GitHub | 1 | README content is valuable; linked repos/deps are not |
| YouTube | 0 | Description links rarely worth chasing |
| Twitter/X (TODO) | 1 | Tweet → linked article (stub resolver — no expansion until resolver implemented) |
| Webpage (arxiv, papers) | 2 | Paper → references → key citations |
| Webpage (generic) | 0 | Don't auto-spider |
| PDF | 0 | Links in PDFs rarely worth chasing |

Auto-scanner skips URLs where `depth > resolver_policy[resolver_hint]`. Manual expansion overrides this (up to hard ceiling of 5).

**Three expansion triggers:**

1. **Auto-scanner** (Celery beat, every `FRONTIER_SCAN_INTERVAL` seconds): Picks top `FRONTIER_BATCH_SIZE` URLs from frontier where `score >= FRONTIER_AUTO_THRESHOLD` and `depth <= resolver_policy`. Enqueues `expand_from_frontier.delay(url_hash)` for each.

2. **Programmatic** (Celery task, called from anywhere): `expand_from_frontier.delay(url_hash, max_depth=3)` — expands a specific URL to a given depth, overriding auto-policy. Used by future MCP tools, ant-keeper triggers, etc.

3. **CLI** (thin wrapper): `python -m seed_storage.expansion.cli expand "github.com/org/repo" --depth 3` — calls the Celery task. Also supports `--dry-run`, `--min-priority`, `--from-source-id`.

**Expansion flow:**

```python
def expand_from_frontier(url_hash, max_depth=None):
    meta = get_frontier_meta(url_hash)
    if meta["depth"] > (max_depth or HARD_DEPTH_CEILING):
        return  # too deep

    # Resolve (from cache or fresh)
    resolved = resolve_or_cache(meta["url"])

    # Ingest content episode (if not already ingested)
    if not is_content_ingested(url_hash):
        ingest_episode.delay(build_content_payload(resolved, meta))
        mark_content_ingested(url_hash)

    # Discover new URLs and add to frontier
    for child_url in resolved.expansion_urls[:MAX_EXPANSION_BREADTH_PER_RESOLVE]:
        child_hash = hash_url(child_url)
        child_meta = {
            "url": child_url,
            "discovered_from_url": meta["url"],
            "discovered_from_source_id": meta["discovered_from_source_id"],
            "source_channel": meta["source_channel"],
            "depth": meta["depth"] + 1,
            "resolver_hint": guess_resolver(child_url),
            "discovered_at": utcnow(),
        }
        priority = compute_priority(child_url, child_meta["depth"], ...)
        add_to_frontier(child_hash, priority, child_meta)  # ZADD NX — don't overwrite if already in frontier

    # Remove processed URL from frontier
    remove_from_frontier(url_hash)
```

---

## 3. Naming Conventions

**All modules reference this section. Pin these conventions and don't deviate.**

- **Owner names:** Always full usernames — `wyler-zahm`, `flynn-cruse`. Never shortened (`wyler`, `flynn`). Applies to: task manifest `owner`, credential `owner`, log output, documentation, all user-facing strings.
- **Credential IDs:** kebab-case — `discord-bot-seed-storage`, `neo4j-seed-storage`, `openai`, `github-pat`, `assemblyai`.
- **Environment variable names:** `UPPER_SNAKE_CASE` — `OPENAI_API_KEY`, `DISCORD_BOT_TOKEN_PATH`, `NEO4J_PASSWORD_PATH`.
- **File credential injection:** Use explicit `_PATH` suffix — `DISCORD_BOT_TOKEN_PATH`, `NEO4J_PASSWORD_PATH`. Never `_FILE` or `_SECRET`.
- **Task ID:** kebab-case, descriptive — `seed-storage`.
- **Redis key namespace:** `seed:` prefix, colon-separated hierarchy. Keys: `seed:seen_messages`, `seed:seen_urls`, `seed:ingested_content`, `seed:content:{hash}`, `seed:frontier`, `seed:frontier:meta:{hash}`, `seed:circuit:{service}`, `seed:cost:daily:{date}`, `seed:ratelimit:graphiti`, `seed:reactions`.
- **Celery task names:** Full module path — `seed_storage.worker.tasks.enrich_message`, `seed_storage.worker.tasks.ingest_episode`, `seed_storage.worker.tasks.expand_from_frontier`.
- **Log fields:** snake_case — `source_id`, `source_type`, `stage`, `status`, `queue`, `worker_id`, `duration_ms`.
- **Graph identifiers:** `group_id` = `"seed-storage"` (single value, never per-channel). Episode `name` = `"{source_type}_{source_id}"` for messages, `"content_{url_hash[:12]}"` for resolved content.

---

## 4. Infrastructure Constants

**Single source of truth. Every module reads from `config.py`. No module hardcodes its own values.**

Seed-storage runs as an ant-keeper daemon (K8s Deployment in the `ant-keeper` namespace). Infrastructure services are K8s services in the same namespace.

| Constant | Value | Notes |
|---|---|---|
| Redis URL | `redis://redis.ant-keeper.svc:6379/2` | **DB 2** — ant-keeper uses DB 0. Shared Redis instance. |
| Neo4j bolt URI | `bolt://neo4j.ant-keeper.svc:7687` | K8s Service (see Section 7 for StatefulSet manifest) |
| Neo4j browser | `http://neo4j.ant-keeper.svc:7474` | In-cluster only; NodePort 30474 for host access |
| Neo4j auth | `neo4j` / (from file-mode credential) | Password read from `/run/credentials/neo4j-seed-storage/password` |
| Celery broker | Same as Redis URL | Shared Redis instance, DB 2 |
| Message dedup set key | `seed:seen_messages` | Redis SET, persistent (Redis has AOF enabled) |
| URL dedup set key | `seed:seen_urls` | Redis SET, persistent (Redis has AOF enabled) |
| Daily cost counter prefix | `seed:cost:daily:` | Redis STRING, key per date (YYYY-MM-DD), TTL 48h |
| Content cache prefix | `seed:content:` | Redis STRING, TTL 7 days |
| Content cache TTL | `604800` seconds (7 days) | |
| Reaction pubsub channel | `seed:reactions` | Workers publish, bot subscribes; used for Discord emoji feedback |
| Worker concurrency: raw_messages | `8` | Lightweight routing. Defined in `config.py` as `WORKER_CONCURRENCY_RAW`. Supervisord reads via `%(ENV_WORKER_CONCURRENCY_RAW)s`. |
| Worker concurrency: graph_ingest | `2` | LLM-bound. Defined in `config.py` as `WORKER_CONCURRENCY_GRAPH`. |
| Semaphore limit | `10` | Max concurrent enrichment HTTP requests |
| Frontier sorted set | `seed:frontier` | Redis ZSET, score = priority. Persistent. |
| Frontier metadata prefix | `seed:frontier:meta:` | Redis HASH per URL, stores discovery context |
| Ingested content set | `seed:ingested_content` | Redis SET, tracks which URL hashes have content episodes |
| Frontier auto-scan interval | `300` seconds (5 min) | Celery beat period for frontier scanner |
| Frontier auto-threshold | `5` | Minimum priority score for auto-expansion |
| Health check port | `8080` | Required by ant-keeper for liveness probe |
| Notification debounce prefix | `seed:notify:debounce:` | Redis STRING, TTL 300s, prevents duplicate Discord webhook messages |
| Dead-letter list | `seed:dead_letters` | Redis LIST, persistent. Failed task payloads stored for replay. |

**Redis DB isolation:** Ant-keeper's own Redis (log streams, internal state) uses DB 0. Seed-storage uses **DB 2** to avoid key collisions. The `/2` suffix in the Redis URL is critical — without it, seed-storage's `seed:*` keys and Celery's internal keys would share DB 0 with ant-keeper.

**Graph backend vs Redis clarification:** Neo4j is the graph backend. Redis is used for Celery broker, dedup, and caching only. They are separate services — do not confuse them.

---

## 5. Error Handling & Failure Modes

### Per-component failure behavior

| Component | Failure | Behavior | Caller sees |
|---|---|---|---|
| Discord bot | WebSocket disconnect | discord.py auto-reconnects with backoff | Logged WARNING; no message loss (Discord replays missed events on reconnect) |
| Discord bot | Channel not found / no permission | Skip channel, log ERROR with channel ID | Other channels continue |
| `enrich_message` worker | Single URL resolution fails | `ResolvedContent` with `extraction_error` set, `text=""` | `ingest_episode` receives partial enrichment — whatever succeeded |
| `enrich_message` worker | All URLs fail | Enriched payload with empty `resolved_contents` but message still ingests | Message episode still created in graph (plain text value) |
| `enrich_message` worker | Redis down | Celery task fails, retried per retry policy | Task re-queued after delay |
| `ingest_episode` worker | Graphiti `add_episode()` fails | Task retried per retry policy; after max retries, payload stored in `seed:dead_letters` Redis list + Discord webhook alert | Message recoverable via `python -m seed_storage.worker.replay` |
| `ingest_episode` worker | LLM API rate limit (429) | Retry with exponential backoff (handled by Celery retry) | Delayed ingestion |
| `ingest_episode` worker | LLM API down (5xx) | Retry with backoff; circuit breaker trips after threshold | Queue pauses until circuit resets |
| Content resolver | HTTP timeout | Return `ResolvedContent.error_result()` | Logged WARNING, enrichment continues |
| Content resolver | SSL error | Return `ResolvedContent.error_result()` | Same |
| Whisper (local) | OOM / crash | Task fails, retried once; if second attempt fails, skip transcription, ingest metadata only | Logged ERROR |
| Neo4j | Connection refused | `ingest_episode` fails, retried | All graph_ingest tasks queue up; raw_messages + enrichment continue |
| Redis | Down completely | **ALL Celery queues block. Entire pipeline non-functional.** | No messages processed. Bot continues receiving but `enrich_message.delay()` fails. |
| Redis | AOF corrupted / data loss | Dedup sets (`seed:seen_messages`, `seed:seen_urls`) lost; all messages re-processed on restart | Duplicate graph episodes; increased LLM cost. Recovery: re-run batch import to rebuild dedup state, or accept duplicates (Graphiti entity resolution handles entity-level dedup). |

Failure modes are per-source. Future sources add their own rows to this table.

**Redis is a single point of failure.** If Redis goes down, the entire pipeline stops — queue broker, dedup, circuit breakers, cost limits, and content cache all depend on it. Recovery: restart Redis. Tasks in-flight when Redis crashed are re-queued automatically (`task_reject_on_worker_lost=True`). Add `socket_connect_timeout=10, socket_timeout=5` to the Redis client configuration so tasks fail fast on Redis latency rather than hanging.

### Partial failure policy

**Batch of URLs in a single message:** Skip failed URLs, continue with the rest. A message with 5 URLs where 3 fail still produces a message episode + 2 content episodes. Never halt a batch for a single URL failure.

**Batch import (thousands of messages):** Continue on per-message failure. Log failed message IDs to a file (`seed_storage_failed_imports_{timestamp}.jsonl`) for replay. Report summary at end: `Imported 2847/3000 messages. 153 failures logged to {path}.`

### Retry policy

| Task | Retried exceptions | Max retries | Backoff | Delay |
|---|---|---|---|---|
| `enrich_message` | `httpx.TransportError`, `httpx.TimeoutException`, `redis.ConnectionError` | 3 | Exponential | 10s, 30s, 90s |
| `ingest_episode` | `httpx.TransportError`, `neo4j.exceptions.ServiceUnavailable`, `openai.RateLimitError` | 5 | Exponential | 15s, 45s, 135s, 405s, 1215s |

Use Celery's `autoretry_for` and `retry_backoff` decorators. `task_reject_on_worker_lost = True` ensures tasks aren't silently acked if a worker crashes.

### Dead-letter storage and replay

Celery+Redis has no built-in dead-letter queue. When a task exhausts retries, implement explicit dead-lettering:

**On final failure** (in each task's `on_failure` handler or via `after_return` signal when `state == FAILURE`):

```python
def dead_letter(task_name: str, payload: dict, exc: Exception, retries: int):
    """Store failed task payload for later replay."""
    entry = json.dumps({
        "task_name": task_name,       # e.g., "seed_storage.worker.tasks.ingest_episode"
        "payload": payload,           # original task args (enriched_payload or raw_payload)
        "error": str(exc),
        "traceback": traceback.format_exc(),
        "retries_exhausted": retries,
        "failed_at": datetime.utcnow().isoformat(),
        "source_id": payload.get("message", {}).get("source_id", "unknown"),
    })
    redis_client.rpush("seed:dead_letters", entry)
```

**Replay CLI:**

```bash
# List dead-lettered tasks (count + preview):
python -m seed_storage.worker.replay --list

# Replay all dead-lettered tasks (pops from list, re-enqueues via .delay()):
python -m seed_storage.worker.replay --all

# Replay one (pops oldest entry):
python -m seed_storage.worker.replay --one

# Dry-run (list payloads without replaying):
python -m seed_storage.worker.replay --list --verbose
```

`replay` pops entries from `seed:dead_letters` via `LPOP` and calls the original task's `.delay()` with the stored payload. If replay itself fails (e.g., Redis down), the entry is lost — but the operator is running this interactively and will see the error. For safety, `--all` logs each replayed `source_id` and prints a summary: `"Replayed 12 tasks. 0 failures."`.

**Dead-letter list is unbounded.** Monitor via health check (`dead_letter_count` in response). If the list grows large, investigate root cause before replaying — mass replay against a still-broken service wastes LLM budget.

**Traceback sanitization:** `dead_letter()` stores `traceback.format_exc()` which may include credential file paths (`/run/credentials/...`) or partial secret values in exception messages. Before storing, strip any path matching `/run/credentials/*` and mask any value matching common API key patterns (`sk-*`, `ghp_*`, `ptok_*`) to `***`. Dead-letter entries are stored in Redis and surfaced via the replay CLI — treat them as potentially sensitive.

### Timeout values

| Operation | Timeout | Notes |
|---|---|---|
| HTTP request (resolver) | 30 seconds | Per-request, via httpx client timeout |
| YouTube metadata extraction (yt-dlp) | 60 seconds | Some videos have slow metadata |
| Whisper transcription (local) | 300 seconds (5 min) | Long audio; kill process if exceeded |
| Whisper API / AssemblyAI | 120 seconds | Network-bound |
| Vision LLM call | 60 seconds | Image analysis |
| `graphiti.add_episode()` | 120 seconds | LLM entity extraction |
| Full `enrich_message` task | 600 seconds (10 min) | Hard ceiling per message |
| Full `ingest_episode` task | 300 seconds (5 min) | Hard ceiling per episode batch |

### Circuit breakers

Implement a simple circuit breaker per external service (LLM API, GitHub API, and future resolvers like Twitter):

```python
class CircuitBreaker:
    """Tracks consecutive failures. Opens after threshold, resets after cooldown."""
    failure_threshold: int = 5
    cooldown_seconds: int = 300  # 5 minutes
    state: Literal["closed", "open", "half-open"]
```

When a circuit opens:
1. Log CRITICAL: `"Circuit breaker OPEN for {service}. {failure_count} consecutive failures. Cooldown: {cooldown}s."`
2. **Send Discord alert** via webhook (see Section 7, Alertable events):
   > **🔴 Circuit Breaker Open** — `{service}` is not responsive. {failure_count} consecutive failures. Paused for {cooldown}s.
3. Tasks that hit an open circuit skip the affected operation (not the whole task) and set `extraction_error` accordingly.
4. After cooldown, one request is allowed through (half-open). If it succeeds, circuit closes and sends a recovery alert. If it fails, circuit re-opens.

When a circuit closes (recovery):
- Log INFO: `"Circuit breaker CLOSED for {service}. Service recovered."`
- Send Discord alert: **🟢 Circuit Breaker Recovered** — `{service}` is responsive again.

State is stored in Redis (`seed:circuit:{service_name}`) so all workers share circuit state.

**Webhook alerts are fire-and-forget.** If the Discord webhook POST fails, log WARNING and continue — circuit breaker behavior must not depend on webhook availability. Use `httpx` with a 5-second timeout and no retries.

### Cost protection

- **LLM call rate limit:** Max 100 `add_episode()` calls per minute across all workers. Enforced via Redis-based sliding window counter (`seed:ratelimit:graphiti`). When limit is hit, tasks sleep and retry rather than failing.
- **Batch size cap:** Batch import processes max 5,000 messages per run. If the export file is larger, log a warning and stop at 5,000 with instructions to re-run with `--offset`.
- **Expansion depth limits:** Per-resolver depth policies (see Section 2) control how deep auto-expansion goes. No single URL chain can exceed depth 5 (hard-coded ceiling). Manual expansion via `expand_from_frontier` task respects the same ceiling.

### Rollback path

If this feature breaks production, execute from the **host**:

```bash
# 1. Disable the new daemon (ant-keeper stops the pod)
curl -X PATCH http://127.0.0.1:7070/api/tasks/seed-storage \
  -H "Authorization: Bearer $ANT_KEEPER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'

# 2. Verify pod is terminated
kubectl get pods -n ant-keeper -l ant-keeper/task-id=seed-storage

# 3. Roll back graph data (delete episodes created after deployment)
kubectl exec -it deployment/ant-keeper-seed-storage -n ant-keeper -- \
  python scripts/rollback.py --after "2026-04-15T00:00:00Z"
# This deletes Episodic nodes by source_description prefix ("Discord message in #", "content_")
# created after the given timestamp. Neo4j data is append-only — this is safe.

# 4. Optionally flush dedup sets to allow re-ingestion
redis-cli -h redis.ant-keeper.svc -n 2 DEL seed:seen_urls seed:seen_messages
```

---

## 6. Security & Networking

### Credential management — iron-proxy model

All credentials are managed by ant-keeper and proxied through the iron-proxy sidecar. **No `.env` files in production.** The task container never sees real API keys — iron-proxy swaps proxy tokens for real secrets on egress.

#### Credential categories

Seed-storage's credentials fall into three categories based on protocol:

**Category 1: HTTP API keys — iron-proxy env-mode (standard path)**

These use HTTP `Authorization` / `x-api-key` headers. Iron-proxy intercepts and swaps proxy tokens for real secrets.

| Credential ID | Env var name | `proxy_target` | Notes |
|---|---|---|---|
| `openai` | `OPENAI_API_KEY` | `https://api.openai.com` | Always required (embeddings). SDK adds `Bearer` — do NOT store with prefix. |
| `anthropic` | `ANTHROPIC_API_KEY` | `https://api.anthropic.com` | Only if LLM_PROVIDER=anthropic |
| `groq` | `GROQ_API_KEY` | `https://api.groq.com` | Only if LLM_PROVIDER=groq |
| `github-pat` | `GITHUB_TOKEN` | `https://api.github.com` | Optional, increases rate limits |
| `assemblyai` | `ASSEMBLYAI_API_KEY` | `https://api.assemblyai.com` | Only if TRANSCRIPTION_BACKEND=assemblyai |

For these: task receives `OPENAI_API_KEY=ptok_openai_xyz...`. Iron-proxy scans outbound requests to `api.openai.com` and replaces the proxy token with the real key in headers. The SDK never knows the difference.

**Category 2: Non-HTTP credentials — file-mode injection**

These use non-HTTP protocols where iron-proxy cannot intercept. The real secret is mounted as a file.

| Credential ID | Env var name | Injection mode | File path | Why not env-mode |
|---|---|---|---|---|
| `discord-bot-seed-storage` | `DISCORD_BOT_TOKEN_PATH` | `file` | `/run/credentials/discord-bot-seed-storage/token` | discord.py sends the token in WebSocket IDENTIFY payload (JSON body), not HTTP headers. Iron-proxy only scans headers. Proxy token would be rejected by Discord gateway. |
| `neo4j-seed-storage` | `NEO4J_PASSWORD_PATH` | `file` | `/run/credentials/neo4j-seed-storage/password` | Bolt protocol, not HTTP. |
| `discord-alerts-webhook` | `DISCORD_ALERTS_WEBHOOK_PATH` | `file` | `/run/credentials/discord-alerts-webhook/url` | Webhook URL contains the token in the URL path (`/api/webhooks/{id}/{token}`). Iron-proxy only swaps header tokens, not URL path segments. File-mode is the only option. |

**Task code must read from file at startup:**
```python
# config.py
DISCORD_BOT_TOKEN: str = ""  # populated from file
NEO4J_PASSWORD: str = ""     # populated from file
DISCORD_ALERTS_WEBHOOK_URL: str = ""  # populated from file; optional — alerts disabled if empty

@model_validator(mode="after")
def _load_file_credentials(self) -> "Settings":
    if path := os.environ.get("DISCORD_BOT_TOKEN_PATH"):
        self.DISCORD_BOT_TOKEN = Path(path).read_text().strip()
    if path := os.environ.get("NEO4J_PASSWORD_PATH"):
        self.NEO4J_PASSWORD = Path(path).read_text().strip()
    if path := os.environ.get("DISCORD_ALERTS_WEBHOOK_PATH"):
        self.DISCORD_ALERTS_WEBHOOK_URL = Path(path).read_text().strip()
    return self
```

**Category 3: No credential required**

| Service | Why no credential | Access method |
|---|---|---|
| Redis | Cluster-internal, no auth configured | `allowed_hosts` in manifest |

#### Why discord.py can't use iron-proxy

This is worth documenting explicitly because it's non-obvious:

1. discord.py's REST API calls (to `discord.com/api/*`) use `Authorization: Bot <token>` header — iron-proxy *could* swap this.
2. But discord.py's gateway WebSocket sends the token in the IDENTIFY payload: `{"op": 2, "d": {"token": "<token>", ...}}` — a JSON body over WSS, not an HTTP header.
3. Iron-proxy only scans HTTP headers (`Authorization`, `x-api-key`, `api-key`). It does not inspect WebSocket frame payloads.
4. discord.py uses the same token object for both REST and WSS — you can't give it a proxy token for REST and a real token for WSS.
5. Therefore: file-mode injection with the real token is the only option.

### Egress allowlist

The ant-keeper manifest's `allowed_hosts` + credential `proxy_target` values define the complete egress allowlist. The task can ONLY reach these hosts.

**HTTP/HTTPS (port 443, handled by iron-proxy):**
- `api.openai.com` (via credential proxy_target)
- `api.anthropic.com` (via credential, if applicable)
- `api.groq.com` (via credential, if applicable)
- `api.github.com` (via credential, if applicable)
- `api.assemblyai.com` (via credential, if applicable)
- `discord.com`, `gateway.discord.gg`, `cdn.discordapp.com` (via `allowed_hosts` — discord.py needs all three)
- Content resolver targets (YouTube, GitHub, Twitter, Wikipedia, etc.) — **curated allowlist** in manifest `allowed_hosts`. URLs on unlisted domains are blocked; resolver returns `error_result()` and message still ingests with plain text. See Section 7 for the initial domain list. New domains added as usage patterns emerge.

**Non-HTTP (via `allowed_hosts` in manifest):**
- `redis.ant-keeper.svc:6379` — Celery broker + dedup
- `neo4j.ant-keeper.svc:7687` — Bolt protocol
- `neo4j.ant-keeper.svc:7474` — HTTP (graph index initialization, browser)
### Credential format

- `DISCORD_BOT_TOKEN`: Raw token string. discord.py handles the `Bot ` prefix internally. Do NOT store with `Bot ` prefix in ant-keeper.
- `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GROQ_API_KEY`: Raw key. SDKs add `Bearer` automatically. Do NOT store with prefix — causes double-prefix 401.
- `GITHUB_TOKEN`: `ghp_*` or `github_pat_*` format. Raw token, no prefix.
- `NEO4J_PASSWORD`: Plain string.

### Dual-key warning (Anthropic/Groq providers)

> **If using Anthropic or Groq as your LLM provider**, you still need the `openai` credential in ant-keeper (for embeddings). Graphiti uses `OpenAIEmbedder` regardless of LLM provider. The ant-keeper manifest must always include `"openai": "OPENAI_API_KEY"` in its credentials dict.
>
> **Vision provider** can differ from `LLM_PROVIDER` via `VISION_PROVIDER`. If `VISION_PROVIDER` uses a different provider than `LLM_PROVIDER`, ensure that provider's credential is also in the manifest and its endpoint is in `allowed_hosts` (or has a `proxy_target`).

### Protocol coverage

- **HTTPS:** All resolver HTTP requests. Iron-proxy terminates TLS (MITM with injected CA cert), scans headers for proxy tokens, forwards to upstream. Task trusts the proxy CA via `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` env vars (auto-injected by ant-keeper).
- **WebSocket (WSS):** Discord gateway. Passes through iron-proxy for the HTTP upgrade, then becomes a raw WebSocket. Token is in payload, not headers — not intercepted.
- **Bolt:** Neo4j. Non-HTTP, bypasses iron-proxy entirely. Unencrypted within cluster (acceptable — same-namespace traffic).
- **Redis protocol:** Non-HTTP, bypasses iron-proxy. No auth, cluster-internal only.

### What is NOT covered

- No authentication on the query CLI — anyone with shell access to the pod can query.
- No encryption at rest for Neo4j or Redis (cluster-internal, acceptable for this use case).
- No audit logging of who queries what.
- URLs on domains not in the curated allowlist will fail resolution silently (by design — see Section 7).

### Operator workflow: setting up the bot

**Prerequisites:** All curl commands below require `$ANT_KEEPER_TOKEN`. This is the ant-keeper admin bearer token, set during ant-keeper initial setup. Retrieve it from `/opt/shared/ant-keeper/.env` on the host: `grep ANT_KEEPER_TOKEN /opt/shared/ant-keeper/.env`. All commands run from the **host** (not inside a pod), unless stated otherwise.

1. Go to [Discord Developer Portal](https://discord.com/developers/applications) → New Application.
2. Bot tab → Reset Token → copy the token.
3. Bot tab → enable "Message Content Intent" (required for reading message text).
4. OAuth2 → URL Generator → select `bot` scope → select permissions: `Read Messages/View Channels`, `Read Message History`, `Add Reactions`.
5. Copy the generated URL, open in browser, select the target server, authorize.
6. Store the token in ant-keeper:
   ```bash
   # Create credential (file-mode for Discord — see why above)
   curl -X POST http://127.0.0.1:7070/api/credentials \
     -H "Authorization: Bearer $ANT_KEEPER_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"id": "discord-bot-seed-storage", "owner": "wyler-zahm", "credential_type": "bearer", "value": "<token>", "injection_mode": "file"}'
   ```
7. Get channel IDs: Discord → User Settings → Advanced → enable Developer Mode. Right-click channel → Copy Channel ID. Set in manifest `env` as `DISCORD_CHANNEL_IDS` (comma-separated): `"DISCORD_CHANNEL_IDS": "123456789012345678,987654321098765432"`.
8. **(Optional) Set up Discord webhook for alerts:** Server Settings → Integrations → Webhooks → New Webhook → copy URL. Store in ant-keeper:
   ```bash
   curl -X POST http://127.0.0.1:7070/api/credentials \
     -H "Authorization: Bearer $ANT_KEEPER_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"id": "discord-alerts-webhook", "owner": "wyler-zahm", "credential_type": "bearer", "value": "<webhook-url>", "injection_mode": "file"}'
   ```
   If skipped, alerts are disabled — pipeline still functions, events are log-only.
9. Enable proxy for HTTP credentials:
   ```bash
   cd /opt/shared/ant-keeper
   ./infra/scripts/proxy-enable.sh openai https://api.openai.com
   # repeat for other HTTP credentials
   ```
10. Deploy via ant-keeper (see Section 7). Verify: check daemon logs for `"Bot connected as {bot_name}. Watching channels: [...]"`.

### Anti-patterns

- **Do NOT paste the Discord bot token into a Discord chat message** — Discord automatically revokes tokens that appear in messages.
- **Do NOT store credentials in manifest `env` field** — use ant-keeper credentials with proper injection mode.
- **Do NOT store API keys with `Bearer`/`Bot` prefix** for SDK-consumed credentials — the SDK adds it, causing double-prefix 401.
- **Do NOT log raw API keys.** Config loading should log `"OPENAI_API_KEY: sk-...{last4}"` format at most.
- **Do NOT bypass iron-proxy** by hardcoding secrets in code, Dockerfiles, or env vars.
- **Do NOT use env-mode for Discord or Neo4j credentials** — non-HTTP protocols, proxy token will be sent as-is and rejected.

---

## 7. Deployment & Operations

Seed-storage deploys as a single ant-keeper **daemon** (K8s Deployment). One container runs all processes internally via supervisord. Infrastructure services (Neo4j) are separate K8s resources in the same namespace. Redis is the existing ant-keeper Redis instance.

### Prerequisites: Neo4j K8s StatefulSet

Neo4j does not exist in the ant-keeper cluster yet. Deploy before the seed-storage daemon:

```yaml
# infra/k8s/neo4j.yaml — apply with: kubectl apply -f infra/k8s/neo4j.yaml
apiVersion: v1
kind: Service
metadata:
  name: neo4j
  namespace: ant-keeper
spec:
  type: NodePort
  ports:
    - name: bolt
      port: 7687
      targetPort: 7687
      nodePort: 30687
    - name: http
      port: 7474
      targetPort: 7474
      nodePort: 30474
  selector:
    app: neo4j
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: neo4j
  namespace: ant-keeper
spec:
  serviceName: neo4j
  replicas: 1
  selector:
    matchLabels:
      app: neo4j
  template:
    metadata:
      labels:
        app: neo4j
    spec:
      containers:
        - name: neo4j
          image: neo4j:5
          ports:
            - containerPort: 7687
            - containerPort: 7474
          env:
            - name: NEO4J_AUTH
              value: "neo4j/changeme"  # WARNING: Change immediately after deployment (step 2 below). Do NOT leave this default.
              # NOTE: Between StatefulSet apply and password change, the default password is active on NodePort 30474.
              # Acceptable risk: cluster-internal + Tailscale network. For hardened environments, use a K8s Secret instead.
            - name: NEO4J_PLUGINS
              value: '["apoc"]'
          volumeMounts:
            - name: neo4j-data
              mountPath: /data
          resources:
            requests:
              cpu: 250m
              memory: 512Mi
            limits:
              cpu: "1"
              memory: 1Gi
          livenessProbe:
            httpGet:
              path: /
              port: 7474
            initialDelaySeconds: 30
            periodSeconds: 15
          readinessProbe:
            httpGet:
              path: /
              port: 7474
            initialDelaySeconds: 10
            periodSeconds: 5
  volumeClaimTemplates:
    - metadata:
        name: neo4j-data
      spec:
        accessModes: ["ReadWriteOnce"]
        resources:
          requests:
            storage: 5Gi
```

After applying, set the Neo4j password and store as ant-keeper credential:
```bash
# Change Neo4j password from default
curl -u neo4j:changeme -X POST http://localhost:30474/user/neo4j/password \
  -H "Content-Type: application/json" \
  -d '{"password": "<new-password>"}'

# Store in ant-keeper (file-mode — Bolt protocol can't use iron-proxy)
curl -X POST http://127.0.0.1:7070/api/credentials \
  -H "Authorization: Bearer $ANT_KEEPER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"id": "neo4j-seed-storage", "owner": "wyler-zahm", "credential_type": "api_key", "value": "<new-password>", "injection_mode": "file"}'
```

### Monolith process management: supervisord

The daemon container runs 5 processes via supervisord:

```ini
# supervisord.conf
[supervisord]
nodaemon=true
logfile=/dev/null
logfile_maxbytes=0

[program:bot]
command=python -m seed_storage.ingestion.bot
autostart=true
autorestart=true
stdout_logfile=/dev/fd/1
stdout_logfile_maxbytes=0
stderr_logfile=/dev/fd/2
stderr_logfile_maxbytes=0

[program:worker-raw]
command=celery -A seed_storage.worker.app worker --queues=raw_messages --concurrency=%(ENV_WORKER_CONCURRENCY_RAW)s --loglevel=info
autostart=true
autorestart=true
stdout_logfile=/dev/fd/1
stdout_logfile_maxbytes=0
stderr_logfile=/dev/fd/2
stderr_logfile_maxbytes=0

[program:worker-graph]
command=celery -A seed_storage.worker.app worker --queues=graph_ingest --concurrency=%(ENV_WORKER_CONCURRENCY_GRAPH)s --loglevel=info
autostart=true
autorestart=true
stdout_logfile=/dev/fd/1
stdout_logfile_maxbytes=0
stderr_logfile=/dev/fd/2
stderr_logfile_maxbytes=0

[program:beat]
command=celery -A seed_storage.worker.app beat --loglevel=info
autostart=true
autorestart=true
stdout_logfile=/dev/fd/1
stdout_logfile_maxbytes=0
stderr_logfile=/dev/fd/2
stderr_logfile_maxbytes=0

[program:health]
command=python -m seed_storage.health
autostart=true
autorestart=true
stdout_logfile=/dev/fd/1
stdout_logfile_maxbytes=0
stderr_logfile=/dev/fd/2
stderr_logfile_maxbytes=0
```

All processes log to stdout/stderr (captured by K8s → ant-keeper log streaming). Supervisord restarts crashed processes automatically. If the pod itself crashes, K8s restarts the entire Deployment.

### Health check endpoint

`seed_storage/health.py` — lightweight HTTP server on port 8080:

```python
"""Health check endpoint for ant-keeper liveness probe."""
# GET /health → 200 if all subsystems are reachable, 503 otherwise
# Response body:
# {
#   "status": "healthy",
#   "checks": {"redis": "ok", "neo4j": "ok", "celery": "ok", "bot": "connected"},
#   "details": {
#     "raw_messages_queue_depth": 12,
#     "graph_ingest_queue_depth": 3,
#     "frontier_size": 47,
#     "dead_letter_count": 0,
#     "daily_cost_usd": 0.42,
#     "daily_budget_usd": 5.00,
#     "messages_seen_total": 2847,
#     "urls_seen_total": 4231,
#     "open_circuit_breakers": []
#   }
# }
# Checks:
#   - Redis: PING
#   - Neo4j: driver.verify_connectivity()
#   - Celery: app.control.inspect().ping() (at least 1 worker responding)
#   - Bot: discord client.is_ready()
# Use aiohttp or simple http.server — no heavy frameworks
```

Ant-keeper probes `GET :8080/health` every 30 seconds. If unhealthy for 3 consecutive probes (90s), K8s restarts the pod.

### Dockerfile

```dockerfile
FROM python:3.12-slim

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy application code
COPY seed_storage/ seed_storage/
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Health check port
EXPOSE 8080

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
```

**Note on local Whisper:** `openai-whisper` pulls ~1GB of model weights on first use. For the Docker image, either:
- Accept the download at runtime (simpler, but slow cold start)
- Pre-download in the Dockerfile: `RUN python -c "import whisper; whisper.load_model('base')"` (larger image, fast start)

Recommendation: pre-download in Dockerfile for production. Add after `RUN pip install`:
```dockerfile
RUN python -c "import whisper; whisper.load_model('base')"
```
The image will be ~2GB but avoids a 5-minute cold start on every pod restart. Ant-keeper builds and stores images in its configured registry.

### Ant-keeper task manifest

```json
{
  "id": "seed-storage",
  "name": "Seed Storage Pipeline",
  "type": "daemon",
  "owner": "wyler-zahm",
  "source": {
    "type": "git",
    "repo": "https://github.com/Cruse-Control/seed-storage",
    "ref": "main"
  },
  "health_check_port": 8080,
  "health_check_path": "/health",
  "credentials": {
    "openai": "OPENAI_API_KEY",
    "discord-bot-seed-storage": "DISCORD_BOT_TOKEN_PATH",
    "neo4j-seed-storage": "NEO4J_PASSWORD_PATH",
    "github-pat": "GITHUB_TOKEN",
    "discord-alerts-webhook": "DISCORD_ALERTS_WEBHOOK_PATH"
  },
  "allowed_hosts": [
    "redis.ant-keeper.svc:6379",
    "neo4j.ant-keeper.svc:7687",
    "neo4j.ant-keeper.svc:7474",
    "discord.com",
    "gateway.discord.gg",
    "cdn.discordapp.com",
    "media.discordapp.net",
    "www.youtube.com",
    "youtu.be",
    "i.ytimg.com",
    "twitter.com",
    "x.com",
    "pbs.twimg.com",
    "nitter.net",
    "raw.githubusercontent.com",
    "en.wikipedia.org",
    "arxiv.org",
    "medium.com",
    "substack.com",
    "news.ycombinator.com",
    "reddit.com",
    "www.reddit.com",
    "docs.google.com"
  ],
  "env": {
    "LLM_PROVIDER": "openai",
    "LLM_MODEL": "gpt-4o-mini",
    "VISION_MODEL": "gpt-4o",
    "TRANSCRIPTION_BACKEND": "whisper-local",
    "DISCORD_CHANNEL_IDS": "FILL_IN_CHANNEL_IDS",
    "REDIS_URL": "redis://redis.ant-keeper.svc:6379/2",
    "NEO4J_URI": "bolt://neo4j.ant-keeper.svc:7687",
    "NEO4J_USERNAME": "neo4j",
    "FRONTIER_AUTO_ENABLED": "true"
  },
  "resources": {
    "cpu": "1",
    "memory": "3Gi",
    "limits_cpu": "2",
    "limits_memory": "6Gi"
  },
  "enabled": true
}
```

**Resource sizing rationale:**
- 3Gi memory request: Whisper base model (~1GB) + Python processes + Celery workers + comfortable headroom
- 6Gi limit: headroom for concurrent enrichment (multiple resolvers, PDF parsing, large documents)
- 1 CPU request / 2 CPU limit: Whisper transcription is CPU-bound; workers are I/O-bound

**If using Anthropic/Groq provider,** add the provider credential to the manifest:
```json
"credentials": {
  "openai": "OPENAI_API_KEY",
  "anthropic": "ANTHROPIC_API_KEY",
  ...
}
```

**Egress allowlist is curated — no wildcard.** Content resolvers can only fetch URLs from domains explicitly listed in the manifest's `allowed_hosts` or credential `proxy_target` values. This is intentional — iron-proxy's egress control is the security boundary.

**Implications:**
- URLs on unlisted domains are blocked by iron-proxy. The resolver gets a connection error, returns `ResolvedContent.error_result()`, and the message still ingests with its plain text. This is the existing graceful degradation path — no new code needed.
- The most valuable content types (YouTube, GitHub, Twitter, major news sites) are known platforms with fixed domains. These go in the initial allowlist.
- Unknown/long-tail domains will fail resolution. This is acceptable — the message text itself still reaches the graph. If a domain appears frequently in logs, add it to the allowlist and redeploy.
- **To add a new domain:** update `allowed_hosts` in the manifest and re-register the task with ant-keeper. Hot-reload — no restart required.

**Initial curated allowlist:** See the `allowed_hosts` array in the manifest above. This is the single canonical copy — do not duplicate. The list will grow over time based on what domains actually appear in Discord messages. Monitor blocked requests in iron-proxy logs (logged at WARN level) to identify candidates.

### Deploy steps (fresh install)

1. **Deploy Neo4j** (if not already running):
   ```bash
   kubectl apply -f infra/k8s/neo4j.yaml
   kubectl wait --for=condition=ready pod -l app=neo4j -n ant-keeper --timeout=120s
   ```
2. **Set Neo4j password and store credential** (see above).
3. **Store all credentials in ant-keeper** (see Section 6 operator workflow):
   - `openai` (env-mode, proxy-enabled)
   - `discord-bot-seed-storage` (file-mode)
   - `neo4j-seed-storage` (file-mode)
   - `github-pat` (env-mode, proxy-enabled, optional)
4. **Enable proxy targets:**
   ```bash
   cd /opt/shared/ant-keeper
   ./infra/scripts/proxy-enable.sh openai https://api.openai.com
   ./infra/scripts/proxy-enable.sh github-pat https://api.github.com
   ```
5. **Register and deploy the daemon:**
   ```bash
   curl -X POST http://127.0.0.1:7070/api/tasks \
     -H "Authorization: Bearer $ANT_KEEPER_TOKEN" \
     -H "Content-Type: application/json" \
     -d @manifest.json
   ```
6. **Verify deployment:**
   ```bash
   kubectl get pods -n ant-keeper -l ant-keeper/task-id=seed-storage
   # Wait for Running state, then check logs:
   curl http://127.0.0.1:7070/api/tasks/seed-storage/logs \
     -H "Authorization: Bearer $ANT_KEEPER_TOKEN"
   ```
7. **Run smoke test** (exec into pod or use ant-keeper run):
   ```bash
   kubectl exec -it deployment/ant-keeper-seed-storage -n ant-keeper -- python -m seed_storage.smoke_test
   ```
8. **Verify real ingestion:** Send a test message in a configured Discord channel. Wait 30s. Query the graph for its content. Verify it appears. Check that emoji reactions (📥 → ⚙️ → 🏷️ → 🧠) appear on the message.

### Upgrade path

1. Push code to `main` branch.
2. Ant-keeper detects git staleness (via `git ls-remote` check) and rebuilds the image automatically on next trigger — or force rebuild:
   ```bash
   curl -X POST http://127.0.0.1:7070/api/tasks/seed-storage/trigger \
     -H "Authorization: Bearer $ANT_KEEPER_TOKEN"
   ```
3. K8s Deployment rolling update replaces the pod. Supervisord restarts all processes with new code. **Note:** With `replicas: 1`, there is a brief gap during rolling update (old pod terminates → new pod starts). This is acceptable — discord.py replays missed events on reconnect, and Celery tasks are persistent in Redis. No messages are lost.
4. Graph schema changes: Graphiti's `build_indices_and_constraints()` runs on startup (idempotent).

**Zero-downtime deployment:** NOT supported in Phase A (single replica). Acceptable because: discord.py replays missed events on gateway reconnect, Celery tasks persist in Redis across restarts, and the brief gap (~10-30s) does not lose data. If zero-downtime is needed later, increase `replicas: 2` and add a leader election mechanism for the Discord bot (only one bot instance should connect at a time).

**Caddy route:** Not required. Seed-storage has no external HTTP endpoints. The health check (`:8080`) is pod-internal (ant-keeper liveness probe). The query CLI runs inside the pod. All access is via `kubectl exec`, ant-keeper API, or direct pod networking.

### Local development (docker-compose)

For local development/testing outside ant-keeper, a `docker-compose.yml` is still provided:

```yaml
services:
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    command: redis-server --appendonly yes

  neo4j:
    image: neo4j:5
    ports: ["7474:7474", "7687:7687"]
    environment:
      NEO4J_AUTH: "neo4j/localdev"
      NEO4J_PLUGINS: '["apoc"]'
```

With a `.env.example` for local overrides:
```bash
REDIS_URL=redis://localhost:6379/0
NEO4J_URI=bolt://localhost:7687
NEO4J_PASSWORD=localdev
DISCORD_BOT_TOKEN=<raw-token-for-local-dev>
# ... etc
```

Config uses `pydantic-settings` which reads from env vars first, then `.env` file as fallback. In ant-keeper deployment, env vars are injected by the manifest — no `.env` file needed.

### Monitoring / observability

- **Ant-keeper dashboard:** task status, logs, run history via ant-keeper API
- **Pod logs:** `kubectl logs deployment/ant-keeper-seed-storage -n ant-keeper -f`
- **Health check:** `curl http://<pod-ip>:8080/health` (or via ant-keeper log stream)
- **Celery inspect** (exec into pod): `celery -A seed_storage.worker.app inspect active`
- **Redis metrics:** `redis-cli -n 2 SCARD seed:seen_urls` (unique URLs), `redis-cli -n 2 SCARD seed:seen_messages` (unique messages), `redis-cli -n 2 DBSIZE` (total keys in DB 2)
- **Neo4j browser:** `http://localhost:30474` (via NodePort)
- **Circuit breaker status:** `redis-cli -n 2 KEYS "seed:circuit:*"` then `GET` each

Log format: structured JSON with fields: `timestamp`, `level`, `source_id` (the correlation key — e.g., Discord snowflake), `source_type`, `stage` (`enrich`|`ingest`|`expand`|`bot`), `status` (`start`|`retry`|`error`|`done`), `queue`, `worker_id`, `duration_ms`, `message`. Use Python `logging` with a JSON formatter. All Celery tasks log start/end with duration. Supervisord captures all process stdout/stderr → K8s pod logs → ant-keeper log stream.

**Tracing a message through the pipeline:** `kubectl logs ... | grep source_id=123456789` shows the full flow: bot → enrich → ingest → expand.

### Alertable events and Discord webhook alerts

All alertable events are both logged (structured JSON) AND pushed to a Discord channel via webhook. Seed-storage owns its own alerting — ant-keeper is not involved. Ant-keeper separately monitors the top-level task (last 5 log lines on failure, reported by queen ant).

**Webhook setup:** Create a Discord webhook in the alerts channel (Server Settings → Integrations → Webhooks). Store the webhook URL as an ant-keeper file-mode credential (`discord-alerts-webhook`). If no webhook is configured, alerts are log-only — the pipeline still functions.

| Log level | Event | Meaning | Discord alert |
|---|---|---|---|
| CRITICAL | `circuit_breaker_open` | External service down, tasks pausing | **Push immediately.** 🔴 "Circuit breaker OPEN for {service}. {failure_count} consecutive failures." |
| CRITICAL | `daily_budget_exceeded` | LLM spend exceeded `DAILY_LLM_BUDGET` | **Push immediately.** 🔴 "Daily LLM budget exceeded (${amount}/${budget}). Graph ingest paused until midnight UTC." |
| ERROR | `ingest_episode_dead_lettered` | Message permanently failed after max retries | **Push immediately.** 🟠 "Message {source_id} permanently failed after {max_retries} retries. Manual replay required." |
| ERROR | `neo4j_connection_refused` | Graph database unreachable | **Debounce (5 min).** 🟠 "Neo4j unreachable." |
| WARNING | `budget_80_percent` | 80% of daily LLM budget consumed | **Debounce (1 hour).** 🟡 "80% of daily LLM budget consumed (${amount}/${budget})." |
| INFO | `circuit_breaker_closed` | Service recovered from outage | **Push immediately.** 🟢 "Circuit breaker CLOSED for {service}. Service recovered." |
| WARNING | `circuit_breaker_half_open` | Service recovery attempt in progress | Log only — no push (transient state). |
| WARNING | `resolver_failed` | Content resolution failed for a URL | Log only — normal for flaky sites. |

**Implementation:**

```python
# seed_storage/notifications.py
import httpx

def send_alert(message: str, debounce_key: str | None = None):
    """Fire-and-forget Discord webhook alert. Never blocks pipeline operations.
    
    Synchronous — callers are Celery task handlers (on_failure, circuit breaker
    state transitions, cost tracking) which are all sync contexts. Using sync
    httpx.Client avoids asyncio.run() issues in Celery signal handlers.
    """
    webhook_url = config.DISCORD_ALERTS_WEBHOOK_URL
    if not webhook_url:
        return  # alerts disabled — no webhook configured

    if debounce_key:
        # Redis-based debounce: skip if same key was sent within debounce window
        if not redis_client.set(f"seed:notify:debounce:{debounce_key}", "1", nx=True, ex=config.NOTIFICATION_DEBOUNCE_SECONDS):
            return  # debounced

    try:
        with httpx.Client(timeout=5.0) as client:
            client.post(webhook_url, json={"content": message})
    except Exception:
        logger.warning("Failed to send Discord webhook alert", exc_info=True)
```

**Constants:**
- `DISCORD_ALERTS_WEBHOOK_URL`: Read from file-mode credential at startup. Empty string = alerts disabled.
- `NOTIFICATION_DEBOUNCE_SECONDS`: 300 (5 min) default — prevents duplicate alerts for the same event.

**`discord.com` is already in `allowed_hosts`** — no manifest change needed for webhook egress.

### CI/CD

Minimum viable: a GitHub Actions workflow that runs `pytest tests/unit/` (no infrastructure) on push. Integration test CI stage with Redis + Neo4j as a follow-up.

---

## 7b. Novel Technology Research

### Inventory of potentially unfamiliar tech

| Technology | Risk level | Why it's novel |
|---|---|---|
| **Graphiti (getzep/graphiti)** | High | Core dependency. LLM-powered knowledge graph construction library from Zep. Relatively new, may not be well-represented in training data. Incorrect usage would silently produce a broken graph. |
| **docling** | Medium | IBM's document conversion library; newer alternative to unstructured/PyMuPDF. |
| **iron-proxy** | High | Ant-keeper's credential proxying sidecar. Custom infrastructure — zero public documentation. Must be understood from ant-keeper source code. |

### Reference materials — READ BEFORE IMPLEMENTING

1. **Graphiti:** https://github.com/getzep/graphiti — Read the README, `examples/` directory, and the `GraphitiClient` API reference. Pay attention to `add_episode()` parameters (especially `source_description`, `reference_time`, `group_id`) and `build_indices_and_constraints()`. The entity resolution behavior (automatic cross-episode entity merging) is the core value — verify it works as documented.
2. **Iron-proxy / ant-keeper credential model:** Read `/opt/shared/ant-keeper/` source — specifically the manifest schema, credential injection logic, and proxy sidecar configuration. No external docs exist.
3. **docling:** https://github.com/DS4SD/docling — Read the README and API reference for `DocumentConverter`. Verify that `.convert(source)` works with HTTP URLs (not just local files). Validate output structure — we need `.text` content extraction. If docling cannot handle remote URLs, download to temp file first.

### Validation examples

- **Graphiti entity merging test (smoke_test.py step 5):** Ingest 3 episodes that each mention "Wyler" in different contexts: (a) "Wyler discussed the deployment plan", (b) "The API was reviewed by Wyler", (c) "Wyler's feedback on the proposal". After all 3 are ingested, run Cypher: `MATCH (e:Entity {name: 'Wyler'}) RETURN count(e)`. Expected: `1` (Graphiti merged them). If count > 1, Graphiti's entity resolution is not working — investigate before proceeding. Also verify MENTIONS edges: `MATCH (e:Entity {name: 'Wyler'})<-[:MENTIONS]-(ep:Episodic) RETURN count(ep)` should return `3`.
- **Graphiti idempotency check:** Call `add_episode()` with the same episode body and `source_description` twice. Verify no duplicate Episodic nodes are created.
- **Iron-proxy validation:** After deploying, verify that the task container's `OPENAI_API_KEY` env var contains a proxy token (starts with `ptok_`), not the real key. Then verify that an actual OpenAI API call succeeds (iron-proxy swaps the token transparently).

### Anti-fallback rule

If Graphiti's `add_episode()` fails or behaves unexpectedly, **do not fall back to direct Cypher queries** or manual entity extraction. Report the failure. Graphiti's entity resolution is the core value proposition — bypassing it produces a graph without cross-episode entity linking, which defeats the purpose. Add a comment in `graphiti_client.py`: `# WARNING: Use Graphiti's add_episode() for all writes. Direct Cypher bypasses entity resolution.`

---

## 8. Third-Party Components

### Graphiti (getzep/graphiti)

- **Docs:** https://github.com/getzep/graphiti, https://help.getzep.com/graphiti
- **Key API:** `graphiti.add_episode(name, episode_body, source_description, reference_time, source, group_id, ...)` — primary write path. Full signature includes `entity_types`, `edge_types`, `custom_extraction_instructions`, `update_communities`, and more. Entity extraction and graph construction happen internally.
- **Config used:** `LLMClient` (provider-specific), `EmbedderClient` (always OpenAI), `build_indices_and_constraints()` for schema setup.
- **Entity resolution (verified):** Graphiti uses a **3-tier dedup pipeline**: (1) embedding similarity search (cosine >= 0.6, up to 15 candidates), (2) deterministic/fuzzy string matching (Jaccard similarity, LSH buckets), (3) LLM escalation for ambiguous cases. When a match is found, the existing Entity node is reused — no duplicate created. This means "Wyler" across different channels merges automatically.
- **Node summary duplication is intentional.** Per Zep docs: each Entity node's summary is self-contained and may repeat relationship information that also appears in connected nodes' summaries. This is by design — not a bug.
- **`group_id` creates full isolation.** Entity dedup only searches within the same `group_id`. Use a single `group_id` (`"seed-storage"`) for all episodes to enable cross-source entity merging. Differentiate source via `source_description`.
- **`source_description` is metadata only** for message/text episodes. It is persisted on the EpisodicNode but does NOT influence entity extraction. For JSON episodes, it IS used in the extraction prompt.
- **Limitation:** Only produces Entity, Episodic, RELATES_TO, MENTIONS node/edge types. Richer types require custom post-processing (out of scope).
- **Search:** `graphiti.search(query, group_ids, num_results)` returns `list[EntityEdge]`. Filter by `group_ids` to scope results.

### trafilatura

- **Docs:** https://trafilatura.readthedocs.io/
- **Config:** `trafilatura.extract(html, include_comments=False, include_tables=True, include_links=True)`
- **Fallback:** If returns `None`, use `readability-lxml` for extraction.

### yt-dlp

- **Docs:** https://github.com/yt-dlp/yt-dlp
- **Config:** Metadata extraction only by default (no video download). For transcription: `-f bestaudio --extract-audio --audio-format wav`.
- **Caption extraction:** `--write-subs --write-auto-subs --sub-langs en` — prefer manual captions, fall back to auto-generated.

### openai-whisper (local)

- **Docs:** https://github.com/openai/whisper
- **Config:** Model size `base` for speed; configurable via `WHISPER_MODEL_SIZE` env var (in `config.py` Settings).
- **Resource:** ~1GB VRAM (base model) or CPU-only. ~2-5 min per hour of audio on CPU.

### docling

- **Docs:** https://github.com/DS4SD/docling
- **Config:** `DocumentConverter().convert(source)` — returns structured document. Extract `.text` content.
- **Fallback:** If docling fails, use `unstructured` partition_pdf.

### Twitter/X (TODO — stub resolver)

X/Twitter content extraction is not implemented in Phase A. The `TwitterResolver` is a stub that returns `ResolvedContent.error_result(url, "Twitter/X resolver not implemented — TODO")`. Platform detection (🐦 emoji) still works via URL pattern matching. The `twitter.com`, `x.com`, `pbs.twimg.com`, and `nitter.net` entries remain in `allowed_hosts` for future use. When a real resolver is implemented, evaluate: twscrape, nitter proxies, browser-based extraction, or official API access.

---

## 9. Testing Strategy

### Test file structure

```
tests/
├── conftest.py                      # shared fixtures — NO infrastructure dependencies
├── unit/
│   ├── test_config.py               # ~15 tests
│   ├── test_dedup.py                # ~12 tests (mock Redis)
│   ├── test_url_canonicalization.py  # ~20 tests
│   ├── test_models.py               # ~15 tests (ResolvedContent)
│   ├── test_dispatcher.py           # ~15 tests (mock resolvers)
│   ├── test_circuit_breaker.py      # ~12 tests (mock Redis)
│   ├── test_cost_tracking.py        # ~10 tests (mock Redis)
│   ├── test_rate_limiting.py        # ~8 tests (mock Redis)
│   ├── test_frontier.py             # ~15 tests (mock Redis)
│   ├── test_notifications.py        # ~8 tests (mock httpx)
│   ├── test_dead_letters.py         # ~8 tests (mock Redis)
│   ├── test_health.py               # ~8 tests (mock subsystems)
│   ├── test_graphiti_client.py      # ~6 tests (mock Graphiti)
│   ├── test_query.py                # ~5 tests (mock Graphiti)
│   ├── test_logging.py              # ~5 tests
│   ├── resolvers/
│   │   ├── test_webpage.py          # ~8 tests (mock HTTP)
│   │   ├── test_youtube.py          # ~8 tests (mock yt-dlp)
│   │   ├── test_github.py           # ~6 tests (mock GitHub API)
│   │   ├── test_twitter.py          # ~2 tests (stub resolver returns error_result, URL pattern matching)
│   │   ├── test_image.py            # ~5 tests (mock vision LLM)
│   │   ├── test_pdf.py              # ~5 tests (mock docling + unstructured)
│   │   ├── test_video.py            # ~5 tests (mock ffmpeg + whisper)
│   │   └── test_fallback.py         # ~4 tests (mock HTTP)
│   ├── tasks/
│   │   ├── test_enrich_message.py   # ~12 tests (mock deps)
│   │   ├── test_ingest_episode.py   # ~10 tests (mock Graphiti)
│   │   ├── test_expand_frontier.py  # ~8 tests (mock deps)
│   │   └── test_scan_frontier.py    # ~5 tests (mock deps)
│   └── ingestion/
│       ├── test_bot.py              # ~10 tests (mock discord.py)
│       └── test_batch.py            # ~10 tests (mock file I/O)
├── integration/
│   ├── conftest.py                  # Redis + Neo4j fixtures — SEPARATE from root conftest
│   ├── test_dedup_redis.py          # ~6 tests (real Redis)
│   ├── test_circuit_breaker_redis.py # ~5 tests (real Redis)
│   ├── test_cost_tracking_redis.py  # ~4 tests (real Redis)
│   ├── test_rate_limiting_redis.py  # ~4 tests (real Redis)
│   ├── test_frontier_redis.py       # ~6 tests (real Redis)
│   ├── test_content_cache_redis.py  # ~4 tests (real Redis, TTL)
│   ├── test_reaction_pubsub.py      # ~3 tests (real Redis pubsub)
│   ├── test_graphiti.py             # ~8 tests (real Neo4j + Graphiti)
│   ├── test_celery_tasks.py         # ~8 tests (real Celery + Redis)
│   ├── test_enrichment_pipeline.py  # ~6 tests (real dispatch, mocked HTTP)
│   ├── test_notifications_integration.py  # ~4 tests (real HTTP to mock server)
│   ├── test_dead_letters_redis.py   # ~4 tests (real Redis, RPUSH/LPOP, replay)
│   ├── test_health_endpoint.py      # ~4 tests (real HTTP, real subsystems)
│   └── test_config_loading.py       # ~3 tests (real env vars, real file credentials)
├── e2e/
│   ├── conftest.py                  # full stack fixtures with cleanup
│   ├── test_message_to_graph.py     # ~6 tests (various content types)
│   ├── test_batch_import.py         # ~4 tests (import, offset, cap, errors)
│   ├── test_query.py                # ~3 tests (search, filter, empty)
│   ├── test_dedup.py                # ~4 tests (message, URL, cross-message URL)
│   ├── test_graceful_degradation.py # ~3 tests (dead URL, timeout, all fail)
│   ├── test_source_tracking.py      # ~3 tests (multi-channel, source_description)
│   ├── test_reactions.py            # ~3 tests (stage progression, platform emojis)
│   ├── test_frontier_expansion.py   # ~4 tests (auto-scan, manual, depth limit)
│   ├── test_circuit_breaker_e2e.py  # ~3 tests (trip, recover, notification sent)
│   ├── test_cost_ceiling.py         # ~3 tests (budget exceeded, pause, recovery)
│   └── test_pipeline_restart.py     # ~2 tests (pod restart, no message loss)
└── security/
    ├── test_injection.py            # ~5 tests (SQL, XSS, SSTI, oversized, unicode)
    ├── test_credential_isolation.py # ~4 tests (no keys in logs at any level)
    ├── test_dedup_key_isolation.py  # ~3 tests (separate sets, no cross-collision)
    ├── test_egress_boundary.py      # ~3 tests (unlisted domains blocked, allowed pass)
    └── test_input_validation.py     # ~5 tests (malformed payload shapes, missing fields, type errors)
```

### Unit tests — zero infrastructure (~185-205 tests)

Unit tests must pass with `pytest tests/unit/` and **no running services**. They must not import from `integration/conftest.py` or require Redis/Neo4j/network.

Mock boundaries:
- `DedupStore` → mock Redis client
- Resolvers → mock `httpx` responses (use `respx` or `httpx.MockTransport`)
- `yt-dlp` → mock subprocess output
- `Graphiti` → mock client (unit tests don't test Graphiti itself)
- `notifications` → mock `httpx` client
- `circuit_breaker` → mock Redis client

#### test_config.py (~15 tests)
- Default values loaded correctly for all fields
- File-mode credential loading: `DISCORD_BOT_TOKEN_PATH` → reads file → populates `DISCORD_BOT_TOKEN`
- File-mode credential loading: `NEO4J_PASSWORD_PATH` → reads file → populates `NEO4J_PASSWORD`
- Missing `DISCORD_BOT_TOKEN` (no direct value, no path) → `ValueError`
- Missing `NEO4J_PASSWORD` (no direct value, no path) → `ValueError`
- `LLM_API_KEY` resolution: `LLM_PROVIDER=openai` → uses `OPENAI_API_KEY`
- `LLM_API_KEY` resolution: `LLM_PROVIDER=anthropic` → uses `ANTHROPIC_API_KEY`
- `LLM_API_KEY` resolution: `LLM_PROVIDER=groq` → uses `GROQ_API_KEY`
- Missing provider key for selected `LLM_PROVIDER` → `ValueError`
- `DISCORD_CHANNEL_IDS` parsed from comma-separated string to `list[int]`
- `TRANSCRIPTION_BACKEND=assemblyai` without `ASSEMBLYAI_API_KEY` → `ValueError`
- Env var takes precedence over `.env` file
- All default values match Section 4 infrastructure constants
- Direct `DISCORD_BOT_TOKEN` value used when both direct and path set (direct wins)
- Empty string credential values treated as missing
- `VISION_PROVIDER` defaults to `LLM_PROVIDER` when not set
- `VISION_PROVIDER=anthropic` without `ANTHROPIC_API_KEY` → `ValueError`

#### test_dedup.py (~12 tests)
- `seen_or_mark()` returns `False` on first call (new item, atomically marked)
- `seen_or_mark()` returns `True` on second call (already seen)
- `is_seen()` returns `False` for unseen key
- `is_seen()` returns `True` after `mark_seen()`
- Message dedup key format: `{source_type}:{source_id}` (e.g., `discord:123456`)
- URL dedup key: SHA256 hex digest of canonical URL
- Content ingestion dedup: uses `seed:ingested_content` set (separate from `seed:seen_urls`)
- Different DedupStore instances with different `set_key` don't cross-contaminate
- Empty key handling (should not crash)
- Very long key handling (>1000 chars)
- `SADD` return value correctly interpreted (0 = existed, 1 = new)
- Multiple calls to `mark_seen()` for same key are idempotent

#### test_url_canonicalization.py (~20 tests)
- Strips `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term`
- Strips `fbclid`
- Strips `ref` parameter
- Strips `si`, `t`, `s` tracking parameters
- Lowercases scheme (`HTTP` → `http`, `HTTPS` → `https`)
- Lowercases host (`YouTube.COM` → `youtube.com`)
- Preserves path case (`/MyRepo/README.md` stays)
- Sorts remaining query params alphabetically
- Removes trailing slash from path
- Removes fragment (`#section`)
- URL with no query params → unchanged (except scheme/host normalization)
- URL with only tracking params → query string removed entirely
- Handles malformed URLs gracefully (returns original, doesn't crash)
- Handles URL with empty query string
- YouTube short URL normalization: `youtu.be/abc` → `youtube.com/watch?v=abc`
- Twitter/X normalization: `x.com/user/status/123` → canonical form
- Preserves essential YouTube params (`v=`, `t=` for timestamp)
- Idempotent: `canonicalize(canonicalize(url)) == canonicalize(url)`
- Unicode URLs handled correctly (IDN domains)
- Protocol-relative URLs (`//example.com`) handled

#### test_models.py (~15 tests)
- `to_dict()` produces all expected keys
- `from_dict(to_dict(rc))` round-trip equality for fully-populated instance
- `error_result()` factory: `text` is empty string
- `error_result()` factory: `extraction_error` is set to provided message
- `error_result()` factory: `resolved_at` is set to current UTC
- `datetime` serialization: ISO 8601 format preserved through round-trip
- `None` optional fields (`title`, `transcript`, `summary`) serialize/deserialize correctly
- Empty `expansion_urls` list round-trips
- Large `metadata` dict with nested structures round-trips
- `content_type` literal values all accepted
- `from_dict()` with missing required field raises error
- `from_dict()` with extra/unknown fields ignores them (forward compatibility)
- `to_dict()` output is fully JSON-serializable (`json.dumps()` succeeds)
- `expansion_urls` with duplicate URLs preserved (dedup is not model's responsibility)
- `extraction_error=None` on success distinguishes from `extraction_error=""`

#### test_dispatcher.py (~15 tests)
- `youtube.com/watch?v=X` → `YouTubeResolver`
- `youtu.be/X` → `YouTubeResolver`
- `youtube.com/shorts/X` → `YouTubeResolver`
- `github.com/org/repo` → `GitHubResolver`
- `twitter.com/user/status/123` → `TwitterResolver` (stub — returns error_result)
- `x.com/user/status/123` → `TwitterResolver` (stub — returns error_result)
- URL ending `.pdf` → `PdfResolver`
- URL with `content-type: application/pdf` → `PdfResolver`
- `.png`, `.jpg`, `.gif`, `.webp` → `ImageResolver`
- `.mp4`, `.mov`, `.webm` → `VideoResolver`
- Generic `https://example.com/article` → `WebpageResolver`
- Unknown protocol `ftp://example.com` → `FallbackResolver`
- Priority ordering: `youtube.com` goes to YouTube, not Webpage
- Multiple URLs dispatched independently (failure in one doesn't block others)
- Resolver raising exception → caught, returns `error_result()`, logged WARNING

#### test_circuit_breaker.py (~12 tests)
- Initial state is `closed`
- Recording success keeps state `closed`
- Recording failure increments counter
- `failure_threshold` consecutive failures → state becomes `open`
- `open` state: `is_open()` returns `True`
- `open` state: operations should be skipped
- After `cooldown_seconds` → state transitions to `half-open`
- `half-open` + success → `closed`, counter reset
- `half-open` + failure → `open` again (cooldown restarts)
- Success after N-1 failures resets counter (doesn't accumulate)
- Multiple breakers with different service names are independent
- State stored under correct Redis key format: `seed:circuit:{service_name}`

#### test_cost_tracking.py (~10 tests)
- Counter increments by `ESTIMATED_COST_PER_CALL` per call
- Daily key format: `seed:cost:daily:YYYY-MM-DD`
- `is_budget_exceeded()` returns `False` when under budget
- `is_budget_exceeded()` returns `True` when at or over `DAILY_LLM_BUDGET`
- 80% threshold: `is_warning_threshold()` returns `True` at 80%+
- Counter TTL is 48 hours (set on creation)
- New date key created at midnight UTC (counter resets naturally)
- `ESTIMATED_COST_PER_CALL` and `DAILY_LLM_BUDGET` configurable via Settings
- `get_current_spend()` returns current dollar amount
- Zero spend ��� `is_budget_exceeded()` returns `False`

#### test_rate_limiting.py (~8 tests)
- Under limit: request allowed
- At limit: request blocked (returns `False` or raises)
- Sliding window: old entries expire, new requests allowed
- Rate limit value from `GRAPHITI_RATE_LIMIT_PER_MINUTE` config
- Redis key: `seed:ratelimit:graphiti`
- Concurrent callers share the same window
- Zero-length window → all requests blocked
- Very large limit → effectively unlimited

#### test_frontier.py (~15 tests)
- `add_to_frontier()` stores URL in sorted set with priority score
- Priority scoring: depth=0 higher than depth=2
- Priority scoring: GitHub resolver bonus (+5) applied
- Priority scoring: arxiv.org domain bonus (+4) applied
- Priority scoring: channel bonus applied
- Priority floor at 0 (no negative scores)
- `pick_top()` returns URLs sorted by score descending
- `pick_top()` with `min_threshold` filters low-priority URLs
- `pick_top()` respects per-resolver depth policies
- `remove_from_frontier()` removes URL from sorted set and metadata hash
- `ZADD NX` semantics: existing URL not overwritten
- Frontier metadata stored and retrieved correctly via hash
- Empty frontier → `pick_top()` returns empty list
- URL hash computation consistent with dedup module
- `MAX_EXPANSION_BREADTH_PER_RESOLVE` limits child URLs per resolution

#### test_notifications.py (~8 tests)
- `send_alert()` POSTs correct JSON body to webhook URL (`{"content": message}`) — sync call using `httpx.Client`
- HTTP failure (connection error) → logs WARNING, doesn't raise
- HTTP timeout (5s) → logs WARNING, doesn't raise
- Debounce: second call with same `debounce_key` within window → skipped
- Debounce: call after window expires → sent
- `debounce_key=None` → no debouncing, always sent
- Empty `DISCORD_ALERTS_WEBHOOK_URL` → skip alert silently (alerts disabled)
- Webhook URL read from file-mode credential at startup

#### test_dead_letters.py (~8 tests)
- `dead_letter()` stores JSON entry in `seed:dead_letters` via `RPUSH`
- Stored entry contains all required fields: `task_name`, `payload`, `error`, `traceback`, `retries_exhausted`, `failed_at`, `source_id`
- `source_id` extracted from nested `payload.message.source_id`; falls back to `"unknown"` if missing
- `list_dead_letters()` returns count and preview of entries without consuming them (`LRANGE`)
- `replay_one()` pops oldest entry (`LPOP`) and returns task name + payload for re-enqueue
- `replay_all()` pops all entries, returns list of (task_name, payload) tuples
- Empty dead-letter list → `replay_one()` returns `None`
- Entry with unknown `task_name` → logged WARNING, skipped (not re-enqueued)

#### test_health.py (~8 tests)
- All checks pass → 200 with `"status": "healthy"`
- Redis unreachable → 503 with `redis: "error"`
- Neo4j unreachable → 503 with `neo4j: "error"`
- No Celery workers → 503 with `celery: "error"`
- Response body includes queue depths, frontier size, dead_letter_count, cost metrics
- Response body includes `open_circuit_breakers` list
- Partial failure (Redis ok, Neo4j down) → 503 with mixed check results
- Health check completes within 5 seconds (timeout each subsystem check)

#### test_graphiti_client.py (~9 tests)
- `LLM_PROVIDER=openai` → constructs `OpenAIClient` + `OpenAIEmbedder`
- `LLM_PROVIDER=anthropic` → constructs `AnthropicClient` + `OpenAIEmbedder`
- `LLM_PROVIDER=groq` → constructs `GroqClient` + `OpenAIEmbedder`
- `build_indices_and_constraints()` called during init
- `get_graphiti()` returns singleton (same instance on repeated calls)
- All `add_episode()` calls enforce `group_id="seed-storage"`
- `get_vision_client()` with `VISION_PROVIDER=openai` → OpenAI client
- `get_vision_client()` with `VISION_PROVIDER=anthropic` → Anthropic client
- `get_vision_client()` defaults to `LLM_PROVIDER` when `VISION_PROVIDER` is None

#### test_query.py (~5 tests)
- Search passes `group_ids=["seed-storage"]`
- `num_results` parameter forwarded to Graphiti
- `EntityEdge` → CLI JSON transformation produces correct shape (`content`, `score`, `metadata`)
- Empty results → empty list output
- Search error propagated with clear message

#### test_logging.py (~5 tests)
- Log output is valid JSON (parse with `json.loads()`)
- Required fields present: `timestamp`, `level`, `source_id`, `stage`, `status`
- API keys masked in log output: `sk-...{last4}` format
- Task start/end logs include `duration_ms`
- No raw secrets appear at any log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)

#### Resolver tests (~43 tests total)

**test_webpage.py (~8):** Successful trafilatura extraction; trafilatura returns None → readability-lxml fallback; both fail → `error_result()`; content truncated at 8000 tokens with `[Content truncated]` note; `expansion_urls` populated from outbound links; non-UTF8 encoding detected and converted; HTTP timeout → `error_result()`; SSL error → `error_result()`.

**test_youtube.py (~8):** Metadata extraction via yt-dlp mock; manual captions preferred; auto-caption fallback; no captions → transcription fallback; transcript truncated at 12000 tokens; yt-dlp timeout → `error_result()`; metadata populated (title, duration, author, publish_date); YouTube Shorts URL handled.

**test_github.py (~6):** Repo metadata + README extraction; text format matches spec; unauthenticated request works; authenticated request uses `GITHUB_TOKEN`; private repo → `error_result()`; rate limited (403) → `error_result()`.

**test_twitter.py (~2):** Stub resolver returns `error_result()` with TODO message for `twitter.com` URL; stub resolver returns `error_result()` for `x.com` URL.

**test_image.py (~5):** Vision LLM called with correct prompt; `summary` and `text` populated; inaccessible URL → `error_result()`; vision timeout → `error_result()`; non-image content-type → `error_result()`.

**test_pdf.py (~5):** docling extraction success; docling fails → unstructured fallback; both fail → `error_result()`; content truncated at 10000 tokens; large PDF handled within timeout.

**test_video.py (~5):** Download → ffmpeg → transcription full path; temp file cleanup in `finally`; whisper timeout (300s) → skip transcription, ingest metadata; download failure → `error_result()`; unsupported codec → `error_result()`.

**test_fallback.py (~4):** HTTP GET + BeautifulSoup title + meta description; never raises (always returns result); timeout → returns minimal result; malformed HTML → returns what's available.

#### Task tests (~35 tests total)

**test_enrich_message.py (~12):** Dedup check: seen message → skipped, returns early; URL extraction from message content; URL canonicalization applied before dedup; per-URL dedup: seen URL → cached result used; builds `enriched_payload` with correct shape; publishes reaction events to Redis pubsub; handles message with no URLs (plain text only); handles message with multiple URLs; partial URL failure: 3/5 fail → 2 resolved + message still enqueued; empty content + empty attachments → skipped; bot message → skipped; `asyncio.run()` wraps async resolver calls correctly.

**test_ingest_episode.py (~10):** Message episode: `source_description` format is `"{source_type.title()} #{source_channel}"`; content episode: `source_description` format includes content_type; `group_id` is always `"seed-storage"`; `expansion_urls` from resolved content written to frontier; daily cost counter incremented after each `add_episode()`; budget exceeded → task sleeps and retries; multiple resolved contents → one episode per content; reaction events published for each stage; empty resolved_contents → message episode still created; cost_limit_exceeded after max retries → dead-lettered.

**test_expand_frontier.py (~8):** Depth ceiling enforced (`HARD_DEPTH_CEILING=5`); `build_content_payload()` produces correct `enriched_payload` shape; `seed:ingested_content` dedup check prevents re-ingestion; cache hit → resolution skipped; child URLs added to frontier with incremented depth; child URL priority computed correctly; `MAX_EXPANSION_BREADTH_PER_RESOLVE` limits children; processed URL removed from frontier.

**test_scan_frontier.py (~5):** `FRONTIER_AUTO_ENABLED=false` → returns immediately (no-op); picks top `FRONTIER_BATCH_SIZE` URLs above threshold; respects per-resolver depth policies; enqueues `expand_from_frontier.delay()` for each picked URL; logs count of URLs enqueued.

#### Ingestion tests (~20 tests total)

**test_bot.py (~10):** Message in configured channel → processed; message in non-configured channel → ignored; empty message → skipped (logged DEBUG); bot author → skipped (logged DEBUG); `raw_payload` shape matches contract (all fields present and correct types); `source_type` is `"discord"`; `source_id` is `str(message.id)`; `source_channel` is `message.channel.name`; attachment URLs extracted into `attachments` list; `metadata` includes `channel_id`, `author_id`, `guild_id`.

**test_batch.py (~10):** DiscordChatExporter JSON parsed correctly; `raw_payload` shape matches contract; `--offset` parameter skips N messages; 5000 message cap per run → stops with instructions; progress logged every 100 messages; failed message written to JSONL failure log; summary report format: `"Imported N/M messages. K failures logged to {path}."`; empty JSON file → graceful exit with message; malformed JSON entry → skipped, logged, continues; `source_type` is `"discord"` for all batch entries.

### Integration tests — declared infrastructure (~69 tests)

Integration tests require Redis and Neo4j. Declared in `tests/integration/conftest.py`:

```python
@pytest.fixture(scope="session")
def redis_client():
    """Requires: redis on localhost:6379. Skip if unavailable."""
    ...

@pytest.fixture(scope="session")
def neo4j_driver():
    """Requires: neo4j on localhost:7687. Skip if unavailable."""
    ...
```

Use `pytest.mark.integration` marker. CI runs unit tests only; integration tests run locally or in a dedicated CI stage with `docker compose up`.

**test_dedup_redis.py (~6):** Real `SADD`/`SISMEMBER` operations; concurrent access from two clients → no race condition; `seen_or_mark()` atomicity under contention; large set (10k members) performance acceptable; set persistence across reconnection; different set keys isolated.

**test_circuit_breaker_redis.py (~5):** State persists across different Redis client instances (simulates multiple workers); concurrent failure recording from two workers; cooldown timing accuracy (within 1s tolerance); state recovery after Redis reconnect; `KEYS seed:circuit:*` returns expected breaker keys.

**test_cost_tracking_redis.py (~4):** Counter increment from two concurrent workers → correct total; TTL set correctly on first write; `GET` returns parseable float; midnight boundary → new key created.

**test_rate_limiting_redis.py (~4):** Sliding window with real timing (100ms resolution); concurrent requests from multiple clients; window expiry frees capacity; counter accuracy under load.

**test_frontier_redis.py (~6):** `ZADD NX` prevents overwrites; `ZRANGEBYSCORE` returns correct priority range; metadata hash stored and retrieved correctly; `ZREM` + `DEL` cleanup; large frontier (1k URLs) performance; score update path (if needed).

**test_content_cache_redis.py (~4):** `SET` with TTL stores serialized `ResolvedContent`; `GET` + `from_dict()` round-trip; expired cache → `None`; cache miss → `None`.

**test_reaction_pubsub.py (~3):** Publish reaction event → subscriber receives; subscriber disconnected → event silently dropped; multiple subscribers all receive.

**test_graphiti.py (~8):** `add_episode()` creates Entity + Episodic nodes; entity merging: "Wyler" across 3 episodes → 1 Entity node; MENTIONS edges: 3 episodes mentioning same entity → 3 edges; idempotency: duplicate episode body → no duplicate nodes; `source_description` persisted on Episodic node; `group_id="seed-storage"` scopes correctly; `build_indices_and_constraints()` idempotent (run twice, no error); `search()` returns results matching ingested content.

**test_celery_tasks.py (~8):** `enrich_message` task executes end-to-end with mocked HTTP; `ingest_episode` task writes to real Neo4j via Graphiti; task retry on transient error (mock Redis blip → retry → succeed); task dead-lettered after `max_retries`; `task_reject_on_worker_lost` re-queues on simulated crash; `expand_from_frontier` task resolves and ingests; `scan_frontier` beat task fires on schedule; task routing: enrich → `raw_messages` queue, ingest → `graph_ingest` queue.

**test_enrichment_pipeline.py (~6):** Full dispatch path: URL → resolver selection → resolution → `ResolvedContent`; multiple URLs: independent resolution; mixed success/failure: partial results returned; cached URL → cache hit, no HTTP call; content cache populated after first resolution; truncation applied at correct token limits.

**test_notifications_integration.py (~4):** Real HTTP POST to mock webhook server → correct Discord webhook body; debounce key in Redis → second call skipped; debounce expired → call proceeds; connection refused → WARNING logged, no crash.

**test_dead_letters_redis.py (~4):** `RPUSH` stores entry, `LLEN` returns count, `LPOP` returns oldest (FIFO order); concurrent dead-lettering from two workers → all entries stored; `LRANGE 0 -1` for listing without consuming; replay + re-enqueue via `.delay()` round-trip with real Celery.

**test_health_endpoint.py (~4):** Real HTTP GET to health endpoint → 200 JSON; queue depth metrics reflect actual queue state; cost metrics reflect actual Redis counter; circuit breaker status reflects actual Redis state.

**test_config_loading.py (~3):** Real env vars loaded by pydantic-settings; real file credential read from tmpfile; `.env` file fallback when env var not set.

### E2E tests — real user workflows (~38 tests)

Each test exercises a full path a user would take. **All E2E tests must clean up after themselves.** Use a test-specific `source_description` prefix (e.g., `"test_e2e_"`) and pytest fixtures with `yield` + teardown that:
- Delete test Episodic/Entity nodes from Neo4j (match by `source_description` prefix)
- Flush test keys from Redis (`seed:seen_messages` test entries, `seed:seen_urls`, `seed:ingested_content`, `seed:frontier`)
- Remove test content cache entries
- Remove test notification debounce keys

**test_message_to_graph.py (~6):** Message with YouTube link → enriched → both message + content episodes in Neo4j with entities; message with GitHub link → repo metadata in graph; message with image attachment → vision LLM summary in graph; message with PDF link → extracted text in graph; message with multiple URLs → all resolved, all in graph; plain text message (no URLs) → message episode in graph.

**test_batch_import.py (~4):** 10-message fixture file → all in graph with correct entities; `--offset 5` → only messages 6-10 imported; file exceeding 5000 cap → stops at cap with log; messages with mixed content types → each resolved correctly.

**test_query.py (~3):** Ingest known content → search → result contains ingested content; search with no matches → empty results; search result `source_description` field enables source filtering.

**test_dedup.py (~4):** Same message sent twice → only one episode set; same URL in two different messages → resolved once (cache hit), ingested in both message contexts; bot + batch overlap: same message via bot then batch → deduped; URL dedup: canonical URL matches despite tracking params.

**test_graceful_degradation.py (~3):** Dead URL → message episode still created, content has `extraction_error`; all URLs in message fail → message episode still created with plain text; resolver timeout → partial results, pipeline continues.

**test_source_tracking.py (~3):** Messages from `#general` and `#research` → different `source_description` on Episodic nodes; Cypher filter by `source_description` returns correct subset; entity merging across channels: same entity name in both channels → 1 Entity node.

**test_reactions.py (~3):** Mock the Discord bot boundary — subscribe to `seed:reactions` Redis pubsub and verify correct events are published. Tests: message pipeline emits reaction events in order (📥 → ⚙️ → 🏷️ → 🧠 with correct `message_id` and `channel_id`); message with YouTube URL → 🎬 platform emoji event published alongside 🏷️; message triggering dedup → 🔁 emoji event published. No real Discord connection required — tests verify the pubsub contract, not the Discord API call.

**test_frontier_expansion.py (~4):** Message with link → `expansion_urls` appear in frontier; auto-scanner processes frontier → expanded content in graph; manual `expand_from_frontier` task → specific URL resolved and ingested; depth limit enforced: depth > policy max → not auto-expanded.

**test_circuit_breaker_e2e.py (~3):** 5 consecutive resolver failures → circuit opens → operations skip → Discord webhook alert sent; cooldown expires → half-open → success → circuit closes → recovery alert; open circuit → resolver returns `error_result()`, message still ingests.

**test_cost_ceiling.py (~3):** Ingest until budget exceeded → `graph_ingest` tasks pause; paused tasks retry after delay (not permanently failed); 80% threshold → WARNING log emitted.

**test_pipeline_restart.py (~2):** Simulate pod restart (stop workers, restart) → in-flight tasks re-queued from Redis; dedup state survives restart (Redis persistence).

### Security boundary tests — mandatory (~20 tests)

**test_injection.py (~5):** Message content with SQL injection patterns → treated as opaque text, no crash; script tags in content → no XSS, treated as text; SSTI patterns (`{{7*7}}`) → no template injection; oversized payload (>1MB content field) �� rejected or handled gracefully; unicode edge cases (null bytes, RTL markers, emoji sequences) → no crash.

**test_credential_isolation.py (~4):** Startup logs captured → no API key values present at any level; task execution logs captured → no API key values; `OPENAI_API_KEY` masked to `sk-...{last4}` format; `DISCORD_BOT_TOKEN` never appears in any log output.

**test_dedup_key_isolation.py (~3):** `seed:seen_messages` and `seed:seen_urls` are separate Redis SETs; message ID cannot collide with URL hash in `seen_messages`; URL hash cannot collide with message ID in `seen_urls`.

**test_egress_boundary.py (~3):** URL on allowlisted domain → resolved successfully; URL on non-allowlisted domain → resolution fails with `error_result()` (not crash); internal Redis/Neo4j addresses → accessible.

**test_input_validation.py (~5):** `raw_payload` missing `source_type` → validation error, no crash; `raw_payload` with wrong type for `timestamp` (int instead of str) → validation error; `raw_payload` with null `content` → handled (skip or empty string); `attachments` with non-URL strings → handled gracefully; deeply nested `metadata` → accepted without stack overflow.

### Test count expectations

| Category | Expected test count | Notes |
|---|---|---|
| Unit | ~185-205 | Config, dedup, canonicalization, models, dispatcher, circuit breaker, cost, rate limit, frontier, notifications, dead letters, health, graphiti client, query, logging, 8 resolvers (twitter stub), 4 task files, 2 ingestion files |
| Integration | ~69 | Real Redis (dedup, circuit breaker, cost, rate limit, frontier, cache, pubsub, dead letters), real Graphiti, real Celery, enrichment pipeline, notifications, health, config |
| E2E | ~38 | Content types, batch, query, dedup, degradation, source tracking, reactions, frontier, circuit breaker, cost ceiling, restart |
| Security | ~20 | Injection, credential isolation, dedup isolation, egress boundary, input validation |
| **Total** | **~310-335** | If actual count drops below ~250, tests may be silently deselected — investigate |

### Smoke test

After deployment, one command confirms the system works:

```bash
python -m seed_storage.smoke_test
```

This script:
1. Checks Redis connectivity (PING)
2. Checks Neo4j connectivity (`driver.verify_connectivity()`)
3. Checks Celery workers are running (both queues respond to ping)
4. Checks Discord alerts webhook configured (or warns if disabled)
5. Sends a synthetic `raw_payload` through `enrich_message.delay()`
6. Waits up to 60 seconds for the corresponding episode to appear in Neo4j
7. Verifies entity extraction occurred (at least 1 Entity node created)
8. Queries the graph for the synthetic content
9. Cleans up synthetic data (Neo4j nodes + Redis keys)
10. Prints PASS/FAIL with per-step timing

---

## 10. Documentation & Continuity

### Deliverable documents

| Document | Purpose | Updated when |
|---|---|---|
| `README.md` | Setup, usage, architecture explanation | Any change to setup steps, dependencies, or architecture |
| `.env.example` | All config vars with descriptions and defaults | Any new config var added |
| `CLAUDE.md` | Agent instructions for working in this repo | Architecture decisions, conventions, gotchas |
| `docs/architecture.md` | Detailed architecture with diagrams | Component changes |
| `docs/resolvers.md` | How to add a new content resolver | New resolver added or base interface changes |

### Docs-to-modules mapping

| Module | Docs to update |
|---|---|
| `config.py` | `.env.example`, `README.md` (setup section) |
| `enrichment/resolvers/*` | `docs/resolvers.md`, `README.md` (supported content types) |
| `worker/tasks.py` | `docs/architecture.md` (queue section) |
| `docker-compose.yml` | `README.md` (setup section), `.env.example` |
| `graphiti_client.py` | `README.md` (LLM provider section), `.env.example` |

When implementation changes touch a module, check and update associated docs. This is a deliverable, not an afterthought.

### Session continuity

Persist in `CLAUDE.md` for future agents using this template:

```markdown
# Seed Storage - Agent Notes

## Architecture Decisions
- Graph backend: neo4j
- LLM provider: [openai|anthropic|groq] — reason: [...]
- Vision provider: [openai|anthropic|groq] — defaults to LLM provider

## Credential Model
- Discord + Neo4j: file-mode (non-HTTP protocols)
- All HTTP API keys: iron-proxy env-mode
- If using Anthropic/Groq: OPENAI_API_KEY still required for embeddings (Graphiti uses OpenAIEmbedder)

## Resolver Quirks
- [yt-dlp]: [version pin, edge cases]
- [trafilatura]: [known failures]
- [twitter]: TODO stub — no content extraction in Phase A

## Celery Tuning
- raw_messages=8, graph_ingest=2 (expansion feeds into graph_ingest)
- Rationale: [...]

## Config Deviations from Defaults
- [setting]: [value] — why: [...]

## Known Limitations
- No temporal queries or graph algorithms (Phase B)
- No MCP server integration (Phase B)
- No conversation threading — Graphiti entity resolution handles cross-message linking
- X/Twitter resolver is a stub — no content extraction (TODO)
- group_id must be "seed-storage" for all episodes — do NOT use per-channel group_ids (breaks entity merging)
- Only Discord ingestion implemented (Phase A). Source-agnostic `raw_payload` contract supports future sources.
```

---

## 11. Acceptance Criteria

### Phase A success metrics

| Metric | Target | How to measure |
|---|---|---|
| Reliability | 99% of Discord messages ingested without loss | `redis-cli -n 2 SCARD seed:seen_messages` vs known Discord message count (over 48h) |
| Latency | Message → graph node within 2 minutes (p99) | Timestamp delta: `raw_payload.timestamp` → `resolved_at` on Episodic node |
| Cost | < $1/day at ~500 msg/day steady state | `redis-cli -n 2 GET seed:cost:daily:{today}` |
| Entity dedup | Entities mentioned in >1 source merge into single node | Sample 10 entities: verify each has 1 Entity node, N MENTIONS edges |
| X/Twitter resolution | TODO — stub resolver only in Phase A | N/A — tracked as future work |

### Code & tests
- [ ] Unit tests pass (`pytest tests/unit/`): ~185-205 tests, zero infrastructure
- [ ] Integration tests pass (`pytest tests/integration/`): ~69 tests, Redis + Neo4j running
- [ ] E2E tests pass (`pytest tests/e2e/`): ~38 tests, full stack
- [ ] Security tests pass (`pytest tests/security/`): ~20 tests
- [ ] Total test count: ~310-335 (investigate if <250)
- [ ] Dedup prevents duplicate processing (message-level, URL-level, and content-level)
- [ ] Resolution failure does not block graph ingestion (message episode still created)
- [ ] Circuit breaker trips after 5 consecutive failures, resets after cooldown
- [ ] Circuit breaker open → Discord webhook alert sent (fire-and-forget)
- [ ] Circuit breaker close (recovery) → Discord webhook recovery alert sent
- [ ] Daily cost ceiling pauses graph_ingest when `DAILY_LLM_BUDGET` exceeded, webhook alert sent
- [ ] 80% budget warning logged and alerted (debounced, 1 hour)
- [ ] Frontier: discovered URLs appear in `seed:frontier` after ingestion
- [ ] Frontier auto-scanner processes URLs above threshold
- [ ] Manual expansion: `expand_from_frontier` task resolves and ingests a specific URL
- [ ] Expansion results feed into `graph_ingest` queue (same path as primary ingestion)
- [ ] ResolvedContent serialization round-trip test passes (`from_dict(to_dict(rc)) == rc`)
- [ ] Linting clean (`ruff check .`) on full repo

### Deployment (ant-keeper)
- [ ] Neo4j StatefulSet deployed and healthy in ant-keeper namespace
- [ ] Ant-keeper daemon registered and running (pod in Running state)
- [ ] Health check endpoint responds 200 at `:8080/health`
- [ ] All 5 supervisord processes running (bot + 2 workers + beat + health)
- [ ] Iron-proxy sidecar present and proxying HTTP credentials
- [ ] File-mode credentials (Discord, Neo4j) readable from `/run/credentials/`
- [ ] Redis DB 2 used (not DB 0 — verified via `redis-cli -n 2 DBSIZE`)
- [ ] Smoke test passes inside pod (`python -m seed_storage.smoke_test`)
- [ ] Discord bot connects and processes real messages from configured channels
- [ ] Discord emoji reactions appear on messages (📥 → ⚙️ → 🏷️ → 🧠 progression + platform emojis)
- [ ] Source tracking: episodes have correct `source_description` built from `source_type` + `source_channel`
- [ ] Source filtering: query by channel source returns only episodes from that channel
- [ ] `discord-alerts-webhook` credential stored (file-mode) for webhook alerts

### Operations (Discord webhook alerts)
- [ ] Circuit breaker open → Discord webhook alert delivered
- [ ] Circuit breaker recovery → Discord webhook recovery alert delivered
- [ ] Budget exceeded → Discord webhook alert delivered
- [ ] Dead-lettered task → payload stored in `seed:dead_letters` + Discord webhook alert delivered
- [ ] Dead-letter replay (`python -m seed_storage.worker.replay --all`) re-enqueues stored payloads
- [ ] Neo4j unreachable → Discord webhook alert delivered (debounced, 5 min)
- [ ] Alerts are fire-and-forget: webhook unreachable → WARNING logged, pipeline continues
- [ ] Alert debounce works: same event within debounce window → single alert
- [ ] Empty webhook URL (no credential) → alerts disabled, pipeline functions normally

### Security
- [ ] No hardcoded secrets in source code or Dockerfile
- [ ] API keys do not appear in log output at any level
- [ ] Discord token stored as file-mode credential (not env-mode) — documented why
- [ ] Neo4j password stored as file-mode credential (not env-mode) — documented why
- [ ] HTTP API credentials use iron-proxy env-mode with correct `proxy_target`
- [ ] Egress limited to declared `allowed_hosts` + credential proxy_targets
- [ ] URLs on non-allowlisted domains blocked (resolver returns `error_result()`)

### Documentation
- [ ] `.env.example` documents every config var with descriptions (for local dev)
- [ ] README covers: ant-keeper deployment, local dev setup, batch import, querying, architecture, cost
- [ ] Dual-key requirement documented prominently for Anthropic/Groq providers
- [ ] `CLAUDE.md` captures architecture decisions for future sessions
- [ ] `manifest.json` in repo root (reference for ant-keeper task registration)

### Local dev parity
- [ ] `docker-compose.yml` brings up Redis + Neo4j for local development
- [ ] Config works with both `.env` (local) and ant-keeper env injection (production)
- [ ] Works from fresh clone for local dev: `git clone && cp .env.example .env && docker compose up -d && pip install -e ".[dev]" && pytest tests/unit/`

## 12. Spec Metadata

- **Spec version:** v2, revision 7. Created 2026-04-12, last updated 2026-04-12.
- **Author:** Generated by Claude from user-provided architecture review and build prompt. Reviewed against Spec-Writing Guide v2 (16-check review). Revision 3: post-review fixes. Revision 4: Phase C incorporated, FalkorDB removed, migration removed, testing expanded to ~300+ tests. Revision 5: 16-check review against Spec-Writing Guide v2 — 9 findings (1 blocker, 3 major, 5 minor), all resolved.
- **Watermark:** Based on architecture review session (2026-04-11, wyler-zahm) and original seed-storage agent build prompt. Cross-referenced with ant-keeper infrastructure knowledge.
- **Confidence levels:**
  - Sections 1-6 (Problem, Architecture, Naming, Constants, Errors, Security): **HIGH** — derived from explicit user input and architecture review.
  - Section 7 (Deployment): **HIGH** — ant-keeper deployment model well-documented.
  - Section 7b (Novel Tech): **LOW** — Graphiti API surface and entity resolution behavior inferred from limited documentation; validation examples are best-guess. Verify against current Graphiti version before implementing.
  - Section 8 (Third-Party): **MEDIUM** — library APIs may have changed since training data cutoff. docling is highest risk. Twitter resolver is a TODO stub.
  - Section 9 (Testing): **HIGH** — structure follows standard patterns, coverage requirements from user.
  - Section 10 (Documentation): **HIGH** — standard deliverables. CLAUDE.md template provided.
  - Section 11 (Acceptance): **HIGH** — derived from all other sections. Success metrics added.
  - Appendix C (Implementation Details): **MEDIUM** — code patterns inferred from library documentation. Async/sync boundary addressed but implementation skeleton does not show `asyncio.run()` wrapping.
- **Resolved questions:**
  - Q1: Conversation threading → Dropped. Graphiti entity resolution handles cross-message linking. Channel source tracked via `source_description`.
  - Q2: Discord reactions → Kept. Full emoji set carried forward in new implementation.
  - Q3: Graphiti entity merging → Verified. 3-tier dedup (embedding → fuzzy ��� LLM). Node summary duplication is intentional per Zep docs.
  - Q4: Redis SPOF → Accepted for Phase A.
- **Revision 3 changes (post-review):**
  - Added Section 3 (Naming Conventions) — consolidated scattered naming rules
  - Renumbered all sections (3→4, 4→5, ..., 12→13) and updated all internal references
  - Defined `build_content_payload()` shape for expansion → ingest boundary
  - Documented three-set dedup model (`seen_messages`, `seen_urls`, `ingested_content`) with semantic differences
  - Added `EntityEdge` → CLI JSON transformation mapping
  - Added Celery beat schedule configuration to `worker/app.py`
  - Added `scan_frontier` task description to worker/tasks.py
  - Added `expansion/cli.py` and `seed_storage/smoke_test.py` to project structure; removed duplicate `scripts/smoke_test.py`
  - Deduplicated `allowed_hosts` list (single canonical copy in manifest)
  - Added zero-downtime and Caddy route statements to deployment section
  - Added docling to novel tech research materials
  - Added test count expectations table and E2E test cleanup requirements
  - Added Neo4j default password exposure window note
  - Fixed `intents.reactions` — not needed for adding reactions, only for receiving
- **Revision 4 changes (user feedback):**
  - Incorporated operations (formerly Phase C) into this spec. Circuit breaker → Discord webhook alert, budget warnings, dead-letter alerts. All fire-and-forget, debounced.
  - Removed FalkorDB throughout — Neo4j is the only graph backend
  - Removed Migration section — clean-room build, no reference to previous system
  - Removed architecture review traceability table — not relevant to clean-room build
  - Expanded testing from ~36 to ~315-340 tests with per-module test specifications
  - Added `notifications.py` module with Discord webhook alerts, `discord-alerts-webhook` file-mode credential, Redis-based debounce
  - Updated manifest with alerts webhook credential
  - Updated acceptance criteria with operations section (8 new criteria)
  - Phase naming: A + A-ops (this spec), Phase B (out of scope)
  - MCP server update → Phase B (out of scope)
- **Resolved questions:**
  - Q5: MCP → Phase B.
  - Q6: Multi-user not relevant to spec. System will support multiple users but no special handling needed.
  - Q7: FalkorDB removed. Neo4j only.
- **Revision 5 changes (16-check review against Spec-Writing Guide v2):**
  - Fixed `_load_file_credentials` in Appendix C — was missing `DISCORD_ALERTS_WEBHOOK_PATH` → `DISCORD_ALERTS_WEBHOOK_URL` loading (blocker: alerts would never work if implementer followed Appendix C)
  - Added dead-letter storage (`seed:dead_letters` Redis LIST) and replay CLI (`python -m seed_storage.worker.replay`). Tasks that exhaust retries now store payloads for later replay instead of being silently lost.
  - Fixed supervisord.conf to use `%(ENV_WORKER_CONCURRENCY_RAW)s` / `%(ENV_WORKER_CONCURRENCY_GRAPH)s` instead of hardcoded values (was inconsistent with Section 4 claims)
  - Removed `--group-id general` from query.py example (contradicted single `group_id="seed-storage"` design)
  - Added reaction pubsub message format (`{message_id, channel_id, emoji}`) — was unspecified
  - Added `neo4j>=5.0,<6.0` to pyproject.toml (was mentioned but not listed)
  - Added `scan_frontier` to `task_routes` dict (was only in beat schedule)
  - Added warning that `EntityEdge._score` may not exist in Graphiti
  - Added `dead_letter_count` to health check response
  - Added `dead_letters.py`, `replay.py` to project structure; `test_dead_letters.py` (unit, ~8), `test_dead_letters_redis.py` (integration, ~4)
  - Updated test counts: unit ~190-210, integration ~69, total ~315-340
- **Revision 6 changes (16-check review by wyler-zahm, 2026-04-12):**
  - **S-2.1 (major):** Changed `send_alert()` from async to sync (`httpx.Client` instead of `AsyncClient`). All callers are synchronous Celery contexts — `on_failure` handlers, circuit breaker transitions, cost tracking. `asyncio.run()` in Celery signal handlers is unreliable.
  - **S-7.1 (minor):** Added `-u neo4j:changeme` authentication to Neo4j password change curl command.
  - **S-11.1 (minor):** Fixed test count inconsistency in Appendix E implementation prompt — now matches Section 9 (~190-210 unit, ~69 integration, ~315-340 total).
  - **S-4.1 (minor):** Added `HARD_DEPTH_CEILING` (default 5) and `MAX_EXPANSION_BREADTH_PER_RESOLVE` (default 20) to config.py Settings class.
  - **S-4.2 (minor):** Added `@field_validator("DISCORD_CHANNEL_IDS")` to parse comma-separated env var strings to `list[int]`.
  - **S-6.1 (minor):** Added `discord-alerts-webhook` credential creation as step 8 in operator workflow (previously only in Section 7 prose).
  - **S-9.1 (minor):** E2E `test_reactions.py` updated to mock the bot boundary — tests verify reaction events published to `seed:reactions` pubsub, not actual Discord API calls. No live Discord connection required.
- **Revision 7 changes (16-check review + user feedback, 2026-04-12):**
  - **Vision provider-agnostic (user directive):** Added `VISION_PROVIDER` config var (defaults to `LLM_PROVIDER`). Image resolver constructs SDK client based on `VISION_PROVIDER`, not hardcoded to OpenAI. Added `get_vision_client()` to graphiti_client.py. Added vision provider validation and tests (~3 new unit tests). Ant-keeper handles credential injection and endpoint allowlisting per provider.
  - **X/Twitter resolver → TODO stub (user directive):** TwitterResolver returns `error_result()` with TODO message. URL pattern matching and platform emoji (🐦) still work. Removed twscrape from dependencies and novel tech research. Reduced twitter tests from ~5 to ~2. Updated test counts: unit ~185-205, total ~310-335. Acceptance criteria X/Twitter metric marked as future work.
  - **Memory budget increased (user directive):** Pod resources: 3Gi request / 6Gi limit (was 2Gi/4Gi). Provides comfortable headroom for Whisper base model + concurrent enrichment.
  - **S-8.1 (major):** Added `openai>=1.30` to pyproject.toml — used directly for vision (image.py), not just transitively via graphiti-core.
  - **S-4.1 (minor):** Added `charset-normalizer>=3.3` to pyproject.toml (encoding detection requirement from Section 2 data quality).
  - **S-8.2 (minor):** Added `WHISPER_MODEL_SIZE: str = "base"` to config.py Settings (was mentioned in Section 8 but missing from config).
  - **S-12.1 (minor):** Fixed confidence level for Section 7b: "[INFERRED]" → "LOW" (matches defined scale).
  - **S-5.1 (minor):** Added traceback sanitization note to dead-letter storage — strip credential paths and mask API key patterns before storing.
  - **S-1.1 (minor):** Clarified vision cost varies by provider in cost envelope.
  - **Open: S-14.1 (major):** Resource impact on flynn-cruse's workloads — needs explicit acknowledgment.

---

## Appendix A: Inputs Required Before Starting

Ask the user for these before writing any code:

1. **Discord bot token**
2. **Target channel IDs** — comma-separated list of Discord channel IDs to ingest
3. **LLM provider + API key** — `openai` (default), `anthropic`, or `groq`
4. **LLM model name** — e.g. `gpt-4o-mini`, `claude-3-5-haiku-20241022`, `llama3-8b-8192`
5. **Vision model** — for image analysis; defaults to `gpt-4o` if provider is OpenAI, else ask
6. **Transcription backend** — `whisper-local` (default, uses openai-whisper), `openai-api`, or `assemblyai`
7. **Ingest mode** — `realtime` (bot), `batch` (DiscordChatExporter JSON), or `both`
8. **Additional sources** — Discord is the only source in Phase A. Future sources (Slack, email, RSS) add ingestion modules that produce the same `raw_payload` shape. No additional input needed now.

If the user says "use defaults," proceed with: OpenAI / gpt-4o-mini / vision=gpt-4o / whisper-local / realtime.

**If using Anthropic or Groq:** also ask for `OPENAI_API_KEY` (needed for embeddings). Explain why.

---

## Appendix B: Project Structure

```
seed-storage/
├── CLAUDE.md
├── Dockerfile                   # Python 3.12 + ffmpeg + supervisord + whisper model
├── supervisord.conf             # process manager: bot + 2 workers + beat + health
├── manifest.json                # ant-keeper task manifest (see Section 7)
├── docker-compose.yml           # LOCAL DEV ONLY — Redis + Neo4j for testing
├── pyproject.toml
├── .env.example                 # local dev overrides — NOT used in production
├── .gitignore                   # must include .env
├── README.md
├── infra/
│   └── k8s/
│       └── neo4j.yaml           # Neo4j StatefulSet + Service for ant-keeper namespace
└── seed_storage/
    ├── __init__.py
    ├── config.py                # pydantic-settings — reads env vars (ant-keeper) or .env (local)
    ├── graphiti_client.py       # Graphiti singleton, provider branching
    ├── dedup.py                 # Redis-backed DedupStore
    ├── circuit_breaker.py       # Redis-backed circuit breaker
    ├── health.py                # HTTP health endpoint on :8080 for ant-keeper liveness
    ├── notifications.py         # fire-and-forget Discord webhook alerts (circuit breaker, budget, dead letters)
    ├── ingestion/
    │   ├── __init__.py
    │   ├── bot.py               # discord.py real-time ingestion → generic raw_payload
    │   └── batch.py             # DiscordChatExporter JSON import → generic raw_payload
    ├── enrichment/
    │   ├── __init__.py
    │   ├── dispatcher.py        # ContentDispatcher — routes URLs to resolvers
    │   ├── models.py            # ResolvedContent — CANONICAL shared type location
    │   └── resolvers/
    │       ├── __init__.py
    │       ├── base.py          # BaseResolver ABC
    │       ├── webpage.py       # trafilatura + readability-lxml fallback
    │       ├── youtube.py       # yt-dlp + transcription backend
    │       ├── video.py         # generic video files via ffmpeg + transcription
    │       ├── image.py         # vision LLM
    │       ├── pdf.py           # docling + unstructured fallback
    │       ├── github.py        # GitHub REST API
    │       ├── twitter.py       # TODO stub — returns error_result(), URL pattern match only
    │       └── fallback.py      # best-effort HTML extraction
    ├── smoke_test.py              # post-deploy verification (runnable as -m seed_storage.smoke_test)
    ├── expansion/
    │   ├── __init__.py
    │   ├── cli.py               # CLI wrapper: python -m seed_storage.expansion.cli expand <url>
    │   ├── frontier.py          # Redis frontier operations (add, score, pick, metadata)
    │   ├── policies.py          # per-resolver depth policies and priority scoring
    │   └── scanner.py           # Celery beat task: scan frontier, enqueue expansions
    ├── worker/
    │   ├── __init__.py
    │   ├── app.py               # Celery app + queue config
    │   ├── tasks.py             # enrich_message, ingest_episode, expand_from_frontier
    │   ├── dead_letters.py      # dead_letter() storage + replay logic
    │   └── replay.py            # CLI: python -m seed_storage.worker.replay (--list, --all, --one)
    └── query/
        ├── __init__.py
        └── search.py            # graphiti.search() wrapper
scripts/
    ├── query.py                 # CLI query interface (outputs JSON: [{content, score, metadata}, ...])
    └── rollback.py              # --after timestamp, removes recent episodes
tests/
    ├── conftest.py              # shared fixtures, NO infrastructure deps
    ├── unit/
    │   └── ...
    ├── integration/
    │   ├── conftest.py          # Redis + Neo4j fixtures (SEPARATE)
    │   └── ...
    ├── e2e/
    │   └── ...
    └── security/
        └── ...
```

---

## Appendix C: Implementation Details

### config.py

Use `pydantic-settings`. All infrastructure constants from Section 4 are defined here. In ant-keeper deployment, env vars come from the manifest + credential injection. For local dev, falls back to `.env` file.

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",              # fallback for local dev — ignored in ant-keeper (no .env exists)
        env_file_encoding="utf-8",
        env_ignore_empty=True,
    )

    # --- Credentials (env-mode: proxy token from iron-proxy; file-mode: read at startup) ---
    DISCORD_BOT_TOKEN: str = ""             # populated from file-mode credential (see validator)
    DISCORD_BOT_TOKEN_PATH: str | None = None  # set by ant-keeper for file-mode injection
    OPENAI_API_KEY: str                     # env-mode (iron-proxy proxied) — always required for embeddings
    LLM_API_KEY: str = ""                   # alias: populated from provider-specific env var (see validator)
    ANTHROPIC_API_KEY: str | None = None    # env-mode, only if LLM_PROVIDER=anthropic
    GROQ_API_KEY: str | None = None         # env-mode, only if LLM_PROVIDER=groq
    GITHUB_TOKEN: str | None = None         # env-mode, optional
    ASSEMBLYAI_API_KEY: str | None = None   # env-mode, only if backend=assemblyai
    NEO4J_PASSWORD: str = ""                # populated from file-mode credential (see validator)
    NEO4J_PASSWORD_PATH: str | None = None  # set by ant-keeper for file-mode injection

    # --- Configuration ---
    DISCORD_CHANNEL_IDS: list[int] = []      # comma-separated in env → parsed by validator below. Discord-specific; future sources add their own config vars.
    LLM_PROVIDER: Literal["openai", "anthropic", "groq"] = "openai"
    LLM_MODEL: str = "gpt-4o-mini"
    VISION_PROVIDER: Literal["openai", "anthropic", "groq"] | None = None  # defaults to LLM_PROVIDER if None (see validator)
    VISION_MODEL: str = "gpt-4o"
    TRANSCRIPTION_BACKEND: Literal["whisper-local", "openai-api", "assemblyai"] = "whisper-local"

    # --- Infrastructure (defaults = ant-keeper K8s services) ---
    NEO4J_URI: str = "bolt://neo4j.ant-keeper.svc:7687"
    NEO4J_USERNAME: str = "neo4j"
    REDIS_URL: str = "redis://redis.ant-keeper.svc:6379/2"   # DB 2 — ant-keeper uses DB 0

    # --- Tuning ---
    SEMAPHORE_LIMIT: int = 10
    GRAPHITI_RATE_LIMIT_PER_MINUTE: int = 100
    CIRCUIT_BREAKER_THRESHOLD: int = 5
    CIRCUIT_BREAKER_COOLDOWN: int = 300
    DAILY_LLM_BUDGET: float = 5.00  # USD — graph_ingest pauses when exceeded, resets at midnight UTC
    ESTIMATED_COST_PER_CALL: float = 0.0004  # USD — gpt-4o-mini entity extraction estimate
    FRONTIER_AUTO_ENABLED: bool = True  # periodic scanner processes frontier automatically
    FRONTIER_AUTO_THRESHOLD: int = 5  # minimum priority score for auto-expansion
    FRONTIER_SCAN_INTERVAL: int = 300  # seconds between frontier scans
    FRONTIER_BATCH_SIZE: int = 10  # URLs to process per scan cycle
    HARD_DEPTH_CEILING: int = 5  # absolute max depth for any expansion chain (manual or auto)
    MAX_EXPANSION_BREADTH_PER_RESOLVE: int = 20  # max child URLs discovered per resolved content
    WHISPER_MODEL_SIZE: str = "base"  # openai-whisper model size: tiny, base, small, medium, large
    WORKER_CONCURRENCY_RAW: int = 8  # supervisord reads via %(ENV_WORKER_CONCURRENCY_RAW)s
    WORKER_CONCURRENCY_GRAPH: int = 2

    # --- Discord alerts ---
    DISCORD_ALERTS_WEBHOOK_URL: str = ""  # populated from file-mode credential; empty = alerts disabled
    DISCORD_ALERTS_WEBHOOK_PATH: str | None = None  # set by ant-keeper for file-mode injection
    NOTIFICATION_DEBOUNCE_SECONDS: int = 300  # 5 min — prevents duplicate webhook alerts

    @field_validator("DISCORD_CHANNEL_IDS", mode="before")
    @classmethod
    def _parse_channel_ids(cls, v: Any) -> list[int]:
        """Parse comma-separated string from env var into list[int]."""
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v

    @model_validator(mode="after")
    def _load_file_credentials(self) -> "Settings":
        """Read file-mode credentials injected by ant-keeper's iron-proxy."""
        if self.DISCORD_BOT_TOKEN_PATH and not self.DISCORD_BOT_TOKEN:
            self.DISCORD_BOT_TOKEN = Path(self.DISCORD_BOT_TOKEN_PATH).read_text().strip()
        if self.NEO4J_PASSWORD_PATH and not self.NEO4J_PASSWORD:
            self.NEO4J_PASSWORD = Path(self.NEO4J_PASSWORD_PATH).read_text().strip()
        if self.DISCORD_ALERTS_WEBHOOK_PATH and not self.DISCORD_ALERTS_WEBHOOK_URL:
            self.DISCORD_ALERTS_WEBHOOK_URL = Path(self.DISCORD_ALERTS_WEBHOOK_PATH).read_text().strip()
        return self

    @model_validator(mode="after")
    def _resolve_provider_defaults(self) -> "Settings":
        """Map provider-specific key to generic LLM_API_KEY. Resolve VISION_PROVIDER default."""
        if not self.LLM_API_KEY:
            key_map = {"openai": self.OPENAI_API_KEY, "anthropic": self.ANTHROPIC_API_KEY, "groq": self.GROQ_API_KEY}
            resolved = key_map.get(self.LLM_PROVIDER)
            if not resolved:
                raise ValueError(f"No API key found for LLM_PROVIDER={self.LLM_PROVIDER}")
            self.LLM_API_KEY = resolved
        if self.VISION_PROVIDER is None:
            self.VISION_PROVIDER = self.LLM_PROVIDER
        # Validate that the vision provider's API key is available
        vision_key_map = {"openai": self.OPENAI_API_KEY, "anthropic": self.ANTHROPIC_API_KEY, "groq": self.GROQ_API_KEY}
        if not vision_key_map.get(self.VISION_PROVIDER):
            raise ValueError(f"No API key found for VISION_PROVIDER={self.VISION_PROVIDER}")
        return self

    @model_validator(mode="after")
    def _validate_credentials(self) -> "Settings":
        if not self.DISCORD_BOT_TOKEN:
            raise ValueError("DISCORD_BOT_TOKEN is required (set directly or via DISCORD_BOT_TOKEN_PATH)")
        if not self.NEO4J_PASSWORD:
            raise ValueError("NEO4J_PASSWORD is required (set directly or via NEO4J_PASSWORD_PATH)")
        if self.TRANSCRIPTION_BACKEND == "assemblyai" and not self.ASSEMBLYAI_API_KEY:
            raise ValueError("ASSEMBLYAI_API_KEY is required when TRANSCRIPTION_BACKEND=assemblyai")
        return self
```

**Design note:** The dual path (`DISCORD_BOT_TOKEN` direct string vs `DISCORD_BOT_TOKEN_PATH` file path) lets the same config work in both environments:
- **Ant-keeper:** sets `DISCORD_BOT_TOKEN_PATH=/run/credentials/discord-bot-seed-storage/token` — validator reads the file
- **Local dev:** sets `DISCORD_BOT_TOKEN=<raw-token>` in `.env` — used directly

### dedup.py

Redis-backed dedup using two persistent sets — one for messages, one for URLs. Separate sets prevent semantic confusion and future collision risk.

```python
class DedupStore:
    def __init__(self, redis_client: redis.Redis, set_key: str):
        """Instantiate with explicit key — caller chooses 'seed:seen_messages' or 'seed:seen_urls'."""
        ...

    def is_seen(self, key: str) -> bool: ...
    def mark_seen(self, key: str) -> None: ...
    def seen_or_mark(self, key: str) -> bool:
        """Atomic check-and-set using SADD. Returns True if already seen."""
        # SADD returns 0 if member already exists — atomic, no race condition
        ...
```

Keys: SHA256 hex digest of canonical URL (strip `utm_*`, `ref`, `fbclid`, `si`, `t`, `s`; lowercase scheme+host; sort query params). Message dedup uses `{source_type}:{source_id}` composite key (e.g., `discord:123456789012345678`).

### enrichment/resolvers/base.py

```python
class BaseResolver(ABC):
    @abstractmethod
    def can_handle(self, url: str) -> bool: ...

    @abstractmethod
    async def resolve(self, url: str) -> ResolvedContent: ...
```

### enrichment/dispatcher.py

`ContentDispatcher` holds an ordered list of resolvers. `dispatch(url)` iterates resolvers, calls `can_handle`, delegates to the first match, falls back to `FallbackResolver`. Log resolver selection at DEBUG. Log resolution errors at WARNING and return `ResolvedContent.error_result()` rather than raising — a resolution failure must never block graph ingestion.

Resolver priority order:
1. `YouTubeResolver` — matches `youtube.com/watch`, `youtu.be`, `youtube.com/shorts`
2. `GitHubResolver` — matches `github.com/*/*`
3. `TwitterResolver` (TODO stub) — matches `twitter.com`, `x.com` → returns `error_result()` with TODO message
4. `PdfResolver` — URL ends in `.pdf` or content-type is `application/pdf`
5. `ImageResolver` — URL ends in `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.svg` or content-type `image/*`
6. `VideoResolver` — `.mp4`, `.mov`, `.webm`, `.avi`, `.mkv`
7. `WebpageResolver` — everything else with HTTP/HTTPS scheme
8. `FallbackResolver` — catch-all (no scheme, unknown protocol)

### Resolver implementations

**webpage.py:** `trafilatura` with `include_comments=False`, `include_tables=True`. Fallback: `readability-lxml`. Truncate to 8000 tokens. Populate `expansion_urls` with outbound links.

**youtube.py:** `yt-dlp` metadata extraction → caption extraction → audio transcription fallback (per `TRANSCRIPTION_BACKEND`). `text` = description + transcript. Truncate transcript to 12000 tokens.

**image.py:** Vision LLM with prompt: `"Describe this image in detail. Note any text, diagrams, code, charts, screenshots, or data visible. Be specific about what information is conveyed."` Return as `summary` and `text`. Handle inaccessible URLs via `error_result()`. **Vision is provider-agnostic** — uses `VISION_PROVIDER` (defaults to `LLM_PROVIDER`) and `VISION_MODEL` from config. Constructs the appropriate SDK client (`OpenAI`, `Anthropic`, or `Groq`) based on `VISION_PROVIDER`. Ant-keeper handles credential injection and endpoint allowlisting for whichever provider is selected.

**pdf.py:** `docling` first, `unstructured` fallback. Text only. Truncate to 10000 tokens.

**video.py:** Download with `httpx` → temp file → `ffmpeg` audio extraction → transcribe. `finally` block cleans temp files.

**github.py:** GitHub REST API (unauthenticated or `GITHUB_TOKEN`). Fetch metadata + README. `text` = `"{repo}: {description}\n\nTopics: {topics}\n\n{readme}"`.

**twitter.py (TODO stub):** Returns `error_result(url, "Twitter/X resolver not implemented")`. URL pattern matching (`twitter.com`, `x.com`) still triggers platform emoji (🐦). No content extraction.

**fallback.py:** `httpx` GET + BeautifulSoup title + meta description. Never raises.

### worker/app.py

```python
app = Celery("seed_storage")
app.config_from_object({
    "broker_url": config.REDIS_URL,
    "result_backend": config.REDIS_URL,
    "task_routes": {
        "seed_storage.worker.tasks.enrich_message": {"queue": "raw_messages"},
        "seed_storage.worker.tasks.ingest_episode": {"queue": "graph_ingest"},
        "seed_storage.worker.tasks.expand_from_frontier": {"queue": "graph_ingest"},
        "seed_storage.worker.tasks.scan_frontier": {"queue": "graph_ingest"},
    },
    "task_acks_late": True,
    "task_reject_on_worker_lost": True,
    "worker_prefetch_multiplier": 1,
    "beat_schedule": {
        "frontier-auto-scan": {
            "task": "seed_storage.worker.tasks.scan_frontier",
            "schedule": config.FRONTIER_SCAN_INTERVAL,  # 300 seconds default
            "options": {"queue": "graph_ingest"},
        },
    },
})
```

### worker/tasks.py

**`enrich_message(raw_payload: dict)`** — dedup check (via `{source_type}:{source_id}`) → extract URLs from `content` → canonicalize → dedup per-URL → dispatch → cache in Redis → build enriched_payload → enqueue `ingest_episode`.

**`ingest_episode(enriched_payload: dict)`** — Episode 1: message text with `source_description=f"{source_type.title()} #{source_channel}"` (e.g., "Discord #imessages", "Slack #engineering", "Email inbox") and `group_id="seed-storage"`. Episode 2+: one per resolved content with non-empty text, `source_description=f"content_from_{source_type.title()}_#{source_channel}:{content_type}"`. After ingestion: for each resolved content's `expansion_urls`, compute priority and write to frontier (`seed:frontier` ZADD, `seed:frontier:meta:{hash}` HSET). Publish reaction event to `seed:reactions` pubsub.

**`expand_from_frontier(url_hash: str, max_depth: int | None = None)`** — pull URL from frontier → check resolution cache → resolve if needed → check episode dedup → ingest if needed → discover new URLs → add to frontier with decremented depth priority. Called by frontier scanner (auto) or directly (manual/programmatic). Results feed into `graph_ingest` queue via `ingest_episode.delay()`.

**`scan_frontier()`** — Celery beat periodic task (every `FRONTIER_SCAN_INTERVAL` seconds). If `FRONTIER_AUTO_ENABLED` is false, returns immediately. Otherwise: picks top `FRONTIER_BATCH_SIZE` URLs from `seed:frontier` where `score >= FRONTIER_AUTO_THRESHOLD` and `depth <= resolver_policy[resolver_hint]`. For each, enqueues `expand_from_frontier.delay(url_hash)`. Logs count of URLs enqueued at INFO.

### graphiti_client.py

Provider branching:
- `openai` → `OpenAIClient` + `OpenAIEmbedder`
- `anthropic` → `AnthropicClient` + `OpenAIEmbedder` (requires separate `OPENAI_API_KEY`)
- `groq` → `GroqClient` + `OpenAIEmbedder` (requires separate `OPENAI_API_KEY`)

Call `build_indices_and_constraints()` on startup. Export `get_graphiti()`. All `add_episode()` calls must use `group_id="seed-storage"` to maintain a unified graph.

Also export `get_vision_client()` — returns the appropriate SDK client based on `VISION_PROVIDER` config (`OpenAI`, `Anthropic`, or `Groq`). Used by `image.py` resolver. Separate from the Graphiti LLM client — vision provider can differ from `LLM_PROVIDER`.

### ingestion/bot.py

`intents.message_content = True`. Filter to `config.DISCORD_CHANNEL_IDS`. Note: `intents.reactions` is NOT needed — the bot adds reactions (requires `Add Reactions` permission, already specified) but does not need to receive reaction events from other users. Skip empty + bot. Add 📥 reaction immediately. Map Discord message to generic `raw_payload`:

```python
raw_payload = {
    "source_type": "discord",
    "source_id": str(message.id),
    "source_channel": message.channel.name,
    "author": message.author.display_name,
    "content": message.content,
    "timestamp": message.created_at.isoformat(),
    "attachments": [a.url for a in message.attachments],
    "metadata": {
        "channel_id": str(message.channel.id),
        "author_id": str(message.author.id),
        "guild_id": str(message.guild.id) if message.guild else None,
    },
}
```

Call `enrich_message.delay(raw_payload)`. Subscribe to `seed:reactions` Redis pubsub — when reaction events arrive, call `message.add_reaction()` on the original message.

### ingestion/batch.py

Accept DiscordChatExporter JSON path. Parse `.messages` array. Map each DiscordChatExporter message to the generic `raw_payload` shape (set `source_type="discord"`, `source_id` from message ID, `source_channel` from channel name, Discord-specific fields into `metadata`). Call `enrich_message.delay(raw_payload)`. Log progress every 100 messages. Cap at 5000 messages per run (Section 5, cost protection).

**Operator usage:**
```bash
# Inside the pod or local dev:
python -m seed_storage.ingestion.batch /path/to/export.json
python -m seed_storage.ingestion.batch /path/to/export.json --offset 5000  # resume after first 5000
```

### query/search.py

```python
async def search(query: str, num_results: int = 10) -> list[EntityEdge]:
    results = await get_graphiti().search(query, group_ids=["seed-storage"], num_results=num_results)
    return results
```

**`EntityEdge` → CLI output mapping:**

Graphiti's `EntityEdge` contains: `fact` (str, the relationship description), `fact_embedding` (list[float]), `source_node` (EntityNode), `target_node` (EntityNode), `created_at`, `expired_at`, `valid_at`, `invalid_at`, `episodes` (list[str]). The CLI transforms this to a simpler JSON shape:

```python
# scripts/query.py — transformation
def edge_to_result(edge: EntityEdge) -> dict:
    return {
        "content": edge.fact,                     # the relationship/fact text
        "score": getattr(edge, "_score", 0.0),    # NOTE: _score is not a documented Graphiti attribute — may always be 0.0. Verify against current Graphiti version; if unavailable, omit or compute from embedding distance.
        "metadata": {
            "source_entity": edge.source_node.name,
            "target_entity": edge.target_node.name,
            "valid_at": edge.valid_at.isoformat() if edge.valid_at else None,
            "episodes": edge.episodes,            # list of episode UUIDs
        },
    }
```

**Operator usage:**
```bash
# CLI query interface — outputs JSON array of SearchResult objects:
# [{content: str, score: float (0-1), metadata: dict}, ...]
# Max 10 results by default. Use --limit N to change.
python scripts/query.py "What do we know about Project Alpha?"
python scripts/query.py "agent deployment" --limit 5
```

### pyproject.toml dependencies

```toml
[project]
dependencies = [
    "discord.py>=2.3",
    "celery[redis]>=5.3",
    "redis>=5.0",
    "graphiti-core>=0.3,<0.5",  # pin upper bound — breaking changes likely in new majors
    "pydantic-settings>=2.0",
    "httpx>=0.27",
    "trafilatura>=1.12",
    "readability-lxml>=0.8",
    "yt-dlp>=2024.0",
    "openai-whisper>=20231117",
    "docling>=2.0",
    "unstructured[pdf]>=0.14",
    "urlextract>=1.9",
    "beautifulsoup4>=4.12",
    "ffmpeg-python>=0.2",
    "neo4j>=5.0,<6.0",         # explicit pin — graphiti-core pulls transitively but version matters
    "openai>=1.30",             # explicit pin — used directly for vision (image.py), also pulled by graphiti-core
    "charset-normalizer>=3.3",  # encoding detection for resolvers (Section 2 data quality)
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "respx>=0.21",       # httpx mocking
    "ruff>=0.4",
]
```

---

## Appendix D: Spec Review Checklist

Run this review before implementation:

1. **COMPLETENESS:** Are there fields/features in the design or user vision that didn't make it into this spec?
2. **CROSS-MODULE CONSISTENCY:** Do types in module A match what module B expects? Do async/sync boundaries align? Do data shapes match at every handoff?
3. **CONSTANTS:** Are service URLs, usernames, connection strings pinned once? (Check: Redis on DB 2, Neo4j on ant-keeper svc, no port overlaps.)
4. **OPERATOR WORKFLOW:** For every feature, can you describe the concrete steps a user takes to set it up?
5. **FAILURE MODES:** For every operation, what happens when it fails? For every external call, what if it's down?
6. **COST GUARDRAILS:** Batch limits, rate limits, cost estimates for LLM calls?
7. **SECURITY / IRON-PROXY:** Is every credential categorized (env-mode vs file-mode)? Are proxy_targets correct? Are non-HTTP credentials using file-mode? Is egress allowlist complete?
8. **VERIFICATION:** Smoke test exists? Health endpoint works? Can "done" be verified without manual checking?
9. **CONTINUITY:** Can a future agent pick this up from the docs alone?
10. **IMPACT:** Are other users (flynn-cruse) affected?
11. **ANT-KEEPER COMPATIBILITY:** Does the manifest match what the code expects? Are resource limits adequate for Whisper + Celery workers? Is supervisord config consistent with the entry points?

---

## Appendix E: Implementation Prompt

Once the spec passes review:

```
You are implementing the seed-storage replacement pipeline.

READ THESE FILES FIRST (in order):
1. This spec — your complete implementation reference
2. seed_storage/enrichment/models.py — canonical shared types, import exactly
3. seed_storage/config.py — all constants, never hardcode your own
4. Section 4 (Infrastructure Constants) — authoritative service URLs
5. Section 6 (Security) — credential injection model (iron-proxy vs file-mode)
6. Section 7 (Deployment) — ant-keeper daemon manifest, Dockerfile, supervisord

RULES:
- This deploys as an ant-keeper daemon (K8s Deployment) — NOT docker-compose in production
- Graph backend is Neo4j only — no FalkorDB
- Discord bot token and Neo4j password use FILE-MODE injection (non-HTTP protocols)
- HTTP API keys use iron-proxy ENV-MODE (proxy tokens, not real keys)
- Config must work in both environments: ant-keeper (env vars) and local dev (.env file)
- Redis DB 2, not DB 0 (ant-keeper uses DB 0)
- Unit tests must pass with zero infrastructure (no Redis, no Neo4j, no network)
- Expected test counts: ~185-205 unit, ~69 integration, ~38 e2e, ~20 security (~310-335 total)
- Integration test fixtures in tests/integration/conftest.py, NOT root conftest.py
- A resolution failure must never block graph ingestion
- Circuit breaker state in Redis, shared across all workers
- Circuit breaker open/close → fire-and-forget Discord webhook alert
- Alerts module: fire-and-forget, debounced, never blocks pipeline. No webhook = alerts disabled.
- Secrets never in logs — mask to last 4 chars
- Health endpoint on :8080 required for ant-keeper liveness probe
- Supervisord manages bot + 2 workers + beat + health as separate processes
- Expansion uses frontier (Redis sorted set), NOT direct task chaining from ingest_episode
- All add_episode() calls use group_id="seed-storage" — NEVER per-channel group_ids
- source_description="Discord #{channel_name}" for message episodes — enables source filtering
- Discord reactions (📥⚙️🏷️🧠❌🔁 + platform emojis) via Redis pubsub callback from workers to bot
- `raw_payload` is source-agnostic — enrichment and graph ingest must NOT reference Discord-specific fields. Only ingestion modules (bot.py, batch.py) know about Discord.
- Run ruff before declaring done
- Execute fully: code, test, deploy, verify, document

ACCEPTANCE CRITERIA:
[See Section 11]

WHEN DONE, report:
- Files created/modified (with line counts)
- Tests passing (count by category: unit, integration, e2e, security) — expected ~310+ total
- Smoke test result
- Ant-keeper deployment status (pod running, health check passing)
- Discord webhook alerts verified (circuit breaker test alert sent, or noted if webhook not configured)
- Any spec ambiguities resolved (and how)
- Any concerns about cross-module compatibility
- Context saved to CLAUDE.md for future sessions
```
