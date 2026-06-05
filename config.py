import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


def _load_env_file(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _csv(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _pipe_list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split("|") if item.strip()]


_load_env_file()

TELEGRAM_API_ID = int(_required("TELEGRAM_API_ID"))
TELEGRAM_API_HASH = _required("TELEGRAM_API_HASH")
TELEGRAM_PHONE = _required("TELEGRAM_PHONE")
TELEGRAM_SESSION = os.environ.get("TELEGRAM_SESSION", "scroller_session")

OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))

MONITORED_CHATS = _csv("MONITORED_CHATS")
TARGET_CHAT = _required("TARGET_CHAT")
PURPOSE = os.environ.get(
    "PURPOSE",
    "Observe Telegram chats and detect when a new job task is assigned.",
)
EVENT_DESCRIPTIONS = _pipe_list(
    "EVENT_DESCRIPTIONS",
    "New job task assigned|Existing task updated|Task cancelled",
)

if not MONITORED_CHATS:
    raise RuntimeError("MONITORED_CHATS must contain at least one chat username or id")


SCROLLER_CONFIG = {
    "purpose": PURPOSE,
    "monitored_chats": MONITORED_CHATS,
    "target_chat": TARGET_CHAT,
    "event_descriptions": EVENT_DESCRIPTIONS,
}
