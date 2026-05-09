"""Pure-logic tests for COPY's per-wallet PnL attribution.

Tests `compute_attribution_rows` in isolation — no DB, no mocks. The DB
orchestration in `attribute_closed_trade` is exercised end-to-end by
the backfill script + leaderboard verification.
"""
import os

# Avoid pulling in framework settings that require Postgres at import time
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://nope:nope@localhost/nope")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from bots.copy.loop_helpers import compute_attribution_rows


# ---------- Happy path -------------------------------------------------------

def test_equal_share_three_wallet_cluster():
    rows = compute_attribution_rows(
        trade_bot_id="copy",
        trade_fill_status="closed",
        trade_venue="solana",
        trade_pnl_usd=6.00,
        trade_pnl_pct=10.0,
        signal_payload={"wallets": ["wA", "wB", "wC"], "cluster_size": 3},
        signal_venue="solana",
    )
    assert len(rows) == 3
    for r in rows:
        assert r["attributed_pnl_usd"] == 2.00
        assert r["attributed_pnl_pct"] == 10.0
        assert r["chain"] == "solana"
        assert r["cluster_size"] == 3
        assert r["notional_contribution_usd"] is None


def test_negative_pnl_split_correctly():
    rows = compute_attribution_rows(
        trade_bot_id="copy", trade_fill_status="closed", trade_venue="solana",
        trade_pnl_usd=-9.00, trade_pnl_pct=-15.0,
        signal_payload={"wallets": ["wA", "wB", "wC"], "cluster_size": 3},
        signal_venue="solana",
    )
    assert len(rows) == 3
    assert all(r["attributed_pnl_usd"] == -3.00 for r in rows)
    assert all(r["attributed_pnl_pct"] == -15.0 for r in rows)


def test_wallet_notionals_passed_through_when_present():
    rows = compute_attribution_rows(
        trade_bot_id="copy", trade_fill_status="closed", trade_venue="solana",
        trade_pnl_usd=10.0, trade_pnl_pct=5.0,
        signal_payload={
            "wallets": ["wA", "wB"],
            "cluster_size": 2,
            "wallet_notionals": {"wA": 8000.0, "wB": 12000.0},
        },
        signal_venue="solana",
    )
    addrs = {r["wallet_address"]: r for r in rows}
    assert addrs["wA"]["notional_contribution_usd"] == 8000.0
    assert addrs["wB"]["notional_contribution_usd"] == 12000.0
    # Equal-share unaffected — both wallets still get pnl/2
    assert addrs["wA"]["attributed_pnl_usd"] == 5.0
    assert addrs["wB"]["attributed_pnl_usd"] == 5.0


def test_chain_falls_back_to_trade_venue_then_solana():
    # signal venue present -> use it
    r = compute_attribution_rows(
        trade_bot_id="copy", trade_fill_status="closed", trade_venue="solana",
        trade_pnl_usd=3.0, trade_pnl_pct=1.0,
        signal_payload={"wallets": ["wA"], "cluster_size": 1},
        signal_venue="base",
    )
    assert r[0]["chain"] == "base"
    # signal venue None -> fall back to trade venue
    r2 = compute_attribution_rows(
        trade_bot_id="copy", trade_fill_status="closed", trade_venue="arbitrum",
        trade_pnl_usd=3.0, trade_pnl_pct=1.0,
        signal_payload={"wallets": ["wA"], "cluster_size": 1},
        signal_venue=None,
    )
    assert r2[0]["chain"] == "arbitrum"


# ---------- Rejection cases --------------------------------------------------

def test_rejects_non_copy_bot():
    rows = compute_attribution_rows(
        trade_bot_id="structure", trade_fill_status="closed", trade_venue="hyperliquid",
        trade_pnl_usd=5.0, trade_pnl_pct=2.0,
        signal_payload={"wallets": ["wA"], "cluster_size": 1},
        signal_venue="hyperliquid",
    )
    assert rows == []


def test_rejects_open_trade():
    rows = compute_attribution_rows(
        trade_bot_id="copy", trade_fill_status="open", trade_venue="solana",
        trade_pnl_usd=5.0, trade_pnl_pct=2.0,
        signal_payload={"wallets": ["wA"], "cluster_size": 1},
        signal_venue="solana",
    )
    assert rows == []


def test_rejects_no_fill():
    rows = compute_attribution_rows(
        trade_bot_id="copy", trade_fill_status="no_fill", trade_venue="solana",
        trade_pnl_usd=None, trade_pnl_pct=None,
        signal_payload={"wallets": ["wA"], "cluster_size": 1},
        signal_venue="solana",
    )
    assert rows == []


def test_rejects_missing_pnl():
    rows = compute_attribution_rows(
        trade_bot_id="copy", trade_fill_status="closed", trade_venue="solana",
        trade_pnl_usd=None, trade_pnl_pct=5.0,
        signal_payload={"wallets": ["wA"], "cluster_size": 1},
        signal_venue="solana",
    )
    assert rows == []


def test_rejects_empty_signal_payload():
    rows = compute_attribution_rows(
        trade_bot_id="copy", trade_fill_status="closed", trade_venue="solana",
        trade_pnl_usd=5.0, trade_pnl_pct=2.0,
        signal_payload=None,
        signal_venue="solana",
    )
    assert rows == []


def test_rejects_payload_missing_wallets():
    rows = compute_attribution_rows(
        trade_bot_id="copy", trade_fill_status="closed", trade_venue="solana",
        trade_pnl_usd=5.0, trade_pnl_pct=2.0,
        signal_payload={"cluster_size": 3},  # no 'wallets' key
        signal_venue="solana",
    )
    assert rows == []


def test_rejects_payload_empty_wallets_list():
    rows = compute_attribution_rows(
        trade_bot_id="copy", trade_fill_status="closed", trade_venue="solana",
        trade_pnl_usd=5.0, trade_pnl_pct=2.0,
        signal_payload={"wallets": [], "cluster_size": 0},
        signal_venue="solana",
    )
    assert rows == []


def test_filters_non_string_wallet_entries():
    """Defensive: payload corruption shouldn't crash attribution."""
    rows = compute_attribution_rows(
        trade_bot_id="copy", trade_fill_status="closed", trade_venue="solana",
        trade_pnl_usd=6.0, trade_pnl_pct=3.0,
        signal_payload={"wallets": ["wA", None, 42, "wB"], "cluster_size": 4},
        signal_venue="solana",
    )
    # cluster_size stays 4 (used for division), but only 2 valid string wallets get rows
    assert len(rows) == 2
    addrs = {r["wallet_address"] for r in rows}
    assert addrs == {"wA", "wB"}
    # Each gets pnl/4 (cluster_size determines divisor, not number of valid rows)
    for r in rows:
        assert r["attributed_pnl_usd"] == 1.5


# ---------- cluster_size fallback --------------------------------------------

def test_cluster_size_inferred_from_wallets_when_missing():
    rows = compute_attribution_rows(
        trade_bot_id="copy", trade_fill_status="closed", trade_venue="solana",
        trade_pnl_usd=4.0, trade_pnl_pct=2.0,
        signal_payload={"wallets": ["wA", "wB"]},  # no cluster_size key
        signal_venue="solana",
    )
    assert len(rows) == 2
    assert all(r["attributed_pnl_usd"] == 2.0 for r in rows)
    assert all(r["cluster_size"] == 2 for r in rows)
