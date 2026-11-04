"""Engine / session plumbing."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

_engine = None


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
        return create_engine(url, pool_pre_ping=True, future=True)
    if _engine is None:
        _engine = create_engine(database_url(), pool_pre_ping=True, future=True)
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


def healthcheck(url: str | None = None) -> dict:
    engine = get_engine(url)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
        row = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            )
        ).scalar()
    return {"ok": True, "tables": int(row or 0)}
