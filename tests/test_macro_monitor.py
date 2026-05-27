"""Tests for framework.macro_monitor — kill-switch + geo-shock alert.

Mocks data fetchers; verifies trigger conditions + alert emission +
auto-halt behavior under env flag.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from framework import macro_monitor as mm


def setup_function(_):
    """Reset throttle dict between tests."""
    mm._last_alert_ts.clear()


# ---------------------------------------------------------------------------
# Kill-switch
# ---------------------------------------------------------------------------

def test_kill_switch_does_not_fire_when_vix_low():
    with patch.object(mm, "_yahoo_chart_latest", return_value={"close": 18.0, "prev_closes": [18, 17, 16]}), \
         patch.object(mm, "_coingecko_btc_intraday", return_value=-8.0), \
         patch.object(mm, "emit_alert") as m_alert, \
         patch.object(mm, "halt_bot") as m_halt:
        mm.check_macro_kill_switch()
    assert m_alert.call_count == 0
    assert m_halt.call_count == 0


def test_kill_switch_does_not_fire_when_btc_drop_too_small():
    with patch.object(mm, "_yahoo_chart_latest", return_value={"close": 35.0, "prev_closes": [35, 30, 28]}), \
         patch.object(mm, "_coingecko_btc_intraday", return_value=-2.0), \
         patch.object(mm, "emit_alert") as m_alert, \
         patch.object(mm, "halt_bot") as m_halt:
        mm.check_macro_kill_switch()
    assert m_alert.call_count == 0
    assert m_halt.call_count == 0


def test_kill_switch_fires_alert_when_both_conditions_met_default_no_auto_halt():
    """Default behavior — alert only, no halt (Roy's preference)."""
    with patch.object(mm, "_yahoo_chart_latest", return_value={"close": 35.5, "prev_closes": [35, 30, 28]}), \
         patch.object(mm, "_coingecko_btc_intraday", return_value=-6.5), \
         patch.object(mm, "emit_alert") as m_alert, \
         patch.object(mm, "halt_bot") as m_halt, \
         patch.object(mm, "is_bot_halted", return_value=False), \
         patch.object(mm, "write_audit"), \
         patch.dict(mm.os.environ, {}, clear=False):
        if mm.KILL_SWITCH_AUTO_HALT_ENV in mm.os.environ:
            del mm.os.environ[mm.KILL_SWITCH_AUTO_HALT_ENV]
        mm.check_macro_kill_switch()
    assert m_alert.call_count == 1
    assert m_halt.call_count == 0  # no auto-halt by default
    args = m_alert.call_args.kwargs
    assert "MACRO SHOCK" in args["title"]
    assert args["metadata"]["auto_halt_enabled"] is False


def test_kill_switch_fires_auto_halt_when_env_flag_set():
    with patch.object(mm, "_yahoo_chart_latest", return_value={"close": 32.0, "prev_closes": [32, 28]}), \
         patch.object(mm, "_coingecko_btc_intraday", return_value=-7.2), \
         patch.object(mm, "emit_alert") as m_alert, \
         patch.object(mm, "halt_bot") as m_halt, \
         patch.object(mm, "is_bot_halted", return_value=False), \
         patch.object(mm, "write_audit"), \
         patch.dict(mm.os.environ, {mm.KILL_SWITCH_AUTO_HALT_ENV: "true"}):
        mm.check_macro_kill_switch()
    assert m_alert.call_count == 1
    assert m_halt.call_count == 2  # both bots halted (structure + copy)
    assert m_alert.call_args.kwargs["metadata"]["auto_halt_enabled"] is True


def test_kill_switch_skips_already_halted_bots():
    """When a bot is already halted, don't double-halt it."""
    def is_halted(bot_id):
        return bot_id == "structure"  # structure already halted

    with patch.object(mm, "_yahoo_chart_latest", return_value={"close": 35.0, "prev_closes": [35]}), \
         patch.object(mm, "_coingecko_btc_intraday", return_value=-6.0), \
         patch.object(mm, "emit_alert"), \
         patch.object(mm, "halt_bot") as m_halt, \
         patch.object(mm, "is_bot_halted", side_effect=is_halted), \
         patch.object(mm, "write_audit"), \
         patch.dict(mm.os.environ, {mm.KILL_SWITCH_AUTO_HALT_ENV: "true"}):
        mm.check_macro_kill_switch()
    # only copy halted (structure was already halted)
    assert m_halt.call_count == 1
    assert m_halt.call_args.args[0] == "copy"


def test_kill_switch_throttles_repeated_alerts():
    """Don't spam every 5 min during sustained shock."""
    with patch.object(mm, "_yahoo_chart_latest", return_value={"close": 35.0, "prev_closes": [35]}), \
         patch.object(mm, "_coingecko_btc_intraday", return_value=-6.0), \
         patch.object(mm, "emit_alert") as m_alert, \
         patch.object(mm, "halt_bot"), \
         patch.object(mm, "is_bot_halted", return_value=False), \
         patch.object(mm, "write_audit"):
        mm.check_macro_kill_switch()
        mm.check_macro_kill_switch()  # second call within throttle window
        mm.check_macro_kill_switch()
    assert m_alert.call_count == 1  # only the first fires


def test_kill_switch_silent_on_data_fetch_failure():
    """If VIX or BTC fetch fails, don't act and don't spam."""
    with patch.object(mm, "_yahoo_chart_latest", return_value=None), \
         patch.object(mm, "_coingecko_btc_intraday", return_value=-7.0), \
         patch.object(mm, "emit_alert") as m_alert:
        mm.check_macro_kill_switch()
    assert m_alert.call_count == 0


# ---------------------------------------------------------------------------
# Geo-shock alert
# ---------------------------------------------------------------------------

def test_geo_shock_does_not_fire_when_vix_low():
    """Sustained low VIX = no shock condition."""
    with patch.object(mm, "_yahoo_chart_latest") as m_yc, \
         patch.object(mm, "emit_alert") as m_alert:
        # Return different stub per symbol
        def by_symbol(sym):
            return {"close": 18.0, "prev_closes": [18, 17, 16, 15, 14]}
        m_yc.side_effect = lambda s: by_symbol(s)
        mm.check_geo_shock_alert()
    assert m_alert.call_count == 0


def test_geo_shock_fires_when_all_three_conditions_met():
    """VIX sustained > 20, DXY weakening, yields not rising."""
    def by_symbol(sym):
        if "VIX" in sym:
            return {"close": 25.0, "prev_closes": [24.0, 25.0, 26.0, 27.0, 25.0]}  # last 2: 27, 25 — both > 20
        if "DXY" in sym or "DX-Y" in sym:
            return {"close": 100.0, "prev_closes": [105.0, 103.0, 102.0, 101.0, 100.0]}  # weakening
        if "TNX" in sym:
            return {"close": 4.0, "prev_closes": [4.5, 4.3, 4.2, 4.1, 4.0]}  # falling
        return None

    with patch.object(mm, "_yahoo_chart_latest", side_effect=by_symbol), \
         patch.object(mm, "emit_alert") as m_alert:
        mm.check_geo_shock_alert()
    assert m_alert.call_count == 1
    assert m_alert.call_args.kwargs["event_type"] == "geo_shock_alert"


def test_geo_shock_does_not_fire_when_yields_rising():
    """Same VIX/DXY conditions but yields rising = no alert."""
    def by_symbol(sym):
        if "VIX" in sym:
            return {"close": 25.0, "prev_closes": [24.0, 25.0, 26.0, 27.0, 25.0]}
        if "DX" in sym:
            return {"close": 100.0, "prev_closes": [105.0, 103.0, 102.0, 101.0, 100.0]}
        if "TNX" in sym:
            return {"close": 4.5, "prev_closes": [4.0, 4.1, 4.2, 4.3, 4.5]}  # rising
        return None

    with patch.object(mm, "_yahoo_chart_latest", side_effect=by_symbol), \
         patch.object(mm, "emit_alert") as m_alert:
        mm.check_geo_shock_alert()
    assert m_alert.call_count == 0


def test_geo_shock_does_not_fire_when_vix_not_sustained():
    """VIX > 20 only on 1 day, not sustained 2+ days."""
    def by_symbol(sym):
        if "VIX" in sym:
            return {"close": 22.0, "prev_closes": [15.0, 16.0, 18.0, 19.0, 22.0]}  # only last day > 20
        if "DX" in sym:
            return {"close": 100.0, "prev_closes": [105.0, 100.0]}
        if "TNX" in sym:
            return {"close": 4.0, "prev_closes": [4.5, 4.0]}
        return None

    with patch.object(mm, "_yahoo_chart_latest", side_effect=by_symbol), \
         patch.object(mm, "emit_alert") as m_alert:
        mm.check_geo_shock_alert()
    assert m_alert.call_count == 0
