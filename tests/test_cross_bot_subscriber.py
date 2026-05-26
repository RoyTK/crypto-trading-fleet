"""Tests for cross-bot bridge: payload persistence + outcome cron arithmetic.

Mocks DB layer + HL venue layer.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Subscriber _write_row
# ---------------------------------------------------------------------------

def _mock_session():
    session_cm = MagicMock()
    session = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False
    return session_cm, session


def test_write_row_persists_valid_payload():
    from bots.structure.signals import cross_bot_subscriber as sub
    session_cm, session = _mock_session()
    with patch.object(sub, "session_scope", return_value=session_cm):
        sub._write_row({
            "cluster_id": "abc-123",
            "asset": "SOL",
            "direction": "long",
            "wallet_count": 4,
            "cluster_size_usd": 12345.6,
            "timestamp_ms": 1700000000000,
        })
    session.add.assert_called_once()
    added = session.add.call_args.args[0]
    assert added.cluster_id == "abc-123"
    assert added.hl_asset == "SOL"
    assert added.wallet_count == 4
    assert added.cluster_size_usd == pytest.approx(12345.6)


def test_write_row_drops_payload_on_db_exception():
    from bots.structure.signals import cross_bot_subscriber as sub
    session_cm = MagicMock()
    session_cm.__enter__.side_effect = Exception("uniqueness violation")
    with patch.object(sub, "session_scope", return_value=session_cm):
        # Should not raise — failure is logged + dropped
        sub._write_row({
            "cluster_id": "dup-1",
            "asset": "BTC",
            "direction": "long",
            "wallet_count": 3,
            "cluster_size_usd": 5000.0,
            "timestamp_ms": 1700000000000,
        })


# ---------------------------------------------------------------------------
# Outcome cron arithmetic
# ---------------------------------------------------------------------------

def _make_row(**overrides):
    """Build a mutable CrossBotSignalLog-like object for outcome cron tests."""
    base = dict(
        cluster_id="test-1",
        hl_asset="SOL",
        direction="long",
        wallet_count=3,
        cluster_size_usd=1000.0,
        event_timestamp_ms=int((datetime.now(timezone.utc) - timedelta(hours=25)).timestamp() * 1000),
        entry_price_usd=None,
        price_at_4h=None,
        price_at_12h=None,
        price_at_24h=None,
        pnl_at_4h_pct=None,
        pnl_at_12h_pct=None,
        pnl_at_24h_pct=None,
        direction_correct_4h=None,
        direction_correct_12h=None,
        direction_correct_24h=None,
        outcome_evaluated_at=None,
    )
    base.update(overrides)
    obj = MagicMock()
    for k, v in base.items():
        setattr(obj, k, v)
    return obj


def test_outcome_cron_fills_entry_and_all_horizons_when_event_old_enough():
    from framework import cross_bot_outcome_cron as cron

    # Event 25h old; entry price not yet set; current SOL = $200, entry should be $200
    # PnL same price -> 0% (direction_correct = False since pnl_pct == 0 not > 0).
    # Use a different price to make outcome unambiguous: simulate entry was filled
    # earlier, now SOL is up 10%.
    row = _make_row(
        entry_price_usd=180.0,
        event_timestamp_ms=int((datetime.now(timezone.utc) - timedelta(hours=25)).timestamp() * 1000),
    )

    session_cm = MagicMock()
    session = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False
    session.execute.return_value.scalars.return_value = [row]

    with patch.object(cron, "session_scope", return_value=session_cm), \
         patch.object(cron, "_fetch_hl_mids", return_value={"SOL": 198.0}):
        cron.run_outcome_evaluation()

    # PnL = (198 - 180) / 180 * 100 = 10.0%
    assert row.price_at_4h == 198.0
    assert row.price_at_12h == 198.0
    assert row.price_at_24h == 198.0
    assert row.pnl_at_4h_pct == pytest.approx(10.0, abs=0.01)
    assert row.direction_correct_4h is True
    assert row.outcome_evaluated_at is not None


def test_outcome_cron_skips_horizons_not_yet_elapsed():
    from framework import cross_bot_outcome_cron as cron

    # Event 6h old: 4h elapsed, 12h + 24h not yet
    row = _make_row(
        entry_price_usd=200.0,
        event_timestamp_ms=int((datetime.now(timezone.utc) - timedelta(hours=6)).timestamp() * 1000),
    )

    session_cm = MagicMock()
    session = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False
    session.execute.return_value.scalars.return_value = [row]

    with patch.object(cron, "session_scope", return_value=session_cm), \
         patch.object(cron, "_fetch_hl_mids", return_value={"SOL": 220.0}):
        cron.run_outcome_evaluation()

    assert row.price_at_4h == 220.0
    assert row.pnl_at_4h_pct == pytest.approx(10.0, abs=0.01)
    assert row.direction_correct_4h is True
    assert row.price_at_12h is None
    assert row.price_at_24h is None
    assert row.outcome_evaluated_at is None  # NOT yet fully evaluated


def test_outcome_cron_short_direction_inverts_pnl():
    from framework import cross_bot_outcome_cron as cron

    row = _make_row(
        direction="short",
        entry_price_usd=200.0,
        event_timestamp_ms=int((datetime.now(timezone.utc) - timedelta(hours=25)).timestamp() * 1000),
    )

    session_cm = MagicMock()
    session = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False
    session.execute.return_value.scalars.return_value = [row]

    with patch.object(cron, "session_scope", return_value=session_cm), \
         patch.object(cron, "_fetch_hl_mids", return_value={"SOL": 220.0}):
        cron.run_outcome_evaluation()

    # Price up 10% but short direction → PnL = -10%, direction NOT correct
    assert row.pnl_at_4h_pct == pytest.approx(-10.0, abs=0.01)
    assert row.direction_correct_4h is False
