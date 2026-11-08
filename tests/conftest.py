"""Test fixtures.

Runs against PostgreSQL when TEST_DATABASE_URL (or DATABASE_URL) is set — which is the
point, since that is the production target. Falls back to a temporary SQLite file so the
suite still runs without a database.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine

from doomscroller import models  # noqa: F401  (registers tables)


def _url() -> str:
    return (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or f"sqlite:///{Path(tempfile.gettempdir()) / 'doomscroller-test.sqlite'}"
    )


# The application reads DATABASE_URL via doomscroller.db; point it at the test database
# so MCP tools and CLI paths hit the same place as the fixtures.
os.environ.setdefault("DATABASE_URL", _url())
# The repo's own .env may carry deployment choices (sampling, model); explicit env wins
# over .env (override=False), so pin the reading defaults to keep tests deterministic.
os.environ.setdefault("SCORER_SAMPLE_EVERY", "1")
os.environ.setdefault("SCORER_REASONING_EFFORT", "none")


@pytest.fixture(scope="session")
def engine():
    url = _url()
    eng = create_engine(url, future=True, pool_pre_ping=True)
    SQLModel.metadata.drop_all(eng)
    SQLModel.metadata.create_all(eng)
    yield eng
    SQLModel.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture(autouse=True)
def clean_tables(engine):
    with engine.begin() as conn:
        for table in reversed(SQLModel.metadata.sorted_tables):
            conn.execute(table.delete())
    yield


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s
        s.rollback()
