"""Voice input: the browser records you, the panel forwards the audio to a
whisper.cpp server you run (e.g. on another PC on your LAN) and returns the
text.  The only outbound call is to the URL in config `speech.whisper_url`.

whisper.cpp's server (examples/server) takes POST /inference as
multipart/form-data with a `file` field and returns {"text": "..."}.
The browser already sends 16 kHz mono 16-bit WAV, so the server needs no
--convert/ffmpeg.
"""

from __future__ import annotations

import json
import secrets
import struct
import urllib.error
import urllib.request
from urllib.parse import urlparse


class SpeechError(Exception):
    pass


def check_url(url: str) -> str:
    """http(s)://host[:port][/path], no credentials, query or fragment."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError("speech.whisper_url must look like http://192.168.1.50:8080")
    return url.rstrip("/")


def wav_info(data: bytes) -> tuple[int, int, int, float]:
    """(channels, sample rate, bits, seconds) of a PCM WAV, or SpeechError."""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise SpeechError("audio must be a WAV file")
    pos, fmt = 12, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt " and len(body) >= 16:
            audio_fmt, ch, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
            if audio_fmt != 1:
                raise SpeechError("audio must be PCM")
            fmt = (ch, rate, bits)
        elif cid == b"data" and fmt:
            ch, rate, bits = fmt
            return ch, rate, bits, len(body) / max(1, ch * rate * bits // 8)
        pos += 8 + size + (size & 1)
    raise SpeechError("not a usable WAV file")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def transcribe(url: str, wav: bytes, *, language: str = "", timeout: float = 60) -> str:
    boundary = "cilly" + secrets.token_hex(12)
    fields = [("response_format", "json"), ("temperature", "0.0")]
    if language:
        fields.append(("language", language))
    parts = []
    for k, v in fields:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="speech.wav"\r\n'
                 "Content-Type: audio/wav\r\n\r\n".encode() + wav + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(check_url(url) + "/inference", data=b"".join(parts), method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    # direct connection (no proxy env), no redirects
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            body = r.read(1024 * 1024)
    except urllib.error.HTTPError as e:
        raise SpeechError(f"whisper server answered {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise SpeechError(f"can't reach the whisper server: {getattr(e, 'reason', e)}") from None
    try:
        text = json.loads(body).get("text", "")
    except (ValueError, AttributeError):
        raise SpeechError("unexpected answer from the whisper server") from None
    return " ".join(str(text).split())
