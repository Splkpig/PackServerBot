#!/usr/bin/env python3
"""
render_cit_inventory.py
=======================

Catalogue every OptiFine CIT custom item texture in a Minecraft 1.8.9 resource
pack as numbered key sheets: 36 entries per image, icon + name per row, with
animated textures playing in place (GIF or APNG).

    python render_cit_inventory.py mypack.zip -o out/ --scale 3

--layout inventory renders the older mockup instead (27 storage + 9 hotbar
slots, armor and crafting left empty); --layout both writes each.

Requires Pillow:  pip install pillow
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import io
import json
import math
import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional, Sequence, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    sys.exit("Pillow is required:  pip install pillow")


# ---------------------------------------------------------------------------
# 1.8.9 inventory geometry, in GUI pixels.
# Mirrors ContainerPlayer: main inventory at (8 + col*18, 84 + row*18),
# hotbar at (8 + col*18, 142). These are item origins, slot borders are -1/-1.
# ---------------------------------------------------------------------------
GUI_W, GUI_H = 176, 166
INVENTORY_GUI = "assets/minecraft/textures/gui/container/inventory.png"

SLOTS: List[Tuple[int, int]] = [
    (8 + col * 18, 84 + row * 18) for row in range(3) for col in range(9)
] + [(8 + col * 18, 142) for col in range(9)]
SLOTS_PER_PAGE = len(SLOTS)  # 36

# Decorative only, used by the procedural fallback panel.
ARMOR_SLOTS = [(8, 8 + i * 18) for i in range(4)]
CRAFT_SLOTS = [(98 + c * 18, 18 + r * 18) for r in range(2) for c in range(2)]
RESULT_SLOT = [(154, 28)]

PANEL = (198, 198, 198, 255)
PANEL_LIGHT = (255, 255, 255, 255)
PANEL_DARK = (85, 85, 85, 255)
SLOT_BG = (139, 139, 139, 255)
SLOT_DARK = (55, 55, 55, 255)
SLOT_LIGHT = (255, 255, 255, 255)

TICK_MS = 50
HEADER_H = 12

CIT_RE = re.compile(
    r"^assets/(?P<ns>[^/]+)/(?:optifine|mcpatcher)/cit/.*\.properties$", re.IGNORECASE
)
MATCH_PREFIX_RE = re.compile(r"^(?:i?pattern|i?regex):", re.IGNORECASE)
SECTION_CODE_RE = re.compile("\u00a7.", re.IGNORECASE)
LORE_KEY_RE = re.compile(r"^nbt\.display\.Lore\.(\d+|\*)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Zip access
# ---------------------------------------------------------------------------
def norm_path(path: str) -> str:
    """Collapse ./ ../ // and leading slashes into a clean zip-style path."""
    parts: List[str] = []
    for part in PurePosixPath(path.replace("\\", "/")).parts:
        if part in ("/", ".", ""):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


class PackReader:
    """Case-insensitive read-only view of a resource pack zip."""

    def __init__(self, zip_path: Path):
        self.zip_path = zip_path
        self.zf = zipfile.ZipFile(zip_path)
        self.names = [n for n in self.zf.namelist() if not n.endswith("/")]
        self._exact = set(self.names)
        self._lower: Dict[str, str] = {}
        for n in self.names:
            self._lower.setdefault(n.lower(), n)

    def close(self) -> None:
        self.zf.close()

    def resolve(self, name: str) -> Optional[str]:
        name = norm_path(name)
        if name in self._exact:
            return name
        return self._lower.get(name.lower())

    def read(self, name: str) -> Optional[bytes]:
        real = self.resolve(name)
        return None if real is None else self.zf.read(real)

    def image(self, name: str) -> Optional[Image.Image]:
        data = self.read(name)
        if data is None:
            return None
        try:
            return Image.open(io.BytesIO(data)).convert("RGBA")
        except Exception:
            return None

    def pack_name(self) -> str:
        return self.zip_path.stem


# ---------------------------------------------------------------------------
# .properties parsing (Java-ish: # ! comments, = or : separators, \ continuation)
# ---------------------------------------------------------------------------
def _unescape(value: str) -> str:
    out, i, n = [], 0, len(value)
    while i < n:
        c = value[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        nxt = value[i + 1]
        if nxt == "u" and i + 5 < n:
            try:
                out.append(chr(int(value[i + 2 : i + 6], 16)))
                i += 6
                continue
            except ValueError:
                pass
        out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
        i += 2
    return "".join(out)


def _split_kv(line: str) -> Tuple[str, str]:
    i, n = 0, len(line)
    while i < n:
        if line[i] == "\\":
            i += 2
            continue
        if line[i] in "=:":
            return line[:i], line[i + 1 :]
        i += 1
    return line, ""


def parse_properties(raw: bytes) -> Dict[str, str]:
    text = raw.decode("utf-8-sig", errors="replace")
    props: Dict[str, str] = {}
    pending = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if pending:
            line = pending + line
            pending = ""
        elif not line or line[0] in "#!":
            continue
        if line.endswith("\\") and not line.endswith("\\\\"):
            pending = line[:-1]
            continue
        key, value = _split_kv(line)
        key = _unescape(key).strip()
        if key:
            props[key] = _unescape(value).strip()
    return props


RARITY_RE = re.compile(r"\b(RARE|EPIC|LEGENDARY|MYTHIC|UNCOMMON|COMMON)\b[!:]*\s*", re.IGNORECASE)


ROMAN_RE = re.compile(r"\s+(I{1,3}|IV|VI{0,3}|IX|XI{0,2}|V|X)$")


def split_level(token: str) -> Tuple[str, str]:
    """('Volley III') -> ('Volley', 'III');  ('Volley') -> ('Volley', '')."""
    m = ROMAN_RE.search(token)
    return (token[: m.start()].strip(), m.group(1)) if m else (token, "")


ALT_RE = re.compile(r"(?:^|[_\-\s])(alt|alternate|alt\d+|v\d+)(?:[_\-\s]|\d*$)", re.IGNORECASE)


def split_on_vocab(label: str, vocab: Sequence[str]) -> List[str]:
    """Split a folder-derived name into known enchant tokens, longest first.
    'Billionaire Executioner' -> ['Billionaire', 'Executioner']."""
    words = label.split()
    by_len = sorted(vocab, key=lambda v: -len(v.split()))
    parts, i = [], 0
    while i < len(words):
        for cand in by_len:
            n = len(cand.split())
            if " ".join(words[i : i + n]).lower() == cand.lower():
                parts.append(cand)
                i += n
                break
        else:
            return []  # something outside the vocabulary: leave the name alone
    return parts


def mark_variants(items: Sequence[CitItem]) -> None:
    """Two textures, same enchants: tag them from the texture name if it says
    alt/alternate, otherwise number them."""
    groups: Dict[str, List[CitItem]] = collections.defaultdict(list)
    for e in items:
        groups[e.label].append(e)
    for label, group in groups.items():
        if len(group) < 2:
            continue
        n = 0
        for e in group:
            stem = PurePosixPath(e.texture_path).stem
            if ALT_RE.search(stem):
                e.label = f"{label} (alt)"
            else:
                n += 1
                e.label = label if n == 1 else f"{label} ({n})"


ALT_RE = re.compile(r"(?:^|[_\- ])(alt|alternate)(\d*)(?:$|[_\- ])", re.IGNORECASE)


def tokenize_against(text: str, vocab: Sequence[str]) -> List[str]:
    """Split a folder/display name into known enchant tokens, longest first.
    Returns [] unless the whole string is consumed by two or more tokens."""
    remaining = text.strip()
    found: List[str] = []
    ordered = sorted(vocab, key=len, reverse=True)
    while remaining:
        for token in ordered:
            if remaining.lower().startswith(token.lower()):
                found.append(token)
                remaining = remaining[len(token):].lstrip(" +-,")
                break
        else:
            return []
    return found if len(found) > 1 else []


def finalize_labels(items: Sequence[CitItem]) -> None:
    """Give every entry the same shape of name: enchant tokens joined by ' + '.

    A level is dropped when the pack has other forms of that enchant ('Volley'
    and 'Volley III' both present -> both read 'Volley'), then put back for any
    group of entries that dropping it would have made indistinguishable
    ("Perun's Wrath II" vs "... III", where those are the only two textures).
    Names that never had lore are split against the vocabulary the lore
    provides, so a folder like 'Any Billionaire Executioner' reads the same way
    a lore-named combo does.
    """
    levels: Dict[str, set] = collections.defaultdict(set)
    for e in items:
        for token in e.lore:
            base, lv = split_level(token)
            levels[base].add(lv)

    def render(token: str) -> str:
        base, lv = split_level(token)
        return f"{base} {lv}" if lv and levels[base] == {lv} else base

    combos: Dict[frozenset, str] = {}
    for e in items:
        if not e.from_lore:
            continue
        parts: List[str] = []
        for token in e.lore:
            name = render(token)
            if name and name not in parts:
                parts.append(name)
        if parts:
            e.label = " + ".join(parts)
            if len(parts) > 1:
                combos.setdefault(frozenset(parts), e.label)

    # Put levels back where two entries would otherwise read identically.
    def with_level(token: str) -> str:
        base, lv = split_level(token)
        return f"{base} {lv}" if lv else base

    groups: Dict[str, List[CitItem]] = collections.defaultdict(list)
    for e in items:
        groups[e.label].append(e)
    for group in groups.values():
        if len(group) < 2:
            continue
        detailed = []
        for e in group:
            parts: List[str] = []
            for token in e.lore:
                name = with_level(token)
                if name and name not in parts:
                    parts.append(name)
            detailed.append(" + ".join(parts) if parts else e.label)
        if len(set(detailed)) == len(group):
            for e, label in zip(group, detailed):
                e.label = label

    vocab = sorted({render(t) for tokens in (e.lore for e in items) for t in tokens})
    for e in items:
        if e.from_lore:
            continue
        parts = tokenize_against(e.label, vocab)
        if not parts:
            continue
        # Reuse the wording a lore-named entry already uses for this same combo,
        # so the two rows read identically instead of in a different order.
        e.label = combos.get(frozenset(parts), " + ".join(parts))

    # Same name on two textures is fine; flag it only when the file says "alt".
    counts = collections.Counter(e.label for e in items)
    alt_seen: Dict[str, int] = collections.defaultdict(int)
    for e in items:
        if counts[e.label] < 2:
            continue
        if ALT_RE.search(PurePosixPath(e.texture_path).stem):
            alt_seen[e.label] += 1
            n = alt_seen[e.label]
            e.label = f"{e.label} (Alt)" if n == 1 else f"{e.label} (Alt {n})"


def ordered_lore(props: Dict[str, str]) -> List[str]:
    """Lore values in naming order: the wildcard line (the base enchant) first,
    then numbered lines ascending."""
    wildcard, numbered = [], []
    for k, v in props.items():
        m = LORE_KEY_RE.match(k)
        if not m:
            continue
        cleaned = clean_label(v)
        if not cleaned:
            continue
        if m.group(1) == "*":
            wildcard.append(cleaned)
        else:
            numbered.append((int(m.group(1)), cleaned))
    return wildcard + [v for _, v in sorted(numbered)]


def clean_label(value: str) -> str:
    """Turn a CIT match expression into something human readable."""
    value = MATCH_PREFIX_RE.sub("", value)
    value = re.sub(r"(?i)\\u00a7.", "", value)
    value = SECTION_CODE_RE.sub("", value)
    value = value.replace("*", "").replace("?", "")
    value = RARITY_RE.sub("", value)
    # "Combo: Perun's Wrath" -> "Perun's Wrath"
    head, sep, tail = value.partition(":")
    if sep and tail.strip() and len(head.split()) <= 3:
        value = tail
    return re.sub(r"\s+", " ", value).strip(" -|,!")


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_LINE_COMMENT_RE = re.compile(r"(?m)//[^\n]*$")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def lenient_json(raw: bytes):
    """Minecraft parses .mcmeta with Gson in lenient mode: trailing commas and
    comments are legal there but not in json.loads. Retry after cleaning."""
    text = raw.decode("utf-8-sig", errors="replace")
    try:
        return json.loads(text)
    except Exception:
        pass
    cleaned = _BLOCK_COMMENT_RE.sub("", _LINE_COMMENT_RE.sub("", text))
    cleaned = _TRAILING_COMMA_RE.sub(r"\1", cleaned)
    try:
        return json.loads(cleaned)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Animation
# ---------------------------------------------------------------------------
@dataclass
class Animation:
    frames: List[Image.Image]
    durations: List[int]
    interpolate: bool = False

    @property
    def animated(self) -> bool:
        return len(self.frames) > 1

    @property
    def cycle(self) -> int:
        return max(1, sum(self.durations))

    def change_ticks(self) -> set:
        """Ticks (within one cycle) at which this animation's output changes."""
        if not self.animated:
            return {0}
        if self.interpolate:
            return set(range(self.cycle))
        ticks, t = set(), 0
        for d in self.durations:
            ticks.add(t)
            t += d
        return ticks

    def signature(self) -> str:
        h = hashlib.sha1()
        for img, dur in zip(self.frames, self.durations):
            h.update(f"{img.size}:{dur}".encode())
            h.update(img.tobytes())
        return h.hexdigest()

    def sample(self, tick: int) -> Image.Image:
        if not self.animated:
            return self.frames[0]
        t = tick % self.cycle
        for i, d in enumerate(self.durations):
            if t < d:
                cur = self.frames[i]
                if self.interpolate and d > 1 and t > 0:
                    nxt = self.frames[(i + 1) % len(self.frames)]
                    if nxt.size == cur.size:
                        return Image.blend(cur, nxt, t / d)
                return cur
            t -= d
        return self.frames[-1]


def split_strip(img: Image.Image, frame_h: int) -> List[Image.Image]:
    count = max(1, img.height // max(1, frame_h))
    return [img.crop((0, i * frame_h, img.width, (i + 1) * frame_h)) for i in range(count)]


def load_animation(pack: PackReader, texture_path: str) -> Optional[Animation]:
    img = pack.image(texture_path)
    if img is None:
        return None

    # Fallback used whenever metadata is absent or unusable: a texture taller
    # than it is wide is a vertical frame strip, never a single squashed icon.
    def bare(reason_frametime: int = 1) -> Animation:
        if img.height > img.width and img.height % img.width == 0:
            strip = split_strip(img, img.width)
            return Animation(strip, [reason_frametime] * len(strip))
        if img.height != img.width:  # odd aspect: keep the top square only
            side = min(img.width, img.height)
            return Animation([img.crop((0, 0, side, side))], [1])
        return Animation([img], [1])

    meta_raw = pack.read(texture_path + ".mcmeta")
    if not meta_raw:
        return bare()
    meta = lenient_json(meta_raw)
    if not isinstance(meta, dict):
        return bare()
    anim = meta.get("animation")
    if not isinstance(anim, dict):
        return bare()

    frame_h = img.width
    if anim.get("width") and anim.get("height"):
        try:
            aw, ah = int(anim["width"]), int(anim["height"])
            frame_h = max(1, round(img.width * ah / aw))
        except (ValueError, ZeroDivisionError):
            pass

    strip = split_strip(img, frame_h)
    count = len(strip)
    try:
        default_time = max(1, int(anim.get("frametime", 1) or 1))
    except (TypeError, ValueError):
        default_time = 1
    if count <= 1:
        return bare(default_time)

    order = anim.get("frames")
    frames: List[Image.Image] = []
    durations: List[int] = []
    if isinstance(order, list) and order:
        for entry in order:
            try:
                if isinstance(entry, dict):
                    idx = int(entry.get("index", 0))
                    time = max(1, int(entry.get("time", default_time)))
                else:
                    idx, time = int(entry), default_time
            except (TypeError, ValueError):
                continue
            if 0 <= idx < count:
                frames.append(strip[idx])
                durations.append(time)
    if not frames:
        frames = strip
        durations = [default_time] * count

    return Animation(frames, durations, bool(anim.get("interpolate", False)))



# Bow render states, in draw order. OptiFine exposes these as texture.<state>.
BOW_STATES = ["bow_standby", "bow_pulling_0", "bow_pulling_1", "bow_pulling_2"]
DEFAULT_BOW_TICKS = (20, 10, 10, 20)


@dataclass
class StateCycle:
    """A bow: several state textures shown in sequence. The per-texture
    animation is sampled on the *global* clock, so a shimmer keeps advancing
    across a state change instead of restarting from frame 0."""

    states: List[Tuple[str, Animation]]
    schedule: List[Tuple[int, int]]  # (state index, ticks to hold)

    @property
    def pull_cycle(self) -> int:
        return max(1, sum(d for _, d in self.schedule))

    @property
    def animated(self) -> bool:
        return len(self.states) > 1 or any(a.animated for _, a in self.states)

    @property
    def cycle(self) -> int:
        total = self.pull_cycle
        for _, a in self.states:
            total = _lcm(total, a.cycle)
        return total

    def state_at(self, tick: int) -> int:
        t = tick % self.pull_cycle
        for idx, dur in self.schedule:
            if t < dur:
                return idx
            t -= dur
        return self.schedule[-1][0]

    def sample(self, tick: int) -> Image.Image:
        return self.states[self.state_at(tick)][1].sample(tick)

    def change_ticks(self) -> set:
        total, ticks, t = self.cycle, set(), 0
        for start in range(0, total, self.pull_cycle):
            for idx, dur in self.schedule:
                ticks.add(start + t)
                anim = self.states[idx][1]
                if anim.animated:
                    for k in range(start + t, min(start + t + dur, total)):
                        if k % anim.cycle in anim.change_ticks():
                            ticks.add(k)
                t += dur
            t = 0
        return {k for k in ticks if k < total}

    def signature(self) -> str:
        h = hashlib.sha1()
        for name, anim in self.states:
            h.update(name.encode())
            h.update(anim.signature().encode())
        h.update(str(self.schedule).encode())
        return h.hexdigest()


def build_bow_states(pack: PackReader, prop_path: str, ns: str, props: Dict[str, str],
                     ticks: Sequence[int]) -> Optional[StateCycle]:
    resolved: List[Tuple[str, Animation, str]] = []
    for state in BOW_STATES:
        value = props.get(f"texture.{state}")
        if not value:
            continue
        path = None
        for cand in texture_candidates(prop_path, ns, value):
            if pack.resolve(cand):
                path = norm_path(cand)
                break
        if path is None:
            continue
        anim = load_animation(pack, path)
        if anim is not None:
            resolved.append((state, anim, path))
    if len(resolved) < 2:
        return None

    # Collapse consecutive states that reuse the same texture: most bows here
    # point all three pulling states at one file, so that becomes standby+pull.
    states: List[Tuple[str, Animation]] = []
    schedule: List[Tuple[int, int]] = []
    for i, (state, anim, path) in enumerate(resolved):
        hold = ticks[min(i, len(ticks) - 1)]
        if states and resolved[i - 1][2] == path:
            idx, prev = schedule[-1]
            schedule[-1] = (idx, prev + hold)
            continue
        states.append((state, anim))
        schedule.append((len(states) - 1, hold))
    if len(states) < 2:
        return None
    return StateCycle(states, schedule)


# ---------------------------------------------------------------------------
# CIT discovery
# ---------------------------------------------------------------------------
@dataclass
class CitItem:
    properties: str
    namespace: str
    label: str
    items: List[str]
    lore: List[str]
    texture_path: str
    animation: object  # Animation or StateCycle
    props: Dict[str, str] = field(default_factory=dict)
    duplicates: int = 0  # extra .properties files that render identically
    from_lore: bool = False

    @property
    def type_hint(self) -> str:
        """Best guess at an item 'category'. Hook for future sorting."""
        base = self.items[0] if self.items else ""
        base = base.split(":")[-1]
        for suffix in ("sword", "axe", "pickaxe", "shovel", "hoe", "bow"):
            if base.endswith(suffix):
                return suffix
        for suffix in ("helmet", "chestplate", "leggings", "boots"):
            if base.endswith(suffix):
                return "armor"
        return base or "other"


def texture_candidates(prop_path: str, ns: str, value: str) -> List[str]:
    v = value.strip().replace("\\", "/")
    if v.lower().endswith(".png"):
        v = v[:-4]
    base = str(PurePosixPath(prop_path).parent)
    out: List[str] = []
    if v.startswith("~/"):
        out.append(f"assets/{ns}/optifine/{v[2:]}.png")
    elif v.startswith("/"):
        out.append(f"assets/{ns}/{v.lstrip('/')}.png")
    else:
        out.append(f"{base}/{v}.png")
        out.append(f"assets/{ns}/{v}.png")
        out.append(f"assets/{ns}/textures/{v}.png")
        if ":" in v:
            tns, rest = v.split(":", 1)
            out.append(f"assets/{tns}/textures/{rest}.png")
    return [norm_path(p) for p in out]


def texture_from_model(pack: PackReader, prop_path: str, ns: str, model_val: str, depth: int = 0) -> Optional[str]:
    """Pull layer0 out of a CIT model JSON (follows one or two parent hops)."""
    if depth > 3:
        return None
    for cand in texture_candidates(prop_path, ns, model_val.replace(".json", "") + ".png"):
        json_path = cand[:-4] + ".json"
        raw = pack.read(json_path)
        if raw is None:
            continue
        try:
            model = json.loads(raw.decode("utf-8-sig", errors="replace"))
        except Exception:
            return None
        textures = model.get("textures") or {}
        ref = textures.get("layer0") or next(
            (v for v in textures.values() if isinstance(v, str) and not v.startswith("#")),
            None,
        )
        if ref:
            tns, _, rest = ref.partition(":")
            if not rest:
                tns, rest = ns, ref
            for path in (
                f"assets/{tns}/textures/{rest}.png",
                f"{PurePosixPath(json_path).parent}/{rest}.png",
            ):
                if pack.resolve(path):
                    return norm_path(path)
        parent = model.get("parent")
        if isinstance(parent, str):
            return texture_from_model(pack, json_path, ns, parent, depth + 1)
        return None
    return None


def resolve_texture(pack: PackReader, prop_path: str, ns: str, props: Dict[str, str]) -> Optional[str]:
    explicit = props.get("texture")
    if not explicit:
        sub = sorted(k for k in props if k.lower().startswith("texture."))
        if sub:
            explicit = props[sub[0]]

    if explicit:
        for cand in texture_candidates(prop_path, ns, explicit):
            if pack.resolve(cand):
                return norm_path(cand)

    model = props.get("model") or next(
        (props[k] for k in sorted(props) if k.lower().startswith("model.")), None
    )
    if model:
        found = texture_from_model(pack, prop_path, ns, model)
        if found:
            return found

    # OptiFine default: <properties basename>.png next to the file.
    stem = PurePosixPath(prop_path).stem
    parent = PurePosixPath(prop_path).parent
    fallbacks = [f"{parent}/{stem}.png"]
    for item in (props.get("items") or props.get("matchItems") or "").split():
        fallbacks.append(f"{parent}/{item.split(':')[-1]}.png")
    for cand in fallbacks:
        if pack.resolve(cand):
            return norm_path(cand)
    return None


def collect_cit_items(pack: PackReader,
                      bow_ticks: Sequence[int] = DEFAULT_BOW_TICKS
                      ) -> Tuple[List[CitItem], List[Tuple[str, str]]]:
    found: List[CitItem] = []
    skipped: List[Tuple[str, str]] = []

    for name in sorted(pack.names):
        m = CIT_RE.match(name)
        if not m:
            continue
        ns = m.group("ns")
        raw = pack.read(name)
        if raw is None:
            continue
        props = parse_properties(raw)

        if props.get("type", "item").lower() != "item":
            skipped.append((name, f"type={props.get('type')}"))
            continue

        tex = resolve_texture(pack, name, ns, props)
        if not tex:
            skipped.append((name, "no texture resolved"))
            continue

        anim = build_bow_states(pack, name, ns, props, bow_ticks)
        if anim is None:
            anim = load_animation(pack, tex)
        else:
            # standby is the representative still for a bow
            for cand in texture_candidates(pack and name, ns, props.get("texture.bow_standby", "")):
                if pack.resolve(cand):
                    tex = norm_path(cand)
                    break
        if anim is None:
            skipped.append((name, f"unreadable texture {tex}"))
            continue

        items = (props.get("items") or props.get("matchItems") or "").split()
        lore = ordered_lore(props)
        from_lore = False
        label = clean_label(props.get("nbt.display.Name", ""))
        if not label:
            from_lore = bool(lore)
            # Combo name: base enchant (Lore.*) first, then the numbered lines.
            seen_l: List[str] = []
            for v in lore:
                if v and v not in seen_l:
                    seen_l.append(v)
            label = " + ".join(seen_l)
        folder = clean_label(PurePosixPath(name).parent.name)
        folder = re.sub(r"^(?:any|all)\s+", "", folder, flags=re.IGNORECASE).strip()
        if label and " " not in label and not lore and " " in folder:
            label = folder  # e.g. nbt Name "BillExe" -> folder "Billionaire Executioner"
        if not label:
            label = folder or PurePosixPath(name).stem

        found.append(
            CitItem(
                properties=name,
                namespace=ns,
                label=label,
                items=items,
                lore=[l for l in lore if l],
                from_lore=from_lore,
                texture_path=tex,
                animation=anim,
                props=props,
            )
        )
    return found, skipped


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------
def frame_hash(anim) -> str:
    return anim.signature()


def label_score(e: CitItem) -> Tuple[int, int, int]:
    """Rank candidate names: combos beat multi-word names beat internal tokens."""
    return (" + " in e.label, " " in e.label, len(e.label))


def dedupe_items(items: List[CitItem], mode: str) -> List[CitItem]:
    """Packs often ship one .properties per lore-line index / damage value, all
    pointing at the same art. Collapse those so each visual appears once."""
    if mode == "none":
        return items
    kept: Dict[str, CitItem] = {}
    order: List[str] = []
    for e in items:
        key = e.texture_path if mode == "texture" else frame_hash(e.animation)
        if key in kept:
            kept[key].duplicates += 1
            if label_score(e) > label_score(kept[key]):
                e.duplicates = kept[key].duplicates
                kept[key] = e
        else:
            kept[key] = e
            order.append(key)
    return [kept[k] for k in order]


# ---------------------------------------------------------------------------
# Sorting -- add new strategies here, they become --sort values automatically.
# ---------------------------------------------------------------------------
SORTERS: Dict[str, Optional[Callable[[CitItem], object]]] = {
    "discovery": None,  # zip order (alphabetical by properties path)
    "path": lambda e: e.properties.lower(),
    "name": lambda e: e.label.lower(),
    "item": lambda e: ((e.items[0] if e.items else "~"), e.label.lower()),
    "texture": lambda e: e.texture_path.lower(),
    # Rough category derived from the matched vanilla item id.
    "type": lambda e: (e.type_hint, (e.items[0] if e.items else ""), e.label.lower()),
    # Example of a pack-specific key; uncomment and adapt:
    # "tier": lambda e: (e.props.get("nbt.PitSim.tier", "zzz"), e.label.lower()),
}


def sort_items(items: List[CitItem], strategy: str) -> List[CitItem]:
    key = SORTERS.get(strategy, None)
    return items if key is None else sorted(items, key=key)


# ---------------------------------------------------------------------------
# GUI background
# ---------------------------------------------------------------------------
def draw_slot(d: ImageDraw.ImageDraw, x: int, y: int) -> None:
    """Slot border is 18x18 around the 16x16 item origin at (x, y)."""
    x0, y0 = x - 1, y - 1
    d.rectangle([x0, y0, x0 + 17, y0 + 17], fill=SLOT_BG)
    d.line([(x0, y0), (x0 + 17, y0)], fill=SLOT_DARK)
    d.line([(x0, y0), (x0, y0 + 17)], fill=SLOT_DARK)
    d.line([(x0 + 17, y0 + 1), (x0 + 17, y0 + 17)], fill=SLOT_LIGHT)
    d.line([(x0 + 1, y0 + 17), (x0 + 17, y0 + 17)], fill=SLOT_LIGHT)


def procedural_gui() -> Image.Image:
    img = Image.new("RGBA", (GUI_W, GUI_H), PANEL)
    d = ImageDraw.Draw(img)
    d.line([(0, 0), (GUI_W - 1, 0)], fill=PANEL_LIGHT)
    d.line([(0, 0), (0, GUI_H - 1)], fill=PANEL_LIGHT)
    d.line([(GUI_W - 1, 0), (GUI_W - 1, GUI_H - 1)], fill=PANEL_DARK)
    d.line([(0, GUI_H - 1), (GUI_W - 1, GUI_H - 1)], fill=PANEL_DARK)
    for x, y in SLOTS + ARMOR_SLOTS + CRAFT_SLOTS + RESULT_SLOT:
        draw_slot(d, x, y)
    return img


def load_gui(pack: PackReader, external: Optional[Path]) -> Image.Image:
    src = None
    if external:
        try:
            src = Image.open(external).convert("RGBA")
        except Exception as exc:
            print(f"[warn] could not read --gui {external}: {exc}", file=sys.stderr)
    if src is None:
        src = pack.image(INVENTORY_GUI)
    if src is None:
        return procedural_gui()
    if src.width < GUI_W or src.height < GUI_H:
        # HD GUI: scale so the 256-wide sheet maps back to GUI pixels.
        return procedural_gui()
    factor = max(1, src.width // 256) if src.width >= 256 else 1
    panel = src.crop((0, 0, GUI_W * factor, GUI_H * factor))
    if factor > 1:
        panel = panel.resize((GUI_W, GUI_H), Image.LANCZOS)
    return panel


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Slot numbering + key sheet
# ---------------------------------------------------------------------------
# Minimal 3x5 bitmap digits so slot numbers stay crisp at any --scale.
DIGITS = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
}
DIGIT_W, DIGIT_H = 3, 5


def draw_number(px, x: int, y: int, text: str, fg=(255, 255, 255, 255), shadow=(0, 0, 0, 255)) -> int:
    """Blit `text` at (x, y) into a pixel-access object. Returns width drawn."""
    cx = x
    for ch in text:
        glyph = DIGITS.get(ch)
        if glyph is None:
            cx += DIGIT_W + 1
            continue
        for row, bits in enumerate(glyph):
            for col, bit in enumerate(bits):
                if bit == "1":
                    px[cx + col + 1, y + row + 1] = shadow
        for row, bits in enumerate(glyph):
            for col, bit in enumerate(bits):
                if bit == "1":
                    px[cx + col, y + row] = fg
        cx += DIGIT_W + 1
    return cx - x


def number_overlay(count: int) -> Image.Image:
    """1x overlay putting each slot's index in its bottom-right corner."""
    layer = Image.new("RGBA", (GUI_W, GUI_H), (0, 0, 0, 0))
    px = layer.load()
    for i in range(count):
        x, y = SLOTS[i]
        label = str(i + 1)
        w = len(label) * (DIGIT_W + 1) - 1
        draw_number(px, x + 16 - w - 1, y + 16 - DIGIT_H - 2, label)
    return layer


def disambiguate(entries: Sequence[CitItem]) -> List[str]:
    """Colour variants often share a display name; fall back to the texture
    file stem so every line of the key is distinguishable."""
    counts = collections.Counter(e.label for e in entries)
    out = []
    for e in entries:
        if counts[e.label] > 1:
            out.append(f"{e.label} [{PurePosixPath(e.texture_path).stem}]")
        else:
            out.append(e.label)
    return out



# ---------------------------------------------------------------------------
# Key sheet (primary output): numbered rows of icon + label, 36 per image
# ---------------------------------------------------------------------------
FONT_CANDIDATES = [
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]

KEY_COLS = 2
KEY_ROWS = 18  # 2 x 18 = 36 per image
SLOT_FILL = (58, 58, 62, 255)
SLOT_EDGE = (92, 92, 98, 255)
TEXT_MAIN = (232, 232, 234, 255)
TEXT_DIM = (150, 150, 156, 255)
TEXT_NUM = (128, 128, 134, 255)


def tint(color, amount: int = 14):
    """Slightly lighter opaque variant of the background, for row striping."""
    r, g, b, a = color
    if a == 0:  # transparent background: stripe with a faint dark grey
        return (255, 255, 255, 16)
    return (min(255, r + amount), min(255, g + amount), min(255, b + amount), 255)


def pick_font(size: int, explicit: Optional[Path] = None):
    paths = ([str(explicit)] if explicit else []) + FONT_CANDIDATES
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def key_rows(entries: Sequence[CitItem]) -> List[Tuple[str, str]]:
    """(main label, dim suffix). The suffix stays empty unless two entries on
    the page would otherwise be indistinguishable."""
    return [(e.label, "") for e in entries]


def build_key_base(entries, scale: int, bg, header: Optional[str], font_path: Optional[Path]):
    """Render everything that never moves; returns (image, icon boxes, icon px)."""
    icon = 16 * scale
    pad = max(4, icon // 4)
    row_h = icon + pad
    fs = max(11, int(icon * 0.34))
    font = pick_font(fs, font_path)
    font_dim = pick_font(max(9, int(fs * 0.8)), font_path)
    font_head = pick_font(max(12, int(fs * 1.15)), font_path)

    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    rows = key_rows(entries)
    text_w = 0
    for main, dim in rows:
        w = probe.textlength(main, font=font)
        if dim:
            w += probe.textlength("   " + dim, font=font_dim)
        text_w = max(text_w, int(w))

    num_w = int(probe.textlength("36", font=font)) + pad
    col_w = num_w + pad + icon + pad + text_w + pad * 2
    head_h = int(fs * 2.2) if header else pad
    n_rows = min(KEY_ROWS, max(1, math.ceil(len(entries) / KEY_COLS)))

    img = Image.new("RGBA", (col_w * KEY_COLS + pad, head_h + n_rows * row_h + pad), bg)
    d = ImageDraw.Draw(img)
    if header:
        d.text((pad + 2, int(fs * 0.5)), header, fill=TEXT_MAIN, font=font_head)

    stripe = tint(bg)
    boxes: List[Tuple[int, int]] = []
    for i, (main, dim) in enumerate(rows):
        col, row = divmod(i, n_rows)
        x = pad + col * col_w
        y = head_h + row * row_h
        if row % 2 == 0:
            d.rectangle([x, y, x + col_w - pad, y + row_h - 1], fill=stripe)

        num = str(i + 1)
        nw = probe.textlength(num, font=font)
        d.text((x + num_w - nw, y + (row_h - fs) // 2 - 1), num, fill=TEXT_NUM, font=font)

        ix, iy = x + num_w + pad, y + pad // 2
        d.rectangle([ix - 2, iy - 2, ix + icon + 1, iy + icon + 1], fill=SLOT_FILL, outline=SLOT_EDGE)
        boxes.append((ix, iy))

        tx = ix + icon + pad
        ty = y + (row_h - fs) // 2 - 1
        d.text((tx, ty), main, fill=TEXT_MAIN, font=font)
        if dim:
            off = probe.textlength(main + "   ", font=font)
            d.text((tx + off, ty + 2), dim, fill=TEXT_DIM, font=font_dim)

    return img, boxes, icon


def render_key_sheet(base: Image.Image, boxes, icon: int, entries, tick: int) -> Image.Image:
    frame = base.copy()
    for e, (x, y) in zip(entries, boxes):
        tex = e.animation.sample(tick)
        if tex.size != (icon, icon):
            resample = Image.NEAREST if icon % max(tex.width, 1) == 0 else Image.LANCZOS
            tex = tex.resize((icon, icon), resample)
        frame.alpha_composite(tex, (x, y))
    return frame


def render_key(entries: Sequence[CitItem], scale: int, bg, header: Optional[str]) -> Image.Image:
    """Legend: index, icon and label for every slot on the page."""
    font = ImageFont.load_default()
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    rows = len(entries)
    cols = 1 if rows <= 18 else 2
    per_col = math.ceil(rows / cols)

    labels = disambiguate(entries)
    label_w = 0
    for lbl in labels:
        label_w = max(label_w, int(probe.textlength(lbl[:40], font=font)))
    label_w = min(max(label_w, 40), 210)

    num_w, icon = 12, 16
    col_w = num_w + icon + 4 + label_w + 10
    row_h = icon + 4
    head_h = HEADER_H if header else 0
    img = Image.new("RGBA", (col_w * cols + 8, head_h + per_col * row_h + 8), bg)
    d = ImageDraw.Draw(img)
    if header:
        d.text((5, 2), header, fill=(220, 220, 220, 255), font=font)

    px = img.load()
    for i, e in enumerate(entries):
        col, row = divmod(i, per_col)
        x = 4 + col * col_w
        y = head_h + 4 + row * row_h
        num = str(i + 1)
        draw_number(px, x + num_w - (len(num) * (DIGIT_W + 1) - 1) - 2, y + 6, num)
        tex = e.animation.sample(0)
        if tex.size != (icon, icon):
            tex = tex.resize((icon, icon), Image.NEAREST if icon % max(tex.width, 1) == 0 else Image.LANCZOS)
        img.alpha_composite(tex, (x + num_w, y + 2))
        d.text((x + num_w + icon + 4, y + 5), labels[i][:40], fill=(230, 230, 230, 255), font=font)

    return img.resize((img.width * scale, img.height * scale), Image.NEAREST)


def key_text(entries: Sequence[CitItem], page: int, pages: int) -> str:
    """Paste-into-Discord version of the legend."""
    lines = [f"Page {page}/{pages} - slots left to right, top to bottom", "```"]
    labels = disambiguate(entries)
    for i, e in enumerate(entries, start=1):
        extra = []
        if e.animation.animated:
            extra.append("anim")
        if e.duplicates:
            extra.append(f"x{e.duplicates + 1}")
        suffix = f"  ({', '.join(extra)})" if extra else ""
        lines.append(f"{i:>2}. {e.label}{suffix}")
    lines.append("```")
    return "\n".join(lines)


def paste_item(canvas: Image.Image, tex: Image.Image, x: int, y: int, scale: int) -> None:
    target = 16 * scale
    if tex.size != (target, target):
        resample = Image.NEAREST if target % max(tex.width, 1) == 0 else Image.LANCZOS
        tex = tex.resize((target, target), resample)
    canvas.alpha_composite(tex, (x * scale, y * scale))


def render_header(text: str, width: int, fg=(220, 220, 220, 255), bg=(0, 0, 0, 0)) -> Image.Image:
    img = Image.new("RGBA", (width, HEADER_H), bg)
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None
    d.text((1, 1), text[: max(1, width // 6)], fill=fg, font=font)
    return img


def render_page(
    gui: Image.Image,
    entries: Sequence[CitItem],
    tick: int,
    scale: int,
    bg: Tuple[int, int, int, int],
    header: Optional[str],
    pad: int = 6,
    numbers: bool = False,
) -> Image.Image:
    panel = gui.resize((GUI_W * scale, GUI_H * scale), Image.NEAREST)
    for entry, (x, y) in zip(entries, SLOTS):
        paste_item(panel, entry.animation.sample(tick), x, y, scale)
    if numbers:
        overlay = number_overlay(len(entries)).resize(
            (GUI_W * scale, GUI_H * scale), Image.NEAREST
        )
        panel.alpha_composite(overlay)

    head_h = HEADER_H * scale if header else 0
    canvas = Image.new(
        "RGBA",
        (panel.width + pad * 2 * scale, panel.height + head_h + pad * 2 * scale),
        bg,
    )
    if header:
        strip = render_header(header, GUI_W).resize((GUI_W * scale, head_h), Image.NEAREST)
        canvas.alpha_composite(strip, (pad * scale, pad * scale // 2))
    canvas.alpha_composite(panel, (pad * scale, head_h + pad * scale))
    return canvas


def _lcm(a: int, b: int) -> int:
    return abs(a * b) // math.gcd(a, b) if a and b else max(a, b, 1)


def build_timeline(entries: Sequence[CitItem], max_frames: int, max_cycle: int = 2048,
                   mode: str = "truncate"):
    """Ticks at which the sheet changes, plus each frame's duration in ms.

    When the exact loop needs more frames than the budget, 'truncate' keeps the
    opening stretch at true speed (correct motion, loop cuts early) while
    'sample' spreads frames across the whole loop (complete loop, but motion is
    slowed and short states can be skipped entirely).
    """
    anims = [e.animation for e in entries if e.animation.animated]
    if not anims:
        return [0], [TICK_MS]

    cycle = 1
    for a in anims:
        cycle = _lcm(cycle, a.cycle)
        if cycle > max_cycle:
            cycle = max(a.cycle for a in anims)
            break

    ticks: set = set()
    for a in anims:
        base = a.change_ticks()
        for start in range(0, cycle, a.cycle):
            ticks.update(start + t for t in base if start + t < cycle)
    ordered = sorted(ticks) or [0]

    end = cycle
    if len(ordered) > max_frames:
        if mode == "sample":
            step = len(ordered) / max_frames
            ordered = [ordered[int(i * step)] for i in range(max_frames)]
        else:
            end = ordered[max_frames]  # loop restarts here instead of at `cycle`
            ordered = ordered[:max_frames]

    durations = []
    for i, t in enumerate(ordered):
        nxt = ordered[i + 1] if i + 1 < len(ordered) else end
        durations.append(max(1, nxt - t) * TICK_MS)
    return ordered, durations


def save_animation(path: Path, frames, durations: List[int], fmt: str) -> None:
    """Frames may be an iterator: they are encoded one at a time so a long
    sheet never holds every frame in memory at once."""
    it = iter(frames)
    first = next(it)

    if fmt == "apng":
        first.save(path, save_all=True, append_images=it, duration=durations, loop=0)
        return

    base = first.convert("RGB")
    palette = base.quantize(colors=255, method=Image.MEDIANCUT)

    def quantized():
        for f in it:
            yield f.convert("RGB").quantize(palette=palette, dither=Image.Dither.NONE)

    base.quantize(palette=palette, dither=Image.Dither.NONE).save(
        path,
        save_all=True,
        append_images=quantized(),
        duration=durations,
        loop=0,
        disposal=1,
        optimize=False,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_color(value: str) -> Tuple[int, int, int, int]:
    v = value.strip().lstrip("#")
    if value.strip().lower() in ("none", "transparent"):
        return (0, 0, 0, 0)
    if len(v) == 6:
        return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16), 255)
    if len(v) == 8:
        return tuple(int(v[i : i + 2], 16) for i in (0, 2, 4, 6))  # type: ignore
    raise argparse.ArgumentTypeError(f"bad color: {value}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pack", type=Path, help="path to the resource pack .zip")
    p.add_argument("-o", "--out", type=Path, default=Path("cit_sheets"), help="output directory")
    p.add_argument("-s", "--scale", type=int, default=4, help="pixel scale (default 4)")
    p.add_argument("--sort", choices=sorted(SORTERS), default="discovery", help="slot ordering")
    p.add_argument("--dedupe", choices=("content", "texture", "none"), default="content",
                   help="collapse entries that render identically (default: content)")
    p.add_argument("--filter", default=None, help="regex; keep items whose path/name/id matches")
    p.add_argument("--gui", type=Path, default=None, help="external inventory.png to use as the background")
    p.add_argument("--bg", type=parse_color, default=parse_color("#181818"), help="canvas color, or 'none'")
    p.add_argument("--format", choices=("gif", "apng"), default="gif", help="animated output format")
    p.add_argument("--max-frames", type=int, default=120, help="cap on animation frames per page (0 = no cap)")
    p.add_argument("--long-anim", choices=("truncate", "sample"), default="truncate",
                   help="when the exact loop exceeds --max-frames: truncate keeps true "
                        "speed, sample covers the whole loop but slows motion")
    p.add_argument("--max-cycle", type=int, default=2048,
                   help="cap on the LCM loop length in ticks; above this, falls back to the "
                        "longest single cycle (animations with other periods then drift)")
    p.add_argument("--no-animation", action="store_true", help="render frame 0 only")
    p.add_argument("--layout", choices=("key", "inventory", "both"), default="key",
                   help="key sheet (default), inventory mockup, or both")
    p.add_argument("--bow-ticks", default=",".join(str(t) for t in DEFAULT_BOW_TICKS),
                   help="ticks to hold standby,pulling_0,pulling_1,pulling_2 (default 20,10,10,20)")
    p.add_argument("--font", type=Path, default=None, help="TTF to use for key labels")
    p.add_argument("--no-text", action="store_true", help="skip the paste-able .txt key")
    p.add_argument("--gif-only", action="store_true",
                   help="write only the animated sheet: no still PNG, no .txt key")
    p.add_argument("--numbers", action="store_true", help="stamp the slot index in each slot")
    p.add_argument("--no-header", action="store_true", help="omit the page caption strip")
    p.add_argument("--manifest", action="store_true", help="also write manifest.json")
    args = p.parse_args(argv)

    if args.scale < 1:
        p.error("--scale must be >= 1")
    if not args.pack.is_file():
        p.error(f"no such file: {args.pack}")

    try:
        pack = PackReader(args.pack)
    except zipfile.BadZipFile:
        p.error(f"not a zip file: {args.pack}")

    try:
        bow_ticks = [max(1, int(t)) for t in str(args.bow_ticks).split(",")]
    except ValueError:
        p.error("--bow-ticks must be comma-separated integers")
    items, skipped = collect_cit_items(pack, bow_ticks)

    if args.filter:
        rx = re.compile(args.filter, re.IGNORECASE)
        items = [
            e for e in items
            if rx.search(e.properties) or rx.search(e.label) or any(rx.search(i) for i in e.items)
        ]

    raw_count = len(items)
    items = dedupe_items(items, args.dedupe)
    if len(items) < raw_count:
        print(f"{raw_count} CIT entries collapsed to {len(items)} unique textures "
              f"(--dedupe {args.dedupe}; use --dedupe none to keep all)")

    if not items:
        print("No CIT item textures found (looked in assets/*/optifine|mcpatcher/cit/**.properties).")
        for path, why in skipped[:10]:
            print(f"  skipped {path}: {why}")
        return 1

    finalize_labels(items)
    items = sort_items(items, args.sort)
    gui = load_gui(pack, args.gui) if args.layout in ("inventory", "both") else None
    args.out.mkdir(parents=True, exist_ok=True)

    pages = [items[i : i + SLOTS_PER_PAGE] for i in range(0, len(items), SLOTS_PER_PAGE)]
    manifest = {"pack": pack.zip_path.name, "count": len(items), "sort": args.sort, "pages": []}

    for idx, page in enumerate(pages, start=1):
        header = None if args.no_header else f"{pack.pack_name()}  {idx}/{len(pages)}"
        animate = any(e.animation.animated for e in page) and not args.no_animation
        ticks, durations = ([0], [TICK_MS])
        if animate:
            ticks, durations = build_timeline(
                page, args.max_frames or 10**9, args.max_cycle, args.long_anim
            )
        outputs: List[str] = []

        if args.layout in ("key", "both"):
            base, boxes, icon = build_key_base(page, args.scale, args.bg, header, args.font)
            if not (args.gif_only and animate):
                still = render_key_sheet(base, boxes, icon, page, 0)
                still_path = args.out / f"key_{idx:02d}.png"
                still.save(still_path)
                outputs.append(still_path.name)
            if animate:
                frames = (render_key_sheet(base, boxes, icon, page, t) for t in ticks)
                anim_path = args.out / (
                    f"key_{idx:02d}_anim.png" if args.format == "apng" else f"key_{idx:02d}.gif"
                )
                save_animation(anim_path, frames, durations, args.format)
                outputs.append(anim_path.name)
            if not args.no_text and not args.gif_only:
                txt = args.out / f"key_{idx:02d}.txt"
                txt.write_text(key_text(page, idx, len(pages)), encoding="utf-8")
                outputs.append(txt.name)

        if args.layout in ("inventory", "both"):
            numbers = args.numbers
            still = render_page(gui, page, 0, args.scale, args.bg, header, numbers=numbers)
            still_path = args.out / f"page_{idx:02d}.png"
            still.save(still_path)
            outputs.append(still_path.name)
            if animate:
                frames = (
                    render_page(gui, page, t, args.scale, args.bg, header, numbers=numbers)
                    for t in ticks
                )
                anim_path = args.out / (
                    f"page_{idx:02d}_anim.png" if args.format == "apng" else f"page_{idx:02d}.gif"
                )
                save_animation(anim_path, frames, durations, args.format)
                outputs.append(anim_path.name)

        print(f"page {idx}/{len(pages)}: {len(page)} items, {len(ticks)} frame(s) -> {', '.join(outputs)}")
        manifest["pages"].append({
            "page": idx,
            "files": outputs,
            "frames": len(ticks),
            "entries": [
                {
                    "index": n + 1,
                    "label": e.label,
                    "items": e.items,
                    "lore": e.lore,
                    "texture": e.texture_path,
                    "properties": e.properties,
                    "animated": e.animation.animated,
                    "duplicate_properties": e.duplicates,
                    "type": e.type_hint,
                }
                for n, e in enumerate(page)
            ],
        })

    if args.manifest:
        (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    if skipped:
        print(f"\n{len(skipped)} properties file(s) skipped:")
        for path, why in skipped[:20]:
            print(f"  {path}: {why}")
        if len(skipped) > 20:
            print(f"  ... and {len(skipped) - 20} more")

    pack.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
