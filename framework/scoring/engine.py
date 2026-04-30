"""Scoring engine — separate process.

Reads `signals` and `trades` from Postgres (read-only), computes PromotionScore
for each bot, persists snapshots to Postgres `scores` table AND to DuckDB for
analytics queries. Bots have no read or write access to this module's outputs;
this is what enforces the bot-blind-to-scoring contract.
"""
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
import duckdb
from sqlalchemy import select, func

from framework.config import get_settings
from framework.db import session_scope
from framework.models import Trade, BotState, Score, Signal
from framework.scoring.formula import (
    ScoreInputs, compute_score, ScoreBreakdown, promotion_outcome,
)
from framework.audit import write_audit
from framework.logging_setup import get_logger

log = get_logger(__name__)

DUCKDB_PATH = Path("/app/data/duckdb/scoring.duckdb")


# --- Helpers ----------------------------------------------------------------

def _wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> float:
    """Wilson score lower bound for a binomial proportion (95%)."""
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + (z * z) / n
    centre = p + (z * z) / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + (z * z) / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def _classify_regime(closed_at: datetime) -> str:
    """Phase 0 stub: bucket trades into a single 'unknown' regime.
    Real classifier (BTC trend / vol regime / funding regime) lands in Item #6 build."""
    return "unknown"


# --- Main scoring function --------------------------------------------------

def score_bot(bot_id: str, *, mode: str = "paper") -> Optional[ScoreBreakdown]:
    """Compute PromotionScore for one bot from its closed trades."""
    with session_scope() as s:
        bot = s.get(BotState, bot_id)
        if bot is None:
            log.warning("score_unknown_bot", bot_id=bot_id)
            return None

        closed_q = (
            select(Trade)
            .where(Trade.bot_id == bot_id, Trade.mode == mode, Trade.fill_status == "closed")
            .order_by(Trade.exit_at.asc())
        )
        trades = list(s.execute(closed_q).scalars())

        if not trades:
            return None

        pnl_values = [t.pnl_pct for t in trades if t.pnl_pct is not None]
        if not pnl_values:
            return None

        # Net return — multiplicative compounding of pct trades
        equity = 1.0
        peak = 1.0
        max_dd_pct = 0.0
        for p in pnl_values:
            equity *= (1.0 + p / 100.0)
            peak = max(peak, equity)
            dd = (peak - equity) / peak * 100.0
            max_dd_pct = max(max_dd_pct, dd)
        net_return_pct = (equity - 1.0) * 100.0

        wins = sum(1 for p in pnl_values if p > 0)
        n = len(pnl_values)
        win_rate = wins / n
        win_rate_confidence = _wilson_lower_bound(wins, n)

        regimes = {}
        for t in trades:
            r = _classify_regime(t.exit_at or t.created_at)
            buf = regimes.setdefault(r, [0, 0])
            buf[0] += 1
            if (t.pnl_pct or 0) > 0:
                buf[1] += 1
        regimes_occurred = len(regimes)
        regimes_profitable = sum(1 for cnt, w in regimes.values() if w > cnt / 2)

        calib_q = """
            SELECT AVG(calibration_ratio)
            FROM calibration_records
            WHERE bot_id = :bid AND calibration_ratio IS NOT NULL
        """
        from sqlalchemy import text
        calib_ratio = s.execute(text(calib_q), {"bid": bot_id}).scalar()

    inputs = ScoreInputs(
        net_return_pct=net_return_pct,
        max_dd_pct=max_dd_pct,
        num_trades=n,
        win_rate=win_rate,
        win_rate_confidence=win_rate_confidence,
        regimes_occurred=regimes_occurred,
        regimes_profitable=regimes_profitable,
        calibration_ratio=calib_ratio,
    )
    return compute_score(inputs)


def persist_score(bot_id: str, breakdown: ScoreBreakdown) -> int:
    """Write a Score row to Postgres and append to DuckDB analytics file."""
    with session_scope() as s:
        bot = s.get(BotState, bot_id)
        clock_day = None
        if bot and bot.paper_clock_started_at:
            elapsed = datetime.now(timezone.utc) - bot.paper_clock_started_at
            clock_day = max(0, elapsed.days)

        row = Score(
            bot_id=bot_id,
            paper_clock_day=clock_day,
            return_score=breakdown.return_score,
            risk_score=breakdown.risk_score,
            confidence_score=breakdown.confidence_score,
            regime_score=breakdown.regime_score,
            calibration_score=breakdown.calibration_score,
            promotion_score=breakdown.promotion_score,
            floor_pass=breakdown.floor_pass,
            floor_failures=breakdown.floor_failures,
            metadata_json={"effective_trade_count": breakdown.effective_trade_count},
        )
        s.add(row)
        s.flush()
        score_id = row.id

        if bot is not None:
            bot.promotion_score = breakdown.promotion_score
            bot.last_score_at = datetime.now(timezone.utc)

    DUCKDB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DUCKDB_PATH))
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS scores (
                id INTEGER, bot_id VARCHAR, paper_clock_day INTEGER,
                return_score DOUBLE, risk_score DOUBLE, confidence_score DOUBLE,
                regime_score DOUBLE, calibration_score DOUBLE,
                promotion_score DOUBLE, floor_pass BOOLEAN, floor_failures VARCHAR,
                computed_at TIMESTAMP
            )
        """)
        con.execute(
            """INSERT INTO scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                score_id, bot_id, clock_day,
                breakdown.return_score, breakdown.risk_score, breakdown.confidence_score,
                breakdown.regime_score, breakdown.calibration_score,
                breakdown.promotion_score, breakdown.floor_pass,
                ",".join(breakdown.floor_failures),
                datetime.now(timezone.utc),
            ],
        )
    finally:
        con.close()

    return score_id


def score_all_bots() -> None:
    log.info("scoring_pass_start")
    with session_scope() as s:
        bot_ids = [b.bot_id for b in s.query(BotState).all()]

    for bot_id in bot_ids:
        try:
            breakdown = score_bot(bot_id)
        except Exception:
            log.exception("score_bot_failed", bot_id=bot_id)
            continue

        if breakdown is None:
            continue

        score_id = persist_score(bot_id, breakdown)
        outcome = promotion_outcome(breakdown.promotion_score, breakdown.floor_pass)
        write_audit(
            "score_computed",
            bot_id=bot_id,
            payload={
                "score_id": score_id,
                "promotion_score": breakdown.promotion_score,
                "floor_pass": breakdown.floor_pass,
                "outcome": outcome,
            },
        )
        log.info(
            "score_computed",
            bot=bot_id,
            score=round(breakdown.promotion_score, 3),
            floor_pass=breakdown.floor_pass,
            outcome=outcome,
        )
    log.info("scoring_pass_complete")
