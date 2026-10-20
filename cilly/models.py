"""Model servers you already run (Ollama, llama.cpp's llama-server, vLLM, LM Studio), for agents
that use a local model instead of Claude's.

cilly runs no model server and installs none.  You give the address of yours, and:

- The panel asks it which models it has, and can send it one short test message.  These are
  the only calls the panel makes to it, and only when you open the model list or press Test.
- An agent set to one of its models runs Claude Code as always, pointed at
  https://<name>.model.cilly.internal.  That name exists only in the agent's egress proxy: the
  proxy answers it with this start's certificate and passes each request on to your server's
  address, with the server's API key if it has one.  The VM never holds the key, and an agent
  that isn't set to a local model can't reach the server at all.

All of them speak Anthropic's Messages API now, which is what Claude Code sends.
"""

from __future__ import annotations

import errno
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

HOST_SUFFIX = "model.cilly.internal"  # <name>.model.cilly.internal, answered only by the egress proxy
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}")  # qwen3-coder:30b, hf.co/org/repo:Q4_K_M
MAX_SERVERS = 20
MAX_MODELS = 500
REPLY_MAX = 1024 * 1024
SMALL_CONTEXT = 32768  # below this, Claude Code's prompt alone fills most of it
KIND_TTL = 600  # how long cilly remembers whether a server is Ollama
_ollama: dict[str, tuple[bool, float]] = {}  # address -> (is it Ollama, when that was found)
OWN_PORTS: set[int] = set()  # the SSH tunnels' ports on 127.0.0.1 (sshtunnel.py): only ever yours


class ModelServerError(Exception):
    pass


def host_for(name: str) -> str:
    return f"{name}.{HOST_SUFFIX}"


def check_url(url) -> dict:
    """http(s)://host[:port][/prefix], no login, query or fragment.  Returns its parts."""
    u = urlparse(url) if isinstance(url, str) else None
    if u is None or u.scheme not in ("http", "https") or not u.hostname or u.username or u.password \
            or u.query or u.fragment or any(c in url for c in " \t\r\n\\"):
        raise ValueError("the address must look like http://192.168.1.50:11434")
    try:
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        raise ValueError("the address has a port that isn't a number from 1 to 65535") from None
    host = u.hostname.lower()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is None:
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*", host):
            raise ValueError("the address must have an IP address or a host name")
    elif ip.is_unspecified or ip.is_multicast or (ip.version == 6 and ip.scope_id):
        raise ValueError("the address must be the server's own address")
    prefix = u.path.rstrip("/")
    if prefix and not re.fullmatch(r"(/[A-Za-z0-9._~-]+)+", prefix):
        raise ValueError("the address's path may only have letters, digits and . _ ~ -")
    return {"scheme": u.scheme, "host": host, "port": port, "prefix": prefix}


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate(servers, listen_port: int) -> None:
    if not isinstance(servers, dict) or len(servers) > MAX_SERVERS:
        raise ValueError(f"model_servers must be an object with at most {MAX_SERVERS} servers")
    for name, s in servers.items():
        if not NAME_RE.fullmatch(str(name)):
            raise ValueError(f"model_servers: {str(name)[:40]!r} isn't a usable name (lowercase letters, digits and -)")
        if not isinstance(s, dict) or set(s) - {"url", "api_key", "secret", "ssh", "ssh_secret"}:
            raise ValueError(f"model_servers.{name} takes url, api_key, secret, ssh and ssh_secret")
        try:
            u = check_url(s.get("url"))
            if "ssh" in s:
                from .sshtunnel import check_ssh
                if check_ssh(s["ssh"]) != s["ssh"]:
                    raise ValueError("ssh has a value that isn't written the way cilly writes it")
                if u["scheme"] != "http":
                    raise ValueError("over SSH, the address starts with http://. The tunnel encrypts it")
        except ValueError as e:
            raise ValueError(f"model_servers.{name}: {e}") from None
        if "ssh" not in s and is_loopback(u["host"]) and u["port"] == listen_port:
            raise ValueError(f"model_servers.{name}: that's the panel's own address")
        if not isinstance(s.get("api_key", False), bool):
            raise ValueError(f"model_servers.{name}.api_key must be true or false")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


class _OwnHTTPConnection(http.client.HTTPConnection):
    """To an SSH tunnel's port: nothing is sent unless you hold it.  Another account on this PC
    could take the port while ssh reconnects, and would get the key and the prompts."""

    def connect(self):
        super().connect()
        from .egress_proxy import loopback_owner
        if loopback_owner(self.sock) != os.getuid():
            self.sock.close()
            raise ConnectionError("the SSH tunnel's port on this PC isn't held by cilly's ssh")


class _OwnHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_OwnHTTPConnection, req)


def unreachable(reason, host: str, port: int, timeout: float) -> str:
    """Why a model server can't be reached, in words, rather than the "[Errno 111]" Python gives."""
    at = f"{'[' + host + ']' if ':' in host else host}:{port}"
    code = getattr(reason, "errno", None)
    if isinstance(reason, ConnectionRefusedError) or code == errno.ECONNREFUSED:
        return f"nothing answers at {at}. Is the server running, and listening on that port?"
    if isinstance(reason, (TimeoutError, socket.timeout)) or code == errno.ETIMEDOUT:
        return f"{at} didn't answer within {timeout:g} s. Is the server on, and is a firewall in the way?"
    if isinstance(reason, socket.gaierror):
        return f"this PC can't find {host}. Check the address."
    if code in (errno.EHOSTUNREACH, errno.ENETUNREACH):
        return f"this PC has no way to {host}. Is that machine on, and on a network this PC reaches?"
    if isinstance(reason, ConnectionResetError) or code == errno.ECONNRESET:
        return f"{at} closed the connection. Is it the right port for the server?"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return f"this PC doesn't trust the certificate at {at}."
    return f"can't reach {at}: {reason}"[:300]


def _call(url: str, path: str, key: str | None, body: dict | None = None, timeout: float = 20) -> tuple[int, object]:
    """One request straight to the server (never through a proxy, no redirects), and its JSON."""
    u = check_url(url)
    full = f"{u['scheme']}://{u['host'] if ':' not in u['host'] else '[' + u['host'] + ']'}:{u['port']}{u['prefix']}{path}"
    headers = {"Accept": "application/json", "User-Agent": "cilly"}
    if key:
        headers.update({"Authorization": f"Bearer {key}", "x-api-key": key})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers.update({"Content-Type": "application/json", "anthropic-version": "2023-06-01"})
    req = urllib.request.Request(full, data=data, headers=headers, method="POST" if data else "GET")
    own = [_OwnHTTPHandler] if u["scheme"] == "http" and is_loopback(u["host"]) and u["port"] in OWN_PORTS else []
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect, *own)
    deadline = time.monotonic() + timeout  # for the whole answer: a server that trickles it doesn't hold the panel

    def read(r) -> bytes:
        raw = b""
        while len(raw) <= REPLY_MAX:
            if time.monotonic() > deadline:
                raise ModelServerError("it took too long to answer")
            piece = getattr(r, "read1", r.read)(65536)  # what has come so far, so the deadline gets a say
            if not piece:
                return raw
            raw += piece
        raise ModelServerError("its answer was too large")
    try:
        with opener.open(req, timeout=timeout) as r:
            status, raw = r.status, read(r)
    except urllib.error.HTTPError as e:
        try:
            status, raw = e.code, read(e) if e.fp else b""
        except (OSError, http.client.HTTPException):
            status, raw = e.code, b""
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        if u["scheme"] == "https" and "WRONG_VERSION_NUMBER" in str(getattr(e, "reason", e)):  # it answered without TLS
            raise ModelServerError("it answers in plain HTTP. Change the address to start with http:// instead of https://") from None
        raise ModelServerError(unreachable(getattr(e, "reason", e), u["host"], u["port"], timeout)) from None
    try:
        return status, json.loads(raw) if raw else None
    except (ValueError, RecursionError):
        return status, None


def hide(text: str, key: str | None) -> str:
    """The server's own words, without the key in them, should it repeat what it was sent."""
    return text.replace(key, "*" * len(key)) if key and len(key) >= 4 else text


def _why(status: int, doc) -> str:
    msg = ""
    if isinstance(doc, dict):
        err = doc.get("error")
        msg = err.get("message", "") if isinstance(err, dict) else err if isinstance(err, str) else ""
    msg = re.sub(r"[^\x20-\x7e]", " ", str(msg))[:200].strip()
    if status in (401, 403):
        return "it wants an API key, or a different one" + (f" ({msg})" if msg else "")
    return f"it answered {status}" + (f": {msg}" if msg else "")


def list_models(url: str, key: str | None = None, timeout: float = 20) -> list[str]:
    """The model names the server offers (OpenAI's /v1/models, which all of them have)."""
    status, doc = _call(url, "/v1/models", key, timeout=timeout)
    if status != 200 or not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
        raise ModelServerError(hide(_why(status, doc), key) if status != 200 else "its model list isn't readable")
    if any(isinstance(m, dict) and "max_model_len" in m for m in doc["data"]):
        _ollama[url] = (False, time.monotonic())  # vLLM: no need to ask for Ollama's own API
    out = []
    for m in doc["data"][:MAX_MODELS]:
        mid = m.get("id") if isinstance(m, dict) else None
        if isinstance(mid, str) and MODEL_ID_RE.fullmatch(mid) and mid not in out:
            out.append(mid)
    return sorted(out, key=str.lower)


EFFORTS = ("low", "medium", "high", "xhigh", "max")  # what claude --effort sends, as output_config.effort
DEFAULT_EFFORT = "high"  # what Claude Code sends when it isn't told


def _message(url: str, key: str | None, model: str, effort: str | None = None, timeout: float = 120) -> tuple[int, object]:
    body = {"model": model, "max_tokens": 64, "messages": [{"role": "user", "content": "Reply with the word OK."}]}
    if effort:
        body["output_config"] = {"effort": effort}
    return _call(url, "/v1/messages", key, body, timeout=timeout)


def effort_warning(url: str, key: str | None, model: str) -> str | None:
    """Why agents on this model need an effort picked, when it refuses the one Claude Code sends by
    default.  Some chat templates take only their own words for it."""
    status, doc = _message(url, key, model, DEFAULT_EFFORT)
    if status == 200:
        return None
    takes = [e for e in EFFORTS if e != DEFAULT_EFFORT and _message(url, key, model, e)[0] == 200]
    if not takes:
        return (f"It fails whenever Claude Code sends an effort, which it always does ({hide(_why(status, doc), key)}). "
                "Agents can't use this model as it is. Another build of it may work.")
    return (f"It refuses the effort Claude Code sends by default ({DEFAULT_EFFORT}), and takes {', '.join(takes)}. "
            f"For agents on {model}, pick one of those under Model by the message box.")


def test(url: str, key: str | None, model: str) -> dict:
    """One short message, the way Claude Code sends it.  {"ok", "text" or "error", "warning", "effort_warning"}."""
    if not MODEL_ID_RE.fullmatch(model or ""):
        raise ModelServerError("pick one of its models first")
    status, doc = _message(url, key, model, timeout=300)  # a model that isn't loaded yet can take minutes
    if status == 404:
        return {"ok": False, "error": "it doesn't take Anthropic-style messages (/v1/messages). "
                                      "Ollama 0.14, llama.cpp, vLLM and LM Studio do in their recent versions."}
    if status != 200 or not isinstance(doc, dict) or not isinstance(doc.get("content"), list):
        return {"ok": False, "error": hide(_why(status, doc), key) if status != 200 else "its answer wasn't a message"}
    text = " ".join(b.get("text", "") for b in doc["content"] if isinstance(b, dict) and isinstance(b.get("text"), str))
    text = re.sub(r"[^\x20-\x7e\u00a0-\uffff]", " ", hide(text, key))
    return {"ok": True, "text": re.sub(r"\s+", " ", text)[:200].strip(), "warning": ollama_context_warning(url, key, model),
            "effort_warning": effort_warning(url, key, model)}


def is_ollama(url: str, key: str | None) -> bool:
    """Whether the server is Ollama, asked once in a while, so other servers don't get a request
    they answer with 404 each time."""
    got = _ollama.get(url)
    if got and time.monotonic() - got[1] < KIND_TTL:
        return got[0]
    try:
        status, ver = _call(url, "/api/version", key, timeout=5)
    except ModelServerError:
        return False  # not reachable now: asked again next time
    yes = status == 200 and isinstance(ver, dict) and "version" in ver
    _ollama[url] = (yes, time.monotonic())
    return yes


def ollama_contexts(url: str, key: str | None) -> dict:
    """{model: tokens of context} for the models Ollama has loaded now: it says so only for those.
    Empty for a server that isn't Ollama, or that doesn't say."""
    if not is_ollama(url, key):
        return {}
    try:
        status, ps = _call(url, "/api/ps", key, timeout=5)
    except ModelServerError:
        return {}
    loaded = ps.get("models") if status == 200 and isinstance(ps, dict) else None
    out = {}
    for m in loaded if isinstance(loaded, list) else []:
        n = m.get("context_length") if isinstance(m, dict) else None
        name = m.get("model") or m.get("name") if isinstance(m, dict) else None
        if isinstance(n, int) and n > 0 and isinstance(name, str) and MODEL_ID_RE.fullmatch(name):
            out[name] = n
    return out


def context_of(url: str, key: str | None, model: str) -> int | None:
    """How many tokens of context the server gives `model`, if it says: vLLM in its model list
    (max_model_len), asked first since every server has that list, then Ollama for a loaded model
    (/api/ps) or one with num_ctx in its Modelfile (/api/show).  None when it doesn't say, or
    isn't reachable."""
    if not MODEL_ID_RE.fullmatch(model or ""):
        return None
    try:
        status, doc = _call(url, "/v1/models", key, timeout=5)
        data = doc.get("data") if status == 200 and isinstance(doc, dict) else None
        n = None
        if isinstance(data, list):
            if any(isinstance(e, dict) and "max_model_len" in e for e in data):
                _ollama[url] = (False, time.monotonic())
            n = next((e.get("max_model_len") for e in data if isinstance(e, dict) and e.get("id") == model), None)
        if n is None and is_ollama(url, key):
            n = ollama_contexts(url, key).get(model)
            if n is None:
                status, doc = _call(url, "/api/show", key, {"model": model}, timeout=5)
                params = doc.get("parameters") if status == 200 and isinstance(doc, dict) else None
                m = re.search(r"(?m)^num_ctx\s+(\d{1,8})\s*$", params) if isinstance(params, str) else None
                n = int(m.group(1)) if m else None
    except ModelServerError:
        return None
    return n if type(n) is int and 0 < n <= 10_000_000 else None


def context_warning(model: str, n: int) -> str:
    return (f"Ollama gives {model} only {n:,} tokens of context. Agents need {SMALL_CONTEXT:,}, so set "
            f"OLLAMA_CONTEXT_LENGTH={SMALL_CONTEXT} on that PC and restart Ollama.")


def ollama_context_warning(url: str, key: str | None, model: str) -> str:
    """Ollama gives a model a small context unless told otherwise, and an agent then fails in
    confusing ways.  After the test the model is loaded, and Ollama says how big its context is."""
    n = ollama_contexts(url, key).get(model)
    return context_warning(model, n) if n is not None and n < SMALL_CONTEXT else ""
