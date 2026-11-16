# Architecture

This document describes how `doomscroller` is put together: the processes, the modules,
the data flow and the invariants the code is built around. For the user-facing tour see
[`README.md`](../README.md); for the database in detail see [data-model.md](data-model.md).

## Design goals

Four properties drive most of the implementation:

1. **Nothing that arrived is lost.** Every Telegram event is appended to
   `message_events`; editing or deleting a post updates `messages` but never erases the
   event history. The cursor and the rows it covers are committed in the same
   transaction, so a crash causes re-processing, never silent loss.
2. **Re-polling is safe.** Ingest is idempotent: the same `new` event stored twice is a
   no-op, not a duplicate.
3. **"Unread" is a query, not a second cursor.** The scorer decides what to grade from
   `post_scores.status` and message timestamps, so there is no separate pointer that can
   drift out of sync with the archive.
4. **The rubric is data.** The scoring rules live in editable markdown files under
   `scoring/`, not in Python constants; the code only knows how to assemble and validate
   them.

## Processes

```
Telegram ──MTProto──▶ poller / watcher ──SQL──▶ PostgreSQL ◀──SQL── scorer ──HTTPS──▶ LLM
   (Telethon, one session)   (doomscroller)          │                  (score --loop)
                                                     │
                                     MCP clients ◀───┘
                               (stdio / streamable-http, read-only archive + scores,
                                plus its own opt-in Telethon session for live tools)
```

| Role | Where it runs | Holds | Never touches |
|---|---|---|---|
| `db` | container | PostgreSQL data | — |
| poller (`doomscroller poll --loop`) | container | Telegram session, ingest cursor | LLM, scores |
| scorer (`doomscroller score --loop`) | container | `DATABASE_URL`, `SCORER_*` | Telegram credentials, session |
| MCP server | client side (host) | `DATABASE_URL`; Telegram credentials only for live tools | ingest cursor, scoring writes |
| webhook forwarder | inside the MCP process | MCP's own Telethon session | archive writes |

The MCP server is deliberately **not** a compose service: it speaks stdio to a coding
agent running on the same machine, and needs only read access to the database.

`docker-compose.yml` mirrors this split: the scorer service receives `SCORER_*` variables
and a read-only mount of `./scoring`, but no `env_file`, so Telegram secrets cannot leak
into a container whose only job is reading the archive and calling a paid model.

## Module map

| Module | Responsibility |
|---|---|
| `doomscroller/cli.py` | argparse entry point; every command's orchestration and output formatting |
| `doomscroller/config.py` | `.env` loading, `Settings` (Telegram/ingest) and `ScorerConfig` (scoring), proxy parsing |
| `doomscroller/db.py` | engine creation, `session_scope`, `session_factory`, `init_db`, healthcheck |
| `doomscroller/models.py` | all nine SQLModel tables |
| `doomscroller/store.py` | event recording (idempotent, append-only), message reads, stats, webhooks rows |
| `doomscroller/ingest.py` | `poll` (cursor-based backfill) and `watch` (live handler), cursor transaction guarantee |
| `doomscroller/subscriptions.py` | reference normalisation, subscribe/unsubscribe/enable, cursor storage |
| `doomscroller/telegram_client.py` | Telethon client construction, connection, login flow, proxy wiring |
| `doomscroller/telegram_ops.py` | live listing/sending/export helpers, send allowlist and rate limit |
| `doomscroller/mcp_server.py` | MCP tool definitions and the read/live split |
| `doomscroller/webhooks.py` | forwarding arriving messages to per-chat webhook URLs |
| `doomscroller/ocr.py` | vision-model OCR of photo posts into `media_ocr` |
| `doomscroller/scoring/agent.py` | the scoring loop: select unread posts, call the model, persist atomically |
| `doomscroller/scoring/prompts.py` | assemble the system/user prompt from the markdown modules |
| `doomscroller/scoring/rubric.py` | parse and validate the model's JSON answer, compute KPD/Eff |
| `doomscroller/scoring/state.py` | `channel_states` projection: EWMA, tags, anchors, shift/peak events |
| `doomscroller/scoring/llm.py` | minimal OpenAI-compatible async client with bounded retries |
| `doomscroller/scoring/calibrate.py` | locate rubric anchor texts in the archive, diff model vs reference scores |
| `doomscroller/compare.py`, `chart.py` | cross-channel statistics and the self-contained HTML rating page |

## Data flow

### Ingest (poll)

`poll` asks Telegram for messages newer than `subscriptions.cursor_message_id`, in
`BATCH_SIZE` batches, oldest first, and calls `store.record_event(...)` for each. After a
batch is stored, the cursor, `last_polled_at`, `message_count` and `last_error` are
updated **and committed in the same transaction as the messages**. If anything raises,
the transaction rolls back and the next run re-reads the batch.

`watch` stays connected and reacts to `NewMessage` / `MessageEdited` / `MessageDeleted`
events. Deletes are only recorded when Telegram supplies a `chat_id` (otherwise the
update is skipped — see `ingest.watch`).

### Storage semantics

`store.record_event` implements the archive rules:

* `new` for an already-known message is a **no-op** (this is what makes re-polling safe).
* `edited` appends an event and updates the current row; `edited_at` is set to the wall
  clock, not to Telegram's edit date.
* `deleted` appends an event and flips `messages.deleted`, but keeps the archived text.
* `message_events` is never updated or deleted — that is the raw history.

### Scoring

`score` calls `run_agent`, which for each enabled subscription walks messages in
`message_id` order and yields the ones that need a score. A post needs one when:

* it has no `post_scores` row,
* its row is `failed` and `attempts < SCORER_MAX_ATTEMPTS`,
* it was edited after it was scored (`rescore_edited`, on by default), or
* OCR text for it was written after it was scored.

A post with neither caption nor OCR text is skipped silently; shorter than
`SCORER_MIN_CHARS` is recorded as `skipped` with a reason. Everything else is sent to
the model, validated by `rubric.finalize`, and written by `store_score` — the score row
*and* the channel-state update in one transaction.

`--limit` bounds **model calls**, not examined posts: walking past thousands of
one-liners is free and must not consume the tick's budget.

### Reading

The CLI and MCP read `messages` (current state) and the append-only logs
(`message_events`, `channel_events`). `chat_history` walks the whole archive of a chat in
`iter_all_messages` batches and compresses each message to id/date/sender/gist so the
result fits in one context window.

## Database layer

`db.py` keeps a single engine (per resolved `DATABASE_URL`) with `pool_pre_ping=True`.
Connections to PostgreSQL get a bounded `connect_timeout` (default 5 s, `DB_CONNECT_TIMEOUT`
overrides), because a blackholed docker-proxy otherwise hangs an MCP tool call until the
client kills it with a bare "Request timed out".

Two session helpers encode different transaction policies:

* `session_scope()` — commits on success, rolls back on exception. The default.
* `session_factory()` — the caller owns the transaction; used where one commit per item
  matters (OCR keeps every image already read if the pass is interrupted).

Schema creation is `SQLModel.metadata.create_all()` in `init_db()`; there is no Alembic.
That is enough for a single-owner archive, but a column type change is a manual migration
(see [development.md](development.md#schema-changes)).

Tests use the same code path with SQLite when `TEST_DATABASE_URL`/`DATABASE_URL` are not
set (`tests/conftest.py`), which is why queries avoid PostgreSQL-only SQL.

## Configuration

`config._load_env()` loads `.env` from the current directory or the repo root with
`override=False`, so an exported environment variable always wins over `.env`. Two
independent configs exist:

* `load_settings()` — Telegram credentials, session name, proxy, data dir, ingest
  knobs, send allowlist. Raises if `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` are missing.
* `load_scorer_config()` — rubric dir, model endpoint/key, sampling, limits, intervals.
  Deliberately does not require any Telegram variable: the scorer must be runnable on a
  machine that has only the database and an API key.

`SCORER_API_KEY` falls back to `NANOGPT_API_KEY`; `SCORER_BASE_URL` defaults to NanoGPT;
`SCORER_MODEL` defaults to `deepseek-chat`.

## MCP server

`mcp_server.py` builds one `MCPServer` and registers tools in three families:

* **archive read** — database only (`read_messages`, `new_messages`, `search_messages`,
  `message_events`, `chat_history`, `archive_stats`, `list_picked_chats`,
  `connector_status`);
* **scoring read** — database only (`post_scores`, `channel_states`, `channel_events`);
* **live Telegram** — lazily opens its own Telethon session
  (`TELEGRAM_MCP_SESSION`, default `<session>_mcp`) on first use, so it never contends
  with the poller for the same session file (`list_chats`, `send_message`).

Sending is refused unless the chat is in `SEND_ALLOWLIST`; the refusal is returned as
JSON rather than raised, because the MCP SDK would otherwise replace it with a generic
error. Webhook subscriptions require `WEBHOOK_TOKEN` for the same "off by default"
reason — the forwarder would otherwise be an open SSRF gadget.

`connector_status` never connects to Telegram: it reports configuration and stored state,
bounded by the database timeout, and returns a `hint` when the database is unreachable.

## Failure model

| Failure | Behaviour |
|---|---|
| Telegram poll error | recorded in `subscriptions.last_error`; other chats keep polling; cursor unchanged |
| storage error mid-batch | whole batch rolls back; cursor unchanged; next run re-reads |
| LLM error (retryable) | `ChatClient` retries `SCORER_RETRIES` times, then the post is `failed` with `attempts+1`; the rest of the channel's tick stops |
| malformed model JSON | `RubricError` → `failed` with detail; the loop continues with the next post |
| OCR error | one `media_ocr` row with `status='error'`; the pass continues |
| webhook endpoint down | delivery counted in `failed`, `last_error` stored; listener keeps running |
| database unreachable | scorer tick logs and reschedules; MCP tools fail fast with a structured error |

## Extension points

* **Rubric** — add or edit `scoring/*.md`. `prompts.load_modules` hashes the concatenation
  into `prompt_version` (12 hex chars) which is stored on every score row, so old rows
  can be traced to the rubric that produced them.
* **New MCP tool** — decorate a function in `mcp_server.py` with `@mcp.tool()`; keep the
  database/Telegram split explicit.
* **New ingest source** — `store.record_event` is the only write path into
  `messages`/`message_events`; anything that can produce an event can reuse it.

## Non-goals

* Multi-user access control: the archive is a personal one; the database credentials are
  the security boundary.
* Distributed polling: one Telegram session per process is a hard Telegram constraint,
  not a design preference.
* Real-time scoring: the scorer is a batch loop by design, so a slow or expensive model
  cannot block ingest.
