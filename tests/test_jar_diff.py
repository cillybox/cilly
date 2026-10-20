"""jar-diff (cilly/guest/jar-diff.py, in the decompilers toolchain), with stand-in decompilers: the real
ones only run in a VM image build, where the recipe's check lines try them on real jars."""

import importlib.util
import io
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
HAVE_YAML = importlib.util.find_spec("yaml") is not None  # the VM image has python3-yaml, CI may not


def cls(name, body=b""):
    """A class file's header, declaring the class name, then any bytes: enough for jar-diff and the stand-ins."""
    n = name.encode()
    return b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + struct.pack(">HBH", 3, 1, len(n)) + n + struct.pack(">BHHHH", 7, 1, 0x21, 2, 0) + body

# argv: vf <jar> <outdir>, or cfr <jar> --outputdir <dir> ... --jarfilter <regex>.  Each class's "source"
# names its bytes; Vineflower "fails" on classes called Broken, and CFR is only asked for those.
FAKE = r'''
import hashlib, os, re, sys, zipfile
kind, jar = sys.argv[1], sys.argv[2]
out = sys.argv[3] if kind == "vf" else sys.argv[sys.argv.index("--outputdir") + 1]
filt = re.compile(sys.argv[sys.argv.index("--jarfilter") + 1]) if "--jarfilter" in sys.argv else None
with zipfile.ZipFile(jar) as z:
    for n in z.namelist():
        if not n.endswith(".class") or ".." in n or n.startswith("/"):
            continue
        head, _, last = n[:-6].rpartition("/")
        if "$" in last.lstrip("$"):
            continue  # an inner class: in its outer class's source
        key = n[:-6]
        if filt and not filt.search(key.replace("/", ".")):
            continue
        body = "// $VF: Couldn't be decompiled\n" if kind == "vf" and key.endswith("Broken") else \
            f"// {kind} {key}\nclass {key.rsplit('/', 1)[-1]} {{ /* {hashlib.sha256(z.read(n)).hexdigest()[:12]} */ }}\n"
        os.makedirs(os.path.dirname(os.path.join(out, key)) or out, exist_ok=True)
        with open(os.path.join(out, key + ".java"), "w") as f:
            f.write(body)
'''



# A decompiler as reckless as Vineflower --folder: it writes EVERY jar entry to the output dir by its
# raw zip name (so an absolute or ../ name escapes), and for each class also writes a this_class escape
# just outside the output dir, to stand in for a class whose internal name holds ../.
DANGEROUS = """
import os, sys, zipfile
out = sys.argv[sys.argv.index("--folder") + 1] if "--folder" in sys.argv else \\
    sys.argv[sys.argv.index("--outputdir") + 1] if "--outputdir" in sys.argv else sys.argv[2]
jar = next(a for a in sys.argv[1:] if a.endswith((".jar", ".class")))
def this_class(data):  # the name in the header cls() writes
    return data[13:13 + int.from_bytes(data[11:13], "big")].decode() if data[:4] == b"\\xca\\xfe\\xba\\xbe" else None
def write(path, data=b"owned"):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        open(path, "wb").write(data)
    except OSError:
        pass
with zipfile.ZipFile(jar) as z:
    for n in z.namelist():
        write(os.path.join(out, n))                       # verbatim: an abs/.. name lands outside out
        if n.endswith(".class"):
            write(os.path.join(out, (this_class(z.read(n)) or n[:-6]) + ".java"), b"// source\\n")
"""


def load():
    spec = importlib.util.spec_from_file_location("jar_diff", os.path.join(ROOT, "cilly", "guest", "jar-diff.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class JarDiffTests(unittest.TestCase):
    def setUp(self):
        self.jd = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.fake = os.path.join(self.d, "fake.py")
        with open(self.fake, "w") as f:
            f.write(FAKE)

    def tearDown(self):
        self.tmp.cleanup()

    def jar(self, name, entries):
        path = os.path.join(self.d, name)
        with zipfile.ZipFile(path, "w") as z:
            for n, data in entries:
                z.writestr(n, data)
        return path

    def compiled(self, tag, source):
        """A real class file from javac when it's installed, else stand-in bytes."""
        if not shutil.which("javac"):
            return cls("me/shop/Greeter", source.encode())
        src = os.path.join(self.d, tag)
        os.makedirs(src)
        with open(os.path.join(src, "Greeter.java"), "w") as f:
            f.write("package me.shop;\n" + source)
        subprocess.run(["javac", "--release", "17", "-d", src, os.path.join(src, "Greeter.java")], check=True)
        with open(os.path.join(src, "me", "shop", "Greeter.class"), "rb") as f:
            return f.read()

    def diff(self, old, new):
        out = os.path.join(self.d, "out")
        lines = self.jd.compare(old, new, out, vineflower=[sys.executable, self.fake, "vf"], cfr=[sys.executable, self.fake, "cfr"])
        return "\n".join(lines), out

    def test_a_plugin_update(self):
        old = self.jar("Shop-1.0.jar", [
            ("app.yml", "name: Shop\nversion: 1.0\n"),
            ("messages.yml", "prefix: '&7[Shop] '\nmessages:\n  no-permission: \"You can't do that.\"\n  bought: \"Bought {item}.\"\n"
                             "  sold: \"Sold {item}.\"\n"),
            ("lang.properties", "greeting=Hi\\\n  there\nfarewell = Bye\n"),
            ("config.json", '{"shop": {"tax": 0.1, "currency": "coins"}}'),
            ("me/shop/Greeter.class", self.compiled("v1", "public class Greeter { String hi() { return \"Hi\"; } }")),
            ("me/shop/Greeter$1.class", cls("me/shop/Greeter$1", b"inner")),
            ("me/shop/Old.class", cls("me/shop/Old")),
            ("me/shop/Same.class", cls("me/shop/Same")),
            ("me/shop/Broken.class", cls("me/shop/Broken", b"v1")),
            ("../../evil.txt", "x"), ("/etc/abs.txt", "x"), ("a/../../b.txt", "x"),
            ("broken.yml", "a: [unclosed\n"),
        ])
        new = self.jar("Shop-2.0.jar", [
            ("app.yml", "name: Shop\nversion: 2.0\n"),
            ("messages.yml", "prefix: '&7[Shop] '\nmessages:\n  errors:\n    no-permission: \"You can't do that.\"\n"
                             "  bought: \"You bought {item}.\"\n  welcome: \"Welcome!\"\n"),
            ("lang.properties", "greeting=Hi there\n"),
            ("config.json", '{"shop": {"tax": 0.2, "currency": "coins"}}'),
            ("me/shop/Greeter.class", self.compiled("v2", "public class Greeter { String hi() { return \"Hello\"; } }")),
            ("me/shop/Greeter$1.class", cls("me/shop/Greeter$1", b"inner")),
            ("me/shop/New.class", cls("me/shop/New")),
            ("me/shop/Same.class", cls("me/shop/Same")),
            ("me/shop/Broken.class", cls("me/shop/Broken", b"v2")),
            ("broken.yml", "a: [still unclosed\n"),
        ])
        text, out = self.diff(old, new)
        for want in ("Added classes (1):\n  me/shop/New", "Removed classes (1):\n  me/shop/Old",
                     "Changed classes (diffs in diff/classes/) (2):\n  me/shop/Broken\n  me/shop/Greeter",
                     'removed  farewell = "Bye"', 'changed  shop.tax: "0.1" -> "0.2"',
                     "broken.yml: not parsed (", "CFR stood in for Vineflower on 1 old class(es):\n  me/shop/Broken",
                     "skipped an entry with an unsafe name: '../../evil.txt'", "'/etc/abs.txt'", "'a/../../b.txt'"):
            self.assertIn(want, text)
        if HAVE_YAML:
            for want in ('removed  messages.sold = "Sold {item}."', 'added    messages.welcome = "Welcome!"',
                         'changed  messages.bought: "Bought {item}." -> "You bought {item}."',
                         "renamed  messages.no-permission  ->  messages.errors.no-permission", "version: \"1.0\" -> \"2.0\""):
                self.assertIn(want, text)
        self.assertNotIn("me/shop/Same", text)
        self.assertIn("lang.properties, key by key:\n  removed  farewell", text)  # the continued line is one value
        self.assertFalse(os.path.exists(os.path.join(self.d, "evil.txt")))
        self.assertFalse(os.path.exists(os.path.join(out, "b.txt")))
        with open(os.path.join(out, "diff", "classes", "me", "shop", "Greeter.java.diff")) as f:
            self.assertTrue(f.read().startswith("--- old/me/shop/Greeter.java"))
        with open(os.path.join(out, "new", "src", "me", "shop", "Broken.java")) as f:
            self.assertIn("// cfr me/shop/Broken", f.read())  # CFR's source replaced the failed one
        self.assertTrue(os.path.isfile(os.path.join(out, "diff", "files", "messages.yml.diff")))
        self.assertTrue(os.path.isfile(os.path.join(out, "old", "files", "app.yml")))

    def test_obfuscated_and_odd_jars_dont_crash_it(self):
        bomb = "a: &a [x, x, x, x, x, x, x, x, x]\n" + "".join(
            f"{chr(98 + i)}: &{chr(98 + i)} [*{chr(97 + i)}, *{chr(97 + i)}, *{chr(97 + i)}, *{chr(97 + i)}]\n" for i in range(9))
        entries = [("a.class", cls("a", b"1")), ("if.class", cls("if")), ("$$.class", cls("$$")), ("a/a/a.class", cls("a/a/a")),
                   ("A.class", cls("A")), ("lang.yml", bomb), ("x.json", "[1, 2"), ("META-INF/versions/17/a.class", cls("a", b"6")),
                   ("module-info.class", cls("module-info"))]
        old = self.jar("o1.jar", entries)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("a.class", cls("a", b"x"))
            z.writestr("a.class", cls("a", b"y"))  # the same name twice
            z.writestr("lang.yml", bomb.replace("x, x", "y, x"))
        new = os.path.join(self.d, "o2.jar")
        with open(new, "wb") as f:
            f.write(buf.getvalue())
        text, _ = self.diff(old, new)
        self.assertIn("a.class is in the jar twice", text)
        self.assertIn("Removed classes", text)
        if HAVE_YAML:
            self.assertIn("lang.yml: not parsed (ValueError)", text)  # the alias bomb stops at the value limit

    def test_command_line(self):
        a = self.jar("a.jar", [("x.yml", "a: 1\n")])
        notjar = os.path.join(self.d, "notes.txt")
        with open(notjar, "w") as f:
            f.write("hi")
        run = lambda *args: subprocess.run([sys.executable, os.path.join(ROOT, "cilly", "guest", "jar-diff.py"), *args],
                                           capture_output=True, text=True, cwd=self.d)
        self.assertEqual(run(a).returncode, 2)
        self.assertIn("isn't a jar", run(a, notjar).stderr)
        os.makedirs(os.path.join(self.d, "full"))
        open(os.path.join(self.d, "full", "x"), "w").close()
        self.assertIn("isn't empty", run(a, a, os.path.join(self.d, "full")).stderr)


if __name__ == "__main__":
    unittest.main()


class JarDiffDetailTests(unittest.TestCase):
    def setUp(self):
        self.jd = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def jar(self, name, entries):
        path = os.path.join(self.d, name)
        with zipfile.ZipFile(path, "w") as z:
            for n, data in entries:
                z.writestr(n, data)
        return path

    def run_cli(self, *args, env=None):
        return subprocess.run([sys.executable, os.path.join(ROOT, "cilly", "guest", "jar-diff.py"), *args],
                              capture_output=True, text=True, cwd=self.d, env=env)

    def test_properties_continuation_counts_backslashes(self):
        p, B = self.jd.parse_properties, chr(92)  # B is one backslash
        self.assertEqual(p("a=one " + B + "\ntwo")["a"], "one two")         # 1 trailing backslash continues
        self.assertEqual(set(p("a=val" + B * 2 + "\nb=next")), {"a", "b"})  # 2 is an escaped backslash: no continuation
        self.assertEqual(set(p("a=one" + B * 3 + "\ntwo")), {"a"})          # 3 continues again

    def test_cfr_batches_stay_small(self):
        keys = [f"pkg/C{i:04d}" for i in range(5000)]
        batches = list(self.jd.cfr_batches(keys, budget=4000))
        self.assertGreater(len(batches), 1)
        self.assertEqual(sorted(k for b in batches for k in b), sorted(keys))
        for b in batches:
            self.assertLessEqual(sum(len(k) * 2 + 2 for k in b), 4000 + max(len(k) * 2 + 2 for k in keys))

    def test_a_nul_file_gets_no_text_diff(self):
        old = self.jar("a.jar", [("x.class", b"c"), ("data.json", b'{"a": 1}')])
        new = self.jar("b.jar", [("x.class", b"c"), ("data.json", b"PK\x00\x03 binary now\x00")])
        out = os.path.join(self.d, "out")
        text = "\n".join(self.jd.compare(old, new, out, vineflower=[sys.executable, "-c", ""], cfr=[sys.executable, "-c", ""]))
        self.assertIn("data.json: binary (has NUL bytes), not diffed", text)
        self.assertNotIn("key by key", text)
        self.assertFalse(os.path.exists(os.path.join(out, "diff", "files", "data.json.diff")))

    def test_untrusted_text_in_the_summary_is_escaped(self):
        """Names, keys, values and notes: no C0 or C1 control (ESC, CSI) or line separator gets through raw."""
        fake = os.path.join(self.d, "fake.py")
        with open(fake, "w") as f:
            f.write(FAKE)
        odd = "x\x9b31m\u2028/Broken"
        old = self.jar("a.jar", [(odd + ".class", cls(odd, b"1")), ("k.properties", "k\x1b]0;title\x07=1\n"),
                                 ("dup\x1b[2J.txt", b"1"), ("dup\x1b[2J.txt", b"2")])
        new = self.jar("b.jar", [(odd + ".class", cls(odd, b"2")), ("k.properties", "k\x1b]0;title\x07=2\n")])
        out = os.path.join(self.d, "out")
        lines = self.jd.compare(old, new, out, vineflower=[sys.executable, fake, "vf"], cfr=[sys.executable, fake, "cfr"])
        text = "\n".join(lines)
        for want in ("CFR stood in", "is in the jar twice", "changed  k\\x1b]0;title\\x07:", "\\x9b31m\\u2028/Broken", "dup\\x1b[2J.txt"):
            self.assertIn(want, text)
        noisy = [sys.executable, "-c", "import sys; sys.stderr.write('\\x1b[31mboom\\x9b2J'); sys.exit(1)"]
        lines += self.jd.compare(old, new, os.path.join(self.d, "out2"), vineflower=noisy, cfr=noisy)
        self.assertIn("exited with 1: \\x1b[31mboom\\x9b2J", "\n".join(lines))  # the decompiler's stderr
        self.assertEqual([ln for ln in lines if RAW_CONTROL.search(ln)], [])

    def test_control_characters_in_names_are_escaped(self):
        spoof = "normal.txt\nAdded classes (99):\n  evil"
        old = self.jar("a.jar", [("x.class", b"c")])
        new = self.jar("b.jar", [("x.class", b"c"), (spoof, b"hi")])
        out = os.path.join(self.d, "out")
        lines = self.jd.compare(old, new, out, vineflower=[sys.executable, "-c", ""], cfr=[sys.executable, "-c", ""])
        self.assertNotIn("  evil", lines)  # the embedded newline can't forge a line
        self.assertTrue(any("\\x0a" in ln for ln in lines))

    def test_default_output_goes_to_scratch(self):
        a = self.jar("a.jar", [("x.yml", "a: 1\n")])
        b = self.jar("b.jar", [("x.yml", "a: 2\n")])
        home = os.path.join(self.d, "home")
        os.makedirs(os.path.join(home, "scratch"))
        env = dict(os.environ, HOME=home)
        r = self.run_cli(a, b, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        made = os.listdir(os.path.join(home, "scratch"))
        self.assertTrue(made and made[0].startswith("jar-diff-"), made)
        self.assertEqual(os.listdir(self.d).count("jar-diff-a-vs-b"), 0)  # not in the working folder

    def test_a_jar_name_starting_with_dash_is_not_an_option(self):
        a = self.jar("-weird.jar", [("x.yml", "a: 1\n")])
        b = self.jar("b.jar", [("x.yml", "a: 2\n")])
        r = self.run_cli("-weird.jar", "b.jar", os.path.join(self.d, "o"), env=dict(os.environ, HOME=self.d))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_bad_timeout_is_a_clear_error(self):
        a = self.jar("a.jar", [("x.yml", "a: 1\n")])
        b = self.jar("b.jar", [("x.yml", "a: 2\n")])
        r = self.run_cli(a, b, os.path.join(self.d, "o"), env=dict(os.environ, HOME=self.d, JAR_DIFF_TIMEOUT="soon"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("JAR_DIFF_TIMEOUT", r.stderr)


class JarDiffSafetyTests(unittest.TestCase):
    """A decompiler must never write outside the output folder, whatever a hostile jar's entries are named."""

    def setUp(self):
        self.jd = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.sentinel = self.tmp.name  # outdir lives deep inside; nothing may be written elsewhere under it
        self.reckless = os.path.join(self.sentinel, "reckless.py")
        with open(self.reckless, "w") as f:
            f.write(DANGEROUS)

    def tearDown(self):
        self.tmp.cleanup()

    def hostile_jar(self, name, climb):
        path = os.path.join(self.sentinel, name)
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("me/app/Main.class", cls("me/app/Main", b" real class"))
            # a class whose internal name climbs out of a decompiler's folder in the output folder
            z.writestr("me/app/Sneaky.class", cls("../" * climb + "escape-this"))
            z.writestr("../escape-relative.txt", "pwned\n")             # climbs out of the output dir
            z.writestr("/" + os.path.join(self.sentinel, "escape-abs.txt"), "pwned\n")  # absolute path
            z.writestr("me/app/../../escape-nested.class", b"\xca\xfe")  # a class name that climbs out
        return path

    def files_under(self, root):
        return [os.path.join(dp, fn) for dp, _, fns in os.walk(root) for fn in fns]

    def assert_only_inside(self, outdir):
        base = os.path.realpath(outdir) + os.sep
        outside = [f for f in self.files_under(self.sentinel)
                   if not os.path.realpath(f).startswith(base)
                   and os.path.realpath(f) not in (os.path.realpath(self.reckless),)
                   and not os.path.realpath(f).endswith(".jar")]
        self.assertEqual(outside, [], f"wrote outside {outdir}: {outside}")

    def test_jar_diff_never_writes_outside_its_folder(self):
        reckless = [sys.executable, self.reckless]
        old, new = self.hostile_jar("old.jar", 5), self.hostile_jar("new.jar", 5)  # from out/old/.decompiling-*/vf
        out = os.path.join(self.sentinel, "work", "diff")
        self.jd.compare(old, new, out, vineflower=reckless, cfr=reckless)
        self.assert_only_inside(os.path.join(self.sentinel, "work"))
        self.assertTrue(os.path.isfile(os.path.join(out, "old", "src", "me", "app", "Main.java")))  # the safe class still came through

    def test_the_vineflower_wrapper_never_writes_outside_its_folder(self):
        jar = self.hostile_jar("app.jar", 4)  # from out/.decompiling-*/out
        out = os.path.join(self.sentinel, "work", "out")
        rc = self.jd.main(["--decompile", "vineflower", jar, out], vineflower=[sys.executable, self.reckless])
        self.assertEqual(rc, 0)
        self.assert_only_inside(os.path.join(self.sentinel, "work"))
        self.assertTrue(os.path.isfile(os.path.join(out, "me", "app", "Main.java")))


class JarDiffClassTests(unittest.TestCase):
    def setUp(self):
        self.jd = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.calls = os.path.join(self.d, "calls")
        # records its arguments and writes one source where it was told to
        self.recorder = os.path.join(self.d, "rec.py")
        with open(self.recorder, "w") as f:
            f.write("import json, os, sys, zipfile\n"
                    "a = sys.argv[1:]\n"
                    "out = a[a.index('--outputdir') + 1] if '--outputdir' in a else a[-1]\n"
                    "jar = next(x for x in a if x.endswith('.jar'))\n"
                    "json.dump({'args': a, 'tmp': out, 'entries': zipfile.ZipFile(jar).namelist()}, open(os.environ['CALLS'], 'w'))\n"
                    "open(os.path.join(out, 'A.java'), 'w').write('class A {}')\n")
        os.environ["CALLS"] = self.calls
        self.addCleanup(os.environ.pop, "CALLS", None)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, data):
        path = os.path.join(self.d, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def called(self):
        import json
        with open(self.calls) as f:
            return json.load(f)

    def test_class_name_reads_this_class(self):
        long_first = (b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + struct.pack(">H", 5) + b"\x05" + bytes(8)  # a long takes two slots
                      + struct.pack(">BH", 1, 6) + b"me/A$1" + struct.pack(">BHHH", 7, 3, 0x21, 4))
        self.assertEqual(self.jd.class_name(self.write("l.class", long_first)), "me/A$1")
        self.assertEqual(self.jd.class_name(self.write("c.class", cls("me/app/Main", b"rest"))), "me/app/Main")
        for bad in (b"junk", b"\xca\xfe\xba\xbe", cls("x")[:20], b"\xca\xfe\xba\xbe" + bytes(4) + b"\x00\x02\x63"):
            self.assertIsNone(self.jd.class_name(self.write("bad.class", bad)), bad)

    def test_the_clean_jar_has_only_classes_named_after_their_path(self):
        files = os.path.join(self.d, "files")
        self.write("files/me/Good.class", cls("me/Good"))
        self.write("files/me/Junk.class", b"not a class")  # no CAFEBABE
        self.write("files/me/Other.class", cls("me/Evil"))  # its name isn't its path
        self.write("files/me/Up.class", cls("../../../up"))
        self.write("files/META-INF/versions/17/me/Good.class", cls("me/Good"))
        self.write("files/notes.txt", b"hi")
        notes = []
        took = self.jd.clean_class_jar(files, os.path.join(self.d, "clean.jar"), notes)
        self.assertEqual(took, {"me/Good.class"})
        with zipfile.ZipFile(os.path.join(self.d, "clean.jar")) as z:
            self.assertEqual(z.namelist(), ["me/Good.class"])
        self.assertEqual(sorted(n.split(":")[0] for n in notes), ["me/Junk.class", "me/Other.class", "me/Up.class"])

    def test_the_wrappers_take_options_before_or_after_the_paths(self):
        jar = self.write("app.jar", b"")
        with zipfile.ZipFile(jar, "w") as z:
            z.writestr("me/A.class", cls("me/A"))
        rec = [sys.executable, self.recorder]
        for i, args in enumerate((["-dgs=1", jar, "o"], [jar, "o", "-dgs=1"])):
            out = os.path.join(self.d, f"vf{i}")
            self.assertEqual(self.jd.main(["--decompile", "vineflower"] + [out if a == "o" else a for a in args], vineflower=rec), 0)
            self.assertEqual(self.called()["args"][0], "-dgs=1")
            self.assertTrue(os.path.isfile(os.path.join(out, "A.java")))
        out = os.path.join(self.d, "cfr")
        self.assertEqual(self.jd.main(["--decompile", "cfr", "--silent", "true", jar, out], cfr=rec), 0)
        self.assertEqual(self.called()["args"][-2:], ["--silent", "true"])

    def test_the_wrappers_keep_their_temp_folder_in_the_output_folder(self):
        jar = self.write("app.jar", b"")
        with zipfile.ZipFile(jar, "w") as z:
            z.writestr("me/A.class", cls("me/A"))
        out = os.path.join(self.d, "out")
        with mock.patch.object(self.jd.tempfile, "tempdir", os.path.join(self.d, "no-such-tmp")):
            self.assertEqual(self.jd.main(["--decompile", "vineflower", jar, out], vineflower=[sys.executable, self.recorder]), 0)
            self.assertTrue(self.called()["tmp"].startswith(out + os.sep))
            b = os.path.join(self.d, "b.jar")
            shutil.copyfile(jar, b)
            lines = self.jd.compare(jar, b, os.path.join(self.d, "diff"), vineflower=[sys.executable, self.recorder], cfr=None)
        self.assertTrue(self.called()["tmp"].startswith(os.path.join(self.d, "diff") + os.sep), lines)
        self.assertEqual(sorted(os.listdir(out)), ["A.java"])  # and nothing is left behind

    def test_a_single_class_goes_where_its_name_says(self):
        path = self.write("Main.class", cls("me/app/Main"))
        out = os.path.join(self.d, "out")
        self.assertEqual(self.jd.main(["--decompile", "vineflower", path, out], vineflower=[sys.executable, self.recorder]), 0)
        self.assertEqual(self.called()["entries"], ["me/app/Main.class"])

    def test_bad_input_to_a_wrapper_is_an_error_not_a_traceback(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            self.assertEqual(self.jd.main(["--decompile", "vineflower", os.path.join(self.d, "missing.jar"), "out"]), 1)
            self.assertEqual(self.jd.main(["--decompile", "cfr", self.write("x.txt", b"hi"), os.path.join(self.d, "o")]), 1)
            self.assertEqual(self.jd.main(["--decompile", "vineflower", "a.jar"]), 2)
            self.assertEqual(self.jd.main(["--decompile", "vineflower", "a.jar", "b", "c"]), 2)
        self.assertIn("missing.jar isn't a file", err.getvalue())
        self.assertIn("x.txt isn't a jar or a class file", err.getvalue())
