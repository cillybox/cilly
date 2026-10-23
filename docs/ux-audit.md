# Cilly panel UX audit (Phase 1, no code changed)

Screenshots are in `~/outbox/ux-audit/` (name-d = 1280x800 desktop, name-m = 390x844 phone at 2x).
Walked against `tools/harness.py --seed` and `--fresh`, with local Playwright scripts (kept in `~/scratch/audit`).

## Read this first: what could not be done

- **Mobbin references:** the Mobbin MCP could not authenticate in this session, so I used a logged-in Mobbin web session you provided and ran 15 Deep Search queries (Web and iOS). Items cite the app and the Mobbin screen link. Screen links need a Mobbin login to open. Where a search returned nothing useful (hover and focus states, persistent save bars, allow-once prompts, run headers) the item says so. Nothing from Mobbin is copied into the repo.
- **Playwright and Chrome DevTools MCP:** the Playwright MCP also failed to connect, and DevTools MCP is not installed. I used the Playwright library in Node instead, with `page.on("console")` and timing. Equivalent for console errors and interaction timing, not for the DevTools performance panel.
- **/impeccable critique and audit:** run inline as a heuristic review plus `impeccable detect`, not as the two isolated sub-agents per area the command asks for. Treat the scoring as degraded.
- **States not captured:** loading (the fake VM answers in under 110 ms, nothing to see) and the first-run Repositories step with real GitHub data. Error was seen on Settings > Local models only.
- **Setup is hard to judge on phones:** a fresh harness on a 390px viewport opens New agent, not the wizard (item 8).

## What is already good

- No console errors or warnings on any screen walked. Zero requests leave 127.0.0.1 (the no-internet constraint holds).
- Interactions are fast: open agent 104 ms, tab switches 34 to 54 ms, Settings 88 ms.
- Focus is visible on every interactive element probed (2px light ring), and the composer has a focus state.
- The phone Settings list (`a-settings-m`) and phone Inbox (`a-inbox-m`) are the best screens in the app: grouped, summarised, one primary action per card.
- Tasks tab (`b-coder-tasks-d`) is clear: date groups, one status line, one action.

## Findings, ranked by impact

### 1. One question to the user lives in five places, with different actions (confusing navigation)
- Screens: `a-inbox-d` / `probe-inbox-d`, `b-agent-coder-chat-d`, `a-inbox-m`.
- Seen: a pending research request shows as the header pill ("Waiting for you..."), an inline card ("pending"), the strip above the composer (Deny / Approve), the Sandbox tab badge, and the Inbox. In the desktop Inbox popover a Research request has `Review` (link) and `Deny` and no primary button, a Questions card has a right-aligned primary `Answer`, and a Project note has `Deny` + primary `Add note`. The phone Inbox page gives every card Deny + one filled primary. The popover also covers the agent's tab row, and its `Close` sits over the page header.
- Mobbin (Deep Search "notifications inbox with approve and deny buttons on each item", Web and iOS):
  - Remote, inbox as a popover panel of notification rows with a per-row menu (Mark as read, Dismiss): https://mobbin.com/screens/5e916e4a-4c86-4c06-a455-9a8d1785603d. Shows our popover is a known pattern, but theirs sits beside the content and has one action per row.
  - Workable, inbox as a page of requests with the action on the row's right ("Go to reviews", "Sign document"): https://mobbin.com/screens/7394bbf7-fef9-4066-89df-881515abc89c.
  - Peec AI, rows with compact approve and dismiss icons on the right: https://mobbin.com/screens/a33f472c-a45f-4a1d-bcab-6aa85d66c70e.
  - X (iOS), "Member requests" card with `Approve` and `Deny` side by side, one filled and one outlined: https://mobbin.com/screens/3f9bfe33-65e6-45cf-8c05-ffb1d185c2ba.
- Fix: (a) one card layout for desktop and phone: type tag, agent, time, one-line summary, `Deny` (ghost) left and one filled primary right; `Review` becomes the card title link. (b) Make the desktop Inbox a page, or a right-hand panel that does not cover the tabs, so Esc and `Close` are not needed. (c) Pick one in-chat surface (the strip) and make the inline card and header pill read-only history and a jump link.

### 2. Settings has two different structures, and Claude policy is in three places (unclear tab structure)
- Screens: `a-settings-d`, `a-set-more-menu-d`, `a-settings-m`, `a-more-m`, `a-policy-d`.
- Seen: desktop is a 10-item tab strip in three groups split by unlabelled hairlines, with Appearance and Advanced hidden under `More`. Claude policy is a separate sidebar link with its own four tabs. On the phone, Claude policy is listed both under More and inside Settings, and groups have no names. Tab order differs between platforms (Local models before Network on desktop, after it on phone).
- Mobbin (Deep Search "settings page with left sidebar navigation grouped into labelled sections", Web; "settings list with grouped rows and the current value on the right", iOS):
  - Google Workspace Admin, left rail with collapsible labelled groups: https://mobbin.com/screens/c12b8d7e-8562-4003-9936-95c49f36591d.
  - Quicken, settings with a left list of sections (General, Accounts, Categories...): https://mobbin.com/screens/d92b8b4d-1174-41a5-97d8-fb8d026df6d4.
  - Laravel Cloud, left settings nav with a content card: https://mobbin.com/screens/ec08f97b-89fa-4bfa-a6b1-100c4bb488ed.
  - Binance (iOS), grouped rows with the current value on the right and small group headings: https://mobbin.com/screens/2110f6a5-e7d0-4fd8-a7d0-f081913dfaac. Copilot (iOS), same pattern with on/off values: https://mobbin.com/screens/59bb9a40-9c88-47fb-a603-af80be44600c. Our phone Settings list already matches this.
- Fix: on desktop use a left settings rail with labelled groups (Accounts, Network and VMs, This PC, Panel) and drop `More`. Put Claude policy in the rail under its own group and keep the sidebar link as a shortcut. Use one order everywhere. Remove the duplicate row from the phone More page.

### 3. Agent header: two "Stop"s, a destructive item beside them, and a stray "?" (buried and misplaced actions)
- Screens: `b-coder-more-actions-d`, `b-coder-more-actions-m`, `b-agent-coder-chat-d`.
- Seen: `Stop` (stops the VM) sits next to the ⋯ menu whose first row is `Stop this task`. The phone sheet lists both. `Delete agent` is in the same list as Rename, one row under a disabled `Reset to clean` (it is already divided off and opens a confirm dialog, which Phase 2 keeps). Disabled rows say why in a grey subtitle (good) but are mixed in with live ones. The header line reads `me/plugin-lib · ?` for an agent with no branch yet.
- Mobbin (Deep Search "more actions overflow menu with delete item separated at the bottom in red", Web; plus Quicken from the settings search):
  - Quicken, row overflow menu with `Delete account` in red at the bottom, set apart from Edit and Mark as closed: https://mobbin.com/screens/d92b8b4d-1174-41a5-97d8-fb8d026df6d4.
  - Clerk, "Delete application" asks you to type the app's name to confirm, with a warning line: https://mobbin.com/screens/71516c8f-ca28-4b68-bca8-01b6ea6e46ec.
  - Toggl Track, delete dialog with a warning box, a safer alternative (`Discontinue`) above the red `Delete`, and `Cancel` last: https://mobbin.com/screens/e62d32e0-7a4e-409f-a1f1-9acd8242dc1a.
  - Todoist (iOS), long-press menu with `Delete Task` in red under a divider: https://mobbin.com/screens/44f33505-7150-46ba-9113-f8442bea3d87.
  - Run headers: Deep Search "run or job details header with status badge and stop or cancel button" returned only close/delete dialogs (Remote, Workable, Deel), so no clean reference for a Stop VM / Stop task pair. This item rests on the menu patterns above.
  - Agent list sidebar (no change needed): Grok, ChatGPT and Lemni show a Projects group above a history list, close to ours (search "sidebar listing AI agents or chats with status indicators grouped by project").
- Fix: rename to `Stop VM` and `Stop task`, place both together; keep the VM button as is and move `Stop this task` next to the running task (chat strip and Tasks row). Separate Delete with a divider and a confirm (it already has the divider; add a typed or two-step confirm). Hide the `?` when there is no value.

### 4. Hover and focus feedback is inconsistent (hover/focus)
- Screen: `states-sheet-d` (rest, hover, focus for nine controls), `s-composer-focus-full`.
- Seen: The model chip and Isolated badge showed no hover change, and the Stop button and state pill only a barely visible one (surface to surface-2, and a 15% brightness step). Sidebar rows get a fill, tabs only change text colour. Focus is a white 2px ring that follows the shape (pill, rounded rectangle), but on tabs it is a hard square, and the composer and text inputs use a different language (1px lavender border). Disabled Send is nearly invisible against the composer.
- Mobbin: not applicable. Static screens do not show hover or focus states, so no reference was searched for this item.
- Fix: one hover rule for every pill-shaped control (`--surface-2` fill, same 120 ms), one focus style (chosen by the user, see `focus-options.png`, option C): no outline, a one-step darkening overlay plus a 1px inset ring in `--accent-line`, with the radius from the element, used for tabs, inputs and the composer too. Raise the disabled Send contrast to the `.btn:disabled` colours.

### 5. Save model is inconsistent and sometimes wrong (extra clicks, trust)
- Screens: `a-set-local-models-d`, `a-set-network-d`, `b-project-d`, `a-policy-m`.
- Seen: Local models says "Changes here are saved right away" while a sticky `Save settings` bar sits under it. Network says "Running agents pick up changes once you save". Project has a `Save` per textarea (Instructions, Allowed sites). Claude policy has `Save and apply live` with an `Up to date (v1)` state (good, the best of the four). No page shows whether there are unsaved edits.
- Mobbin (Deep Search "unsaved changes bar with discard and save buttons", Web): no persistent save bar came back, only unsaved-changes dialogs.
  - Salesforce, "Save changes to this segment?" with three clear choices, `Cancel`, `Discard`, `Save`: https://mobbin.com/screens/ff8c986b-e12e-4bee-8b31-53740c045b5f.
  - Squarespace, "Exit without saving?" dialog: https://mobbin.com/screens/9825b5a3-da36-4ae9-8a15-7c9d78f08f1b.
  - The Discard and Save pairing is supported. The "bar appears only when something changed" idea is from general practice, not from a Mobbin screen I saw.
- Fix: show the bar only when something changed ("2 unsaved changes", `Discard`, `Save`); hide it on auto-saving tabs; use the Claude policy wording everywhere; make the project textareas share the same bar.

### 6. Sandbox tab holds unrelated jobs, and the chat's Allow button hides its scope (buried actions)
- Screens: `b-coder-sandbox-d`, `b-agent-coder-chat-d`.
- Seen: the tab stacks blocked requests, self-test, per-agent instructions, extra hosts, the global allowlist, and a VM details column. "Instructions for this agent" is not a sandbox setting. The Blocked request cards offer `Allow for coder` and `Everywhere...`, but the chat strip shows only `Allow sync.example.net:8080` and the scope is not stated.
- Mobbin (Deep Search "permission prompt asking to allow once, always, or for this project", Web): no "allow once / always" prompts came back.
  - Dropbox Dash, access request that lets you choose which pages are shared, with `Back` and a filled `Allow access`: https://mobbin.com/screens/1572b338-ade3-49ec-98e4-ec6ccc5a858c. Closest match for stating scope in the prompt.
  - Databricks, a grant dialog with explicit privileges: https://mobbin.com/screens/fb478ec9-87eb-4e56-afce-b47b2c3c29a1.
- Fix: move per-agent instructions to the Chat tab's composer options or the agent menu; keep Sandbox for network and VM only, with Blocked requests first (already). Label the strip button `Allow for coder` with an `Everywhere...` overflow, the same as the tab.

### 7. Phone tab strips clip with no cue (mobile)
- Screens: `b-agent-coder-chat-m` (Research cut off), `a-policy-m` (`MC` cut off), `b-coder-sandbox-m`.
- Seen: the agent tab strip and the Claude policy tab strip scroll sideways, but the last tab is cut mid-word with no fade or arrow. The agent header uses about 145 of 844 px (title, status, branch, PR, tabs), leaving the chat short.
- Mobbin (Deep Search "horizontally scrolling tab bar under the header with the last tab cut off" and "AI chat screen with compact header showing title and status", iOS):
  - Formula 1 (iOS), tab row that continues past the screen edge, so the cut-off last tab is the scroll cue: https://mobbin.com/screens/ca607a19-9932-441a-99f9-1ffd89086b31. Zomato shows the same on both edges.
  - Lloyds Mobile Banking (iOS), chat header is one line, a back arrow, a short title and a close icon, with the status in a small line under it: https://mobbin.com/screens/0b513db0-3b95-44c1-aa50-6ebd386b1c8a. Lyft Support (iOS) puts "Powered by AI" under the title the same way.
  - So a cut-off tab is an accepted cue on phones, but our strip clips mid-word and the Claude policy one has no active-tab scrolling. Fix stays: scroll the active tab into view and shrink the header.
- Fix: edge fade on the strip, scroll the active tab into view, fold branch and PR into one line, shorten `Needs you` row. For Claude policy use the phone Settings-style list instead of tabs.

### 8. Tablet widths and first run on a phone (mobile)
- Screens: `t-700-agent`, `setup-1-m`.
- Seen: between 641 and 767 px there is no sidebar and no bottom bar, only a small sidebar icon with a dot at top left (the sidebar collapses at 767, the phone layout starts at 640). On a fresh install a 390 px phone lands on New agent; the six-step setup is not shown.
- Mobbin (Deep Search "onboarding setup checklist card with progress bar and steps to finish", Web): HoneyBook ("Let's start step-by-step", 6/7 completed bar, each step a row with a chevron), Remote ("2 tasks remaining", 67% complete, finished rows ticked green), Klaviyo ("Let's get started", 1/4 guides completed, steps with a `Manage` button on the right), Apollo (setup steps with `Start` or `Completed` per row). Search URL: https://mobbin.com/search/apps/web?content_type=screens&q=onboarding+setup+checklist+card+with+progress+bar+and+steps+to+finish&mode=deep
  - Tablet breakpoint: not applicable, no Mobbin reference.
- Fix: switch to the bottom bar at 767 so one breakpoint covers both. On phones, show the Finish setting up checklist at the top of Agents until done, linking to each step (host steps say "do this on the PC").

### 9. New agent: the key choice is small and the checklist pushes the form down (weak hierarchy)
- Screens: `b-newagent-d`, `b-newagent-m` / `b-newagent-research-d`.
- Seen: `Coding / Research` is a tiny toggle inside the composer, next to repository and name. The "Finish setting up" card sits between the prompt and Project and Lifecycle, so Lifecycle starts below the fold at 800 px. The right-hand Virtual machine column is always open.
- Mobbin (Deep Search "create new project page with a segmented type switch above the form", Web):
  - GitLab, "Create or import your first project" with `Create | Import` tabs at the top of the form: no screen link logged; open GitLab from the search: https://mobbin.com/search/apps/web?content_type=screens&q=create+new+project+page+with+a+segmented+type+switch+above+the+form&mode=deep
  - Codecademy, choice of a theme as a grid of buttons above the primary action. Todoist, add-project modal with name, colour and workspace.
  - The type switch at the top of the form is supported by GitLab. Onboarding checklists (item 8 references) are kept compact and on their own card, which backs collapsing ours.
- Fix: move the type switch above the prompt as a segmented control (as on the phone, which is already better), collapse the checklist to a one-line bar, and default the VM column to a summary line with `Customise`.

### 10. Files viewer on phone truncates its own metadata (viewer)
- Screens: `c-files-open-m`, `c-files-open-d`, `c-files-image-d`.
- Seen: the phone header cuts `Text file, 56 B, 4 l...` while `Copy` and `Download` take the width as bare text. The viewer fills the whole screen with a lighter background than the app. Desktop is good: split list and preview, warning for the hidden direction character is clear. Images open the lightbox directly with prev/next, fine.
- Mobbin (Deep Search "file preview screen with file name, size, and share or download icons", iOS):
  - Microsoft Outlook (iOS), title with the type and size on a line below ("PDF · 2 KB") and a single overflow icon in the header: https://mobbin.com/screens/bb94440b-d97e-418a-9539-30a6af7bb08f.
  - Linktree (iOS), file name with "PDF · 663.43 KB" on its own line under it, and a download button: https://mobbin.com/screens/1e66ab17-7d4a-41ff-a52e-f6f8003aac39.
  - Google Drive (iOS), full-screen preview with a title in the header and the actions in a bottom sheet.
- Fix: put the metadata under the title on its own line, turn `Copy` and `Download` into icon buttons with labels for screen readers.

### 11. Small visual drift (low)
- Phone bottom-bar icons are inline SVG drawn in `mobile.js`, not the bundled Phosphor set the rest of the app uses (the constraint says Phosphor only).
- `style.css` uses radii outside the documented scale (3, 4, 16, 18, 20, 22 px) and one thick left border on `.image-card` (`style.css:1339`) which the detector flags as a "side-tab" tell. Settings pages use a narrow 730 px column that leaves large dead areas on a 1280 px screen (`a-set-network-d`).
- Fix: swap the nav icons for Phosphor masks, add the missing radii to DESIGN.md or snap them, drop the card side border for a tag, let the Settings column grow to about 880 px.

## Proposed Phase 2 order (one commit per area)

1. Inbox and in-chat asks (item 1)
2. Agent header and menu (items 3, 6)
3. Hover, focus, disabled tokens (item 4)
4. Settings structure and save model (items 2, 5)
5. Phone and tablet (items 7, 8, 10)
6. New agent (item 9)
7. Visual drift (item 11)

Nothing is committed beyond the initial import. `DESIGN.md` and this file are uncommitted on branch `ux-audit`.
