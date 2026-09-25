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

import discord
from discord.ext import tasks

from config import Config, ConfigError
from gifs import GifMaker
from mirror import Mirror
from sheets import SheetError, SheetSource

log = logging.getLogger("sheetmirror")


class MirrorBot(discord.Client):
    def __init__(self, cfg: Config, once: bool = False):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.message_content = bool(cfg.discord["message_content_intent"])
        super().__init__(intents=intents)
        self.cfg = cfg
        self.once = once
        self.mirror = Mirror(self, cfg, SheetSource(cfg), GifMaker(cfg))
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
            await interaction.response.send_message(
                f"Tracking {tracked} channels · polling every "
                f"{self.cfg.runtime['poll_seconds']}s · GIFs {gif_state}"
                f"{f' ({failing} failing)' if failing else ''} · {mode}",
                ephemeral=True,
            )

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
            return  # --once drives a single sync itself; no poll loop
        self.poll.change_interval(seconds=int(self.cfg.runtime["poll_seconds"]))
        self.poll.start()

    async def on_ready(self):
        log.info("Connected as %s (guild %s)", self.user, self.cfg.guild_id)

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
