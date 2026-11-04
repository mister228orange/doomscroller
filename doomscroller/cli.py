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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from . import __version__
from .config import load_scorer_config, load_settings
from .db import init_db, session_scope
from .models import ChannelEvent, ChannelState, Message, PostScore, Subscription
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


# ── anchors: locate the rubric's reference texts, and calibrate against them ──

def _anchor_row(check) -> dict[str, Any]:
    return check.as_dict()


def cmd_anchors(args) -> int:
    init_db()
    from .scoring.prompts import load_modules
    from .scoring.rubric import parse_anchors
    from .scoring.calibrate import locate

    modules = load_modules()
    anchors = parse_anchors(modules.anchor_text)
    with session_scope() as session:
        checks = locate(session, anchors)

    if args.calibrate:
        return _run_async(_calibrate_anchors(args, modules, anchors))

    if args.json:
        print(json.dumps([_anchor_row(c) for c in checks], ensure_ascii=False, indent=2, default=str))
        return 0

    found = sum(1 for c in checks if c.archived)
    print(f"rubric {modules.version}: {len(checks)} anchors, {found} с текстом в архиве")
    print(f"{'ID':<4} {'genre':<18} {'primary':<6} {'КПД':>5} {'chars':>6}  locator")
    for check in checks:
        locator = f"{check.channel}#{check.message_id}" if check.channel else "—"
        chars = str(check.chars) if check.archived else "—"
        kpd = f"{check.reference_kpd:.2f}" if check.reference_kpd is not None else "—"
        print(f"{check.anchor_id:<4} {check.genre:<18} {check.primary:<6} {kpd:>5} {chars:>6}  {locator}")
        if check.note:
            print(f"     └ {check.note}")
    if found and not args.json:
        print("\nпереоценить по этим текстам: doomscroller anchors --calibrate")
    return 0


async def _calibrate_anchors(args, modules, anchors) -> int:
    from .scoring.calibrate import axis_summary, calibrate
    from .scoring.llm import ChatClient, LLMError

    config = load_scorer_config()
    client = ChatClient(
        base_url=config.base_url,
        model=config.model,
        api_key=config.api_key,
        temperature=config.temperature,
        timeout=config.timeout,
        retries=config.retries,
        json_mode=config.json_mode,
        max_tokens=config.max_tokens or None,
    )
    try:
        async with client:
            checks = await calibrate(
                anchors,
                session_factory=session_scope,
                llm=client,
                modules=modules,
                max_chars=config.max_chars,
                only=args.anchor,
            )
    except LLMError as exc:
        print(f"calibration needs a model backend: {exc}", file=sys.stderr)
        return 2

    summary = axis_summary(checks)
    if args.json:
        print(json.dumps(
            {
                "rubric": modules.version,
                "model": config.model,
                "checks": [c.as_dict() for c in checks],
                "by_axis": summary,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ))
        return 0

    print(f"калибровка: {config.model}, рубрика {modules.version}\n")
    scored = [c for c in checks if c.model]
    for check in checks:
        if not check.model:
            reason = check.error or check.note or "не оценён"
            print(f"{check.anchor_id:<4} —        {reason}")
            continue
        deltas = "  ".join(
            f"{axis} {check.reference.get(axis, 0):.1f}→{check.model.get(axis, 0):.1f}"
            f" ({check.deltas.get(axis, 0):+.1f})"
            for axis in ("I", "V", "U", "P", "S", "D")
        )
        print(f"{check.anchor_id:<4} {check.verdict:<6} КПД {check.reference_kpd:.2f}→{check.model_kpd:.2f}  {deltas}")
    if scored:
        print("\nпо осям (средний сдвиг модели против якорей):")
        for axis in ("I", "V", "U", "P", "S", "D"):
            if axis in summary:
                row = summary[axis]
                print(f"  {axis}: {row['mean']:+.2f} (|сдвиг| {row['mean_abs']:.2f}, n={row['n']})")
        drift = [c.anchor_id for c in checks if c.verdict == "drift"]
        print(f"\nв допуске ±{0.5}: {len(scored) - len(drift)} из {len(scored)}"
              + (f"; расходятся: {', '.join(drift)}" if drift else ""))
    return 0


# ── scoring commands ──────────────────────────────────────────────────────────

def _score_row(score: PostScore) -> dict[str, Any]:
    return {
        "chat_id": score.chat_id,
        "message_id": score.message_id,
        "status": score.status,
        "date": score.message_date.isoformat() if score.message_date else None,
        "title": score.title,
        "genre": score.genre,
        "primary": score.primary,
        "secondary": score.secondary,
        "thesis": score.main_thesis,
        "scores": {
            axis: getattr(score, f"score_{axis.lower()}")
            for axis in ("I", "V", "U", "P", "S", "D")
        },
        "kpd": score.kpd,
        "eff": score.eff,
        "density_type": score.density_type,
        "flags": score.flags,
        "deviation": score.deviation,
        "trend_position": score.trend_position,
        "shift_candidate": score.shift_candidate,
        "confidence": score.confidence,
        "nearest_anchors": score.nearest_anchors,
        "selfcheck": score.selfcheck,
        "attempts": score.attempts,
        "detail": score.detail,
        "model": score.model,
        "prompt_version": score.prompt_version,
        "scored_at": score.scored_at.isoformat() if score.scored_at else None,
    }


def _fmt_axes(score: PostScore) -> str:
    parts = []
    for axis in ("I", "V", "U", "P", "S", "D"):
        value = getattr(score, f"score_{axis.lower()}")
        parts.append(f"{axis} {value:.1f}" if value is not None else f"{axis}  — ")
    return " ".join(parts)


def cmd_scores(args) -> int:
    init_db()
    with session_scope() as session:
        chat_id = _resolve_chat_id(session, args.chat)
        stmt = select(PostScore).where(PostScore.chat_id == chat_id)
        if not args.all:
            stmt = stmt.where(PostScore.status == "scored")
        stmt = stmt.order_by(PostScore.message_id.desc()).limit(args.limit)
        rows = list(session.exec(stmt).all())
        if args.json:
            print(json.dumps([_score_row(r) for r in rows], ensure_ascii=False, indent=2, default=str))
            return 0
        if not rows:
            print("(no scores yet — run `doomscroller score`)")
            return 0
        print(f"{'msg':>8}  {'primary':<6} {'I':>4} {'V':>4} {'U':>4} {'P':>4} {'S':>4} {'D':>4} "
              f"{'КПД':>5} {'Эфф':>5}  thesis")
        for row in rows:
            kpd = f"{row.kpd:.2f}" if row.kpd is not None else "  — "
            eff = f"{row.eff:.2f}" if row.eff is not None else "   — "
            mark = "*" if row.trend_position == "above" else " " if row.trend_position == "at" else "v"
            thesis = (row.main_thesis or "").replace("\n", " ")[:60]
            print(
                f"{row.message_id:>8}  {(row.primary or '—'):<6} "
                f"{(row.score_i or 0):>4.1f} {(row.score_v or 0):>4.1f} {(row.score_u or 0):>4.1f} "
                f"{(row.score_p or 0):>4.1f} {(row.score_s or 0):>4.1f} {(row.score_d or 0):>4.1f} "
                f"{kpd:>5} {eff:>5} {mark} {thesis}"
            )
    return 0


def cmd_channels(args) -> int:
    init_db()
    with session_scope() as session:
        states = list(
            session.exec(select(ChannelState).order_by(ChannelState.updated_at.desc())).all()
        )
        from .scoring.state import pending_counts, state_snapshot

        pending = pending_counts(session)
        if args.json:
            print(json.dumps(
                [
                    state_snapshot(session, s, pending=pending.get(int(s.chat_id), 0))
                    for s in states
                ],
                ensure_ascii=False,
                indent=2,
                default=str,
            ))
            return 0
        if not states:
            print("(no channel states yet — run `doomscroller score`)")
            return 0
        print(f"{'channel':<28} {'scored':>6} {'skip':>4} {'fail':>4} {'pend':>4} {'КПД':>5} {'max':>5}  summary")
        for state in states:
            kpd = f"{state.kpd_mean:.2f}" if state.kpd_mean is not None else "  — "
            top = f"{state.kpd_max:.2f}" if state.kpd_max is not None else "  — "
            summary = (state.summary or state.profile_description or "").replace("\n", " ")[:50]
            print(
                f"{(state.chat_ref or state.chat_id):<28} {state.total_scored:>6} "
                f"{state.total_skipped:>4} {state.total_failed:>4} "
                f"{pending.get(int(state.chat_id), 0):>4} {kpd:>5} {top:>5}  {summary}"
            )
    return 0


def cmd_channel_state(args) -> int:
    init_db()
    with session_scope() as session:
        chat_id = _resolve_chat_id(session, args.chat)
        state = session.exec(
            select(ChannelState).where(ChannelState.chat_id == chat_id)
        ).first()
        events = list(
            session.exec(
                select(ChannelEvent)
                .where(ChannelEvent.chat_id == chat_id)
                .order_by(ChannelEvent.id.desc())
                .limit(args.events)
            ).all()
        )
        if state is None:
            print("(no state yet for that channel — run `doomscroller score`)")
            return 0
        from .scoring.state import pending_counts, state_snapshot

        payload = state_snapshot(
            session,
            state,
            context_anchors=5,
            events=args.events,
            pending=pending_counts(session, [int(state.chat_id)]).get(int(state.chat_id), 0),
        )
        payload["events"] = [
            {
                "date": e.created_at.isoformat() if e.created_at else None,
                "kind": e.kind,
                "message_id": e.message_id,
                "note": e.note,
            }
            for e in reversed(events)
        ]
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"channel    : {payload['chat_ref']} (id {payload['chat_id']})")
    print(f"profile    : {payload['profile_description'] or '—'}")
    print(f"summary    : {payload['summary'] or '—'}")
    if payload["drift_note"]:
        print(f"drift      : {payload['drift_note']}")
    print(f"posts      : scored {payload['total_scored']}, skipped {payload['total_skipped']}, "
          f"failed {payload['total_failed']}, pending {payload.get('pending') or 0}")
    if payload["means"]:
        means = " ".join(f"{a} {payload['means'][a]:.2f}" for a in ("I", "V", "U", "P", "S", "D")
                         if a in payload["means"])
        print(f"trend      : {means}")
        print(f"KPD mean   : {payload['kpd_mean']:.2f}  max {payload['kpd_max'] or 0:.2f}")
    print(f"deviation  : streak {payload['deviation_streak']}")
    if payload["tag_distribution"]:
        print("tags       : " + ", ".join(f"{k} {v}" for k, v in
                                        sorted(payload["tag_distribution"].items(), key=lambda kv: -kv[1])))
    for anchor in payload["local_anchors"]:
        scores = anchor.get("scores") or {}
        axes = " ".join(f"{a} {scores.get(a, 0):.1f}" for a in ("I", "V", "U", "P", "S", "D"))
        print(f"anchor     : {anchor['id']} «{anchor['title']}» [{anchor['primary']}] {axes} "
              f"→ КПД {anchor['kpd']:.2f}")
    for event in payload["events"]:
        print(f"event      : {event['date']} {event['kind']}: {event['note']}")
    return 0


async def _score_doctor(config, args) -> int:
    """`score --check` / `score --list-models`: validate the deployment before spending.

    Three independent things can be wrong — the rubric directory, the database, the paid
    endpoint — so they are checked in that order and reported together. No post is scored.
    """
    from .models import ChannelState, PostScore
    from .scoring.llm import ChatClient, LLMError
    from .scoring.prompts import load_modules, rubric_dir_candidates
    from .scoring.state import pending_counts

    ok = True
    print("scorer")
    print(f"  base_url  : {config.base_url}")
    print(f"  model     : {config.model}")
    print(f"  api_key   : {'set' if config.api_key else 'MISSING (SCORER_API_KEY / NANOGPT_API_KEY)'}")
    print(f"  rubric_dir: {config.rubric_dir}")

    try:
        modules = load_modules(config.rubric_dir)
        anchors = modules.anchor_text.count("## A")
        print(f"  rubric    : version {modules.version}, {len(modules.texts)} modules, {anchors} anchors")
    except Exception as exc:  # noqa: BLE001 - this command exists to report failures
        ok = False
        print(f"  rubric    : FAILED — {exc}")
        for candidate in rubric_dir_candidates():
            print(f"              tried {candidate} (exists: {candidate.is_dir()})")

    try:
        init_db()
        with session_scope() as session:
            channels = list(session.exec(select(ChannelState)).all())
            pending = pending_counts(session)
            scored = len(
                session.exec(select(PostScore).where(PostScore.status == "scored")).all()
            )
        print(
            f"  database  : ok — {len(channels)} channel(s), {scored} scored, "
            f"{sum(pending.values())} pending"
        )
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  database  : FAILED — {str(exc).splitlines()[0]}")

    client = ChatClient(
        base_url=config.base_url,
        model=config.model,
        api_key=config.api_key,
        temperature=config.temperature,
        timeout=min(config.timeout, 120.0),
        retries=0,  # a doctor should report the first failure, not wait through retries
        json_mode=config.json_mode,
        max_tokens=config.max_tokens or None,
    )

    # The model list is public on NanoGPT, so it works before a key is configured — which
    # is exactly when you need it to pick a model id.
    if args.list_models is not None:
        try:
            async with client:
                ids = await client.list_models(args.list_models or None)
        except LLMError as exc:
            print(f"  models    : FAILED — {exc}")
            return 2
        print(f"  models    : {len(ids)} match(es) for {args.list_models!r}")
        for model_id in ids:
            mark = "  <- current" if model_id == config.model else ""
            print(f"              {model_id}{mark}")
        return 0 if ok else 2

    if not config.api_key:
        # Not an error in the rubric or the database — an unfinished deployment.
        print("  backend   : not checked (no key) — the scorer cannot run until this is set")
        return 2

    try:
        async with client:
            response = await client.ping()
    except LLMError as exc:
        print(f"  backend   : FAILED — {exc}")
        return 2

    same = response.model == config.model
    print(f"  backend   : ok — answered as {response.model}" + ("" if same else "  (differs from the configured id)"))
    print(
        f"  latency   : {response.latency_ms} ms, tokens "
        f"{response.prompt_tokens} -> {response.completion_tokens}"
    )
    print(f"  reply     : {response.text.strip().splitlines()[0][:80]}")
    return 0 if ok else 2


async def _score(args) -> int:
    from .scoring.agent import run_agent, run_loop
    from .scoring.llm import LLMError

    config = load_scorer_config()
    if args.genre:
        config.genre = args.genre
    if args.check or args.list_models is not None:
        return await _score_doctor(config, args)
    summary_every = 0 if args.no_summary else args.summary_every

    if args.loop:
        await run_loop(
            config,
            interval=args.interval,
            only_chat=args.chat,
            limit=args.limit,
            force=args.force,
            dry_run=args.dry_run,
            summary_every=summary_every,
        )
        return 0

    try:
        report = await run_agent(
            config,
            only_chat=args.chat,
            limit=args.limit,
            force=args.force,
            dry_run=args.dry_run,
            summary_every=summary_every,
        )
    except LLMError as exc:
        # Misconfigured model endpoint: a message, not a traceback. The scorer container
        # is a different deployment from the poller, so this is a common first-run state.
        print(f"scoring is not configured: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError as exc:
        print(f"database is not reachable: {str(exc).splitlines()[0]}", file=sys.stderr)
        return 2

    if args.dry_run:
        prompts = report.prompts
        if args.json:
            print(json.dumps(prompts, ensure_ascii=False, indent=2, default=str))
            return 0
        if not prompts:
            print("(nothing to score — no unread posts)", file=sys.stderr)
            return 0
        for prompt in prompts:
            print(f"=== {prompt['chat']} / message {prompt['message_id']} "
                  f"(prompt version {prompt['prompt_version']}) ===")
            print("--- system ---")
            print(prompt["system"])
            print("--- user ---")
            print(prompt["user"])
            print()
        print(f"({len(prompts)} prompt(s), nothing was scored)", file=sys.stderr)
        return 0

    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2, default=str))
    else:
        for channel in report.channels:
            line = (f"{channel.chat_ref}: scored {channel.scored}, skipped {channel.skipped}, "
                    f"failed {channel.failed}, memory {channel.summaries}")
            if channel.error:
                line += f" — {channel.error}"
            print(line)
        if report.aborted:
            print(f"aborted: {report.aborted}", file=sys.stderr)
        print(f"total: scored {report.scored}, skipped {report.skipped}, failed {report.failed}")
        if report.prompt_tokens or report.completion_tokens:
            print(
                f"tokens: {report.prompt_tokens:,} in / {report.completion_tokens:,} out"
                .replace(",", " ")
            )
    return 0 if report.failed == 0 else 1


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


async def _chats(args) -> int:
    """List the Telegram chats visible to the account (live, not the archive)."""
    from .telegram_client import build_client, ensure_connected
    from .telegram_ops import list_chats

    settings = load_settings()
    client = build_client(settings, args.session_path)
    await ensure_connected(client, settings)
    try:
        rows = await list_chats(client, limit=args.limit)
    finally:
        await client.disconnect()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    else:
        for r in rows:
            user = f"@{r['username']}" if r["username"] else ""
            print(f"{str(r['chat_id']):>16}  {r['kind']:<11} {user:<22} {r['title']}")
    return 0


async def _send(args) -> int:
    """Send a text message. Refused unless the chat is in SEND_ALLOWLIST."""
    from .telegram_client import build_client, ensure_connected
    from .telegram_ops import SendNotAllowed, send_text

    settings = load_settings()
    client = build_client(settings, args.session_path)
    await ensure_connected(client, settings)
    try:
        result = await send_text(client, settings, args.chat, args.text)
    except SendNotAllowed as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    finally:
        await client.disconnect()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


async def _export(args) -> int:
    """Dump the last N messages of chats to a JSON file."""
    from .telegram_client import build_client, ensure_connected
    from .telegram_ops import fetch_history, history_for_entity

    settings = load_settings()
    client = build_client(settings, args.session_path)
    await ensure_connected(client, settings)

    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "limit_per_chat": args.limit,
        "chats": [],
    }
    try:
        if args.chats:
            refs = [r.strip() for r in args.chats.split(",") if r.strip()]
            print(f"exporting {len(refs)} specified chat(s)", file=sys.stderr)
            for ref in refs:
                try:
                    data = await fetch_history(client, ref, limit=args.limit)
                except Exception as exc:  # noqa: BLE001
                    data = {"chat": ref, "error": f"{type(exc).__name__}: {exc}", "message_count": 0}
                payload["chats"].append(data)
                print(f"  {data.get('title') or data.get('chat')}: {data.get('message_count')}", file=sys.stderr)
                await asyncio.sleep(args.sleep)
        else:
            dialogs = await client.get_dialogs(limit=args.max_chats)
            print(f"exporting {len(dialogs)} dialog(s), last {args.limit} messages each", file=sys.stderr)
            for dialog in dialogs:
                try:
                    data = await history_for_entity(
                        client, dialog.entity, limit=args.limit, label=dialog.name
                    )
                except Exception as exc:  # noqa: BLE001
                    data = {"chat": dialog.name, "error": f"{type(exc).__name__}: {exc}", "message_count": 0}
                payload["chats"].append(data)
                print(f"  {data.get('title') or data.get('chat')}: {data.get('message_count')}", file=sys.stderr)
                await asyncio.sleep(args.sleep)
    finally:
        await client.disconnect()

    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    total = sum(c.get("message_count", 0) for c in payload["chats"])
    print(json.dumps({
        "out": str(out),
        "chats": len(payload["chats"]),
        "messages": total,
        "failed_chats": sum(1 for c in payload["chats"] if c.get("error")),
        "bytes": out.stat().st_size,
    }, ensure_ascii=False, indent=2))
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

    s = sub.add_parser("chats", help="list Telegram chats visible to the account (live)")
    s.add_argument("--limit", type=int, default=200)
    s.add_argument("--json", action="store_true")
    s.add_argument("--session-path", default=None, help="override the Telethon session file")
    s.set_defaults(func=lambda a: _run_async(_chats(a)))

    s = sub.add_parser("send", help="send a text message (requires SEND_ALLOWLIST)")
    s.add_argument("chat")
    s.add_argument("text")
    s.add_argument("--session-path", default=None)
    s.set_defaults(func=lambda a: _run_async(_send(a)))

    s = sub.add_parser("export", help="dump last N messages of chats to JSON")
    s.add_argument("--out", default="~/Desktop/telegram_export.json")
    s.add_argument("--limit", type=int, default=100, help="messages per chat")
    s.add_argument("--chats", default=None, help="comma-separated refs; default: every dialog")
    s.add_argument("--max-chats", type=int, default=200)
    s.add_argument("--sleep", type=float, default=0.25, help="seconds between chats (be gentle)")
    s.add_argument("--session-path", default=None)
    s.set_defaults(func=lambda a: _run_async(_export(a)))

    s = sub.add_parser("mcp", help="serve the archive, scores and (opt-in) live Telegram over MCP")
    s.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(func=cmd_mcp)

    s = sub.add_parser("score", help="score unread posts one by one (the agent)")
    s.add_argument("--chat", default=None, help="only this chat")
    s.add_argument("--limit", type=int, default=None, help="max posts this run")
    s.add_argument("--force", action="store_true", help="re-score posts that are already scored")
    s.add_argument("--dry-run", action="store_true", help="print the assembled prompt, call nothing")
    s.add_argument("--loop", action="store_true", help="keep ticking on an interval (scheduler)")
    s.add_argument("--interval", type=int, default=None, help="seconds between ticks with --loop")
    s.add_argument("--summary-every", type=int, default=None, help="refresh channel memory every N posts")
    s.add_argument("--no-summary", action="store_true", help="never refresh the channel memory")
    s.add_argument("--genre", default=None, help="fix the genre for every post of this run")
    s.add_argument(
        "--check",
        action="store_true",
        help="verify rubric, database and model endpoint, then exit (nothing is scored)",
    )
    s.add_argument(
        "--list-models",
        nargs="?",
        const="deepseek",
        default=None,
        metavar="SUBSTRING",
        help="list model ids offered by the endpoint (default filter: deepseek), then exit",
    )
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=lambda a: _run_async(_score(a)))

    s = sub.add_parser("anchors", help="rubric anchors: locate their texts in the archive, calibrate")
    s.add_argument("--calibrate", action="store_true",
                   help="score every archived anchor and diff it against the reference scores")
    s.add_argument("--anchor", action="append", default=None,
                   help="only these anchors (repeatable), e.g. --anchor A3")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_anchors)

    s = sub.add_parser("scores", help="read stored rubric scores for a chat")
    s.add_argument("chat")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--all", action="store_true", help="include skipped and failed rows")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_scores)

    s = sub.add_parser("channels", help="channel trajectories (trend, counts, summary)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_channels)

    s = sub.add_parser("channel-state", help="full trajectory state of one channel")
    s.add_argument("chat")
    s.add_argument("--events", type=int, default=10)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_channel_state)

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
