"""Daily 7am check-in report.

Renders one report covering all bots and the fleet. Sent to Discord + email.
Phase 0 emits the report cleanly against an empty fleet (zero rows everywhere).
"""
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Any
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from framework.db import session_scope
from framework.models import BotState, Trade, Halt, Score, Signal


@dataclass
class BotSummary:
    bot_id: str
    state: str
    paper_clock_day: int | None
    net_total_usd: float  # lifetime closed PnL — profitability is the bottom line now
    trades_24h: int
    pnl_24h_usd: float
    pnl_24h_pct: float | None
    open_positions: int
    halts_24h: int
    last_score_at: str | None


@dataclass
class FleetSummary:
    generated_at: str
    window_hours: int
    bots: list[BotSummary]
    fleet_signals_24h: int
    fleet_trades_24h: int
    fleet_halts_24h: int
    deployed_capital_usd: float


def _bot_summary(s: Session, bot: BotState, since: datetime) -> BotSummary:
    trades_24h = s.execute(
        select(func.count(Trade.id)).where(
            Trade.bot_id == bot.bot_id,
            Trade.created_at >= since,
        )
    ).scalar() or 0

    pnl_24h_usd = s.execute(
        select(func.coalesce(func.sum(Trade.pnl_usd), 0.0)).where(
            Trade.bot_id == bot.bot_id,
            Trade.exit_at >= since,
            Trade.fill_status == "closed",
        )
    ).scalar() or 0.0

    open_positions = s.execute(
        select(func.count(Trade.id)).where(
            Trade.bot_id == bot.bot_id,
            Trade.fill_status == "open",
        )
    ).scalar() or 0

    halts_24h = s.execute(
        select(func.count(Halt.id)).where(
            Halt.bot_id == bot.bot_id,
            Halt.halted_at >= since,
        )
    ).scalar() or 0

    pnl_24h_pct = None  # require equity baseline; skip in Phase 0

    net_total_usd = s.execute(
        select(func.coalesce(func.sum(Trade.pnl_usd), 0.0)).where(
            Trade.bot_id == bot.bot_id,
            Trade.fill_status == "closed",
        )
    ).scalar() or 0.0

    # bot_state.paper_clock_day is a dead column — never written by the
    # scoring engine even though it's in the schema. Compute live from
    # paper_clock_started_at (matches what every Grafana panel does).
    if bot.paper_clock_started_at is not None:
        elapsed_s = (datetime.now(timezone.utc) - bot.paper_clock_started_at).total_seconds()
        paper_clock_day = max(0, int(elapsed_s // 86400))
    else:
        paper_clock_day = None

    return BotSummary(
        bot_id=bot.bot_id,
        state=bot.state,
        paper_clock_day=paper_clock_day,
        net_total_usd=float(net_total_usd),
        trades_24h=trades_24h,
        pnl_24h_usd=float(pnl_24h_usd),
        pnl_24h_pct=pnl_24h_pct,
        open_positions=open_positions,
        halts_24h=halts_24h,
        last_score_at=bot.last_score_at.isoformat() if bot.last_score_at else None,
    )


# Deprioritized/inactive states excluded from the daily report. EVENT and
# SNIPER were deprioritized 2026-05-05 (see decision log) — they still sit
# in bot_state as 'initializing' but shouldn't pollute the daily check-in.
_INACTIVE_STATES = {"initializing", "killed"}

# Excluded from the generic bot_state iteration:
#  - structure: DECOMMISSIONED 2026-06-25 (stale bot_state row shouldn't render).
#  - copy + copy_conviction + copy_teamfollow: COPY is one bot_id running THREE
#    isolated strategies (own bankroll/halt/metrics). Surfaced explicitly as three
#    'copy:<strategy>' rows below (exact strategy tag → retired '*_pre_reset' trades
#    are excluded, which the old lumped 'copy' row wrongly counted).
_REPORT_EXCLUDE_BOT_IDS = {"structure", "copy", "copy_conviction", "copy_teamfollow"}

# (display_label, strategy tag, halt_id) for the COPY sub-strategies.
_COPY_STRATEGIES = [
    ("copy:cluster", "cluster", "copy"),
    ("copy:conviction", "conviction", "copy_conviction"),
    ("copy:teamfollow", "teamfollow", "copy_teamfollow"),
]


def _copy_strategy_summary(
    s: Session, label: str, strategy: str, halt_id: str,
    copy_bot: "BotState | None", since: datetime,
) -> BotSummary:
    """Per-strategy COPY row: metrics scoped by the exact strategy tag + own halt_id."""
    from sqlalchemy import text

    def _strat():  # fresh clause per query (avoid bound-param reuse)
        return text("sim_metadata->>'strategy' = :strat").bindparams(strat=strategy)

    trades_24h = s.execute(
        select(func.count(Trade.id)).where(
            Trade.bot_id == "copy", Trade.created_at >= since, _strat())
    ).scalar() or 0
    pnl_24h = s.execute(
        select(func.coalesce(func.sum(Trade.pnl_usd), 0.0)).where(
            Trade.bot_id == "copy", Trade.exit_at >= since,
            Trade.fill_status == "closed", _strat())
    ).scalar() or 0.0
    net_total = s.execute(
        select(func.coalesce(func.sum(Trade.pnl_usd), 0.0)).where(
            Trade.bot_id == "copy", Trade.fill_status == "closed", _strat())
    ).scalar() or 0.0
    open_positions = s.execute(
        select(func.count(Trade.id)).where(
            Trade.bot_id == "copy", Trade.fill_status == "open", _strat())
    ).scalar() or 0
    halts_24h = s.execute(
        select(func.count(Halt.id)).where(
            Halt.bot_id == halt_id, Halt.halted_at >= since)
    ).scalar() or 0
    active_halt = s.execute(
        select(func.count(Halt.id)).where(
            Halt.bot_id == halt_id, Halt.resumed_at.is_(None))
    ).scalar() or 0

    state = "halted" if active_halt else (copy_bot.state if copy_bot else "paper")
    if copy_bot is not None and copy_bot.paper_clock_started_at is not None:
        elapsed_s = (datetime.now(timezone.utc) - copy_bot.paper_clock_started_at).total_seconds()
        clock = max(0, int(elapsed_s // 86400))
    else:
        clock = None
    return BotSummary(
        bot_id=label, state=state, paper_clock_day=clock, net_total_usd=float(net_total),
        trades_24h=trades_24h, pnl_24h_usd=float(pnl_24h), pnl_24h_pct=None,
        open_positions=open_positions, halts_24h=halts_24h,
        last_score_at=copy_bot.last_score_at.isoformat() if (copy_bot and copy_bot.last_score_at) else None,
    )


def build_summary(window_hours: int = 24, deployed_capital_usd: float = 0.0) -> FleetSummary:
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    with session_scope() as s:
        # COPY runs three isolated strategies under bot_id='copy' — surface each.
        copy_bot = s.query(BotState).filter(BotState.bot_id == "copy").one_or_none()
        copy_summaries = [
            _copy_strategy_summary(s, label, strat, halt_id, copy_bot, since)
            for (label, strat, halt_id) in _COPY_STRATEGIES
        ]
        # Any OTHER real bots (none today — fleet is COPY-only, structure decommissioned).
        other_bots = list(
            s.query(BotState)
            .filter(~BotState.state.in_(_INACTIVE_STATES))
            .filter(~BotState.bot_id.in_(_REPORT_EXCLUDE_BOT_IDS))
            .order_by(BotState.bot_id)
            .all()
        )
        bot_summaries = copy_summaries + [_bot_summary(s, b, since) for b in other_bots]
        fleet_signals = s.execute(
            select(func.count(Signal.id)).where(Signal.created_at >= since)
        ).scalar() or 0
        fleet_trades = s.execute(
            select(func.count(Trade.id)).where(Trade.created_at >= since)
        ).scalar() or 0
        fleet_halts = s.execute(
            select(func.count(Halt.id)).where(Halt.halted_at >= since)
        ).scalar() or 0

    return FleetSummary(
        generated_at=datetime.now(timezone.utc).isoformat(),
        window_hours=window_hours,
        bots=bot_summaries,
        fleet_signals_24h=fleet_signals,
        fleet_trades_24h=fleet_trades,
        fleet_halts_24h=fleet_halts,
        deployed_capital_usd=deployed_capital_usd,
    )


def render_text(summary: FleetSummary) -> str:
    lines = [
        f"Daily check-in — {summary.generated_at}",
        f"Window: {summary.window_hours}h. Deployed capital: ${summary.deployed_capital_usd:,.2f}",
        "",
        "Per-bot:",
    ]
    for b in summary.bots:
        clock = f"day {b.paper_clock_day}" if b.paper_clock_day is not None else "no clock"
        lines.append(
            f"  {b.bot_id:>15} | {b.state:<14} | {clock:<10} | "
            f"net ${b.net_total_usd:>+10.2f} | trades24h {b.trades_24h:>4} | "
            f"pnl24h ${b.pnl_24h_usd:>+9.2f} | open {b.open_positions:>3} | halts24h {b.halts_24h:>2}"
        )
    lines.append("")
    lines.append(
        f"Fleet 24h: signals={summary.fleet_signals_24h} "
        f"trades={summary.fleet_trades_24h} halts={summary.fleet_halts_24h}"
    )
    return "\n".join(lines)


def render_discord_embed(summary: FleetSummary) -> dict[str, Any]:
    """Discord-friendly embed payload (used by the alerting Discord bot)."""
    rows = []
    for b in summary.bots:
        clock = b.paper_clock_day if b.paper_clock_day is not None else "—"
        rows.append(
            f"`{b.bot_id:<15}` {b.state:<14} day {clock} net ${b.net_total_usd:+.2f} "
            f"tr {b.trades_24h} pnl24h ${b.pnl_24h_usd:+.2f} open {b.open_positions} halts {b.halts_24h}"
        )
    description = "\n".join(rows) if rows else "(no bots yet)"
    return {
        "title": "Daily check-in",
        "description": description,
        "fields": [
            {"name": "Window", "value": f"{summary.window_hours}h", "inline": True},
            {"name": "Capital", "value": f"${summary.deployed_capital_usd:,.0f}", "inline": True},
            {
                "name": "Fleet 24h",
                "value": (
                    f"signals: {summary.fleet_signals_24h}\n"
                    f"trades: {summary.fleet_trades_24h}\n"
                    f"halts: {summary.fleet_halts_24h}"
                ),
                "inline": False,
            },
        ],
        "footer": {"text": f"Generated {summary.generated_at}"},
    }
