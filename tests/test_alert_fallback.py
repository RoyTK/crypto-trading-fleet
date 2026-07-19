"""RB3 regression guard — direct-Discord fallback for critical alerts.

The fleet's whole alert path funnels through one `alerting` dispatcher process. When
it dies, the watchdog tries to report that death THROUGH the dead dispatcher and the
message is silently dropped. RB3 sends P0/P1 alerts straight to Discord (bot-token
REST, no gateway) whenever the publish didn't reach a live dispatcher. These pin the
decision logic, the message format, and the trigger behavior.
"""
from __future__ import annotations

import types

import pytest

from framework.alert_emit import (
    should_fallback,
    format_direct_content,
    CRITICAL_SEVERITIES,
)
import framework.alert_emit as ae


# ---- should_fallback -----------------------------------------------------

def test_no_fallback_when_dispatcher_subscribed():
    # Normal case: publish OK, >=1 subscriber -> dispatcher has it, no direct post.
    assert should_fallback(True, 1) is False
    assert should_fallback(True, 3) is False


def test_fallback_when_zero_subscribers():
    # Dispatcher dead / restarting: published fine but nobody received it.
    assert should_fallback(True, 0) is True


def test_fallback_when_publish_failed():
    # Redis itself unreachable -> the dispatcher couldn't have gotten it either.
    assert should_fallback(False, 0) is True


# ---- format_direct_content -----------------------------------------------

def test_format_includes_mention_and_markers():
    out = format_direct_content("p0", "halt fired", "cluster dd -30%", "copy_cluster", "42")
    assert out.startswith("<@42> ")
    assert "**[P0]**" in out
    assert "(direct)" in out          # distinguishes fallback from normal dispatcher output
    assert "[copy_cluster]" in out
    assert "halt fired" in out and "cluster dd -30%" in out


def test_format_no_mention_when_owner_missing():
    out = format_direct_content("p1", "t", "b", None, "")
    assert not out.startswith("<@")
    assert "**[P1]**" in out


def test_format_truncates_long_body():
    out = format_direct_content("p1", "t", "x" * 5000, None, None)
    assert len(out) <= 1900


# ---- emit_alert trigger behavior -----------------------------------------

class _FakeRedis:
    def __init__(self, ret):
        self._ret = ret          # int subscriber count, or an Exception to raise
        self.published = []

    def publish(self, channel, data):
        self.published.append((channel, data))
        if isinstance(self._ret, Exception):
            raise self._ret
        return self._ret


@pytest.fixture
def capture_direct(monkeypatch):
    """Stub the network POST + settings so we can assert whether the fallback fired."""
    calls = []
    monkeypatch.setattr(ae, "post_discord_direct",
                        lambda token, channel, content: calls.append((token, channel, content)) or 200)
    fake_settings = types.SimpleNamespace(
        redis_url="redis://x",
        discord_bot_token="tok",
        discord_alert_channel_id="123",
        discord_owner_user_id="42",
    )
    monkeypatch.setattr(ae, "get_settings", lambda: fake_settings)
    return calls


def test_emit_p1_fires_direct_when_no_subscriber(capture_direct):
    ae.emit_alert("p1", "t", "b", redis_client=_FakeRedis(0))
    assert len(capture_direct) == 1


def test_emit_p1_no_direct_when_dispatcher_up(capture_direct):
    ae.emit_alert("p1", "t", "b", redis_client=_FakeRedis(1))
    assert capture_direct == []


def test_emit_p0_fires_direct_when_redis_down(capture_direct):
    ae.emit_alert("p0", "t", "b", redis_client=_FakeRedis(RuntimeError("redis down")))
    assert len(capture_direct) == 1


def test_emit_p2_never_fires_direct(capture_direct):
    # Non-critical: no fallback even with zero subscribers (avoid direct-post spam).
    ae.emit_alert("p2", "t", "b", redis_client=_FakeRedis(0))
    assert capture_direct == []
    assert "p2" not in CRITICAL_SEVERITIES


def test_emit_unconfigured_token_skips_direct(monkeypatch):
    # Dispatcher down AND no bot token/channel -> degrade gracefully, no crash.
    calls = []
    monkeypatch.setattr(ae, "post_discord_direct",
                        lambda *a: calls.append(a) or 200)
    monkeypatch.setattr(ae, "get_settings", lambda: types.SimpleNamespace(
        redis_url="redis://x", discord_bot_token="", discord_alert_channel_id="",
        discord_owner_user_id="",
    ))
    ae.emit_alert("p0", "t", "b", redis_client=_FakeRedis(0))
    assert calls == []
