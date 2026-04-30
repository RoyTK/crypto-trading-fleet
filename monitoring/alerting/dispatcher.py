"""Alert dispatcher.

Subscribes to Redis channels:
- alerts:emit          → AlertEvent JSON; routes per taxonomy
- alerts:heartbeat     → watchdog dispatches; built-in severity routing
- alerts:daily_report  → daily report (Discord embed + text)
- panic:execute:*      → handled by per-bot executors, NOT here

Phase 0: Discord, Telegram, Twilio connectors are stubs that send to channel
IDs / chat IDs configured in .env. If creds are missing, the connector logs
'skipped' rather than crashing — lets us run the dispatcher in local dev with
no external creds.
"""
import json
import asyncio
from typing import Any
import redis.asyncio as redis_async

from framework.config import get_settings
from framework.logging_setup import get_logger, configure_logging
from framework.audit import write_audit
from framework.heartbeat import ping
from monitoring.alerting.taxonomy import AlertEvent, Severity, channels_for
from monitoring.alerting.connectors.discord_connector import DiscordConnector
from monitoring.alerting.connectors.telegram_connector import TelegramConnector
from monitoring.alerting.connectors.twilio_connector import TwilioConnector

log = get_logger(__name__)

PROCESS_NAME = "alerting"


class Dispatcher:
    def __init__(self) -> None:
        self.discord = DiscordConnector()
        self.telegram = TelegramConnector()
        self.twilio = TwilioConnector()
        settings = get_settings()
        self.redis = redis_async.from_url(settings.redis_url, decode_responses=True)

    async def start(self) -> None:
        await self.discord.start()
        await self.telegram.start()

        pubsub = self.redis.pubsub()
        await pubsub.subscribe("alerts:emit", "alerts:heartbeat", "alerts:daily_report")

        ping(PROCESS_NAME, {"role": "alerting"})
        write_audit("alerting_started")

        log.info("dispatcher_listening")
        async for msg in pubsub.listen():
            if msg["type"] != "message":
                continue
            channel = msg["channel"]
            try:
                payload = json.loads(msg["data"])
            except Exception:
                log.warning("alert_payload_bad", channel=channel)
                continue

            try:
                if channel == "alerts:emit":
                    await self._handle_emit(payload)
                elif channel == "alerts:heartbeat":
                    await self._handle_heartbeat(payload)
                elif channel == "alerts:daily_report":
                    await self._handle_daily_report(payload)
            except Exception:
                log.exception("alert_handling_failed", channel=channel)

    async def stop(self) -> None:
        await self.discord.stop()
        await self.telegram.stop()
        await self.redis.aclose()

    # -- Handlers ----------------------------------------------------------

    async def _handle_emit(self, payload: dict[str, Any]) -> None:
        try:
            sev = Severity(payload["severity"])
        except Exception:
            log.warning("alert_emit_bad_severity", payload=payload)
            return
        event = AlertEvent(
            severity=sev,
            title=payload.get("title", ""),
            body=payload.get("body", ""),
            bot_id=payload.get("bot_id"),
            event_type=payload.get("event_type", "generic"),
            metadata=payload.get("metadata", {}),
        )
        await self._fanout(event)

    async def _handle_heartbeat(self, payload: dict[str, Any]) -> None:
        sev = Severity(payload.get("severity", "p1"))
        process = payload.get("process", "unknown")
        silent = payload.get("silent_seconds", 0)
        event = AlertEvent(
            severity=sev,
            title=f"heartbeat silent: {process}",
            body=f"{process} silent for {silent:.0f}s",
            event_type="heartbeat",
            metadata=payload,
        )
        await self._fanout(event)

    async def _handle_daily_report(self, payload: dict[str, Any]) -> None:
        await self.discord.send_embed(
            channel_id=payload["channel_id"],
            content=payload.get("content", ""),
            embed=payload.get("embed", {}),
        )

    async def _fanout(self, event: AlertEvent) -> None:
        targets = channels_for(event.severity)
        log.info(
            "alert_dispatch",
            severity=event.severity, title=event.title,
            targets=targets, bot=event.bot_id,
        )

        if "twilio" in targets:
            await self.twilio.send(f"[{event.severity.upper()}] {event.title}\n{event.body}")
        if "discord_ping" in targets:
            await self.discord.send_alert(event, mention_owner=True)
        if "discord" in targets:
            await self.discord.send_alert(event, mention_owner=False)
        if "telegram" in targets:
            await self.telegram.send_alert(event)


async def _main() -> None:
    configure_logging()
    d = Dispatcher()
    try:
        await d.start()
    finally:
        await d.stop()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
