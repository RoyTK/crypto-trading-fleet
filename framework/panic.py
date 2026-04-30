"""/panic command implementation.

Sequence:
1. Halt all bots (state machine)
2. Cancel all open orders across all venues (handed to per-bot executors via Redis pub/sub; Phase 0 stub)
3. Close all open positions market (handed to per-bot executors; Phase 0 stub)
4. Emit fleet halt + P0 alert
5. Return confirmation summary

Phase 0: only the halt + audit + confirmation pieces are wired. Per-venue
order cancellation and position closure activate in each bot's build phase.
"""
from datetime import datetime, timezone
from typing import Any
import json
import redis

from framework.config import get_settings
from framework.db import session_scope
from framework.models import BotState, Trade
from framework.audit import write_audit
from framework.halt_state import halt_fleet


PANIC_CHANNEL = "panic:execute"


def trigger_panic(actor: str) -> dict[str, Any]:
    """
    Run the panic sequence. `actor` is e.g. 'discord:roy', 'telegram:roy', 'manual'.
    Returns confirmation summary.
    """
    started_at = datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "started_at": started_at.isoformat(),
        "actor": actor,
        "bots_halted": [],
        "open_trades": 0,
        "panic_dispatched_to": [],
    }

    write_audit("panic_invoked", actor=actor, note="/panic command received")

    halt_fleet(
        halt_type="panic",
        reason=f"/panic invoked by {actor}",
        severity="p0",
        metadata={"actor": actor},
    )

    with session_scope() as s:
        bots = s.query(BotState).all()
        for b in bots:
            if b.state not in ("halted", "killed"):
                b.state = "halted"
                b.halt_reason = f"/panic by {actor}"
                summary["bots_halted"].append(b.bot_id)

        open_trades = s.query(Trade).filter(Trade.fill_status == "open").all()
        summary["open_trades"] = len(open_trades)

    settings = get_settings()
    try:
        r = redis.Redis.from_url(settings.redis_url)
        payload = json.dumps({"actor": actor, "started_at": started_at.isoformat()})
        for bot_id in ("structure", "copy", "event", "sniper"):
            r.publish(f"{PANIC_CHANNEL}:{bot_id}", payload)
            summary["panic_dispatched_to"].append(bot_id)
    except Exception as e:
        summary["redis_dispatch_error"] = str(e)

    summary["completed_at"] = datetime.now(timezone.utc).isoformat()
    write_audit("panic_completed", actor=actor, payload=summary)
    return summary


def format_summary(summary: dict[str, Any]) -> str:
    lines = [
        "PANIC EXECUTED",
        f"actor: {summary.get('actor')}",
        f"started: {summary.get('started_at')}",
        f"completed: {summary.get('completed_at')}",
        f"bots halted: {', '.join(summary.get('bots_halted') or []) or '(none)'}",
        f"open trades at trigger: {summary.get('open_trades')}",
        f"panic dispatched to: {', '.join(summary.get('panic_dispatched_to') or []) or '(none)'}",
    ]
    if summary.get("redis_dispatch_error"):
        lines.append(f"redis error: {summary['redis_dispatch_error']}")
    return "\n".join(lines)
