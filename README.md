# Discord ↔ Google Sheet channel mirror

Polls one worksheet and makes a Discord guild match it. Each row becomes a text
channel, grouped under an `A` / `B` / `C`… category by first letter, with an info
message posted (and kept up to date) inside:

```
Name: Akame Ga Kill
Status: Public
Creator: Reptaxi
Link: https://github.com/Splkpig/PackServer/blob/main/...zip
```

Rows added → channels created. Rows removed → channels deleted. Any cell edited →
the channel's messages are deleted and the whole thing is reposted.

If the row has a pack download, `render_cit_inventory.py` is run over it and the
resulting inventory sheets are uploaded under the info message.

## Pack rendering

`gif.command` is the invocation, as a list of arguments. Placeholders:

| Placeholder | Meaning |
| --- | --- |
| `{python}` | the interpreter running the bot — keeps the renderer in this venv, where Pillow is |
| `{project}` | the directory holding `bot.py` and `render_cit_inventory.py` |
| `{download}` | the fetched `.zip`; including it is what tells the bot to download first |
| `{outdir}` | empty directory the renderer writes into |
| `{url}`, `{name}`, `{slug}`, `{workdir}`, `{config_dir}` | also available |

```yaml
command:
  - "{python}"
  - "{project}/render_cit_inventory.py"
  - "{download}"
  - "-o"
  - "{outdir}"
  - "--scale"
  - "3"
  - "--sort"
  - "item"
  - "--dedupe"
  - "content"
  - "--layout"
  - "key"
  - "--no-text"
```

Written with `{python}` and `{project}` the same config works on Windows and on
the Pi with no edits. GitHub `/blob/` links are rewritten to
`raw.githubusercontent.com` before download.

`--layout key` matters: the renderer's `key` layout emits only `key_NN.png` /
`key_NN.gif`, which is one set of images per page. `--layout both` would also
write `page_NN.*` and every channel would get duplicate sheets. `--no-text`
skips the `.txt` key files, which aren't wanted in Discord.

Only images are collected, grouped by page stem: the `.gif` is uploaded,
falling back to the `.png` when a page has nothing animated. Text keys and
manifests in the output directory are ignored. `include_pattern` (a regex on the
filename, e.g. `^key_`) is there if you ever switch to `--layout both`.

Pillow is in `requirements.txt`, so the venv that runs the bot can run the
renderer too — which is exactly what `{python}` arranges.

Results are cached in `gif-cache/` keyed by download URL, so a pack whose link
hasn't changed is never re-rendered no matter how often the sheet is polled.
Change the link and it re-renders; `/sync force:true` re-renders everything.

Rendering is capped at `max_jobs_per_cycle` per poll with `concurrency: 1`, so a
first pass over a full sheet spreads the work across several cycles instead of
pinning the Pi. Packs whose render fails are retried `max_retries` times, then
left alone until their link changes.

Watch the size limit: a long multi-frame sheet can easily clear 10 MB, and
anything over `max_upload_mb` is skipped with a log line rather than failing the
channel. If that bites, drop `--scale` to 2 or add `--max-frames`.

## Dashboard

The bot serves a status page on **port 2728** (`dashboard.host` / `dashboard.port`).
`0.0.0.0` means anything on the LAN can reach it; use `127.0.0.1` to keep it on the
Pi only.

```
http://<pi-address>:2728
```

It shows connection state, channels tracked, sheet rows, when the last sync ran and
what it changed, when the next one is due, how many renders are failing, how many
channels are blocked by permissions, and a live console tail. **Sync now** runs a
normal sync; **Force rebuild** ignores every cache and re-renders all packs (hours
of work on a Pi, so it asks first).

Endpoints, if you want to drive it from a script:

| Route | Purpose |
| --- | --- |
| `GET /` | the page |
| `GET /api/status` | JSON state plus recent log lines (`?after=<seq>` for only new ones) |
| `POST /api/sync` | start a sync; body `{"force": true}` to ignore caches |
| `GET /healthz` | liveness, no auth |

A sync runs in the background, so `POST /api/sync` returns straight away — `202`-style
`{"started": true}`, or `409` if one is already running. It uses the same code path as
`/sync` in Discord, so the button and the slash command behave identically.

Anyone who can reach the port can trigger a sync, which downloads and renders every
pack. Set `dashboard.auth_token` to require a shared secret; the page asks for it once
and keeps it for the browser session, and `/api/*` then needs an `X-Auth-Token`
header. The bot logs a warning at startup whenever the token is empty.

To reach it from outside the LAN, put it behind a reverse proxy or a VPN rather than
forwarding port 2728 — there is no TLS here.

## Scheduling

`runtime.poll_seconds` is **86400** (once a day). The packs change rarely and a full
pass is expensive, so the daily sweep is a backstop; use the dashboard button or
`/sync` when you have actually edited the sheet. The minimum accepted value is 60.

## Channel naming

A row's channel name is its slugified sheet name: `Andrecks Vapor (Eveoi Additions)`
becomes `andrecks-vapor-eveoi-additions`. Two rows in this sheet don't follow that,
because the channels were shortened by hand when they were made, so they are listed
explicitly in `discord.channel_overrides`:

```yaml
channel_overrides:
  "Control Pit Pack": control
  "Novis Pit Edit": novis
```

Plain `Pit` / `Pack` names were kept as-is (`blue-pit`, `log-pack`, `marpacker`) and
need no entry. Add a line here for any future row whose channel doesn't match its
sheet name — the row's identity in `state.json` stays the slug, so adding or changing
an override renames the channel rather than recreating it.

## Permissions

The pack categories deny `send_messages` to `@everyone` and allow it only for the
`Admin` role, so the bot cannot post in them by default. With
`discord.fix_permissions: true` the bot grants itself what it needs on each managed
category, and the pack channels — which are permission-synced to their category —
inherit it and stay synced. Channels created later inherit it too.

That requires the **Manage Permissions** (`manage_roles`) permission. Server Settings
→ Roles → `PackServer` → enable Manage Permissions, or re-invite with it included.
Without it the bot logs one clear error and changes nothing.

Set `fix_permissions: false` if you would rather add the allow yourself: for each of
the letter/`#` categories (or run `/setup-server`), add the bot's role and allow View Channel, Send
Messages, Read Message History, Manage Messages and Attach Files.

**The bot never purges a channel it cannot post into.** The rebuild deletes before it
reposts, so without send access a channel would be emptied and left empty. Each
channel is permission-checked first and skipped with a reason if anything is missing;
those show up in the sync summary as `blocked_by_permissions`, and on the dashboard as
a tile and a banner.

## Rebuild on change

Each channel stores a hash of its info text plus the GIF filenames. When that
hash changes, the bot clears the channel and reposts: info message first, then
one message per GIF.

`purge_mode` decides what "clear" means. It is set to **`all`** — every message
in the channel — because this is a takeover: the channels were populated by hand
before the bot existed, and those messages were posted from a personal account,
not the bot's. Under `bot_only` the bot would find nothing of its own to delete
and would post its copy *underneath* the existing one, leaving every channel
with duplicates.

Switch to `bot_only` once there is conversation in these channels worth keeping;
from then on a row edit only clears the bot's own messages.

Messages older than 14 days can't be bulk-deleted by Discord, so those are
removed one at a time with a short delay; that's slow but only matters on the
first cleanup pass.

## 1. Discord setup

1. <https://discord.com/developers/applications> → New Application → Bot → Reset Token.
   Put the token in `.env` (copy `.env.example`), never in the config file.
2. No privileged intents are required. Nothing here reads message text — the bot
   identifies its own messages by author ID — so leave **Message Content Intent**
   off and `message_content_intent: false`. If you turn the config flag on without
   enabling it in the portal, startup fails with a clear error.
3. Invite it with **Manage Channels**, **Manage Permissions**, **View Channels**,
   **Send Messages**, **Read Message History**, **Attach Files**, **Embed Links**,
   **Manage Messages** (bulk delete is required for the rebuild path), and **Add
   Reactions**, **Create Public/Private Threads** and **Send Messages in Threads**
   (Discord only lets the bot deny these to `@everyone` if it holds them itself).
   The list lives in `REQUIRED_PERMISSIONS` in `bot.py`; on startup the bot logs any
   that its role lacks, with a ready-made invite link.

   ```
   https://discord.com/api/oauth2/authorize?client_id=YOUR_APP_ID&permissions=378225683536&scope=bot%20applications.commands
   ```
4. Right-click your server → Copy Server ID (Developer Mode on) → `discord.guild_id`.

## 2. Google Sheets setup

1. Google Cloud console → new project → enable **Google Sheets API**.
2. Create a **service account**, add a JSON key, save it as `service_account.json`
   next to `bot.py`. `chmod 600` it.
3. Share the spreadsheet with the service account's email address, **Viewer** is enough.
4. Sheet ID is the long string in the sheet URL between `/d/` and `/edit`.

This sheet has **no header row** — row 1 is already data — so `sheets.header_row`
is `0` and the columns are mapped by letter: `A` name, `B` status, `C` creator,
`D` link (`E` is always "none" and unused). If you ever add a header row, set
`header_row: 1` and you can use the header text instead of letters.

## 3. Install

### On the Pi

```bash
sudo apt install -y python3-venv
git clone <or copy this folder> ~/discord-sheet-mirror
cd ~/discord-sheet-mirror
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp config.example.yaml config.yaml && nano config.yaml
cp .env.example .env && nano .env && chmod 600 .env
```

### On Windows (for testing before deploying)

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

copy config.example.yaml config.yaml    # then edit guild_id + spreadsheet_id
copy .env.example .env                  # then put the token in it
```

`.env` is read directly by `config.py`, so it works the same way on both
platforms — systemd's `EnvironmentFile=` is not required.

### Check the wiring first

```bash
.venv/bin/python smoke_test.py          # Windows: .venv\Scripts\python.exe smoke_test.py
```

This needs no Discord token and no sheet. It builds a small synthetic resource
pack, runs the real renderer over it and checks the whole path: download, URL
rewrite, renderer invocation, image collection, upload-size cap, cache. Run it
after changing `gif.command`, after moving the project, and once on the Pi to
see what a render actually costs there.

Then, with credentials filled in:

```bash
.venv/bin/python bot.py --once --dry-run
```

That logs every channel it *would* create, move or delete, and every message it
would post, without touching the guild. Read it carefully — in particular check
that the channels which already exist are logged as `Adopted existing channel`
and not as creates. When the log looks right:

```bash
.venv/bin/python bot.py --once
```

If the config is incomplete, the bot lists everything that is missing at once
and exits 2 without connecting.

## 4. Run as a service

```bash
sudo cp sheet-mirror.service /etc/systemd/system/
sudo nano /etc/systemd/system/sheet-mirror.service   # fix User= and paths if not 'pi'
sudo systemctl daemon-reload
sudo systemctl enable --now sheet-mirror
journalctl -u sheet-mirror -f
```

## How it behaves

- **Adoption.** Your server already has these channels. On the first run the bot
  matches each row to the existing channel of the same name and takes ownership
  instead of duplicating it. It records the channel ID in `state.json`.
- **Ownership.** It only ever deletes channels listed in `state.json` — channels it
  created or adopted. Anything else in the server is invisible to it, and
  `protected_channels` / `protected_categories` are excluded even from adoption.
- **Never mass-delete.** If the sheet read fails or returns zero rows, the cycle is
  skipped entirely. If more than `max_delete_fraction` of tracked channels
  disappeared at once, deletions are refused and logged loudly — that's almost
  always a truncated or mis-shared sheet, not an intentional purge.
- **Rate limits.** Channel create/delete is one of Discord's harshest limits
  (roughly 2 per 10 minutes per guild in practice). `max_changes_per_cycle` caps
  the work per pass; leftovers are picked up next poll. The first full build of a
  large sheet takes several cycles by design.
- **Renames.** Without `key_column`, the channel name *is* the row's identity, so
  renaming in the sheet = delete + create. Set `key_column` to a stable ID column
  and it renames the existing channel instead, keeping the message and history.
- **Private rows.** By default `Status` is just text in the message. Set
  `private_mode: lock` to also hide those channels from `@everyone`.

## Commands

- `/sync` — re-read the sheet immediately (requires Manage Channels).
- `/sync force:true` — same, but ignores every cache and rebuilds all channels
  and GIFs. Expensive; use after changing the GIF script.
- `/mirror-status` — state, channels tracked, sheet rows, sync interval, render
  health, when the last sync ran and what it changed, and the dashboard port.
- `/setup-server` — create the `#` and `A`–`Z` categories (27 in all) and set their
  permissions: `@everyone` cannot send messages, create threads or add reactions;
  the bot can view, send, attach files, read history and manage messages. Categories
  that already exist keep their other overwrites (e.g. `Admin`) and are fixed in
  place; channels synced to them are re-synced. Safe to run again. Honors
  `--dry-run` and `protected_categories`. Requires Manage Channels.

`/sync` and the dashboard's **Sync now** button are the same operation; use whichever
is closer to hand.

## Files

| File | Purpose |
| --- | --- |
| `bot.py` | entry point, poll loop, slash commands |
| `mirror.py` | reconcile logic, channel rebuild, ownership state |
| `gifs.py` | pack download, renderer invocation, output cache |
| `sheets.py` | read-only Sheets client and column mapping |
| `config.py` | config loading, `.env` reading, defaults and validation |
| `dashboard.py` | the status page, its JSON API, and the in-memory log buffer |
| `render_cit_inventory.py` | the CIT inventory renderer invoked per pack |
| `smoke_test.py` | offline check of the renderer/bot wiring; no credentials needed |
| `gif-cache/` | generated — rendered sheets per download URL. Safe to delete; they re-render. |
| `state.json` | generated — channel/message IDs the bot owns. Back it up; deleting it makes the bot re-adopt by name and forget what it may delete. |
