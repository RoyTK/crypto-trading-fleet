"""API vetter: swap parsing, hold spans, profit-weighted hold and the recalibrated verdicts."""
from bots.copy.api_vetter import (bundle_groups, classify, hold_spans, parse_swaps,
                                  profit_share_ge15m)

SOL = "So11111111111111111111111111111111111111112"


def _tx(ts, tok, amt):
    return {"blockTime": ts, "balanceChange": [
        {"address": SOL, "amount": -amt}, {"address": tok, "amount": amt}]}


def test_parse_and_spans():
    txs = [_tx("2026-11-01T00:00:00+00:00", "A", 5), _tx("2026-11-01T00:30:00+00:00", "A", -5),
           _tx("2026-11-01T00:00:00+00:00", "B", 5), _tx("2026-11-01T00:00:30+00:00", "B", -5),
           _tx("2026-11-01T00:00:00+00:00", "C", 5)]                  # C never sold
    sw = parse_swaps(txs)
    assert ("A", "buy", sw[0][2]) == sw[0] and sw[1][1] == "sell"
    sp = hold_spans(sw)
    assert round(sp["A"]) == 30 and round(sp["B"], 1) == 0.5 and "C" not in sp


def test_profit_weighted_hold_beats_median():
    # 4 fast losers + 1 slow big winner: median hold is fast, but the PROFIT comes from >=15m
    spans = {"f1": 1, "f2": 2, "f3": 3, "f4": 4, "slow": 120}
    pnl = {"f1": {"realized": -10}, "f2": {"realized": -10}, "f3": {"realized": 5},
           "f4": {"realized": -10}, "slow": {"realized": 500}}
    assert profit_share_ge15m(spans, pnl) > 0.95


def _summary(realized, unrealized=0, trades=5000, tokens=200):
    return {"summary": {"unique_tokens": tokens, "counts": {"total_trade": trades},
                        "pnl": {"realized_profit_usd": realized, "unrealized_usd": unrealized,
                                "avg_profit_per_trade_usd": realized / max(trades, 1)}}}


def _tokens(n_slow_winners=3):
    spans, pnl = {}, {}
    for i in range(n_slow_winners):
        spans[f"w{i}"] = 60
        pnl[f"w{i}"] = {"realized": 1000, "realized_pct": 150, "buys": 3, "bought_qty": 10, "sold_qty": 10}
    for i in range(4):
        spans[f"l{i}"] = 2
        pnl[f"l{i}"] = {"realized": -50, "realized_pct": -30, "buys": 3, "bought_qty": 10, "sold_qty": 10}
    return spans, pnl


def test_cluster_keep_on_profit_weighted_hold():
    spans, pnl = _tokens()
    assert classify(_summary(50_000), spans, pnl)["cluster"] == "KEEP"


def test_cluster_bag_with_strong_entries_is_uncertain_not_reject():
    spans, pnl = _tokens()
    assert classify(_summary(50_000, unrealized=-80_000), spans, pnl)["cluster"] == "UNCERTAIN"


def test_cluster_too_fast_when_profit_is_fast():
    spans = {f"t{i}": 1 for i in range(6)}
    pnl = {t: {"realized": 100, "realized_pct": 150, "buys": 1, "bought_qty": 1, "sold_qty": 1} for t in spans}
    assert classify(_summary(50_000), spans, pnl)["cluster"] == "TOO_FAST"


def test_conviction_2x_rate_is_not_a_gate():
    spans, pnl = _tokens(n_slow_winners=0)
    for i in range(5):                       # mid-hold accumulators, no 2x wins, all exited
        spans[f"m{i}"] = 90
        pnl[f"m{i}"] = {"realized": 300, "realized_pct": 40, "buys": 4, "bought_qty": 10, "sold_qty": 10}
    assert classify(_summary(20_000, unrealized=-30_000), spans, pnl)["conviction"] == "KEEP_CONVICTION"


def test_bundle_groups():
    a = [(f"t{i}", "buy", 1000.0 + i * 100) for i in range(12)]
    b = [(f"t{i}", "buy", 1010.0 + i * 100) for i in range(12)]
    c = [(f"x{i}", "buy", 1000.0 + i * 100) for i in range(12)]
    assert bundle_groups({"A": a, "B": b, "C": c}) == [{"A", "B"}]


def test_admission_gate_only_blocks_junk():
    from bots.copy.api_vetter import admit_cluster_probation
    ok = {"cluster": "TOO_FAST", "metrics": {"realized": 90_000}}
    assert admit_cluster_probation(ok)
    assert not admit_cluster_probation({"cluster": "MM_BOT", "metrics": {"realized": 3_000_000}})
    assert not admit_cluster_probation({"cluster": "BUNDLE", "metrics": {"realized": 90_000}})
    assert not admit_cluster_probation({"cluster": "KEEP", "metrics": {"realized": 4_000}})
