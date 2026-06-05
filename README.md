# DoomScroller

DoomScroller observes Telegram chats and uses a local Ollama model to extract
events from new messages, such as a new job task being assigned. When an event is
detected, it sends a notification to a configured Telegram chat.

## What it does

- Connects to Telegram with Telethon.
- Polls one or more configured chats.
- Keeps per-chat state in `doom_states/`.
- Sends new messages plus current state to an Ollama model.
- Expects the model to return JSON with an updated state and optional event.
- Sends detected event notifications to `TARGET_CHAT`.

## Requirements

- Python 3.12 or newer.
- `uv` for dependency management.
- A Telegram API ID and API hash from `https://my.telegram.org/apps`.
- Ollama running locally, or reachable through `OLLAMA_URL`.
- The configured Ollama model pulled locally, for example:

```bash
ollama pull llama3
```

## Setup

Install dependencies:

```bash
uv sync
```

Edit `.env` and replace the placeholder values:

```dotenv
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=replace_with_api_hash
TELEGRAM_PHONE=+10000000000
TELEGRAM_SESSION=scroller_session

MONITORED_CHATS=jobs_chat,team_tasks_chat
TARGET_CHAT=me

OLLAMA_MODEL=llama3
OLLAMA_URL=http://localhost:11434
POLL_INTERVAL=10

PURPOSE=Observe Telegram chats and detect when a new job task is assigned.
EVENT_DESCRIPTIONS=New job task assigned|Existing task updated|Task cancelled
```

`MONITORED_CHATS` is a comma-separated list. `EVENT_DESCRIPTIONS` is a
pipe-separated list because event descriptions may contain commas.

## Running

Start Ollama, then run:

```bash
uv run python main.py
```

On the first run, Telethon may ask for a Telegram login code. It stores the
session in `TELEGRAM_SESSION`, which is ignored by git.

## Configuration

All runtime settings are loaded by `config.py`. Values can come from `.env` or
the process environment. Process environment variables win when both are set.

Important settings:

- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_PHONE`: Telegram credentials.
- `TELEGRAM_SESSION`: local Telethon session file name.
- `MONITORED_CHATS`: chats to observe.
- `TARGET_CHAT`: chat that receives notifications.
- `OLLAMA_MODEL`, `OLLAMA_URL`: local LLM runtime.
- `POLL_INTERVAL`: seconds between polling cycles per chat.
- `PURPOSE`: high-level instruction for event extraction.
- `EVENT_DESCRIPTIONS`: event names the model should detect.

## Local files

The following files are local runtime data and are ignored by git:

- `.env`
- `*.session`
- `doom_states/`
