# REA in cilly

REA ([morluto/rea](https://github.com/morluto/rea), npm package `rea-agents`, MIT) is an MCP server and CLI for reverse engineering. It drives other tools: Ghidra, Rizin, objdump, JADX, Apktool, TShark, GDB and pwntools. The `rea` toolchain installs it with those tools (see the [guide](guide.md#vm-images-and-toolchains)). This page is what was checked before adding it, for REA 6.5.0.

## Where It Comes From

- The npm package `rea-agents` names `github.com/morluto/rea` as its repository, and REA's README lists that package as one of its three official sources.
- Version 6.5.0 was published by GitHub Actions through npm trusted publishing, with SLSA provenance. The provenance names the workflow `.github/workflows/release.yml` in `morluto/rea` and commit `3b79af2` ("release rea-agents 6.5.0"), and its digest matches the tarball's `sha512` integrity.
- `npm audit signatures` verified the registry signatures of all 173 packages it installs and the 36 provenance attestations among them, including `rea-agents`.

## How It's Installed

- Node.js 24.22.0 from nodejs.org in `/opt/rea/node`, checked against its SHA-256 (the one in Node's `SHASUMS256.txt`, signed by a Node release key). It's not on `PATH`, so the rest of the VM keeps Debian's Node.
- REA and its whole dependency tree with `npm ci` from a committed lockfile (`cilly/guest/rea/package-lock.json`), in `/opt/rea/app`, root-owned. Install scripts are off, so the optional Frida bindings, which would download binaries, aren't installed.
- pwntools 4.15.0 with pyelftools 0.33 and unicorn 2.1.2, the exact versions REA needs, in a venv with every wheel pinned by its PyPI SHA-256 (`cilly/guest/rea/pwntools.txt`).
- rizin 0.9.1 (static build, `rizin`, `rz-bin` and `rz-diff`), the jadx-headless-mcp 0.7.1 jar, both by SHA-256, and Apktool, TShark and GDB from Debian.
- Not installed: Binwalk 3.1 (source only), pwndbg (not in the layout REA expects), Hopper, IDA and JEB (not free), and Frida.
- **What keeps REA from updating itself:** `NO_UPDATE_NOTIFIER=1` stops the background version check, and the install folder `/opt/rea` is root-owned, so REA's own updater fails when it tries to write there, like anything else the agent runs. The `rea` wrapper also refuses `update`, `--update`, `--incur-update-check`, `setup` and `mcp add` (checking the first non-flag argument, so `rea --json update` is caught). That wrapper is a guard against an accidental update, not a security boundary. The read-only, root-owned tree is the boundary.
- **Frida** is listed as an optional dependency but isn't built (install scripts are off), so Frida instrumentation can't load.
- **Website analysis** isn't available: the runner only passes a fixed set of tool-path variables to the agent, not `REA_BROWSER_EXECUTABLE`, so REA has no browser to drive.
- **Default-reachable hosts:** `registry.npmjs.org`, `pypi.org` and `github.com` are on cilly's default egress allowlist, so coding agents can reach them without you allowing anything. REA doesn't need them at runtime for the offline tools, but they're there.

## How It Starts

The MCP server is `rea mcp`, over stdio. It opens no HTTP or WebSocket server. It starts Ghidra and Cutter bridges on `127.0.0.1` or a Unix socket, and JADX as a stdio child process. Everything it uses works without a display, except Hopper, which isn't installed.

## What It Contacts

REA has no telemetry. At runtime:

| Host | Why | When |
|---|---|---|
| `registry.npmjs.org` | A version check from its CLI library (incur) | Only in an interactive terminal, at most daily. Off in cilly (`NO_UPDATE_NOTIFIER=1`) |
| `registry.npmjs.org` | `rea update`, `rea --update` and `rea mcp add` (the last runs an unpinned `npx add-mcp`) | Turned off in cilly |
| `www.hopperapp.com` | Downloading the Hopper demo | Only `rea setup` with Hopper selected, which is turned off in cilly |
| The page you name, and what it loads | Website analysis in a browser, and its source maps | Only when an agent analyzes a website |

GDB, Rizin and Cutter commands can run anything, including network commands, as the agent user.

In a cilly VM, all of these go through the allowlist: a host you haven't allowed is refused and shows up in the Blocked list.
