"""Pure-function tests for the COPY DEX-aware fill simulator.

Mocks the dex_quoter — we test the simulator's translation from quote → SimulatedFill.
"""
import pytest

from bots.copy.fill_simulator import CopyFillSimulator, DEX_FEE_PCT
from bots.copy.venue.dex_quoter import DexQuote


sim = CopyFillSimulator()


def _good_quote(price_per_token_usd: float = 1.5, slippage_bps: float = 30.0) -> DexQuote:
    return DexQuote(
        asset="TOKEN_X",
        chain="solana",
        input_usd=100.0,
        expected_out_native=66.66,
        expected_price_per_token_usd=price_per_token_usd,
        slippage_bps=slippage_bps,
    )


def test_fill_from_good_quote_returns_filled():
    q = _good_quote()
    fill = sim._fill_from_quote(q, notional_usd=100.0, side="entry")
    assert fill.fill_price == pytest.approx(1.5)
    assert fill.slippage_bps == pytest.approx(30.0)
    assert fill.no_fill_reason is None
    assert fill.fees_usd == pytest.approx(100.0 * DEX_FEE_PCT / 100)


def test_fill_from_none_quote_no_route():
    fill = sim._fill_from_quote(None, notional_usd=100.0, side="entry")
    assert fill.fill_price is None
    assert fill.no_fill_reason == "no_quote_route"


def test_fill_from_zero_output_quote_no_fill():
    q = DexQuote(
        asset="TOKEN_X", chain="solana", input_usd=100.0,
        expected_out_native=0.0, expected_price_per_token_usd=0.0, slippage_bps=0.0,
    )
    fill = sim._fill_from_quote(q, notional_usd=100.0, side="entry")
    assert fill.fill_price is None
    assert fill.no_fill_reason == "zero_output"


def test_simulate_entry_short_unsupported():
    """Cluster signals are always longs in v0; reject short entries cleanly."""
    import asyncio
    from bots.copy.fill_simulator import CopyMarketSnapshot

    class _DummySession:
        async def get(self, *a, **k):
            raise AssertionError("should not hit network for short entry")

    snap = CopyMarketSnapshot(chain="solana", session=_DummySession())  # type: ignore[arg-type]
    fill = asyncio.run(sim.simulate_entry_async(
        asset="TOKEN_X",
        notional_usd=100.0,
        leverage=1.0,
        direction="short",
        market_snapshot=snap,
    ))
    assert fill.fill_price is None
    assert fill.no_fill_reason == "unsupported_direction"


def test_sync_entry_raises_use_async():
    with pytest.raises(NotImplementedError):
        sim.simulate_entry()


def test_sync_exit_raises_use_async():
    with pytest.raises(NotImplementedError):
        sim.simulate_exit()
