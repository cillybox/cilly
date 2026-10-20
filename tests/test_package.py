"""cilly as a package: a wheel (pipx install) holds everything the panel reads at run time."""

import fnmatch
import os
import stat
import tomllib
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_SH = os.path.join(ROOT, "cilly", "install.sh")


class PackageTests(unittest.TestCase):
    def test_package_data_covers_every_file_the_panel_reads(self):
        """A wheel holds only what pyproject.toml lists: the UI, what goes into VM images, and install.sh."""
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
            globs = tomllib.load(f)["tool"]["setuptools"]["package-data"]["cilly"]
        pkg = os.path.join(ROOT, "cilly")
        for sub in ("static", "guest"):
            for dirpath, dirnames, files in os.walk(os.path.join(pkg, sub)):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for name in files:
                    rel = os.path.relpath(os.path.join(dirpath, name), pkg)
                    self.assertTrue(any(fnmatch.fnmatch(rel, g) and rel.count("/") == g.count("/") for g in globs), rel)
        self.assertIn("install.sh", globs)
        self.assertTrue(os.stat(INSTALL_SH).st_mode & stat.S_IXUSR)
        self.assertEqual(os.path.realpath(os.path.join(ROOT, "install.sh")), INSTALL_SH)  # the copy at the top is a link

    def test_guest_files_live_in_the_package(self):
        from cilly import base_image as BI
        from cilly import vm as V
        self.assertEqual(V.GUEST_DIR, os.path.join(ROOT, "cilly", "guest"))
        self.assertEqual(BI.GUEST_DIR, V.GUEST_DIR)
        for name in ("cilly-runner.py", "cilly-runner.service"):
            self.assertTrue(os.path.isfile(os.path.join(V.GUEST_DIR, name)))
        self.assertFalse(os.path.exists(os.path.join(ROOT, "guest")))


if __name__ == "__main__":
    unittest.main()
