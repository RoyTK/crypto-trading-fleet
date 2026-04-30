"""Tests for the median-realistic fill simulator. No network."""
import pytest

from bots.structure.fill_simulator import StructureFillSimulator
from bots.structure.venue import L2Book
from bots.structure.config import TAKER_FEE_PCT


def make_book(asset="BTC", mid=100.0, bid_levels=None, ask_levels=None) -> L2Book:
    bids = bid_levels or [(99.0, 1000.0), (98.0, 1000.0), (97.0, 1000.0)]
    asks = ask_levels or [(101.0, 1000.0), (102.0, 1000.0), (103.0, 1000.0)]
    return L2Book(asset=asset, bids=bids, asks=asks)


def test_long_entry_within_top_level():
    """Small order fills entirely at the inside ask."""
    sim = StructureFillSimulator()
    book = make_book(mid=100.0)
    fill = sim.simulate_entry(
        asset="BTC",
        notional_usd=5_000.0,
        leverage=2.0,
        direction="long",
        market_snapshot=book,
    )
    assert fill.fill_price == pytest.approx(101.0)
    assert fill.fees_usd == pytest.approx(5_000.0 * TAKER_FEE_PCT / 100)
    assert fill.no_fill_reason is None


def test_long_entry_walks_multiple_levels():
    """Order larger than top level walks the book."""
    sim = StructureFillSimulator()
    book = make_book(
        ask_levels=[(101.0, 100.0), (102.0, 100.0), (103.0, 100.0)],
    )
    # 100 * 101 = 10100; 100 * 102 = 10200; 100 * 103 = 10300; total ~30600
    fill = sim.simulate_entry(
        asset="BTC",
        notional_usd=20_000.0,
        leverage=1.0,
        direction="long",
        market_snapshot=book,
    )
    assert fill.fill_price is not None
    # VWAP must be between 101 and 102 (top two levels)
    assert 101.0 < fill.fill_price < 102.0
    assert fill.no_fill_reason is None


def test_long_entry_insufficient_depth():
    sim = StructureFillSimulator()
    book = make_book(ask_levels=[(101.0, 1.0), (102.0, 1.0)])  # ~$203 total depth
    fill = sim.simulate_entry(
        asset="BTC",
        notional_usd=10_000.0,
        leverage=1.0,
        direction="long",
        market_snapshot=book,
    )
    assert fill.fill_price is None
    assert fill.no_fill_reason == "insufficient_depth"
    assert fill.metadata["requested_usd"] == 10_000.0


def test_short_entry_uses_bid_side():
    sim = StructureFillSimulator()
    book = make_book(bid_levels=[(99.0, 1000.0)], ask_levels=[(101.0, 1000.0)])
    fill = sim.simulate_entry(
        asset="BTC",
        notional_usd=5_000.0,
        leverage=2.0,
        direction="short",
        market_snapshot=book,
    )
    assert fill.fill_price == pytest.approx(99.0)


def test_long_exit_uses_bid_side():
    """When closing a long, sell into the bids."""
    sim = StructureFillSimulator()
    book = make_book(bid_levels=[(99.0, 1000.0)], ask_levels=[(101.0, 1000.0)])
    fill = sim.simulate_exit(
        asset="BTC",
        entry_price=100.0,
        exit_target_price=99.0,
        notional_usd=5_000.0,
        leverage=2.0,
        direction="long",
        market_snapshot=book,
    )
    assert fill.fill_price == pytest.approx(99.0)


def test_short_exit_uses_ask_side():
    sim = StructureFillSimulator()
    book = make_book(bid_levels=[(99.0, 1000.0)], ask_levels=[(101.0, 1000.0)])
    fill = sim.simulate_exit(
        asset="BTC",
        entry_price=100.0,
        exit_target_price=101.0,
        notional_usd=5_000.0,
        leverage=2.0,
        direction="short",
        market_snapshot=book,
    )
    assert fill.fill_price == pytest.approx(101.0)


def test_empty_book_returns_no_fill():
    sim = StructureFillSimulator()
    book = L2Book(asset="BTC", bids=[], asks=[])
    fill = sim.simulate_entry(
        asset="BTC",
        notional_usd=1_000.0,
        leverage=1.0,
        direction="long",
        market_snapshot=book,
    )
    assert fill.fill_price is None
    assert fill.no_fill_reason == "empty_book"


def test_wrong_asset_snapshot_returns_no_fill():
    sim = StructureFillSimulator()
    book = make_book(asset="ETH")
    fill = sim.simulate_entry(
        asset="BTC",  # mismatch!
        notional_usd=1_000.0,
        leverage=1.0,
        direction="long",
        market_snapshot=book,
    )
    assert fill.fill_price is None
    assert fill.no_fill_reason == "bad_snapshot"
