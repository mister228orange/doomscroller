# MCP reference

The MCP server exposes the archive, the scores and (opt-in) live Telegram to an agent. It
is normally started by the client over stdio on the same machine as the database access;
it is not a compose service.

```bash
doomscroller mcp                                        # stdio
doomscroller mcp --transport streamable-http --host 127.0.0.1 --port 8765
doomscroller mcp --transport sse
```

Server name: `doomscroller`; version: the package version. Every tool returns a JSON
string (`ensure_ascii=False, indent=2`), so an agent can read it directly or parse it.

## Requirements by tool family

| Family | Tools | Needs |
|---|---|---|
| Archive read | `list_picked_chats`, `connector_status`, `read_messages`, `new_messages`, `search_messages`, `message_events`, `chat_history`, `archive_stats` | `DATABASE_URL` only |
| Scoring read | `post_scores`, `channel_states`, `channel_events` (+ the scoring block of `connector_status`) | `DATABASE_URL` only |
| Webhooks | `webhook_subscribe`, `webhook_unsubscribe`, `webhook_list` | `DATABASE_URL`, `WEBHOOK_TOKEN` to subscribe |
| Live Telegram | `list_chats`, `send_message` | `TELEGRAM_*`, `DATA_DIR`, its own session; `SEND_ALLOWLIST` to send |

The live family opens its Telethon client lazily on first use, with a **separate session
file** (`TELEGRAM_MCP_SESSION`, default `<TELEGRAM_SESSION>_mcp`), so it cannot contend
with the poller for one SQLite session. Ingest is deliberately not exposed: storing
messages belongs to the process that owns the poller's session and cursor.

`chat` arguments accept a subscription reference (`design4TOR`), `@username`, a `t.me`
link, or a numeric chat id. A chat that is subscribed but has no resolved `chat_id` yet
is an error with a hint to poll it once.

## Archive read tools

### `list_picked_chats()`

Every subscription with `chat_id`, `title`, `kind`, `enabled`, `cursor_message_id`,
`message_count`, `last_polled_at`, `last_error`.

### `connector_status()`

Readiness report; never connects to Telegram.

```jsonc
{
  "tool_version": "0.3.0",
  "telegram": { "api_id_set": true, "api_hash_set": true,
                "session_file": "/app/scroller_session.session",
                "session_present": true, "proxy": "direct" },
  "database": { "ok": true, "tables": 9 },
  "subscriptions": { "total": 5, "enabled": 5,
                     "stale_or_failing": [ { "chat": "x", "seconds_since_poll": 7200, "last_error": null } ] },
  "counts": { "subscriptions": 5, "messages": 735, "message_events": 812, "...": "..." },
  "scoring": { "channels": 4, "scored_posts": 191, "skipped_posts": 43, "failed_posts": 0,
               "pending_posts": 501, "models_in_use": ["deepseek/deepseek-v4-pro"],
               "prompt_versions": ["c163f83fad1d"], "last_scored_at": "2026-11-08T17:21:00+00:00" }
}
```

A subscription is "stale" when it has a `last_error`, has never been polled, or was last
polled more than an hour ago. The database probe is bounded by `DB_CONNECT_TIMEOUT`
(default 5 s); on failure the `database` block carries `ok: false`, the error and, where
recognisable, a `hint`.

### `read_messages(chat, limit=50, before_id=None, after_id=None)`

Newest first, up to 1000 rows. `before_id` pages to older messages, `after_id` to newer;
both are Telegram message ids, not primary keys.

Message shape:

```jsonc
{ "chat_id": -1001234567890, "message_id": 4321, "date": "2026-11-08T12:00:00+00:00",
  "sender": "Channel name", "sender_id": 123, "text": "...", "reply_to": null,
  "edited": null, "deleted": false }
```

### `new_messages(chat, since_id=None)`

Everything newer than the stored cursor (or than `since_id`). Read-only: it does not
advance the cursor.

### `search_messages(query, chat=None, limit=20)`

Case-insensitive substring search over `messages.text` and `messages.sender_name`,
optionally scoped to one chat. Limit is clamped to 1–200.

### `message_events(chat, message_id)`

The append-only event log for one message, oldest first:
`{ "event": "new"|"edited"|"deleted", "at": "...", "text": "..." }`.

### `chat_history(chat, limit=280, after_id=None, include_deleted=False)`

The entire stored history compressed into one JSON array, oldest first. One item per
message: `id`, `date`, `from`, `t` (first `limit` characters of the text with URLs,
control characters and whitespace runs removed) and `more: true` when truncated.
`limit=0` keeps no text. This is the "read a whole channel in one context" tool; for
edits, replies or raw payloads use `read_messages` / `message_events`.

### `archive_stats()`

Row counts: `subscriptions`, `subscriptions_enabled`, `messages`, `message_events`,
`detected_events`, `deleted_messages` (and `messages_in_chat` when a chat is given).

## Scoring read tools

The MCP server never writes scores. It reports what is in the database, including the
models and prompt versions that actually produced the rows.

### `post_scores(chat, limit=20, include_skipped=False)`

Newest first, up to 500 rows. Each row contains the six axes, `kpd`, `eff`, the
classification (`genre`, `primary`, `secondary`, `thesis`, `density_type`, `flags`), the
channel-relative view (`deviation`, `trend_position`, `shift_candidate`, `confidence`),
`nearest_anchors`, `selfcheck`, `attempts`, `detail`, `model`, `prompt_version`,
`scored_at`.

### `channel_states()`

Every `channel_states` row, ordered by `updated_at` desc, as `state.scoring.state.state_snapshot`
output: profile, summary, drift note, counters, means/deltas, KPD/Eff, tag distribution,
the last five local anchors, recent events and the pending count for that channel.

### `channel_events(chat, limit=20)`

Trajectory events (`shift` / `peak` / `summary`) newest first, with `kind`, `at`,
`message_id` and `note`.

## Webhook tools

Forward every message arriving in a chat to an HTTP endpoint. The body is the raw
Telethon update (`{chat_id, message_id, raw}`); the token is sent as
`X-Webhook-Token: $WEBHOOK_TOKEN`, not in the URL.

| Tool | Notes |
|---|---|
| `webhook_subscribe(chat, url)` | absolute http(s) URL; re-subscribing replaces the URL; refused without `WEBHOOK_TOKEN` |
| `webhook_unsubscribe(chat)` | takes effect on the next message — the forwarder re-reads the table |
| `webhook_list()` | every webhook with `delivered`, `failed`, `last_error`, `last_delivered_at` |

Delivery runs on the MCP process's Telethon client, so it lives as long as that process:
under stdio that is one client session, for a permanent forwarder use
`--transport streamable-http`.

## Live Telegram tools

These need Telegram credentials and open the MCP's own session on first call.

### `list_chats(limit=200)`

Dialogs visible to the account: `chat_id`, `title`, `username`, `kind`, `unread`.
Use it to discover a reference for `read_messages` or `send_message`.

### `send_message(chat, text)`

Sends as the **real user account**, not a bot. Denied unless the chat matches
`SEND_ALLOWLIST` (exact normalised reference or `*`), rate-limited by
`SEND_RATE_PER_MIN`. A refusal is returned as
`{"sent": false, "refused": true, "reason": "..."}` with exit-style semantics preserved
in JSON, because an MCP client would otherwise replace a raised error with a generic
message.

## Client configuration

Any MCP client that can run a stdio command works. The generic shape:

```jsonc
{
  "mcpServers": {
    "doomscroller": {
      "command": "python",
      "args": ["-m", "doomscroller", "mcp"],
      "env": {
        "DATABASE_URL": "postgresql+psycopg://doomscroller:doomscroller@127.0.0.1:5434/doomscroller",
        "TELEGRAM_API_ID": "...",        // only for list_chats / send_message
        "TELEGRAM_API_HASH": "...",
        "DATA_DIR": "/home/me/doomscroller"
      }
    }
  }
}
```

Client-specific snippets for DSH, Claude Code, Codex CLI, Cursor, Gemini CLI and VS Code
are in the README, [§ MCP: use the archive from an agent](../README.md#mcp-use-the-archive-from-an-agent).

For HTTP transport, clients connect to `http://127.0.0.1:8765/mcp` with
`--transport streamable-http`.

## Security notes

* **The archive tools are read-only.** Ingest is not exposed over MCP, so an agent cannot
  move the cursor or corrupt the log.
* **Sending is opt-in twice**: `SEND_ALLOWLIST` must be set *and* the chat must match.
* **Webhook subscribing is opt-in** (`WEBHOOK_TOKEN`), because it would otherwise let any
  MCP caller point a real Telegram account at an arbitrary URL.
* **The database URL is the trust boundary.** Whoever can start the MCP server can read
  the whole archive; there is no per-chat authorisation.
* **No secrets in tool output**: `connector_status` reports whether credentials are set,
  never their values; the webhook token is not returned by `webhook_list`.
