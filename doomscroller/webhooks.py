"""Forward arriving messages of a chat to a webhook.

Subscribing is an MCP tool (``webhook_subscribe``); delivery is this module's job. The
listener runs on the MCP server's own Telethon session, so it sees messages as Telegram
delivers them — including chats that were never subscribed for archiving.

Three deliberate properties:

* **Raw update, not a summary.** The body of a delivery is the *whole* Telethon update as
  a plain dict (``client.to_json`` order preserved, non-JSON scalars stringified). The
  receiver is an agent or a script that may want anything from it, so nothing is thrown
  away at the boundary.
* **Unsubscribing stops delivery immediately.** The chat set is re-read for every
  incoming message, so a ``webhook_unsubscribe`` call takes effect without a restart.
* **A dead endpoint does not kill the listener.** Failures are counted and the last error
  is stored on the row, which is what ``webhook_list`` reports.

The token is a shared secret in ``WEBHOOK_TOKEN``. It is sent as ``X-Webhook-Token``
rather than in the URL so it does not end up in logs and proxy access records.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any, Optional

import httpx
from telethon import TelegramClient, events

from .db import session_scope
from .store import list_webhooks, record_delivery

logger = logging.getLogger(__name__)

DELIVERY_TIMEOUT = 10.0


def webhook_token() -> str:
    return (os.environ.get("WEBHOOK_TOKEN") or "").strip()


def _plain(value: Any) -> Any:
    """Make a Telethon object JSON-safe without losing information.

    Telethon's own ``to_dict`` drops ``None`` fields; that is fine for an update, and
    dates become ISO strings. Anything still exotic becomes ``str()`` rather than being
    dropped — a receiver should see the field even if it is not a plain scalar.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return {str(k): _plain(v) for k, v in to_dict().items()}
        except Exception:  # noqa: BLE001 — a broken TL object must still be forwarded
            pass
    return str(value)


def update_payload(event: Any) -> dict:
    """The JSON body a webhook receives for one arriving message."""
    message = getattr(event, "message", None)
    return {
        "chat_id": getattr(event, "chat_id", None),
        "message_id": getattr(message, "id", None),
        "raw": _plain(getattr(message, "to_dict", None) and message.to_dict() or message),
    }


async def deliver(
    url: str,
    payload: dict,
    *,
    token: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[str]:
    """POST ``payload`` to ``url``. Returns ``None`` on success, an error string otherwise."""
    headers = {"Content-Type": "application/json"}
    secret = token if token is not None else webhook_token()
    if secret:
        headers["X-Webhook-Token"] = secret
    try:
        if client is not None:
            response = await client.post(url, json=payload, headers=headers,
                                         timeout=DELIVERY_TIMEOUT)
        else:
            async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT) as own:
                response = await own.post(url, json=payload, headers=headers)
    except Exception as exc:  # noqa: BLE001 — network errors are reported, not raised
        return f"{type(exc).__name__}: {exc}"[:500]
    if response.status_code >= 400:
        return f"HTTP {response.status_code}: {response.text[:200]}"
    return None


async def forward_to_chat(chat_id: int, payload: dict) -> Optional[str]:
    """Deliver one message to its chat's webhook, updating the counters afterwards.

    Reads the row *now* rather than caching it, so an unsubscribe between two messages
    is honoured immediately. Returns the error string when the chat has no webhook or
    the delivery failed.
    """
    with session_scope() as session:
        row = next((w for w in list_webhooks(session) if int(w.chat_id) == int(chat_id)), None)
        if row is None or not row.enabled:
            return None
        url = row.url

    body = {**payload, "chat_id": int(chat_id)}
    error = await deliver(url, body)

    with session_scope() as session:
        record_delivery(session, int(chat_id), error=error)
    if error:
        logger.warning("webhook delivery to %s for chat %s failed: %s", url, chat_id, error)
    return error


def register_forwarder(client: TelegramClient) -> Any:
    """Attach the live listener to ``client``. Returns the event builder for tests."""

    @client.on(events.NewMessage(incoming=True))
    async def _forward(event: Any) -> None:  # noqa: ANN001 — Telethon handler signature
        try:
            await forward_to_chat(
                int(getattr(event, "chat_id")), update_payload(event)
            )
        except Exception:  # noqa: BLE001 — one bad update must not stop the listener
            logger.exception("webhook forwarder failed for one message")

    return _forward
