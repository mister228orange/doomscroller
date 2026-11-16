# CLI reference

`doomscroller` is both a console script (`doomscroller ...`) and a module
(`python -m doomscroller ...`). Every command needs `DATABASE_URL`; Telegram commands
additionally need `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` and a logged-in session.

Global options: `-v/--verbose` (debug logging) and `--version`.

```bash
doomscroller --help
doomscroller <command> --help
```

Exit codes: `0` success, `1` a handled failure (some poll/score runs failed), `2`
configuration or backend cannot work (missing key, unreachable database, refused send),
`130` interrupted with Ctrl-C.

## Subscriptions

| Command | What it does |
|---|---|
| `subscribe REF [REF...]` | add chats/channels; idempotent |
| `unsubscribe REF [REF...]` | remove a subscription (archived messages stay) |
| `subscriptions [--json]` | list subscriptions with cursor, count, last error |
| `enable REF [--disable]` | toggle polling/scoring for one subscription |

`REF` accepts `@name`, `name`, `t.me/name`, `https://t.me/name` or a numeric id. The
reference is normalised, so `@chan` and `t.me/chan` cannot become two rows.

## Ingest

```bash
doomscroller poll                  # one pass over every enabled subscription
doomscroller poll --chat NAME      # one chat only
doomscroller poll --loop           # poll every POLL_INTERVAL seconds (compose default)
doomscroller watch                 # stay connected; store new/edited/deleted live
```

`poll` is resumable and safe to re-run: it stores everything newer than the cursor and
advances the cursor in the same transaction. `POLL_INTERVAL` and `BATCH_SIZE` control the
loop; `SUBSCRIPTIONS` (comma-separated) is synced into the table on every `poll`/`watch`
start.

`watch` uses Telegram's live updates instead of the cursor, and needs its own session (the
poller must not run on the same session file at the same time).

## Reading the archive

```bash
doomscroller fetch CHAT --limit 50
doomscroller fetch CHAT --all > archive.txt        # entire history, TSV, streamed
doomscroller fetch CHAT --before 12345 --limit 20  # page backwards (older)
doomscroller fetch CHAT --after 12345              # page forwards (newer)
doomscroller fetch CHAT --oldest-first --json
doomscroller new CHAT                              # everything newer than the cursor
doomscroller new CHAT --since 1000                 # override the cursor
doomscroller search "grant" --chat CHAT --limit 20
doomscroller events CHAT 12345                     # raw new/edit/delete log for one message
doomscroller stats                                 # row counts per table
```

`fetch` and `new` never advance the ingest cursor. `--include-deleted` adds messages whose
Telegram original was deleted. `--all` streams oldest first in batches of `--batch`
(default 1000) and prints TSV, not the human table.

## Live Telegram

These talk to Telegram directly, not to the archive, and each opens its own session
(`--session-path` overrides the default). Do not point them at a session that a running
poller is using.

```bash
doomscroller chats [--limit 200] [--json]      # dialogs visible to the account
doomscroller send CHAT "text"                  # refused unless SEND_ALLOWLIST matches
doomscroller export [--chats a,b] [--limit 100] [--out FILE] [--sleep 0.25]
```

`send` is denied by default: `SEND_ALLOWLIST` must list the chat (or `*`) and
`SEND_RATE_PER_MIN` bounds the burst. A refusal prints `REFUSED: ...` and exits `2`.

`export` dumps the last `--limit` messages of each dialog (or of `--chats`) to JSON,
sleeping `--sleep` seconds between chats. Output defaults to
`~/Desktop/telegram_export.json`.

## Login

Two-phase login, resumable across processes:

```bash
doomscroller login                                  # requests the code
doomscroller login --code 12345 --hash <hash>       # signs in
```

With a TTY attached, the first command asks for the code in-process (the
`phone_code_hash` never has to leave memory) and asks for the 2FA password with
`getpass` when needed. If Telegram revoked the session (`AuthKeyDuplicatedError`, e.g. the
same session was used from two IPs), the stale file is renamed to
`<name>.revoked-<timestamp>.session` and a fresh login starts.

## MCP server

```bash
doomscroller mcp                                   # stdio (default)
doomscroller mcp --transport streamable-http --host 127.0.0.1 --port 8765
doomscroller mcp --transport sse
```

Tool reference: [mcp.md](mcp.md). Per-client configuration: README
[§ MCP](../README.md#mcp-use-the-archive-from-an-agent).

## Scoring

```bash
doomscroller score                          # one tick: up to SCORER_MAX_POSTS_PER_RUN posts
doomscroller score --chat NAME --limit 5
doomscroller score --dry-run                # print the exact prompts, call nothing, write nothing
doomscroller score --force                  # re-score even already scored posts (ignores sampling)
doomscroller score --loop [--interval 900]  # the scheduler (compose scorer service)
doomscroller score --no-summary             # do not refresh channel memory this run
doomscroller score --summary-every 5        # refresh memory every N posts
doomscroller score --genre трактат          # pin the genre for every post of this run
doomscroller score --sample-every 5         # read 1 of every N first-time posts
doomscroller score --check                  # rubric + database + endpoint health, no spending
doomscroller score --list-models deepseek   # list model ids offered by the endpoint
```

`--limit` bounds model calls, not posts examined. `--json` prints the run report. `score`
exits `1` if any post failed, `2` if the backend cannot be used at all.

`--check` is the first command to run on a new deployment: it verifies the rubric
directory, the database and the paid endpoint in that order and reports all three.

### Reading scores

```bash
doomscroller scores CHAT [--limit 30] [--all] [--json]   # per-post rubric rows
doomscroller channels [--json]                           # one line per channel trajectory
doomscroller channel-state CHAT [--events 10] [--json]   # full trajectory of one channel
doomscroller compare A B [--json]                        # side-by-side channel statistics
doomscroller chart CHAT [--out reports/x.html]           # self-contained HTML rating page
```

`scores --all` includes `skipped` and `failed` rows; without it only `scored` rows are
shown. `chart` prints ASCII to the terminal and writes HTML (default
`reports/<chat>.html`); it requires at least one scored post.

### Anchors and calibration

```bash
doomscroller anchors                     # locate rubric anchor texts in the archive
doomscroller anchors --anchor A3         # one anchor (repeatable)
doomscroller anchors --calibrate         # score anchors and diff against reference scores
doomscroller anchors --calibrate --json
```

`anchors` reports which reference texts are present and where. `--calibrate` sends them
through the configured model and shows per-axis deltas; a capture within ±0.5 counts as
"in tolerance", otherwise it is flagged as drift.

## OCR

```bash
doomscroller ocr CHAT [--limit 200] [--force] [--model ID] [--json]
```

Reads photo posts through a vision model and stores one `media_ocr` row per image. Two
language passes run in order (English, then Russian); the first that returns text wins, and
an image with no readable text is stored as `no_text` and not retried. Needs
`SCORER_API_KEY`.

## Database

```bash
doomscroller init-db     # create tables (idempotent)
```

Also called implicitly by most commands.

## Environment knobs that change CLI behaviour

| Variable | Effect |
|---|---|
| `DATABASE_URL` | required by every command |
| `TELEGRAM_*`, `TG_PROXY` | Telegram commands and login |
| `DATA_DIR`, `TELEGRAM_SESSION` | where the `.session` file lives |
| `SUBSCRIPTIONS` | synced into the table on `poll`/`watch` start |
| `POLL_INTERVAL`, `BATCH_SIZE` | poll loop pacing |
| `SEND_ALLOWLIST`, `SEND_RATE_PER_MIN` | `send` / MCP `send_message` |
| `SCORER_*` | scoring, OCR and calibration (see [scoring.md](scoring.md)) |
