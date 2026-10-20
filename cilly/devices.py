"""Paired devices (e.g. your phone): a second way in besides the per-start panel token.

Pairing works like adding a second device in a messenger.  The new device asks for
access and shows a 6-digit code; the panel on your PC shows the same code and you
approve it there.  The device then gets its own long-lived token.  Only a SHA-256 of
each token is stored (devices.json in the config folder, mode 0600), so the file
alone doesn't let anyone in.  Revoking a device works immediately.

Paired devices get a smaller role than the panel token (see server.DEVICE_ROUTES):
they can use agents and answer the inbox, but not touch host folders, Settings,
Claude policy, secrets or other devices.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time

from . import config as C

PAIR_TTL = 300          # a pairing request expires after 5 minutes
MAX_PENDING = 3         # at most this many waiting at once
MAX_STARTS = 10         # and at most this many new requests per 10 minutes
MAX_DEVICES = 20
SEEN_EVERY = 600        # write last_seen to disk at most every 10 minutes per device


class DeviceError(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def clean_name(name) -> str:
    name = re.sub(r"[^\w .,'()+-]+", "", str(name or ""), flags=re.UNICODE).strip()[:40]
    return name or "Device"


class DeviceStore:
    def __init__(self, path: str | None = None, now=time.time):
        self.path = path or os.path.join(C.config_dir(), "devices.json")
        self.now = now
        self.lock = threading.Lock()
        self.devices: dict[str, dict] = {}   # id -> {id, name, hash, created, last_seen}
        self.pending: dict[str, dict] = {}   # secret poll id -> {id, name, code, created, state, token}
        self.starts: list[float] = []
        self._load()

    # -- storage --------------------------------------------------------------
    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            return  # unreadable: no devices (the panel token still works)
        for d in data.get("devices", []) if isinstance(data, dict) else []:
            if isinstance(d, dict) and re.fullmatch(r"[0-9a-f]{8}", str(d.get("id"))) and re.fullmatch(r"[0-9a-f]{64}", str(d.get("hash"))):
                self.devices[d["id"]] = {"id": d["id"], "name": clean_name(d.get("name")), "hash": d["hash"],
                                         "created": float(d.get("created") or 0), "last_seen": float(d.get("last_seen") or 0)}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        tmp = self.path + ".new"
        data = json.dumps({"devices": list(self.devices.values())}, indent=2).encode()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)

    # -- auth -----------------------------------------------------------------
    def check(self, token: str) -> dict | None:
        """The device this token belongs to, or None."""
        if not token or len(token) > 200:
            return None
        h = _hash(token)
        with self.lock:
            for d in self.devices.values():
                if secrets.compare_digest(d["hash"], h):
                    t = self.now()
                    if t - d["last_seen"] > SEEN_EVERY:
                        d["last_seen"] = t
                        try:
                            self._save()
                        except OSError:
                            pass
                    return {"id": d["id"], "name": d["name"]}
        return None

    def list(self) -> dict:
        with self.lock:
            self._expire()
            return {"devices": [{k: d[k] for k in ("id", "name", "created", "last_seen")} for d in self.devices.values()],
                    "pending": [{"id": p["id"], "name": p["name"], "code": p["code"], "created": p["created"]}
                                for p in self.pending.values() if p["state"] == "pending"]}

    def revoke(self, did: str) -> None:
        with self.lock:
            if self.devices.pop(did, None) is None:
                raise DeviceError("no such device", 404)
            self._save()

    # -- pairing --------------------------------------------------------------
    def _expire(self) -> None:
        t = self.now()
        for k in [k for k, p in self.pending.items() if t - p["created"] > PAIR_TTL]:
            del self.pending[k]
        self.starts = [s for s in self.starts if t - s < 600]

    def start(self, name) -> dict:
        """A device asks to pair.  Returns the secret it polls with and the code to compare."""
        with self.lock:
            self._expire()
            if sum(p["state"] == "pending" for p in self.pending.values()) >= MAX_PENDING or len(self.starts) >= MAX_STARTS:
                raise DeviceError("Too many pairing requests right now. Try again in a few minutes.", 429)
            if len(self.devices) >= MAX_DEVICES:
                raise DeviceError(f"You can pair at most {MAX_DEVICES} devices. Remove one on your PC first.", 409)
            self.starts.append(self.now())
            poll = secrets.token_urlsafe(32)
            rec = {"id": secrets.token_hex(4), "name": clean_name(name), "code": f"{secrets.randbelow(10**6):06d}",
                   "created": self.now(), "state": "pending", "token": None}
            self.pending[poll] = rec
            return {"pair": poll, "code": rec["code"], "expires_in": PAIR_TTL}

    def poll(self, poll_id) -> dict:
        """The waiting device asks how it went.  An approved token is handed out exactly once."""
        with self.lock:
            self._expire()
            rec = self.pending.get(str(poll_id or ""))
            if rec is None:
                return {"state": "expired"}
            if rec["state"] == "pending":
                return {"state": "pending"}
            del self.pending[poll_id]
            if rec["state"] == "approved":
                return {"state": "approved", "token": rec["token"], "device": {"id": rec["device_id"], "name": rec["name"]}}
            return {"state": "denied"}

    def decide(self, pid: str, approve: bool) -> dict:
        """You approved or denied a request on the PC (by its public id)."""
        with self.lock:
            self._expire()
            rec = next((p for p in self.pending.values() if p["id"] == pid and p["state"] == "pending"), None)
            if rec is None:
                raise DeviceError("That request has expired or was already answered.", 404)
            if not approve:
                rec["state"] = "denied"
                return {"denied": rec["name"]}
            if len(self.devices) >= MAX_DEVICES:
                raise DeviceError(f"You can pair at most {MAX_DEVICES} devices. Remove one first.", 409)
            token = secrets.token_urlsafe(32)
            did = secrets.token_hex(4)
            while did in self.devices:
                did = secrets.token_hex(4)
            self.devices[did] = {"id": did, "name": rec["name"], "hash": _hash(token), "created": self.now(), "last_seen": 0.0}
            self._save()
            rec.update(state="approved", token=token, device_id=did)
            return {"device": {"id": did, "name": rec["name"]}}
