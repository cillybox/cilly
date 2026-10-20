# Security

cilly runs AI agents that may be tricked by something they read, so the VM, not the agent, has to be the safe part. A hole in that is the most important kind of bug, and reports are very welcome.

## Reporting a problem

Please report it privately. Don't open a public issue or pull request for it.

1. Go to this repository's **Security** tab and click **Report a vulnerability**.
2. Describe what goes wrong and how to repeat it. A short script, a config snippet or a log helps a lot. Hide your own tokens and addresses first.
3. Say which commit you tested and what your host runs (distribution, QEMU version, whether bubblewrap is on).

Your report stays private until a fix is out. You'll be credited in the release notes unless you'd rather not be.

## What counts

Anything that breaks a promise in the [threat model](README.md#threat-model). For example:

- An agent in a VM reaching a host, address or port it wasn't allowed, including your local network or this PC.
- A real token or secret ending up inside a VM, or in a log, chat or finding without being hidden.
- An agent changing an injected folder, or getting out of it through a symlink.
- Something from a VM (chat events, files, findings, logs) running as code or markup in the panel, or driving your terminal.
- A web page, another local user or a paired phone doing more than it may: getting past the token, the Host and Origin checks or the limits on paired devices.
- An agent becoming root in its VM, or anything in normal use that needs root on the host.
- A file cilly hands back (exports, builds, the outbox) landing outside the folder it was meant for.

## What doesn't

- **Bugs in QEMU, KVM, the kernel, bubblewrap or Claude Code themselves.** Please report those to their projects. If cilly sets one of them up in a way that makes a bug easier to reach, that does count.
- **The limits the threat model already names,** such as an agent misusing a host or a token you gave it. Ideas to narrow them further are welcome as normal issues.
- **Someone who is already logged in as you on the PC.** They can read the panel's token and data like any of your files.

## Supported versions

cilly is young and has no release branches yet. Fixes go into `main`, so please test against the latest commit before you report.
