# doomscroller

[![CI](https://github.com/mister228orange/doomscroller/actions/workflows/ci.yml/badge.svg)](https://github.com/mister228orange/doomscroller/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-server-purple.svg)](https://modelcontextprotocol.io/)

**Open source (MIT).** Telegram channel archiver + MCP server that gives agents
structured, queryable access to everything you follow — plus optional LLM scoring
of posts against a rubric you control.

## What it is

Subscribe to Telegram **channels or chats**, archive **every event** in PostgreSQL, and read
back **any amount** of stored history — or just what's new since last time.

```
subscribe  →  poll/watch  →  PostgreSQL  →  fetch | new | search
                                   ↓
                            score  →  post_scores + channel_states  →  scores | channels
                                   ↓
                              mcp  →  agents (tools: read, scores, webhooks)
```

The archiver stores what a channel said. The **scoring agent** (`score`) reads that archive
on a schedule and grades every post against a six-axis rubric, one post at a time, keeping a
per-channel trajectory — see [Scoring agent](#scoring-agent-score). The **MCP server**
publishes the archive, the scores and a webhook forwarder to agents.

> **Current state (2026-11-09):** tests are green (`247 passed`); the MCP surface gained
> `chat_history` (whole history, compressed into one array) and webhook forwarding
> (`webhook_subscribe` / `_unsubscribe` / `_list`); `connector_status` now always answers
> within ~5 s instead of hanging past a client's 30 s timeout. Open items and the plan are in
> [State and roadmap](#state-and-roadmap).

## Topology

Three containers and one process that deliberately is **not** a container:

| Role | Runs where | Holds | Never touches |
|---|---|---|---|
| `db` | compose | PostgreSQL | — |
| `doomscroller` (poller) | compose | **the Telegram session**, the ingest cursor | an LLM |
| `scorer` | compose | a model endpoint, the rubric | **Telegram, the session, `SEND_ALLOWLIST`** |
| `mcp` | **outside compose** (stdio from DSH / Claude Code / Cursor) | `DATABASE_URL`; Telegram only for live tools | the ingest cursor |

The point of the split is that each process holds only what its job needs:

* the poller is the **only** owner of the login — the scorer container does not even have
  the session file mounted, and gets no Telegram credentials in its environment;
* the scorer grades the archive and writes `post_scores` / `channel_states`; it talks to a
  model, never to Telegram;
* the MCP server is a **client-side** process. It reads the same database over the host
  port, so it works from a laptop, from another machine over HTTP, or from the same box —
  and it needs no model configuration at all, because the scores are already rows.

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
- **Scoring** of the archive by a separate agent (`score`), with a six-axis rubric and a
  per-channel trajectory — see [Scoring agent](#scoring-agent-score).
- **Live Telegram from an agent** (`chats`, `send`, and the MCP tools) — reads are free,
  **sends are denied by default** and need `SEND_ALLOWLIST`.
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
| `login [--code N] [--hash H]` | request the login code, then sign in (heals a revoked session) |
| `chats [--limit N]` | list Telegram chats visible to the account (live, needs the session) |
| `send CHAT TEXT` | send a message (refused unless the chat is in `SEND_ALLOWLIST`) |
| `export CHAT... [--limit N]` | dump recent messages of one or more chats to JSON |
| `score [--chat REF] [--limit N] [--loop] [--dry-run] [--force]` | grade unread posts (the agent) |
| `scores CHAT [--limit N] [--json] [--all]` | stored rubric rows for a chat |
| `channels [--json]` | per-channel trajectory: counts, means, summary |
| `channel-state CHAT [--json]` | the full trajectory of one channel |
| `ocr CHAT [--limit N] [--force] [--model M]` | read text off photo posts (English pass, then Russian; no text → ignored) |
| `chart CHAT [--out PATH]` | render the channel rating: self-contained HTML + ASCII sparkline |
| `compare CHAT... [--json]` | side-by-side statistics: volume, KPD spread, axes, spheres, best posts |
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

Or the resumable two-command login — it also heals a session that Telegram revoked
(`AuthKeyDuplicatedError`, the key was seen from two IPs; the dead file is moved aside
automatically):

```bash
python -m doomscroller login        # asks for the code from Telegram right there
```

One command: it sends the code, asks for it, and handles the two-factor password prompt.
Without a terminal (scripts) it falls back to printing `phone_code_hash` for a second call
with `--code` and `--hash`. The same flow works for the MCP session by pointing
`TELEGRAM_MCP_SESSION` at a fresh file.

The session file lives at `$DATA_DIR/$TELEGRAM_SESSION.session`. Compose mounts the repo at
`/app` with `DATA_DIR=/app`, so an existing `scroller_session.session` at the repo root is
reused as-is.

## MCP: use the archive from an agent

The archive, the scores, and (if you allow it) live Telegram are served over
[MCP](https://modelcontextprotocol.io). This normally runs **outside compose**: the agent
client starts the server over stdio on whatever machine the agent runs on.

```bash
python -m doomscroller mcp                                            # stdio: one process per client
python -m doomscroller mcp --transport streamable-http --port 8765    # HTTP: many clients, one server
```

Three families of tools, with different requirements — this is the only thing you have to
get right when wiring a new client:

| Tools | Needs | Notes |
|---|---|---|
| `read_messages`, `new_messages`, `search_messages`, `message_events`, `chat_history`, `list_picked_chats`, `archive_stats` | `DATABASE_URL` | reads `messages` / `message_events`, so any writer is queryable |
| `post_scores`, `channel_states`, `channel_events`, `connector_status` | `DATABASE_URL` | the scorer writes those rows; MCP only reads them, and reports the models + prompt versions actually recorded in the database |
| `list_chats`, `send_message` | `DATABASE_URL` + Telegram credentials + a session | client opened lazily, on first use only |
| `webhook_subscribe`, `webhook_unsubscribe`, `webhook_list` | `DATABASE_URL` + `WEBHOOK_TOKEN` (for subscribe/unsubscribe) | stores a per-chat URL; the forwarder is attached to the live session |

A client that only wants the archive and the scores needs **no Telegram configuration at
all**. `send_message` acts as the real user account, so it is denied by default: a chat must
be listed in `SEND_ALLOWLIST` (or `*`), and sends are rate-limited by `SEND_RATE_PER_MIN`.

### Reading a whole history at once: `chat_history`

`read_messages` pages, `chat_history` does not — it returns the **entire** stored history of
one chat as a single flat JSON **array**, oldest first, with only the main information per
message:

```json
[{"id": 3, "date": "2024-02-19T22:38:49+00:00", "from": "Slowpoke Investments",
  "t": "Путь слоупока к финансовому благополучию начинается с…", "more": true}]
```

- `id` — Telegram message id; `date` — ISO timestamp; `from` — sender name; `t` — gist.
- Dropped as noise: reply ids, edit timestamps, deleted flags, raw payloads, the event log
  (use `message_events` for those), URLs, control/zero-width characters and repeated
  whitespace.
- `t` is capped at `limit` characters (default 280, `0` = metadata only, max 4000);
  `"more": true` marks a message whose text was cut.
- `after_id` and `include_deleted` narrow the window, same as `read_messages`.

On a real 467-message channel this is ~486 KB via `read_messages` versus ~159 KB via
`chat_history` at 280 chars (and ~60 KB with `limit: 0`), so a full history fits in one
context window. When you only need a page, use `read_messages` instead.

### Forwarding arriving messages to a webhook

Three tools, one per chat:

| Tool | What it does |
|---|---|
| `webhook_subscribe(chat, url)` | every message arriving in `chat` is POSTed to `url`; subscribing again replaces the URL |
| `webhook_unsubscribe(chat)` | stops delivery (takes effect on the next message — the table is re-read, no restart) |
| `webhook_list()` | every subscription with delivery counters and the last error |

The body is the **raw Telethon update** — nothing is thrown away, because the receiver is
usually an agent that may want any field:

```json
{"chat_id": -1009990001111, "message_id": 42,
 "raw": {"id": 42, "message": "привет", "from_id": {"user_id": 7}}}
```

Delivery needs both halves:

- **`WEBHOOK_TOKEN` in `.env`** — sent as the `X-Webhook-Token` header. Without it
  `webhook_subscribe` is *refused*: pointing the account at an arbitrary URL is a standing
  SSRF, so it is off by default like sending is.
- **a live session in the MCP process** — the forwarder attaches to the MCP's own
  Telethon client when the first live tool is used. Deliveries therefore happen while that
  process runs; `python -m doomscroller mcp --transport streamable-http` is the mode for a
  long-lived forwarder.

A dead endpoint does not unsubscribe anything: the attempt is counted, `last_error` is
recorded, and `webhook_list` shows it.


Two environment traps, both learned the hard way:

1. **`.env` holds the compose hostname.** `DATABASE_URL` in `.env` is
   `...@db:5432/doomscroller`, which only resolves inside the compose network. An agent
   running on the host must receive `...@127.0.0.1:5434/doomscroller` explicitly — the
   server reads `.env` with `override=False`, so an explicit variable wins.
   **Unless the published port is broken on your machine** — see the next note.
2. **One live session per process.** `list_chats` and `send_message` open their own session
   file (`TELEGRAM_MCP_SESSION`, default `<session>_mcp`) so they never contend with the
   poller. If several MCP processes may use the live tools at once, give each one a distinct
   `TELEGRAM_MCP_SESSION` — two processes sharing one SQLite session file is a locking race.

### When the published port does not work: run the MCP inside compose

A published port can accept TCP and still deliver nothing: the host connects, sends its
startup packet, and no byte ever comes back, so every tool call dies on the client's
timeout. This is a docker-proxy/netfilter failure, not a doomscroller bug — check it with

```bash
docker compose exec -T db pg_isready -h 127.0.0.1 -p 5432   # healthy inside
nc -z -w 3 127.0.0.1 5434 && echo port-open                  # port-open from the host
```

If *every* published port behaves that way (not just this one), skip the host entirely and
run the MCP server inside the compose network. `db:5432` keeps working, and the client only
needs `/usr/bin/docker`:

```yaml
- id: mcp-doomscroller
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: doomscroller
    transport: stdio
    command: /usr/bin/docker
    args: ['compose', 'run', '--rm', '--no-deps', '-T',
           '-e', 'DATABASE_URL=postgresql+psycopg://doomscroller:doomscroller@db:5432/doomscroller',
           'doomscroller', 'python', '-m', 'doomscroller', 'mcp', '--transport', 'stdio']
    cwd: <repo>
    failOnStartupError: false
    toolCallTimeoutMs: 30000
```

This runs the same image as the poller, so the MCP cannot drift from the service version.
Two consequences: each tool call pays container startup (~5 s for the first one), and
`connector_status` reports `session_present: false` unless `TELEGRAM_SESSION`/`DATA_DIR` are
passed too — the repo is mounted at `/app`, so the live session is
`TELEGRAM_SESSION=scroller_session DATA_DIR=/app`.

Either way, the database connection is bounded by `DB_CONNECT_TIMEOUT` (default 5 s), so a
dead database surfaces as a report with a `hint`, not as a client-side timeout.


### Where each client keeps its config

| Client | Config location | Add command | Verify |
|---|---|---|---|
| DeepSeek Harness | `~/.dsh/profiles/<profile>/cordis.patch.yml` | edit the patch (see below) | tools named `mcp__doomscroller__*` |
| Claude Code | `claude mcp list`, scopes `local`/`project`/`user` | `claude mcp add` | `claude mcp list` |
| Codex CLI | `~/.codex/config.toml` | `codex mcp add` | `codex mcp list` |
| Cursor | `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` | edit the file | `cursor-agent mcp list`, `cursor-agent mcp list-tools doomscroller` |
| Gemini CLI | `~/.gemini/settings.json` | `gemini mcp add` | `gemini mcp list` |
| VS Code (Copilot agent mode) | `.vscode/mcp.json` (project) or user profile | edit the file | MCP servers panel / tool picker |
| Anything else | its own `mcpServers` map | — | call `connector_status` |

Substitute `<repo>` with the checkout path and `<db-url>` with a PostgreSQL URL the client
machine can actually reach (`postgresql+psycopg://doomscroller:doomscroller@127.0.0.1:5434/doomscroller`
for the compose database, published on port 5434).

`<repo>/.venv/bin/python` works from any directory because the package is installed editable
(`uv pip install -e .`). If you point a client at a plain system Python instead, either install
it there too or pass `PYTHONPATH=<repo>` — otherwise `python -m doomscroller` will not find the
package, and the client will report a server that starts and then immediately dies.

### DeepSeek Harness (DSH)

DSH mounts plugins from the profile's patch layer. Add one entry — the row id is what makes
it addressable, so keep it stable:

```yaml
- id: mcp-doomscroller
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: doomscroller
    transport: stdio
    command: <repo>/.venv/bin/python
    args: ['-m', 'doomscroller', 'mcp', '--transport', 'stdio']
    cwd: <repo>
    env:
      DATABASE_URL: <db-url>
      DATA_DIR: <repo>
    failOnStartupError: false
    toolCallTimeoutMs: 30000
```

The file sits at `~/.dsh/profiles/web/cordis.patch.yml` (a top-level YAML array). This is a
live profile: changes made through `plugin_manager` apply immediately, and after a hand edit
the `mcp__doomscroller__*` tools appear without a full restart. Confirm by asking the agent to
call `connector_status` — it reports the database, the session file and the scoring state.
`failOnStartupError: false` keeps a broken database from blocking the whole session.

Two notes specific to this harness:

- **Register it once.** A row inserted by a bundle and a row in the patch file that share the
  loader id do not coexist — the later layer shadows the earlier one, and removing the
  shadowing bundle removes the row rather than falling back. Keep a single registration.
- Prefer `plugin_manager` (`install_bundle` with a package whose `cordis.patch.yml` inserts
  this entry) over hand-editing the profile when you want it to survive across machines;
  hand-editing is fine for a personal profile, and the entry above is exactly what a bundle
  would insert.

### Claude Code

```bash
claude mcp add doomscroller \
  -e DATABASE_URL=<db-url> \
  -s user \
  -- <repo>/.venv/bin/python -m doomscroller mcp --transport stdio

claude mcp list        # should show doomscroller as connected
```

`-s user` makes it available in every project; `-s project` writes it into the repository's
`.mcp.json` so teammates get it with the repo. Add `-e DATA_DIR=<repo>` plus the Telegram
variables only if you want `list_chats` / `send_message`.

### Codex CLI

```bash
codex mcp add doomscroller \
  --env DATABASE_URL=<db-url> \
  -- <repo>/.venv/bin/python -m doomscroller mcp --transport stdio

codex mcp list
```

This writes a `[mcp_servers.doomscroller]` table into `~/.codex/config.toml`; editing that
table directly is equivalent.

### Cursor

`.cursor/mcp.json` in the repository (or `~/.cursor/mcp.json` for every project):

```json
{
  "mcpServers": {
    "doomscroller": {
      "command": "<repo>/.venv/bin/python",
      "args": ["-m", "doomscroller", "mcp", "--transport", "stdio"],
      "env": { "DATABASE_URL": "<db-url>" }
    }
  }
}
```

Verify from the terminal: `cursor-agent mcp list` (connection status) and
`cursor-agent mcp list-tools doomscroller` (the twelve tool names).

### Gemini CLI

```bash
gemini mcp add doomscroller <repo>/.venv/bin/python -m doomscroller mcp --transport stdio
gemini mcp list
```

Then put `DATABASE_URL` (and `DATA_DIR` for live tools) into the `env` block of the generated
entry in `~/.gemini/settings.json` — the CLI adds the command, not the environment.

### VS Code (Copilot agent mode)

`.vscode/mcp.json` in the repository:

```json
{
  "servers": {
    "doomscroller": {
      "type": "stdio",
      "command": "<repo>/.venv/bin/python",
      "args": ["-m", "doomscroller", "mcp", "--transport", "stdio"],
      "env": { "DATABASE_URL": "<db-url>" }
    }
  }
}
```

VS Code spells the map `servers` (not `mcpServers`) and wants an explicit `"type": "stdio"`.

### One server, many agents

Run the HTTP transport once and point every client at it — the useful setup when several
agents share one machine, or when one agent runs elsewhere:

```bash
python -m doomscroller mcp --transport streamable-http --host 127.0.0.1 --port 8765
# clients connect to http://127.0.0.1:8765/mcp
```

Then a client points at that URL instead of spawning a process — same tools, no per-client
install:

```bash
claude mcp add --transport http doomscroller http://127.0.0.1:8765/mcp
codex mcp add doomscroller --url http://127.0.0.1:8765/mcp
```

```yaml
# DSH: same profile patch, different transport
- id: mcp-doomscroller
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: doomscroller
    transport: streamable-http
    url: http://127.0.0.1:8765/mcp
```

The server-side environment (`DATABASE_URL`, Telegram session) then belongs to the one
`mcp` process you started, not to each client — which is also how you avoid several agents
racing over live Telegram sessions. Put it behind TLS if it leaves localhost — **the HTTP
transport has no authentication**, so treat an exposed port as an open read API for your
archive (and, with `SEND_ALLOWLIST` set, as a way to post as you). For stdio clients nothing
is exposed: each client gets its own process.

Ingest is deliberately not exposed over MCP: the cursor belongs to the poller, which is the
only process that holds the account's ingest session.

## Scoring agent (`score`)

A separate service that grades everything the archiver stores, using **DeepSeek via NanoGPT**
by default. It is driven by a scheduler: wake up, read what has not been scored yet, score it
**one post at a time**, update the channel's trajectory, go back to sleep.

```bash
# once: grade a handful of posts and show what happened
python -m doomscroller score --limit 5

# verify rubric + database + paid endpoint before spending anything
python -m doomscroller score --check
python -m doomscroller score --list-models deepseek

# inspect the exact prompt that would be sent, without calling a model
python -m doomscroller score --chat somechannel --dry-run

# the scheduler (this is the compose service)
python -m doomscroller score --loop --interval 900
```

It reads **PostgreSQL only** — never Telegram, never the session file. So it can run next to
the poller without contending for the login, and it grades whatever is in the archive
regardless of which ingest wrote it.

### The rubric is editable markdown, not code

`scoring/` holds the methodology as independent modules:

| File | Contents |
|---|---|
| `00_system.md` | role, input, boundaries |
| `01_axes.md` | the six axes with rubrics |
| `02_classification.md` | spheres (primary) and cross-cutting tags (secondary) |
| `03_rules.md` | rules 1–15 |
| `04_formulas.md` | KPD / Eff and their interpretation |
| `05_anchors.md` | global anchors A1–A10 |
| `06_procedure.md` | procedure + answer format (sent in the user turn) |
| `07_selfcheck.md` | self-check + glossary |
| `08_channel_context.md` | how the channel trajectory is used |
| `09_summary.md` | the separate "update the channel memory" call |

Edit one file without touching the others or the code. Each score stores a
`prompt_version` (short hash of every module), so a row can be traced back to the rubric
that produced it — and re-scored later with `--force`. Modules are re-read on every run;
there is no cache to invalidate. Point `SCORER_RUBRIC_DIR` elsewhere to keep a second
rubric side by side.

### Model configuration: DeepSeek via NanoGPT

The default backend is [NanoGPT](https://nano-gpt.com)'s OpenAI-compatible endpoint
(`https://nano-gpt.com/api/v1`) with a pinned DeepSeek model. Both defaults live in the code
([`config.py`](doomscroller/config.py)), so this is all a `.env` needs:

```dotenv
SCORER_API_KEY=sk-...          # NANOGPT_API_KEY is accepted as the same thing
```

| Model id | $/1M in → out | Context | Use it when |
|---|---|---|---|
| `deepseek-chat` (default) | 0.10 → 0.43 | 128k | the original DeepSeek V3: cheap, structured output, steady |
| `deepseek/deepseek-v3.2` | 0.21 → 0.31 | 163k | a newer successor; a bit more per token, more headroom |
| `deepseek/deepseek-v4.1-flash` | 0.09 → 0.25 | 1M | big backfills, reasoning on tap |
| `deepseek/deepseek-v4-pro` | 0.70 → 1.40 | 1M | auditing the rubric itself |
| `deepseek/deepseek-v3.2:thinking` | 0.21 → 0.31 | 163k | reasoning trace; slower, JSON can suffer |

The default is the original DeepSeek model rather than a derivative: `deepseek-chat` is
what NanoGPT lists as "DeepSeek original V3 model" — no abliterated fork, no TEE re-host.

`doomscroller score --list-models` asks the endpoint what it actually offers (NanoGPT carries
several hundred models); `doomscroller score --list-models v4` narrows it down.

**Check before spending.** `--check` verifies the three independent things that can be wrong
— rubric directory, database, paid endpoint — and scores nothing:

```console
$ python -m doomscroller score --check
scorer
  base_url  : https://nano-gpt.com/api/v1
  model     : deepseek/deepseek-v3.2
  api_key   : set
  rubric    : version a463b3fa2d97, 10 modules, 10 anchors
  database  : ok — 1 channel(s), 3 scored, 0 pending
  backend   : ok — answered as deepseek-v3.2
  latency   : 812 ms, tokens 12 -> 1
```

`--check` exits `2` when something is missing or misconfigured (including "no key"), so it
is usable as a pre-flight step in a script. `--list-models` needs no key at all — NanoGPT
serves its model list publicly, and that is exactly when you need it.

Paying per token is why the failure modes are named: a bad key is reported as
`authentication failed` and is **not** retried, an empty balance as `payment required`,
an unknown model points back at `--list-models`, and only 5xx/429 are retried. A wrong model
id therefore costs one request, not three.

Any OpenAI-compatible endpoint still works — `SCORER_BASE_URL` is the only switch:

```dotenv
# DeepSeek directly
SCORER_BASE_URL=https://api.deepseek.com/v1
SCORER_MODEL=deepseek-chat
# a local Ollama (compose: http://host.docker.internal:11434/v1)
SCORER_BASE_URL=http://127.0.0.1:11434/v1
SCORER_MODEL=qwen2.5:32b
```

| Variable | Default | Meaning |
|---|---|---|
| `SCORER_BASE_URL` | `https://nano-gpt.com/api/v1` | OpenAI-compatible base URL (`.../v1`) |
| `SCORER_API_KEY` (or `NANOGPT_API_KEY`) | — | bearer token |
| `SCORER_MODEL` | `deepseek/deepseek-v3.2` | model id |
| `SCORER_TEMPERATURE` | `0` | pinned for reproducibility |
| `SCORER_TIMEOUT` / `SCORER_RETRIES` | `600` / `2` | per-call timeout and transport retries |
| `SCORER_MAX_POSTS_PER_RUN` | `20` | model calls per scheduler tick |
| `SCORER_INTERVAL` | `900` | seconds between ticks with `--loop` |
| `SCORER_MIN_CHARS` | `120` | shorter posts are archived but recorded as `skipped` |
| `SCORER_MAX_CHARS` | `12000` | one huge post cannot bury the rubric |
| `SCORER_MAX_ATTEMPTS` | `3` | retries for a post that failed to score |
| `SCORER_SUMMARY_EVERY` | `5` | rebuild the channel memory every N posts (`0` = never) |
| `SCORER_WINDOW` | `5` | EWMA window of the channel trend |
| `SCORER_JSON_MODE` | `auto` | ask the backend for JSON; falls back if it rejects it |
| `SCORER_REASONING_EFFORT` | *(model decides)* | pin the thinking budget (`none` stops a reasoning model burning 4–7k output tokens per post) |
| `SCORER_SAMPLE_EVERY` | `1` | read every Nth post (1-из-5 = `5`); first-time reads only, `--force` and rescores ignore it |

Inside compose, `python -m doomscroller score --loop` is the `scorer` service — a separate
container from the poller, with a separate environment:

```bash
docker compose up -d scorer
```

The `scorer` service mounts **only** `./scoring` (read-only) and receives **only**
`DATABASE_URL` + `SCORER_*`. Telegram credentials, the session file and `SEND_ALLOWLIST`
are not passed to it at all, because grading an archive never requires acting as the
account. Rule files stay editable on the host and are picked up on the next tick; code
changes need `docker compose up -d --build scorer`.

### What "unread" means

There is no second cursor to drift. A post is unread when `post_scores` has no usable row
for `(chat_id, message_id)`:

* no row → score it;
* `skipped` (too short / no text) → done, never a rubric candidate;
* `failed` → retried until `attempts` reaches `SCORER_MAX_ATTEMPTS`;
* `scored` → done, unless the message was **edited after** it was scored, or `--force`.

Posts are taken oldest first, so a fresh channel is backfilled from its very first stored
message. A run costs at most one attempt per post per tick, and short posts do not consume
the per-tick model budget.

### Photo posts go through OCR first

A channel post is often a screenshot: the caption is a stub and the content is in the
image. Before such a post is scored, its text is read off the picture:

```bash
python -m doomscroller ocr design4TOR            # English pass, then Russian
python -m doomscroller ocr design4TOR --model google/gemma-4-26b-a4b-it   # another vision model
```

The rule is literal: pass one asks for the transcription assuming English, pass two runs
only if the first read nothing and assumes Russian; an image that yields no text in either
pass is recorded as `no_text` and never looked at again. Only real pictures are processed —
webpage previews and video thumbnails are link decorations, not content. The engine is a
vision model over the same endpoint as the scorer (`deepseek/deepseek-v4.1-flash` by
default, ~$0.0002 per image); images are downscaled to 1600 px first, because a huge
screenshot otherwise dies with HTTP 413.

OCR text is stored in `media_ocr`, joined by `(chat_id, message_id)` — the archive itself
stays untouched. For scoring, a post's body becomes *caption + `[текст с изображения]` +
transcription*, and OCR that arrived after a post was scored puts the post back into the
queue, exactly like an upstream edit.

### One transaction per post

Same invariant as the ingest cursor, for the same reason:

> **The score row and the updated channel state are committed together.**

If the process dies mid-run, a post is either scored and folded into the trajectory, or
neither — never counted twice, never skipped silently. A backend outage aborts the tick
cleanly, records the failed attempt, and the next tick retries.

### The channel trajectory

A post in isolation and a post in a trajectory are different texts, so every score is also
given a channel-relative view:

* **Trend** — an EWMA (window 5) per axis, recomputed from `post_scores`.
* **Deviation** — `|axis − mean| > 1.0` is recorded per post; the trend used is the one
  *before* that post.
* **Local anchors** — the last 5 posts of the same channel, passed into the next prompt.
* **Shift** — 3 consecutive posts that each move ≥ 1.5 on 2+ axes appends a `shift` event.
* **Peak** — a new channel-best KPD (≥ 7.5) appends a `peak` event.
* **Memory** — every `SCORER_SUMMARY_EVERY` posts one extra call refreshes the channel
  profile and a 3–6 point trajectory summary. That text is what the next scoring call sees
  as `PROFILE` / `SUMMARY`.

Deviation never changes the *scores* — it is a flag, not a correction. A post may be far
worse than its channel's average; that is a fact about the post.

The methodology describes this state as `state/{{channel}}.json` + `.jsonl`. Here it lives
in `channel_states` (the derived projection) and `post_scores` (the full history), so the
scorer and every reader see one copy, and the state can be re-derived at any time — which is
what makes re-scoring an edited post safe.

### Reading the results back

```bash
python -m doomscroller scores somechannel            # table of rubric rows
python -m doomscroller channels                      # one line per channel
python -m doomscroller channel-state somechannel     # trend, anchors, events
python -m doomscroller scores somechannel --json     # everything, machine-readable
```

Over MCP: `post_scores`, `channel_states`, `channel_events`, plus a `scoring` block in
`connector_status` with counts, the models actually used and the prompt versions — all of it
read from the database, so an MCP client needs no scorer configuration to see the results.

### The rating page

```bash
python -m doomscroller chart design4TOR
# html: reports/design4TOR.html
```

Writes a single self-contained HTML file — inline SVG, no JavaScript, no external assets —
with the KPD of every scored post in channel order (bars colored by sphere, the EWMA trend
line, the mean), the six axis means, sphere and tag distributions, the best and worst posts
with `t.me` links, and the trajectory events. The same command prints an ASCII sparkline
for the terminal.

### Calibrating against the anchors

`scoring/05_anchors.md` is prose with numbers. A `LOCATOR: channel#message_id` line turns an
anchor into something checkable — the real post in the archive:

```
## A3 «Пуговки» (короткий пост)
SCI + [TRUST, DEGR]
LOCATOR: design4TOR#99
I 8.6 | V 8.8 | U 8.7 | P 4.5 | S 4.5 | D 9.0 → 8.78 / 1.95
```

```console
$ doomscroller anchors
rubric c163f83fad1d: 10 anchors, 5 с текстом в архиве
ID   genre              primary   КПД  chars  locator
A1   афоризм            POWER   9.75   1957  design4TOR#44
A2   трактат            SCI     9.50   3489  design4TOR#72
A3   короткий пост      SCI     8.78    391  design4TOR#99
A4   пост + образ       TECH    9.33   3616  design4TOR#98
A7   короткая модель    ECON    8.05    321  design4TOR#100

$ doomscroller anchors --calibrate
```

`--calibrate` re-scores each archived anchor with the configured model — one call each, no
`CHANNEL_STATE` (an anchor is a global reference, not a channel's post) — and diffs every
axis against the reference. It reports per-axis drift, the mean signed/absolute shift across
anchors, and which anchors fall outside ±0.5. It never writes to `post_scores`: this is a
diagnostic, not a scoring run.

If a clamp of the rubric itself is what disagrees (a `трактат` with `I 9.5` against rule 12's
ceiling of 9.0), the anchor row says so — that is the point of doing it.

### Cost

One model call per scored post, plus one per `SCORER_SUMMARY_EVERY` posts for the memory.
Skipped posts and already-scored posts cost nothing. `--limit` bounds a tick; `--dry-run`
shows the exact prompt before you spend anything, `--check` proves the endpoint answers.

Each run reports what it actually spent, in tokens, per channel and in total:

```console
demo_channel: scored 20, skipped 0, failed 0, memory 4
total: scored 20, skipped 0, failed 0
tokens: 173 402 in / 21 380 out
```

The rubric is a large prompt (~2.5k tokens of rules and anchors) and a DeepSeek answer is
~1k, so a post costs on the order of 4k tokens — the reason `SCORER_MAX_POSTS_PER_RUN`
exists rather than an unbounded backfill.

## Live chats, sending and export

Three commands talk to Telegram directly (they need the session, not the archive):

```bash
python -m doomscroller chats --limit 50          # live chat list (id, kind, @username, title)
python -m doomscroller send "me" "hello"         # send text — needs SEND_ALLOWLIST
python -m doomscroller export --limit 100 \
       --out ~/Desktop/telegram_all_chats_100.json   # last N msgs of EVERY chat -> JSON
```

`export` writes one JSON document: `{exported_at, limit_per_chat, chats:[{chat_id, title,
kind, username, message_count, messages:[{message_id, date, sender_id, sender_name, text,
reply_to_message_id, has_media, views, forwards}]}]}`. Use `--chats ref1,ref2` to export
specific chats, `--max-chats` to cap, `--sleep` to be gentle.

### Sending is denied by default

Sending uses a **real user account**, which Telegram rate-limits and can ban. So:

- `SEND_ALLOWLIST` is empty by default → every send is refused;
- list specific chats (`me,safe_chat`) or `*` to allow all;
- refusals are returned as `{"sent": false, "refused": true, "reason": "..."}`, not exceptions,
  so an agent can read why;
- a per-minute rate limit (`SEND_RATE_PER_MIN`, default 20) applies.

The MCP surface gains `list_chats` and `send_message` alongside the read-only archive tools.

## Schema

| Table | Contents |
|---|---|
| `subscriptions` | chat ref, resolved id, title, kind, enabled, **cursor**, last poll/error, message count |
| `messages` | current state, one row per `(chat_id, message_id)`; text survives deletion |
| `message_events` | **append-only** log of every `new` / `edited` / `deleted` event |
| `detected_events` | optional model-detected events (`detector` extra) |
| `post_scores` | one rubric row per post (`scored` / `skipped` / `failed`) with the six axes, KPD/Eff, classification, deviations and the prompt version |
| `channel_states` | derived trajectory per channel: moving means, tag distribution, last 5 local anchors, summary, counters |
| `channel_events` | **append-only** trajectory log: `shift` / `peak` / `summary` |
| `media_ocr` | OCR text of image posts, joined by `(chat_id, message_id)` |
| `chat_webhooks` | one row per chat: where to POST arriving messages, plus delivery counters and the last error |

## The cursor guarantee

The single most important property here:

> **The cursor is committed in the same transaction as the messages it covers.**

If storing a batch fails for any reason, the cursor does not move, and the next run re-reads
that batch. A crash causes **re-processing, never silent loss**. Ingest is idempotent, so
re-processing is harmless.

This is a deliberate fix. The earlier implementation advanced `last_msg_id` *before* calling
the model, so a timeout silently dropped those messages forever. There is a test pinning the
behaviour: `tests/test_ingest_cursor.py::test_cursor_does_not_advance_when_storage_fails`.

## State and roadmap

### Where this stands right now (2026-11-11)

Checked against the live database over MCP (`connector_status`, `channel_states`,
`list_picked_chats`); the test suite still reports `247 passed`.

**Everything is built; nothing is currently running.** Only the `db` container is up —
`doomscroller` (poller) and `scorer` are stopped, so no message has been ingested since
**2026-11-08 02:38 UTC** and nothing has been scored since **2026-11-08 17:21 UTC**.
`connector_status` flags all five subscriptions as stale with `last_error: null`: nothing is
failing, the pipeline is simply not running. The numbers below are a snapshot of 2026-11-08,
not the channels as they are today.

Archive and scoring coverage at that point:

| Channel | Messages | Scored | Skipped | Never scored | Mean KPD |
|---|---:|---:|---:|---:|---:|
| `slowpoke_investments` | 467 | 88 | 13 | **366** | 4.06 |
| `bukvy_znaesh` | 157 | 28 | 3 | **126** | 6.37 |
| `design4TOR` | 94 | 72 | 13 | 9 | 6.46 |
| `demo_channel` | 17 | 3 | 14 | 0 | 7.82 |
| **Total** | **735** | **191** | **43** | **501** | |

Two caveats on reading those means: the rows were produced by two models and two prompt
versions (`deepseek/deepseek-v4-pro` + `c163f83fad1d`, and older `qwen3:4b` +
`a463b3fa2d97`), so a channel's average mixes both; and `slowpoke_investments` is 78 % ungraded,
so its mean describes the graded tail, not the channel.

Working and covered by tests:

| Area | State |
|---|---|
| Ingest, cursor, edits/deletes, search | stable; the cursor guarantee below is pinned by a test |
| Scoring agent, rubric, calibration, OCR | stable; `SCORER_*` knobs documented above |
| MCP read surface | `read_messages` / `new_messages` / `search_messages` / `message_events` / `chat_history` / `archive_stats` |
| MCP scoring surface | `post_scores` / `channel_states` / `channel_events` |
| MCP live Telegram | `list_chats` / `send_message` (sending denied by default) |
| Webhook forwarding | `webhook_subscribe` / `webhook_unsubscribe` / `webhook_list`; raw Telethon update, `X-Webhook-Token` |
| `connector_status` | bounded by `DB_CONNECT_TIMEOUT` (5 s); returns a report with a `hint` when the database is down |

Known limits, not bugs to be surprised by:

- **A broken published port is not a doomscroller problem.** On the current host every
  docker-published port accepts TCP but delivers no data, so clients must run the MCP inside
  the compose network — see [the port note](#when-the-published-port-does-not-work-run-the-mcp-inside-compose).
  This needs a reboot of the host to clear; the workaround is documented and in use.
- **The webhook forwarder lives as long as its MCP process.** Under stdio that is one
  client session; a permanent forwarder needs `--transport streamable-http`.
- **Real §-per-post cost.** NanoGPT holds a per-request deposit sized from
  `SCORER_MAX_TOKENS`; keep `SCORER_MAX_TOKENS=2000` on a small balance (see `AGENTS.md`).

### Next

Deferred by the owner on 2026-11-11 as non-urgent — do not start these on a fresh session
without being asked:

1. **Restart the pipeline** (`docker compose up -d doomscroller scorer`). Nothing else below
   can be judged until the 501-post backlog is working through; at `SCORER_SAMPLE_EVERY=5`
   decide first whether the tail is sampled or caught up in full.
2. **Homogenise the scores.** Two models and two prompt versions are mixed in `post_scores`;
   a re-scoring pass (or a `--since` reset) would make channel means comparable.
3. **A long-lived forwarder service.** Run the MCP as a plain compose service
   (`--transport streamable-http`), so webhook delivery and live tools do not depend on an
   editor's process being open.
4. **`chat_history` on the CLI.** The compressed view exists only over MCP today; `fetch --all`
   still prints one line per message.
5. **Delivery retries.** A failed webhook POST is counted and reported, never retried. A
   small backoff queue (attempt N after N seconds, capped) would make a briefly-down receiver
   survivable.
6. **Webhook payload selection.** Always sending the raw update is a deliberate default;
   a `mode=normalized` option would keep receivers that only want `chat_id/text` cheap.
7. **Score freshness in `connector_status`.** It reports what is scored, not what is *stale*;
   a per-channel "last scored N minutes ago" line would make coverage visible at a glance.

## Proxy

Set `TG_PROXY` to an MTProto link (`https://t.me/proxy?server=...&port=443&secret=...`) or a
SOCKS5 URL (`socks5://user:pass@host:port`). Leave it empty or set `off` to force a direct
connection.

## Secrets

`.env` and `*.session` are gitignored. **A Telethon session file is full access to the
Telegram account** — treat it like a private key, and never commit it. If a secret ever
reaches git history, rotating it is the only real fix; scrubbing the tip is not enough.

There are now two such files, and that is on purpose:

| File | Owner | Why |
|---|---|---|
| `scroller_session.session` | the poller container | ingest |
| `scroller_session_mcp.session` | the MCP process, wherever it runs | live tools, without contending with the poller for one SQLite file |

Both are the same kind of key and both need the same care. What does **not** hold the key:
the `scorer` container. Compose gives it only `DATABASE_URL` + `SCORER_*` and mounts only
`./scoring` — no `.env`, no session file. A scoring container that carries the login could
act as the account for no reason at all.

One more asymmetry worth knowing: the MCP server passes to the client only what it needs,
and if it runs from this checkout it also reads the repo's `.env` (variables passed in the
client config win — dotenv never overrides an existing value). Move the server to another
machine and you must supply everything explicitly.

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

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues: [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE).
