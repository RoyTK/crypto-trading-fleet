"""Halt state machine.

Two scopes: per-bot, fleet-wide.
Halts are persisted (halts table). Active halts are checked by the bot loop and
the executor before any order submission.

Resume rules:
- Per-bot halts: manual resume (operator) OR bot-specific auto-resume condition
- Fleet -25% kill: auto-resume after 48h ONLY IF BTC moved <5% in prior 24h
"""
from datetime import datetime, timedelta, timezone
from typing import Optional, Any
from sqlalchemy import select
from framework.db import session_scope
from framework.models import Halt, BotState
from framework.audit import write_audit


def halt_bot(
    bot_id: str,
    halt_type: str,
    reason: str,
    *,
    severity: str = "p1",
    metadata: Optional[dict[str, Any]] = None,
    halted_until: Optional[datetime] = None,
) -> int:
    """Halt a single bot. Returns halt id."""
    with session_scope() as s:
        h = Halt(
            scope="bot",
            bot_id=bot_id,
            halt_type=halt_type,
            severity=severity,
            reason=reason,
            metadata_json=metadata,
        )
        s.add(h)

        bot = s.get(BotState, bot_id)
        if bot is not None:
            bot.state = "halted"
            bot.halted_until = halted_until
            bot.halt_reason = reason

        s.flush()
        write_audit(
            "bot_halt",
            bot_id=bot_id,
            payload={"halt_id": h.id, "type": halt_type, "severity": severity},
            note=reason,
        )
        return h.id


def halt_fleet(
    halt_type: str,
    reason: str,
    *,
    severity: str = "p0",
    metadata: Optional[dict[str, Any]] = None,
) -> int:
    """Halt the entire fleet. Returns halt id."""
    with session_scope() as s:
        h = Halt(
            scope="fleet",
            bot_id=None,
            halt_type=halt_type,
            severity=severity,
            reason=reason,
            metadata_json=metadata,
        )
        s.add(h)
        s.flush()
        write_audit(
            "fleet_halt",
            payload={"halt_id": h.id, "type": halt_type, "severity": severity},
            note=reason,
        )
        return h.id


def resume_halt(halt_id: int, resumed_by: str = "manual:roy") -> None:
    with session_scope() as s:
        h = s.get(Halt, halt_id)
        if h is None:
            raise ValueError(f"halt {halt_id} not found")
        if h.resumed_at is not None:
            return
        h.resumed_at = datetime.now(timezone.utc)
        h.resumed_by = resumed_by

        if h.scope == "bot" and h.bot_id is not None:
            bot = s.get(BotState, h.bot_id)
            if bot is not None and bot.state == "halted":
                bot.state = "paper"
                bot.halted_until = None
                bot.halt_reason = None

        write_audit(
            "halt_resumed",
            bot_id=h.bot_id,
            actor=resumed_by,
            payload={"halt_id": halt_id, "type": h.halt_type},
        )


def is_bot_halted(bot_id: str) -> bool:
    with session_scope() as s:
        bot = s.get(BotState, bot_id)
        if bot is None:
            return False
        if bot.state in ("halted", "killed"):
            if bot.halted_until is not None and bot.halted_until <= datetime.now(timezone.utc):
                return False  # halt window expired; bot loop should handle resume
            return True
        return False


def is_fleet_halted() -> bool:
    with session_scope() as s:
        active = s.execute(
            select(Halt).where(Halt.scope == "fleet", Halt.resumed_at.is_(None))
        ).scalar_one_or_none()
        return active is not None


def active_fleet_halt() -> Optional[Halt]:
    with session_scope() as s:
        return s.execute(
            select(Halt).where(Halt.scope == "fleet", Halt.resumed_at.is_(None))
        ).scalar_one_or_none()


def auto_resume_eligible(halt: Halt, *, btc_24h_change_pct: float, auto_resume_hours: int, btc_stability_pct: float) -> bool:
    """Fleet -25% kill auto-resume gate."""
    if halt.scope != "fleet" or halt.halt_type != "dd_total":
        return False
    age = datetime.now(timezone.utc) - halt.halted_at
    if age < timedelta(hours=auto_resume_hours):
        return False
    if abs(btc_24h_change_pct) >= btc_stability_pct:
        return False
    return True
