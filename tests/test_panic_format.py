"""Lightweight tests for /panic confirmation formatting (no DB, no Redis)."""
from framework.panic import format_summary


def test_format_summary_full_payload():
    summary = {
        "actor": "discord:roy",
        "started_at": "2026-04-29T07:00:00+00:00",
        "completed_at": "2026-04-29T07:00:01+00:00",
        "bots_halted": ["structure", "copy"],
        "open_trades": 3,
        "panic_dispatched_to": ["structure", "copy", "event", "sniper"],
    }
    text = format_summary(summary)
    assert "PANIC EXECUTED" in text
    assert "discord:roy" in text
    assert "structure, copy" in text
    assert "open trades at trigger: 3" in text


def test_format_summary_no_bots_halted():
    summary = {
        "actor": "telegram:roy",
        "started_at": "2026-04-29T07:00:00+00:00",
        "completed_at": "2026-04-29T07:00:01+00:00",
        "bots_halted": [],
        "open_trades": 0,
        "panic_dispatched_to": [],
    }
    text = format_summary(summary)
    assert "(none)" in text


def test_format_summary_with_redis_error():
    summary = {
        "actor": "manual",
        "started_at": "2026-04-29T07:00:00+00:00",
        "completed_at": "2026-04-29T07:00:01+00:00",
        "bots_halted": ["sniper"],
        "open_trades": 1,
        "panic_dispatched_to": [],
        "redis_dispatch_error": "ConnectionRefusedError",
    }
    text = format_summary(summary)
    assert "redis error: ConnectionRefusedError" in text
