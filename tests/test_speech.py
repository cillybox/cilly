import io
import json
import os
import struct
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from cilly import speech as S


def make_wav(seconds=1.0, rate=16000, channels=1, width=2):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds) * channels)
    return buf.getvalue()


class FakeWhisper(BaseHTTPRequestHandler):
    seen = []
    mode = "ok"

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        FakeWhisper.seen.append((self.path, self.headers.get("Content-Type"), body))
        if FakeWhisper.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.end_headers()
            return
        out = json.dumps({"text": "  add a   button\n"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class SpeechTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeWhisper)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        FakeWhisper.seen.clear()
        FakeWhisper.mode = "ok"

    def test_transcribe_sends_multipart_wav_directly(self):
        wav = make_wav()
        with mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9"}):
            text = S.transcribe(self.url, wav, language="en")
        self.assertEqual(text, "add a button")
        path, ctype, body = FakeWhisper.seen[0]
        self.assertEqual(path, "/inference")
        self.assertTrue(ctype.startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="file"; filename="speech.wav"', body)
        self.assertIn(b'name="language"\r\n\r\nen\r\n', body)
        self.assertIn(wav, body)

    def test_redirects_not_followed(self):
        FakeWhisper.mode = "redirect"
        with self.assertRaisesRegex(S.SpeechError, "302"):
            S.transcribe(self.url, make_wav())
        self.assertEqual(len(FakeWhisper.seen), 1)

    def test_unreachable(self):
        with self.assertRaisesRegex(S.SpeechError, "can't reach"):
            S.transcribe("http://127.0.0.1:1", make_wav(), timeout=3)

    def test_wav_info(self):
        self.assertEqual(S.wav_info(make_wav(2.0)), (1, 16000, 16, 2.0))
        self.assertEqual(S.wav_info(make_wav(1.0, rate=44100, channels=2))[:3], (2, 44100, 16))
        for bad in (b"", b"RIFF" + b"\0" * 60, b"x" * 100):
            with self.assertRaises(S.SpeechError):
                S.wav_info(bad)
        float_wav = bytearray(make_wav())
        float_wav[20:22] = struct.pack("<H", 3)
        with self.assertRaisesRegex(S.SpeechError, "PCM"):
            S.wav_info(bytes(float_wav))

    def test_url_check(self):
        self.assertEqual(S.check_url("http://192.168.1.50:8080/"), "http://192.168.1.50:8080")
        for bad in ("ftp://x", "http://user:pw@host", "http://host/?q=1", "192.168.1.5:8080", "http://"):
            with self.assertRaises(ValueError, msg=bad):
                S.check_url(bad)


class UnreachableTests(unittest.TestCase):
    """A whisper server that can't be reached fails in seconds, not after the whole timeout per phrase."""

    def test_refused_and_silent_servers_fail_fast(self):
        import socket
        with socket.socket() as s:  # a port nothing listens on
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with self.assertRaises(S.SpeechError) as cm:
            S.transcribe(f"http://127.0.0.1:{port}", make_wav(), timeout=60)
        self.assertIn(f"can't reach the whisper server at 127.0.0.1:{port}", str(cm.exception))
        seen = []

        def silent(addr, timeout):
            seen.append((addr, timeout))
            raise TimeoutError("timed out")
        with mock.patch.object(S.socket, "create_connection", side_effect=silent), \
                mock.patch.object(S.urllib.request.OpenerDirector, "open", side_effect=AssertionError("no audio is sent")):
            with self.assertRaises(S.SpeechError) as cm:
                S.transcribe("https://gpu-box.local", make_wav(), timeout=60)
        self.assertEqual(seen, [(("gpu-box.local", 443), S.CONNECT_TIMEOUT)])
        self.assertEqual(str(cm.exception), "can't reach the whisper server at gpu-box.local: no answer within 5 s")


if __name__ == "__main__":
    unittest.main()
