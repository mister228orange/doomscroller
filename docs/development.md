# Development

## Requirements

* Python **3.12+** (`pyproject.toml` pins `requires-python = ">=3.12"`).
* PostgreSQL 16/18 for production-like runs; the test suite falls back to SQLite.
* Docker + Compose for the container topology (optional).
* A Telegram API id/hash from <https://my.telegram.org/apps> for live ingest.

## Layout

```
doomscroller/
├── doomscroller/          # package: CLI, storage, ingest, MCP, scoring, OCR
│   └── scoring/           # the scoring agent (prompts, rubric, state, LLM client)
├── scoring/               # rubric modules 00..09 (markdown, shipped as data)
├── tests/                 # pytest suite (SQLite or PostgreSQL)
├── docker-compose.yml     # db + poller + scorer + adminer
├── Dockerfile
└── .env.example           # every knob, documented
```

## Setup

```bash
git clone https://github.com/mister228orange/doomscroller.git
cd doomscroller

# with uv
uv sync

# or with plain venv + pip
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"

cp .env.example .env      # fill TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_PHONE
```

If `python3 -m venv` fails with "ensurepip is not available", install the distro package
(`python3-venv`) or use `uv venv`. The environment does not need to be inside the repo —
`data_dir` is controlled by `DATA_DIR`.

Run the CLI without installing:

```bash
python -m doomscroller --help
```

## First run

```bash
docker compose up -d db                     # start PostgreSQL (127.0.0.1:5434)
python -m doomscroller init-db              # create tables
python -m doomscroller login                # Telethon login (code, then 2FA)
python -m doomscroller subscribe @somechannel
python -m doomscroller poll                 # resolve id + backfill
python -m doomscroller fetch somechannel --limit 10
```

The local `DATABASE_URL` must point at `127.0.0.1:5434`; inside compose the poller gets
`db:5432` from `docker-compose.yml`. `.env` is loaded from the current directory or the
repo root, with exported environment variables taking precedence (`override=False`).

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

`tests/conftest.py` resolves the database in this order:

1. `TEST_DATABASE_URL`
2. `DATABASE_URL`
3. a temporary SQLite file (`$TMPDIR/doomscroller-test.sqlite`)

With a database configured, the suite runs against PostgreSQL — the production target.
Without it, SQLite must be enough: that is why queries avoid PostgreSQL-only SQL. Tables
are dropped and recreated per session, and truncated between tests, so never point
`TEST_DATABASE_URL` at a database you care about.

Tests also pin `SCORER_SAMPLE_EVERY=1` and `SCORER_REASONING_EFFORT=none` so a developer's
`.env` cannot make the suite non-deterministic.

CI (`.github/workflows/ci.yml`) installs the package with `pip install -e . pytest` on
Python 3.12 and runs `python -m pytest -q`.

## Docker

```bash
docker compose up -d db
docker compose run --rm doomscroller init-db
docker compose up -d                    # poller (poll --loop)
docker compose up -d scorer             # scoring loop (needs SCORER_* in .env)
docker compose logs -f scorer
```

The poller container mounts the repo at `/app` and keeps the Telethon session there. The
scorer container gets no `env_file`: only `SCORER_*` are passed in, plus a read-only mount
of `./scoring` at `/app/scoring`, so rule edits take effect on the next tick without a
rebuild.

Adminer (a database UI) is on port `${ADMINER_PORT:-8082}`.

## Packaging

The wheel is built with hatchling; `force-include` ships `scoring/` as
`doomscroller/rubric`, so a plain `pip install doomscroller` can score without a checkout.
`scoring/prompts.py::rubric_dir_candidates()` looks for the rubric in this order:

1. `SCORER_RUBRIC_DIR` (a contract: if set and wrong, loading fails loudly),
2. `<repo>/scoring`,
3. `doomscroller/rubric` (inside the wheel),
4. `./scoring` relative to the working directory.

## Schema changes

There is no migration framework: `init_db()` is `SQLModel.metadata.create_all()`, which
creates missing tables but never alters existing ones. When you add or change a column:

1. update the model in `doomscroller/models.py`,
2. write the `ALTER TABLE` for PostgreSQL yourself (or recreate the database and re-poll —
   the archive is reproducible from Telegram, scores are not cheap to reproduce),
3. add a test that exercises the new column through the store/agent path.

For a throwaway dev database, `docker compose down -v` then `up -d db` and `init-db` is
the fastest reset.

## Adding to the rubric

The rubric is nine markdown files (`scoring/00_system.md` … `09_summary.md`), all of which
must exist — `prompts.load_modules` raises `RubricError` otherwise. `00`–`08` build the
system prompt and task turn; `06_procedure.md` is the task block placed in the user turn;
`09_summary.md` is the separate channel-memory call. The version is a SHA-256 of the
concatenated modules, stored as `prompt_version` on every score row.

After editing, run `doomscroller score --dry-run` to inspect the assembled prompt without
spending anything, then `doomscroller anchors --calibrate` to see whether the model still
reproduces the reference scores.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| MCP tool hangs, client reports "Request timed out" | Database accepted TCP but never completed the handshake; the connect timeout is `DB_CONNECT_TIMEOUT` (5 s). Check the port mapping. |
| `psycopg.OperationalError: connection refused` | Nothing on that host:port — start `db`, and remember inside compose the host is `db`, not `127.0.0.1`. |
| `HTTP 402 Insufficient balance` while scoring | NanoGPT holds a per-request deposit sized from `SCORER_MAX_TOKENS`; set `SCORER_MAX_TOKENS=2000`. A ping still works. |
| Scoring is slow and burns output tokens | Reasoning model: pin `SCORER_REASONING_EFFORT=none`; keep `SCORER_TIMEOUT` low (e.g. 90) so one hung call cannot block the tick for 10 minutes. |
| `AuthKeyDuplicatedError` | The same session file was used from two IPs; Telegram revoked the key. `doomscroller login` moves the stale file aside and starts fresh. |
| Two processes fight over one session | Never run `poll --loop`, `watch`, `mcp` and `chats`/`send` on the same session file; the MCP server uses its own by default. |
| `uv sync` fails with `Read-only file system` on `~/.cache/uv` | Sandboxed environment; use the venv + pip path or set `UV_CACHE_DIR` to a writable directory. |
| `uv.lock` out of date after editing `pyproject.toml` | Regenerate with `uv lock`; CI installs with pip, so the lock only matters to uv users. |
| `ModuleNotFoundError: doomscroller` | Run from the checkout root or install with `pip install -e .`. |

Machine-specific operational notes (broken docker port publishing, the stopped pipeline,
proxy quirks) live in [`AGENTS.md`](../AGENTS.md); they describe the owner's host, not the
project in general.

## Conventions

* Commit messages follow `type: imperative summary` (`feat:`, `fix:`, `docs:`), with the
  body explaining *why*.
* New behaviour lands with a test in `tests/`; the suite is the contract for the cursor
  guarantee, idempotent ingest and rubric validation.
* Comments explain reasoning, not mechanics — several non-obvious decisions (cursor
  transaction, OCR commits, JSON-mode fallback) are documented at the code site.
