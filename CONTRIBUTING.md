# Contributing

Thanks for looking. This is a small, opinionated project — issues and PRs are
welcome, but please keep changes focused.

## Setup

```bash
git clone https://github.com/mister228orange/doomscroller.git
cd doomscroller
cp .env.example .env          # fill TELEGRAM_API_ID / HASH / PHONE
docker compose up -d db
docker compose run --rm doomscroller init-db
```

The poller, the scorer and the MCP server are **three separate roles** and
should stay that way. The poller is the only component holding the Telegram
login; the scorer gets PostgreSQL and a model, and no Telegram credentials.

## Before opening a PR

- `python -m pytest` passes
- no secrets in the diff (`.env`, `*.session`, tokens)
- the MCP tool surface is documented in the README if you change it

## Adding a feature

Open an issue first for anything larger than a bug fix, so we don't both build
the same thing.

## Security

Do not open a public issue for a vulnerability — see [SECURITY.md](SECURITY.md).
