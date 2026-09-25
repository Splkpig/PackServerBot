"""Small HTTP status dashboard for the mirror.

Serves one page plus two JSON endpoints on the bot's own event loop. aiohttp is
already a discord.py dependency, so this adds nothing to requirements.txt.

    GET  /            the page
    GET  /api/status  current state + recent log lines
    POST /api/sync    run a sync now ({"force": true} to ignore caches)

A sync can take a long time (55 packs on a Pi), so POST /api/sync starts it in
the background and returns immediately; the page then watches the state flip from
running back to idle.

The dashboard binds 0.0.0.0 by default, which means anyone who can reach the Pi
can trigger a sync. Set dashboard.auth_token to require a shared secret.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import secrets
import time
from typing import Any, Deque, Dict, List, Optional

from aiohttp import web

log = logging.getLogger(__name__)


class RingLogHandler(logging.Handler):
    """Keeps the last N log records in memory for the console panel."""

    def __init__(self, capacity: int = 400):
        super().__init__()
        self.records: Deque[Dict[str, Any]] = collections.deque(maxlen=capacity)
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._seq += 1
            self.records.append({
                "seq": self._seq,
                "time": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            })
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)

    def tail(self, limit: int = 80, after: int = 0) -> List[Dict[str, Any]]:
        return [r for r in self.records if r["seq"] > after][-limit:]


class Dashboard:
    def __init__(self, bot, cfg, log_handler: RingLogHandler):
        self.bot = bot
        self.cfg = cfg
        self.log_handler = log_handler
        self.dash = cfg.dashboard
        self.token = str(self.dash.get("auth_token") or "")
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._tasks: set[asyncio.Task] = set()

    # ---------- lifecycle ----------

    async def start(self) -> None:
        app = web.Application()
        app.add_routes([
            web.get("/", self._page),
            web.get("/api/status", self._status),
            web.post("/api/sync", self._sync),
            web.get("/healthz", self._health),
        ])
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        host = str(self.dash["host"])
        port = int(self.dash["port"])
        self._site = web.TCPSite(self._runner, host, port)
        try:
            await self._site.start()
        except OSError as exc:
            log.error("Dashboard could not bind %s:%s - %s", host, port, exc)
            self._site = None
            return
        log.info("Dashboard on http://%s:%s", host, port)
        if not self.token:
            log.warning(
                "Dashboard has no auth_token, so anyone who can reach %s:%s can "
                "trigger a sync. Set dashboard.auth_token to require a secret.",
                host, port,
            )

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._runner is not None:
            await self._runner.cleanup()
            log.info("Dashboard stopped")

    # ---------- helpers ----------

    def _authorised(self, request: web.Request) -> bool:
        if not self.token:
            return True
        supplied = request.headers.get("X-Auth-Token") or request.query.get("token", "")
        return secrets.compare_digest(supplied, self.token)

    def _snapshot(self) -> Dict[str, Any]:
        bot, mirror = self.bot, self.bot.mirror
        guild = bot.get_guild(self.cfg.guild_id) if bot.is_ready() else None

        next_run = None
        poll = getattr(bot, "poll", None)
        if poll is not None and poll.is_running():
            upcoming = poll.next_iteration
            if upcoming is not None:
                next_run = upcoming.timestamp()

        failing = sum(1 for e in mirror.state.channels.values() if e.get("gif_error"))
        status = dict(mirror.status)
        return {
            "now": time.time(),
            "bot": {
                "connected": bot.is_ready(),
                "user": str(bot.user) if bot.user else None,
                "guild": guild.name if guild else None,
                "guild_id": self.cfg.guild_id,
                "latency_ms": round(bot.latency * 1000) if bot.is_ready() and bot.latency == bot.latency else None,
            },
            "mirror": {
                **status,
                "busy": mirror.busy,
                "tracked": len(mirror.state.channels),
                "dry_run": mirror.dry_run,
                "gif_enabled": bool(mirror.gifmaker and mirror.gifmaker.enabled),
                "gif_failing": failing,
            },
            "schedule": {
                "poll_seconds": int(self.cfg.runtime["poll_seconds"]),
                "next_run": next_run,
                "running": bool(poll is not None and poll.is_running()),
            },
            "auth_required": bool(self.token),
        }

    # ---------- routes ----------

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "ready": self.bot.is_ready()})

    async def _status(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        try:
            after = int(request.query.get("after", "0"))
        except ValueError:
            after = 0
        payload = self._snapshot()
        payload["log"] = self.log_handler.tail(limit=120, after=after)
        return web.json_response(payload)

    async def _sync(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"error": "unauthorised"}, status=401)

        force = False
        if request.can_read_body:
            try:
                body = await request.json()
                force = bool(body.get("force"))
            except Exception:  # noqa: BLE001 - empty or malformed body means defaults
                force = False

        mirror = self.bot.mirror
        if not self.bot.is_ready():
            return web.json_response({"error": "bot is not connected yet"}, status=503)
        if mirror.busy:
            return web.json_response({"error": "a sync is already running"}, status=409)

        peer = request.remote or "dashboard"
        reason = f"dashboard ({peer}){' force' if force else ''}"
        task = asyncio.create_task(self._run(reason, force))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        log.info("Sync requested from the dashboard by %s%s", peer, " (force)" if force else "")
        return web.json_response({"started": True, "force": force})

    async def _run(self, reason: str, force: bool) -> None:
        from sheets import SheetError  # local import keeps the module import graph flat
        try:
            await self.bot.mirror.sync(reason=reason, force=force)
        except SheetError as exc:
            log.error("Sheet unavailable: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the dashboard must not take the bot down
            log.exception("Dashboard-triggered sync failed")

    async def _page(self, request: web.Request) -> web.Response:
        return web.Response(text=PAGE, content_type="text/html")


# ---------------------------------------------------------------------------
# The page. Status colours come from the reserved status palette and are always
# paired with an icon and a text label, never carried by colour alone.
# ---------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pack Mirror</title>
<style>
  :root {
    color-scheme: light;
    --surface-0: #f4f4f1;
    --surface-1: #fcfcfb;
    --surface-2: #eceae4;
    --border:    #dcdad2;
    --text-primary:   #0b0b0b;
    --text-secondary: #52514e;
    --text-muted:     #82807a;
    --good: #0ca30c;
    --warning: #fab219;
    --serious: #ec835a;
    --critical: #d03b3b;
    --accent: #2a78d6;
    --console-bg: #14140f;
    --console-fg: #d8d6cc;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --surface-0: #111110;
      --surface-1: #1a1a19;
      --surface-2: #24241f;
      --border:    #35342d;
      --text-primary:   #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted:     #8f8d82;
      --good: #0ca30c;
      --warning: #fab219;
      --serious: #ec835a;
      --critical: #d03b3b;
      --accent: #3987e5;
      --console-bg: #0d0d0a;
      --console-fg: #d8d6cc;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--surface-0); color: var(--text-primary);
    font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .wrap { max-width: 1000px; margin: 0 auto; padding: 24px 16px 48px; }
  header { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 4px; }
  h1 { font-size: 20px; margin: 0; letter-spacing: -0.01em; }
  .sub { color: var(--text-secondary); font-size: 13px; margin: 0 0 20px; }
  .pill {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 3px 10px; border-radius: 999px; font-size: 13px; font-weight: 600;
    background: var(--surface-2); border: 1px solid var(--border);
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-muted); flex: none; }
  .pill.good .dot { background: var(--good); }
  .pill.run  .dot { background: var(--accent); animation: pulse 1.2s ease-in-out infinite; }
  .pill.warn .dot { background: var(--warning); }
  .pill.bad  .dot { background: var(--critical); }
  @keyframes pulse { 0%,100% { opacity: 1 } 50% { opacity: .35 } }

  .tiles { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); margin-bottom: 20px; }
  .tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }
  .tile .label { font-size: 12px; color: var(--text-secondary); text-transform: uppercase; letter-spacing: .04em; }
  .tile .value { font-size: 24px; font-weight: 650; margin-top: 4px; font-variant-numeric: tabular-nums; letter-spacing: -0.02em; }
  .tile .note  { font-size: 12px; color: var(--text-muted); margin-top: 2px; }

  .actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 20px; }
  button {
    font: inherit; font-weight: 600; cursor: pointer; border-radius: 8px;
    padding: 9px 16px; border: 1px solid transparent; background: var(--accent); color: #fff;
  }
  button.secondary { background: var(--surface-1); color: var(--text-primary); border-color: var(--border); }
  button:disabled { opacity: .5; cursor: not-allowed; }
  button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .msg { font-size: 13px; color: var(--text-secondary); }
  .msg.err { color: var(--critical); font-weight: 600; }

  .panel { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
  .panel h2 {
    font-size: 13px; margin: 0; padding: 10px 14px; border-bottom: 1px solid var(--border);
    color: var(--text-secondary); text-transform: uppercase; letter-spacing: .04em;
    display: flex; justify-content: space-between; align-items: center; gap: 8px;
  }
  .panel h2 label { text-transform: none; letter-spacing: 0; font-weight: 400; display: flex; gap: 6px; align-items: center; }
  #console {
    margin: 0; padding: 12px 14px; height: 340px; overflow-y: auto;
    background: var(--console-bg); color: var(--console-fg);
    font: 12px/1.6 ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    white-space: pre-wrap; word-break: break-word;
  }
  #console .t { color: #6f6d63; }
  #console .WARNING { color: var(--warning); }
  #console .ERROR, #console .CRITICAL { color: var(--serious); font-weight: 600; }
  #console .INFO { color: var(--console-fg); }
  #console .DEBUG { color: #7f8f7f; }
  .empty { color: var(--text-muted); font-style: italic; }
  .err-box {
    margin: 0 0 20px; padding: 10px 14px; border-radius: 8px; font-size: 13px;
    background: var(--surface-1); border: 1px solid var(--critical);
    border-left-width: 4px;
  }
  .err-box strong { color: var(--critical); }
  .err-box.warn { border-color: var(--warning); }
  .err-box.warn strong { color: var(--warning); }
  input[type=password] { font: inherit; padding: 5px 8px; border-radius: 6px; border: 1px solid var(--border);
    background: var(--surface-1); color: var(--text-primary); width: 150px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Pack Mirror</h1>
    <span id="state" class="pill"><span class="dot"></span><span id="stateText">connecting</span></span>
  </header>
  <p class="sub" id="sub">&nbsp;</p>

  <div id="errBox" class="err-box" hidden><strong>Last sync failed</strong> — <span id="errText"></span></div>
  <div id="permBox" class="err-box warn" hidden><strong>&#9888; Channels skipped</strong> —
    the bot cannot post in <span id="permCount"></span> channel(s), so they were left untouched
    rather than emptied. <span id="permList"></span></div>

  <div class="tiles">
    <div class="tile"><div class="label">Channels tracked</div><div class="value" id="tracked">—</div><div class="note" id="trackedNote">&nbsp;</div></div>
    <div class="tile"><div class="label">Sheet rows</div><div class="value" id="rows">—</div><div class="note">last successful read</div></div>
    <div class="tile"><div class="label">Last sync</div><div class="value" id="last">—</div><div class="note" id="lastNote">&nbsp;</div></div>
    <div class="tile"><div class="label">Next sync</div><div class="value" id="next">—</div><div class="note" id="nextNote">&nbsp;</div></div>
    <div class="tile"><div class="label">Renders failing</div><div class="value" id="failing">—</div><div class="note" id="failNote">&nbsp;</div></div>
    <div class="tile"><div class="label">Permission blocked</div><div class="value" id="blocked">—</div><div class="note" id="blockedNote">&nbsp;</div></div>
  </div>

  <div class="actions">
    <button id="syncBtn">Sync now</button>
    <button id="forceBtn" class="secondary">Force rebuild</button>
    <span id="tokenWrap" hidden><input type="password" id="token" placeholder="auth token"></span>
    <span class="msg" id="msg"></span>
  </div>

  <div class="panel">
    <h2>Console
      <label><input type="checkbox" id="follow" checked> follow</label>
    </h2>
    <div id="console"><span class="empty">waiting for log output…</span></div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let lastSeq = 0, firstLoad = true, busyBefore = false;

const tokenBox = $('token');
tokenBox.value = sessionStorage.getItem('dashToken') || '';
tokenBox.addEventListener('change', () => sessionStorage.setItem('dashToken', tokenBox.value));

function headers() {
  const h = {'Content-Type': 'application/json'};
  if (tokenBox.value) h['X-Auth-Token'] = tokenBox.value;
  return h;
}

function ago(sec) {
  if (sec == null) return '—';
  const d = Math.max(0, Math.round(sec));
  if (d < 60) return d + 's';
  if (d < 3600) return Math.round(d / 60) + 'm';
  if (d < 86400) return (d / 3600).toFixed(1) + 'h';
  return (d / 86400).toFixed(1) + 'd';
}
function dur(sec) {
  if (sec == null) return '—';
  if (sec < 60) return sec.toFixed(1) + 's';
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return m + 'm ' + s + 's';
}
function statsText(st) {
  if (!st) return '';
  const parts = Object.entries(st).filter(([, v]) => v).map(([k, v]) => v + ' ' + k);
  return parts.length ? parts.join(', ') : 'no changes';
}

function setPill(cls, icon, text) {
  $('state').className = 'pill ' + cls;
  $('stateText').textContent = icon + ' ' + text;
}

async function poll() {
  try {
    const r = await fetch('api/status?after=' + lastSeq, {headers: headers()});
    if (r.status === 401) {
      $('tokenWrap').hidden = false;
      setPill('warn', '⚠', 'auth token required');
      $('msg').textContent = 'Enter the dashboard auth token.';
      $('msg').className = 'msg err';
      return;
    }
    const d = await r.json();
    $('msg').className = 'msg';
    if (d.auth_required) $('tokenWrap').hidden = false;
    render(d);
  } catch (e) {
    setPill('bad', '✖', 'dashboard unreachable');
  }
}

function render(d) {
  const m = d.mirror, b = d.bot;

  if (!b.connected)      setPill('warn', '⚠', 'Discord disconnected');
  else if (m.busy)       setPill('run',  '▶', 'syncing…');
  else if (m.state === 'error') setPill('bad', '✖', 'last sync failed');
  else                   setPill('good', '✔', 'idle');

  const bits = [];
  if (b.user) bits.push(b.user);
  bits.push(b.guild ? 'guild: ' + b.guild : 'guild ' + b.guild_id + ' not visible');
  if (b.latency_ms != null) bits.push(b.latency_ms + ' ms');
  if (m.dry_run) bits.push('DRY RUN — no changes are made');
  bits.push('renders ' + (m.gif_enabled ? 'on' : 'off'));
  $('sub').textContent = bits.join(' · ');

  $('errBox').hidden = !m.last_error;
  if (m.last_error) $('errText').textContent = m.last_error;

  const blockedList = m.blocked || [];
  $('permBox').hidden = blockedList.length === 0;
  if (blockedList.length) {
    $('permCount').textContent = blockedList.length;
    $('permList').textContent = blockedList.slice(0, 6).map(n => '#' + n).join(', ')
      + (blockedList.length > 6 ? ' and ' + (blockedList.length - 6) + ' more' : '');
  }

  $('tracked').textContent = m.tracked;
  $('trackedNote').textContent = m.sync_count + ' sync' + (m.sync_count === 1 ? '' : 's') + ' this run';
  $('rows').textContent = m.rows_seen == null ? '—' : m.rows_seen;
  $('last').textContent = m.last_finished ? ago(d.now - m.last_finished) + ' ago' : '—';
  $('lastNote').textContent = m.last_finished
    ? 'took ' + dur(m.last_duration) + ' · ' + statsText(m.last_stats) : 'not yet run';
  $('next').textContent = d.schedule.next_run ? 'in ' + ago(d.schedule.next_run - d.now) : (d.schedule.running ? '—' : 'paused');
  $('nextNote').textContent = 'every ' + ago(d.schedule.poll_seconds);
  const nblocked = (m.blocked || []).length;
  $('blocked').textContent = nblocked;
  $('blockedNote').textContent = nblocked
    ? (m.blocked.slice(0, 3).join(', ') + (nblocked > 3 ? ' +' + (nblocked - 3) + ' more' : ''))
    : 'bot can post everywhere';
  $('failing').textContent = m.gif_failing;
  $('failNote').textContent = m.gif_failing ? 'retried until the link changes' : 'all renders healthy';

  const busy = m.busy || !b.connected;
  $('syncBtn').disabled = busy;
  $('forceBtn').disabled = busy;
  $('syncBtn').textContent = m.busy ? 'Syncing…' : 'Sync now';
  if (busyBefore && !m.busy) $('msg').textContent = 'Sync finished.';
  busyBefore = m.busy;

  if (d.log && d.log.length) appendLog(d.log);
}

function appendLog(lines) {
  const box = $('console');
  if (firstLoad) { box.innerHTML = ''; firstLoad = false; }
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  for (const l of lines) {
    lastSeq = Math.max(lastSeq, l.seq);
    const t = new Date(l.time * 1000).toLocaleTimeString();
    const row = document.createElement('div');
    row.className = l.level;
    const ts = document.createElement('span');
    ts.className = 't'; ts.textContent = t + '  ';
    row.appendChild(ts);
    row.appendChild(document.createTextNode(l.level.padEnd(7) + ' ' + l.message));
    box.appendChild(row);
  }
  while (box.childElementCount > 500) box.removeChild(box.firstChild);
  if ($('follow').checked && atBottom) box.scrollTop = box.scrollHeight;
}

async function trigger(force) {
  const msg = $('msg');
  if (force && !confirm('Force rebuild re-renders every pack and reposts every channel. On a Pi this takes hours. Continue?')) return;
  msg.className = 'msg';
  msg.textContent = 'Requesting…';
  try {
    const r = await fetch('api/sync', {method: 'POST', headers: headers(), body: JSON.stringify({force})});
    const d = await r.json().catch(() => ({}));
    if (r.ok) { msg.textContent = force ? 'Force rebuild started.' : 'Sync started.'; busyBefore = true; }
    else { msg.className = 'msg err'; msg.textContent = d.error || ('HTTP ' + r.status); }
  } catch (e) {
    msg.className = 'msg err';
    msg.textContent = 'Request failed: ' + e.message;
  }
  poll();
}

$('syncBtn').addEventListener('click', () => trigger(false));
$('forceBtn').addEventListener('click', () => trigger(true));
poll();
setInterval(poll, 3000);
</script>
</body>
</html>
"""
