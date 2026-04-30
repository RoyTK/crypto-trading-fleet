"""Pure-function tests for the PromotionScore formula. No DB."""
import math
import pytest

from framework.scoring.formula import (
    ScoreInputs, compute_score, promotion_outcome,
    RETURN_FLOOR_PCT, DD_FLOOR_PCT, TRADE_FLOOR, REGIME_FLOOR, CALIB_FLOOR,
)


def _strong_inputs(**overrides) -> ScoreInputs:
    base = dict(
        net_return_pct=30.0,
        max_dd_pct=20.0,
        num_trades=80,
        win_rate=0.60,
        win_rate_confidence=0.55,
        regimes_occurred=3,
        regimes_profitable=3,
        calibration_ratio=1.0,
    )
    base.update(overrides)
    return ScoreInputs(**base)


def test_strong_promote_path():
    b = compute_score(_strong_inputs())
    assert b.floor_pass is True
    assert b.promotion_score >= 1.0
    assert promotion_outcome(b.promotion_score, b.floor_pass) == "strong_promote"


def test_negative_return_kills_score():
    b = compute_score(_strong_inputs(net_return_pct=-10.0))
    assert b.return_score == 0.0
    assert b.promotion_score == 0.0
    assert b.floor_pass is False


def test_dd_floor_excludes_promotion():
    b = compute_score(_strong_inputs(max_dd_pct=DD_FLOOR_PCT + 1))
    assert b.floor_pass is False
    assert any("max_dd" in f for f in b.floor_failures)


def test_trade_floor_excludes_promotion():
    b = compute_score(_strong_inputs(num_trades=TRADE_FLOOR - 1))
    assert b.floor_pass is False


def test_regime_floor_excludes_promotion():
    b = compute_score(_strong_inputs(regimes_profitable=REGIME_FLOOR - 1, regimes_occurred=3))
    assert b.floor_pass is False


def test_calibration_floor_excludes_promotion():
    b = compute_score(_strong_inputs(calibration_ratio=CALIB_FLOOR - 0.01))
    assert b.floor_pass is False


def test_calibration_none_treated_as_floor_failure():
    b = compute_score(_strong_inputs(calibration_ratio=None))
    assert b.floor_pass is False
    assert b.calibration_score == 0.0


def test_anti_gaming_random_padding_self_defeats():
    """Padding trades with random outcomes should produce near-50% win rate
    and a low Wilson confidence — score collapses."""
    n = 500
    win_rate_confidence = 0.45  # near coin-flip with weak Wilson lower bound
    b = compute_score(_strong_inputs(
        num_trades=n, win_rate=0.50, win_rate_confidence=win_rate_confidence,
    ))
    assert b.confidence_score <= 1.0
    # If confidence is forced low, total promotion score should be modest at best
    assert b.promotion_score < 1.5


def test_calibration_score_drops_as_ratio_drifts():
    matched = compute_score(_strong_inputs(calibration_ratio=1.0))
    drifted = compute_score(_strong_inputs(calibration_ratio=0.7))
    assert drifted.calibration_score < matched.calibration_score


def test_components_clipped_to_range():
    """Even with extreme inputs, individual components stay within [0, 1.5]."""
    b = compute_score(ScoreInputs(
        net_return_pct=500.0, max_dd_pct=0.0, num_trades=10000,
        win_rate=1.0, win_rate_confidence=1.0,
        regimes_occurred=4, regimes_profitable=4,
        calibration_ratio=1.0,
    ))
    for v in (b.return_score, b.risk_score, b.confidence_score,
              b.regime_score, b.calibration_score):
        assert 0.0 <= v <= 1.5


def test_promotion_outcome_buckets():
    assert promotion_outcome(1.5, True) == "strong_promote"
    assert promotion_outcome(0.99, True) == "conditional_promote"
    assert promotion_outcome(0.49, True) == "extended_paper"
    assert promotion_outcome(0.10, True) == "kill"
    assert promotion_outcome(0.10, False) == "kill"
    assert promotion_outcome(0.99, False) == "pre_floor"
