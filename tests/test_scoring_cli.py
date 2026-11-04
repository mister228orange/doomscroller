"""The CLI surface of the scoring agent."""

from __future__ import annotations

import json

from doomscroller.cli import main
from doomscroller.models import PostScore
from doomscroller.scoring.state import apply_score, get_or_create_state
from doomscroller.store import record_event
from doomscroller.subscriptions import subscribe

CHAT = -1007770002222
POST = (
    "Первый тезис: форма задаёт содержание, и связка не становится аргументом. "
    "Второй тезис: инструмент отличается от информации тем, что его можно применить "
    "к другому случаю. Третий тезис: закрытая плотность повышает D и понижает P."
)


def _seed(session) -> None:
    sub = subscribe(session, "clichat")
    sub.chat_id = CHAT
    sub.title = "CLI Channel"
    record_event(session, chat_id=CHAT, message_id=1, event_type="new", text=POST)
    record_event(session, chat_id=CHAT, message_id=2, event_type="new", text=POST)
    session.commit()


def _seed_score(session) -> None:
    state = get_or_create_state(session, chat_id=CHAT, chat_ref="clichat")
    row = PostScore(
        chat_id=CHAT,
        message_id=1,
        status="scored",
        title="первый пост",
        primary="SCI",
        secondary=["TRUST"],
        main_thesis="инструмент, а не информация",
        score_i=8.6,
        score_v=8.8,
        score_u=8.7,
        score_p=4.5,
        score_s=4.5,
        score_d=9.0,
        kpd=8.78,
        eff=1.95,
        confidence="high",
        flags=[],
    )
    apply_score(session, state, row)
    session.commit()


def test_score_dry_run_prints_the_prompt(session, capsys):
    _seed(session)
    code = main(["score", "--chat", "clichat", "--dry-run"])
    out = capsys.readouterr()
    assert code == 0
    assert "message 1" in out.out
    assert "--- system ---" in out.out
    assert "--- user ---" in out.out
    assert "Третий тезис" in out.out
    assert "nothing was scored" in out.err


def test_score_dry_run_does_not_write(session, capsys):
    _seed(session)
    main(["score", "--chat", "clichat", "--dry-run"])
    capsys.readouterr()
    assert session.query(PostScore).count() == 0


def test_scores_command_prints_a_table(session, capsys):
    _seed(session)
    _seed_score(session)
    code = main(["scores", "clichat"])
    out = capsys.readouterr().out
    assert code == 0
    assert "8.6" in out and "8.78" in out
    assert "SCI" in out


def test_scores_command_json(session, capsys):
    _seed(session)
    _seed_score(session)
    main(["scores", "clichat", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["message_id"] == 1
    assert payload[0]["scores"] == {"I": 8.6, "V": 8.8, "U": 8.7, "P": 4.5, "S": 4.5, "D": 9.0}
    assert payload[0]["kpd"] == 8.78


def test_scores_command_reports_an_empty_channel(session, capsys):
    _seed(session)
    main(["scores", "clichat"])
    assert "no scores yet" in capsys.readouterr().out


def test_channels_command_lists_the_trajectory(session, capsys):
    _seed(session)
    _seed_score(session)
    code = main(["channels"])
    out = capsys.readouterr().out
    assert code == 0
    assert "clichat" in out
    assert "8.78" in out


def test_channel_state_command_prints_the_state(session, capsys):
    _seed(session)
    _seed_score(session)
    code = main(["channel-state", "clichat", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["total_scored"] == 1
    assert payload["means"]["I"] == 8.6
    assert payload["local_anchors"][0]["id"] == "p_1"


def test_score_on_an_unknown_chat_fails_clearly(session, capsys):
    _seed(session)
    try:
        main(["score", "--chat", "nope", "--dry-run"])
    except SystemExit as exc:  # _ordered_subscriptions raises SystemExit
        assert "not subscribed" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected SystemExit for an unknown chat")

# ── the doctor: `score --check` / `--list-models` ────────────────────────────

class _StubClient:
    """Stands in for ChatClient: no network, same surface the doctor uses."""

    instances: list["_StubClient"] = []
    reply: object = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls: list[str] = []
        _StubClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def ping(self):
        from doomscroller.scoring.llm import LLMResponse

        self.calls.append("ping")
        if isinstance(self.reply, Exception):
            raise self.reply
        return LLMResponse(
            text="ok", model="deepseek-v3.2", prompt_tokens=12, completion_tokens=1,
            latency_ms=41,
        )

    async def list_models(self, contains=None):
        self.calls.append(f"list:{contains}")
        if isinstance(self.reply, Exception):
            raise self.reply
        return ["deepseek-chat", "deepseek/deepseek-v4-pro"]


def _stub_backend(monkeypatch, reply=None):
    import doomscroller.scoring.llm as llm

    _StubClient.instances = []
    _StubClient.reply = reply
    monkeypatch.setattr(llm, "ChatClient", _StubClient)
    return _StubClient


def test_check_without_a_key_exits_nonzero_and_says_why(session, capsys, monkeypatch):
    _seed(session)
    monkeypatch.delenv("SCORER_API_KEY", raising=False)
    monkeypatch.delenv("NANOGPT_API_KEY", raising=False)
    code = main(["score", "--check"])
    out = capsys.readouterr().out
    assert code == 2
    assert "MISSING" in out
    assert "no key" in out
    # the local parts are still checked
    assert "rubric" in out and "database  : ok" in out


def test_check_reports_a_healthy_paid_endpoint(session, capsys, monkeypatch):
    _seed(session)
    monkeypatch.setenv("NANOGPT_API_KEY", "sk-test")
    stub = _stub_backend(monkeypatch)
    code = main(["score", "--check"])
    out = capsys.readouterr().out
    assert code == 0
    assert "backend   : ok" in out
    assert "latency" in out and "41 ms" in out
    assert stub.instances[0].kwargs["api_key"] == "sk-test"
    assert stub.instances[0].kwargs["retries"] == 0  # report the first failure
    assert stub.instances[0].calls == ["ping"]


def test_check_surfaces_an_auth_failure(session, capsys, monkeypatch):
    from doomscroller.scoring.llm import LLMError

    _seed(session)
    monkeypatch.setenv("SCORER_API_KEY", "sk-wrong")
    _stub_backend(monkeypatch, reply=LLMError("HTTP 401 — authentication failed", retryable=False))
    code = main(["score", "--check"])
    out = capsys.readouterr().out
    assert code == 2
    assert "authentication failed" in out


def test_list_models_works_without_a_key(session, capsys, monkeypatch):
    _seed(session)
    monkeypatch.delenv("SCORER_API_KEY", raising=False)
    monkeypatch.delenv("NANOGPT_API_KEY", raising=False)
    stub = _stub_backend(monkeypatch)
    code = main(["score", "--list-models", "deepseek"])
    out = capsys.readouterr().out
    assert code == 0
    assert "deepseek/deepseek-v4-pro" in out
    assert "<- current" in out
    assert stub.instances[0].calls == ["list:deepseek"]
