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

    @property
    def session_path(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir / self.session_name


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
    )
