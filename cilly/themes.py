"""Themes and fonts: how the panel looks.

A theme sets colors, fonts and text sizes.  Its colors are the tokens in style.css's :root that
hold a plain color, and it only lists the ones it changes, so Classic lists none.  The panel
has its own themes, and you can save your own in config.json under "appearance".  The
standard theme is what every browser and paired device shows.

Fonts you add are kept in <data dir>/fonts/, with fonts.json listing them.  Only font files
are taken (WOFF2, WOFF, TrueType, OpenType), checked by their first bytes, and they never
leave this PC except to the panel's own pages.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import unicodedata

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
HEX = re.compile(r"#[0-9a-f]{6}")
ID_RE = re.compile(r"[0-9a-f]{8}")
NAME_MAX = 40
SIZES = range(75, 151, 5)  # text sizes in percent, for the font, the chat font and the code font
# how shapes look besides the colors: classic, or terminal (static/theme/terminal.css: square
# corners, pixel icons and cursors)
STYLES = ("classic", "terminal")
MAX_THEMES = 50
MAX_FONT = 4 << 20
MAX_FONTS = 20

# the fonts the panel has: key -> (name, CSS font-family)
BUILTIN_FONTS = {
    "geist": ("Geist", '"Geist", system-ui, sans-serif'),
    "geist-mono": ("Geist Mono", '"Geist Mono", ui-monospace, monospace'),
    "geist-pixel-square": ("Geist Pixel Square", '"Geist Pixel Square", "Geist Mono", monospace'),
    "geist-pixel-grid": ("Geist Pixel Grid", '"Geist Pixel Grid", "Geist Pixel Square", monospace'),
    "geist-pixel-line": ("Geist Pixel Line", '"Geist Pixel Line", "Geist Pixel Square", monospace'),
    "geist-pixel-triangle": ("Geist Pixel Triangle", '"Geist Pixel Triangle", "Geist Pixel Square", monospace'),
    "geist-pixel-circle": ("Geist Pixel Circle", '"Geist Pixel Circle", "Geist Pixel Square", monospace'),
    "pixelify-sans": ("Pixelify Sans", '"Pixelify Sans", "Geist Pixel Square", monospace'),
    "silkscreen": ("Silkscreen", '"Silkscreen", "Geist Pixel Square", monospace'),
    "departure-mono": ("Departure Mono", '"Departure Mono", "Geist Mono", monospace'),
    "system": ("System", "system-ui, sans-serif"),
    "system-mono": ("System mono", "ui-monospace, monospace"),
}

DEFAULTS = {"style": "classic", "font": "geist", "heading": "", "prose": "", "mono": "geist-mono",
            "size": 100, "prose_size": 100, "mono_size": 100}  # "" is the same as the font

STANDARD = "dusk"  # the standard theme until you pick another, and again when the one in use is deleted

# the landing page's colors: cold blue-black, one lavender accent
DYSTOPIAN_COLORS = {
    "--bg-sidebar": "#07070b", "--bg": "#0a0b10", "--surface-soft": "#0e0f15", "--surface": "#11121a",
    "--surface-2": "#171923", "--surface-3": "#24222b", "--code-bg": "#08090d", "--editor-bg": "#0d0e14",
    "--line": "#1c1e28", "--line-strong": "#22242f", "--sidebar-line": "#1c1e28", "--hover": "#11121a",
    "--head-bg": "#11121a", "--preview-bg": "#0d0e14", "--image-bg": "#07070b",
    "--fg": "#ece8e1", "--fg-2": "#cfcac2", "--fg-3": "#a8a2b3", "--fg-4": "#8f8a99", "--fg-5": "#858098",
    "--fg-6": "#6c7385", "--chat-fg": "#ece8e1", "--sidebar-fg": "#cfcac2",
    "--accent": "#3e3858", "--accent-fg": "#a99bd6", "--accent-line": "#4a4268", "--accent-hover": "#4a4268",
    "--accent-link-hover": "#cfc5f0", "--on-accent": "#ddd6f7", "--accent-chip-fg": "#ddd6f7",
    "--tag-accent-bg": "#1d1b2a", "--lavender": "#a99bd6", "--lavender-bg": "#1d1b2a",
    "--control-line": "#3a3d4c", "--grabber": "#2a2d3a", "--grabber-hover": "#3e3858",
    "--ok": "#8fd18b", "--ok-soft": "#b9e3b6", "--ok-bg": "#131919",
    "--warn": "#f2b45a", "--warn-soft": "#131213", "--warn-bg": "#181514", "--warn-line": "#5b462a",
    "--warn-note-fg": "#e0c79a",
    "--err": "#ff5d62", "--err-bg": "#1e1217", "--err-line": "#60282d", "--err-text": "#ff8f93",
    "--danger-btn": "#c2414a", "--run": "#7fb4ca", "--ansi-6": "#7fb4ca", "--hl-type": "#7fb4ca",
    "--paper": "#e6e1d8", "--ink": "#07060b", "--on-fg": "#07060b", "--bevel-hi": "#2a2d3a", "--bevel-lo": "#08090d"}

BUILTIN_THEMES = {
    # Dystopian's colors with plain type, easy to read for a long time
    "dusk": dict(DEFAULTS, name="Dusk", font="system", heading="geist-mono", size=110, prose_size=110,
                 colors=DYSTOPIAN_COLORS),
    "classic": dict(DEFAULTS, name="Classic", colors={}),
    # the landing page's look: pixel type (but a plain sans for long chat replies), square corners, pixel icons
    "dystopian": dict(DEFAULTS, name="Dystopian", style="terminal", font="pixelify-sans", heading="silkscreen",
                      prose="geist", size=130, prose_size=110, colors=DYSTOPIAN_COLORS),
}

# first bytes of each font format we take, and its MIME type
FONT_MAGIC = {b"wOF2": "font/woff2", b"wOFF": "font/woff", b"\x00\x01\x00\x00": "font/ttf",
              b"true": "font/ttf", b"OTTO": "font/otf"}


class ThemeError(ValueError):
    pass


def _read_tokens() -> dict:
    with open(os.path.join(STATIC, "style.css"), encoding="utf-8") as f:
        css = f.read()
    start = css.index(":root {")
    root = css[start:css.index("}", start)]
    out = {}
    for name, value in re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", root):
        value = value.strip().lower()
        if re.fullmatch(r"#[0-9a-f]{3}", value):
            value = "#" + "".join(c * 2 for c in value[1:])
        if HEX.fullmatch(value):
            out[name] = value
    return out


# the color tokens a theme may set, with Classic's value
TOKENS = _read_tokens()


def clean_name(name) -> str:
    name = unicodedata.normalize("NFC", str(name or ""))
    name = "".join(c for c in name if unicodedata.category(c)[0] not in "C").strip()
    name = re.sub(r"\s+", " ", name)
    if not name:
        raise ThemeError("Give it a name.")
    if len(name) > NAME_MAX:
        raise ThemeError(f"A name can be at most {NAME_MAX} characters.")
    return name


def font_key_ok(key) -> bool:
    return isinstance(key, str) and (key in BUILTIN_FONTS or bool(ID_RE.fullmatch(key)))


def check_theme(t, where: str = "theme") -> dict:
    """A clean copy of one theme, or ThemeError."""
    if not isinstance(t, dict):
        raise ThemeError(f"{where} must be a JSON object")
    colors = t.get("colors", {})
    if not isinstance(colors, dict):
        raise ThemeError(f"{where}.colors must be a JSON object")
    clean = {}
    for k, v in colors.items():
        if k not in TOKENS:
            raise ThemeError(f"{where}.colors: {str(k)[:40]} isn't a color the panel has")
        v = str(v).lower()
        if not HEX.fullmatch(v):
            raise ThemeError(f"{where}.colors.{k} must be a color like #1e1e1e")
        if v != TOKENS[k]:
            clean[k] = v
    out = {"name": clean_name(t.get("name")), "colors": clean}
    if t.get("style", "classic") not in STYLES:
        raise ThemeError(f"{where}.style must be one of {', '.join(STYLES)}")
    out["style"] = t.get("style", "classic")
    for k in ("font", "heading", "prose", "mono"):
        v = t.get(k, DEFAULTS[k])
        if not (font_key_ok(v) or (v == "" and k in ("heading", "prose"))):
            raise ThemeError(f"{where}.{k} must be a font the panel has or one you added")
        out[k] = v
    for k in ("size", "prose_size", "mono_size"):
        v = t.get(k, 100)
        if type(v) is not int or v not in SIZES:
            raise ThemeError(f"{where}.{k} must be a text size from {SIZES[0]} to {SIZES[-1]} percent, in steps of 5")
        out[k] = v
    return out


def check_appearance(a) -> None:
    """For config.validate: the "appearance" section."""
    if not isinstance(a, dict):
        raise ValueError("appearance must be a JSON object")
    themes = a.get("themes", {})
    if not isinstance(themes, dict) or len(themes) > MAX_THEMES:
        raise ValueError(f"appearance.themes must be a JSON object with at most {MAX_THEMES} themes")
    for tid, t in themes.items():
        if not ID_RE.fullmatch(str(tid)):
            raise ValueError("appearance.themes: each key must be 8 hex digits")
        check_theme(t, f"appearance.themes.{tid}")
    if a.get("theme") not in BUILTIN_THEMES and a.get("theme") not in themes:
        raise ValueError("appearance.theme must be one of the themes")


def all_themes(a: dict) -> dict:
    """Every theme: the panel's own first, then yours, each with builtin true or false."""
    out = {k: dict(v, builtin=True) for k, v in BUILTIN_THEMES.items()}
    for k, v in (a.get("themes") or {}).items():
        out[k] = dict(v, builtin=False)
    return out


def save_theme(a: dict, tid, theme) -> tuple[dict, str]:
    """A new appearance with the theme saved (tid None: a new one).  Returns it and the id."""
    themes = dict(a.get("themes") or {})
    if tid is None:
        if len(themes) >= MAX_THEMES:
            raise ThemeError(f"At most {MAX_THEMES} themes. Delete one first.")
        tid = secrets.token_hex(4)
    elif tid not in themes:
        raise ThemeError("The panel's own themes can't be changed. Save a copy instead." if tid in BUILTIN_THEMES
                         else "no such theme")
    themes[tid] = check_theme(theme)
    return dict(a, themes=themes), tid


def delete_theme(a: dict, tid: str) -> dict:
    themes = dict(a.get("themes") or {})
    if tid not in themes:
        raise ThemeError("The panel's own themes can't be deleted." if tid in BUILTIN_THEMES else "no such theme")
    del themes[tid]
    out = dict(a, themes=themes)
    if out.get("theme") == tid:
        out["theme"] = STANDARD
    return out


def set_standard(a: dict, tid) -> dict:
    if tid not in BUILTIN_THEMES and tid not in (a.get("themes") or {}):
        raise ThemeError("no such theme")
    return dict(a, theme=tid)


def font_type(data: bytes) -> str | None:
    return FONT_MAGIC.get(data[:4])


class FontStore:
    """Fonts you added, in <data dir>/fonts: <id>.font files and fonts.json."""

    def __init__(self, root: str):
        self.root = root
        self.index = os.path.join(root, "fonts.json")
        self._lock = threading.Lock()
        self._fonts = self._load()

    def _load(self) -> dict:
        try:
            with open(self.index, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return {}
        out = {}
        for f in raw.get("fonts", []) if isinstance(raw, dict) else []:
            try:
                fid, name = str(f["id"]), clean_name(f["name"])
            except (KeyError, TypeError, ThemeError):
                continue
            if ID_RE.fullmatch(fid) and f.get("type") in FONT_MAGIC.values() and os.path.isfile(self._path(fid)):
                out[fid] = {"id": fid, "name": name, "type": f["type"], "size": int(f.get("size") or 0),
                            "added": str(f.get("added", ""))[:40]}
        return out

    def _path(self, fid: str) -> str:
        return os.path.join(self.root, fid + ".font")

    def _save(self) -> None:
        tmp = self.index + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"fonts": list(self._fonts.values())}, f, indent=1)
        os.replace(tmp, self.index)

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(f) for f in self._fonts.values()]

    def add(self, name, data: bytes) -> dict:
        name = clean_name(name)
        ctype = font_type(data)
        if not ctype:
            raise ThemeError("That isn't a font file. Use a WOFF2, WOFF, TTF or OTF file.")
        if len(data) > MAX_FONT:
            raise ThemeError(f"A font can be at most {MAX_FONT >> 20} MB.")
        with self._lock:
            if len(self._fonts) >= MAX_FONTS:
                raise ThemeError(f"At most {MAX_FONTS} fonts. Remove one first.")
            os.makedirs(self.root, mode=0o700, exist_ok=True)
            fid = secrets.token_hex(4)
            tmp = os.path.join(self.root, ".upload-" + fid)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, self._path(fid))
            self._fonts[fid] = {"id": fid, "name": name, "type": ctype, "size": len(data),
                                "added": time.strftime("%Y-%m-%d %H:%M")}
            self._save()
            return dict(self._fonts[fid])

    def remove(self, fid: str) -> None:
        with self._lock:
            if fid not in self._fonts:
                raise ThemeError("no such font")
            del self._fonts[fid]
            try:
                os.unlink(self._path(fid))
            except FileNotFoundError:
                pass
            self._save()

    def read(self, fid: str) -> tuple[str, bytes]:
        """(MIME type, bytes) of a font you added."""
        with self._lock:
            f = self._fonts.get(fid)
        if not f:
            raise ThemeError("no such font")
        fd = os.open(self._path(fid), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as fh:
            data = fh.read(MAX_FONT + 1)
        if len(data) > MAX_FONT or font_type(data) != f["type"]:
            raise ThemeError("That font file changed on disk. Remove it and add it again.")
        return f["type"], data
