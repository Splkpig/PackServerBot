"""Reconciles a Google Sheet against Discord channels."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import discord

log = logging.getLogger(__name__)

MAX_TOPIC = 1024
BULK_DELETE_AGE = timedelta(days=13, hours=12)  # Discord bulk delete cuts off at 14 days


def slugify(name: str) -> str:
    """'Andrecks Vapor (Eveoi Additions)' -> 'andrecks-vapor-eveoi-additions'"""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    text = text.lower().replace("&", " and ").replace("+", " plus ")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text[:100] or "unnamed"


def digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


@dataclass
class Desired:
    key: str
    channel_name: str
    category: Optional[str]
    body: str
    topic: Optional[str]
    private: bool
    download: str = ""
    row: dict = field(default_factory=dict)


class State:
    """Remembers which channels and messages this bot owns, so it never
    deletes anything it did not create or adopt."""

    def __init__(self, path: str):
        self.path = path
        self.channels: Dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.channels = data.get("channels", {})
        except (OSError, ValueError) as exc:
            log.error("Could not read state file %s (%s); starting empty", self.path, exc)

    def save(self) -> None:
        payload = json.dumps({"version": 1, "channels": self.channels}, indent=2)
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.error("Could not write state file: %s", exc)
            if os.path.exists(tmp):
                os.unlink(tmp)


class Mirror:
    def __init__(self, bot: discord.Client, cfg, source, gifmaker=None):
        self.bot = bot
        self.cfg = cfg
        self.source = source
        self.gifmaker = gifmaker
        self.state = State(cfg.runtime["state_file"])
        self.dry_run = bool(cfg.runtime["dry_run"])
        self._lock = asyncio.Lock()
        self._gif_budget = 0

    # ---------- building the desired picture ----------

    def _category_for(self, slug: str) -> Optional[str]:
        if self.cfg.discord["category_mode"] == "none":
            return None
        first = slug[0]
        return first.upper() if first.isalpha() else self.cfg.discord["other_category"]

    def _is_private(self, row: dict) -> bool:
        field_name = self.cfg.discord["private_field"]
        wanted = [v.lower() for v in self.cfg.discord["private_values"]]
        return row.get(field_name, "").strip().lower() in wanted

    def _build_body(self, row: dict) -> str:
        msg = self.cfg.message
        lines = []
        for name in msg["fields"]:
            value = row.get(name, "").strip()
            if value.lower() in [v.lower() for v in msg["omit_values"]]:
                if msg["omit_empty"]:
                    continue
                value = msg["empty_placeholder"]
            label = msg["labels"].get(name, name.title())
            lines.append(f"{label}: {value}")
        return "\n".join(lines)

    def _build_topic(self, row: dict) -> Optional[str]:
        if not self.cfg.discord["set_topic"]:
            return None
        try:
            topic = self.cfg.discord["topic_template"].format(**row)
        except KeyError as exc:
            log.warning("topic_template references unknown field %s", exc)
            return None
        return topic.strip()[:MAX_TOPIC] or None

    def _download_for(self, row: dict) -> str:
        if not (self.gifmaker and self.gifmaker.enabled):
            return ""
        value = row.get(self.cfg.gif["download_field"], "").strip()
        if value.lower() in [v.lower() for v in self.cfg.message["omit_values"]]:
            return ""
        if not value.lower().startswith(("http://", "https://")):
            return ""
        return value

    def build_desired(self, rows: List[dict]) -> Dict[str, Desired]:
        desired: Dict[str, Desired] = {}
        used_names: Dict[str, str] = {}
        for row in rows:
            slug = slugify(row["name"])
            key = row.get("_key") or slug
            channel_name = slug
            if channel_name in used_names and used_names[channel_name] != key:
                suffix = 2
                while f"{slug}-{suffix}"[:100] in used_names:
                    suffix += 1
                channel_name = f"{slug}-{suffix}"[:100]
                log.warning(
                    "Duplicate channel name for row %s (%r); using #%s",
                    row.get("_row_number"), row["name"], channel_name,
                )
            used_names[channel_name] = key
            desired[key] = Desired(
                key=key,
                channel_name=channel_name,
                category=self._category_for(channel_name),
                body=self._build_body(row),
                topic=self._build_topic(row),
                private=self._is_private(row),
                download=self._download_for(row),
                row=row,
            )
        return desired

    # ---------- Discord helpers ----------

    def _protected(self, channel: discord.abc.GuildChannel) -> bool:
        if channel.name in self.cfg.discord["protected_channels"]:
            return True
        category = getattr(channel, "category", None)
        return bool(category and category.name in self.cfg.discord["protected_categories"])

    def _overwrites(self, guild: discord.Guild, private: bool):
        if self.cfg.discord["private_mode"] != "lock":
            return None
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=not private)}
        if private:
            for role_id in self.cfg.discord["private_role_ids"]:
                role = guild.get_role(int(role_id))
                if role:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True)
        return overwrites

    async def _ensure_category(self, guild: discord.Guild, name: Optional[str]):
        if name is None:
            return None
        for category in guild.categories:
            if category.name == name:
                return category
        if self.dry_run:
            log.info("[dry-run] would create category %s", name)
            return None
        category = await guild.create_category(name=name, reason="sheet mirror: new category")
        log.info("Created category %s", name)
        return category

    async def _purge(self, channel: discord.TextChannel) -> int:
        """Clear the channel before rebuilding it."""
        mode = self.cfg.discord["purge_mode"]
        limit = int(self.cfg.discord["purge_limit"])
        cutoff = datetime.now(timezone.utc) - BULK_DELETE_AGE

        try:
            candidates = [
                m async for m in channel.history(limit=limit)
                if mode == "all" or m.author.id == self.bot.user.id
            ]
        except discord.Forbidden:
            log.warning("No read access to #%s; cannot rebuild", channel.name)
            return 0
        if not candidates:
            return 0

        if self.dry_run:
            log.info("[dry-run] would delete %d message(s) in #%s", len(candidates), channel.name)
            return len(candidates)

        recent = [m for m in candidates if m.created_at > cutoff]
        old = [m for m in candidates if m.created_at <= cutoff]
        removed = 0

        for start in range(0, len(recent), 100):
            chunk = recent[start:start + 100]
            try:
                if len(chunk) == 1:
                    await chunk[0].delete()
                else:
                    await channel.delete_messages(chunk, reason="sheet mirror: row changed")
                removed += len(chunk)
            except discord.HTTPException as exc:
                log.warning("Bulk delete failed in #%s: %s", channel.name, exc)

        for message in old:  # older than 14 days: one at a time
            try:
                await message.delete()
                removed += 1
                await asyncio.sleep(0.4)
            except discord.HTTPException:
                pass

        log.info("Cleared %d message(s) from #%s", removed, channel.name)
        return removed

    async def _post_gifs(self, channel: discord.TextChannel, paths: List[str]) -> List[int]:
        cap = float(self.cfg.gif["max_upload_mb"]) * 1024 * 1024
        per_message = max(1, min(10, int(self.cfg.gif["gifs_per_message"])))

        sendable = []
        for path in paths:
            size = os.path.getsize(path)
            if size > cap:
                log.warning("Skipping %s — %.1f MB, over the %s MB upload limit",
                            os.path.basename(path), size / 1048576,
                            self.cfg.gif["max_upload_mb"])
                continue
            sendable.append(path)

        if self.dry_run:
            log.info("[dry-run] would upload %d page(s) to #%s", len(sendable), channel.name)
            return []

        ids = []
        for start in range(0, len(sendable), per_message):
            batch = sendable[start:start + per_message]
            files = [discord.File(p, filename=os.path.basename(p)) for p in batch]
            try:
                message = await channel.send(files=files)
                ids.append(message.id)
            except discord.HTTPException as exc:
                log.error("Could not upload to #%s: %s", channel.name, exc)
        return ids

    async def _ensure_content(self, channel: discord.TextChannel, want: Desired,
                              entry: dict, force: bool) -> dict:
        """Post the info message and GIFs. If anything about the row changed,
        the channel's messages are deleted and everything is regenerated."""
        gif_paths: List[str] = []
        gif_error: Optional[str] = None
        failures = int(entry.get("gif_failures", 0))
        link_changed = entry.get("download") != want.download

        if self.gifmaker and self.gifmaker.enabled and want.download:
            cached = self.gifmaker.cached(want.download)
            if cached is not None and not force:
                gif_paths = cached
            elif link_changed or force or failures < int(self.cfg.gif["max_retries"]):
                if self._gif_budget <= 0:
                    log.info("Render budget spent; %s queued for the next cycle", want.channel_name)
                    return entry  # leave the hash alone so we come back to it
                self._gif_budget -= 1
                result = await self.gifmaker.ensure(
                    want.download, want.row.get("name", want.channel_name),
                    want.channel_name, force=force or link_changed,
                )
                gif_paths, gif_error = result.paths, result.error
                failures = 0 if not gif_error else failures + 1
                if gif_error:
                    log.error("Render failed for %s: %s", want.channel_name, gif_error)
            else:
                log.debug("Skipping render for %s (%d prior failures)", want.channel_name, failures)
                gif_error = entry.get("gif_error")

        manifest = [os.path.basename(p) for p in gif_paths]
        content_hash = digest("\n".join([want.body, "--", *manifest]))

        if entry.get("content_hash") == content_hash and entry.get("message_ids") and not force:
            return entry  # unchanged

        await self._purge(channel)

        message_ids: List[int] = []
        if not self.dry_run:
            info = await channel.send(want.body)
            message_ids.append(info.id)
            if self.cfg.discord["pin_message"]:
                try:
                    await info.pin(reason="sheet mirror")
                except discord.HTTPException as exc:
                    log.warning("Could not pin in #%s: %s", channel.name, exc)
        else:
            log.info("[dry-run] would post the info message in #%s", channel.name)

        if gif_paths:
            message_ids.extend(await self._post_gifs(channel, gif_paths))
        elif gif_error and self.cfg.gif["post_errors"] and not self.dry_run:
            note = await channel.send(f"_Preview render unavailable: {gif_error[:300]}_")
            message_ids.append(note.id)

        log.info("Rebuilt #%s (%d page(s))", channel.name, len(gif_paths))
        return {
            "message_ids": message_ids,
            "content_hash": content_hash,
            "download": want.download,
            "gif_files": manifest,
            "gif_failures": failures,
            "gif_error": gif_error,
        }

    # ---------- the sync ----------

    async def sync(self, reason: str = "scheduled", force: bool = False) -> Counter:
        async with self._lock:
            return await self._sync(reason, force)

    async def _sync(self, reason: str, force: bool = False) -> Counter:
        stats: Counter = Counter()
        guild = self.bot.get_guild(self.cfg.guild_id)
        if guild is None:
            log.error("Guild %s not visible to the bot", self.cfg.guild_id)
            return stats

        rows = await asyncio.to_thread(self.source.fetch)
        if not rows:
            log.warning("Sheet returned no usable rows; skipping this cycle (nothing deleted)")
            return stats

        desired = self.build_desired(rows)
        log.info("Sync (%s): %d rows, %d tracked channels", reason, len(desired), len(self.state.channels))

        by_name = {c.name: c for c in guild.text_channels}
        budget = int(self.cfg.discord["max_changes_per_cycle"])
        self._gif_budget = int(self.cfg.gif["max_jobs_per_cycle"])

        for key, want in sorted(desired.items(), key=lambda kv: kv[1].channel_name):
            entry = dict(self.state.channels.get(key, {}))
            channel = None
            if entry.get("channel_id"):
                channel = guild.get_channel(int(entry["channel_id"]))
            if channel is None and self.cfg.discord["adopt_existing"]:
                candidate = by_name.get(want.channel_name)
                if candidate is not None and self._protected(candidate):
                    log.debug("Not adopting protected channel #%s", candidate.name)
                elif candidate is not None:
                    channel = candidate
                    log.info("Adopted existing channel #%s", channel.name)
                    stats["adopted"] += 1

            if channel is None:
                if budget <= 0:
                    log.info("Change budget spent; remaining work continues next cycle")
                    break
                category = await self._ensure_category(guild, want.category)
                if self.dry_run:
                    log.info("[dry-run] would create #%s in %s", want.channel_name, want.category)
                    budget -= 1
                    stats["created"] += 1
                    continue
                try:
                    channel = await guild.create_text_channel(
                        name=want.channel_name,
                        category=category,
                        topic=want.topic,
                        overwrites=self._overwrites(guild, want.private),
                        reason="sheet mirror: row added",
                    )
                except discord.HTTPException as exc:
                    log.error("Could not create #%s: %s", want.channel_name, exc)
                    continue
                by_name[channel.name] = channel
                budget -= 1
                stats["created"] += 1
                log.info("Created #%s", channel.name)
            else:
                if self._protected(channel):
                    log.debug("Skipping protected channel #%s", channel.name)
                    continue
                if channel.name != want.channel_name and not self.dry_run:
                    old = channel.name
                    await channel.edit(name=want.channel_name, reason="sheet mirror: row renamed")
                    by_name.pop(old, None)
                    by_name[channel.name] = channel
                    stats["renamed"] += 1
                    log.info("Renamed #%s -> #%s", old, channel.name)
                current_category = channel.category.name if channel.category else None
                if want.category != current_category and budget > 0:
                    if self.dry_run:
                        log.info("[dry-run] would move #%s to %s", channel.name, want.category)
                    else:
                        category = await self._ensure_category(guild, want.category)
                        await channel.edit(category=category, reason="sheet mirror: category")
                        log.info("Moved #%s to %s", channel.name, want.category)
                    budget -= 1
                    stats["moved"] += 1
                if want.topic and channel.topic != want.topic and not self.dry_run:
                    await channel.edit(topic=want.topic, reason="sheet mirror: topic")
                    stats["topic"] += 1
                if self.cfg.discord["private_mode"] == "lock" and not self.dry_run:
                    everyone = channel.overwrites_for(guild.default_role)
                    if everyone.view_channel is not (not want.private):
                        await channel.edit(
                            overwrites=self._overwrites(guild, want.private),
                            reason="sheet mirror: visibility",
                        )
                        stats["visibility"] += 1

            if channel is not None:
                before = entry.get("content_hash")
                content = await self._ensure_content(channel, want, entry, force)
                if content.get("content_hash") != before:
                    stats["rebuilt"] += 1
                self.state.channels[key] = {
                    **content,
                    "channel_id": channel.id,
                    "channel_name": channel.name,
                    "category": want.category,
                }
                if not self.dry_run:
                    self.state.save()  # checkpoint: GIF runs are slow

        stats.update(await self._delete_stale(guild, desired, budget))

        if self.cfg.discord["sort_channels"] and not self.dry_run:
            await self._sort(guild, desired)

        if not self.dry_run:
            self.state.save()
        log.info("Sync done: %s", dict(stats) or "no changes")
        return stats

    async def _delete_stale(self, guild: discord.Guild, desired: Dict[str, Desired], budget: int) -> Counter:
        stats: Counter = Counter()
        if not self.cfg.discord["delete_removed"]:
            return stats

        stale = [k for k in self.state.channels if k not in desired]
        if not stale:
            return stats

        limit = max(3, int(len(self.state.channels) * float(self.cfg.discord["max_delete_fraction"])))
        if len(stale) > limit:
            log.error(
                "Refusing to delete %d channels in one pass (limit %d). "
                "This usually means the sheet was truncated. Fix the sheet, or raise "
                "discord.max_delete_fraction if the removal is intentional.",
                len(stale), limit,
            )
            return stats

        for key in stale:
            if budget <= 0:
                break
            entry = self.state.channels[key]
            channel_id = entry.get("channel_id")
            if not channel_id:
                log.warning("State entry %r has no channel_id; dropping it", key)
                self.state.channels.pop(key, None)
                continue
            channel = guild.get_channel(int(channel_id))
            if channel is None:
                self.state.channels.pop(key, None)
                continue
            if self._protected(channel):
                log.info("Row gone but #%s is protected; leaving it", channel.name)
                continue
            if self.dry_run:
                log.info("[dry-run] would delete #%s", channel.name)
            else:
                try:
                    await channel.delete(reason="sheet mirror: row removed")
                except discord.HTTPException as exc:
                    log.error("Could not delete #%s: %s", channel.name, exc)
                    continue
                self.state.channels.pop(key, None)
                log.info("Deleted #%s", channel.name)
            budget -= 1
            stats["deleted"] += 1

        if self.cfg.discord["prune_empty_categories"] and not self.dry_run:
            managed = {d.category for d in desired.values() if d.category}
            for category in guild.categories:
                if category.name in managed or category.name in self.cfg.discord["protected_categories"]:
                    continue
                if not category.channels and len(category.name) <= 2:
                    try:
                        await category.delete(reason="sheet mirror: empty category")
                        stats["categories_deleted"] += 1
                    except discord.HTTPException:
                        pass
        return stats

    async def _sort(self, guild: discord.Guild, desired: Dict[str, Desired]) -> None:
        """Alphabetise channels inside each managed category. Costs one API call
        per out-of-place channel, so it is off by default."""
        wanted_names = {d.channel_name for d in desired.values()}
        for category in guild.categories:
            children = [c for c in category.text_channels if c.name in wanted_names]
            if len(children) < 2:
                continue
            for position, channel in enumerate(sorted(children, key=lambda c: c.name)):
                if channel.position != position:
                    try:
                        await channel.edit(position=position, reason="sheet mirror: sort")
                    except discord.HTTPException:
                        return
