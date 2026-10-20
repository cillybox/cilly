"""Secrets you type once, with the secrets command: secrets set claude.

The value is encrypted with systemd-creds (systemd 256 or newer), bound to this
machine (and its TPM if it has one), and stored under ~/.config/cilly/credentials/.
The config then points at a `systemd-creds decrypt` command, which the panel runs
like any other secret command: no prompt, and the plain value only ever lives in
the panel's memory and, per task, in the VM's memory.

With an older systemd (Ubuntu 24.04, Debian 12), it goes into your login keyring with
secret-tool instead, and the config points at a `secret-tool lookup` command.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess

from . import config as C
from . import host as H

ALIASES = {"claude": "agent:claude:CLAUDE_CODE_OAUTH_TOKEN", "research": "research:CLAUDE_CODE_OAUTH_TOKEN"}


def _slots(cfg: dict) -> dict:
    """Every secret the config can hold -> (the dict it lives in, its field)."""
    out = {f"token:{n}": (t, "secret") for n, t in cfg["tokens"].items()}
    out["panel_token"] = (cfg["panel_token"], "secret")
    for an, a in cfg["agents"].items():
        for sn in a.get("secrets", {}):
            out[f"agent:{an}:{sn}"] = (a["secrets"], sn)
    for sn in cfg["research"].get("secrets", {}):
        out[f"research:{sn}"] = (cfg["research"]["secrets"], sn)
    for srv in cfg.get("mcp_servers", {}).values():
        for var in C.mcp_vars(srv):
            out[f"mcp:{var}"] = (cfg.setdefault("mcp_secrets", {}), var)
    for name, r in cfg["egress"].get("routes", {}).items():
        if r.get("user") or r.get("kind") == "wireguard":  # a proxy's password, or a VPN's keys
            out[f"route:{name}"] = (r, "secret")
    for name, m in cfg.get("model_servers", {}).items():
        if m.get("api_key"):  # a model server's API key
            out[f"model:{name}"] = (m, "secret")
        if m.get("ssh"):  # the private key of its SSH tunnel
            out[f"model-ssh:{name}"] = (m, "ssh_secret")
    return out


def resolve(cfg: dict, key: str) -> str:
    key = ALIASES.get(key, key)
    if key == "github" and cfg["tokens"]:
        key = "token:" + next(iter(cfg["tokens"]))
    if key not in _slots(cfg):
        raise SystemExit(f"unknown secret {key!r}; see `{H.cli()} secrets`")
    return key


def describe(ref: dict, key: str) -> str:
    cmd = (ref or {}).get("command")
    if not cmd:
        return "pasted in the panel each start"
    if is_ours(ref, key):  # your own systemd-creds or secret-tool command is a command like any other
        return "stored with systemd-creds (no prompt)" if cmd[0] == "systemd-creds" else "stored in your login keyring"
    return "command: " + " ".join(cmd)[:80]


def _systemd_creds(run) -> bool:
    """systemd-creds with --user (systemd 256 or newer)."""
    try:
        r = run(["systemd-creds", "--version"], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    m = re.match(rb"systemd (\d+)", r.stdout or b"")
    return r.returncode == 0 and bool(m) and int(m.group(1)) >= 256


def backend(run=None, which=shutil.which) -> str | None:
    """Where "Save on this PC" keeps secrets: systemd-creds, else the login keyring, else nowhere."""
    if _systemd_creds(run or subprocess.run):
        return "systemd-creds"
    return "secret-tool" if which("secret-tool") else None


def available(run=None, which=shutil.which) -> bool:
    return backend(run, which) is not None


def command_for(key: str, kind: str = "systemd-creds") -> list:
    if kind == "secret-tool":
        return ["secret-tool", "lookup", "service", "cilly", "key", key]
    name, path = _paths(key)
    return ["systemd-creds", "--user", f"--name={name}", "decrypt", path, "-"]


def is_ours(ref: dict, key: str) -> bool:
    """The exact command this module writes for `key` (the web UI may set nothing else)."""
    cmd = (ref or {}).get("command")
    return cmd is not None and cmd in (command_for(key), command_for(key, "secret-tool"))


def saved_at(key: str) -> float | None:
    """When the encrypted file was written (its mtime), for Settings."""
    try:
        return os.stat(_paths(key)[1]).st_mtime
    except OSError:
        return None


def _paths(key: str) -> tuple:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    folder = C.ensure_private_dir(os.path.join(C.config_dir(), "credentials"))
    # the name is bound into the encrypted file; one saved before the rename keeps its own name in the config's command
    return f"cilly-{safe}", os.path.join(folder, safe + ".cred")


def store(cfg: dict, key: str, value: str, run=None, kind: str = "systemd-creds") -> dict:
    """Encrypt `value`, check it decrypts back, and point the config at it (returns the new ref)."""
    run = run or subprocess.run
    value = value.strip()
    if not value or "\n" in value:
        raise SystemExit("empty or multi-line value; nothing stored")
    if kind == "secret-tool":
        return _store_keyring(cfg, key, value, run)
    name, path = _paths(key)
    tmp = path + ".new"
    r = run(["systemd-creds", "--user", f"--name={name}", "encrypt", "-", tmp], input=value.encode(),
            capture_output=True, check=False)
    if r.returncode != 0:
        _rm(tmp)
        raise SystemExit("systemd-creds couldn't encrypt it (it needs systemd 256 or newer for --user): "
                         + r.stderr.decode(errors="replace").strip()[:300])
    os.chmod(tmp, 0o600)
    decrypt = ["systemd-creds", "--user", f"--name={name}", "decrypt"]
    back = run(decrypt + [tmp, "-"], capture_output=True, check=False)
    if back.returncode != 0 or back.stdout.decode(errors="replace").strip() != value:
        _rm(tmp)
        raise SystemExit("it encrypted, but didn't decrypt back to the same value; nothing changed")
    os.replace(tmp, path)  # only now: a failed attempt never breaks a key that already works
    cmd = decrypt + [path, "-"]
    holder, field = _slots(cfg)[key]
    holder[field] = {"command": cmd}
    return holder[field]


def _store_keyring(cfg: dict, key: str, value: str, run) -> dict:
    """The login keyring (Secret Service), through secret-tool.  It keeps the old value when this fails."""
    try:
        r = run(["secret-tool", "store", f"--label=cilly: {key}", "service", "cilly", "key", key],
                input=value.encode(), capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SystemExit(f"secret-tool couldn't store it: {e.__class__.__name__}") from None
    if r.returncode != 0:
        raise SystemExit("secret-tool couldn't store it (is your login keyring running?): "
                         + r.stderr.decode(errors="replace").strip()[:300])
    cmd = command_for(key, "secret-tool")
    try:
        back = run(cmd, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SystemExit(f"it was stored, but reading it back failed ({e.__class__.__name__}); the config is unchanged") from None
    if back.returncode != 0 or back.stdout.decode(errors="replace").strip() != value:
        raise SystemExit("it was stored, but didn't read back as the same value; the config is unchanged")
    holder, field = _slots(cfg)[key]
    holder[field] = {"command": cmd}
    return holder[field]


def forget(cfg: dict, key: str, run=None) -> None:
    holder, field = _slots(cfg)[key]
    was = (holder.get(field) or {}).get("command")
    holder[field] = {}
    _rm(_paths(key)[1])
    if was == command_for(key, "secret-tool"):
        try:
            (run or subprocess.run)(["secret-tool", "clear", "service", "cilly", "key", key],
                                    capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _rm(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def main(args) -> int:
    cfg = C.load()
    if args.action == "list":
        for key, (holder, field) in _slots(cfg).items():
            print(f"{key:45} {describe(holder.get(field), key)}")
        print(f"\nStore one: {H.cli()} secrets set claude   (aliases: claude, research, github)")
        return 0
    key = resolve(cfg, args.key)
    if args.action == "forget":
        forget(cfg, key)
        C.save(cfg)
        print(f"{key}: forgotten; the panel asks for it again")
        return 0
    kind = backend()
    if not kind:
        raise SystemExit("Saving needs systemd-creds from systemd 256 or newer, or secret-tool for your login keyring.")
    value = getpass.getpass(f"{key} (input is hidden): ")
    store(cfg, key, value, kind=kind)
    C.save(cfg)  # a running panel picks this up on its own
    where = "with systemd-creds" if kind == "systemd-creds" else "in your login keyring"
    print(f"{key}: stored {where}; the panel uses it from now on, without asking")
    return 0
