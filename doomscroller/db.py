"""Engine / session plumbing."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlmodel import Session, SQLModel, create_engine

_engine = None

# Seconds to wait for a TCP connection to PostgreSQL. Without this, libpq waits forever
# when the database is unreachable but its port accepts SYNs (a blackholed docker-proxy,
# a suspended container, a firewall that drops rather than rejects). An MCP client gives
# a tool call ~30 s and then reports a bare "Request timed out", so the tool must fail
# fast and explain itself instead of hanging the agent.
DEFAULT_CONNECT_TIMEOUT = 5


def _connect_args(url: str) -> dict:
    """Per-dialect connect arguments, chiefly a bounded connect timeout."""
    if url.startswith("sqlite"):
        return {}
    try:
        query = make_url(url).query
    except Exception:  # noqa: BLE001 — a malformed URL is reported by create_engine
        query = {}
    if "connect_timeout" in query:
        return {}
    timeout = os.environ.get("DB_CONNECT_TIMEOUT", "").strip()
    try:
        seconds = int(timeout) if timeout else DEFAULT_CONNECT_TIMEOUT
    except ValueError:
        seconds = DEFAULT_CONNECT_TIMEOUT
    return {"connect_timeout": max(1, seconds)}


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and set it, e.g.\n"
            "  postgresql+psycopg://doomscroller:doomscroller@127.0.0.1:5434/doomscroller"
        )
    return url


def get_engine(url: str | None = None):
    global _engine
    if url is not None:
        return create_engine(
            url, pool_pre_ping=True, future=True, connect_args=_connect_args(url)
        )
    if _engine is None:
        resolved = database_url()
        _engine = create_engine(
            resolved,
            pool_pre_ping=True,
            future=True,
            connect_args=_connect_args(resolved),
        )
    return _engine


def init_db(url: str | None = None) -> None:
    """Create tables if they do not exist. Idempotent."""
    from . import models  # noqa: F401  (register tables)

    engine = get_engine(url)
    SQLModel.metadata.create_all(engine)


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    engine = get_engine(url)
    # expire_on_commit=False: rows returned to callers stay readable after the
    # session closes, which is what the CLI wants.
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_factory(url: str | None = None) -> Iterator[Session]:
    """A session whose transaction the caller owns (commits itself, closes on exit).

    For passes that want one transaction per item — an interrupted OCR run keeps every
    image it already read instead of losing the whole batch.
    """
    session = Session(get_engine(url), expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()


def healthcheck(url: str | None = None) -> dict:
    engine = get_engine(url)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
        # Inspector rather than information_schema: the same check has to work on
        # PostgreSQL (production) and on the SQLite file the test suite falls back to.
        tables = inspect(conn).get_table_names()
    return {"ok": True, "tables": len(tables)}
