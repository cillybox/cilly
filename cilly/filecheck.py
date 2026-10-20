"""What a file from an agent really is, judged by its bytes, not its name.

Used before the panel shows a file.  It decides how (or whether) the browser may
preview it, and lists anything you should know first: a name that doesn't match the
content, a program or script, data hidden after an image, invisible or
direction-changing characters in text (the "Trojan Source" trick).

Previews stay safe whatever this says: files are only ever sent inside JSON, text is
shown as text, and images are decoded and redrawn by the browser from pixels only, so
the original bytes are never displayed or run.  This check is about telling you the truth.
"""
from __future__ import annotations

import os
import re
import struct
import zlib

MAX_PIXELS = 40_000_000      # no preview for bigger images (decompression bombs)
TEXT_PREVIEW = 512 * 1024    # text shown in the panel (the download is the whole file)

# extension -> what the content should be
_EXT = {
    "png": "png", "jpg": "jpeg", "jpeg": "jpeg", "gif": "gif", "webp": "webp",
    "pdf": "pdf", "zip": "zip", "jar": "zip", "apk": "zip", "docx": "zip", "xlsx": "zip", "pptx": "zip",
    "gz": "gzip", "tgz": "gzip",
}
_TEXT_EXT = {"txt", "md", "markdown", "rst", "log", "csv", "tsv", "json", "jsonl", "yaml", "yml", "toml", "ini", "cfg",
             "conf", "xml", "py", "js", "mjs", "ts", "tsx", "jsx", "java", "kt", "kts", "gradle", "rs", "go", "c", "h", "cc",
             "cpp", "hpp", "cs", "rb", "php", "lua", "sql", "css", "scss", "properties", "env", "gitignore", "dockerfile",
             "makefile", "cmake", "patch", "diff", "sh", "bash", "zsh", "fish", "ps1", "bat", "html", "htm", "svg"}
_RUNNABLE_EXT = {"sh", "bash", "zsh", "fish", "ps1", "bat", "cmd", "exe", "msi", "com", "scr", "app", "desktop", "command",
                 "jar", "apk", "appimage", "deb", "rpm", "dmg", "pkg", "run", "bin", "elf", "so", "dll", "dylib", "vbs"}
_NAMES = {"png": "PNG image", "jpeg": "JPEG image", "gif": "GIF image", "webp": "WebP image", "pdf": "PDF document",
          "zip": "ZIP archive", "gzip": "gzip archive", "elf": "Linux program (ELF)", "pe": "Windows program (EXE/DLL)",
          "macho": "macOS program (Mach-O)", "text": "text file", "binary": "binary data", "empty": "empty file",
          "html": "HTML page", "svg": "SVG image (XML)", "script": "script"}
_AN = {"elf", "html", "empty"}  # "an ELF ...", "an HTML page", "an empty file"


def _a(kind: str) -> str:
    return ("an " if kind in _AN else "a ") + _NAMES.get(kind, kind)

# invisible characters and the ones that reorder how text is displayed
_BIDI = re.compile("[‪-‮⁦-⁩‎‏؜]")
# also C1 controls and Unicode tag characters, which can hide text from people that a model still reads
_INVISIBLE = re.compile("[​-‍⁠⁡-⁤­᠎﻿\u0080-\u009f\U000e0000-\U000e007f]")
# the England, Scotland and Wales flags: a black flag, tag letters, and a cancel tag, which aren't hiding anything
_FLAG = re.compile("\U0001f3f4[\U000e0030-\U000e0039\U000e0061-\U000e007a]{2,7}\U000e007f")
_CONTROL = re.compile("[\x00-\x08\x0b\x0e-\x1f\x7f]")


def _ext(name: str) -> str:
    base = os.path.basename(name).lower()
    if base in ("dockerfile", "makefile"):
        return base
    return base.rsplit(".", 1)[1] if "." in base[1:] else ""


def _png(b: bytes):
    """(width, height, bytes after IEND) if every chunk is well-formed with a correct CRC."""
    if not b.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    i, w, h, first = 8, None, None, True
    while i + 12 <= len(b):
        (n,) = struct.unpack(">I", b[i:i + 4])
        typ = b[i + 4:i + 8]
        if n > len(b) or i + 12 + n > len(b) or not typ.isalpha():
            return None
        data = b[i + 8:i + 8 + n]
        (crc,) = struct.unpack(">I", b[i + 8 + n:i + 12 + n])
        if zlib.crc32(typ + data) & 0xFFFFFFFF != crc:
            return None
        if first:
            if typ != b"IHDR" or n != 13:
                return None
            w, h = struct.unpack(">II", data[:8])
            first = False
        i += 12 + n
        if typ == b"IEND":
            return w, h, len(b) - i
    return None


def _jpeg(b: bytes):
    if not b.startswith(b"\xff\xd8\xff"):
        return None
    i, w, h = 2, None, None
    while i + 2 <= len(b):
        if b[i] != 0xFF:
            return None
        m = b[i + 1]
        if m == 0xFF:
            i += 1
            continue
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        if m == 0xD9:
            return (w, h, len(b) - i - 2) if w else None
        if i + 4 > len(b):
            return None
        (n,) = struct.unpack(">H", b[i + 2:i + 4])
        if n < 2 or i + 2 + n > len(b):
            return None
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF) and n >= 7:
            h, w = struct.unpack(">HH", b[i + 5:i + 9])
        if m == 0xDA:  # entropy-coded data until the next real marker
            j = i + 2 + n
            while j + 1 < len(b):
                if b[j] == 0xFF and b[j + 1] != 0 and not (0xD0 <= b[j + 1] <= 0xD7):
                    break
                j += 1
            i = j
            continue
        i += 2 + n
    return None


def _gif(b: bytes):
    if b[:6] not in (b"GIF87a", b"GIF89a") or len(b) < 14:
        return None
    w, h = struct.unpack("<HH", b[6:10])
    end = b.rfind(b"\x3b")
    return (w, h, len(b) - end - 1) if end > 10 else None


def _webp(b: bytes):
    if len(b) < 30 or b[:4] != b"RIFF" or b[8:12] != b"WEBP":
        return None
    (size,) = struct.unpack("<I", b[4:8])
    kind = b[12:16]
    if kind == b"VP8 " and b[23:26] == b"\x9d\x01\x2a":
        w, h = struct.unpack("<HH", b[26:30])
        w, h = w & 0x3FFF, h & 0x3FFF
    elif kind == b"VP8L" and b[20] == 0x2F:
        v = int.from_bytes(b[21:25], "little")
        w, h = (v & 0x3FFF) + 1, ((v >> 14) & 0x3FFF) + 1
    elif kind == b"VP8X":
        w, h = int.from_bytes(b[24:27], "little") + 1, int.from_bytes(b[27:30], "little") + 1
    else:
        return None
    return w, h, max(0, len(b) - (size + 8))


def _sniff(b: bytes) -> str:
    if not b:
        return "empty"
    if b.startswith(b"\x89PNG"):
        return "png"
    if b.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    if b.startswith(b"%PDF-"):
        return "pdf"
    if b[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "zip"
    if b.startswith(b"\x1f\x8b"):
        return "gzip"
    if b.startswith(b"\x7fELF"):
        return "elf"
    if b.startswith(b"MZ"):
        return "pe"
    if b[:4] in (b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
        return "macho"
    return "text?"


def check(name: str, data: bytes) -> dict:
    """Describe a file for the panel: what it is, how to preview it, what to watch out for."""
    ext, kind = _ext(name), _sniff(data)
    warnings: list[str] = []
    out = {"name": os.path.basename(name), "size": len(data), "preview": "none", "width": None, "height": None}
    if kind in ("png", "jpeg", "gif", "webp"):
        info = {"png": _png, "jpeg": _jpeg, "gif": _gif, "webp": _webp}[kind](data)
        if info is None:
            warnings.append(f"It starts like {_a(kind)} but isn't a well-formed one, so it isn't shown.")
        else:
            w, h, extra = info
            out.update(width=w, height=h)
            if extra > 0:
                warnings.append(f"Has {extra} bytes of extra data after the end of the image, where another file could hide. "
                                "The preview shows only the picture.")
            if w and h and w * h <= MAX_PIXELS:
                out["preview"] = "image"
            else:
                warnings.append(f"Too large to preview ({w}×{h} pixels).")
    elif kind == "text?":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            kind = "binary"
        else:
            kind = "text"
            if _CONTROL.search(text):
                kind = "binary"
            else:
                head = text.lstrip()[:400].lower()
                if text.startswith("#!"):
                    kind = "script"
                    warnings.append(f"This is a script ({text.splitlines()[0][:80]}). It's shown as text and never run here. "
                                    "Read it before you run it anywhere.")
                elif ext in ("html", "htm") or head.startswith(("<!doctype html", "<html")) or "<script" in head:
                    kind = "html"
                    warnings.append("This is an HTML page, so it's shown as text. Opened in a browser, it could run scripts.")
                elif ext == "svg" or head.startswith("<svg") or ("<svg" in head and head.startswith("<?xml")):
                    kind = "svg"
                    warnings.append("SVG images can contain scripts, so this one is shown as text instead of drawn.")
                bidi, inv = _BIDI.findall(text), _INVISIBLE.findall(_FLAG.sub("", text.lstrip("﻿")))
                if bidi:
                    lines = sorted({text.count("\n", 0, m.start()) + 1 for m in _BIDI.finditer(text)})[:10]
                    warnings.append(f"Contains {len(bidi)} {'character that changes' if len(bidi) == 1 else 'characters that change'} the "
                                    f"direction text is displayed in (line {', '.join(map(str, lines))}). Such characters can make code look "
                                    "different from what it does. The preview marks them.")
                if inv:
                    warnings.append(f"Contains {len(inv)} invisible {'character' if len(inv) == 1 else 'characters'}, such as "
                                    "zero-width spaces. The preview marks them.")
                out["preview"] = "text"
                out["text"] = text[:TEXT_PREVIEW]
                out["text_truncated"] = len(text) > TEXT_PREVIEW
                out["lines"] = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
    if kind in ("elf", "pe", "macho"):
        warnings.append("This is a program. It's never run here. Only run it if you trust where it came from.")
    expect = _EXT.get(ext)
    if expect and kind != expect:
        warnings.append(f"Named .{ext}, but the content is {_a(kind)}.")
    elif ext in _TEXT_EXT and kind not in ("text", "script", "html", "svg", "empty"):
        warnings.append(f"Named .{ext}, which is usually text, but the content is {_a(kind)}.")
    if ext in _RUNNABLE_EXT and kind not in ("elf", "pe", "macho", "script"):
        warnings.append(f"Files ending in .{ext} can run code when opened. This one is never run here.")
    out.update(kind=kind, type=_NAMES.get(kind, kind), warnings=warnings)
    return out
