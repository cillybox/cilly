import struct
import unittest
import zlib

from cilly.filecheck import MAX_PIXELS, check


def png(w, h, extra=b"", bad_crc=False):
    def chunk(t, d):
        crc = zlib.crc32(t + d) & 0xFFFFFFFF
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", crc ^ (1 if bad_crc and t == b"IDAT" else 0))
    raw = b"".join(b"\0" + b"\0" * (w * 3) for _ in range(min(h, 4)))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"") + extra)


def jpeg(w, h, extra=b""):
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    sos = b"\xff\xda" + struct.pack(">H", 8) + b"\x01\x01\x00\x00\x3f\x00" + b"\x12\x34\xff\x00\x56"
    return b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + sof + sos + b"\xff\xd9" + extra


class FileCheckTests(unittest.TestCase):
    def test_real_images_preview_with_size(self):
        for name, data, w, h in [("a.png", png(64, 40), 64, 40), ("b.jpg", jpeg(30, 20), 30, 20),
                                 ("c.gif", b"GIF89a" + struct.pack("<HH", 5, 6) + b"\x00\x00\x00" + b"\x3b", 5, 6)]:
            r = check(name, data)
            self.assertEqual((r["preview"], r["width"], r["height"], r["warnings"]), ("image", w, h, []), name)

    def test_hidden_data_after_an_image_is_flagged(self):
        for name, data in [("a.png", png(4, 4, b"PK\x03\x04secret")), ("b.jpg", jpeg(4, 4, b"<script>"))]:
            r = check(name, data)
            self.assertEqual(r["preview"], "image")
            self.assertIn("extra data after the end of the image", r["warnings"][0])

    def test_broken_or_disguised_images_are_not_shown(self):
        r = check("a.png", png(4, 4, bad_crc=True))
        self.assertEqual(r["preview"], "none")
        self.assertIn("isn't a well-formed", r["warnings"][0])
        r = check("photo.png", b"PK\x03\x04 a zip")
        self.assertEqual((r["kind"], r["preview"]), ("zip", "none"))
        self.assertIn("Named .png, but the content is a ZIP archive.", r["warnings"])
        r = check("huge.png", png(100_000, MAX_PIXELS // 100_000 + 1))
        self.assertEqual(r["preview"], "none")

    def test_programs_and_scripts(self):
        self.assertIn("program", check("tool", b"\x7fELF\x02\x01")["warnings"][0])
        r = check("cat.jpg", b"MZ\x90\x00")
        self.assertEqual(r["kind"], "pe")
        self.assertTrue(any("Named .jpg" in w for w in r["warnings"]))
        r = check("run.sh", b"#!/bin/bash\nrm -rf /tmp/x\n")
        self.assertEqual((r["kind"], r["preview"]), ("script", "text"))

    def test_html_and_svg_are_text_only(self):
        for name, data in [("x.svg", b"<svg onload=alert(1)/>"), ("p.html", b"<!DOCTYPE html><script>x</script>"),
                           ("notes.txt", b"hello <script>alert(1)</script>")]:
            r = check(name, data)
            self.assertEqual(r["preview"], "text", name)
            self.assertTrue(r["warnings"], name)

    def test_trojan_source_and_invisible_characters(self):
        r = check("a.py", "ok = 1\nif admin:  # ‮ } ⁦\nx​y = 2\n".encode())
        self.assertEqual(r["preview"], "text")
        self.assertTrue(any("direction" in w and "line 2" in w for w in r["warnings"]))
        self.assertTrue(any("invisible" in w for w in r["warnings"]))
        self.assertEqual(check("bom.txt", "﻿hello".encode())["warnings"], [])

    def test_tag_characters_and_c1_controls_are_hidden_too(self):
        # tag characters spell "ignore" invisibly; U+0085 is a C1 control
        hidden = "".join(chr(0xE0000 + ord(c)) for c in "ignore")
        for text in (f"Summary: fine.{hidden}\n", "line one\u0085line two\n"):
            r = check("notes.md", text.encode())
            self.assertEqual(r["preview"], "text")
            self.assertTrue(any("invisible" in w for w in r["warnings"]), text)

    def test_the_subdivision_flags_arent_hidden_text(self):
        flag = lambda tags, end=True: "\U0001f3f4" + "".join(chr(0xE0000 + ord(c)) for c in tags) + ("\U000e007f" if end else "")  # noqa: E731
        r = check("notes.md", f"Go {flag('gbeng')} {flag('gbsct')} {flag('gbwls')}!\n".encode())
        self.assertEqual(r["warnings"], [])
        for text in (f"{flag('gbeng', end=False)} rest\n",  # no cancel tag: not a flag
                     f"{flag('gbeng')}{chr(0xE0069)}\n",  # a stray tag after a flag
                     flag("IGNORE ALL") + "\n"):  # tag characters a flag never uses
            r = check("notes.md", text.encode())
            self.assertTrue(any("invisible" in w for w in r["warnings"]), repr(text))

    def test_binary_and_control_characters(self):
        self.assertEqual(check("a.txt", b"abc\x00def")["kind"], "binary")
        self.assertEqual(check("a.bin", bytes(range(256)))["preview"], "none")
        self.assertEqual(check("empty.txt", b"")["kind"], "empty")

    def test_text_preview_is_capped(self):
        r = check("big.log", b"x" * (600 * 1024))
        self.assertTrue(r["text_truncated"])
        self.assertEqual(len(r["text"]), 512 * 1024)


if __name__ == "__main__":
    unittest.main()
