"""RB3 — alert emission with a direct-Discord fallback for critical alerts.

Every alert in the fleet is fire-and-forget: the emitter does
`redis.publish("alerts:emit", ...)` and the single `alerting` dispatcher process
(holding the Discord bot GATEWAY connection) fans it out. That dispatcher is a
single point of failure — and the worst case is self-referential: when the
dispatcher dies, the watchdog notices its stale `alerting` heartbeat and tries to
alert THROUGH the dead dispatcher. Redis pub/sub is fire-and-forget, so with no
subscriber the message is silently dropped. The one alert that matters most can't
get out.

This module adds a stateless fallback: for P0/P1 alerts, if the publish did not
reach a live dispatcher (0 subscribers, or Redis itself is down), POST the message
straight to the Discord channel via the REST API using the EXISTING bot token — no
gateway, no dispatcher, no new credential. `redis.publish` conveniently returns the
subscriber count, which is exactly the "did the dispatcher receive this?" signal, so
there is zero duplication in the normal case (dispatcher up -> count >= 1 -> no
fallback).
"""
from __future__ import annotations

import json
import urllib.request
from typing import Optional

import redis

from framework.config import get_settings
from framework.logging_setup import get_logger

log = get_logger(__name__)

ALERT_EMIT_CHANNEL = "alerts:emit"
CRITICAL_SEVERITIES = ("p0", "p1")
_DISCORD_API = "https://discord.com/api/v10"


def should_fallback(publish_ok: bool, subscriber_count: int) -> bool:
    """Fire the direct-Discord fallback iff the dispatcher did NOT receive the alert:
    the publish raised (Redis unreachable) or nobody was subscribed (dispatcher dead
    / restarting). Pure so it's unit-testable."""
    return (not publish_ok) or (subscriber_count <= 0)


def format_direct_content(
    severity: str, title: str, body: str,
    bot_id: Optional[str], owner_user_id: Optional[str],
) -> str:
    """Build the raw Discord message for the direct path. Mirrors DiscordConnector's
    format, tagged (direct) so a fallback message is distinguishable from the normal
    dispatcher output. Truncated to Discord's practical single-message limit."""
    mention = f"<@{owner_user_id}> " if owner_user_id else ""
    tag = f"[{bot_id}] " if bot_id else ""
    return f"{mention}**[{severity.upper()}]** (direct) {tag}{title}\n{body}"[:1900]


def post_discord_direct(bot_token: str, channel_id: str, content: str) -> int:
    """POST a message to a channel via the Discord REST API using a bot token.
    Stateless — works from any process without the gateway. Returns the HTTP status.
    Raises on network/HTTP error (caller catches)."""
    req = urllib.request.Request(
        f"{_DISCORD_API}/channels/{channel_id}/messages",
        data=json.dumps({"content": content[:2000]}).encode("utf-8"),
        headers={
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
            # Discord requires a descriptive User-Agent on bot REST calls.
            "User-Agent": "DiscordBot (https://github.com/RoyTK/crypto-trading-fleet, 1.0)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.getcode()


def emit_alert(
    severity: str,
    title: str,
    body: str,
    *,
    bot_id: Optional[str] = None,
    event_type: str = "generic",
    metadata: Optional[dict] = None,
    redis_client: Optional["redis.Redis"] = None,
) -> None:
    """Publish an alert to the dispatcher, with a direct-Discord fallback for P0/P1.

    Normal path (dispatcher up): publish to alerts:emit; the dispatcher fans out to
    Discord/Telegram/Twilio per the taxonomy. Fallback path (dispatcher down or Redis
    unreachable) for P0/P1 only: POST straight to Discord via the bot-token REST API
    so the alert still reaches Roy. Never raises — an alerting failure must not crash
    the caller.
    """
    s = get_settings()
    payload = {
        "severity": severity,
        "title": title,
        "body": body,
        "bot_id": bot_id,
        "event_type": event_type,
        "metadata": metadata or {},
    }

    publish_ok = False
    subscribers = 0
    try:
        r = redis_client or redis.Redis.from_url(s.redis_url)
        subscribers = int(r.publish(ALERT_EMIT_CHANNEL, json.dumps(payload)))
        publish_ok = True
    except Exception as e:
        log.warning("alert_emit_publish_failed", event_type=event_type, error=str(e))

    if severity not in CRITICAL_SEVERITIES:
        return
    if not should_fallback(publish_ok, subscribers):
        return

    # The dispatcher did not receive this critical alert — go direct.
    if not (s.discord_bot_token and s.discord_alert_channel_id):
        log.warning("alert_direct_discord_unconfigured", event_type=event_type,
                    note="dispatcher unreachable and no bot token/channel for fallback")
        return
    try:
        content = format_direct_content(
            severity, title, body, bot_id, s.discord_owner_user_id
        )
        status = post_discord_direct(s.discord_bot_token, s.discord_alert_channel_id, content)
        log.info("alert_direct_discord_sent", severity=severity, event_type=event_type,
                 http_status=status, subscribers=subscribers, publish_ok=publish_ok)
    except Exception:
        log.exception("alert_direct_discord_failed", event_type=event_type)
