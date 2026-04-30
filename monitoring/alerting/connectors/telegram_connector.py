"""Telegram connector.

Mirror of Discord — exposes /panic and /status, sends alerts. Owner check
on /panic is enforced via TELEGRAM_OWNER_USER_ID.
"""
import asyncio
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes,
)

from framework.config import get_settings
from framework.logging_setup import get_logger
from framework.panic import trigger_panic, format_summary
from framework.audit import write_audit
from monitoring.alerting.taxonomy import AlertEvent

log = get_logger(__name__)


class TelegramConnector:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.app: Optional[Application] = None
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()

    async def start(self) -> None:
        if not self.settings.telegram_bot_token:
            log.warning("telegram_skipped", reason="no token configured")
            return

        self.app = ApplicationBuilder().token(self.settings.telegram_bot_token).build()
        self.app.add_handler(CommandHandler("panic", self._panic))
        self.app.add_handler(CommandHandler("status", self._status))

        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        self._ready.set()
        log.info("telegram_ready")

    async def stop(self) -> None:
        if self.app is not None:
            try:
                await self.app.updater.stop()
                await self.app.stop()
                await self.app.shutdown()
            except Exception:
                log.exception("telegram_stop_failed")

    # --- Commands ----------------------------------------------------------

    async def _panic(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        owner_id = self.settings.telegram_owner_user_id
        user = update.effective_user
        if not user or str(user.id) != owner_id:
            if update.message:
                await update.message.reply_text("Not authorized.")
            write_audit(
                "panic_denied",
                actor=f"telegram:{user.id if user else 'unknown'}",
                note="unauthorized /panic",
            )
            return
        if update.message:
            await update.message.reply_text("PANIC starting...")
        summary = trigger_panic(actor=f"telegram:{user.id}")
        if update.message:
            await update.message.reply_text(f"```\n{format_summary(summary)}\n```", parse_mode="Markdown")

    async def _status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        owner_id = self.settings.telegram_owner_user_id
        user = update.effective_user
        if not user or str(user.id) != owner_id:
            if update.message:
                await update.message.reply_text("Not authorized.")
            return
        from framework.reporting.daily_report import build_summary, render_text
        summary = build_summary(window_hours=24, deployed_capital_usd=self.settings.deployed_capital_usd)
        text = render_text(summary)
        if update.message:
            await update.message.reply_text(f"```\n{text}\n```", parse_mode="Markdown")

    # --- Outbound ----------------------------------------------------------

    async def send_alert(self, event: AlertEvent) -> None:
        if self.app is None or not self._ready.is_set():
            log.warning("telegram_send_skipped", reason="not ready")
            return
        owner_id = self.settings.telegram_owner_user_id
        if not owner_id:
            return
        bot_tag = f"[{event.bot_id}] " if event.bot_id else ""
        msg = f"[{event.severity.upper()}] {bot_tag}{event.title}\n{event.body}"
        try:
            await self.app.bot.send_message(chat_id=int(owner_id), text=msg[:3500])
        except Exception:
            log.exception("telegram_send_failed")
