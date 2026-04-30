"""Discord connector.

Lifecycle:
- start(): connects to Discord gateway, registers /panic and /status slash commands
- send_alert(): posts to alert channel; mention_owner=True pings Roy on P0/P1
- send_embed(): posts a structured embed (used for daily report)

The /panic slash command checks the invoker's user ID against
DISCORD_OWNER_USER_ID before executing. Anyone else gets a denial.
"""
from typing import Any, Optional
import asyncio
import discord
from discord import app_commands

from framework.config import get_settings
from framework.logging_setup import get_logger
from framework.panic import trigger_panic, format_summary
from framework.audit import write_audit
from monitoring.alerting.taxonomy import AlertEvent, Severity

log = get_logger(__name__)


class DiscordConnector:
    def __init__(self) -> None:
        self.settings = get_settings()
        intents = discord.Intents.default()
        intents.message_content = True
        self.client: Optional[discord.Client] = None
        self.tree: Optional[app_commands.CommandTree] = None
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()

    async def start(self) -> None:
        if not self.settings.discord_bot_token:
            log.warning("discord_skipped", reason="no token configured")
            return

        self.client = discord.Client(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self.client)
        self._register_commands()

        @self.client.event
        async def on_ready() -> None:
            try:
                guild_id = int(self.settings.discord_guild_id) if self.settings.discord_guild_id else None
                if guild_id:
                    guild = discord.Object(id=guild_id)
                    self.tree.copy_global_to(guild=guild)
                    await self.tree.sync(guild=guild)
                else:
                    await self.tree.sync()
                log.info("discord_ready", user=str(self.client.user))
                self._ready.set()
            except Exception:
                log.exception("discord_command_sync_failed")

        async def _run():
            try:
                await self.client.start(self.settings.discord_bot_token)
            except Exception:
                log.exception("discord_run_failed")

        self._task = asyncio.create_task(_run())
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=20)
        except asyncio.TimeoutError:
            log.warning("discord_ready_timeout")

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.close()
        if self._task is not None:
            self._task.cancel()

    def _register_commands(self) -> None:
        owner_id = self.settings.discord_owner_user_id

        @self.tree.command(name="panic", description="Halt all bots, cancel orders, close positions")
        async def panic_cmd(interaction: discord.Interaction) -> None:
            if str(interaction.user.id) != owner_id:
                await interaction.response.send_message(
                    "Not authorized.", ephemeral=True,
                )
                write_audit(
                    "panic_denied",
                    actor=f"discord:{interaction.user.id}",
                    note=f"unauthorized /panic from {interaction.user}",
                )
                return

            await interaction.response.send_message("PANIC starting...", ephemeral=False)
            summary = trigger_panic(actor=f"discord:{interaction.user.id}")
            await interaction.followup.send(f"```\n{format_summary(summary)}\n```")

        @self.tree.command(name="status", description="Show fleet status")
        async def status_cmd(interaction: discord.Interaction) -> None:
            from framework.reporting.daily_report import build_summary, render_text
            summary = build_summary(window_hours=24, deployed_capital_usd=self.settings.deployed_capital_usd)
            text = render_text(summary)
            await interaction.response.send_message(f"```\n{text}\n```", ephemeral=True)

    # --- Outbound helpers -------------------------------------------------

    async def send_alert(self, event: AlertEvent, *, mention_owner: bool) -> None:
        if self.client is None or not self._ready.is_set():
            log.warning("discord_send_skipped", reason="not ready")
            return
        channel_id = self.settings.discord_alert_channel_id
        if not channel_id:
            log.warning("discord_send_skipped", reason="no alert channel")
            return
        channel = self.client.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.client.fetch_channel(int(channel_id))
            except Exception:
                log.exception("discord_channel_fetch_failed")
                return

        prefix = ""
        if mention_owner and self.settings.discord_owner_user_id:
            prefix = f"<@{self.settings.discord_owner_user_id}> "
        bot_tag = f"[{event.bot_id}] " if event.bot_id else ""
        msg = f"{prefix}**[{event.severity.upper()}]** {bot_tag}{event.title}\n{event.body}"
        try:
            await channel.send(msg[:1900])
            log.info("discord_alert_sent", severity=event.severity.value, title=event.title)
        except Exception:
            log.exception("discord_send_failed")

    async def send_embed(self, *, channel_id: str, content: str, embed: dict[str, Any]) -> None:
        if self.client is None or not self._ready.is_set():
            log.warning("discord_embed_skipped", reason="not ready")
            return
        try:
            ch = self.client.get_channel(int(channel_id)) or await self.client.fetch_channel(int(channel_id))
        except Exception:
            log.exception("discord_embed_channel_fetch_failed")
            return
        try:
            await ch.send(
                content=content[:1900] if content else None,
                embed=discord.Embed.from_dict(embed) if embed else None,
            )
            log.info("discord_embed_sent", channel_id=channel_id)
        except Exception:
            log.exception("discord_embed_send_failed")
