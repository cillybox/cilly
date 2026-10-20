"""Stored logs are inert (cilly/vm.py, inert_log): colors stay, every other escape or control character
becomes a visible mark, so `cat` on a log can't drive the terminal."""

import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cilly import vm as V  # noqa: E402

ESC, BEL = chr(0x241B), chr(0x2407)  # the marks for ESC and BEL
LIVE = re.compile(rb"[\x00-\x08\x0b-\x1f\x7f]")  # control bytes a terminal acts on (tab and newline aside)


def inert(text: str) -> str:
    return V.inert_log(text.encode()).decode()


class InertLogTests(unittest.TestCase):
    def assert_inert(self, raw: str, want: str):
        out = V.inert_log(raw.encode())
        self.assertEqual(out.decode(), want)
        # nothing a terminal would act on is left: only SGR colors and CRLF
        self.assertIsNone(LIVE.search(re.sub(rb"\x1b\[[0-9;]*m|\r\n", b"", out)), out)
        self.assertNotIn("\x9b", out.decode())

    def test_terminal_commands_come_out_visible(self):
        self.assert_inert("\x1b]52;c;ZXZpbA==\x07", f"{ESC}]52;c;ZXZpbA=={BEL}")  # OSC 52: write the clipboard
        self.assert_inert("\x1b]8;;https://evil.example/\x1b\\docs\x1b]8;;\x1b\\",  # OSC 8: a disguised link
                          f"{ESC}]8;;https://evil.example/{ESC}\\docs{ESC}]8;;{ESC}\\")
        self.assert_inert("\x1b]0;my title\x07\x1b]2;other\x1b\\", f"{ESC}]0;my title{BEL}{ESC}]2;other{ESC}\\")  # titles
        self.assert_inert("\x1bPq#0;2;0;0;0\x1b\\", f"{ESC}Pq#0;2;0;0;0{ESC}\\")  # DCS
        self.assert_inert("a\x1bb", f"a{ESC}b")  # a lone ESC
        self.assert_inert("\x1b[2J\x1b[?1049h\x1b[5A", f"{ESC}[2J{ESC}[?1049h{ESC}[5A")  # CSI that isn't a color
        self.assert_inert("\u009b31m\u009d0;t\u009c", f"{ESC}[31m{ESC}]0;t{ESC}\\")  # C1 controls: ESC and a character
        self.assert_inert("bell\x07 nul\x00 del\x7f cr\rover", "bell" + BEL + " nul" + chr(0x2400) + " del" + chr(0x2421)
                          + " cr" + chr(0x240D) + "over")

    def test_raw_c1_bytes_that_arent_utf8_come_out_visible(self):
        self.assertEqual(V.inert_log(b"x\x9b2Jy\xff"), f"x{ESC}[2Jy".encode() + b"\xff")

    def test_colors_and_normal_text_are_kept(self):
        for text in ("\x1b[1;31mred\x1b[0m and \x1b[m plain\x1b[38;5;208m orange\x1b[39m\n",
                     "tab\there, CRLF\r\n, unicode " + "".join(map(chr, (0xFC, 0x4E2D, 0x1F642, 0x200B))) + "\n", "",
                     "plain line\n"):
            self.assertEqual(inert(text), text)


class LogSinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "log")

    def stored(self, sink, *writes) -> bytes:
        for w in writes:
            sink.write(w)
        sink.close()
        with open(self.path, "rb") as f:
            return f.read()

    def test_a_sink_stores_inert_output(self):
        hide = lambda data, hold: (data.replace(b"sk-secret-123", b"[hidden]"), b"")  # noqa: E731
        for redact in (None, hide):
            if os.path.exists(self.path):
                os.unlink(self.path)
            out = self.stored(V._LogSink(self.path, redact), b"\x1b]52;c;ZXZpbA==\x07 sk-secret-123 \x1b[32mok\x1b[0m\r\n")
            want = f"{ESC}]52;c;ZXZpbA=={BEL} ".encode() + (b"[hidden]" if redact else b"sk-secret-123") + b" \x1b[32mok\x1b[0m\r\n"
            self.assertEqual(out, want)

    def test_non_ascii_text_cut_between_reads_stays_whole(self):
        text = "".join(map(chr, (0x11B, 0x161, 0x10D, 0x20, 0x4E2D, 0x6587, 0x20, 0x2026, 0x20, 0x1F642))) + " done\n"
        raw = text.encode()
        out = self.stored(V._LogSink(self.path), *(raw[i:i + 1] for i in range(len(raw))))  # one byte at a time
        self.assertEqual(out.decode(), text)
        # with secret hiding, a part is only written early on a line over 64 KiB: cut it at every byte
        def hide(secrets):
            return lambda data, hold: tuple(x.encode("utf-8", "surrogateescape") for x in V.hide_secrets(
                data.decode("utf-8", "surrogateescape"), secrets, hold))
        long = b"x" * V.LOG_LINE_MAX
        for secrets in ([], ["sk-secret-12345"]):
            for k in range(1, len(raw)):
                os.unlink(self.path)
                out = self.stored(V._LogSink(self.path, hide(secrets)), long + raw[:k], raw[k:])
                self.assertEqual(out, long + raw, (secrets, k))

    def test_a_real_c1_control_is_still_marked(self):
        out = self.stored(V._LogSink(self.path), b"a\xc2", b"\x9b2J\n")  # U+009B, CSI, cut in two
        self.assertEqual(out, f"a{ESC}[2J\n".encode())

    def test_a_color_code_or_crlf_cut_in_two_by_a_read_stays_whole(self):
        out = self.stored(V._LogSink(self.path), b"a\x1b", b"[3", b"1mred\x1b[0m\r", b"\nnext\x1b[")
        self.assertEqual(out, b"a\x1b[31mred\x1b[0m\r\nnext" + ESC.encode() + b"[")  # an unfinished one at the end is marked


if __name__ == "__main__":
    unittest.main()
