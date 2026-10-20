"""Model servers you run yourself: the address you give, the list of models, and the test message."""

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cilly import models as M  # noqa: E402


class FakeOllama:
    """Ollama's OpenAI model list, its Anthropic-style messages, and its own /api/version and /api/ps."""

    def __init__(self, models=("qwen3-coder:30b", "gpt-oss:20b"), context=4096, key=None, messages=True, extra=None,
                 show=None, efforts=None, vllm=False):
        self.seen = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, doc):
                raw = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _ok(self):
                outer.seen.append((self.command, self.path, self.headers.get("Authorization")))
                if key and self.headers.get("Authorization") != f"Bearer {key}":
                    self._send(401, {"error": {"message": f"invalid api key {self.headers.get('x-api-key')}"}})
                    return False
                return True

            def do_GET(self):
                if not self._ok():
                    return
                if self.path == "/v1/models":
                    data = [dict({"id": m}, **({"max_model_len": context} if vllm else {})) for m in models] + (extra or [])
                    self._send(200, {"object": "list", "data": data})
                elif vllm:
                    self._send(404, {"detail": "Not Found"})  # Ollama's own API isn't there
                elif self.path == "/api/version":
                    self._send(200, {"version": "0.14.2"})
                elif self.path == "/api/ps":
                    self._send(200, {"models": [{"name": models[0], "model": models[0], "context_length": context}]})
                else:
                    self._send(404, {})

            def do_POST(self):
                if not self._ok():
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.body = body
                if self.path == "/api/show" and show is not None and not vllm:  # the model's Modelfile parameters
                    self._send(200, {"parameters": show.get(body.get("model"), "")})
                    return
                effort = (body.get("output_config") or {}).get("effort")
                if self.path == "/v1/messages" and messages and effort and efforts is not None and effort not in efforts:
                    self._send(500, {"error": {"message": f"Jinja Exception: Unexpected reasoning effort {effort}."}})
                elif self.path == "/v1/messages" and messages:
                    self._send(200, {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "OK"}]})
                else:
                    self._send(404, {"error": "not found"})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        M._ollama.pop(self.url, None)  # its port may be the next fake's


class AddressTests(unittest.TestCase):
    def test_addresses_it_takes(self):
        self.assertEqual(M.check_url("http://192.168.1.50:11434"),
                         {"scheme": "http", "host": "192.168.1.50", "port": 11434, "prefix": ""})
        self.assertEqual(M.check_url("https://gpu.lan/llm/")["prefix"], "/llm")
        self.assertEqual(M.check_url("http://[fd00::5]:8080")["host"], "fd00::5")
        self.assertEqual(M.check_url("http://localhost:1234")["port"], 1234)
        for bad in ("192.168.1.50:11434", "ftp://x", "http://u:p@x:1", "http://x:1/?a=1", "http://x:1/#f",
                    "http://x:99999", "http://0.0.0.0:1", "http://[fe80::1%25eth0]:1", "http://x:1/a b",
                    "http://x:1/a%2e%2e", "http://bad_host:1", None, 5):
            with self.assertRaises(ValueError, msg=bad):
                M.check_url(bad)

    def test_what_settings_take(self):
        M.validate({"gpu": {"url": "http://192.168.1.50:11434", "api_key": False, "secret": {}}}, 8765)
        for bad in ({"GPU": {"url": "http://x:1"}}, {"gpu": {"url": "http://x:1", "extra": 1}},
                    {"gpu": {"url": "http://127.0.0.1:8765"}},  # the panel itself
                    {"gpu": {"url": "http://localhost:8765/x"}},
                    {"gpu": {"url": "http://x:1", "api_key": "yes"}}, {"gpu": "http://x:1"}, []):
            with self.assertRaises(ValueError, msg=bad):
                M.validate(bad, 8765)
        self.assertEqual(M.host_for("gpu"), "gpu.model.cilly.internal")


class ServerTests(unittest.TestCase):
    def serve(self, **kw):
        s = FakeOllama(**kw)
        self.addCleanup(s.close)
        return s

    def test_its_models_and_a_test_message(self):
        s = self.serve(extra=[{"id": "bad name\nwith a line"}, {"id": 5}, {"no": "id"}])
        self.assertEqual(M.list_models(s.url), ["gpt-oss:20b", "qwen3-coder:30b"])  # odd entries left out
        res = M.test(s.url, None, "qwen3-coder:30b")
        self.assertEqual((res["ok"], res["text"]), (True, "OK"))
        self.assertEqual(s.body["model"], "qwen3-coder:30b")
        self.assertIn("4,096 tokens", res["warning"])  # Ollama's small default context
        self.assertIn("OLLAMA_CONTEXT_LENGTH", res["warning"])

    def test_which_efforts_it_takes(self):
        """Claude Code sends high unless told, and some chat templates take only their own words."""
        self.assertIsNone(M.test(self.serve(context=65536).url, None, "qwen3-coder:30b")["effort_warning"])
        res = M.test(self.serve(context=65536, efforts={"low", "medium", "xhigh"}).url, None, "qwen3-coder:30b")
        self.assertTrue(res["ok"])
        self.assertIn("refuses the effort Claude Code sends by default (high), and takes low, medium, xhigh", res["effort_warning"])
        res = M.test(self.serve(context=65536, efforts=set()).url, None, "qwen3-coder:30b")
        self.assertIn("fails whenever Claude Code sends an effort", res["effort_warning"])
        self.assertIn("Unexpected reasoning effort high", res["effort_warning"])

    def test_a_big_enough_context_has_no_warning(self):
        s = self.serve(context=65536)
        self.assertEqual(M.test(s.url, None, "qwen3-coder:30b")["warning"], "")

    def test_with_an_api_key(self):
        s = self.serve(key="sk-123")
        with self.assertRaises(M.ModelServerError) as cm:
            M.list_models(s.url)
        self.assertIn("API key", str(cm.exception))
        self.assertEqual(M.list_models(s.url, "sk-123"), ["gpt-oss:20b", "qwen3-coder:30b"])
        self.assertTrue(M.test(s.url, "sk-123", "gpt-oss:20b")["ok"])

    def test_a_server_without_anthropic_messages(self):
        s = self.serve(messages=False)
        res = M.test(s.url, None, "gpt-oss:20b")
        self.assertFalse(res["ok"])
        self.assertIn("/v1/messages", res["error"])

    def test_a_broken_server_is_an_error_not_a_crash(self):
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        self.addCleanup(srv.close)
        replies = [b"NOT HTTP AT ALL\r\n\r\n", b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\n{", b"HTTP/1.1 200 OK\r\n\r\n" + b"[" * 100000]

        def serve():
            for reply in replies:
                c, _ = srv.accept()
                c.recv(65536)
                c.sendall(reply)
                c.close()
        threading.Thread(target=serve, daemon=True).start()
        url = f"http://127.0.0.1:{srv.getsockname()[1]}"
        for _ in replies:
            with self.assertRaises(M.ModelServerError):
                M.list_models(url)

    def test_a_trickling_server_runs_out_of_time(self):
        import socket
        import time
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        self.addCleanup(srv.close)

        def serve():
            c, _ = srv.accept()
            c.recv(65536)
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
            for _ in range(40):
                try:
                    c.sendall(b" ")
                except OSError:
                    break
                time.sleep(0.1)
            c.close()
        threading.Thread(target=serve, daemon=True).start()
        t0 = time.monotonic()
        with self.assertRaises(M.ModelServerError) as cm:
            M.list_models(f"http://127.0.0.1:{srv.getsockname()[1]}", timeout=1)
        self.assertLess(time.monotonic() - t0, 3)
        self.assertIn("too long", str(cm.exception))

    def test_the_key_is_never_shown(self):
        s = self.serve(key="sk-real-key-123")
        with self.assertRaises(M.ModelServerError) as cm:
            M.list_models(s.url, "sk-wrong-123")
        self.assertNotIn("sk-wrong-123", str(cm.exception))
        self.assertEqual(M.hide("bad key sk-wrong-123", "sk-wrong-123"), "bad key ************")

    def test_how_much_context_a_model_gets(self):
        s = self.serve(context=65536, show={"gpt-oss:20b": "stop                           \"<|end|>\"\nnum_ctx                        32768"})
        self.assertEqual(M.context_of(s.url, None, "qwen3-coder:30b"), 65536)  # loaded: Ollama says
        self.assertEqual(M.context_of(s.url, None, "gpt-oss:20b"), 32768)      # set in its Modelfile
        self.assertIsNone(M.context_of(s.url, None, "devstral:24b"))           # neither: unknown
        self.assertIsNone(M.context_of(s.url, None, "bad name"))
        v = self.serve(extra=[{"id": "Qwen/Qwen3-Coder", "max_model_len": 131072}, {"id": "x", "max_model_len": "9"}])
        self.assertEqual(M.context_of(v.url, None, "Qwen/Qwen3-Coder"), 131072)  # vLLM's model list
        self.assertIsNone(M.context_of(v.url, None, "x"))
        url = v.url
        v.close()
        self.assertIsNone(M.context_of(url, None, "qwen3-coder:30b"))  # unreachable: no number, no error

    def test_vllm_isnt_asked_for_ollamas_api(self):
        """Its model list says how much context a model has. Asking Ollama's endpoints first filled its log with 404s."""
        v = self.serve(context=131072, vllm=True)
        self.assertEqual(M.context_of(v.url, None, "qwen3-coder:30b"), 131072)
        self.assertEqual(M.list_models(v.url), ["gpt-oss:20b", "qwen3-coder:30b"])
        self.assertEqual(M.ollama_contexts(v.url, None), {})
        self.assertEqual(M.context_of(v.url, None, "unknown"), None)
        self.assertEqual([p for _, p, _ in v.seen if not p.startswith("/v1/")], [])  # not one request to /api/
        o = self.serve(context=65536)  # Ollama: asked once whether it is, then its loaded models
        for _ in range(3):
            self.assertEqual(M.context_of(o.url, None, "qwen3-coder:30b"), 65536)
        self.assertEqual([p for _, p, _ in o.seen].count("/api/version"), 1)

    def test_https_to_a_plain_http_server_says_so(self):
        s = self.serve()
        with self.assertRaises(M.ModelServerError) as cm:
            M.list_models(s.url.replace("http://", "https://"))
        self.assertIn("Change the address to start with http:// instead of https://", str(cm.exception))

    def test_nothing_there(self):
        s = self.serve()
        url = s.url
        s.close()
        with self.assertRaises(M.ModelServerError) as cm:
            M.list_models(url)
        port = url.rsplit(":", 1)[1].split("/")[0]
        self.assertEqual(str(cm.exception), f"nothing answers at 127.0.0.1:{port}. Is the server running, and listening on that port?")
        with self.assertRaises(M.ModelServerError):
            M.test(url, None, "bad name")

    def test_why_it_cant_be_reached_is_in_words(self):
        import errno
        import socket
        import ssl
        why = lambda reason: M.unreachable(reason, "192.168.1.50", 11434, 20)  # noqa: E731
        self.assertEqual(why(ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")),
                         "nothing answers at 192.168.1.50:11434. Is the server running, and listening on that port?")
        self.assertEqual(why(TimeoutError("timed out")),
                         "192.168.1.50:11434 didn't answer within 20 s. Is the server on, and is a firewall in the way?")
        self.assertEqual(why(socket.gaierror(-2, "Name or service not known")), "this PC can't find 192.168.1.50. Check the address.")
        self.assertEqual(why(OSError(errno.EHOSTUNREACH, "No route to host")),
                         "this PC has no way to 192.168.1.50. Is that machine on, and on a network this PC reaches?")
        self.assertEqual(why(ConnectionResetError(errno.ECONNRESET, "reset")),
                         "192.168.1.50:11434 closed the connection. Is it the right port for the server?")
        self.assertEqual(why(ssl.SSLCertVerificationError("bad cert")), "this PC doesn't trust the certificate at 192.168.1.50:11434.")
        self.assertEqual(why("something else"), "can't reach 192.168.1.50:11434: something else")  # anything else keeps its own words
        self.assertTrue(M.unreachable(ConnectionRefusedError(), "fd00::5", 8080, 20).startswith("nothing answers at [fd00::5]:8080."))
        for text in (why(ConnectionRefusedError()), why(TimeoutError())):
            self.assertNotIn("Errno", text)


if __name__ == "__main__":
    unittest.main()
