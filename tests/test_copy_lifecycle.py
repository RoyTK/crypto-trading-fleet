"""Light lifecycle tests for COPY bot — verify wiring without DB or network.

These tests construct the bot and verify constructor invariants. Full
end-to-end (DB/Helius/Cielo) behavior is exercised by the shakedown scripts,
not pytest.
"""
import os

# Avoid pulling in framework settings that require Postgres at import time
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://nope:nope@localhost/nope")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from bots.copy.config import (
    ALLOCATION_CAP_PCT,
    CLUSTER_MIN_NOTIONAL_PER_WALLET_USD,
    CLUSTER_MIN_WALLETS,
    CLUSTER_WINDOW_MINUTES,
    PER_TRADE_NOTIONAL_CAP_PCT,
    SIGNAL_SPECS,
    get_copy_settings,
)


def test_locked_thresholds_match_design():
    """Item #7 thresholds — lock these in a test so a refactor can't silently shift them."""
    assert CLUSTER_MIN_WALLETS == 3
    assert CLUSTER_WINDOW_MINUTES == 15
    # Re-lowered to $1k 2026-05-24 — live data showed $5k floor was too tight
    # for 2026 memecoin buy-size distribution. See config.py comment.
    assert CLUSTER_MIN_NOTIONAL_PER_WALLET_USD == 1_000.0
    assert PER_TRADE_NOTIONAL_CAP_PCT == 8.0
    assert ALLOCATION_CAP_PCT == 50.0


def test_signal_spec_cluster_buy_has_exits():
    spec = SIGNAL_SPECS["cluster_buy"]
    assert spec.stop_pct > 0
    assert spec.take_profit_pct > 0
    assert spec.timeout_hours > 0


def test_settings_defaults_loadable():
    s = get_copy_settings()
    assert s.copy_paper_capital_usd > 0
    assert 0 < s.copy_shadow_pct <= 100
    assert s.copy_dd_daily_pct == 12.0
    assert s.copy_dd_weekly_pct == 28.0
    assert s.copy_dd_total_pct == 50.0


def test_dd_thresholds_match_dd_monitor_registry():
    """Cross-check: framework/dd_monitor.py BOT_DD_THRESHOLDS must match copy config."""
    from framework.dd_monitor import BOT_DD_THRESHOLDS
    s = get_copy_settings()
    th = BOT_DD_THRESHOLDS["copy"]
    assert th["daily"] == s.copy_dd_daily_pct
    assert th["weekly"] == s.copy_dd_weekly_pct
    assert th["total"] == s.copy_dd_total_pct
