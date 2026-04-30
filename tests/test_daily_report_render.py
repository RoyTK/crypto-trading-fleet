"""Render-only tests for the daily report (no DB)."""
from framework.reporting.daily_report import (
    BotSummary, FleetSummary, render_text, render_discord_embed,
)


def _empty_summary() -> FleetSummary:
    return FleetSummary(
        generated_at="2026-04-29T07:00:00+00:00",
        window_hours=24,
        bots=[],
        fleet_signals_24h=0,
        fleet_trades_24h=0,
        fleet_halts_24h=0,
        deployed_capital_usd=0.0,
    )


def _populated_summary() -> FleetSummary:
    bots = [
        BotSummary(
            bot_id="structure", state="paper",
            paper_clock_day=14, promotion_score=0.812,
            trades_24h=5, pnl_24h_usd=42.10, pnl_24h_pct=None,
            open_positions=1, halts_24h=0,
            last_score_at="2026-04-29T06:45:00+00:00",
        ),
        BotSummary(
            bot_id="copy", state="shakedown",
            paper_clock_day=None, promotion_score=None,
            trades_24h=0, pnl_24h_usd=0.0, pnl_24h_pct=None,
            open_positions=0, halts_24h=0,
            last_score_at=None,
        ),
    ]
    return FleetSummary(
        generated_at="2026-04-29T07:00:00+00:00",
        window_hours=24,
        bots=bots,
        fleet_signals_24h=12,
        fleet_trades_24h=5,
        fleet_halts_24h=0,
        deployed_capital_usd=1500.0,
    )


def test_render_text_empty_fleet_does_not_crash():
    text = render_text(_empty_summary())
    assert "Daily check-in" in text
    assert "Per-bot:" in text


def test_render_text_populated_includes_bot_rows():
    text = render_text(_populated_summary())
    assert "structure" in text
    assert "copy" in text
    assert "0.812" in text
    assert "$1,500.00" in text


def test_render_discord_embed_empty_fleet():
    embed = render_discord_embed(_empty_summary())
    assert embed["title"] == "Daily check-in"
    assert "(no bots yet)" in embed["description"]


def test_render_discord_embed_populated():
    embed = render_discord_embed(_populated_summary())
    assert "structure" in embed["description"]
    assert "copy" in embed["description"]
    fleet_field = next(f for f in embed["fields"] if f["name"] == "Fleet 24h")
    assert "signals: 12" in fleet_field["value"]
