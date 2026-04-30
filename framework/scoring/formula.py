"""PromotionScore formula.

PromotionScore = ReturnScore * RiskScore * ConfidenceScore * RegimeScore * CalibrationScore
All components in [0, 1.5], multiplicative.

Strict floors for promotion eligibility:
    NetReturn% >= +5
    MaxDD% <= 50
    EffectiveTradeCount >= 15
    RegimesProfitable >= 2
    CalibrationRatio >= 0.6

EffectiveTradeCount = NumTrades * WinRateConfidence (anti-gaming).

Bots are blind to this module — only the scoring engine process imports it.
"""
from dataclasses import dataclass, asdict
from typing import Any, Optional


# --- Floors -----------------------------------------------------------------
RETURN_FLOOR_PCT = 5.0
DD_FLOOR_PCT = 50.0
TRADE_FLOOR = 15
REGIME_FLOOR = 2
CALIB_FLOOR = 0.6

# --- Score normalization constants -----------------------------------------
RETURN_FULL_SCALE_PCT = 30.0   # +30% return → ReturnScore = 1.0; capped at 1.5
RISK_FULL_SCALE_DD_PCT = 60.0  # 0% DD → 1.0; >=60% DD → 0.0
CONFIDENCE_FULL_SCALE = 50     # >=50 effective trades → 1.0


@dataclass
class ScoreInputs:
    net_return_pct: float
    max_dd_pct: float
    num_trades: int
    win_rate: float                 # 0..1
    win_rate_confidence: float      # 0..1, e.g. Wilson lower bound
    regimes_occurred: int
    regimes_profitable: int
    calibration_ratio: Optional[float]  # actual_pnl / sim_pnl across closed shadow trades


@dataclass
class ScoreBreakdown:
    return_score: float
    risk_score: float
    confidence_score: float
    regime_score: float
    calibration_score: float
    promotion_score: float
    floor_pass: bool
    floor_failures: list[str]
    effective_trade_count: float

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def compute_score(inp: ScoreInputs) -> ScoreBreakdown:
    return_score = _clip(inp.net_return_pct / RETURN_FULL_SCALE_PCT, 0.0, 1.5)
    risk_score = _clip(1.0 - (inp.max_dd_pct / RISK_FULL_SCALE_DD_PCT), 0.0, 1.5)

    effective_trades = inp.num_trades * inp.win_rate_confidence
    confidence_score = _clip(effective_trades / CONFIDENCE_FULL_SCALE, 0.0, 1.0)

    if inp.regimes_occurred <= 0:
        regime_score = 0.0
    else:
        regime_score = _clip(inp.regimes_profitable / inp.regimes_occurred, 0.0, 1.5)

    if inp.calibration_ratio is None:
        calibration_score = 0.0
    else:
        calibration_score = _clip(1.0 - abs(1.0 - inp.calibration_ratio), 0.0, 1.5)

    promotion_score = (
        return_score * risk_score * confidence_score * regime_score * calibration_score
    )

    floor_failures: list[str] = []
    if inp.net_return_pct < RETURN_FLOOR_PCT:
        floor_failures.append(f"return {inp.net_return_pct:.2f}% < {RETURN_FLOOR_PCT}%")
    if inp.max_dd_pct > DD_FLOOR_PCT:
        floor_failures.append(f"max_dd {inp.max_dd_pct:.2f}% > {DD_FLOOR_PCT}%")
    if inp.num_trades < TRADE_FLOOR:
        floor_failures.append(f"trades {inp.num_trades} < {TRADE_FLOOR}")
    if inp.regimes_profitable < REGIME_FLOOR:
        floor_failures.append(f"profitable_regimes {inp.regimes_profitable} < {REGIME_FLOOR}")
    if inp.calibration_ratio is None or inp.calibration_ratio < CALIB_FLOOR:
        floor_failures.append(
            f"calibration {inp.calibration_ratio} < {CALIB_FLOOR}"
        )

    return ScoreBreakdown(
        return_score=return_score,
        risk_score=risk_score,
        confidence_score=confidence_score,
        regime_score=regime_score,
        calibration_score=calibration_score,
        promotion_score=promotion_score,
        floor_pass=len(floor_failures) == 0,
        floor_failures=floor_failures,
        effective_trade_count=effective_trades,
    )


def promotion_outcome(score: float, floor_pass: bool) -> str:
    """Map (score, floor_pass) to one of:
       'strong_promote' | 'conditional_promote' | 'extended_paper' | 'kill' | 'pre_floor'
    """
    if not floor_pass:
        if score < 0.2:
            return "kill"
        return "pre_floor"
    if score >= 1.0:
        return "strong_promote"
    if score >= 0.5:
        return "conditional_promote"
    if score >= 0.2:
        return "extended_paper"
    return "kill"
