"""Daily report dispatcher.

Sends the rendered report to:
- Discord (#daily-report channel via webhook from the bot process via Redis pub/sub)
- Email (SMTP via STARTTLS)

The Discord side hands off to the alerting Discord bot via a Redis channel so
the bot connection lifecycle stays in one place.
"""
import json
import smtplib
from email.message import EmailMessage
from datetime import datetime, timezone
import redis

from framework.config import get_settings
from framework.audit import write_audit
from framework.logging_setup import get_logger
from framework.reporting.daily_report import build_summary, render_text, render_discord_embed

log = get_logger(__name__)

DISCORD_REPORT_CHANNEL = "alerts:daily_report"


def send_discord(text_body: str, embed: dict) -> None:
    settings = get_settings()
    payload = {
        "channel_id": settings.discord_daily_report_channel_id,
        "content": text_body,
        "embed": embed,
    }
    r = redis.Redis.from_url(settings.redis_url)
    r.publish(DISCORD_REPORT_CHANNEL, json.dumps(payload))


def send_email(text_body: str) -> None:
    settings = get_settings()
    if not (settings.smtp_host and settings.smtp_user and settings.smtp_to):
        log.warning("smtp_skipped", reason="missing config")
        return
    msg = EmailMessage()
    msg["Subject"] = f"Fleet daily check-in — {datetime.now(timezone.utc).date()}"
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = settings.smtp_to
    msg.set_content(text_body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)


def send_daily_report() -> None:
    settings = get_settings()
    summary = build_summary(
        window_hours=24,
        deployed_capital_usd=settings.deployed_capital_usd,
    )
    text_body = render_text(summary)
    embed = render_discord_embed(summary)

    try:
        send_discord(text_body=text_body, embed=embed)
    except Exception:
        log.exception("daily_discord_failed")

    try:
        send_email(text_body=text_body)
    except Exception:
        log.exception("daily_email_failed")

    write_audit(
        "daily_report_sent",
        payload={
            "bots": len(summary.bots),
            "fleet_trades_24h": summary.fleet_trades_24h,
            "fleet_signals_24h": summary.fleet_signals_24h,
            "fleet_halts_24h": summary.fleet_halts_24h,
        },
    )
    log.info("daily_report_sent", bots=len(summary.bots))
