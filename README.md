# doomscroller

Subscribe to Telegram **channels or chats**, archive **every event** in PostgreSQL, and read
back **any amount** of stored history — or just what's new since last time.

```
subscribe  →  poll/watch  →  PostgreSQL  →  fetch | new | search
```

## What it does

- **Subscriptions** to channels, groups, or users (a `@username`, a `t.me/...` link, or a
  numeric id). The list lives in the database, not in a config file.
- **Full archiving.** Every message is stored, and an **append-only event log** records every
  `new` / `edited` / `deleted` event. Edits update the current row; deletes mark it while
  keeping the text you already archived. Nothing is silently overwritten.
- **Two ingest modes.** `poll` catches up on everything missed (resumable, safe to re-run);
  `watch` stays connected and stores events as they arrive.
- **Unbounded retrieval.** `fetch --limit N`, or `fetch --all` to stream the entire archive.
  `new` returns only what arrived since the stored cursor.
- **MTProto / SOCKS5 proxy support** (`TG_PROXY`), for networks where Telegram is blocked.
- **PostgreSQL in Compose**, with Adminer for inspecting the data.

## Quick start (Compose)

```bash
cp .env.example .env
# fill in TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE

docker compose up -d db
docker compose run --rm doomscroller python -m doomscroller init-db

# subscribe and backfill
docker compose run --rm doomscroller python -m doomscroller subscribe @somechannel
docker compose run --rm doomscroller python -m doomscroller poll

# read back
docker compose run --rm doomscroller python -m doomscroller fetch @somechannel --limit 20
docker compose run --rm doomscroller python -m doomscroller new @somechannel

# start the continuous poller
docker compose up -d
```

Adminer: <http://127.0.0.1:8082> (system `PostgreSQL`, server `db`, user/password from `.env`).

Postgres is published on **5434**, not 5432, so it cannot collide with another stack.

## Quick start (local, no Docker)

```bash
uv venv && uv pip install -e '.[dev]'
cp .env.example .env      # set DATABASE_URL to a reachable postgres

python -m doomscroller init-db
python -m doomscroller subscribe @somechannel
python -m doomscroller poll
python -m doomscroller fetch @somechannel --limit 50
```

## Commands

| Command | Purpose |
|---|---|
| `subscribe REF...` | add chats/channels |
| `unsubscribe REF...` | remove one |
| `subscriptions [--json]` | list with cursor, message count, last error |
| `enable REF [--disable]` | pause/resume without losing the cursor |
| `poll [--chat REF] [--loop]` | store everything new (idempotent) |
| `watch` | stay connected, store events live |
| `fetch CHAT [--limit N] [--before ID] [--after ID] [--all] [--json]` | read stored messages |
| `new CHAT [--since ID] [--json]` | everything newer than the cursor |
| `search QUERY [--chat REF] [--limit N]` | substring search across stored messages |
| `events CHAT MESSAGE_ID` | raw event log for one message |
| `stats` | row counts per table |
| `init-db` | create tables |

`fetch --all` streams TSV (id, date, sender, text) so it is safe to pipe into a file even
for very large archives.

## First login

Telethon needs an interactive login once per session file. Either set `TELEGRAM_PHONE` and
let it prompt, or run the login step interactively:

```bash
docker compose run --rm -it doomscroller python -c \
  "import asyncio; from doomscroller.config import load_settings; \
   from doomscroller.telegram_client import build_client; \
   s=load_settings(); c=build_client(s); asyncio.run(c.start(phone=s.phone))"
```

The session file lives at `$DATA_DIR/$TELEGRAM_SESSION.session`. Compose mounts the repo at
`/app` with `DATA_DIR=/app`, so an existing `scroller_session.session` at the repo root is
reused as-is.

## MCP: use the archive from an AI agent

doomscroller serves its archive over [MCP](https://modelcontextprotocol.io). **Read-only** —
there is no send tool, so an agent cannot post as you.

```bash
python -m doomscroller mcp                                              # stdio (local clients)
python -m doomscroller mcp --transport streamable-http --port 8765      # HTTP (remote clients)
```

Tools: `list_picked_chats`, `connector_status`, `read_messages`, `new_messages`,
`search_messages`, `message_events`, `archive_stats`.

Because these tools read **PostgreSQL** and not Telegram, they are already
gateway-agnostic: whatever writes into `messages` is queryable here.

### DeepSeek Harness (DSH)

Add to `~/.dsh/profiles/web/cordis.patch.yml` (a top-level YAML array):

```yaml
- id: mcp-doomscroller
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: doomscroller
    transport: stdio
    command: /home/onemanarmy/Documents/doomscroller/.venv/bin/python
    args: ['-m', 'doomscroller', 'mcp', '--transport', 'stdio']
    cwd: /home/onemanarmy/Documents/doomscroller
    env:
      DATABASE_URL: postgresql+psycopg://doomscroller:doomscroller@127.0.0.1:5434/doomscroller
      DATA_DIR: /home/onemanarmy/Documents/doomscroller
```

The tools appear as `mcp__doomscroller__read_messages`, `mcp__doomscroller__search_messages`,
and so on. The profile has `patchReload: live`, so saving the file hot-reloads — no restart.
`failOnStartupError: false` keeps a broken database from blocking the whole session.

### Claude Code, Cursor, Codex, or any other MCP client

Same stdio command, their own config file:

```json
{
  "mcpServers": {
    "doomscroller": {
      "command": "/home/onemanarmy/Documents/doomscroller/.venv/bin/python",
      "args": ["-m", "doomscroller", "mcp", "--transport", "stdio"],
      "env": { "DATABASE_URL": "postgresql+psycopg://...", "PYTHONPATH": "/home/onemanarmy/Documents/doomscroller" }
    }
  }
}
```

### Another machine, or several agents at once

Run the HTTP transport once and point every client at it:

```bash
python -m doomscroller mcp --transport streamable-http --host 0.0.0.0 --port 8765
# clients connect to http://<host>:8765/mcp
```

Put it behind TLS if it leaves localhost — the HTTP transport has no built-in
authentication, so treat an exposed port as an open read API for your archive.

## Schema

| Table | Contents |
|---|---|
| `subscriptions` | chat ref, resolved id, title, kind, enabled, **cursor**, last poll/error, message count |
| `messages` | current state, one row per `(chat_id, message_id)`; text survives deletion |
| `message_events` | **append-only** log of every `new` / `edited` / `deleted` event |
| `detected_events` | optional model-detected events (`detector` extra) |

## The cursor guarantee

The single most important property here:

> **The cursor is committed in the same transaction as the messages it covers.**

If storing a batch fails for any reason, the cursor does not move, and the next run re-reads
that batch. A crash causes **re-processing, never silent loss**. Ingest is idempotent, so
re-processing is harmless.

This is a deliberate fix. The earlier implementation advanced `last_msg_id` *before* calling
the model, so a timeout silently dropped those messages forever. There is a test pinning the
behaviour: `tests/test_ingest_cursor.py::test_cursor_does_not_advance_when_storage_fails`.

## Proxy

Set `TG_PROXY` to an MTProto link (`https://t.me/proxy?server=...&port=443&secret=...`) or a
SOCKS5 URL (`socks5://user:pass@host:port`). Leave it empty or set `off` to force a direct
connection.

## Secrets

`.env` and `*.session` are gitignored. **A Telethon session file is full access to the
Telegram account** — treat it like a private key, and never commit it. If a secret ever
reaches git history, rotating it is the only real fix; scrubbing the tip is not enough.

## Legacy: the Ollama event detector

The original feature — watching chats and using a local Ollama model to detect events —
still works and is untouched. It predates the database layer and keeps its own state files:

```bash
uv pip install -e .
python main.py           # old entrypoint; reads MONITORED_CHATS / TARGET_CHAT / OLLAMA_*
```

It writes per-chat state to `doom_states/*.json`. The new archiver supersedes it for
*storage and retrieval*; the detector remains for *LLM event extraction*, and the
`detected_events` table exists to receive those results if you wire the two together.

## License

MIT
