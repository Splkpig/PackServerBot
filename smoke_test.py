#!/usr/bin/env python3
"""Offline check that the pieces fit together — no Discord, no Google needed.

Builds a small synthetic OptiFine CIT pack, serves it over localhost, and drives
the real GifMaker through the whole path: download, GitHub URL rewrite, renderer
subprocess, image collection, cache. Then exercises the Mirror's naming and
message-building against the names from the server screenshots.

    .venv/bin/python smoke_test.py          # Pi / Linux
    .venv\\Scripts\\python.exe smoke_test.py  # Windows

Run it after changing gif.command, after moving the project, and once on the Pi
to see what a render actually costs there (HANDOFF.md §6.5).
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import io
import json
import os
import socketserver
import sys
import tempfile
import threading
import time
import zipfile

import yaml

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

PASS = "  ok   "
FAIL = "  FAIL "
failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    print(f"{PASS if ok else FAIL} {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)
    return ok


# --------------------------------------------------------------------------
# a synthetic pack, so the test needs no real resource pack on disk
# --------------------------------------------------------------------------

def build_pack(path: str) -> None:
    from PIL import Image

    def png(color, w=16, h=16) -> bytes:
        buf = io.BytesIO()
        Image.new("RGBA", (w, h), color).save(buf, "PNG")
        return buf.getvalue()

    def strip(colors) -> bytes:
        img = Image.new("RGBA", (16, 16 * len(colors)))
        for i, colour in enumerate(colors):
            img.paste(Image.new("RGBA", (16, 16), colour), (0, i * 16))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    base = "assets/minecraft/optifine/cit"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("pack.mcmeta", json.dumps({"pack": {"pack_format": 1, "description": "Smoke Pack"}}))
        z.writestr(f"{base}/alpha/alpha.properties",
                   "type=item\nitems=diamond_sword\ntexture=alpha.png\n"
                   "nbt.display.Name=ipattern:*Alpha Blade*\n")
        z.writestr(f"{base}/alpha/alpha.png", png((200, 40, 40, 255)))
        # animated, with the trailing comma in .mcmeta that plain json rejects
        z.writestr(f"{base}/beta/beta.properties",
                   "type=item\nitems=bow\ntexture=beta.png\n"
                   "nbt.display.Name=ipattern:*Beta Bow*\n")
        z.writestr(f"{base}/beta/beta.png",
                   strip([(40, 200, 40, 255), (40, 120, 40, 255), (20, 60, 20, 255)]))
        z.writestr(f"{base}/beta/beta.png.mcmeta", '{"animation":{"frametime":4,}}')
        # armor entries must be skipped, not rendered
        z.writestr(f"{base}/delta/delta.properties",
                   "type=armor\nitems=diamond_chestplate\ntexture=delta.png\n")
        z.writestr(f"{base}/delta/delta.png", png((220, 220, 40, 255)))


def make_config(work: str, pack_dir: str) -> str:
    """A real Config built from config.example.yaml, with dummy credentials."""
    with open(os.path.join(APP_DIR, "config.example.yaml"), encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg["discord"]["guild_id"] = 123456789012345678
    cfg["sheets"]["spreadsheet_id"] = "smoke-test-sheet"
    account = os.path.join(work, "service_account.json")
    with open(account, "w", encoding="utf-8") as fh:
        json.dump({"type": "service_account", "client_email": "smoke@test.iam.gserviceaccount.com"}, fh)
    cfg["sheets"]["service_account_file"] = account
    cfg["gif"]["cache_dir"] = os.path.join(work, "cache")
    cfg["gif"]["work_dir"] = os.path.join(work, "work")
    cfg["runtime"]["state_file"] = os.path.join(work, "state.json")
    path = os.path.join(work, "config.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh)
    return path


# --------------------------------------------------------------------------

async def run(work: str) -> None:
    pack = os.path.join(work, "SmokePack.zip")
    build_pack(pack)

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):  # keep the test output readable
            pass

    handler = functools.partial(QuietHandler, directory=work)
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}/SmokePack.zip"

        os.environ.setdefault("DISCORD_TOKEN", "smoke-test-token")
        from config import Config
        from gifs import GifMaker, to_direct_url
        from mirror import Mirror, slugify

        cfg = Config(make_config(work, work))
        check(cfg.discord["purge_mode"] == "all",
              "purge_mode is 'all'", "channels are rebuilt from scratch on change")

        # GitHub blob -> raw, with the § colour codes percent-encoded
        blob = ("https://github.com/Splkpig/PackServer/blob/main/"
                "%C2%A74Akame%20Ga%20Kill%20%C2%A78[%C2%A716x%C2%A78].zip")
        check(to_direct_url(blob).startswith("https://raw.githubusercontent.com/Splkpig/PackServer/main/"),
              "GitHub /blob/ rewritten to raw", to_direct_url(blob)[-40:])

        # channel names from the screenshots
        expected = {
            "Akame Ga Kill": "akame-ga-kill",
            "Andrecks Vapor (Eveoi Additions)": "andrecks-vapor-eveoi-additions",
            "Andrecks Vapor (Stymy Additions)": "andrecks-vapor-stymy-additions",
            "Blue Gem": "blue-gem",
            "Blue Pit": "blue-pit",
            "Amethyst": "amethyst",
        }
        wrong = {k: slugify(k) for k, v in expected.items() if slugify(k) != v}
        check(not wrong, f"slugify matches {len(expected)} real channel names",
              str(wrong) if wrong else "")

        class FakeBot:
            user = None

        mirror = Mirror(FakeBot(), cfg, source=None, gifmaker=GifMaker(cfg))
        desired = mirror.build_desired([
            {"name": "Akame Ga Kill", "status": "Public", "creator": "Reptaxi",
             "link": blob, "_row_number": "2"},
            {"name": "Secret Pack", "status": "Private", "creator": "Someone",
             "link": "none", "_row_number": "3"},
        ])
        public = desired["akame-ga-kill"]
        private = desired["secret-pack"]
        check(public.category == "A" and private.category == "S",
              "letter categories assigned", "akame->A, secret-pack->S")
        check("Link:" not in private.body,
              "'none' link dropped from the message", private.body.replace("\n", " | "))
        check(private.private is True and public.private is False,
              "Private status detected")

        # the real render path
        gif = GifMaker(cfg)
        check(gif.enabled, "gif rendering enabled in config.example.yaml")
        started = time.monotonic()
        result = await gif.ensure(url, "Akame Ga Kill", "akame-ga-kill")
        elapsed = time.monotonic() - started

        if not check(result.error is None, "renderer ran", result.error or ""):
            return
        names = [os.path.basename(p) for p in result.paths]
        check(bool(result.paths), f"collected {len(names)} page(s)", ", ".join(names))
        check(all(n.lower().endswith((".gif", ".png")) for n in names),
              "only images collected", "no .txt key, no manifest.json")
        check(len(names) == len(set(os.path.splitext(n)[0] for n in names)),
              "one upload per page stem", "gif preferred over the still png")

        cap = float(cfg.gif["max_upload_mb"]) * 1024 * 1024
        big = [n for n, p in zip(names, result.paths) if os.path.getsize(p) > cap]
        check(not big, f"pages under the {cfg.gif['max_upload_mb']} MB upload limit",
              ", ".join(big))

        cached = await gif.ensure(url, "Akame Ga Kill", "akame-ga-kill")
        check(cached.from_cache and not cached.ran,
              "second call served from cache", "an unchanged link never re-renders")

        print(f"\n  render took {elapsed:.1f}s "
              f"(timeout is {cfg.gif['timeout_seconds']}s; a real pack is much heavier)")
        httpd.shutdown()


def main() -> int:
    print("sheet-mirror smoke test\n")
    with tempfile.TemporaryDirectory(prefix="sheet-mirror-smoke-") as work:
        try:
            asyncio.run(run(work))
        except Exception as exc:  # noqa: BLE001 - report, do not traceback
            print(f"{FAIL} unexpected error: {type(exc).__name__}: {exc}")
            failures.append("unexpected error")
    if failures:
        print(f"\nFAILED: {len(failures)} check(s) — {', '.join(failures)}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
