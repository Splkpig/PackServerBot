"""Configuration loading for the sheet -> Discord mirror."""

from __future__ import annotations

import copy
import logging
import os
from typing import Any, Dict

import yaml

log = logging.getLogger(__name__)

def load_env_file(path: str) -> None:
    """Read a KEY=VALUE .env file into os.environ.

    systemd reads .env itself via EnvironmentFile=, but a manual run (notably on
    Windows, where there is no such thing) would not, so the token has to be
    picked up here too. Real environment variables always win.
    """
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        log.warning("Could not read %s: %s", path, exc)
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


class ConfigError(ValueError):
    """Raised with every configuration problem found, not just the first."""


DEFAULTS: Dict[str, Any] = {
    "discord": {
        "token": None,  # prefer the DISCORD_TOKEN environment variable
        "guild_id": 0,
        "category_mode": "letter",      # "letter" (A, B, C...) or "none"
        "other_category": "#",          # category for names not starting with a letter
        "adopt_existing": True,         # take over channels that already have the right name
        # Sheet name -> channel name, for rows whose channel does not follow
        # slugification. Matched case-insensitively on the row name or its slug.
        "channel_overrides": {},
        # Grant the bot the access it needs on channels/categories it manages.
        # Needs the Manage Permissions (manage_roles) permission. Preferred on the
        # category so permission-synced channels inherit it and stay synced.
        "fix_permissions": True,
        "delete_removed": True,         # delete channels whose row disappeared from the sheet
        "prune_empty_categories": False,
        "protected_channels": [],       # never touched, by channel name
        "protected_categories": [],     # never touched, by category name
        "max_changes_per_cycle": 25,    # create/delete/move budget per sync (rate-limit safety)
        "max_delete_fraction": 0.25,    # abort deletions if more than this share of rows vanished
        "sort_channels": False,
        "pin_message": False,
        "set_topic": False,
        "topic_template": "{creator}",
        "private_mode": "ignore",       # "ignore" or "lock" (hide channel from @everyone)
        "private_field": "status",
        "private_values": ["private"],
        "private_role_ids": [],         # roles that keep access when private_mode is "lock"
        # Off by default: nothing here ever reads message.content. The purge
        # path identifies the bot's own messages by author id, which needs no
        # privileged intent. Leave it false unless you add content-reading code.
        "message_content_intent": False,
        # When a row changes, the channel's contents are torn down and rebuilt.
        # "bot_only" wipes just this bot's messages; "all" wipes everything.
        # "all" is the default because this is a takeover: the channels already
        # hold info messages posted from a human account, and bot_only would
        # leave those in place and post duplicates beside them.
        "purge_mode": "all",
        "purge_limit": 200,
    },
    "sheets": {
        "spreadsheet_id": "",
        "worksheet": "Sheet1",
        "header_row": 1,                # 1-based; use 0 if the sheet has no header row
        "service_account_file": "service_account.json",
        "key_column": None,             # optional stable ID column; enables renames
        "columns": {
            "name": "Name",
            "status": "Status",
            "creator": "Creator",
            "link": "Link",
        },
    },
    "message": {
        "fields": ["name", "status", "creator", "link"],
        "labels": {
            "name": "Name",
            "status": "Status",
            "creator": "Creator",
            "link": "Link",
        },
        "omit_values": ["", "none", "n/a", "-"],
        "omit_empty": True,
        "empty_placeholder": "none",
    },
    "gif": {
        "enabled": False,
        "command": [],                  # see gifs.py for the placeholders
        "download_field": "link",       # which sheet column holds the download
        "cache_dir": "gif-cache",
        "work_dir": "work",  # scratch for downloads/renders; relative to the config file
        "timeout_seconds": 900,
        "download_timeout": 300,
        "max_download_mb": 250,
        "media_extensions": [".gif", ".png"],   # preference order, per page stem
        "include_pattern": "",                  # optional regex on the filename
        "max_gifs_per_channel": 10,
        "gifs_per_message": 1,
        "max_upload_mb": 9.5,           # raise to 49 / 99 on a boosted guild
        "concurrency": 1,
        "max_jobs_per_cycle": 3,        # how many rows may run the script per poll
        "max_retries": 3,               # give up on a failing link until it changes
        "post_errors": False,           # post a note in-channel when generation fails
    },
    "dashboard": {
        "enabled": True,
        "host": "0.0.0.0",              # reachable from the LAN; use 127.0.0.1 to keep it local
        "port": 2728,
        "auth_token": "",               # set a secret to require it for status and sync
        "log_lines": 400,               # size of the in-memory console buffer
    },
    "runtime": {
        "poll_seconds": 86400,          # once a day; /sync or the dashboard for on-demand
        "state_file": "state.json",
        "dry_run": False,
        "log_level": "INFO",
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    def __init__(self, path: str):
        self.path = path
        with open(path, "r", encoding="utf-8") as fh:
            user = yaml.safe_load(fh) or {}
        merged = _deep_merge(DEFAULTS, user)

        self.discord = merged["discord"]
        self.sheets = merged["sheets"]
        self.message = merged["message"]
        self.gif = merged["gif"]
        self.dashboard = merged["dashboard"]
        self.runtime = merged["runtime"]

        self.base_dir = os.path.dirname(os.path.abspath(path))
        base_dir = self.base_dir

        # Look beside the config first, then beside the code. Normally the same
        # directory; they differ when --config points somewhere else. Values
        # already in the real environment always win over both.
        for env_dir in dict.fromkeys((base_dir, os.path.dirname(os.path.abspath(__file__)))):
            load_env_file(os.path.join(env_dir, ".env"))

        self.token = os.environ.get("DISCORD_TOKEN") or self.discord.get("token")
        self.guild_id = int(os.environ.get("DISCORD_GUILD_ID") or self.discord["guild_id"] or 0)
        for section, key in (
            ("sheets", "service_account_file"),
            ("runtime", "state_file"),
            ("gif", "cache_dir"),
            ("gif", "work_dir"),
        ):
            value = getattr(self, section)[key]
            if value and not os.path.isabs(value):
                getattr(self, section)[key] = os.path.join(base_dir, value)

        self._validate()

    def _validate(self) -> None:
        """Collect every problem, so a first-time setup sees the whole list at
        once instead of fixing one thing per run."""
        problems: list[str] = []

        if not self.token:
            problems.append(
                "No Discord token. Put DISCORD_TOKEN=... in .env (copy .env.example), "
                "or set discord.token in the config."
            )
        if not self.guild_id:
            problems.append(
                "discord.guild_id is not set. Enable Developer Mode in Discord, "
                "right-click the server and Copy Server ID."
            )
        if not self.sheets["spreadsheet_id"]:
            problems.append(
                "sheets.spreadsheet_id is not set. It is the long id in the sheet URL: "
                "docs.google.com/spreadsheets/d/<THIS>/edit"
            )
        else:
            account = self.sheets["service_account_file"]
            if not os.path.exists(account):
                problems.append(
                    f"sheets.service_account_file not found at {account}. Download the "
                    "service account key JSON and share the sheet with its client_email."
                )

        if "name" not in self.sheets["columns"]:
            problems.append("sheets.columns must include a 'name' entry — it drives the channel name.")
        if self.discord["category_mode"] not in ("letter", "none"):
            problems.append("discord.category_mode must be 'letter' or 'none'.")
        if self.discord["private_mode"] not in ("ignore", "lock"):
            problems.append("discord.private_mode must be 'ignore' or 'lock'.")
        if self.discord["purge_mode"] not in ("bot_only", "all"):
            problems.append("discord.purge_mode must be 'bot_only' or 'all'.")

        if self.gif["enabled"]:
            command = self.gif["command"]
            if not command:
                problems.append("gif.enabled is true but gif.command is empty.")
            elif not isinstance(command, list):
                problems.append("gif.command must be a list of arguments, not a shell string.")
            else:
                # Catch a bad renderer path now rather than on the first render,
                # which only happens once a row with a download link syncs.
                app_dir = os.path.dirname(os.path.abspath(__file__))
                for part in command:
                    if not isinstance(part, str) or not part.endswith(".py"):
                        continue
                    resolved = part.format(
                        python="", project=app_dir, config_dir=self.base_dir,
                        url="", download="", outdir="", workdir="", name="", slug="",
                    )
                    if not os.path.exists(resolved):
                        problems.append(
                            f"gif.command references {resolved}, which does not exist. "
                            "Use {project}/render_cit_inventory.py to point at the copy "
                            "shipped next to bot.py."
                        )
            if self.gif["download_field"] not in self.sheets["columns"]:
                problems.append(
                    f"gif.download_field {self.gif['download_field']!r} is not in sheets.columns."
                )

        if self.dashboard["enabled"]:
            try:
                port = int(self.dashboard["port"])
            except (TypeError, ValueError):
                port = -1
            if not 1 <= port <= 65535:
                problems.append(f"dashboard.port must be 1-65535, got {self.dashboard['port']!r}.")
            if not str(self.dashboard["host"]).strip():
                problems.append("dashboard.host must be set (0.0.0.0 for the LAN, 127.0.0.1 for local only).")

        try:
            if int(self.runtime["poll_seconds"]) < 60:
                problems.append("runtime.poll_seconds must be at least 60.")
        except (TypeError, ValueError):
            problems.append(f"runtime.poll_seconds must be a number, got {self.runtime['poll_seconds']!r}.")

        if problems:
            raise ConfigError(
                "Configuration is not usable yet:\n"
                + "\n".join(f"  - {p}" for p in problems)
            )
