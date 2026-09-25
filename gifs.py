"""Runs the CIT inventory renderer over each row's pack and caches the output.

The script is invoked as configured in ``gif.command``; these placeholders are
filled in per row:

    {python}   the interpreter running this bot (sys.executable) — use this so
               the renderer always runs in the same venv, which is where Pillow is
    {project}  directory holding the bot's code, i.e. where render_cit_inventory.py
               sits next to bot.py. Anchored to this module, not to the working
               directory, because the renderer runs with cwd set to its scratch dir
    {config_dir} directory holding config.yaml (usually the same place)
    {url}      the download link straight from the sheet
    {download} path to the downloaded .zip (including {download} is what tells
               this module to fetch the pack first)
    {outdir}   empty directory the script writes its output into
    {workdir}  the scratch directory for this render
    {name}     row name, e.g. "Akame Ga Kill"
    {slug}     channel name, e.g. "akame-ga-kill"

Using {python} and {project} keeps one config working on both Windows and the Pi.

Only image output is collected. Files are grouped by stem so a page that has
both a .gif and a .png yields one upload (the .gif), and non-image output —
text keys, manifests — is ignored.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

log = logging.getLogger(__name__)

GITHUB_BLOB = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/blob/(.+)$")

# Where bot.py and render_cit_inventory.py live. The renderer subprocess runs
# with cwd set to a scratch directory, so its path has to be absolute.
APP_DIR = os.path.dirname(os.path.abspath(__file__))


def to_direct_url(url: str) -> str:
    """GitHub blob pages are HTML; rewrite them to the raw file."""
    match = GITHUB_BLOB.match(url.strip())
    if match:
        owner, repo, rest = match.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{rest}"
    return url.strip()


def url_key(url: str) -> str:
    return hashlib.sha1(to_direct_url(url).encode("utf-8")).hexdigest()[:16]


@dataclass
class GifResult:
    paths: List[str] = field(default_factory=list)
    error: Optional[str] = None
    from_cache: bool = False
    ran: bool = False


class GifMaker:
    def __init__(self, cfg):
        self.cfg = cfg.gif
        self.app_dir = APP_DIR
        self.config_dir = getattr(cfg, "base_dir", APP_DIR)
        self.enabled = bool(self.cfg["enabled"]) and bool(self.cfg["command"])
        self.cache_dir = self.cfg["cache_dir"]
        self.work_dir = self.cfg["work_dir"]
        self._sem = asyncio.Semaphore(max(1, int(self.cfg["concurrency"])))
        pattern = str(self.cfg.get("include_pattern") or "")
        self._include = re.compile(pattern) if pattern else None
        if self.enabled:
            os.makedirs(self.cache_dir, exist_ok=True)
            os.makedirs(self.work_dir, exist_ok=True)

    # ---------- cache ----------

    def _cache_path(self, url: str) -> str:
        return os.path.join(self.cache_dir, url_key(url))

    def cached(self, url: str) -> Optional[List[str]]:
        folder = self._cache_path(url)
        meta_path = os.path.join(folder, "meta.json")
        if not os.path.exists(meta_path):
            return None
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            return None
        paths = [os.path.join(folder, n) for n in meta.get("files", [])]
        if paths and all(os.path.exists(p) for p in paths):
            return paths
        return [] if meta.get("files") == [] else None

    def _store(self, url: str, produced: List[str]) -> List[str]:
        folder = self._cache_path(url)
        shutil.rmtree(folder, ignore_errors=True)
        os.makedirs(folder, exist_ok=True)
        stored = []
        for path in produced:
            target = os.path.join(folder, os.path.basename(path))
            shutil.copy2(path, target)
            stored.append(target)
        with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {"url": url, "files": [os.path.basename(p) for p in stored], "generated": time.time()},
                fh, indent=2,
            )
        return stored

    def invalidate(self, url: str) -> None:
        shutil.rmtree(self._cache_path(url), ignore_errors=True)

    # ---------- download ----------

    def _download(self, url: str, dest_dir: str) -> str:
        direct = to_direct_url(url)
        parsed = urllib.parse.urlparse(direct)
        filename = urllib.parse.unquote(os.path.basename(parsed.path)) or "download.zip"
        # Pack names carry § colour codes; keep them, drop path separators.
        filename = filename.replace("/", "_").replace("\\", "_")[:120]
        dest = os.path.join(dest_dir, filename)

        cap = int(float(self.cfg["max_download_mb"]) * 1024 * 1024)
        written = 0
        with requests.get(
            direct, stream=True, timeout=int(self.cfg["download_timeout"]),
            headers={"User-Agent": "sheet-mirror/1.0"},
        ) as response:
            response.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in response.iter_content(chunk_size=1 << 16):
                    written += len(chunk)
                    if written > cap:
                        raise RuntimeError(f"download exceeded {self.cfg['max_download_mb']} MB")
                    fh.write(chunk)
        log.debug("Downloaded %s (%d bytes)", filename, written)
        return dest

    # ---------- run ----------

    async def ensure(self, url: str, name: str, slug: str, force: bool = False) -> GifResult:
        if not self.enabled or not url:
            return GifResult()

        if force:
            self.invalidate(url)
        else:
            hit = self.cached(url)
            if hit is not None:
                return GifResult(paths=hit, from_cache=True)

        async with self._sem:
            hit = self.cached(url)
            if hit is not None and not force:
                return GifResult(paths=hit, from_cache=True)
            return await self._generate(url, name, slug)

    async def _generate(self, url: str, name: str, slug: str) -> GifResult:
        scratch = os.path.join(self.work_dir, f"{slug}-{url_key(url)}")
        outdir = os.path.join(scratch, "out")
        shutil.rmtree(scratch, ignore_errors=True)
        os.makedirs(outdir, exist_ok=True)

        try:
            template = list(self.cfg["command"])
            download_path = ""
            if any("{download}" in part for part in template):
                log.info("Downloading pack for %s", name)
                download_path = await asyncio.to_thread(self._download, url, scratch)

            argv = [
                part.format(
                    python=sys.executable, project=self.app_dir,
                    config_dir=self.config_dir,
                    url=to_direct_url(url), download=download_path,
                    outdir=outdir, name=name, slug=slug, workdir=scratch,
                )
                for part in template
            ]
            log.info("Rendering %s", name)
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=scratch,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=int(self.cfg["timeout_seconds"])
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return GifResult(error=f"render timed out after {self.cfg['timeout_seconds']}s", ran=True)

            if process.returncode != 0:
                tail = (stderr or b"").decode("utf-8", "replace").strip()[-500:]
                return GifResult(error=f"render exited {process.returncode}: {tail}", ran=True)

            produced = self._collect(outdir)
            if not produced:
                listing = ", ".join(sorted(os.listdir(outdir))[:8]) or "nothing"
                return GifResult(error=f"no images collected (outdir held: {listing})", ran=True)

            produced = produced[: int(self.cfg["max_gifs_per_channel"])]
            stored = await asyncio.to_thread(self._store, url, produced)
            log.info("Rendered %d page(s) for %s", len(stored), name)
            return GifResult(paths=stored, ran=True)

        except Exception as exc:  # noqa: BLE001 - surfaced to the caller as text
            return GifResult(error=f"{type(exc).__name__}: {exc}", ran=True)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _collect(self, outdir: str) -> List[str]:
        """One upload per page stem: the first extension in media_extensions
        that exists. Anything else in the output directory is ignored."""
        preference = [e.lower() for e in self.cfg["media_extensions"]]

        by_stem: Dict[str, Dict[str, str]] = {}
        for root, _dirs, files in os.walk(outdir):
            for filename in files:
                stem, ext = os.path.splitext(filename)
                if ext.lower() not in preference:
                    continue
                if self._include and not self._include.search(filename):
                    continue
                by_stem.setdefault(stem, {})[ext.lower()] = os.path.join(root, filename)

        collected = []
        for stem in sorted(by_stem):
            found = by_stem[stem]
            for ext in preference:
                if ext in found:
                    collected.append(found[ext])
                    break
        return collected
