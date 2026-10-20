# The cilly guide

How each part of cilly works, in more detail than the [README](../README.md).

## Contents

- [How an agent runs](#how-an-agent-runs)
- [Agents and lifecycle modes](#agents-and-lifecycle-modes)
- [Projects](#projects)
- [Research agents](#research-agents)
- [MCP servers](#mcp-servers)
- [Plugins](#plugins)
- [Your own skills](#your-own-skills)
- [Network approvals and installing tools](#network-approvals-and-installing-tools)
- [Local models](#local-models)
- [Claude Code policy](#claude-code-policy)
- [Secrets](#secrets)
- [The web UI](#the-web-ui)
- [Using cilly from your phone](#using-cilly-from-your-phone)
- [Injecting a host folder](#injecting-a-host-folder)

## How an agent runs

```
browser ──(127.0.0.1, token header)──► cilly (python stdlib, you)
                                          │ per VM:
                                          ├─ egress_proxy.py  (allowlist, own process, no tokens)
                                          │      ▲ unix socket
                                          └─ bwrap --unshare-all ── qemu-system-x86_64 -enable-kvm
                                                   net: user,restrict=on + guestfwd → relay → proxy
                                                   vda: overlay.qcow2 → base-*.qcow2 (read-only)
                                                   vdb: boot.tar (readonly=on): boot.json + inject/ + memory/
                                                   vdc: memout.img (memory mode only, guest→host, validated)
                                                   virtio-serial: control (root-only: policy, tasks+secrets,
                                                                  status), logs
                                                   virtio-balloon: free page reporting only
```

Inside the VM, `cilly-runner` (a root systemd daemon, Python stdlib):

1. Reads `boot.json` and installs the Claude policy root-owned under `/etc/claude-code/`.
2. Mounts `/inject` read-only, restores stored memory (memory mode), runs a network self-test (its checks run at the same time, so it takes as long as the slowest one), reports ready.
3. For each task you send: clones or fetches the repo through the proxy, checks out the agent branch, and runs `claude -p` as the unprivileged user `agent`. It continues the previous conversation if asked, commits leftovers (minus excluded paths), pushes the branch, and opens or keeps a draft PR. The task's secrets exist only in that run's environment.
4. On Stop: exports memory (memory mode), then powers off.

When bubblewrap is installed, QEMU runs inside it or not at all. Before every start, cilly checks that bubblewrap can build its sandbox. If it can't, for any reason, the VM doesn't start, and the error says why and what to do. On Ubuntu 23.10 and newer, AppArmor stops it from creating the user namespaces it needs until it has a profile, which `install.sh` turns on. Elsewhere it's a kernel setting, `kernel.unprivileged_userns_clone` or `user.max_user_namespaces`, and the error gives the `sysctl` command. The host status keeps a failed check for 30 s, but a start always checks again, so a VM starts as soon as it's fixed. QEMU only runs without bubblewrap when it isn't installed (with `vm.use_bwrap` on `auto`, the default), or when you set `vm.use_bwrap` to `off` in the config file yourself. With `required`, a missing bubblewrap stops VMs too. The first line after each start in an agent's log says which it is, for example "QEMU runs inside bubblewrap (/usr/bin/bwrap)". `cilly bootcheck` and image builds make the same choice.

### Files an agent uses to talk to cilly

An agent talks to the panel through a few plain files in its home folder. None of them is tied to Claude Code, so any agent can use them. They are described in the "About this machine" notes every agent gets, and the runner picks them up when a task ends. Whatever arrives is checked on the PC like anything else from a VM, and shown only as text.

| File | What it's for |
|---|---|
| `~/status.txt` | One line on where its work stands. It's shown next to its name in the agent list. |
| `~/ask/<name>.json` | Questions only you can answer. They're shown as a picker above the message box (see below). |
| `~/requests/<topic>.md` | A web question for its research agent (see [Research agents](#research-agents)). |
| `~/outbox/` | Files meant for you: reports, screenshots, archives. A research agent's findings go here. |
| `~/pull-request.md` | The title and description for its pull request. |

**Asking you questions.** When an agent needs decisions only you can make, it writes them to `~/ask/<name>.json`, ends its turn and waits. The name is letters, digits, dots, dashes or underscores, ending in `.json`. The file looks like this:

```json
{"version": 1, "questions": [
  {"header": "Database", "question": "Which database should the plugin use?", "multi": false,
   "options": [{"label": "SQLite", "description": "One file, no server."}, {"label": "MySQL"}]}]}
```

- `version` is 1.
- 1 to 4 questions.
- Each question has:
  - a `header` of up to 12 characters, shown on its tab;
  - the `question`, one short paragraph (at most 600 characters);
  - 2 to 4 `options`, each with a `label` (at most 80 characters) and an optional one-line `description` (at most 200);
  - `multi`, which is true when several options may be picked.
- The recommended option comes first.
- Unknown keys are ignored. Anything else, or a file over 16 KiB, is refused, and the chat says why.
- After a task, at most 4 files are read, and the newest valid set replaces any that are still open.

The questions appear above the message box in an amber card headed with the agent's name ("coder is asking"), and the agent shows as **Waiting for you**. The picker works like this:
- **Moving:** each question is a tab, with ✓ once you've answered it, followed by **Submit**. ← and → move between tabs.
- **Answering:** ↑, ↓ and Enter pick an option, and so does its number. Space ticks the boxes of a question that takes several answers. **Type something** lets you answer in your own words.
- **Submit** shows your answers and sends them as one message that quotes each question. It goes into the same conversation, and if the agent is stopped it starts first.
- **Answer in chat instead** closes the picker so you can write a message of your own. Any message you send answers open questions too.
- **Phones:** the picker is a bottom sheet with large rows, and the questions are also in the inbox.

The chat keeps a short card of what was asked and what you picked.

## Agents and lifecycle modes

You create named agents and can stop and start the same agent whenever you like:

| Mode | On Stop | On Start | Good for |
|---|---|---|---|
| **Ephemeral** | VM disk deleted | fresh VM | one-off or untrusted work (safest) |
| **Persistent** | disk kept | same disk: checkout, installed tools, full Claude Code history | a long-running helper you talk to for weeks |
| **Ephemeral + memory** | Claude Code's conversations and memory notes (`~/.claude/projects`, `CLAUDE.md`) are exported, **validated by the panel**, and stored, then the disk is deleted | fresh VM with that memory restored | long-lived context with a clean machine every time |

- **Send instructions** to a running agent at any time. The **Conversation** chip in the message box picks where your next message goes:
  - **Continue the current one** (the default) runs `claude -p --continue`, so the agent keeps its context across messages, and across restarts in persistent and memory mode.
  - **Start a new one** gives the agent a fresh conversation. Files in the VM stay as they are.
  - **Go back to** lists the earlier conversations, with their first message and when they ended. Picking one sends your next message into it with `claude --resume <session id>`. There is also a **Back to this conversation** link under the last reply of each one. The session id comes from Claude's own output and is checked to be a UUID on both sides. It works while the conversation's files exist: always in persistent and memory mode, and in ephemeral mode until the VM stops.
- "Start a new branch" moves it to a new branch from the default branch.
- **Branch names** (Settings → GitHub access) are one of three:
  - **Kind and a name** (the default): `fix/login-timeout`. The kind is `feat`, `fix`, `docs`, `refactor`, `test` or `chore`, then a few lowercase words joined by hyphens.
  - **Prefix and a name**: `cilly/login-timeout`, with a prefix you pick.
  - **Agent ID**: `cilly/9a70b774-2`, as cilly named branches before.

  With a name, cilly makes one from the task's first words ("Fix the login timeout" becomes `fix/login-timeout`), and the agent may rename the branch with `git branch -m` until it's first pushed. Its notes tell it how, and to keep private details out, since a branch name stays on its pull request's page even after the branch is deleted. When GitHub already has a branch of that name that isn't the agent's, cilly adds `-2` (up to `-9`), so it never works on someone else's branch. Renaming needs a VM image built after this change. On an older image the agent keeps the name cilly made.
- **When it has nothing to do** (in the agent's **···** menu) is one choice for when the VM stops: **Keep running**, **Stop right away**, or after 15 min, 30 min, 1 hour or 2 hours. Picking one sets both settings below, so they never disagree. Research agents have no Stop right away, since they stay up between requests.
  - **Stop right away** (Stop after each task) powers the VM off when a task finishes and nothing else is queued. Your next message starts it again, with whatever its lifecycle kept. A task you stopped yourself leaves the VM on. It's the default for ephemeral agents.
  - **A time** (Stop when idle) stops the VM after it has been Idle for that long. Idle means the VM is on with no task running or queued, nothing in the inbox, and no export or file view in progress. Just before it stops, the panel checks all of this once more. Any message, task or inbox item starts the clock again, and so does **Restart the clock** on the agent's page. The sidebar shows "Idle, stops in N min". It's a normal Stop: memory agents save their memory first and persistent agents keep their disk. Persistent agents start with the default from **Settings → VM limits** (30 min). Turning it on for an ephemeral or memory agent warns you that files in the VM you haven't exported are lost when it stops. VMs stop with the panel, so after a panel restart the clock starts again when an agent starts.
  - **Keep running** leaves the VM and its conversation up until you press Stop. It's the default for ephemeral + memory and research agents.
  - The API keeps `auto-stop` and `idle-stop`, and `when-idle` sets both at once (`keep`, `task` or minutes). Turning one on turns the other off. An agent saved with both on (from an older version) stops after each task and runs no idle clock, which is what its menu shows.
- **When cilly stops** (Ctrl+C, closing its terminal, logging out or `systemctl --user stop cilly`), it stops each running agent like the Stop button does, so memory agents save their conversation first. If one takes longer than 180 seconds, the same time Stop waits, its VM is killed. Pressing Ctrl+C a second time kills them all at once. After a restart, every agent is stopped until you start it.
- **Stop during a task** ends Claude Code, lets the task finish up (save the conversation, commit and push) for up to 90 seconds, then saves the memory and powers off.
- **RAM on your PC:** a VM takes memory as it uses it, and never more than the size you set for it. When it's idle, its free memory goes back to your PC on its own, and after each task it also empties its file cache so that memory can go back too. How much an idle agent keeps after a big build is still to be measured on real hardware. The aim is a few hundred MiB.
- **Reset to clean** (persistent) deletes the disk. **Clear memory** (memory mode) forgets stored conversations.
- **Move to new image** (persistent, in the **···** menu, only on your PC) puts a stopped agent on another VM image and keeps its home folder: its files in `~/scratch` and `~/outbox`, its checkout with uncommitted work, Claude's conversations, caches and settings. Anything installed outside the home folder comes from the new image. The VM starts twice, and the status says "Moving to new image (1 of 2)":
  1. The agent's own disk boots with an empty transfer disk next to it. The runner packs the home folder onto it, as the agent user, and the VM powers off.
  2. A fresh disk from the new image boots with the transfer disk read-only. The runner checks the packed files against their SHA-256 and unpacks them into the new home folder, again as the agent user. tar's own rules keep everything inside the home folder, with no absolute paths, no `..` and no setuid bits.

  Your PC never opens what's packed. It only passes the transfer disk from the first VM to the second, always as a raw disk, so QEMU never reads anything the VM wrote there as an image format. The old disk stays until the second boot says the move worked. If anything goes wrong, or cilly stops halfway, the agent goes back to its old disk and image, unchanged, and says why. While it moves, your PC needs free space for the packed files and the new disk, and cilly checks that first. Afterwards the transfer disk and the old disk are deleted. The **This VM** card then says which image it moved from.
- Memory export is validated by the panel, which accepts only plain files and folders under `CLAUDE.md`, `projects/` or `cilly-state.json`, with no links, bad names or oversize content. Otherwise it's rejected and the previous memory is kept.

### Attaching files and images to a message

On the agent page, and for a new agent's first message, use **Attach files**, paste a screenshot into the message box, or drop files anywhere on the window. The message box gets a colored outline while files are over the window. They go into the VM read-only, root-owned, at `/attachments/<id>/<name>`, and the message tells Claude where they are (it can open images directly). A file alone is a message too: send a screenshot without typing anything, and the agent gets "Have a look at the attached file" with the list of files. The browser uploads them to the panel, which keeps them on the host only until the message is sent, then deletes them. The panel never reads paths on your machine for this. Names are cleaned (no folders, no `..`), and there are at most 10 files of 25 MiB each per message (`attachments` in the config). A bigger file is refused as you attach it, before the browser reads it. For those, put them in a folder and give it to the agent as its **Host folder** on the Sandbox tab, which takes up to 4 GiB. This direction can't harm the host. Just remember that whatever you attach, the agent can read (and a research agent could send it anywhere on the web).

### Voice input with whisper.cpp

A **Talk** button next to the message boxes records your microphone and fills in the text as you speak. It runs on your own [whisper.cpp](https://github.com/ggml-org/whisper.cpp) server, so your voice never goes to an outside service. whisper.cpp doesn't stream, so the page cuts your speech into phrases at pauses (about 0.7 s). A finished phrase is transcribed once and stays, and only the phrase you're still saying is re-sent for the live preview. Every request stays a few seconds long however long you talk, silence is never sent (whisper tends to invent text for it), and Stop only waits for the last phrase.

whisper.cpp can run on this PC, or on another PC on your network, ideally one with a graphics card.

1. Build whisper.cpp and download a model. You need git, CMake and a C++ compiler.
   ```sh
   git clone https://github.com/ggml-org/whisper.cpp.git
   cd whisper.cpp
   sh ./models/download-ggml-model.sh base.en
   cmake -B build
   cmake --build build -j --config Release
   ```
   `base.en` is small and quick, and only knows English. Bigger models such as `small.en` or `large-v3-turbo` are more accurate and slower. For other languages, pick a model without `.en`. With an NVIDIA card, add `-DGGML_CUDA=1` to the first `cmake` line, or `-DGGML_VULKAN=1` for other graphics cards.
2. Start the server.
   - On this PC, it listens only here:
     ```sh
     ./build/bin/whisper-server -m models/ggml-base.en.bin --host 127.0.0.1 --port 8080
     ```
     Its URL is `http://127.0.0.1:8080`.
   - On another PC, use `--host 0.0.0.0` so it listens on the network, and allow port 8080 only from this PC (for example `ufw allow from <this-ip> to any port 8080 proto tcp`). The server has no login. Its URL is `http://<whisper-pc-ip>:8080`.

   No `--convert` is needed, because the page already sends 16 kHz mono WAV. whisper-server assumes English. For another language, start it with `-l auto` to detect it, or with its code, such as `-l de`.
3. In the panel, open **Settings → Speech to text**, paste the URL and press **Test connection**. It sends half a second of silence and says how long the answer took. Then turn on **Show the microphone** and press **Save settings**. It works right away, with no restart.

The browser asks for microphone permission the first time. Audio goes browser → panel → your whisper server, and nowhere else. There's no proxy, no redirects are followed, and the VMs can't reach the whisper server. It travels unencrypted over your network (whisper.cpp's server has no HTTPS), so use another PC only on a network you trust, or reach it through Tailscale, WireGuard or an SSH tunnel. Recordings stop after the maximum set there (120 s unless you change it).

### Agents without a repository

Leave the repository empty and the agent works in `~/scratch` inside its VM, with no git, no GitHub token and no PR. Two ways to get the work out:

- **Files in the VM** (agent page → Files): browse `~/outbox`, `~/scratch` or the checkout of a running agent, look at a file in the preview next to the list (double-click opens it large). A click on a picture opens the image viewer instead (below). On a phone a file opens full screen with ← to go back, the sort button cycles Name, Size and Added, and Attach… opens as a sheet), or download it to the device you're on (a paired phone too). Nothing is written on your PC. Sort by name, size or when it was added (folders stay first, and the order is remembered per source), or filter by name (on a phone, behind the **Filter** button). The list keeps up with what the agent writes, every few seconds while it's open, and a file the agent rewrote is fetched again when you open it. Before anything is shown, the panel checks the file by its bytes, not its name, and says what it really is: a `.png` that is really a ZIP archive, a program, a script, an HTML or SVG file (these can contain scripts), extra data hidden after the end of an image, and text with invisible or direction-changing characters (the "Trojan Source" trick, marked in the preview). Previews can't run anything. Text is shown as text, and **Copy** puts the whole file on your clipboard (it tells you when there are hidden characters in it, because they're copied too). Images are decoded by the browser and redrawn on a canvas from their pixels, so the original bytes are never displayed, and the panel also checks that the browser decoded the same picture size it found. Files only reach the browser inside JSON, never as a page or with their own type. Links are listed but never followed, `.git` is skipped, and one file is at most 25 MiB.
- **Export** (agent page → Files → **Export…**): it exports what the browser shows (`~/scratch`, `~/outbox` or, for repo agents, the checkout without `.git`) into a host folder you name. Recent exports are listed in the same popup. The panel copies the files into a **new** subfolder named `<agent>-<what>-<UTC time>`. It only happens when you press the button. The VM never sees your host folders or picks paths, nothing is overwritten, links/devices/`..` paths and paths deeper than 32 folders or longer than 1024 bytes are rejected (a rejected export leaves nothing behind), executable bits are removed, Git folders are left out (a `.git` in any spelling, and any folder holding a HEAD file git would accept, which makes it a repository, because git runs commands named in a repository's config as soon as you or your shell prompt run it there, and the export says how many), and size and count are capped (`exports.max_bytes`, default 512 MiB, and `exports.max_files`, 20000 files and folders together). Destinations like your home folder itself, `~/.config`, `~/.ssh` or `~/.local` are refused. The agent must be running.
- **Keep its outbox when it stops** (in the same Export popup, and under **New agent → Options**, for ephemeral agents and ephemeral agents with memory): before such an agent's disk is deleted, its `~/outbox` is copied the same way into a new subfolder of the folder you named. That's after each task with **Stop after each task**, when it stops after being idle, and when you press **Stop**. It happens only when the outbox has files, and it's listed with the other exports. The VM stops once the copy is done, or fails (it says why), or after 10 min. Quitting cilly doesn't wait for it. Turn it on before the agent's first task when it has **Stop after each task**, since its disk is gone right after the task. A project can set a folder for all its new agents, under **Defaults for new agents → Keep outboxes**. If that folder is gone when you create one, the agent is made without it and a toast says so. A paired device can't name a folder, as it's a folder on your PC, but a project's folder still applies to agents made from a phone.
- **Attach repository**: create an empty repository on GitHub **with a README**, add it to the bot token's repositories in Settings, then press **Attach repository…** on the agent's Files tab. It lists your repositories and asks GitHub which ones fit: an empty one with a README that the token can push to. One with more commits, or that's read-only for the token, is shown greyed out with the reason (on a paired phone the list isn't checked, and the panel checks your pick either way). The panel keeps GitHub's answers for a minute, in memory only. The runner turns `~/scratch` into a checkout. The scratch files go on top of the default branch on a new branch (files both have keep the scratch version), and it pushes and opens a draft PR. From then on the agent is a normal coding agent on that repo, still in `~/scratch`, so `--continue` keeps the conversation. If the agent had run `git init` itself, that `.git` is moved to `~/scratch-git-<task>`. If attaching fails, the agent goes back to scratch mode.

## Projects

A project is a reusable setup for agents that work on the same thing, like a project in Claude or ChatGPT. Create one with the **+** next to **Projects** in the sidebar, or with **New project** under More on a phone. Its name in the sidebar opens its page, and the arrow next to it folds its agents away. Groups from before became projects with nothing set yet.

**What a project gives its coding agents:**

- **Instructions.** Added to each agent's CLAUDE.md after your Claude policy, under "Project: <name>". Use them for what's true of the whole project: the platform, the build, the conventions. For what only one agent should know, use that agent's own **Instructions** (see [Claude Code policy](#claude-code-policy)).
- **Notes.** Short, lasting facts every agent should know. You can write them yourself. Agents can suggest them too: each agent is told to add a line to `~/outbox/project-notes.md` when it learns something worth sharing. After every task, cilly reads that file, before Stop after each task powers the VM off, and each new line becomes a **Project note** in your inbox. Nothing is added until you press **Add**, and you can edit the text first. A suggestion you dismissed doesn't come back, even if the agent keeps it in the file. Notes become part of every agent's CLAUDE.md, so read them like any text from an agent.
- **Files.** Reference files you add on the project page, such as an API guide, your style rules or a server config. Each coding agent in the project gets them read-only at `/attachments/project/`, through the same checked transfer as attachments: when it starts, and right away when you add or remove one. Up to 50 files, 25 MiB each and 100 MiB together. They're stored on your PC in `~/.local/share/cilly/projects/`. On a persistent agent, a file you removed only disappears once its VM image has been rebuilt with this version.
- **Allowed sites.** Added to the global allowlist for the project's coding agents, and applied at once. The agent's **Sandbox** tab marks them "from project", so you can always see where an allowed host comes from. The open web (`*`) is never allowed for coding agents.
- **A research agent.** Coding agents in the project ask it their web questions, with YOLO mode if you turn it on for the project. Moving an agent into the project links it.

**Defaults for new agents.** Pick the project in New agent, or press **New agent** on its page, and its repositories come first in the list, under **No repository**, which New agent always starts on so a private session never gets one by accident, with its lifecycle, Stop after each task, where to keep outboxes, VM image, model, effort, MCP servers and the sites of the global allowlist it leaves out filled in. You can still change each of them.

**Moving agents.** An agent's **⋮** menu has **Move to project**. Moving an agent in gives it the project's instructions, sites, files and research agent, live. Moving it out takes the instructions and sites away again. Its own extra sites stay its own either way. Deleting a project keeps its agents, under Other agents, with their own settings.

**Research agents** can be in a project too, to keep them next to the agents they answer. They don't get its files, instructions or notes, because they have the open web and anything they hold could leak. This follows the same rule as host folders, which research agents never get either.

**From a phone:** you can open a project, see its agents and make new agents in it. It also shows the project's instructions, notes, files, defaults, research agent and allowed sites, to read only. Changing a project, and adding suggested notes, is for the PC, like the other settings.

Projects are stored in `~/.config/cilly/projects.json` (mode 0600, no secrets in it).

### Example: a Rust web service

A project for a sync server written in Rust, with its own tests and a database. Paste each part into the project page.

**Instructions:**

```
We build Lantern's sync server, a web service in Rust.
- Rust stable. Run cargo fmt, cargo clippy and cargo test before you finish.
- Use axum for HTTP and sqlx for the database, and keep handlers thin.
- Never log note contents or tokens, not even at debug level.
- Add new settings to config.toml with a comment and a sensible default.
- Don't bump dependency versions unless the task asks for it.
```

**Allowed sites:**

```
docs.rs
sqlite.org
```

Turn on the **crates.io** preset in Settings as well, for the crate registry.

**Defaults:** Persistent, with Stop after each task off, so each agent keeps its build cache and conversation. Pick the image with Rust.

**Files:** your `config.example.toml` and a short `CONVENTIONS.md` if you have one.

**A first note** you could add yourself: "The server runs on SQLite in WAL mode, so test anything that writes with two clients at once."

## Research agents

A research agent has the open web and no credentials.

The dangerous combination is one agent that reads untrusted web pages, holds valuable credentials, and can send data anywhere. So web research runs in a separate agent:

| | Coding agent | Research agent |
|---|---|---|
| Network | your allowlist (plus hosts you approve per agent) | open web (any public host on port 443, and on 80 if you add 80 to `egress.ports`), but this PC, your local network, VPNs and addresses in Never reach stay blocked |
| GitHub token, repo | yes | **no** |
| Injected folders | yes | **no** (anything injected could leak) |
| Claude login | the agent token | its own slot (`research:CLAUDE_CODE_OAUTH_TOKEN`, see "Claude login" below) |
| Output | branches and PRs | text files in `~/outbox/`, returned to the panel after each task |

How work flows between them:

```
coding agent ──writes ~/requests/<topic>.md──► panel: "Requests from this agent"
                                                  │ you Approve (or YOLO: automatic)
                                                  ▼
                                   research agent researches, writes ~/outbox/*.md
                                                  │ panel validates: UTF-8 text only, safe names, ≤ 2 MB
                                                  ▼
                       panel: View the findings, then Forward (or YOLO: automatic)
                                                  │
coding agent ◄── /inbox/<id>/ (read-only) + "results are in /inbox/<id>, continue" (queued)
```

- **Linking:** link a coding agent to a research agent on the coding agent's Research tab ("Answered by"). Linked coding agents are told, in every task, how to ask, with one question per file in `~/requests/`.
- **Asking directly:** you can also give a research agent instructions yourself and forward its findings to any coding agent.
- **Review vs YOLO:**
  - **Review (default):** you approve each request and read the findings before forwarding them.
  - **YOLO (per coding agent):** requests are approved, and findings forwarded, without you in the loop. It's convenient, but untrusted web text then reaches the agent that holds your GitHub token with nobody reading it first. The other limits (allowlist, token scope, branch protection) still apply. YOLO allows 3 automatic rounds in a row by default, so an agent that keeps asking can't run on without you. After that, its new questions wait in your Inbox until you send it a message, which starts the count again. Change the number, or turn the limit off, under **Settings → YOLO mode** (`research.yolo_rounds`, `null` for no limit).
- **Findings the panel refuses** (too large, a file name it doesn't take, or too many waiting for you) show as an error on the research agent. In its VM they move to `~/sent/refused-<task>`, and they're never sent again. Findings that never reached the panel (because the link to the VM broke, say) also show as an error, and they wait in `~/sent/unconfirmed-<task>`. Either way, the question they answered fails with the reason, so you can approve it again.
- **A deleted research agent** takes its findings with it. The questions it was working on, or had answered without you forwarding the findings yet, then fail with that reason, so **Approve** asks your current research agent again.
- **When the coding agent is off:** forwarded findings wait and are delivered when it next starts. A message sent to a busy agent is queued and runs after the current task.

## MCP servers

You don't set MCP servers up inside the VMs. Keep a library of them in **Claude policy → MCP servers**, and turn them on per agent.

**+ Add a server** asks for:

- **Remote:** a name and an `https://` address. If it needs an API key, turn on **It needs an API key**, name the key (the name is suggested from the server's, like `PIXELLAB_API_KEY`), pick whether it's sent as `Authorization: Bearer` or as `x-api-key`, and paste the key. You can leave the key empty and paste it later.
- **Local:** a name and the command that runs it inside the VM. Its key goes in the command's environment under the key's name.
- **Paste a command:** the `claude mcp add …` command a server's docs give you. The panel reads it, and swaps any key in it (a Bearer token, an API-key header, a `-e KEY=value`) for a `${NAME}` placeholder.

Each server in the list shows where it runs, its keys and which agents have it on. **Set key** (or **Replace**) pastes a key. **···** beside it, or a right-click or long-press on the row, has **Duplicate…**, which copies it under a new name, and **Remove**, which turns it off for every agent and project first. These are saved right away. Only the panel on your PC can change the library.

**Edit as JSON** under the list has the library in the same format as `.mcp.json`, with keys as `${VAR}` placeholders, and applies when you press **Save and apply live**:

```json
{"pixellab": {"type": "http", "url": "https://api.pixellab.ai/mcp",
              "headers": {"Authorization": "Bearer ${PIXELLAB_API_KEY}"}}}
```

A server that runs inside the VM is a `stdio` entry. For REA, from the `rea` toolchain:

```json
{"rea": {"type": "stdio", "command": "rea", "args": ["mcp"]}}
```

Only tick it for agents whose image has the `rea` toolchain. Elsewhere Claude Code can't start it.

- **Per agent:** tick the servers an agent may use with **MCP** by the message box, under **MCP servers** on its Sandbox tab (next to its Instructions), or when creating it. Claude policy stays the library you choose from. Only those are installed in its VM, as `/etc/claude-code/managed-mcp.json`. It's root-owned and *exclusive*, so the agent and the repo's own `.mcp.json` can't add servers. A server you remove, or change under the same name (another URL, command, arguments or secret, also by hand in config.json), is turned off for every agent and project default, so you tick it again where you want it. The panel keeps a hash of each definition in `~/.local/share/cilly/mcp-seen.json` to notice that.
- **Secrets:** a key you didn't paste when you added the server is asked for the first time a task needs it. With **Save on this PC** on, it's saved encrypted on this PC. Otherwise it's kept in memory. It never goes on the VM's disk. Claude Code fills in the placeholder from the task's environment. To fetch it from `pass` instead, set `"mcp_secrets": {"PIXELLAB_API_KEY": {"command": ["pass", "show", "cilly/pixellab"]}}` in the config. A key typed directly into a server definition is rejected.
- **Network:** a remote server's host (e.g. `api.pixellab.ai`) is added to that agent's allowlist automatically. A remote MCP server is also somewhere the agent can send data, so only add servers you trust.
- **Changes apply live:** they reach running agents at their next task.

The key itself never goes into the config. Typing `claude mcp add` in a message to an agent does nothing useful, and that's on purpose. The VM only loads the MCP servers the panel installs (root-owned, `managed-mcp.json`), so an agent can't wire itself to a server you didn't approve.

## Plugins

Claude Code plugins bring skills, commands and agents, like [Impeccable](https://github.com/pbakaus/impeccable) for design work. cilly keeps a library of them, and each coding agent's VM installs the ones it has.

- **Add one** under **Claude policy → Plugins**, with the name the plugin has in its marketplace and the marketplace itself: a GitHub `owner/repo` or an `https` git URL. Or type `/plugin add pbakaus/impeccable` in an agent's message box, which adds it to the library and turns it on for that agent. Without a name, it takes the repository's name. A paired phone can add one with `/plugin add` too, but only from a GitHub `owner/repo`, as GitHub is on every agent's allowlist already. A plugin from another site would add that site to the agent's allowlist, so that one only goes from the panel on your PC.
- **Turn it on** for an agent with **MCP** by the message box, under **Plugins** on its Sandbox tab, in **New agent → Options**, or for every new agent in a project under its **Defaults for new agents**. A paired phone can turn plugins on and off too.
- **In the chat**, `/plugins` lists an agent's plugins, and `/plugin remove impeccable` turns one off. cilly handles these itself. Claude never sees them, and they don't start the agent. Any other message that starts with `/` goes to Claude as before. Claude Code's own `/plugin` menu doesn't work in cilly, as Claude Code runs without its interactive screen.
- **Installing** happens in the VM, before the agent's next task, as the agent user, with Claude Code's own `claude plugin install`. It goes through the agent's proxy, so its allowlist and route apply. GitHub is on the default allowlist, and a plugin's own host is added to the agent's allowlist. The chat says "Installed the plugin impeccable", or why it couldn't. A plugin you turn off is removed before the next task. A persistent agent keeps its plugins on its disk, and an ephemeral one installs them again at each start, which takes a few seconds. The task's tokens don't go along to the installer.
- **Install commands:** some marketplaces install a plugin by running a command of their own. Claude Code doesn't run it without asking, so the plugin shows that command under **Plugins**, with **Allow this command**. Allowing it saves its sha256 in the library, and the next install runs exactly that command in the VM. If the marketplace changes the command, it asks again.
- **Hooks:** a plugin's hooks don't run while your managed settings have `allowManagedHooksOnly`, as they do by default. Its skills, commands and agents still work. A plugin's MCP servers don't load either, as `managed-mcp.json` is exclusive.
- **Versions:** a plugin isn't pinned. An ephemeral agent gets its latest version at each start. Everything a plugin does stays inside the agent's VM, like anything else the agent runs.

## Your own skills

A skill you wrote yourself doesn't have to go on GitHub. Upload it under **Claude policy → Skills** with **Upload a skill**: a `.zip` of the skill's folder, or just its `SKILL.md`. It stays on your PC.

- **What a skill needs:** a `SKILL.md` at the top of its folder that starts with a `name:` (small letters, digits and `-`, like `pixel-eye`) and a `description:` that tells Claude when to use it. Other files next to it, such as scripts or references, come along.
- **What's checked** on your PC before it's kept: up to 10 MiB uploaded and 20 MiB unpacked, at most 2,000 files and 16 levels of folders inside the skill's own folder, plain files only (no links), names of letters, digits and `._-`, and nothing that climbs out of the folder. The skill is the folder with the SKILL.md nearest the top, so a zip of a repository with `skills/<name>/` works too, and what's outside that folder is left out. Hidden files, like `.git`, and what a Mac adds to a zip are left out. The panel never unpacks it on your PC. It packs the checked files again into a tar of its own, kept in `~/.local/share/cilly/skills/`.
- **Turn it on** for an agent with **MCP** by the message box, under **Skills** on its Sandbox tab, in **New agent → Options**, or for every new agent in a project under its **Defaults for new agents**. A paired phone can turn skills on and off, but only the panel on your PC can upload or remove one.
- **In the VM** the tar arrives read-only at `/attachments/skills/`, through the same checked transfer as project files. Before the next task, the runner checks its hash and unpacks it into `~/.claude/skills/<name>/`, as the agent user, so it can't write anywhere the agent can't. The chat says "Added the skill pixel-eye". A skill you turn off is removed before the next task. Uploading one with the same name replaces it, and the agents that have it get the new one.
- **Research agents** never get skills. They have the open web, so anything they hold could leak.
- Skills need an image built with this version of cilly. An older image ignores them.

## Network approvals and installing tools

Blocked hosts and research questions also appear right above the message box, with **Deny** and **Allow** (this agent only) or **Approve** (only for questions short enough to read there, while longer ones open the review page). Every agent page lists the **blocked hosts** that agent tried to reach. For each one:

- **Allow for this agent:** adds it to that agent's own list.
- **Allow everywhere:** adds it to the global allowlist.

Either way it applies live, on the agent's next connection. When the port isn't one every site gets, the blocked list offers the host with its port, such as **Allow github.com:22**, so only that port opens.

To give one agent a whole list of sites, use **··· → Allowed sites for this agent** (or "Paste a list of sites" on its Sandbox tab) and paste it, one per line or comma-separated. URLs, bullets and `#` comments are fine. The panel keeps the host names, with a port when one is given, and tells you what it skipped (a port outside 1 to 65535, a user name in a URL, anything that isn't a host). That list is the agent's own, added on top of the global allowlist in Settings, and saving replaces it. You can edit a host into a wildcard such as `*.gradle.org` before allowing it. Coding agents can never get the open web (`*`).

The global allowlist holds the defaults every coding agent gets. To leave some of them out for one agent, uncheck them under **New agent → Options → Advanced**, or later on its Sandbox tab under **Global allowlist**, where the change applies at once. A project can leave some out for all its new agents, under **Defaults for new agents → Global allowlist**. Each default shows what it's for. Before you leave one out:
- An agent on a repository needs `github.com` to clone and push, and `api.github.com` for its pull request. The panel warns when you leave one of those out.
- `deb.debian.org`, `pypi.org`, `files.pythonhosted.org` and `registry.npmjs.org` are for installing packages. An agent often needs them only while it sets up, so you can leave them in for its first task and take them out afterwards.

If the agent then tries a site you left out, it shows up as blocked, and **Allow** puts it back for that agent. Its project's sites and its own sites stay allowed whatever you leave out.

This is also how agents install their own tools. They don't need root to install into their home directory: a JDK tarball or SDKMAN, Maven, the Gradle wrapper, `pip --user`, npm with a user prefix, rustup and so on. Let the agent try, approve the download hosts it was blocked on, and tell it to retry.

Tools installed this way survive only in **persistent** mode. Ephemeral and memory agents start from the base image every time, so for toolchains you use often, build them into an image.

### Ports per site

A site can carry its own port, like `sync.example.net:8080` or `*.example.com:7777`, with a port from 1 to 65535. This works everywhere a site can be allowed: the global allowlist in Settings, a project's sites and an agent's own sites.

- A site with a port allows just that port. `sync.example.net:8080` doesn't open 443 on that host. Add `sync.example.net` too if you need both.
- A site without a port gets the ports in `egress.ports` (only 443 unless you add more).
- So to reach one server on an unusual port, name it with its port. You no longer need to add the port to `egress.ports`, which opens it for every allowed site.
- The open web stays on 443, and 80 if it's in `egress.ports`. A research agent can't get other ports.
- Every address rule still applies, whatever the port: this PC, your local network, VPNs and Never reach stay blocked.
- From a paired phone you can allow a site only on the ports in `egress.ports`. Other ports need the panel on your PC, and the phone says so.
- The proxy only tunnels connections (`CONNECT`). HTTPS, git over HTTPS and other TLS or raw TCP clients that use the proxy work. A plain `http://` request to the port is refused, as it is on port 80.
- The proxy looks the name up on your PC and connects over IPv4 when the site has an IPv4 address, and over IPv6 only when it doesn't. A slow IPv6 route at your provider, which browsers often hide, then can't slow down your agents and image builds. Every address goes through the same checks.

### What no agent can reach

Some refusals can't be fixed by allowing a host, so the blocked list only offers **Dismiss** for them and says why:

- **Your own machines.** For every connection, the proxy reads this PC's routes and addresses from `/proc/net`, from every routing table, and refuses an address that is one of the PC's own, or inside any route other than the default route. That covers your local network, including the global IPv6 addresses most home routers give every device, VPNs such as WireGuard or Tailscale, and Docker or libvirt bridges. A network change counts at once. If the files can't be read, nothing is let through. Very wide routes (IPv4 /4 or shorter, IPv6 /15 or shorter) count as the default route, because a VPN's full tunnel uses them in its place, like OpenVPN's `0.0.0.0/1` and `128.0.0.0/1`. The egress log notes each one once.
- **Never reach.** Addresses and networks you list under **Settings → Never reach**, one per line, like `203.0.113.7` or `2001:db8:1234::/56`. They're checked after the name is looked up, so a dynamic DNS name for your home doesn't help. Changes reach running agents on their next connection.
- **Ports for the open web.** The open web only gets 443, and 80 if you add it to `egress.ports`, never any other port. A host allowed by name gets every port in `egress.ports`, or just the port it names (see [Ports per site](#ports-per-site)). The agent's Sandbox tab says which ports the open web gets.

These apply to every agent and to image builds. Coding agents only reach named sites, so they rarely notice.

Your PC can't learn its home's public address without asking a service on the internet, which cilly never does, so put these in Never reach yourself:

- **Your public IPv4 address.** Your router's status page shows it, usually as the WAN or internet address. If it starts with `100.64` to `100.127`, your provider shares one address among many customers (CGNAT), and there's nothing of yours to list. If your provider changes it now and then, update the entry when it does.
- **Your IPv6 prefix.** The router's status page shows the prefix your provider delegated, often a `/56` or `/48`, such as `2a02:8070:1234:5600::/56`. List the whole prefix. The `/64` this PC is on is blocked anyway, but other home networks behind the same router may not be in its routes. `ip -6 addr show scope global` on this PC shows its own addresses, from which you can see the start of the prefix.
- **Servers you own,** such as a VPS or a NAS someone else hosts, by their addresses.

Agents can't ping or send UDP at all. QEMU's user networking runs with `restrict=on`, which drops ICMP and UDP from the VM and lets only the TCP connection to the proxy through (libslirp answers a ping to its own virtual gateway itself, without anything leaving).

### Routes: a proxy, a VPN or Tor

Off by default. A route sends an agent's traffic on through a proxy you choose, so the sites it reaches see that proxy's address instead of yours. It's for people who need that: privacy at a research agent's searches, a VPN's exit, or Tor. Set routes up in **Settings → Network → Routes**:

- **SOCKS5** (a VPN provider's SOCKS5 server, your own server, `ssh -D`) or **HTTP proxy**, at a host and port, with an optional user. Its password goes under **Settings → Secrets** once you save, never into the config file.
- **WireGuard VPN**, from its `.conf` file (the one your VPN provider or your own server gives you). Paste it under **+ Add a WireGuard VPN**. Its private key and preshared key go straight to **Settings → Secrets**, and the rest to the route. cilly never runs anything the file says. It leaves out `PostUp` and the like, and the panel tells you which. cilly runs [wireproxy](https://github.com/windtf/wireproxy) for it, as you, with no root and no network interface. It runs inside bubblewrap when that's installed, from a config cilly writes in a private folder in memory, with its SOCKS5 server on 127.0.0.1 only and a login made up for each run. Names are looked up at the VPN's own DNS server, through the tunnel. One wireproxy runs per VPN that agents use, it starts again if it stops, and it stops when you remove the route or quit cilly. Installing `wireproxy` is optional. Arch packages it, and elsewhere it's a single program from its releases page. The host check lists it.
- **Tor**, built in. cilly runs its own `tor` as you, the first time an agent's route needs it: inside bubblewrap when it's installed (the same rule as for QEMU), seeing only `/usr`, its own data folder and its socket folder, never your home. It reads no torrc, listens only on a Unix socket in a private folder of your runtime folder (no TCP port, no control port), and stops with cilly. Each agent gets its own Tor circuits, so sites can't tie two agents together. When a Tor exit can't look a name up or reach an address, the agent moves to a new circuit and tries once more. If Tor stops, cilly starts it again. Installing `tor` is optional. The host check lists it.

**Which route an agent takes:** its own (the **Route** row in its Sandbox tab, or **Route** on New agent), else its project's (in the project's **Allowed sites** card), else the **Standard route** in Settings. **Straight out** is a choice too, so an agent can skip the standard on purpose. A change applies at once, also to running agents. A paired phone can pick a route for an agent, but routes are set up only on your PC.

**Every check still runs first, on your PC.** The allowlist, the block on your own network and Never reach all apply as before. cilly looks the name up itself, checks the address, and asks the route for a connection to that exact address, so a name can't answer one way to the check and another way to the proxy. Then:

- **With Tor,** the name is looked up at the Tor exit, never on your PC.
- **With a WireGuard VPN,** the name is looked up at the VPN's own DNS server, through the tunnel, so its private names work too. A VPN config without DNS uses what you pick under **Look names up with**.
- **With SOCKS5 or HTTP,** names are looked up with what you pick under **Look names up with**: this PC's DNS (your provider sees the names), or DNS-over-HTTPS at Quad9, Mullvad, Cloudflare, AdGuard (no filtering) or your own resolver. The lookup goes through the route to the resolver's fixed address, with its certificate checked, so the resolver sees the route's address and your provider sees no names.

**Private hosts it may reach.** Your own network is blocked for agents, on every route. A route can list private hosts it may reach anyway, such as a self-hosted Git server behind your VPN. They're exact names (`git.office.lan`) or private addresses (`10.8.0.20`), never a wildcard, and there are none by default. They're reached only through that route, so never on your own network and never straight out. An agent also needs each one among its allowed sites, by name, so agents on the open web never reach them. Never reach still wins, and loopback and link-local addresses are never allowed. Set them in the route's **Edit**, on your PC only.

**A kill switch.** If the route is down, refuses, or can't be worked out (a route you deleted, a password that isn't there), the agent's connections are refused. Nothing ever falls back to going out directly. The agent's network log says why. A blocked site then says which route failed and how, like "Route office: the route closed the connection". Allowing the site doesn't help, since it was allowed already. The route is what failed.

**Test a route.** In Settings, under Network, **Test** on a saved route, or on Tor, connects through it to a site, as an agent would, and checks the site's certificate. It shows how long that took, or what went wrong, such as a proxy that can't be reached or that hung up. The site is api.github.com, or one you give under **Site to test with**, which has to be on the allowlist. The panel calls nothing else.

**cilly's own downloads.** With **Also for cilly's own downloads** on, the GitHub API calls of the panel, the Debian image download and image builds take the standard route too. Their names are looked up at the far end, and plain HTTP is refused. Downloads, GitHub calls and the program in the VM send a generic user agent rather than one that names cilly.

### VM images and toolchains

Every image has git, gh, Python 3, Node and npm, gcc, g++, make, ripgrep, curl and jq, which covers JavaScript, TypeScript, React, Tailwind, HTML, CSS and plain C and C++. Add more when you build an image. Use `cilly` if you installed it as a command, or `python3 -m cilly` from the cilly folder:

```bash
python3 -m cilly build-base --download --preset full   # downloads Debian's image and checks it first
python3 -m cilly build-base --image ~/vm-images/debian-13-genericcloud-amd64.qcow2 --preset full   # coding agents
python3 -m cilly build-base --image ~/vm-images/debian-13-genericcloud-amd64.qcow2 --preset slim   # research agents
python3 -m cilly build-base --image ~/vm-images/debian-13-genericcloud-amd64.qcow2 --with java,rust  # your own mix
python3 -m cilly build-base --image ~/vm-images/debian-13-genericcloud-amd64.qcow2 --preset full --with ghidra  # a custom image: full plus Ghidra
```

`full` has every recipe below except the optional ones, Ghidra and REA, which are large. It becomes the coding agents' default. `slim` has only the basics and becomes the research agents' default (they only need Claude and the web). **Settings → VM images** does the same with a **Build** button and shows the build's progress. It remembers where your Debian image is. While a build runs, the button reads **Add to queue**, and the next builds wait in a list under the progress, up to 8 of them. They run one after another, in order, and a build that fails doesn't stop the next one. **Remove** takes a build off the list before it starts. The page also says which earlier builds failed and why. Builds you start in a terminal don't join the queue. Under **New image**, pick a preset, or **Custom** to tick the toolchains you want and give the image a name, like "Plugin RE". A toolchain that needs another, like Ghidra needing Java, brings it along. The page shows about how much space the image takes and which extra hosts the build reaches. A custom image doesn't replace a default. Use **Make default**, or pick it for an agent under **New agent → Options**.

With `--download`, or with the file left empty in **Settings → VM images**, cilly downloads `debian-13-genericcloud-amd64.qcow2` and Debian's `SHA512SUMS` from `https://cloud.debian.org`, and follows a redirect only to that same host. It checks that the download wasn't damaged. It keeps the image only when its SHA-512 matches the one Debian lists, the same check as `sha512sum -c SHA512SUMS`. Both files come from the same server, so the check doesn't prove who made the image. The image goes into a new temporary file of its own, which is hashed as it is on disk and only then renamed to `~/.local/share/cilly/downloads/debian-13-genericcloud-amd64.qcow2`. The next build downloads it again only when Debian has published a newer one. This is the only time cilly downloads anything itself, and only when you ask for a build without a file. Each image shows its age, and New agent warns when the image it would use is over 30 days old. Rebuild for the latest Claude Code and security updates.

Images are set up to boot without waiting: the VM's network address is fixed (QEMU's user network always gives it 10.0.2.15) instead of asked for over DHCP, there's no DNS server (the VM reaches out only through the proxy, which looks names up on your PC), no boot menu and no apt or man-db timers, and the wait for the network is capped at 20 seconds. Images built before this keep booting as they did. Images are also written compressed (zstd where your qemu-img has it, else zlib), after the space freed during the build is trimmed and apt's package lists and download caches are removed. Man pages, docs and translations stay. VMs read a compressed image like any other, and what they write goes to their own disk.

When Debian's servers stop answering during a build, apt tries each download again a few times and gives up on a server that's silent for 30 seconds. If apt still gets stuck retrying the same download, the build stops at once and says so, instead of waiting out its hour. Its logs stay in the `build-` folder next to your images, and a failed build leaves nothing else behind.

| Recipe | What it installs |
|---|---|
| `java` | OpenJDK 25 and Maven, from Debian (Gradle projects use their own `./gradlew`, and Java 25 needs Gradle 9.1+) |
| `rust` | stable Rust with cargo, clippy and rustfmt, via the official rustup (checksum-verified), system-wide in `/opt/rust` |
| `cpp` | CMake, Ninja, Clang, clangd, clang-format, gdb |
| `python` | venv, pip, pipx and Python headers |
| `go` | Go, from Debian |
| `browser` | Playwright 1.63.0 (pinned) with the headless Chromium build it was tested with, in `/opt/ms-playwright`, plus the system libraries it needs |
| `decompilers` | Vineflower 1.12.0 and CFR 0.152 (pinned, checksum-verified) in `/opt/decompilers`, `jar-diff`, and unzip, zip, file, xxd and binutils from Debian. It needs `java`, so building it adds Java. About 15 MB on top of Java |
| `ghidra` | Ghidra 12.1.4 (pinned, checksum-verified) in `/opt/ghidra`, with `ghidra-decompile`. It needs `java` and uses its JDK. About 900 MB, so it's not in `full` |
| `rea` | [REA](rea.md) 6.5.0 (an MCP server and CLI for reverse engineering) on its own Node.js 24.22.0, with rizin 0.9.1, JADX (jadx-headless-mcp 0.7.1), Apktool, TShark, GDB, binutils and pwntools 4.15.0. Everything is pinned: downloads by SHA-256, REA's npm packages by a lockfile, pwntools by wheel hashes. It needs `java`. About 1.05 GB, and like Ghidra it's optional, so add it with `--with rea` |

With `browser`, an agent can open a page and take a screenshot, for example of a site it's building, with `npx playwright screenshot <url> shot.png`. Claude Code can look at an image file, so the agent sees what it built. It puts the screenshots worth showing in `~/outbox/`, where you see them under **Files in the VM**. Websites go through the same allowlist as everything else. Pages the agent serves inside the VM on localhost always work.

With `decompilers`, an agent can look inside a plugin jar you don't have the source for. `vineflower [options] <jar> <outdir>` decompiles it into Java sources (`cfr <jar> <outdir> [options]` is a second opinion), and `jar-diff <old.jar> <new.jar> [outdir]` compares two versions: it unpacks and decompiles both, lists added, removed and changed classes and files, and shows YAML, JSON and `.properties` files key by key, including keys that were renamed. Both trees stay on disk for a closer look. Attach the jars to your message (or inject a folder), so they arrive read-only, and the agent works in `~/scratch`. For example, after updating a shop plugin:

> Compare /attachments/…/Shop-4.2.jar with /attachments/…/Shop-5.0.jar using jar-diff. Then take my custom messages from /attachments/…/messages.yml (made for 4.2) and write a messages.yml for 5.0: carry each of my texts over to its new key, use the plugin's default for keys that are new, and list the ones you couldn't map.

The agent gets a summary like `renamed  messages.no-permission  ->  messages.errors.no-permission`, so moved keys keep your wording.

With `ghidra`, an agent can look inside native programs. `ghidra-decompile <binary> <outdir>` runs Ghidra headless and writes decompiled C for every function to one file, and keeps Ghidra's project in `~/ghidra-projects` for a closer look with `/opt/ghidra/support/analyzeHeadless` and its own scripts. `GHIDRA_INSTALL_DIR` is set, so tools that use Ghidra find it. Ghidra needs a lot of memory: give an agent that uses it 6 to 8 GB under **New agent → Options** (`ghidra-decompile` gives Ghidra about 70% of the VM's memory). So it runs one at a time: a second call, from a subagent say, waits for the first and says so, since two at once run the VM out of memory. The kernel then ends one of them, inside the agent's memory limit (see Resources). A big binary can take many minutes, so run it in the background (end the command with `&`) and watch the output folder. It stops a single program's analysis after 20 minutes (`GHIDRA_ANALYSIS_TIMEOUT` seconds). Adding `--with` to a preset makes a custom image that becomes the coding default, never the research default. For example:

> Decompile /attachments/…/libnative.so with ghidra-decompile into ~/scratch/libnative, then explain what the function that reads the license file does.

With `rea`, an agent gets REA's reverse-engineering tools: binary overviews, decompiling, cross-references, strings, APKs, firmware and packet captures. To give them to an agent as MCP tools, add REA to the MCP library (see [MCP servers](#mcp-servers)) and tick it for the agent. The `rea` command in the VM runs the same workflows. REA uses Ghidra too when the image also has the `ghidra` toolchain. Static analysis only reads files. Runtime capture and debugging run the target inside the agent's VM. REA can't update itself there, since its install folder is root-owned. `rea update`, `--update`, `rea setup` and `rea mcp add` are turned off too, and a newer REA comes with a newer image. [docs/rea.md](rea.md) says what was checked about it, and what it may contact. For example:

> Use REA to give me an overview of /attachments/…/server-helper: what it is, which libraries it uses, and what it does with the network. Write your notes to ~/scratch/server-helper.md.

Recipes are fixed in `cilly/base_image.py` and run as root during the build only. Files they download have a fixed version and are checked against a SHA-256 written into the recipe before they're used. The build may reach their download sites (`static.rust-lang.org` for Rust, `cdn.playwright.dev`, `playwright.download.prss.microsoft.com` and `storage.googleapis.com` for the browser, `repo1.maven.org` for the decompilers, `github.com` and `release-assets.githubusercontent.com` for Ghidra, and `nodejs.org`, `registry.npmjs.org`, `pypi.org`, `files.pythonhosted.org` and the same GitHub hosts for REA), but agents don't. You can keep several images (say, one for web work and one with Java and Rust) and pick one per agent under **New agent → Options** or in the agent's **Sandbox** popover. **Settings → VM images** lists them, sets the default and builds the command for you. Each agent is told in its CLAUDE.md notes which toolchains its image has. A new build becomes the default unless you pass `--no-default`.

Builds still need the registries at task time. For Java with Gradle, turn on the "Maven + Gradle" preset (Maven Central and the Gradle wrapper), and for Rust, "crates.io". Plugins for Paper, a Minecraft server, need the "Paper MC" preset too.

## Local models

Agents can use a model you run yourself instead of Claude's. cilly doesn't install or run a model server. You give the address of one you already have, and agents run Claude Code as always, on your model.

1. Run a server that speaks Anthropic's Messages API (`/v1/messages`). Recent versions of Ollama (0.14 or later), llama.cpp's `llama-server`, vLLM and LM Studio all do.
2. In **Settings → Local models**, add it with a name, like `gpu`, and its address, like `http://192.168.1.50:11434`. If it needs an API key, turn on **It needs an API key**, save, and paste the key in **Settings → Secrets**.
3. Press **Test**. cilly lists the server's models and sends one of them a short message, the way Claude Code does.
4. In an agent's model menu, pick one of the server's models under **Local**, where each model has its server's name next to it. Claude's models are under **Cloud**. A server that doesn't answer is left out of the menu, so it doesn't get in the way. It's used from the next message on, and you can switch back to a Claude model at any time. **New agent** and a project's **Defaults for new agents** list the same models, so a project can start its agents on one of yours.

How it works:

- **Through the agent's egress proxy only.** The agent's Claude Code talks to `https://<name>.model.cilly.internal`. Only that agent's egress proxy knows the name. It answers with the certificate of this start's CA, as it does for keeping tokens on this PC, and passes each request on to the address you gave.
- **Only while the agent's model is on that server.** The proxy learns the server when a message on one of its models is sent, forgets it when the agent starts again, and loses it at once when you remove the server or change its address in Settings. Other agents' proxies don't know the name, so they can't reach the server at all. Never reach still wins: an address on that list is refused.
- **Only the Messages API, and only the model you picked.** The proxy passes on `POST /v1/messages`, `POST /v1/messages/count_tokens` and `GET /v1/models`, and nothing else. Ollama has no login, so this keeps an agent from pulling or deleting your models through Ollama's own API. A request that names another of the server's models is refused, so an agent can't switch to a paid model the server forwards to, or load one too big for your GPU.
- **No Claude login goes along.** On a local model, an agent's task doesn't get your Claude login at all, so you can use local models without one. The VM never holds the server's API key either. The proxy adds it, and masks it if the server ever repeats it.
- **No route.** The connection goes straight from your PC to the address you gave, because the server is usually on your own network. For a server somewhere else, such as a rented GPU, use an SSH tunnel (below), which can take a route.
- **A start's certificates.** An agent gets certificates for the model servers you have when it starts. For a server you add later, stop the agent and start it again. openssl is needed, as for keeping tokens on this PC.

### Over SSH

A model server on another machine, such as a GPU you rent, usually isn't open to the internet. You reach it over SSH, and cilly can open that tunnel for you. You can still open one yourself and give cilly its local address, as before.

1. In **Settings → Local models**, add the server, set **Reach it** to **Over SSH**, and give the SSH server, its port, the user and a route. For **Address on that server**, give the model server's address as that machine sees it, like `http://127.0.0.1:11434`.
2. cilly makes a key pair just for this server. Add the line it shows to `~/.ssh/authorized_keys` for that user on the server. The line starts with `restrict,port-forwarding,permitopen="..."`, so the key can forward to the model server's port and do nothing else: no shell, no commands, no other ports.
3. Press **Get host key**. cilly asks the server for its host key, through the same route, without logging in, and shows its fingerprint. Check it on the server with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`, or in your provider's console, and press **It matches**. Until you do, the tunnel doesn't start, and after that cilly refuses any other host key.
4. Press **Test**. The tunnel starts when something first needs the server, and stays up while the panel runs.

How it works:

- **Nothing of yours.** cilly runs `ssh` with `-F /dev/null`, its own key only (`IdentitiesOnly`), no SSH agent, and a known-hosts file of its own. It never reads `~/.ssh`, your SSH config or your keys. ssh runs no command on the server (`-N`).
- **The key.** The private key goes to **Settings → Secrets** as "SSH key for gpu", saved on this PC when systemd-creds or your keyring can hold it. It's written to a private folder in memory only while the tunnel runs. **New key** makes another, and the old one stops working once you take it off the server. Removing the server deletes the key.
- **In bubblewrap.** ssh runs as you, inside bubblewrap when it's there, as Tor and wireproxy do. It sees /usr, its own folder and a passwd entry cilly writes, never your home. It reconnects on its own.
- **Through a route.** With Tor, a VPN or a proxy as the route, the SSH connection itself leaves that way, and the server's name is looked up at the far end. The server and your provider see the route's address, not yours. The route works as a kill switch here too: when it's down, the tunnel is down.
- **Only you.** The tunnel listens on this PC's 127.0.0.1 only. Before the panel or an agent's proxy sends anything to it, cilly checks that the port belongs to your account, so another account on this PC can't take the port while ssh reconnects and get your prompts or the API key.
- **Agents.** An agent reaches the server through its egress proxy, as with any local model, and the VM never sees the tunnel, the key or the server's real address.

Things to know:

- **Context size.** Ollama gives a model only 4,096 tokens of context unless you set `OLLAMA_CONTEXT_LENGTH`. Claude Code's instructions alone fill most of that, and the agent then fails in confusing ways. Set it to 32768 or more where Ollama runs. **Test** warns you when it's too small. When a task is sent, cilly asks the server how much context the model has and tells Claude Code, which would otherwise assume 200k for a model it doesn't know, and compact too late. Ollama says so for a loaded model, or one with `num_ctx` in its Modelfile, and vLLM always does. The context meter uses the same number.
- **Tool calls.** How well an agent works depends on how well the model handles Claude Code's tools. Models made for agentic coding work best.
- **MCP servers.** On Claude's models, Claude Code loads an MCP server's tools only when it needs them. On yours it sends all of them with every message, so a server with many tools can fill the context by itself (REA's take about 180,000 tokens). The model menu warns about this. Turn off the servers an agent on a local model doesn't need.
- **Switching to Claude's models.** An agent keeps its conversation when you switch models, and Claude Code sends it all to whichever model answers next. When the conversation ran on one of your models, the menu asks first: start a new conversation, which sends nothing from before, or continue and send it to Anthropic.
- **Effort.** Claude Code sends an effort with every request, `high` unless you pick another in the model menu. Many local models take it, but some chat templates only take their own words for it (say low, medium and xhigh) and fail on anything else. **Test** checks this and names the efforts the model takes, so you can pick one of those for agents on it. Switching between Claude's models and yours sets the effort back to the default, since one picked for either may not suit the other.
- **System messages.** Claude Code sends part of its instructions as a system message after your first message. Many chat templates only allow one at the very start, and the server then fails. So the proxy refuses such a request to your server with a 400 error, and Claude Code sends the same instructions again inside your message, which every template takes.
- **Research agents** can use your models too. They browse the web and read a lot, so a model with a large context works best. On one of your models, a research agent's task doesn't get its Claude login either.

## Claude Code policy

**Claude policy** in the panel edits three things that apply to every agent:

- **CLAUDE.md:** your code style and working rules. It's installed as `/etc/claude-code/CLAUDE.md`. It starts empty. Setup offers cilly's recommended rules, and **Add recommended rules** on Claude policy adds them later. They always go after your own text, never in place of it, and lines you already have are skipped. They're about 25 lines on what most people ask for: ask when a request is unclear, do only what was asked, keep changes focused, run the tests before saying it's done and never weaken them, ask before adding a dependency or deleting anything, don't push unless asked, plain commit messages, and direct answers. Three switches add style lines under "# Style": no em or en dashes, plain punctuation in code and config, and no emojis. With no dashes on, **Check each file Claude writes for em and en dashes** is turned on too (`claude_policy.dash_check`). It's a managed hook that runs after every edit, and when the file has one, it tells Claude the line numbers so it fixes them. You can turn the check on or off by itself. **Keep the plan through compaction** (`claude_policy.keep_plan`) is for long tasks, and for local models, which compact often. It adds a short "Long tasks" section to CLAUDE.md: keep the plan in `~/plan.md` and update it after every step, carry on from its next open step after a compaction, and give subagents the goal and their step, since they don't see the conversation. It also adds a managed SessionStart hook that runs after each compaction and puts the first 8,000 bytes of `~/plan.md` back into the conversation, so the plan doesn't depend on the summary. An ephemeral agent loses the file when it stops, so use a persistent one, or Ephemeral + memory, for a task that spans several starts. Unless you switch it off (`claude_policy.vm_notes`), the panel adds a short "About this machine" section per agent, so it doesn't waste turns finding out. The section tells it that it's in a disposable VM and that only allowlisted hosts work. A blocked site shows up for you to allow (it should say what it needed and never work around a block). There's no sudo, `/attachments` and `/inject` are read-only, and `~/outbox/` is for files meant for you (you take them out with Export). The section also names its working folder (`~/scratch`, or the checkout, where it commits on its branch and the panel pushes and opens the PR), says that an ephemeral VM forgets everything on stop and, with a linked research agent, explains how to ask questions through `~/requests/`.
- **Managed settings (JSON):** installed as `/etc/claude-code/managed-settings.json`. These outrank user and project settings, so a repo's own `.claude/settings.json` can't loosen them. Defaults:
  - `allowManagedHooksOnly` and `allowManagedPermissionRulesOnly`, so repo hooks and permission rules are ignored
  - transcripts kept for 365 days
  - telemetry, error reporting, feedback and updates off
  - no attribution in commits or PRs
  - deny rules for `.env` files under `/inject` and for force-push.
- **Permission mode:** passed on every run.
  - `bypassPermissions`: everything is allowed except your deny rules (deny rules hold even in this mode).
  - `dontAsk`: only your allow rules run. The panel then also sets `permissions.disableBypassPermissionsMode`, so nothing can switch bypass back on.

The files are root-owned, so the agent user can't edit them. **Save** pushes the policy to every running agent over the control port, and Claude Code reloads managed settings when they change. Agents that aren't running get the policy at their next start.

**Instructions for one agent** are on the agent's **Sandbox** tab, next to its other settings. They go into that agent's CLAUDE.md after your Claude policy and the project's part, under "Instructions for this agent", and apply from its next task. They work for research agents too. They can be up to 20,000 characters, and control and invisible characters are removed, as in project instructions. Only the panel on your PC can change them. A paired device sees them read-only.

The **egress allowlist** is live too. Editing it in Settings rewrites each agent's allowlist file, and running proxies pick it up on their next connection.

## Secrets

Secrets are never written to disk in plain text.

In config, each secret is either a **command** whose first output line is the secret, run without a shell:

```json
"tokens": {
  "bot": {
    "description": "my-agent-bot fine-grained PAT",
    "repos": ["you/project-a", "you/project-b"],
    "secret": {"command": ["pass", "show", "cilly/gh-bot"]}
  }
},
"panel_token": {"secret": {"command": ["secret-tool", "lookup", "service", "cilly", "key", "panel"]}},
"agents": {"claude": {"secrets": {"CLAUDE_CODE_OAUTH_TOKEN": {"command": ["pass", "show", "cilly/claude"]}}, "...": "..."}}
```

…or `{}`, in which case you paste the value in **Settings → Secrets**. Pasted values live only in the panel's memory and are lost when it stops.

**Type each one once (recommended):** wherever the panel asks for a key (the "Secret needed" prompt, Settings → Secrets, the setup wizard, or the keys in a `claude mcp add` command you import), leave **Save on this PC** on. The panel then stores it with systemd's encrypted credentials (systemd 256 or newer, which Arch and Fedora have) and never asks again. With an older systemd, as on Ubuntu 24.04 and Debian 12, it goes into your login keyring with `secret-tool` instead (on Debian and Ubuntu, `sudo apt install libsecret-tools`, and the panel notices it within a minute). **Forget** in Settings deletes it. The web UI can only ever write the panel's own `systemd-creds decrypt` command for its own file, or its own `secret-tool lookup service cilly key <name>` command, this way. Any other command still has to be put in `config.json` by hand. The same from a terminal, in the cilly folder:

```sh
python3 -m cilly secrets                 # what's configured, and how each is loaded
python3 -m cilly secrets set claude      # also: research, github, token:<name>, mcp:<VAR>, panel_token
python3 -m cilly secrets forget claude   # back to pasting it
```

You type the value once (hidden). It's encrypted with `systemd-creds --user`, bound to this machine (and its TPM if it has one), and saved under `~/.config/cilly/credentials/`, so a copy of the file is useless elsewhere. In the login keyring, it's encrypted with your login password instead, and unlocked when you log in. The config then points at a `systemd-creds decrypt` command. The panel runs that like any other secret command, at every start and with no prompt, and a running panel picks the change up by itself. Nothing changes for the VMs. They still get the value only in memory, with each task. Anything running as your user can decrypt it too, the same as with `pass` once it's unlocked or a value you pasted into the panel.

If a command fails, the error shows the command's own first error line (for `pass`, usually "… is not in the password store", meaning the entry name is wrong), and the panel offers to paste the value instead for this session. Fix the command in `config.json` for next time. `pass ls` lists your entry names.

### Keeping tokens on this PC

On by default (**Settings → Network → Tokens → Keep tokens on this PC**). A VM gets stand-ins for the Claude login and the GitHub token, never the real ones. When the agent talks to Anthropic or GitHub, cilly's egress proxy on your PC puts the real token in. So an agent can use them while it works, but it can't take them anywhere, and a stand-in it copies is worth nothing outside its VM.

How it works:

- **A certificate authority for each start.** At every start, cilly makes one with openssl. It may only vouch for the hosts that need a token (`api.anthropic.com`, `claude.ai` and `platform.claude.com` for the Claude login, `github.com` and `api.github.com` for GitHub, the agent's remote MCP servers whose key can stay here, and their subdomains), signs one certificate for each, and then its key is deleted, so nothing can make another one. The VM trusts it (the system's certificate store, and `NODE_EXTRA_CA_CERTS` for Claude Code). The certificates' keys stay in a private folder in memory on your PC, outside QEMU's sandbox, and go when the agent stops.
- **Only the token hosts are opened.** For those, the proxy checks the real site's certificate, answers the VM with this start's certificate, and reads one HTTP/1.1 request at a time, strictly. It puts the real token only in the login headers (`Authorization`, also inside git's login, and `x-api-key`) and drops any other header that holds a stand-in. It sends the request on and masks the real token in the reply, also where the site repeats the login back. It reads chunked replies before masking them, and refuses compressed ones, which it couldn't check. Every other connection stays a sealed tunnel, as before.
- **The GitHub token goes only with the agent's own repository:** git for that repository, the API for that repository, GraphQL queries, and reading `/user` and `/rate_limit`, which `gh` needs. Anything else goes out without it, so public repositories still work. GraphQL names the repository inside the request, so the proxy reads each GraphQL request first and lets the token go only with one that reads. A mutation, which could change a branch or another repository the token reaches, is refused with a message that says why. `gh pr view`, `gh pr checks` and the like work. cilly opens and updates the pull request itself, over the API for the agent's repository.
- **Agents push only to their own branches** (a second switch, on by default): a git push may change only the agent's own branches, and anything else, a tag included, is refused before any of it reaches GitHub. Its own branches are the ones named after its ID (`cilly/9a70b774-2`, under any prefix it had), and the ones it created. It may also create a new branch, named the way **Branch names** says. A create can never replace a branch that's there, since GitHub refuses it, and the proxy counts a branch as the agent's only once GitHub's answer says it created it. It keeps those in the agent's folder (`branches.jsonl`, written only by the proxy), so they stay the agent's at its next start. So an agent can't change your branches, even ones named like `fix/…`, or another agent's. One case is left: if an agent's branch is deleted after its merge and someone later makes a branch of the same name, that agent could still push to it. git shows why a push was refused, as `! [remote rejected] HEAD -> main (cilly lets this agent push only to its own branches and to a new branch named kind/name, …)`. The token also doesn't go with API calls that change branches, tags, releases or the repository's settings. Agents still read every branch, and still open and comment on pull requests. GraphQL isn't held this way, so to be sure `main` can't change, also keep it protected on GitHub.
- **openssl is needed.** The host check lists it, and `install.sh` installs it when it's missing. Without it, an agent doesn't start rather than getting the real tokens. Turn the switch off to give VMs the real tokens as before.
- **MCP servers' keys** stay on this PC the same way when a remote server (`https://`, on port 443) gets its key in its `Authorization` or `x-api-key` header, as a pasted `claude mcp add` command with a Bearer token or an API-key header does. The VM gets a stand-in, and the proxy puts the real key in for that server's host only. A key that a server running inside the VM needs (a stdio server, or one that takes the key in its address or in another header) still reaches the VM in the task's environment, because that server has to send it itself. So does a key shorter than 8 characters. A server you tick while the agent runs gets its real key until the agent starts again, since its certificate is made at the start. Adding a server and its key works as before.
- A change applies from an agent's next start. A running agent keeps what it started with.

### Claude login with a subscription or an API key

**With a Pro/Max subscription (the default):**

1. On your host, where you can open a browser, run:
   ```sh
   claude setup-token
   ```
   It prints a one-year token. It can only make model requests, not claude.ai chats or connectors.
2. Keep it out of plain files. Either store it with `pass insert cilly/claude` and use the `command` form shown above, or paste it into **Settings → Secrets → `agent:claude:CLAUDE_CODE_OAUTH_TOKEN`** each time you start the panel.
3. Agents use it as `CLAUDE_CODE_OAUTH_TOKEN`. With Keep tokens on this PC, the VM holds a stand-in, and the proxy puts the token in. The allowlist for Claude agents includes `api.anthropic.com`, plus `platform.claude.com` and `claude.ai` for subscription authentication.

Things to know about subscription tokens:

- **No per-token revocation.** Claude Code has no documented way to revoke one token on its own. The documented way to kill it is "sign out everywhere". If you suspect a token leaked, do that and generate a new one.
- **Shared usage limits.** All agents draw on your plan's limits. Anthropic's terms allow your own subscription in the unmodified Claude Code for your own work. Limits assume "ordinary, individual usage", so many agents running at once will hit them sooner.
- **For research agents, consider an API key instead.** A research agent browses the open web while it can use this token, and it's the agent most exposed to a page trying to misuse it. With Keep tokens on this PC it can't take the token itself. The research slot (`research:CLAUDE_CODE_OAUTH_TOKEN`) is separate, so you can either:
  - paste the same token (simplest), or
  - switch that slot to an API key with a low spend limit, which you can revoke on its own, by setting `"research": {"secrets": {"ANTHROPIC_API_KEY": {}}}` in the config.

**With an API key:** set `"agents": {"claude": {"secrets": {"ANTHROPIC_API_KEY": {}}}}` in the config. If both are set, the API key wins.

### How often you type a password

Secrets stay in the panel's memory until it stops, so you deal with them once per panel start at most. Pick one of:

| Option | Setup | Day to day |
|---|---|---|
| **Paste** | nothing | paste each token in Settings → Secrets after starting the panel |
| **`pass`** (GPG) | `pass insert cilly/claude` and `"command": ["pass", "show", "cilly/claude"]` | one pinentry prompt the first time a secret is needed. Use **Unlock now** in Settings to get it over with up front. |
| **Login keyring** (GNOME Keyring or KeePassXC with Secret Service) | `secret-tool store --label="cilly claude" service cilly key claude` and `"command": ["secret-tool", "lookup", "service", "cilly", "key", "claude"]` | no prompt, because the keyring is unlocked when you log in |

A token you paste or save in the panel replaces the old one right away. If you change it behind a command instead (a new entry in `pass`, say), press **Forget** on that secret, or restart the panel, so it runs the command again.

Secrets travel with each task over the root-only control port, whose socket sits in `$XDG_RUNTIME_DIR`. In the VM they exist only in that task's process environment, so a persistent agent doesn't keep them between tasks. They're never written to the image, the boot disk, the QEMU command line, logs or config. Tests check this.

## The web UI

The panel looks like a chat app, with agents in the sidebar and one agent at a time on the right.

**First run:** with no agents yet, the panel opens a short setup: host check (with the exact install commands), your Claude login and the bot's GitHub token (saved on this PC, encrypted, unless you switch that off), which repositories agents may use (it asks GitHub which ones the token reaches and warns if it has admin), network presets with a preview of exactly what gets added or removed, the permission mode, a CLAUDE.md (the recommended rules with their switches, your own, or none for now), and a first agent. Every step but the host check can be skipped. Whatever is left stays as a "Finish setting up" checklist on the New agent page until it's done (or you hide it).

- **New agent:** describe the task, pick Coding or Research, a repository (or none) and a lifecycle mode. Resources, stop after each task, the VM image, MCP servers, the PR title and a folder to inject are in the **Options** card beside it. **+ Add** there (and in a project's defaults, and in the message box's MCP popover) adds a server to the MCP library and turns it on, with the same dialog as on **Claude policy** (see [MCP servers](#mcp-servers)). Only the panel on your PC can add a server. A key that comes from a command, like `pass`, is still set in the config file.
- **Resources:** to change an agent's vCPUs and memory later, stop it and pick new values under **Resources** in the Sandbox tab's **This VM** card, then **Save**. While it runs, the card also says how much of your PC's memory the VM uses right now (read from `/proc`, QEMU and its helper processes together). The limits are the same as for a new agent (`vm.max_cpus` and `vm.max_memory_mb` in Settings), and the new values apply at its next start. Only the panel on your PC can change them. Inside the VM, the task's Claude Code run and everything it starts share a memory limit a little below the VM's (10% less, at least 512 MiB and at most 2 GiB). When they need more, Ghidra and jadx at once say, the kernel ends the biggest of them right away, inside that limit, and the agent sees the command end with Killed. Without the limit the whole VM would crawl with no memory left, the runner too, so the panel's file list and Stop the task wouldn't get an answer. The runner sets it up through systemd when the VM starts, after checking that systemd starts the run as exactly the agent user with no other groups, and otherwise runs it as before. The agent can't change the limit.
- **Limits:** three rows in the same card. **Bash command limit** is the longest one Bash command may run in the foreground inside the agent's Claude Code (its `BASH_MAX_TIMEOUT_MS`, 2 min to 4 h). Claude Code still gives a command 2 minutes unless it asks for more, up to this limit, so a stuck command doesn't hold the agent for the whole limit. Its own default is 10 minutes. A `BASH_MAX_TIMEOUT_MS` in the `env` of your Claude policy's managed settings wins over it. **Background work limit** is how long background commands and subagents may still run after Claude's last reply in a task (its `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`). Then Claude Code stops them, and its log says "Background tasks still running after the last turn; stopping them". Its own default is 10 minutes. **Until the task ends** waits for them, up to the task time limit, so a background server that never ends holds the agent until then. **Task time limit** is how long one task may run before it's stopped (5 min to 24 h). Its default is `vm.task_timeout_minutes` in Settings. The runner in the VM stops Claude Code at the limit. Should a task still run 10 minutes after it, the panel interrupts the task itself, and stops the VM if the task hasn't ended a minute later. Like Stop, that drops the messages still waiting for the agent: they say "Not sent: the time limit stopped the VM", and **Run again** in Tasks puts one back in the message box. Such a task says "Stopped: the time limit ended it", unless it still ends as done after the interrupt. Pick a value and **Save**: each applies from the next message you send, also while the agent runs (messages already waiting keep what they had). A paired phone can change them too. The rows show the limits the next task really gets: a saved value outside these ranges (from a hand-edited agent file) is ignored, and its row shows the default and says so.
- **Agent page:**
  - **Chat:** your messages, and Claude's replies, tool calls ("Ran ./gradlew build") and edit diffs as they happen. Claude runs with `--output-format stream-json`, and the VM turns those events into small, size-capped chat events that the panel checks again before storing them per task (`chat.jsonl` in the agent's folder). While a VM boots, a progress card shows each step. Once it's up, a "Started in 6 s" line marks the boot in the chat. Hover over it, or tap it on a touch screen, to see where the time went (QEMU and firmware, the boot loader, Linux and systemd, the runner, policy and setup, the self-test), measured by the panel's own clock. cilly's journal gets the same line. Enter sends. On a touch screen Enter adds a line, and only the send button sends. Your messages are shown as Markdown too, like Claude's replies. A message over 2,000 characters shows its start, with **Show more** for the rest. The agent always gets all of it. Attach files with **+**, by pasting or by dropping them. A message can have up to 50,000 characters. Pasted text that would go past that is attached as `pasted-text.txt` instead, since text copied out of a chat can carry whole commands and their output. **Start and send** boots a stopped agent and queues the message. A queued message waits in a card above the message box ("Queued. It runs after the current task."), and only shows in the chat once it runs. **Cancel** takes it out of the queue, and **Edit** takes it back into the message box to change it. The files you attached come back too when you edit on the page you sent from. **Send now** gives that message to Claude right away. When the agent can take messages while it works, the message goes into the running task: Claude reads it now, and its subagents and background commands keep running. The chat shows the message inside that task, followed by Claude's answer, and the Tasks tab lists it as delivered into that task. If Claude Code refuses or cancels the message instead of answering it, the chat says so in that task, and you can send it again. It needs a message without attached files, for the same conversation, model and effort, a VM started with this version of cilly, and a Claude Code in the image that offers this (2.1.300 does). Otherwise Send now stops the running task, background work included, and runs the message next, ahead of any others, in the same conversation. It asks before it does that, and it never stops a task without asking: when a message it didn't ask about can't go in after all (the run ended or stopped taking messages meanwhile), it waits first in line and a toast says so. Research agents always use this second way. The line under the message box only shows when it matters: where your voice goes while you record, or that attached files are copied in read-only at `/attachments`. The conversation chip turns teal when your next message doesn't continue the current conversation, and the model chip when it isn't the default.
  - **Stopping a task:** while a task runs and the message box is empty, the send button becomes a stop button (also **··· → Stop this task**, and from a paired phone). It ends only Claude's run. The task still finishes up: it saves the conversation, commits and pushes what's there and opens or updates the pull request, and the chat shows "Stopped by you". The VM stays on, even with Stop after each task, and your next message continues the conversation. If that conversation can't continue after being stopped, a new one starts and the chat says so. When the task is already committing and pushing, nothing is cut off. An agent started before this version can only be stopped with Stop. Restart it to get this.
  - **Model and effort:** the Model chip in the message box picks the model per agent (Default, Opus, Fable, Sonnet, Haiku, or a model id) and the effort, meaning how much it thinks (low, medium, high, xhigh, max). The runner adds `--model` and `--effort` from the next message on. Both are checked on both sides, so they can only ever be a model name and one of those five words.
  - **Details and subagents:** click any tool line ("Ran …") to see the whole command and its output (capped at 4000 and 2000 characters). When Claude starts subagents or background commands, each gets a card in the chat (type, task, tool count, tokens, time, and its own steps when you open it), and a strip above the message box names what is running right now. Open it to see what each one is doing and for how long, and click a row to jump to its card. This comes from Claude Code's own `task_started`/`task_progress`/`task_notification` events and the `parent_tool_use_id` on a subagent's messages, checked on the host like everything else.
  - **Reading back:** scroll up and a ↓ button appears to jump to the latest message. It says "New messages" when output arrived while you were reading.
  - **Progress:** while a task runs, the chat shows how long it has been working and when the last update came, a tool that is still running says so, and Claude Code's own notes (compacting the conversation, retrying the API) appear as status lines, so a long pause doesn't look stuck.
  - **Markdown:** Claude's replies and research findings are rendered as Markdown (headings, lists, tables, code blocks with Copy) by a small renderer that only builds text nodes, so nothing in agent text can become HTML. Code blocks and edit diffs get syntax highlighting from a small tokenizer (comments, strings, numbers, keywords) that only wraps text in fixed classes and scans in linear time, so hostile input can't stall the page. Links are clickable. A `https://github.com/` link opens right away. Any other `http` or `https` link first asks, showing the site's name and the whole address, since an agent could hide data in an address it wants you to open. A link's text can't pretend to go elsewhere, as the real site follows it in brackets. Other kinds of address, and ones with a name and @ before the host, stay text. Images are never loaded. Logs are colored the same careful way. Only ANSI color codes are honored, as a fixed set of classes. Every other escape or control character is shown as a mark such as ␛, just as the log file holds it, so reading a log with `cat` can't drive your terminal either.
  - **Tasks** lists what runs now, what runs next (in order, with **Move up** on your PC and **Remove**) and what ran, by day (times in UTC). Each finished task says Done, "Stopped (killed: time limit or out of memory)", "Stopped: the time limit ended it" (the panel ended a task the VM let run on), "Not sent: the time limit stopped the VM" or why it failed, shows a branch only when it isn't the agent's own, and has **Run again**, which puts its text in the message box.
  - **Files** (Export…, Attach repository…), **Logs** (agent, serial console, network decisions), **Sandbox**, **Research** (requests on the left; "Answered by", YOLO and, once YOLO is on, the round limit in a Settings card on the right. The round limit is `research.yolo_rounds`, so changing it there changes it for every agent, and only from your PC), or **Findings** on a research agent.
  - **Agent list:** under each name is a line the agent writes itself: it keeps one line in `~/status.txt` on what it's doing now, or what it finished and what's next. The panel reads it after each task and about every 30 seconds while one runs, keeps only the first line (up to 80 characters, control and invisible characters removed, secrets hidden) and shows it as plain text. Before the first one, or with an older VM image, the line is your last message without its Markdown, cleaned and cut the same way. Before the name is the panel's own state, never the agent's, as an icon: **Running**, **Waiting for you** (it has something in the inbox, also while it's stopped, since questions, notes and sites to allow still need you then), **Idle** (on, with nothing to do), **Starting**, **Stopping**, **Stopped** or **Failed**. Each state has its own icon as well as its color: a turning ring, an amber disc with !, a hollow ring, a dashed ring, a square and a red disc with ✕. On the right are only the words the icon can't say, so the name keeps its room: how many messages are queued ("2 queued"), when an idle agent stops ("Stops in 12 min"), the boot step ("Starting (3 of 7)") or a move to a new image. The full state is in the tooltip, and a screen reader reads it with the icon. A narrow window's header shortens its words too, such as "Running +3" and "Needs you". A project with no agents takes one line, with a **+** that starts a new agent in it.
  - **Agent header:** the state pill says what the agent waits on ("Waiting for you: a blocked site") and leads there: click it to open that item, the logs of a failed agent, or else Tasks. The line under the name shows the repository and branch. The agent's own summary is under its name in the sidebar. Click the repository and branch to copy the branch name. Click the name to rename the agent in place: Enter or clicking away saves, Esc keeps the old name. On a phone the state under the name does the same as the pill, and the line below it has the branch (tap to copy), the PR link and a Context tag when the conversation is getting full.
  - **Sidebar:** double-click an agent to rename it in place. Its **⋮** menu renames it, pins it to the top, moves it into a project, archives it (it must be stopped first, then it waits under "Archived" until you start it again) or deletes it. Right-click an agent to open the same menu. For the agent you have open, the same works from anywhere on the page, even while you type: **Alt+Shift+R** renames it, **Alt+Shift+P** pins or unpins it, **Alt+Shift+M** moves it to a project, **Alt+Shift+A** archives it and **Alt+Shift+D** deletes it (after asking). On a Mac it's **⌥⇧** and the letter. Its menu shows the keys.
  - **Quick switcher:** ⌘K (Ctrl+K off a Mac) opens a search over every agent (with its state), project and page, including each part of Settings. ↑ and ↓ move, Enter opens, Esc closes. The sidebar's search box has a small key hint that opens it. On a phone, tap the search icon at the top of the Agents page.
  - **Can't reach cilly:** when the panel stops answering, one bar says so, with **Retry**. It goes away by itself when the panel answers again. If the very first load fails, you see an empty page with the same bar.
  - **Phones:** one screen at a time. A bottom bar (Agents, Inbox, Pull requests, More) replaces the sidebar. Agents has the sidebar's groups (pinned, each project with its agents, other agents, archived). Tap an agent's ⋮ there, or long-press the agent, for its menu. A project shows its agents and suggested notes, then a list (Instructions, Notes, Files, Defaults, Web research, Allowed sites) where each row opens its own screen, with Save at the bottom. **Rename** is a link at the top, and Rename and Delete are also in its **···**. Inbox is a full page with the same safe one-tap actions as on the desktop (findings always open the review page), and More holds Claude policy, Settings and the host status. On a phone, Settings is a grouped list with a short summary on each row ("1 missing", "12 hosts", "8 vCPU, 16 GiB"). Each row opens its own screen, and the ones that need it have Save settings at the bottom. What you change on one screen is kept while you look at the others, until you save. A row says **Unsaved** while its screen has changes you haven't saved. On the PC, **Save settings** has an "Unsaved changes" note beside it and a dot on the tab. Popups (sandbox, ···, MCP, model, Start) open as bottom sheets. Pull one down by its top or tap outside it to close it. A short, slow pull lets it spring back. The sandbox sheet lists blocked hosts so you can allow one without leaving the chat. On an agent, Start/Stop, Model and New branch are in **···**. New agent opens as a full-screen form. Giving an agent a host folder needs the desktop (the folder browser doesn't fit a phone), but a folder it already remembers is still injected when you start it from the phone. Opening an agent hides the bar and gives the screen to the chat. The phone's Back button goes up a level: it closes a sheet, a dialog or the file viewer, or goes from a screen to the one above it (a Settings part to Settings, Settings to More, a project part to the project, a project or an agent to Agents, a review to Inbox). Narrow desktop windows get the sidebar as a drawer.
  - **Inbox** (sidebar): everything waiting on you, across agents: research questions to approve, blocked hosts to allow or deny, findings to forward, project notes to add and devices that ask to pair (on your PC only). Each has a review page with the full content and the agent's recent conversation. On purpose, the list itself only offers safe one-click actions. **Allow** there means this agent only (all agents is on the review page, with a confirmation), findings are always forwarded from the review page (they are untrusted web text), and a research question can be approved from the list only when it's short enough to read in full. On the review page you can edit the question before it leaves the sandbox. Denied hosts come back only if the agent tries them again, and show as a red line in its chat.
  - The **context** meter in the header (on a phone, in **···**) shows how full the agent's current conversation is, for example "Context 62%", and "124k of 200k tokens" when you hover over it. The bar turns amber from 70% and red from 90%. Clicking it shows what's left and **Start a new chat**, which makes your next message start a new conversation. The VM reports the numbers as Claude Code works, at most every 5 seconds, and only for the main conversation, not its subagents. A new conversation starts at zero, and going back to an earlier one brings its count back. An agent whose VM was started before this version shows no meter until its next start.
  - The **sandbox** badge in the header shows whether the last boot's self-test passed and a red count of blocked requests. Clicking it opens a short summary (self-test, network, blocked, secrets, lifecycle). Any row takes you to the **Sandbox** tab, which has the details. The main column holds blocked requests (edit a host into a wildcard, then allow it for this agent or everywhere), the self-test (collapsed to "All N passed" unless a check failed), this agent's own instructions, its extra hosts (add one right there) and its MCP servers. The **This VM** card beside it shows the lifecycle, disk, resources, the Bash command and task time limits, VM image, host folder, the secret names it gets and the policy version. On a phone the card comes after the rest. **···** holds what the agent does when it has nothing to do (stop when idle, or keep running), Clear memory, Move to new image, Reset to clean and Delete. Clear memory, Move to new image and Reset to clean are greyed out with the reason when they don't apply.
  - **Images in the chat:** images you attach show as thumbnails in your message, and images the agent puts in `~/outbox` during a task show right after the step that made them (a command or file write that names the image), or at the end of its reply. Click one to open the image viewer: a large picture over the page, which stays visible behind it, blurred. The arrows (or ← and → on a keyboard, or a swipe) go to the next and previous picture of the chat, or of the folder when you open one from **Files**. Click the picture to zoom in where you clicked, drag it to move, click again to zoom out. **Esc**, ✕ or a click outside closes it. They load from the VM through the same checked viewer as **Files in the VM**, so they show while the agent is running.
  - **Findings and requests** that were forwarded or dismissed move to an **Archive** (on a research agent) or **Done** (on a coding agent's Research tab), so the main list only shows what still needs you. To put findings away without forwarding them, press **Archive** on the research agent's Findings tab, or **Dismiss** where they came back on the coding agent's Research tab, next to **Review** and **Forward**. Either one moves both the findings and the question.
  - **Long lists** show one page at a time with a "Show more" button. The chat shows the newest 20 messages (older ones on request), and the task list, inbox, research requests, findings, pull requests, blocked hosts and folder listings work the same way.
  - **Badges** only count things waiting on you: the Inbox, an agent's blocked requests and its research requests.
- **Claude policy:** CLAUDE.md, managed settings and permission mode, applied live.
- **Pull requests:** open PRs from your agents' branches, with **Fetch build** into a folder you choose.
- **Settings**, in tabs:
  - **Secrets:** each secret's name, who uses it, how it's stored (pasted, saved on this PC or from a command) and when it was set, never any part of its value. Missing ones come first, then locked ones (a command not read yet). **Unlock now** reads every command at once.
  - **GitHub access:** per token, the repositories GitHub says it reaches, as a checklist. Ticked ones are the ones agents get the token for. Read-only ones can't be ticked. A repository that's ticked but missing from GitHub's list (only the first 300 are fetched) stays ticked and says so, until you untick it. The permission comes from the bot account's role on the repository, so for a fine-grained token it doesn't prove the token itself was given `Contents` write access. **Commits by** sets the name and email in agents' commits: the bot's handle and its noreply address, so GitHub shows the commits as the bot's and your own address never appears. Setup fills them in from the bot's token, and **Fill in from the bot's token** asks GitHub again. Until they're set, agents commit under a made-up name that GitHub links to no account, and the tab says so. A change applies from each agent's next start.
  - **Network:** the allowlist, and **Never reach**, with the built-in blocks listed.
  - **VM images:** each image with its size and the agents that use it. **Make default** sets the coding default. The research default changes with **Use for research** or a new slim build. **Rebuild** builds a fresh image from the same preset, or queues it while another build runs. **Delete** removes an image file, but never a default, never during a build, and never while an agent runs on it or keeps a persistent disk built on it (reset that agent first). Agents that picked it go back to the default.
  - **VM limits** (with the YOLO round limit), **Speech to text**, **Devices** (paired devices, and notifications for this browser) and **Advanced** (the whole config as JSON). The config file (`~/.config/cilly/config.json`) keeps only what you changed, so a setting you never touched follows cilly's default when a new version changes it. `cilly show-config` prints all of it.
  - **Appearance:** the themes, as small previews. Click one and it becomes the standard theme: this PC and your paired devices all show it within a few seconds. The panel has three: **Dusk**, the standard until you pick another, **Dystopian** and **Classic**, the earlier look. Dusk has Dystopian's colors with plain type that's easier on the eyes. It uses the system's own font at 110%, with Geist Mono for headings and code. Dystopian is the landing page's look (cold blue-black, one lavender accent, Pixelify Sans with Silkscreen headings, and plain Geist for chat replies so long ones stay easy to read). Dystopian uses the Terminal style: square corners, paper-colored main buttons, cards with a double rule, pixel icons and pixel cursors (drawn from 32 px files on sharp screens), and the filled warning triangle wherever something needs a look. In every theme, dropdowns open in the panel's own colors in Chrome and Edge 135 or newer with a mouse. Other browsers and phones show the system's own list. **Customize** on one of the panel's own themes opens an editor for a copy, and **Edit** opens one you saved. The editor sets the name, the style (Classic or Terminal), the font, the heading font, the chat font and the code font, a text size for the font, the chat and the code (75% to 150%), and every color, grouped (Accent, Backgrounds, Text, Lines, Status, Code), each with a color picker and its hex value. The whole panel shows your changes as you make them, also when you go to another page to see how it looks there. A bar at the bottom then says the theme isn't saved, with **Back to editor** and **Discard**. A reload drops an unsaved draft. **Save and use** saves the theme and makes it the standard, and **Cancel** goes back to the theme in use. Your themes are kept in `config.json` under `appearance`, with only the colors they change. The panel takes nothing but `#rrggbb` colors for the tokens in `style.css`, and fonts it knows. **Add font** takes a WOFF2, WOFF, TTF or OTF file of up to 4 MB (20 at most), checked by its first bytes, and keeps it in the data folder under `fonts/`, so a font whose license doesn't allow sharing never has to be in cilly itself. Paired devices load it from the panel. Removing a font puts Geist back in the themes that used it. To use NeueBit and Mondwest, which the Dystopian design was drawn with, add them yourself and pick them in a copy of Dystopian: their license doesn't allow shipping them with cilly. Only the panel on your PC can change any of this. This browser remembers the last theme it showed, so a reload doesn't flash Classic first. The panel bundles Geist, Geist Mono, the Geist Pixel styles (Square, Grid, Line, Triangle, Circle), Pixelify Sans, Silkscreen and Departure Mono, all under the SIL Open Font License.
  - **This PC:** the host check from setup, with the command for each missing piece on your distribution, and **Start when you log in**. That switch writes a systemd user service for cilly, as you, and enables it for your next login. Turning it off disables it, and deletes the service file if cilly wrote it. A service file you wrote yourself is never changed. Open the panel with `cilly open` (or `python3 -m cilly open` from the cilly folder), which reads its link from a file in your runtime folder that only you can read. The service file names the checkout when cilly runs from one, and otherwise the Python of its pipx venv with your home as its folder, so a newer Python doesn't break it. A paired phone can't use this switch.
  - Anything that makes the *host* run a program (secret commands, `vm.qemu_binary`), and `vm.use_bwrap`, which decides whether QEMU runs inside bubblewrap, can only be changed by editing the config file, not from the web UI. The web UI can never turn the sandbox around QEMU off.

## Using cilly from your phone

The panel only ever listens on 127.0.0.1. To reach it from your phone, put an HTTPS reverse proxy **on the same machine** in front of it (the same way you'd serve Home Assistant at `ha.home`), give it a local DNS name, and tell the panel that name in `config.json` (it can't be set from the web UI):

```json
"remote_hosts": ["cilly.home"]
```

Caddy on the machine that runs the panel, for example (`tls internal` uses Caddy's own local CA):

```
{
	auto_https disable_redirects
}

cilly.home {
	tls internal
	reverse_proxy 127.0.0.1:8765
}
```

Every device that opens it must trust Caddy's root certificate, `/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt` with Arch's caddy package. Android: Settings → Security → Encryption & credentials → Install a certificate → CA certificate. iPhone: open the file, install the profile, then turn it on under Settings → General → About → Certificate Trust Settings. Arch: `sudo trust anchor --store root.crt`. If you already run Caddy with `tls internal` elsewhere (a Raspberry Pi serving Home Assistant, say) and your devices trust that CA, you can copy that Caddy's `pki/authorities/local/` folder to this machine's Caddy before its first start, so both sign with the same CA and nothing new needs installing. The catch is that the CA's private key then lives on two machines.

Caddy sends `X-Forwarded-Proto: https` by itself. With nginx, add `proxy_set_header X-Forwarded-Proto $scheme;`. The panel refuses the name on any request that didn't come in over HTTPS, so the access token never crosses your Wi-Fi in clear text, and HTTPS is also what lets the phone use the microphone. Anyone on your network can reach the login page, but nothing works without the token. Firewall port 443 to your phone if you want less. Keep the proxy on this machine, because if it ran elsewhere, the hop to the panel would be plain HTTP on your LAN.

**Pair the phone** (like adding a second device to a messenger): open `https://cilly.home` on it, then **Add to Home Screen**. The panel shows **Pair this device**. Tap it and the phone shows a 6-digit code. The request shows up in the inbox on your PC (and a notification, if you turned them on). Open it with **Review**: the review page shows the whole code, and it's the only place with **Pair**. Pair it only if the codes match. The phone then has its own token and stays signed in, also after the panel restarts, until you revoke it under **Settings → Devices** on the PC (or tap **Sign out this device** on the phone). Requests expire after 5 minutes, and at most 3 can wait at once.

The panel stores only a SHA-256 of each device token (`~/.config/cilly/devices.json`, mode 0600). A paired device gets a smaller role than the startup link. It can use agents (chat, start and stop, create, model, MCP, the Bash command and task time limits, a research agent with YOLO on or off), open and download the files in an agent's VM (Files), answer the inbox, allow a site **for one agent** on the ports in `egress.ports` (a site with any other port can only be allowed from the panel on your PC), and see pull requests. It can't browse or inject host folders, export, download builds, delete, reset or clear an agent, change a project (an agent it creates in a project or moves into one follows the project's research agent and YOLO setting, as on the PC), allow a site for all agents, approve another device, or open Settings, Claude policy, secrets or the paired-devices list. The server refuses those calls. It isn't just the buttons being hidden. The startup link (`/#t=…`, new at every start) still works everywhere and keeps the full role. "Or use the startup link" on the sign-in page takes it. Opened on the PC itself (127.0.0.1 or localhost), the sign-in page leads with the startup link instead, with the command that opens it (`cilly open`, or `python3 -m cilly open` from the cilly folder), and pairing under it. cilly never prints the link or writes it to the journal. It keeps it in a file in your runtime folder that only you can read, and `cilly open --print` prints it when you need to paste it.

**If you lose a paired phone:** revoke it under Settings → Devices. It's signed out at once. An agent can show the keys it holds, so to be safe also sign out your Claude login (claude.ai → Settings) and rotate the bot's GitHub token.

**Notifications:** turn them on in that same Settings section, per browser. You get one when a task finishes (it says whether the agent is ready or stopped, never what you wrote) and when an agent needs you (a blocked site, a research question, findings), but not for the agent you're looking at. They come from the open panel page through a small service worker (`sw.js`, with no caching, no push service and no outside calls), so they arrive while the panel is open somewhere, even in a background tab. Nothing arrives when every panel tab is closed. Browsers only allow them on HTTPS or localhost.

## Injecting a host folder

1. Pick a folder. The panel scans it **without following symlinks** and shows:
   - the file list and total size
   - warnings: `.env`, keys, tokens found in file contents, `.git` (credentials in `.git/config`), hardlinked files, symlinks that point outside the folder (excluded).
2. If there are warnings, you must tick "I reviewed the warnings".
3. **Copy (default):** the approved files are packed into a tar on a read-only virtual disk. They appear at `/inject`, read-only and root-owned, and the agent user can't write there. If any file changes between the scan and launch, the task aborts. The VM never opens your original folder. It's a snapshot from when the agent starts. To hand over something new mid-conversation, attach it to a message instead.
   - **Every start:** the agent remembers the folder and gets it again whenever it starts (after a panel restart too). It's scanned again each time, so new files are included, and new warnings you haven't approved stop the start until you review them. **Start → Stop injecting it**, or **Remove** in the Sandbox tab's **This VM** card, forgets it. Picking another folder replaces it. The card's **Add a folder…**, **Change** and **Rescan** run the same scan and warnings check and save the folder without starting the agent. It goes in at the next start (a running agent says so). Research agents never get host folders, and a paired phone can't add one (the server refuses).
   - **A working copy in `~/scratch`** (agents without a repository, on by default there): at boot the files are also copied into `~/scratch`, by a helper running as the agent user, so it can edit them in place. Take the results out with Export. Files already in `~/scratch` are never overwritten, so a persistent agent's earlier work is safe when it starts again.
4. **Live share (opt-in, experimental):** `virtiofsd --readonly --sandbox=namespace` run as you. The guest sees later changes. Read-only is enforced on the host side.
5. Refused outright: `/`, `/home`, your home directory, any parent of it, and `~/.ssh`, `~/.gnupg`, `~/.config`, `~/.local`, `~/.aws`, `~/.claude` and similar.
