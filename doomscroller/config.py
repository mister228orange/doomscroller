"""Configuration. Reads .env if present, then the process environment."""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional: .env is convenient locally, unnecessary in compose
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover
    load_dotenv = None

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    if load_dotenv is None:
        return
    for candidate in (Path.cwd() / ".env", BASE_DIR / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=False)


_load_env()


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw.replace(",", "."))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from exc


def _csv(name: str) -> list[str]:
    return [p.strip() for p in _env(name).split(",") if p.strip()]


@dataclass
class ProxyConfig:
    kind: str = "none"  # none | mtproxy | socks5
    host: str = ""
    port: int = 0
    secret: str = ""
    username: str = ""
    password: str = ""

    def telethon_kwargs(self) -> dict:
        """Return kwargs merged into TelegramClient(...)."""
        if self.kind == "mtproxy":
            from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate

            return {
                "connection": ConnectionTcpMTProxyRandomizedIntermediate,
                "proxy": (self.host, self.port, self.secret),
            }
        if self.kind == "socks5":
            proxy: dict = {"proxy_type": "socks5", "addr": self.host, "port": self.port}
            if self.username:
                proxy["username"] = self.username
            if self.password:
                proxy["password"] = self.password
            return {"proxy": proxy}
        return {}

    def describe(self) -> str:
        if self.kind == "none":
            return "direct (no proxy)"
        return f"{self.kind} {self.host}:{self.port}"


def parse_proxy(raw: str) -> ProxyConfig:
    """Parse TG_PROXY.

    Accepts ``off``/empty (direct), a ``t.me/proxy?...`` / ``tg://proxy?...`` link,
    or a ``socks5://user:pass@host:port`` URL. Never logs the secret.
    """
    raw = (raw or "").strip()
    if not raw or raw.lower() in {"off", "none", "direct"}:
        return ProxyConfig()

    if "t.me/proxy" in raw or raw.startswith("tg://proxy"):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
        try:
            return ProxyConfig(
                kind="mtproxy",
                host=q["server"][0],
                port=int(q["port"][0]),
                secret=q["secret"][0],
            )
        except (KeyError, ValueError, IndexError) as exc:
            raise RuntimeError(f"TG_PROXY looks like an MTProto link but is malformed: {exc}") from exc

    if raw.startswith(("socks5://", "socks4://")):
        u = urllib.parse.urlparse(raw)
        return ProxyConfig(
            kind="socks5",
            host=u.hostname or "",
            port=u.port or 1080,
            username=urllib.parse.unquote(u.username or ""),
            password=urllib.parse.unquote(u.password or ""),
        )

    raise RuntimeError(
        "TG_PROXY must be empty, 'off', a t.me/proxy link, or a socks5:// URL"
    )


@dataclass
class Settings:
    api_id: int = 0
    api_hash: str = ""
    phone: str = ""
    session_name: str = "scroller_session"
    data_dir: Path = field(default_factory=lambda: BASE_DIR)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    database_url: str = ""
    subscriptions: list[str] = field(default_factory=list)
    poll_interval: int = 15
    batch_size: int = 100
    # Outbound sending is DENIED unless a chat is explicitly allowlisted here.
    # "*" allows every chat (a deliberate opt-in). Sending acts as a real user
    # account, which Telegram rate-limits and can ban, so it is off by default.
    send_allowlist: list[str] = field(default_factory=list)
    send_rate_per_min: int = 20
    mcp_session_name: str = ""

    @property
    def session_path(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir / self.session_name

    @property
    def mcp_session_path(self) -> Path:
        """The MCP server's own session file, so it never contends with the poller."""
        name = self.mcp_session_name or f"{self.session_name}_mcp"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir / name

    def send_allowed(self, chat_ref: str) -> bool:
        """True only if this chat is explicitly allowlisted for outbound sends."""
        if not self.send_allowlist:
            return False
        if "*" in self.send_allowlist:
            return True
        from .subscriptions import normalise_ref

        target = normalise_ref(chat_ref).lower()
        return any(normalise_ref(a).lower() == target for a in self.send_allowlist)


def load_settings() -> Settings:
    api_id_raw = _env("TELEGRAM_API_ID")
    if not api_id_raw:
        raise RuntimeError("TELEGRAM_API_ID is not set")
    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise RuntimeError("TELEGRAM_API_ID must be an integer") from exc

    api_hash = _env("TELEGRAM_API_HASH")
    if not api_hash:
        raise RuntimeError("TELEGRAM_API_HASH is not set")

    data_dir = Path(_env("DATA_DIR") or BASE_DIR).expanduser()
    return Settings(
        api_id=api_id,
        api_hash=api_hash,
        phone=_env("TELEGRAM_PHONE"),
        session_name=_env("TELEGRAM_SESSION", "scroller_session"),
        data_dir=data_dir,
        proxy=parse_proxy(_env("TG_PROXY")),
        database_url=_env("DATABASE_URL"),
        subscriptions=_csv("SUBSCRIPTIONS"),
        poll_interval=_env_int("POLL_INTERVAL", 15),
        batch_size=_env_int("BATCH_SIZE", 100),
        send_allowlist=_csv("SEND_ALLOWLIST"),
        send_rate_per_min=_env_int("SEND_RATE_PER_MIN", 20),
        mcp_session_name=_env("TELEGRAM_MCP_SESSION"),
    )


# ── scoring agent ─────────────────────────────────────────────────────────────

#: NanoGPT's OpenAI-compatible endpoint. The scorer's default backend: DeepSeek models,
#: one key, a dollar-based balance instead of a local GPU.
NANOGPT_BASE_URL = "https://nano-gpt.com/api/v1"

#: The original DeepSeek model: `deepseek-chat` is DeepSeek's own V3 (NanoGPT describes it
#: as "DeepSeek original V3 model"), not a derivative, an abliterated fork or a TEE re-host.
#: A pinned id, not a `-latest` alias: a score row stores the model that produced it, and two
#: runs of the same rubric should differ only because the text differs.
DEFAULT_SCORER_MODEL = "deepseek-chat"

@dataclass
class ScorerConfig:
    """Everything the scoring agent needs.

    Deliberately independent of :class:`Settings`: the scorer reads PostgreSQL and calls
    an LLM, so it must be runnable without Telegram credentials at all.
    """

    rubric_dir: Path = field(default_factory=lambda: BASE_DIR / "scoring")
    #: NanoGPT by default — DeepSeek behind an OpenAI-compatible path.
    base_url: str = NANOGPT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_SCORER_MODEL
    temperature: float = 0.0
    timeout: float = 600.0
    retries: int = 2
    #: auto | on | off — whether to ask the backend for a JSON response format.
    json_mode: str = "auto"
    max_tokens: int = 0
    #: Pin the model's thinking budget (e.g. `none` for a reasoning model that starts
    #: burning thousands of output tokens per post). Empty means let the model decide.
    reasoning_effort: Optional[str] = None
    #: Read every Nth post (by message_id modulo). 1 = read everything. Sampling gates
    #: first-time scoring only; `--force` and rescores ignore it.
    sample_every: int = 1
    #: Posts shorter than this are archived but not rubric material — they are recorded
    #: as `skipped` so a one-word post cannot be scored as if it were an essay.
    min_chars: int = 120
    #: Cap on how much of one post is sent; a 100k-char post would bury the rubric.
    max_chars: int = 12000
    #: How many posts one run may score, per the whole run (backfill spans runs).
    max_posts_per_run: int = 20
    #: Retries for posts whose scoring failed.
    max_attempts: int = 3
    #: Rebuild the channel memory every N new posts; 0 disables the extra call.
    summary_every: int = 5
    #: EWMA window of the channel trend.
    window: int = 5
    #: Seconds between scheduler ticks when running with --loop.
    interval: int = 900
    #: Optional fixed genre for every post of this run.
    genre: str = ""


def load_scorer_config() -> ScorerConfig:
    rubric_dir = Path(_env("SCORER_RUBRIC_DIR") or BASE_DIR / "scoring").expanduser()
    return ScorerConfig(
        rubric_dir=rubric_dir,
        base_url=_env("SCORER_BASE_URL") or NANOGPT_BASE_URL,
        # NANOGPT_API_KEY is accepted too: it is the name the provider itself uses, so an
        # existing key does not have to be copied under a second name.
        api_key=_env("SCORER_API_KEY") or _env("NANOGPT_API_KEY"),
        model=_env("SCORER_MODEL") or DEFAULT_SCORER_MODEL,
        temperature=_env_float("SCORER_TEMPERATURE", 0.0),
        timeout=_env_float("SCORER_TIMEOUT", 600.0),
        retries=_env_int("SCORER_RETRIES", 2),
        json_mode=(_env("SCORER_JSON_MODE", "auto") or "auto").lower(),
        max_tokens=_env_int("SCORER_MAX_TOKENS", 0),
        reasoning_effort=_env("SCORER_REASONING_EFFORT") or None,
        sample_every=_env_int("SCORER_SAMPLE_EVERY", 1),
        min_chars=_env_int("SCORER_MIN_CHARS", 120),
        max_chars=_env_int("SCORER_MAX_CHARS", 12000),
        max_posts_per_run=_env_int("SCORER_MAX_POSTS_PER_RUN", 20),
        max_attempts=_env_int("SCORER_MAX_ATTEMPTS", 3),
        summary_every=_env_int("SCORER_SUMMARY_EVERY", 5),
        window=_env_int("SCORER_WINDOW", 5),
        interval=_env_int("SCORER_INTERVAL", 900),
        genre=_env("SCORER_GENRE"),
    )
