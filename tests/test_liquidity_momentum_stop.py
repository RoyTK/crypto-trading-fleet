"""Unit tests for the cluster live liquidity-momentum stop selection.

The rule (2026-06-28): pick the cluster hard stop from the EMA-smoothed liquidity
trajectory — widen when liquidity is BUILDING vs entry, keep tight when flat/
draining, and an OFF-by-default low-flat-liquidity floor capability.
"""
from bots.copy.trailing_stop import liquidity_aware_stop_pct

BASE = 8.0
GROW = 1.5
DEEP = 30.0


def test_building_liquidity_widens_to_deep():
    # EMA liquidity at 2x entry (>= grow_ratio) -> deep stop (give it room).
    assert liquidity_aware_stop_pct(100_000, 200_000, base_stop=BASE,
                                    grow_ratio=GROW, deep_stop=DEEP) == DEEP


def test_flat_liquidity_keeps_tight():
    # Roughly flat (1.0x) -> base/tight stop.
    assert liquidity_aware_stop_pct(100_000, 100_000, base_stop=BASE,
                                    grow_ratio=GROW, deep_stop=DEEP) == BASE


def test_draining_liquidity_keeps_tight():
    assert liquidity_aware_stop_pct(100_000, 40_000, base_stop=BASE,
                                    grow_ratio=GROW, deep_stop=DEEP) == BASE


def test_just_below_grow_ratio_stays_tight():
    # 1.49x with grow_ratio 1.5 -> not yet widened (hysteresis margin).
    assert liquidity_aware_stop_pct(100_000, 149_000, base_stop=BASE,
                                    grow_ratio=GROW, deep_stop=DEEP) == BASE


def test_missing_liquidity_falls_back_to_base():
    assert liquidity_aware_stop_pct(None, 200_000, base_stop=BASE,
                                    grow_ratio=GROW, deep_stop=DEEP) == BASE
    assert liquidity_aware_stop_pct(100_000, None, base_stop=BASE,
                                    grow_ratio=GROW, deep_stop=DEEP) == BASE
    assert liquidity_aware_stop_pct(0, 0, base_stop=BASE,
                                    grow_ratio=GROW, deep_stop=DEEP) == BASE


def test_flat_floor_off_by_default_is_noop():
    # flat_floor=0 (default OFF): a low, non-growing token stays at base stop.
    assert liquidity_aware_stop_pct(5_000, 5_000, base_stop=BASE,
                                    grow_ratio=GROW, deep_stop=DEEP,
                                    flat_floor=0.0, flat_stop=3.0) == BASE


def test_flat_floor_enabled_applies_flat_stop():
    # floor enabled at $10k: smoothed $5k < floor AND not growing -> flat stop.
    assert liquidity_aware_stop_pct(5_000, 5_000, base_stop=BASE,
                                    grow_ratio=GROW, deep_stop=DEEP,
                                    flat_floor=10_000, flat_stop=3.0) == 3.0


def test_flat_floor_does_not_override_building():
    # Even below the floor, if liquidity is BUILDING it gets the deep stop.
    assert liquidity_aware_stop_pct(2_000, 6_000, base_stop=BASE,
                                    grow_ratio=GROW, deep_stop=DEEP,
                                    flat_floor=10_000, flat_stop=3.0) == DEEP
