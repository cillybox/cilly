"""In-memory secret handling.  Nothing here is ever written to disk.

A secret is identified by a key such as ``token:bot``, ``panel_token`` or
``agent:claude:CLAUDE_CODE_OAUTH_TOKEN``.  It is obtained either from a command you
configured (run without a shell, e.g. ``["pass", "show", "agents/gh-bot"]``)
or from a value pasted into the panel for this session.

Command results are cached in memory for the life of the panel, so a
password manager prompt (pass/GPG pinentry, a locked keyring) appears at most
once per panel start, not before every task.  "Forget" drops the cached value.
A value you paste is newer than anything loaded before, so it's used from then on,
also when the key has a command (you replaced the token, or saved a new one).
"""

from __future__ import annotations

import subprocess
import threading
import time

from . import creds


class SecretError(Exception):
    pass


class SecretStore:
    def __init__(self) -> None:
        self._pasted: dict[str, str] = {}
        self._cached: dict[str, tuple] = {}   # key -> (command, value)
        self._newer: set[str] = set()         # keys whose pasted value replaces what the command gave
        self._set_at: dict[str, float] = {}   # key -> when it was pasted (the time only, for Settings)
        self._read_at: dict[str, float] = {}  # key -> when its command last gave a value
        self._lock = threading.Lock()

    def set_pasted(self, key: str, value: str) -> None:
        value = value.strip()
        if not value or len(value) > 16384 or "\n" in value:
            raise SecretError("secret must be a single non-empty line")
        with self._lock:
            self._pasted[key] = value
            self._cached.pop(key, None)
            self._newer.add(key)
            self._set_at[key] = time.time()

    def forget(self, key: str) -> None:
        with self._lock:
            self._pasted.pop(key, None)
            self._cached.pop(key, None)
            self._newer.discard(key)
            self._set_at.pop(key, None)
            self._read_at.pop(key, None)

    def values(self) -> list:
        """Every secret value held right now, so the panel can hide them in logs and chat."""
        with self._lock:
            return list(self._pasted.values()) + [v for _, v in self._cached.values()]

    def has_pasted(self, key: str) -> bool:
        with self._lock:
            return key in self._pasted

    def get(self, key: str, ref: dict) -> str:
        cmd = ref.get("command")
        with self._lock:
            if key in self._newer:
                return self._pasted[key]
        if cmd:
            try:
                return self._from_command(key, cmd)
            except SecretError:
                with self._lock:
                    v = self._pasted.get(key)
                if v is not None:
                    return v  # you pasted it because the command doesn't work
                raise
        with self._lock:
            v = self._pasted.get(key)
        if v is None:
            raise SecretError(f"{key}: not set (paste it in Settings → Secrets, or configure a command)")
        return v

    def _from_command(self, key: str, cmd: list) -> str:
        with self._lock:
            hit = self._cached.get(key)
        if hit and hit[0] == tuple(cmd):
            return hit[1]
        try:
            p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                               timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise SecretError(f"{key}: secret command failed: {e.__class__.__name__}") from None
        if p.returncode != 0:
            # its own first error line says why (e.g. pass: "... is not in the password store.")
            why = next((ln.strip() for ln in p.stderr.decode(errors="replace").splitlines() if ln.strip()), "")
            raise SecretError(f"{key}: `{' '.join(cmd)[:120]}` exited with {p.returncode}" + (f": {why[:200]}" if why else ""))
        lines = p.stdout.decode(errors="strict").splitlines()
        if not lines or not lines[0].strip():
            raise SecretError(f"{key}: secret command printed nothing")
        value = lines[0].strip()  # `pass` convention: first line is the secret
        with self._lock:
            self._cached[key] = (tuple(cmd), value)
            self._read_at[key] = time.time()
        return value

    def times(self, key: str) -> dict:
        """When it was pasted and when its command last gave it (None: not this panel run).  Never the value."""
        with self._lock:
            return {"pasted": self._set_at.get(key), "read": self._read_at.get(key)}

    def status(self, key: str, ref: dict) -> str:
        if ref.get("command"):
            with self._lock:
                hit = self._cached.get(key)
            # stored on this PC (cilly secrets / "Save on this PC"): cilly's own systemd-creds or keyring command
            saved = creds.is_ours(ref, key)
            with self._lock:
                newer = key in self._newer
            if newer:
                return "saved on this PC (loaded)" if saved else "pasted, used instead of the command"
            if hit and hit[0] == tuple(ref["command"]):
                return "saved on this PC (loaded)" if saved else "command (loaded)"
            if saved:
                return "saved on this PC"
            return "command failed, using pasted" if self.has_pasted(key) else "command (not loaded yet)"
        return "pasted" if self.has_pasted(key) else "missing"
