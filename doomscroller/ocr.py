"""Text extraction from photo posts.

A Telegram channel post is often a screenshot: the caption is a stub and the content is
inside the image. The scoring rubric can only read text, so photos go through OCR before
they reach the queue.

The rule from the methodology: try English first, then Russian; an image that yields no
text at all is ignored. With no system OCR available (tesseract needs a binary this
deployment cannot install), the engine is a vision model over the same OpenAI-compatible
endpoint the scorer uses — and the two-pass order is preserved literally: pass one asks
for the transcription assuming English, pass two runs only if pass one found nothing and
assumes Russian. A row with ``status='no_text'`` is the "ignore it" verdict, so a textless
image is looked at once and never again.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlmodel import Session, select

from .models import MediaOcr

logger = logging.getLogger(__name__)

#: Vision-capable, cheapest DeepSeek on the same endpoint the scorer already uses.
DEFAULT_OCR_MODEL = "deepseek/deepseek-v4.1-flash"

ENGLISH_PROMPT = (
    "Transcribe ALL text visible in this image, exactly as written, preserving line "
    "breaks. The text is most likely in English. Output only the transcription — no "
    "commentary, no markdown fences. If the image contains no text, output nothing."
)
RUSSIAN_PROMPT = (
    "Transcribe ALL text visible in this image, exactly as written, preserving line "
    "breaks. The text is most likely in Russian. Output only the transcription — no "
    "commentary, no markdown fences. If the image contains no text, output nothing."
)

#: (language, prompt) in the order the methodology prescribes.
PASSES: tuple[tuple[str, str], ...] = (("eng", ENGLISH_PROMPT), ("rus", RUSSIAN_PROMPT))

#: Marks where the caption ends and the transcription begins inside a scored body.
OCR_MARKER = "[текст с изображения]"


@dataclass
class OcrOutcome:
    text: str
    language: Optional[str]  # "eng", "rus" or "none"
    attempts: int
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class OcrReport:
    chat: str
    photos: int = 0
    processed: int = 0
    ok: int = 0
    no_text: int = 0
    skipped_existing: int = 0
    errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error_details: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "chat": self.chat,
            "photos": self.photos,
            "processed": self.processed,
            "ok": self.ok,
            "no_text": self.no_text,
            "skipped_existing": self.skipped_existing,
            "errors": self.errors,
            "tokens": {
                "prompt": self.prompt_tokens,
                "completion": self.completion_tokens,
            },
            "error_details": self.error_details,
        }


def combined_body(caption: Optional[str], ocr_text: Optional[str]) -> str:
    """The text a post carries for scoring: caption first, then what OCR read off the image.

    The marker keeps the two sources apart — the rubric treats the author's own words and
    words photographed from elsewhere differently (rule 11 on borrowed content).
    """
    parts: list[str] = []
    if caption and caption.strip():
        parts.append(caption.strip())
    if ocr_text and ocr_text.strip():
        parts.append(f"{OCR_MARKER}\n" + ocr_text.strip())
    return "\n\n".join(parts)


def _prepare_image(image: bytes, *, max_side: int = 1600, quality: int = 85) -> tuple[bytes, str]:
    """Bound the payload: NanoGPT rejects oversized request bodies with HTTP 413.

    Telegram photos can arrive far larger than OCR needs. With Pillow available the image
    is scaled to ``max_side`` on the long side and re-encoded as JPEG; without it the
    original bytes pass through unchanged (and a huge screenshot may fail with 413).
    """
    try:
        from io import BytesIO

        from PIL import Image
    except ImportError:  # pragma: no cover - pillow is an optional comfort
        return image, "image/jpeg"
    try:
        img = Image.open(BytesIO(image))
    except Exception:  # noqa: BLE001 — not parseable here; let the backend judge it
        return image, "image/jpeg"
    with img:
        width, height = img.size
        if max(width, height) <= max_side and img.format == "JPEG":
            return image, "image/jpeg"
        scale = min(1.0, max_side / max(width, height))
        if scale < 1.0:
            img = img.resize((max(1, int(width * scale)), max(1, int(height * scale))))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=quality)
        return buffer.getvalue(), "image/jpeg"


def _vision_messages(prompt: str, image_b64: str, mime: str) -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{image_b64}"},
                },
            ],
        }
    ]


async def transcribe_image(
    llm: Any,
    image: bytes,
    *,
    passes: tuple[tuple[str, str], ...] = PASSES,
) -> OcrOutcome:
    """Run the language passes in order; the first pass that reads text wins."""
    image, mime = _prepare_image(image)
    image_b64 = base64.b64encode(image).decode("ascii")
    prompt_tokens = 0
    completion_tokens = 0
    for attempt, (language, prompt) in enumerate(passes, start=1):
        response = await llm.chat_raw(
            _vision_messages(prompt, image_b64, mime),
            json_mode=False,
            max_tokens=2000,
        )
        prompt_tokens += response.prompt_tokens or 0
        completion_tokens += response.completion_tokens or 0
        text = response.text.strip()
        if text:
            return OcrOutcome(
                text=text,
                language=language,
                attempts=attempt,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
    return OcrOutcome(
        text="",
        language="none",
        attempts=len(passes),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def is_image_message(message: Any) -> bool:
    """A real picture: a photo, or an image file sent as a document.

    Webpage previews (``MessageMediaWebPage``) are link decorations — their banner rarely
    carries the post's text, so they are not OCR material. The same goes for videos and
    documents that merely carry a thumbnail: ``message.photo`` is truthy for those, which
    is exactly how a 7 MB video ended up base64-encoded into a request once.
    """
    try:
        from telethon.tl.types import (
            DocumentAttributeImageSize,
            MessageMediaDocument,
            MessageMediaPhoto,
        )
    except ImportError:  # pragma: no cover - telethon is a hard dep in practice
        return bool(getattr(message, "photo", False))
    media = getattr(message, "media", None)
    if isinstance(media, MessageMediaPhoto):
        return True
    if isinstance(media, MessageMediaDocument):
        document = media.document
        if document is None:
            return False
        if (document.mime_type or "").startswith("image/"):
            return True
        return any(
            isinstance(attr, DocumentAttributeImageSize)
            for attr in (document.attributes or [])
        )
    return False


async def ocr_channel(
    client: Any,
    *,
    chat_ref: str,
    llm: Any,
    model: str = DEFAULT_OCR_MODEL,
    limit: int = 200,
    force: bool = False,
    session_factory: Any,
) -> OcrReport:
    """Find photo posts, read their text, store one ``media_ocr`` row per message.

    Each message commits in its own transaction: an interrupted pass keeps everything it
    already read, and ``status='no_text'`` rows make the ignore verdict permanent unless
    ``force`` is set.
    """
    from .telegram_ops import resolve_chat  # local import: keeps module import cheap

    report = OcrReport(chat=chat_ref)
    entity = await resolve_chat(client, chat_ref)
    chat_id = entity.id

    with session_factory() as session:
        existing = {
            row.message_id: row
            for row in session.exec(
                select(MediaOcr).where(MediaOcr.chat_id == chat_id)
            ).all()
        }

    iterator = client.iter_messages(entity, limit=limit)
    while True:
        try:
            message = await iterator.__anext__()
        except StopAsyncIteration:
            break
        except Exception as exc:  # noqa: BLE001 — a desynced connection must not lose the pass
            # Telethon can hit a malformed server response mid-iteration (the exception text
            # is raw protocol bytes). Everything committed so far stays; the next run
            # resumes from the first message without an OCR row.
            truncated = str(exc)[:160].replace("\n", " ")
            logger.error("ocr pass interrupted after %d processed: %s", report.processed, truncated)
            report.errors += 1
            report.error_details.append(f"iteration interrupted: {truncated}")
            break
        if not is_image_message(message):
            # Webpage previews, videos, non-image documents: not OCR material. An error
            # row left behind by an earlier, less careful pass is closed out here so it
            # stops being retried.
            previous = existing.get(message.id)
            if previous is not None and previous.status == "error":
                _store(
                    session_factory, chat_id, message.id,
                    status="skipped", detail="media is not an image",
                )
            continue
        report.photos += 1
        previous = existing.get(message.id)
        if previous is not None and not force and previous.status in ("ok", "no_text", "skipped"):
            report.skipped_existing += 1
            continue

        try:
            image = await client.download_media(message, file=bytes)
            if not image:
                raise RuntimeError("media download returned nothing")
            outcome = await transcribe_image(llm, image)
        except Exception as exc:  # noqa: BLE001 — one bad image must not stop the pass
            report.errors += 1
            report.error_details.append(f"#{message.id}: {exc}")
            logger.warning("ocr failed for %s/%s: %s", chat_ref, message.id, exc)
            _store(session_factory, chat_id, message.id, status="error", detail=str(exc)[:500])
            continue

        report.processed += 1
        report.prompt_tokens += outcome.prompt_tokens
        report.completion_tokens += outcome.completion_tokens
        status = "ok" if outcome.text else "no_text"
        _store(
            session_factory,
            chat_id,
            message.id,
            status=status,
            ocr_text=outcome.text or None,
            language=outcome.language,
            engine="llm-vision",
            model=model,
            attempts=outcome.attempts,
            prompt_tokens=outcome.prompt_tokens,
            completion_tokens=outcome.completion_tokens,
        )
        if status == "ok":
            report.ok += 1
            logger.info(
                "ocr %s/%s: %s, %d chars (%d tokens in / %d out)",
                chat_ref, message.id, outcome.language, len(outcome.text),
                outcome.prompt_tokens, outcome.completion_tokens,
            )
        else:
            report.no_text += 1
            logger.info("ocr %s/%s: no text in either pass — ignored", chat_ref, message.id)
        # Yield to the event loop between images; keeps the session responsive.
        await asyncio.sleep(0)

    return report


def _store(session_factory: Any, chat_id: int, message_id: int, **fields: Any) -> None:
    with session_factory() as session:
        row = session.exec(
            select(MediaOcr).where(
                MediaOcr.chat_id == chat_id, MediaOcr.message_id == message_id
            )
        ).first()
        if row is None:
            row = MediaOcr(chat_id=chat_id, message_id=message_id)
        for key, value in fields.items():
            setattr(row, key, value)
        session.add(row)
        session.commit()
