"""Command line interface.

Typical use::

    doomscroller subscribe @somechannel t.me/another
    doomscroller poll                 # store everything new (safe to re-run)
    doomscroller fetch @somechannel --limit 100
    doomscroller fetch @somechannel --all > archive.txt
    doomscroller new @somechannel
    doomscroller search "grant" --limit 20
    doomscroller stats
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any, Optional

from sqlmodel import Session

from . import __version__
from .config import load_settings
from .db import init_db, session_scope
from .models import Message, Subscription
from .store import (
    events_for_message,
    fetch_messages,
    iter_all_messages,
    new_messages,
    search_messages,
    stats,
)
from .subscriptions import (
    get_subscription,
    list_subscriptions,
    normalise_ref,
    set_enabled,
    subscribe,
    sync_from_env,
    unsubscribe,
)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _resolve_chat_id(session: Session, ref: str) -> int:
    sub = get_subscription(session, ref)
    if sub is None:
        # allow raw numeric chat ids that were never subscribed
        if normalise_ref(ref).lstrip("-").isdigit():
            return int(normalise_ref(ref))
        raise SystemExit(
            f"{ref!r} is not subscribed. Add it with `doomscroller subscribe {ref}` "
            "and run `doomscroller poll` to resolve its id."
        )
    if sub.chat_id is None:
        raise SystemExit(
            f"{ref!r} has no resolved chat id yet — run `doomscroller poll` first."
        )
    return int(sub.chat_id)


def _message_row(m: Message) -> dict[str, Any]:
    return {
        "chat_id": m.chat_id,
        "message_id": m.message_id,
        "date": m.date.isoformat() if m.date else None,
        "sender": m.sender_name,
        "sender_id": m.sender_id,
        "text": m.text,
        "reply_to": m.reply_to_message_id,
        "edited": m.edited_at.isoformat() if m.edited_at else None,
        "deleted": m.deleted,
    }


def _print_messages(rows: list[Message], as_json: bool) -> None:
    if as_json:
        print(json.dumps([_message_row(m) for m in rows], ensure_ascii=False, indent=2, default=str))
        return
    if not rows:
        print("(no messages)")
        return
    for m in rows:
        when = m.date.strftime("%Y-%m-%d %H:%M") if m.date else "?"
        who = (m.sender_name or str(m.sender_id) or "?")[:20]
        flags = []
        if m.edited_at:
            flags.append("edited")
        if m.deleted:
            flags.append("deleted")
        flag = f" [{'/'.join(flags)}]" if flags else ""
        text = (m.text or "").replace("\n", " ")
        if len(text) > 300:
            text = text[:300] + "…"
        print(f"{m.message_id:>8}  {when}  {who:<20}{flag}  {text}")


# ── sync commands ─────────────────────────────────────────────────────────────

def cmd_subscribe(args) -> int:
    init_db()
    with session_scope() as session:
        for ref in args.refs:
            existed = get_subscription(session, ref) is not None
            sub = subscribe(session, ref)
            print(f"{'already subscribed' if existed else 'subscribed'}: {sub.chat_ref}")
    return 0


def cmd_unsubscribe(args) -> int:
    init_db()
    with session_scope() as session:
        for ref in args.refs:
            ok = unsubscribe(session, ref)
            print(f"{'removed' if ok else 'not found'}: {normalise_ref(ref)}")
    return 0


def cmd_enable(args) -> int:
    init_db()
    with session_scope() as session:
        sub = set_enabled(session, args.ref, not args.disable)
        if sub is None:
            print(f"not found: {args.ref}", file=sys.stderr)
            return 1
        print(f"{sub.chat_ref}: enabled={sub.enabled}")
    return 0


def cmd_subscriptions(args) -> int:
    init_db()
    with session_scope() as session:
        subs = list_subscriptions(session)
        if not subs:
            print("(none — add one with `doomscroller subscribe <chat>`)")
            return 0
        if args.json:
            print(json.dumps(
                [
                    {
                        "chat_ref": s.chat_ref,
                        "chat_id": s.chat_id,
                        "title": s.title,
                        "kind": s.kind,
                        "enabled": s.enabled,
                        "cursor_message_id": s.cursor_message_id,
                        "message_count": s.message_count,
                        "last_polled_at": s.last_polled_at.isoformat() if s.last_polled_at else None,
                        "last_error": s.last_error,
                    }
                    for s in subs
                ],
                ensure_ascii=False,
                indent=2,
                default=str,
            ))
            return 0
        for s in subs:
            state = "on " if s.enabled else "off"
            err = f"  ERR: {s.last_error[:60]}" if s.last_error else ""
            print(
                f"[{state}] {s.chat_ref:<32} id={str(s.chat_id):<16} "
                f"msgs={s.message_count:<7} cursor={s.cursor_message_id:<10} "
                f"{(s.title or '')[:30]}{err}"
            )
    return 0


def cmd_fetch(args) -> int:
    init_db()
    with session_scope() as session:
        chat_id = _resolve_chat_id(session, args.chat)
        if args.all:
            for m in iter_all_messages(
                session, chat_id, after_id=args.after, batch_size=args.batch,
                include_deleted=args.include_deleted,
            ):
                when = m.date.strftime("%Y-%m-%d %H:%M") if m.date else "?"
                print(f"{m.message_id}\t{when}\t{m.sender_name or ''}\t{(m.text or '').replace(chr(10),' ')}")
            return 0
        rows = fetch_messages(
            session,
            chat_id,
            limit=args.limit,
            before_id=args.before,
            after_id=args.after,
            include_deleted=args.include_deleted,
            newest_first=not args.oldest_first,
        )
        _print_messages(rows, args.json)
    return 0


def cmd_new(args) -> int:
    init_db()
    with session_scope() as session:
        chat_id = _resolve_chat_id(session, args.chat)
        rows = new_messages(session, chat_id, since_id=args.since)
        _print_messages(rows, args.json)
        print(f"\n({len(rows)} new)", file=sys.stderr)
    return 0


def cmd_search(args) -> int:
    init_db()
    with session_scope() as session:
        chat_id = None
        if args.chat:
            chat_id = _resolve_chat_id(session, args.chat)
        rows = search_messages(
            session, args.query, chat_id=chat_id, limit=args.limit,
            include_deleted=args.include_deleted,
        )
        _print_messages(rows, args.json)
    return 0


def cmd_events(args) -> int:
    init_db()
    with session_scope() as session:
        chat_id = _resolve_chat_id(session, args.chat)
        events = events_for_message(session, chat_id, args.message_id)
        if not events:
            print("(no events recorded for that message)")
            return 0
        for e in events:
            print(f"{e.id:>6}  {e.event_type:<8} {e.created_at.isoformat()}  {(e.text or '')[:120]}")
    return 0


def cmd_stats(args) -> int:
    init_db()
    with session_scope() as session:
        print(json.dumps(stats(session), indent=2))
    return 0


def cmd_mcp(args) -> int:
    """Serve the archive over MCP (stdio by default; HTTP for remote clients)."""
    from .mcp_server import run

    if args.transport == "stdio":
        run(transport="stdio")
    else:
        run(transport=args.transport, host=args.host, port=args.port)
    return 0


def cmd_init_db(args) -> int:
    init_db()
    print("schema ready")
    return 0


# ── async commands ────────────────────────────────────────────────────────────

def _run_async(coro) -> int:
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        return 130


async def _poll(args) -> int:
    from .ingest import poll_all, poll_subscription, run_poll_loop
    from .telegram_client import build_client, ensure_connected

    settings = load_settings()
    init_db()
    with session_scope() as session:
        sync_from_env(session, settings.subscriptions)

    client = build_client(settings)
    await ensure_connected(client, settings)
    try:
        if args.loop:
            await run_poll_loop(client, settings)
            return 0
        if args.chat:
            with session_scope() as session:
                sub = get_subscription(session, args.chat)
                if sub is None:
                    print(f"not subscribed: {args.chat}", file=sys.stderr)
                    return 1
                count = await poll_subscription(client, session, sub, settings)
                print(f"{sub.chat_ref}: {count} new message(s)")
            return 0
        results = await poll_all(client, settings)
        for ref, res in results.items():
            if res.get("ok"):
                print(f"{ref}: {res['new']} new")
            else:
                print(f"{ref}: FAILED — {res['error']}", file=sys.stderr)
        return 0 if all(r.get("ok") for r in results.values()) else 1
    finally:
        await client.disconnect()


async def _watch(args) -> int:
    from .ingest import watch
    from .telegram_client import build_client, ensure_connected

    settings = load_settings()
    init_db()
    with session_scope() as session:
        sync_from_env(session, settings.subscriptions)

    client = build_client(settings)
    await ensure_connected(client, settings)
    await watch(client, settings)
    return 0


# ── parser ────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="doomscroller",
        description="Subscribe to Telegram chats/channels, archive every event, query anytime.",
    )
    p.add_argument("--version", action="version", version=f"doomscroller {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("subscribe", help="add chats/channels to archive")
    s.add_argument("refs", nargs="+")
    s.set_defaults(func=cmd_subscribe)

    s = sub.add_parser("unsubscribe", help="remove a subscription")
    s.add_argument("refs", nargs="+")
    s.set_defaults(func=cmd_unsubscribe)

    s = sub.add_parser("subscriptions", help="list subscriptions and their state")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_subscriptions)

    s = sub.add_parser("enable", help="enable a subscription")
    s.add_argument("ref")
    s.add_argument("--disable", action="store_true")
    s.set_defaults(func=cmd_enable)

    s = sub.add_parser("poll", help="store everything new (safe to re-run)")
    s.add_argument("--chat", help="only this chat")
    s.add_argument("--loop", action="store_true", help="keep polling forever")
    s.set_defaults(func=lambda a: _run_async(_poll(a)))

    s = sub.add_parser("watch", help="stay connected and store events live")
    s.set_defaults(func=lambda a: _run_async(_watch(a)))

    s = sub.add_parser("fetch", help="read stored messages from a chat")
    s.add_argument("chat")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--before", type=int, default=None, help="older than this message id")
    s.add_argument("--after", type=int, default=None, help="newer than this message id")
    s.add_argument("--all", action="store_true", help="stream the entire archive (TSV)")
    s.add_argument("--batch", type=int, default=1000, help="batch size for --all")
    s.add_argument("--oldest-first", action="store_true")
    s.add_argument("--include-deleted", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser("new", help="everything newer than the stored cursor")
    s.add_argument("chat")
    s.add_argument("--since", type=int, default=None, help="override the cursor")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("search", help="search stored messages")
    s.add_argument("query")
    s.add_argument("--chat", default=None)
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--include-deleted", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("events", help="show the raw event log for one message")
    s.add_argument("chat")
    s.add_argument("message_id", type=int)
    s.set_defaults(func=cmd_events)

    s = sub.add_parser("stats", help="counts per table")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("mcp", help="serve the archive over MCP (read-only)")
    s.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(func=cmd_mcp)

    s = sub.add_parser("init-db", help="create tables")
    s.set_defaults(func=cmd_init_db)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
