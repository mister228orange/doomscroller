"""Deployment topology: three roles, three trust boundaries.

The poller owns the Telegram login. The scorer holds a model endpoint and the rubric and
must NOT hold the login. The MCP server is a client-side process that may run anywhere and
needs the database (plus Telegram only for its live tools).

These are not stylistic preferences — a scoring container that quietly carries
``TELEGRAM_API_HASH`` and a session file can act as the account, and a scorer that needs
model configuration to *read* results is unusable from another machine.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from doomscroller.config import load_scorer_config, load_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPOSE = PROJECT_ROOT / "docker-compose.yml"


# ── environment boundaries ───────────────────────────────────────────────────

def test_the_scorer_starts_without_any_telegram_configuration():
    """A scorer container gets no Telegram credentials — and must not miss them."""
    # Empty strings, not missing keys: config._load_env() reads the repo's .env with
    # override=False, so a variable that is already present (even empty) is not replaced.
    # This is exactly what the scorer container looks like — it has no .env at all.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(PROJECT_ROOT),
        "DATABASE_URL": "postgresql+psycopg://user:pw@127.0.0.1:5434/db",
        "TELEGRAM_API_ID": "",
        "TELEGRAM_API_HASH": "",
        "TELEGRAM_PHONE": "",
    }
    code = (
        "import json\n"
        "import doomscroller.scoring.agent  # the scorer must import without the account\n"
        "from doomscroller.config import load_scorer_config, load_settings\n"
        "out = {'scorer': load_scorer_config().model}\n"
        "try:\n"
        "    load_settings()\n"
        "    out['telegram_required'] = False\n"
        "except RuntimeError as exc:\n"
        "    out['telegram_required'] = 'TELEGRAM_API_ID' in str(exc)\n"
        "print(json.dumps(out))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout.strip().splitlines()[-1])
    # With no SCORER_* in the environment the scorer still has usable defaults
    # (NanoGPT + DeepSeek), and it still does not need the account.
    assert data["scorer"] == "deepseek-chat"
    assert data["telegram_required"] is True  # the ingest path does need the account


def test_mcp_uses_its_own_session_file(monkeypatch, tmp_path):
    """Two processes, two sessions: the MCP server must not contend with the poller."""
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_PHONE", "+10000000000")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_MCP_SESSION", raising=False)

    settings = load_settings()
    assert settings.session_path != settings.mcp_session_path
    assert settings.mcp_session_path.name == f"{settings.session_name}_mcp"


def test_mcp_session_name_is_configurable(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_MCP_SESSION", "agent_side")

    settings = load_settings()
    assert settings.mcp_session_path.name == "agent_side"


def test_send_is_denied_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SEND_ALLOWLIST", "")
    settings = load_settings()
    assert settings.send_allowlist == []
    assert settings.send_allowed("anychat") is False


def test_send_allowlist_matches_canonical_refs(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SEND_ALLOWLIST", "@somechat,t.me/other_chat")
    settings = load_settings()
    assert settings.send_allowed("@somechat") is True
    assert settings.send_allowed("t.me/somechat") is True
    assert settings.send_allowed("other_chat") is True
    assert settings.send_allowed("somechat_evil") is False


# ── compose boundaries ───────────────────────────────────────────────────────

def _service_block(name: str) -> str:
    """Extract one 2-space-indented service block from docker-compose.yml.

    Not a YAML parser on purpose: this test is a guard on *our* file's shape, and adding a
    YAML dependency to the test suite to check five lines is not worth it.
    """
    lines = COMPOSE.read_text().splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.rstrip() == f"  {name}:":
            start = index + 1
            break
    assert start is not None, f"service {name!r} not found in {COMPOSE}"
    block: list[str] = []
    for line in lines[start:]:
        if line.strip() and not line.startswith("    "):
            break
        block.append(line)
    return "\n".join(block)


def test_scorer_container_holds_no_telegram_secrets():
    block = _service_block("scorer")
    for secret in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_PHONE",
                   "TELEGRAM_SESSION", "TELEGRAM_MCP_SESSION", "SEND_ALLOWLIST"):
        assert secret not in block, f"the scorer container must not receive {secret}"


def test_scorer_container_does_not_mount_the_session_file():
    """The repo root holds the Telethon session; the scorer mounts only the rubric."""
    block = _service_block("scorer")
    assert "- .:/app" not in block
    assert "./scoring:/app/scoring" in block
    assert "SCORER_RUBRIC_DIR: /app/scoring" in block


def test_scorer_container_gets_its_own_configuration():
    block = _service_block("scorer")
    assert "DATABASE_URL:" in block
    for knob in ("SCORER_BASE_URL", "SCORER_MODEL", "SCORER_INTERVAL"):
        assert knob in block, f"scorer is missing {knob}"


def test_poller_keeps_the_session_and_gets_no_model_configuration():
    block = _service_block("doomscroller")
    assert "env_file:" in block
    assert "- .:/app" in block
    assert "SCORER_" not in block
    assert "poll" in block


def test_mcp_is_not_pinned_into_compose():
    """MCP is a client-side process; compose must not grow an implicit dependency on it."""
    text = COMPOSE.read_text()
    assert "  mcp:" not in text


# ── the MCP process with a database-only environment ────────────────────────

async def test_mcp_archive_and_score_tools_need_only_the_database(monkeypatch, session):
    """No TELEGRAM_*, no SCORER_*, no DATA_DIR: DB tools must still work."""
    from mcp import Client

    from doomscroller.mcp_server import mcp
    from doomscroller.store import record_event
    from doomscroller.subscriptions import subscribe

    for name in (
        "TELEGRAM_API_ID",
        "TELEGRAM_API_HASH",
        "TELEGRAM_PHONE",
        "SCORER_MODEL",
        "SCORER_BASE_URL",
        "DATA_DIR",
    ):
        monkeypatch.delenv(name, raising=False)

    sub = subscribe(session, "offlinechat")
    sub.chat_id = -1005550002222
    record_event(session, chat_id=sub.chat_id, message_id=1, event_type="new",
                 text="пост, к которому есть доступ у внешнего агента")
    session.commit()

    async with Client(mcp) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        assert {"read_messages", "post_scores", "channel_states", "connector_status"} <= tools

        read = json.loads(
            "".join(
                block.text
                for block in (
                    await client.call_tool("read_messages", {"chat": "offlinechat"})
                ).content
            )
        )
        assert read["count"] == 1

        status = json.loads(
            "".join(
                block.text
                for block in (await client.call_tool("connector_status", {})).content
            )
        )
        assert status["database"]["ok"] is True
        assert status["scoring"]["scored_posts"] == 0


async def test_mcp_live_tool_fails_clearly_without_telegram_credentials(monkeypatch, session):
    """A missing session must be a tool error, not a dead MCP server."""
    from mcp import Client

    from doomscroller import mcp_server

    for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_PHONE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(mcp_server, "_telegram", None)

    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("list_chats", {"limit": 5})
        text = "".join(getattr(block, "text", "") for block in result.content)
    assert result.is_error or "TELEGRAM_API_ID" in text


def test_read_path_does_not_import_telethon():
    """The scoring read path is DB-only; Telethon is imported only by live tools."""
    env = {
        **os.environ,
        "PYTHONPATH": str(PROJECT_ROOT),
        "DATABASE_URL": "sqlite://",
    }
    code = (
        "import sys, json, doomscroller.scoring.state\n"
        "print(json.dumps({'telethon': 'telethon' in sys.modules,"
        " 'agent': 'doomscroller.scoring.agent' in sys.modules}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = json.loads(result.stdout.strip().splitlines()[-1])
    assert loaded == {"telethon": False, "agent": False}
