"""Themes and fonts: what a theme may set, and what the font store takes."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cilly import config as C  # noqa: E402
from cilly import themes as TH  # noqa: E402

WOFF2 = b"wOF2" + b"\0" * 60


class ThemeTests(unittest.TestCase):
    def test_tokens_come_from_the_stylesheet(self):
        self.assertEqual(TH.TOKENS["--bg"], "#1e1e1e")
        self.assertEqual(TH.TOKENS["--surface-soft"], "#222222")  # short colors are spelled out
        self.assertNotIn("--accent-tint", TH.TOKENS)  # mixed from --accent, so a theme sets --accent instead
        self.assertNotIn("--font", TH.TOKENS)

    def test_a_theme_keeps_only_what_it_changes(self):
        t = TH.check_theme({"name": "  Night\n owl ", "colors": {"--bg": "#000000", "--fg": "#ECECEC"}, "font": "geist-pixel-square"})
        self.assertEqual(t, {"name": "Night owl", "colors": {"--bg": "#000000"}, "style": "classic", "font": "geist-pixel-square",
                             "heading": "", "prose": "", "mono": "geist-mono", "size": 100, "prose_size": 100, "mono_size": 100})
        self.assertEqual(TH.check_theme({"name": "Big", "size": 125, "mono_size": 75})["size"], 125)

    def test_a_theme_sets_colors_and_fonts_only(self):
        bad = [{"name": "x", "colors": {"--bg": "red"}},
               {"name": "x", "colors": {"--bg": "#000000; background: url(x)"}},
               {"name": "x", "colors": {"--nope": "#000000"}},
               {"name": "x", "colors": {"--accent-tint": "#000000"}},
               {"name": "x", "font": "Comic Sans"},
               {"name": "x", "mono": "../fonts.json"},
               {"name": ""}, {"name": "x" * 41}, {"name": "x", "colors": []}, "x",
               {"name": "x", "size": 200}, {"name": "x", "size": 102}, {"name": "x", "mono_size": "100"}, {"name": "x", "size": True},
               {"name": "x", "style": "url(x)"}, {"name": "x", "font": ""}, {"name": "x", "heading": "Comic Sans"}]
        for t in bad:
            with self.assertRaises(TH.ThemeError, msg=t):
                TH.check_theme(t)

    def test_the_panels_own_themes_are_valid(self):
        for tid, t in TH.BUILTIN_THEMES.items():
            self.assertEqual(TH.check_theme(t), {k: v for k, v in t.items()}, tid)  # nothing the checks would change
        self.assertEqual(TH.BUILTIN_THEMES["dystopian"]["style"], "terminal")
        dusk = TH.BUILTIN_THEMES["dusk"]  # Dystopian's colors with plain type
        self.assertEqual((dusk["style"], dusk["font"], dusk["heading"], dusk["prose"], dusk["mono"], dusk["size"], dusk["prose_size"], dusk["mono_size"]),
                         ("classic", "system", "geist-mono", "", "geist-mono", 110, 110, 100))
        self.assertEqual(dusk["colors"], TH.BUILTIN_THEMES["dystopian"]["colors"])
        for t in TH.BUILTIN_THEMES.values():  # the fonts they use are bundled
            for k in ("font", "heading", "prose", "mono"):
                self.assertIn(t[k] or t["font"], TH.BUILTIN_FONTS)

    def test_saving_deleting_and_the_standard(self):
        a = {"theme": "classic", "themes": {}}
        a, tid = TH.save_theme(a, None, {"name": "Mine", "colors": {"--accent": "#ff0000"}})
        self.assertRegex(tid, "^[0-9a-f]{8}$")
        a = TH.set_standard(a, tid)
        a, same = TH.save_theme(a, tid, {"name": "Mine 2"})
        self.assertEqual((same, a["themes"][tid]["name"], a["themes"][tid]["colors"]), (tid, "Mine 2", {}))
        all_ = TH.all_themes(a)
        self.assertEqual(list(all_)[:3], ["dusk", "classic", "dystopian"])
        self.assertEqual((all_["classic"]["builtin"], all_[tid]["builtin"]), (True, False))
        with self.assertRaises(TH.ThemeError):
            TH.save_theme(a, "classic", {"name": "Changed"})
        with self.assertRaises(TH.ThemeError):
            TH.delete_theme(a, "classic")
        with self.assertRaises(TH.ThemeError):
            TH.set_standard(a, "deadbeef")
        a = TH.delete_theme(a, tid)
        self.assertEqual(a, {"theme": "dusk", "themes": {}})  # the standard goes back to the default

    def test_the_config_file_is_checked_too(self):
        cfg = C._merge(C.DEFAULT_CONFIG, {})
        C.validate(cfg)
        self.assertEqual((cfg["appearance"]["theme"], TH.STANDARD), ("dusk", "dusk"))  # Dusk unless you pick another
        for a in ({"theme": "gone", "themes": {}},
                  {"theme": "classic", "themes": {"Mine": {"name": "Mine"}}},
                  {"theme": "classic", "themes": {"0123abcd": {"name": "Mine", "colors": {"--bg": "blue"}}}}):
            with self.assertRaises(ValueError, msg=a):
                C.validate(dict(cfg, appearance=a))
        C.validate(dict(cfg, appearance={"theme": "0123abcd", "themes": {"0123abcd": {"name": "Mine"}}}))


class FontStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "fonts")
        self.store = TH.FontStore(self.root)

    def test_a_font_is_kept_and_read_back(self):
        f = self.store.add("Mondwest", WOFF2)
        self.assertEqual((f["name"], f["type"], f["size"]), ("Mondwest", "font/woff2", len(WOFF2)))
        self.assertEqual(self.store.read(f["id"]), ("font/woff2", WOFF2))
        self.assertEqual(os.stat(os.path.join(self.root, f["id"] + ".font")).st_mode & 0o777, 0o600)
        self.assertEqual(TH.FontStore(self.root).list(), self.store.list())  # survives a restart
        self.store.remove(f["id"])
        self.assertEqual(os.listdir(self.root), ["fonts.json"])
        with self.assertRaises(TH.ThemeError):
            self.store.read(f["id"])

    def test_only_font_files_are_taken(self):
        for data in (b"<svg onload=alert(1)>", b"\x89PNG\r\n\x1a\n", b"", b"wOF"):
            with self.assertRaises(TH.ThemeError):
                self.store.add("x", data)
        with self.assertRaises(TH.ThemeError):
            self.store.add("x", b"OTTO" + b"\0" * TH.MAX_FONT)
        with self.assertRaises(TH.ThemeError):
            self.store.add("", WOFF2)
        for magic in (b"wOFF", b"\x00\x01\x00\x00", b"true", b"OTTO"):
            self.store.add("ok", magic + b"\0" * 8)
        self.assertEqual(os.listdir(self.root).count("fonts.json"), 1)

    def test_how_many_fonts(self):
        for i in range(TH.MAX_FONTS):
            self.store.add(f"f{i}", WOFF2)
        with self.assertRaises(TH.ThemeError):
            self.store.add("one more", WOFF2)

    def test_a_hand_edited_list_is_checked(self):
        f = self.store.add("Good", WOFF2)
        with open(os.path.join(self.root, "fonts.json"), "w") as fh:
            json.dump({"fonts": [dict(f), {"id": "../../x", "name": "Bad", "type": "font/woff2"},
                                 {"id": "0123abcd", "name": "Missing", "type": "font/woff2"},
                                 {"id": f["id"], "name": "Wrong type", "type": "text/html"}]}, fh)
        self.assertEqual([x["name"] for x in TH.FontStore(self.root).list()], ["Good"])

    def test_a_font_changed_on_disk_is_not_served(self):
        f = self.store.add("Good", WOFF2)
        with open(os.path.join(self.root, f["id"] + ".font"), "wb") as fh:
            fh.write(b"<html>")
        with self.assertRaises(TH.ThemeError):
            self.store.read(f["id"])

    def test_a_symlink_is_never_followed(self):
        f = self.store.add("Good", WOFF2)
        path = os.path.join(self.root, f["id"] + ".font")
        secret = os.path.join(self.tmp.name, "secret")
        with open(secret, "wb") as fh:
            fh.write(b"wOF2 secret")
        os.remove(path)
        os.symlink(secret, path)
        with self.assertRaises(OSError):
            self.store.read(f["id"])


if __name__ == "__main__":
    unittest.main()
