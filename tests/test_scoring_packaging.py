"""Where the rubric comes from, and what the read path is allowed to depend on.

Two deployments matter here, and they are different services:

* the **scorer** container/process — needs the rubric files and a model;
* the **MCP server** — may run on another machine entirely, reads PostgreSQL only, and
  must not need the rubric, a model, or Telegram credentials to serve scores.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from doomscroller.scoring import prompts
from doomscroller.scoring.prompts import (
    default_rubric_dir,
    load_modules,
    rubric_dir_candidates,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_RUBRIC = PROJECT_ROOT / "scoring"


def test_a_checkout_uses_its_own_scoring_directory():
    assert REAL_RUBRIC.is_dir()
    assert default_rubric_dir() == REAL_RUBRIC
    assert (default_rubric_dir() / "00_system.md").is_file()


def test_the_checkout_wins_over_a_packaged_copy(tmp_path, monkeypatch):
    """Editing a rule file must take effect — a stale bundled copy must never shadow it."""
    site = tmp_path / "site-packages"
    package = site / "doomscroller" / "scoring"
    package.mkdir(parents=True)
    rubric = site / "doomscroller" / "rubric"  # where force-include puts the modules
    rubric.mkdir()
    # pretend the checkout is gone
    monkeypatch.setattr(prompts, "__file__", str(package / "prompts.py"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SCORER_RUBRIC_DIR", raising=False)

    candidates = rubric_dir_candidates()
    assert site / "scoring" in candidates  # <repo>/scoring — absent in this simulation
    assert rubric in candidates  # shipped inside the wheel
    # with no checkout, the packaged copy is used
    assert not (site / "scoring").exists()
    for name in ("00_system.md", "06_procedure.md"):
        shutil.copy(REAL_RUBRIC / name, rubric / name)
    assert default_rubric_dir() == rubric


def test_env_override_wins(tmp_path, monkeypatch):
    custom = tmp_path / "my-rubric"
    custom.mkdir()
    monkeypatch.setenv("SCORER_RUBRIC_DIR", str(custom))
    assert default_rubric_dir() == custom
    assert rubric_dir_candidates()[0] == custom


def test_env_override_pointing_nowhere_is_an_explicit_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SCORER_RUBRIC_DIR", str(tmp_path / "nope"))
    with pytest.raises(Exception) as excinfo:
        load_modules()
    assert "rubric directory not found" in str(excinfo.value)


def test_modules_load_from_a_packaged_layout(tmp_path, monkeypatch):
    """A `pip install doomscroller` with no checkout next to it still scores."""
    package = tmp_path / "site-packages" / "doomscroller" / "scoring"
    rubric = tmp_path / "site-packages" / "doomscroller" / "rubric"
    rubric.mkdir(parents=True)
    package.mkdir(parents=True, exist_ok=True)
    for path in REAL_RUBRIC.glob("*.md"):
        shutil.copy(path, rubric / path.name)
    monkeypatch.setattr(prompts, "__file__", str(package / "prompts.py"))
    monkeypatch.delenv("SCORER_RUBRIC_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    modules = load_modules()
    assert modules.directory == rubric
    assert modules.version  # same files, same version as the checkout
    assert modules.version == load_modules(REAL_RUBRIC).version


def test_the_read_path_does_not_import_the_scorer():
    """The MCP reader may run anywhere: no agent, no chat client pulled in."""
    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT), "DATABASE_URL": "sqlite://"}
    code = (
        "import sys, json, doomscroller.scoring.state\n"
        "print(json.dumps({\n"
        "  'agent': 'doomscroller.scoring.agent' in sys.modules,\n"
        "  'llm': 'doomscroller.scoring.llm' in sys.modules,\n"
        "  'httpx': 'httpx' in sys.modules,\n"
        "  'telethon': 'telethon' in sys.modules,\n"
        "  'sqlalchemy': 'sqlalchemy' in sys.modules,\n"
        "}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = __import__("json").loads(result.stdout.strip().splitlines()[-1])
    assert loaded["agent"] is False
    assert loaded["llm"] is False
    assert loaded["httpx"] is False
    assert loaded["telethon"] is False
    assert loaded["sqlalchemy"] is True  # it does read the archive tables
