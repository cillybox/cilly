"""VM images: written compressed, and read back the same, also under an agent's own disk.  A build
that apt gets stuck in stops early."""

import os
import shutil
import subprocess
import sys
import copy
import io
import json
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cilly.base_image import AptWatch, compression_options, user_data  # noqa: E402
from cilly import config as C  # noqa: E402


@unittest.skipUnless(shutil.which("qemu-img"), "needs qemu-img")
class CompressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name

    def path(self, name):
        return os.path.join(self.d, name)

    def test_an_image_is_written_compressed_and_reads_back_the_same(self):
        raw = self.path("disk.raw")
        with open(raw, "wb") as f:  # text that compresses, some random data, and a long run of zeros
            f.write(b"cilly agent image\n" * 200_000 + os.urandom(1 << 20) + bytes(8 << 20))
        opts = compression_options(self.d)
        self.assertEqual(opts[0], "-c")
        self.assertFalse(os.path.exists(self.path("zstd-probe.qcow2")))  # the probe cleans up after itself
        plain, packed = self.path("plain.qcow2"), self.path("packed.qcow2")
        subprocess.run(["qemu-img", "convert", "-O", "qcow2", raw, plain], check=True)
        subprocess.run(["qemu-img", "convert", "-O", "qcow2", *opts, raw, packed], check=True)
        self.assertLess(os.path.getsize(packed), os.path.getsize(plain) * 0.7)
        subprocess.run(["qemu-img", "compare", raw, packed], check=True, capture_output=True)
        # an agent's disk sits on top of the image: reads go through to it unchanged
        top = self.path("agent.qcow2")
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", packed, top], check=True)
        subprocess.run(["qemu-img", "compare", raw, top], check=True, capture_output=True)
        info = subprocess.run(["qemu-img", "info", packed], check=True, capture_output=True, text=True).stdout
        if "compression_type=zstd" in " ".join(opts):
            self.assertIn("zstd", info)


class StuckAptTests(unittest.TestCase):
    LINE = (b"[ 3561.722013] cloud-init[537]: W: Tried to start delayed item https://deb.debian.org/debian "
            b"trixie/main amd64 lib32gcc-s1 amd64 14.2.0-19, but failed\r\n")

    def test_apt_retries_without_the_delay_that_loops(self):
        """apt's delayed retry can loop forever on one package (Debian bug #1111554)."""
        doc = json.loads(user_data(C.DEFAULT_CONFIG).split("\n", 1)[1])
        conf = doc["apt"]["conf"]
        self.assertIn('Acquire::Retries "3";', conf)
        self.assertIn('Acquire::Retries::Delay "false";', conf)
        self.assertIn('Acquire::http::Timeout "30";', conf)
        self.assertEqual(doc["apt"]["https_proxy"], doc["apt"]["http_proxy"])  # the proxy stays as it was

    def test_the_loop_is_noticed_even_split_across_reads(self):
        w = AptWatch(limit=20)
        data = self.LINE * 19
        self.assertFalse(any(w.feed(data[i:i + 7]) for i in range(0, len(data), 7)))  # 19: not yet
        self.assertTrue(w.feed(self.LINE[:30]) or w.feed(self.LINE[30:]))  # the 20th, in two pieces

    def test_normal_apt_output_never_counts(self):
        w = AptWatch(limit=1)
        for line in (b"Get:353 https://deb.debian.org/debian trixie/main amd64 node-es6-error all 4.1.1-5\n",
                     b"W: Failed to fetch https://deb.debian.org/x  Connection timed out\n", b"Tried to start\n"):
            self.assertFalse(w.feed(line))


FAKE_QEMU = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_bin", "qemu-system-x86_64")


@unittest.skipUnless(shutil.which("qemu-img") and shutil.which("xorriso"), "needs qemu-img and xorriso")
class BuildTests(unittest.TestCase):
    """A whole build against the fake QEMU, playing the build VM."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = self.tmp.name
        env = {"XDG_CONFIG_HOME": f"{t}/cfg", "XDG_DATA_HOME": f"{t}/data", "XDG_RUNTIME_DIR": f"{t}/run"}
        os.makedirs(env["XDG_RUNTIME_DIR"])
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        real = os.access
        p = mock.patch("os.access", lambda path, mode, **kw: True if path == "/dev/kvm" else real(path, mode, **kw))
        p.start()
        self.addCleanup(p.stop)
        self.src = f"{t}/debian.qcow2"
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", self.src, "1M"], check=True)
        self.cfg = copy.deepcopy(C.DEFAULT_CONFIG)
        self.cfg["vm"].update(qemu_binary=FAKE_QEMU, use_bwrap="off")

    def build(self, fake):
        from cilly import base_image as BI
        with mock.patch.dict(os.environ, {"FAKE_BUILD": fake}):
            return BI.build(self.src, self.cfg, timeout_min=2, out=io.StringIO())

    def leftovers(self):
        from cilly import config as C2
        run = C2.runtime_dir()
        if not os.path.isdir(run):
            return []
        routes = os.path.join(run, "routes")  # shared with agents' route files, so it may stay, empty
        return sorted(set(os.listdir(run)) - {"routes"}) + (os.listdir(routes) if os.path.isdir(routes) else [])

    def test_a_blocked_bubblewrap_stops_the_build_before_it_makes_anything(self):
        from cilly import base_image as BI
        from cilly.vm import VMError, images_dir
        self.cfg["vm"]["use_bwrap"] = "auto"
        with mock.patch.object(BI, "bwrap_decision", side_effect=VMError("bubblewrap is installed but can't build its sandbox here")), \
                self.assertRaises(SystemExit) as cm:
            self.build("ok")
        self.assertIn("can't build its sandbox", str(cm.exception))
        self.assertEqual(([d for d in os.listdir(images_dir()) if d.startswith("build-")], self.leftovers()), ([], []))

    def test_a_stuck_apt_stops_the_build_at_once(self):
        from cilly.vm import images_dir
        t0 = time.monotonic()
        with self.assertRaises(SystemExit) as cm:
            self.build("stuck")
        self.assertLess(time.monotonic() - t0, 60)  # not the build's whole time limit
        self.assertIn("apt got stuck", str(cm.exception))
        self.assertEqual(self.leftovers(), [])  # no build folder with a dead proxy socket left behind
        logs = [d for d in os.listdir(images_dir()) if d.startswith("build-")]
        self.assertEqual(len(logs), 1)  # the logs stay, for a look at what happened
        with open(os.path.join(images_dir(), logs[0], "console.log"), "rb") as f:
            self.assertIn(b"Tried to start delayed item", f.read())

    def test_a_build_that_finishes_leaves_only_the_image(self):
        from cilly.vm import images_dir
        started, real = [], subprocess.Popen
        with mock.patch("subprocess.Popen", lambda argv, *a, **kw: started.append(argv) or real(argv, *a, **kw)):
            name = self.build("ok")
        proxy = next(a for a in started if any(x.endswith("egress_proxy.py") for x in a))
        run = os.path.dirname(proxy[proxy.index("--socket") + 1])  # the folder the build's QEMU sandbox sees
        self.assertFalse(proxy[proxy.index("--route-file") + 1].startswith(run + os.sep))
        self.assertTrue(os.path.isfile(os.path.join(images_dir(), name)))
        self.assertEqual(self.leftovers(), [])
        self.assertEqual([d for d in os.listdir(images_dir()) if d.startswith("build-")], [])


if __name__ == "__main__":
    unittest.main()
