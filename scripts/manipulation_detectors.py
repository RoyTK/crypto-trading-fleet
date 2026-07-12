"""Memecoin manipulation detectors — reusable, adaptable core.

Implements the detection heuristics from two studies (both defensive/detection
framing; WE use them offensively — as timing/exit + quality signals, not as
"avoid manipulation" filters, since ~83% of >100% runners ARE manipulated):

  Paper 1 "A Midsummer Meme's Dream" (Mongardini & Mei) — market-wide detectors:
    wash trading (zero-risk position, circular volume), Liquidity-Pool-based Price
    Inflation (LPI), ownership concentration. Manipulation PRECEDES extraction
    (62.9% of dumps had prior wash/LPI) → leading indicator for the exit.
  Paper 2 "Resisting Manipulative Bots in Memecoin Copy Trading" (Luo et al.) —
    Pump.fun bot taxonomy + wash-score algorithm; KOL-quality features.

Pure functions over already-fetched data (no network) so they're unit-testable
and reusable in a live loop, a backtest, or a shadow detector. Every threshold is
a named arg with the paper default — CALIBRATE per our own data, don't adopt
verbatim (their goal was low false-positives; ours is not being exit liquidity).

A "trade" here is a dict: {owner, side ('buy'|'sell'), base_amount, usd, ts}.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

# ---- paper-default thresholds (override at call site) -----------------------
ZERO_RISK_TOL = 0.02        # buy/sell amounts within 2% = zero-risk round trip
WASH_SCORE_FLAG = 50.0      # Paper 2 Algo 2 per-trader flag threshold
CONCENTRATION_TOP10 = 0.30  # top-10 holders > 30% supply = concentrated
LPI_PRICE_JUMP = 1.00       # +100% in the window
LPI_VOL_GROWTH = 0.20       # while volume grows < 20%...
LPI_ABS_VOL = 1000.0        # ...or absolute window volume < $1000
LPI_BUY_FRAC = 0.90         # buy volume > 90% of total
LPI_MAX_TRADERS = 10        # <= 10 unique traders


@dataclass
class ManipReport:
    n_trades: int = 0
    n_owners: int = 0
    wash_score_max: float = 0.0          # Paper 2: max per-owner wash score
    wash_flagged: bool = False
    zero_risk_vol_frac: float = 0.0      # Paper 1: frac of volume from zero-risk makers
    zero_risk_flagged: bool = False      # >= ~0.5 of volume is zero-risk
    circular_vol_frac: float = 0.0       # frac of volume from makers who buy AND sell
    lpi_flagged: bool = False
    lpi_reason: str = ""
    detail: dict = field(default_factory=dict)


def _by_owner(trades):
    buys, sells = defaultdict(float), defaultdict(float)   # base-amount sums
    for t in trades:
        amt = abs(float(t.get("base_amount") or 0))
        if t.get("side") == "buy":
            buys[t["owner"]] += amt
        elif t.get("side") == "sell":
            sells[t["owner"]] += amt
    return buys, sells


def wash_score(trades, flag_threshold: float = WASH_SCORE_FLAG):
    """Paper 2 Algo 2: per-trader ratio of matched (offsetting) round-trip volume
    to |net position|. A wash trader churns huge offsetting volume near-zero net →
    high score. Token flagged if ANY trader exceeds flag_threshold. Returns
    (max_score, flagged, per_owner_scores)."""
    buys, sells = _by_owner(trades)
    owners = set(buys) | set(sells)
    scores = {}
    for o in owners:
        b, s = buys[o], sells[o]
        round_trip = 2.0 * min(b, s)          # offsetting volume
        net = abs(b - s)
        scores[o] = round_trip / net if net > 1e-12 else (round_trip and 1e6 or 0.0)
    mx = max(scores.values()) if scores else 0.0
    return mx, mx >= flag_threshold, scores


def zero_risk_fraction(trades, tol: float = ZERO_RISK_TOL):
    """Paper 1: fraction of total base volume transacted by 'zero-risk' makers —
    those whose buy and sell amounts match within `tol` (buy then dump ~same size,
    no price risk). Their single strongest wash tell (~96% catch)."""
    buys, sells = _by_owner(trades)
    owners = set(buys) | set(sells)
    total_vol = sum(buys.values()) + sum(sells.values())
    if total_vol <= 0:
        return 0.0
    zr_vol = 0.0
    for o in owners:
        b, s = buys[o], sells[o]
        if b > 0 and s > 0 and abs(b - s) / max(b, s) <= tol:
            zr_vol += b + s
    return zr_vol / total_vol


def circular_fraction(trades):
    """Paper 1 'circular volume': fraction of volume from makers who BOTH buy and
    sell in the window (regardless of size match). 99%+ = classic wash market."""
    buys, sells = _by_owner(trades)
    total = sum(buys.values()) + sum(sells.values())
    if total <= 0:
        return 0.0
    both = set(b for b in buys if buys[b] > 0) & set(s for s in sells if sells[s] > 0)
    return (sum(buys[o] + sells[o] for o in both)) / total


def lpi_signature(price_start: float, price_peak: float,
                  window_usd_volume: float, prior_usd_volume: float,
                  buy_usd: float, total_usd: float, n_traders: int,
                  price_jump=LPI_PRICE_JUMP, vol_growth=LPI_VOL_GROWTH,
                  abs_vol=LPI_ABS_VOL, buy_frac=LPI_BUY_FRAC,
                  max_traders=LPI_MAX_TRADERS):
    """Paper 1 LPI: a big price jump manufactured by a tiny buy in a thin pool —
    price >+100% while (volume grew <20% OR absolute window volume < $1000) AND
    buy-vol >90% AND <=10 traders. Median cost to pull off was $54. Returns
    (flagged, reason)."""
    if price_start <= 0:
        return False, "no_price"
    jump = price_peak / price_start - 1.0
    if jump < price_jump:
        return False, f"jump_only_{jump:.0%}"
    grew = (window_usd_volume / prior_usd_volume - 1.0) if prior_usd_volume > 0 else 9.9
    thin = (grew < vol_growth) or (window_usd_volume < abs_vol)
    bfrac = (buy_usd / total_usd) if total_usd > 0 else 0.0
    if thin and bfrac >= buy_frac and n_traders <= max_traders:
        return True, (f"jump{jump:.0%} volgrow{grew:.0%} winvol${window_usd_volume:.0f} "
                      f"buy{bfrac:.0%} traders{n_traders}")
    return False, (f"jump{jump:.0%} thin={thin} buy{bfrac:.0%} traders{n_traders}")


def concentration_flag(top10_frac: float, threshold: float = CONCENTRATION_TOP10):
    """Paper 1: top-10 holders > 30% of supply flagged ~51% of high-return tokens
    (bundle-buy / fresh-address disguise). NOTE: current-state; noisy for old tokens."""
    return (top10_frac is not None and top10_frac > threshold), top10_frac


def analyze(trades, *, wash_flag=WASH_SCORE_FLAG, zr_tol=ZERO_RISK_TOL) -> ManipReport:
    """Run the transaction-based detectors over one token's window of trades."""
    r = ManipReport(n_trades=len(trades))
    if not trades:
        return r
    buys, sells = _by_owner(trades)
    r.n_owners = len(set(buys) | set(sells))
    r.wash_score_max, r.wash_flagged, _ = wash_score(trades, wash_flag)
    r.zero_risk_vol_frac = zero_risk_fraction(trades, zr_tol)
    r.zero_risk_flagged = r.zero_risk_vol_frac >= 0.5
    r.circular_vol_frac = circular_fraction(trades)
    return r


if __name__ == "__main__":
    # self-test
    organic = [{"owner": f"w{i}", "side": "buy", "base_amount": 100} for i in range(20)]
    wash = ([{"owner": "W", "side": "buy", "base_amount": 1000, "usd": 1000},
             {"owner": "W", "side": "sell", "base_amount": 1000, "usd": 1000}] * 30
            + [{"owner": "real", "side": "buy", "base_amount": 50}])
    ro, rw = analyze(organic), analyze(wash)
    print(f"organic: wash_max={ro.wash_score_max:.1f} zr={ro.zero_risk_vol_frac:.2f} "
          f"circ={ro.circular_vol_frac:.2f} flagged={ro.wash_flagged}")
    print(f"wash:    wash_max={rw.wash_score_max:.1f} zr={rw.zero_risk_vol_frac:.2f} "
          f"circ={rw.circular_vol_frac:.2f} flagged={rw.wash_flagged}")
    lpi = lpi_signature(1e-6, 3e-6, 40, 5000, 38, 40, 4)
    print(f"lpi (thin $30 pump): {lpi}")
    assert rw.wash_flagged and not ro.wash_flagged and lpi[0]
    print("self-test OK")
