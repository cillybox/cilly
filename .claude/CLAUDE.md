# Working on cilly

cilly is a local control panel that runs Claude Code agents in QEMU/KVM virtual machines, started as the user's normal user. The [README](README.md) explains what it does and has the file layout. The [guide](docs/guide.md) explains each feature in detail. Read both before a larger change.

## Rules that never bend

These are the user's security rules. Don't change any of them without asking first, and say so plainly when a change comes near one.

- Nothing runs as root or needs sudo in normal use. One-time setup that needs root is listed separately in the README.
- The panel never receives the user's personal GitHub credentials. Agents use a separate bot account.
- Never mount the user's home folder or any broad host path into a VM.
- Injected folders are read-only from the VM's side. Nothing an agent does can change the original.
- Symlinks in injected folders are resolved or rejected, never followed outside the folder.
- No telemetry or outside calls from the panel, except the GitHub API and what the user configures.
- Keep dependencies minimal. The panel is Python's standard library only, and the UI is plain JavaScript with no build step. Ask before adding any dependency, and pin exact versions.
- Prefer simple and auditable over clever. Ask before any decision that changes the security model.

Treat everything that comes from a VM as untrusted: chat events, files, notes, findings, requests and errors. Check sizes, types and names on the host side, and show text as text.

## Security-sensitive code

Changes to these need extra care, a test that shows the protection still holds, and a clear mention in the pull request:

- `cilly/server.py`: the token and Host/Origin checks, what a paired device may do (`DEVICE_ROUTES`, `device_body_ok`), what owners may change from the web UI
- `cilly/egress_proxy.py`, `cilly/relay.py`: the network allowlist
- `cilly/vm.py`: the QEMU command line, bubblewrap, the control channel, secret hiding
- `cilly/inject.py`, `cilly/memory.py`, `cilly/exports.py`, `cilly/handoff.py`, `cilly/filecheck.py`, `cilly/projects.py`: everything that moves files between the host and a VM
- `cilly/guest/cilly-runner.py`: the program inside the VM. It runs as root there, so anything in the agent's home is handled as the agent user.
- `cilly/base_image.py`: what gets installed into the VM images
- `cilly/debian_image.py`: downloading the Debian image that every VM image is built from, and checking it
- `cilly/install.sh`: the one-time setup that runs as root
- `cilly/launch.py`: the panel's link with its token, and the systemd user service

## Writing style

For all text in the UI, the docs, comments meant for people, and commit messages:

- Plain, short sentences, following the GNOME and Apple writing guidelines.
- No em dashes. No colons or semicolons standing in for a dash. A colon that introduces a list or labels a field is fine.
- "·" goes only between metadata items ("main · 3 turns · 05:12"), never inside a sentence.
- Sentence case for everything: buttons, headings, tabs, menu items, switch labels, dialog titles, body text, toasts, tooltips, placeholders and field labels ("Add to notes", "Stop after each task", "Save settings").
- Names keep their spelling: GitHub, PC, VM, MCP, YOLO, Claude, Claude Code, CLAUDE.md, JSON, PR. A label named in a sentence keeps its first capital ("Tap New agent").
- Status lines: "Starting VM (3 of 7)", "Started in 6 s", "Failed: {reason}". Put a space between a number and its unit ("6 s", "2 min").
- Agent and project names stay exactly as the user typed them.

## Code conventions

- Match the code around you: its naming, its idioms and how much it comments.
- UI: build DOM with `el()` and only ever insert text (`textContent`). The page's CSP forbids inline styles, so style with classes. Setting `.style` from JavaScript is allowed.
- UI views have `mount` and `update`. Guard redraws with `changed(key, [...])`, so a redraw never happens under the user's hands while they type.
- Long lists use `paged()`. Popovers use `openPop`, which becomes a bottom sheet on phones.
- Keep older VM images working: a new control message must be ignored by an older runner, and the panel must cope with an older runner that doesn't send something new. When a change needs an image rebuild, say so in the pull request.

## Testing

Run the whole suite before every commit. It must pass:

```sh
python3 -m unittest discover -s tests -t .
```

- Add a test for every behavior change, and for every bug you fix, one that fails without the fix.
- `tests/fake_bin/qemu-system-x86_64` plays the VM side, including a hostile VM. Many tests run whole agent lifecycles against it.
- For UI changes, run `python3 tools/harness.py --seed`, open `http://127.0.0.1:8765/#t=testtoken` with Playwright, and check both a desktop window (1280 wide) and a phone (390 wide). There must be no page errors, and look at the screenshots yourself.

Inside a cilly VM there is no KVM, on purpose, so real VMs can't be tested there. The fake QEMU covers the logic. Say which parts still need a check on real hardware.

## Commits and pull requests

- Short subject in the imperative ("Show pinned agents on phones"). Add a body only when the reason isn't obvious.
- No AI attribution anywhere: no Co-Authored-By lines, no "Generated with", no session links.
- One topic per commit. Keep pull requests focused and explain what changed and why.

## Open ideas

Things the user has talked about but not asked for yet. Ask before starting one:

- A route for a model server (Tor, a VPN or a proxy), to reach a rented GPU without your own address.
- Projects: quick tasks (saved first messages).
- Far off: a central panel with several machines running the agents.

The README's Status section lists what has been confirmed on real hardware and what hasn't.
