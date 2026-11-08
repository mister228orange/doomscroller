"""OCR of photo posts: two language passes, the ignore verdict, and scoring integration."""

from __future__ import annotations

import asyncio

import pytest
from sqlmodel import select

from doomscroller.models import MediaOcr, PostScore
from doomscroller.ocr import combined_body, transcribe_image


def _jpeg() -> bytes:
    """A real (tiny) JPEG: the payload now goes through image preparation."""
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (40, 20), "white").save(buffer, format="JPEG")
    return buffer.getvalue()
from doomscroller.scoring.agent import iter_pending_posts, store_score
from doomscroller.scoring.prompts import load_modules
from doomscroller.scoring.rubric import extract_json, finalize, parse_anchors
from doomscroller.store import record_event
from doomscroller.subscriptions import subscribe
from fakes import FakeScorerLLM

CHAT = -1007770002222


@pytest.fixture
def anchors():
    return parse_anchors(load_modules().anchor_text)


def _seed(session, message_id: int, text: str | None) -> None:
    subscribe(session, "ocrtest")
    # The subscription row gets a generated id; pin the chat id the tests use.
    from doomscroller.models import Subscription

    sub = session.exec(select(Subscription)).first()
    sub.chat_id = CHAT
    record_event(
        session,
        chat_id=CHAT,
        message_id=message_id,
        event_type="new",
        text=text,
        sender_name="author",
    )
    session.commit()


def test_transcribe_uses_the_english_pass_when_it_reads_text():
    llm = FakeScorerLLM(["  some english text\n"])
    outcome = asyncio.run(transcribe_image(llm, _jpeg()))
    assert outcome.text == "some english text"
    assert outcome.language == "eng"
    assert outcome.attempts == 1
    # One vision payload was sent, and it carried an image part.
    assert len(llm.raw_prompts) == 1
    part = llm.raw_prompts[0][0]["content"][1]
    assert part["type"] == "image_url"
    assert part["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_transcribe_falls_back_to_the_russian_pass():
    llm = FakeScorerLLM(["", "  русский текст\n"])
    outcome = asyncio.run(transcribe_image(llm, _jpeg()))
    assert outcome.text == "русский текст"
    assert outcome.language == "rus"
    assert outcome.attempts == 2
    assert len(llm.raw_prompts) == 2


def test_transcribe_ignores_an_image_with_no_text_in_either_pass():
    llm = FakeScorerLLM(["", "   "])
    outcome = asyncio.run(transcribe_image(llm, _jpeg()))
    assert outcome.text == ""
    assert outcome.language == "none"
    assert outcome.attempts == 2


def test_combined_body_keeps_caption_and_ocr_apart():
    assert combined_body("подпись", "текст с картинки") == (
        "подпись\n\n[текст с изображения]\nтекст с картинки"
    )
    assert combined_body(None, "только оцифровка") == "[текст с изображения]\nтолько оцифровка"
    assert combined_body("только подпись", None) == "только подпись"
    assert combined_body(None, None) == ""


def test_ocr_only_post_becomes_a_scoring_candidate(session, anchors):
    """A photo with no caption but readable text must be scored — on its OCR text."""
    _seed(session, 1, text=None)
    session.add(
        MediaOcr(
            chat_id=CHAT,
            message_id=1,
            status="ok",
            ocr_text="Текст, прочитанный с картинки",
            language="rus",
        )
    )
    session.commit()

    pending = list(iter_pending_posts(session, chat_id=CHAT, force=False, rescore_edited=False))
    assert len(pending) == 1
    message, score, body = pending[0]
    assert message.message_id == 1
    assert score is None
    assert "[текст с изображения]" in body
    assert "Текст, прочитанный с картинки" in body


def test_textless_photo_stays_ignored(session):
    """status='no_text' is the ignore verdict: no caption, no OCR text, no candidate."""
    _seed(session, 1, text=None)
    session.add(MediaOcr(chat_id=CHAT, message_id=1, status="no_text", language="none"))
    session.commit()
    pending = list(iter_pending_posts(session, chat_id=CHAT, force=False, rescore_edited=False))
    assert pending == []


def test_captioned_photo_scores_on_both_sources(session, anchors):
    """Caption + OCR text are combined, and the stored row carries the combined title."""
    _seed(session, 1, text="Короткая подпись автора")
    session.add(MediaOcr(chat_id=CHAT, message_id=1, status="ok", ocr_text="Скриншот чужого треда"))
    session.commit()

    llm = FakeScorerLLM([])
    modules = load_modules()
    message, _score, body = next(
        iter(iter_pending_posts(session, chat_id=CHAT, force=False, rescore_edited=False))
    )
    assert "Короткая подпись автора" in body and "Скриншот чужого треда" in body

    rubric = finalize(extract_json(llm.default), anchors=anchors, genre_hint=None)
    store_score(
        CHAT, message, rubric, model="fake-vision", version=modules.version, window=5, body=body
    )
    row = session.exec(select(PostScore).where(PostScore.message_id == 1)).one()
    assert row.title.startswith("Короткая подпись автора")


def test_ocr_newer_than_the_score_makes_the_post_pending_again(session, anchors):
    """OCR text that arrived after scoring is an edit as far as the rubric is concerned."""
    from datetime import timedelta

    from doomscroller.models import utcnow

    _seed(session, 1, text="Подпись длиной хотя бы сто двадцать символов, чтобы пост считался постом, а не огрызком — вот эти сто двадцать и наберём.")
    llm = FakeScorerLLM([])
    modules = load_modules()
    message, _score, body = next(
        iter(iter_pending_posts(session, chat_id=CHAT, force=False, rescore_edited=False))
    )
    rubric = finalize(extract_json(llm.default), anchors=anchors, genre_hint=None)
    store_score(CHAT, message, rubric, model="m", version=modules.version, window=5, body=body)

    # OCR lands ten seconds after the score: the post must come back.
    session.add(
        MediaOcr(
            chat_id=CHAT, message_id=1, status="ok", ocr_text="Текст со скриншота",
            updated_at=utcnow() + timedelta(seconds=10),
        )
    )
    session.commit()
    pending = list(iter_pending_posts(session, chat_id=CHAT, force=False, rescore_edited=False))
    assert [item[0].message_id for item in pending] == [1]
    assert "Текст со скриншота" in pending[0][2]
