#!/usr/bin/env python3
"""Discord <- Google Sheet channel mirror.

Runs as a long-lived service: every poll_seconds it reads the sheet and makes
the guild match it (create/rename/move/delete channels, post or edit the info
message in each one).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import time

import discord
from discord.ext import tasks

from config import Config, ConfigError
from dashboard import Dashboard, RingLogHandler
from gifs import GifMaker
from mirror import Mirror
from sheets import SheetError, SheetSource

log = logging.getLogger("sheetmirror")

# Server-wide permissions the bot's role needs. A bot cannot raise its own role's
# permissions, so these go into the invite link; on startup the bot checks them
# and logs a link that adds whatever is missing. Discord only lets the bot allow or
# deny a permission on a category if it holds that permission itself, which is why
# the thread and reaction permissions it denies to @everyone are listed too.
REQUIRED_PERMISSIONS = discord.Permissions(
    manage_channels=True,        # create/rename/move/delete channels and categories
    manage_roles=True,           # "Manage Permissions": set category/channel overwrites
    view_channel=True,
    send_messages=True,
    read_message_history=True,
    manage_messages=True,        # bulk delete for the rebuild path
    embed_links=True,
    attach_files=True,
    add_reactions=True,
    create_public_threads=True,
    create_private_threads=True,
    send_messages_in_threads=True,
)


class MirrorBot(discord.Client):
    def __init__(self, cfg: Config, once: bool = False):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.message_content = bool(cfg.discord["message_content_intent"])
        super().__init__(intents=intents)
        self.cfg = cfg
        self.once = once
        self.mirror = Mirror(self, cfg, SheetSource(cfg), GifMaker(cfg))
        self.log_ring = RingLogHandler(capacity=int(cfg.dashboard["log_lines"]))
        self.log_ring.setLevel(logging.INFO)
        logging.getLogger().addHandler(self.log_ring)
        self.dashboard = (
            Dashboard(self, cfg, self.log_ring) if cfg.dashboard["enabled"] and not once else None
        )
        self.tree = discord.app_commands.CommandTree(self)
        self._register_commands()

    def _register_commands(self) -> None:
        guild = discord.Object(id=self.cfg.guild_id)

        @self.tree.command(name="sync", description="Re-read the sheet and update channels now", guild=guild)
        @discord.app_commands.describe(force="Rebuild every channel and regenerate GIFs, ignoring caches")
        @discord.app_commands.checks.has_permissions(manage_channels=True)
        async def sync_now(interaction: discord.Interaction, force: bool = False):
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                stats = await self.mirror.sync(reason=f"/sync by {interaction.user}", force=force)
            except SheetError as exc:
                await interaction.followup.send(f"Sheet error: {exc}", ephemeral=True)
                return
            summary = ", ".join(f"{k}: {v}" for k, v in sorted(stats.items())) or "no changes"
            await interaction.followup.send(f"Sync complete — {summary}", ephemeral=True)

        @self.tree.command(name="mirror-status", description="Show mirror status", guild=guild)
        async def status(interaction: discord.Interaction):
            tracked = len(self.mirror.state.channels)
            mode = "dry-run" if self.mirror.dry_run else "live"
            gif_state = "on" if self.mirror.gifmaker.enabled else "off"
            failing = sum(1 for e in self.mirror.state.channels.values() if e.get("gif_error"))
            st = self.mirror.status
            hours = int(self.cfg.runtime["poll_seconds"]) / 3600
            lines = [
                f"**State** {'syncing now' if self.mirror.busy else st['state']} · {mode}",
                f"**Channels tracked** {tracked}",
                f"**Sheet rows** {st['rows_seen'] if st['rows_seen'] is not None else 'not read yet'}",
                f"**Automatic sync** every {hours:.0f}h",
                f"**Renders** {gif_state}" + (f" ({failing} failing)" if failing else ""),
            ]
            if st["last_finished"]:
                mins = (time.time() - float(st["last_finished"])) / 60
                summary = ", ".join(f"{v} {k}" for k, v in sorted(st["last_stats"].items()) if v) or "no changes"
                lines.append(f"**Last sync** {mins:.0f}m ago in {float(st['last_duration']):.0f}s — {summary}")
            if st["last_error"]:
                lines.append(f"**Last error** {str(st['last_error'])[:300]}")
            if self.dashboard is not None:
                lines.append(f"**Dashboard** port {self.cfg.dashboard['port']}")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        @self.tree.command(
            name="setup-server",
            description="Create the # and A-Z categories with the pack channel permissions",
            guild=guild,
        )
        @discord.app_commands.checks.has_permissions(manage_channels=True)
        async def setup_server(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            missing = self.missing_permissions(interaction.guild)
            if missing:
                await interaction.followup.send(
                    f"The bot's role is missing {', '.join(missing)}. "
                    f"Re-invite it with this link, then run this again:\n{self.invite_url()}",
                    ephemeral=True,
                )
                return
            stats = await self.mirror.setup_categories(interaction.guild)
            summary = ", ".join(f"{k}: {v}" for k, v in sorted(stats.items()))
            mode = " (dry-run, nothing changed)" if self.mirror.dry_run else ""
            text = f"Setup complete{mode} — {summary}"
            if stats["failed"]:
                text += "\nSome categories failed; see the bot log for details."
            await interaction.followup.send(text, ephemeral=True)

        async def on_tree_error(interaction: discord.Interaction, error: Exception) -> None:
            if isinstance(error, discord.app_commands.CheckFailure):
                text = "You need the Manage Channels permission to use this."
            else:
                log.exception("Slash command failed", exc_info=error)
                text = f"Command failed: {type(error).__name__}: {error}"
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(text, ephemeral=True)
                else:
                    await interaction.response.send_message(text, ephemeral=True)
            except discord.HTTPException:
                pass

        self.tree.on_error = on_tree_error

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.cfg.guild_id)
        try:
            synced = await self.tree.sync(guild=guild)
            log.info("Registered %d slash command(s): %s",
                     len(synced), ", ".join(sorted(c.name for c in synced)) or "none")
        except discord.HTTPException as exc:
            log.warning("Could not register slash commands: %s", exc)
        if self.once:
            return  # --once drives a single sync itself; no poll loop and no dashboard
        if self.dashboard is not None:
            await self.dashboard.start()
        interval = int(self.cfg.runtime["poll_seconds"])
        self.poll.change_interval(seconds=interval)
        self.poll.start()
        log.info("Automatic sync every %ds (%.1f hours)", interval, interval / 3600)

    def invite_url(self) -> str:
        return discord.utils.oauth_url(
            self.application_id,
            permissions=REQUIRED_PERMISSIONS,
            guild=discord.Object(id=self.cfg.guild_id),
            scopes=("bot", "applications.commands"),
        )

    def missing_permissions(self, guild: discord.Guild | None) -> list[str]:
        """REQUIRED_PERMISSIONS the bot's roles do not grant in this guild."""
        if guild is None or guild.me is None:
            return []
        have = guild.me.guild_permissions
        if have.administrator:
            return []
        return [name for name, wanted in REQUIRED_PERMISSIONS if wanted and not getattr(have, name)]

    async def on_ready(self):
        log.info("Connected as %s (guild %s)", self.user, self.cfg.guild_id)
        missing = self.missing_permissions(self.get_guild(self.cfg.guild_id))
        if missing:
            log.error(
                "The bot's role is missing server permissions: %s. Enable them under "
                "Server Settings -> Roles, or re-invite with this link: %s",
                ", ".join(missing), self.invite_url(),
            )

    async def close(self) -> None:
        if self.dashboard is not None:
            await self.dashboard.stop()
        await super().close()

    @tasks.loop(seconds=300)
    async def poll(self):
        try:
            await self.mirror.sync()
        except SheetError as exc:
            log.error("Sheet unavailable this cycle: %s", exc)
        except discord.HTTPException as exc:
            log.error("Discord API error: %s", exc)
        except Exception:  # keep the service alive
            log.exception("Unexpected error during sync")

    @poll.before_loop
    async def before_poll(self):
        await self.wait_until_ready()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true", help="sync a single time, then exit")
    parser.add_argument("--dry-run", action="store_true", help="log intended changes without making them")
    parser.add_argument("--connect-timeout", type=float, default=120.0,
                        help="seconds to wait for the Discord connection in --once mode")
    args = parser.parse_args()

    try:
        cfg = Config(args.config)
    except FileNotFoundError:
        print(f"No config file at {args.config}.", file=sys.stderr)
        print("Copy config.example.yaml to config.yaml and fill it in.", file=sys.stderr)
        return 2
    except ConfigError as exc:
        print("", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        print("", file=sys.stderr)
        print(f"Edit {args.config}, then run this again.", file=sys.stderr)
        return 2

    if args.dry_run:
        cfg.runtime["dry_run"] = True

    logging.basicConfig(
        level=getattr(logging, str(cfg.runtime["log_level"]).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)

    bot = MirrorBot(cfg, once=args.once)

    if args.once:
        return asyncio.run(run_once(bot, cfg, timeout=args.connect_timeout))

    bot.run(cfg.token, log_handler=None)
    return 0


async def run_once(bot: MirrorBot, cfg: Config, timeout: float = 120.0) -> int:
    """Connect, sync exactly once, disconnect. Used by --once / --dry-run."""
    rc = 0
    async with bot:
        runner = asyncio.create_task(bot.start(cfg.token), name="discord-start")
        ready = asyncio.create_task(bot.wait_until_ready(), name="wait-ready")
        done, _pending = await asyncio.wait(
            {runner, ready}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )

        try:
            if runner in done:
                ready.cancel()
                exc = runner.exception()
                if isinstance(exc, discord.LoginFailure):
                    log.error("Discord rejected the token: %s", exc)
                elif isinstance(exc, discord.PrivilegedIntentsRequired):
                    log.error(
                        "Privileged intent not enabled in the developer portal: %s. "
                        "Either enable Message Content, or set "
                        "discord.message_content_intent: false in config.yaml.", exc,
                    )
                elif exc is not None:
                    log.error("Could not connect: %s: %s", type(exc).__name__, exc)
                else:
                    log.error("Discord client stopped before it was ready")
                return 1

            if ready not in done:
                log.error("Timed out after %.0fs waiting for Discord to become ready", timeout)
                return 1

            try:
                await bot.mirror.sync(reason="--once")
            except SheetError as exc:
                log.error("Sheet unavailable: %s", exc)
                rc = 1
        finally:
            for task in (ready, runner):
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await runner
    return rc


if __name__ == "__main__":
    sys.exit(main())
