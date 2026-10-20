"""Skills you upload: what passes the check, what doesn't, and what's stored."""

import io
import os
import stat
import tarfile
import tempfile
import unittest
import zipfile

from cilly import skills as S

SKILL = b"---\nname: pixel-eye\ndescription: Draws eyes in pixel art.\n---\n\n# Pixel eye\n\nUse 32x32.\n"


def make_zip(entries, top=""):
    """entries: {path: bytes} or {path: (bytes, mode)}; mode 0o120777 makes a link."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for path, v in entries.items():
            body, mode = v if isinstance(v, tuple) else (v, 0o100644)
            info = zipfile.ZipInfo(top + path)
            info.external_attr = mode << 16
            z.writestr(info, body)
    return buf.getvalue()


class CheckTests(unittest.TestCase):
    def test_a_skill_md_on_its_own(self):
        name, desc, files = S.check("SKILL.md", SKILL)
        self.assertEqual((name, desc, list(files)), ("pixel-eye", "Draws eyes in pixel art.", ["SKILL.md"]))

    def test_a_zip_of_the_folder_with_scripts(self):
        data = make_zip({"SKILL.md": SKILL, "scripts/draw.py": (b"print(1)\n", 0o100755), "refs/palette.txt": b"#000\n",
                         ".git/config": b"x", "__MACOSX/._SKILL.md": b"x", "refs/.DS_Store": b"x"}, top="pixel-eye/")
        name, _, files = S.check("pixel-eye.zip", data)
        self.assertEqual(name, "pixel-eye")
        self.assertEqual(sorted(files), ["SKILL.md", "refs/palette.txt", "scripts/draw.py"])  # its folder taken off, hidden files left out
        self.assertTrue(files["scripts/draw.py"][1])
        self.assertFalse(files["refs/palette.txt"][1])

    def test_what_is_refused(self):
        bomb = make_zip({"SKILL.md": SKILL, "big.bin": b"\0" * (S.MAX_TOTAL + 1)})
        cases = {
            "a link": make_zip({"SKILL.md": SKILL, "out": (b"/etc/passwd", 0o120777)}),
            "climbing out": make_zip({"SKILL.md": SKILL, "../evil.sh": b"x"}),
            "an absolute path": make_zip({"SKILL.md": SKILL, "/etc/cron.d/x": b"x"}),
            "a backslash": make_zip({"SKILL.md": SKILL, "a\\..\\b": b"x"}),
            "a control character": make_zip({"SKILL.md": SKILL, "a\nb": b"x"}),
            "too deep": make_zip({"SKILL.md": SKILL, "/".join("d" * (S.MAX_DEPTH + 1)) + "/f": b"x"}),
            "too big unpacked": bomb,
            "too many files": make_zip({"SKILL.md": SKILL, **{f"f{i}": b"x" for i in range(S.MAX_ENTRIES)}}),
            "no SKILL.md": make_zip({"README.md": SKILL}),
            "two skills": make_zip({"a/SKILL.md": SKILL, "b/SKILL.md": SKILL}),
            "a lone dot": make_zip({"SKILL.md": SKILL, "a/./b": b"x"}),
            "no name": make_zip({"SKILL.md": b"---\ndescription: d\n---\n"}),
            "a bad name": make_zip({"SKILL.md": b"---\nname: ../../etc\ndescription: d\n---\n"}),
            "capitals in the name": make_zip({"SKILL.md": b"---\nname: Pixel\ndescription: d\n---\n"}),
            "no description": make_zip({"SKILL.md": b"---\nname: pixel\n---\n"}),
            "frontmatter never closed": make_zip({"SKILL.md": b"---\nname: pixel\ndescription: d\n"}),
            "not text": make_zip({"SKILL.md": b"\xff\xfe\x00"}),
            "not a zip": b"\x7fELF....",
            "a broken zip": b"PK\x03\x04garbage",
            "empty": b"",
            "too big an upload": b"PK\x03\x04" + b"\0" * S.MAX_UPLOAD,
        }
        for what, data in cases.items():
            with self.assertRaises(S.SkillError, msg=what) as cm:
                S.check("x.zip", data)
            self.assertEqual(str(cm.exception)[:1], str(cm.exception)[:1].upper(), what)  # sentence case

    def test_a_skill_with_many_reference_files(self):
        """Hundreds of small files are fine (the size limits hold it): a skill with a big library of references."""
        refs = {f"references/r{i:04}.md": b"# ref\n" for i in range(1500)}
        _, _, files = S.check("mythic.zip", make_zip({"SKILL.md": SKILL, **refs}, top="mythic/"))
        self.assertEqual(len(files), 1501)

    def test_a_skill_deeper_in_a_repository(self):
        """A zip of a repository with skills/<name>/: the skill is the folder with the SKILL.md nearest the top."""
        data = make_zip({"README.md": b"repo", ".claude-plugin/plugin.json": b"{}", "skills/mythicmobs/SKILL.md": SKILL,
                         "skills/mythicmobs/references/docs/guides/-28extra-29-text-editors.md": b"# Text editors\n",
                         "skills/mythicmobs/references/docs/SKILL.md": b"not the skill"}, top="mythic-expert/")
        name, _, files = S.check("mythic-expert.zip", data)
        self.assertEqual(name, "pixel-eye")
        self.assertEqual(sorted(files), ["SKILL.md", "references/docs/SKILL.md", "references/docs/guides/-28extra-29-text-editors.md"])
        with self.assertRaises(S.SkillError) as cm:
            S.check("x.zip", make_zip({"skills/a/SKILL.md": SKILL, "skills/b/SKILL.md": SKILL}))
        self.assertEqual(str(cm.exception), "It holds 2 skills (a, b). Upload each skill's folder on its own.")

    def test_depth_counts_from_the_skills_own_folder(self):
        """A docs mirror nests deep, and a repository puts the skill in skills/<name>/: only the levels inside it count."""
        deep = "skills/mythicmobs/references/docs/skills/mechanics/particle/particle-types/angry/index/page.md"
        _, _, files = S.check("mythic-expert.zip", make_zip({"skills/mythicmobs/SKILL.md": SKILL, deep: b"# Angry\n"}, top="mythic-expert/"))
        self.assertIn("references/docs/skills/mechanics/particle/particle-types/angry/index/page.md", files)
        too = "skills/x/" + "/".join(f"level{i}" for i in range(S.MAX_DEPTH + 1)) + "/f.md"
        with self.assertRaises(S.SkillError) as cm:
            S.check("x.zip", make_zip({"skills/x/SKILL.md": SKILL, too: b"x"}))
        msg = str(cm.exception)
        self.assertIn("nested too deep", msg)
        self.assertIn(f"at most {S.MAX_DEPTH} levels of folders", msg)
        self.assertIn("/f.md'", msg)  # the end of a long path, which says where it is
        with self.assertRaises(S.SkillError) as cm:  # a bad name is still a bad name, shown whole when it's short
            S.check("x.zip", make_zip({"SKILL.md": SKILL, "a/b<c>.md": b"x"}))
        self.assertEqual(str(cm.exception), "It holds a name a skill can't have, 'a/b<c>.md'. Use letters, digits and ._-")

    def test_a_description_over_several_lines(self):
        for md in (b"---\nname: review\ndescription: |\n  Reviews code.\n  Use when: a PR.\nallowed-tools:\n  - Read\n---\n",
                   b"---\nname: review\ndescription: >-\n  Reviews code.\n  Use when: a PR.\n---\n",
                   b"---\nname: review\ndescription: Reviews code.\n  Use when: a PR.\n---\n"):
            self.assertEqual(S.check("SKILL.md", md)[:2], ("review", "Reviews code. Use when: a PR."), md)
        with self.assertRaises(S.SkillError):
            S.check("SKILL.md", b"---\nname: review\ndescription: |\n---\n")  # an empty block is no description

    def test_a_description_is_plain_text(self):
        _, desc, _ = S.check("SKILL.md", "---\nname: x\ndescription: \"Use it \x1b[31mnow\u202e\"\n---\n".encode())
        self.assertEqual(desc, "Use it now")

    def test_the_pack_is_plain_and_the_same_every_time(self):
        _, _, files = S.check("x.zip", make_zip({"SKILL.md": SKILL, "scripts/run.sh": (b"echo\n", 0o100755)}))
        a, b = S.pack(files), S.pack(dict(reversed(list(files.items()))))
        self.assertEqual(a, b)
        with tarfile.open(fileobj=io.BytesIO(a)) as tar:
            members = tar.getmembers()
        self.assertEqual([(m.name, m.isreg(), stat.S_IMODE(m.mode), m.uid, m.mtime) for m in members],
                         [("SKILL.md", True, 0o644, 0, 0), ("scripts/run.sh", True, 0o755, 0, 0)])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "skills")

    def test_add_replace_list_remove(self):
        st = S.SkillStore(self.root)
        got = st.add("SKILL.md", SKILL)
        self.assertEqual((got["name"], got["files"], got["replaced"]), ("pixel-eye", 1, False))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.root, "pixel-eye.tar")).st_mode), 0o600)
        again = st.add("x.zip", make_zip({"SKILL.md": SKILL, "more.md": b"m"}))
        self.assertEqual((again["files"], again["replaced"]), (2, True))
        self.assertNotEqual(again["sha256"], got["sha256"])
        self.assertEqual(list(S.SkillStore(self.root).list()), ["pixel-eye"])  # kept on disk
        files = st.files(["pixel-eye", "missing"])
        self.assertEqual([(f["name"], f["skill"], f["sha256"]) for f in files], [("pixel-eye.tar", "pixel-eye", again["sha256"])])
        st.remove("pixel-eye")
        self.assertEqual((st.list(), os.path.exists(os.path.join(self.root, "pixel-eye.tar"))), ({}, False))
        with self.assertRaises(S.SkillError):
            st.remove("pixel-eye")

    def test_a_hand_edited_index_is_checked_on_load(self):
        st = S.SkillStore(self.root)
        st.add("SKILL.md", SKILL)
        with open(os.path.join(self.root, "skills.json"), "w") as f:
            f.write('{"skills": [{"name": "../x", "sha256": "' + "0" * 64 + '"}, {"name": "gone", "sha256": "' + "0" * 64 + '"},'
                    ' {"name": "pixel-eye", "sha256": "nope"}]}')
        self.assertEqual(S.SkillStore(self.root).list(), {})

    def test_at_most_so_many(self):
        st = S.SkillStore(self.root)
        for i in range(S.MAX_SKILLS):
            st.add("SKILL.md", SKILL.replace(b"pixel-eye", f"s{i}".encode()))
        with self.assertRaises(S.SkillError):
            st.add("SKILL.md", SKILL)
        st.add("SKILL.md", SKILL.replace(b"pixel-eye", b"s0"))  # replacing one still works


if __name__ == "__main__":
    unittest.main()
