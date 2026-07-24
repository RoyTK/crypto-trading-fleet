"""Position liquidity-trajectory logging (2026-07-23).

Passive/fleet-wide observability: on each exit-management cycle, snapshot every open
position's (price, liquidity, peak_pct, age) — throttled to once per interval per trade —
into `position_liquidity_log`. No behavior change; fail-open (a logging error must NEVER
block exit management).

WHY: we currently store liquidity only at ENTRY (sim_metadata.entry_liquidity_usd) and can
fetch it NOW, but have nothing in between. So we cannot answer "does live liquidity at the
2-day mark separate the promobuy rugs from the survivors" (the flatline-exit / 1322 question).
This builds that trajectory dataset so the flatline exit can later gain a "liq stable/rising
-> spare the late-runner" gate instead of cutting every liquid flatline.

Run standalone to create the table:  python -m bots.copy.position_liq_log
"""
from __future__ import annotations

from sqlalchemy import text

from framework.db import session_scope
from framework.logging_setup import get_logger

log = get_logger("position_liq_log")

DDL = """
CREATE TABLE IF NOT EXISTS position_liquidity_log (
    id            BIGSERIAL PRIMARY KEY,
    trade_id      INTEGER NOT NULL,
    bot_id        VARCHAR(32) NOT NULL,
    strategy      VARCHAR(32),
    asset         VARCHAR(128) NOT NULL,
    price         DOUBLE PRECISION,
    liquidity_usd DOUBLE PRECISION,
    peak_pct      DOUBLE PRECISION,
    age_hours     DOUBLE PRECISION,
    logged_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_posliq_trade_time ON position_liquidity_log (trade_id, logged_at);
CREATE INDEX IF NOT EXISTS ix_posliq_strategy_time ON position_liquidity_log (strategy, logged_at);
"""

_INSERT = text("""
INSERT INTO position_liquidity_log
    (trade_id, bot_id, strategy, asset, price, liquidity_usd, peak_pct, age_hours)
VALUES
    (:trade_id, :bot_id, :strategy, :asset, :price, :liquidity_usd, :peak_pct, :age_hours)
""")


def ensure_table() -> None:
    with session_scope() as s:
        for stmt in DDL.split(";"):
            if stmt.strip():
                s.execute(text(stmt))


def record(rows: list[dict]) -> None:
    """Batch-insert position liquidity snapshots. `rows` is a list of dicts with keys
    trade_id, bot_id, strategy, asset, price, liquidity_usd, peak_pct, age_hours.
    Fail-open: never raises into the caller."""
    if not rows:
        return
    try:
        with session_scope() as s:
            s.execute(_INSERT, rows)
    except Exception:
        log.exception("position_liq_log_insert_failed", n=len(rows))


if __name__ == "__main__":
    ensure_table()
    print("position_liquidity_log table ensured")
