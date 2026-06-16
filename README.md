# bayou desktop

A private, local AI assistant for Apple Silicon — a full chat app (think
ChatGPT/Claude) whose brain is an **open-source model running on your own Mac**
through the [bayou](../bayou-oss) agent harness. No accounts, no metered tokens,
nothing leaves the machine. It's a real **tool-using agent**: it searches the
web, reads your files, and runs commands (with your approval).

## Architecture: hosted UI + local helper

```
  ┌─────────────┐   ws://127.0.0.1:8780   ┌──────────────────────────────┐
  │  bayou UI   │ ───────────────────────▶│  bayou helper (local daemon) │
  │ (a website) │                          │  MLX model · tools · memory  │
  └─────────────┘                          └──────────────────────────────┘
   served from a host                       runs on YOUR Mac (LaunchAgent)
   → push updates freely                    → model + chats stay local
```

- **The UI is a website.** Host `ui/index.html` anywhere static; reloading picks
  up updates. It connects to the local helper over `ws://localhost:8780`
  (localhost is a secure context, and the helper sends the Private-Network-Access
  header, so an HTTPS site can reach it).
- **The helper is a small local service** (`backend/server.py`) that holds the
  MLX model, runs the agent loop + tools, and stores conversations on disk in
  `~/.bayou_desktop/`. It auto-starts on login via a macOS LaunchAgent.
- **First run:** with no model installed the UI shows a download screen; pick a
  small open-source model (Qwen2.5 / Llama 3.2) and it pulls a ready-to-run MLX
  build, then loads it. One time; cached locally forever.

## Install (the helper)

```bash
cd bayou-desktop
./helper/install.sh        # sets up a venv, installs the engine, registers the LaunchAgent
```

It now runs in the background on `ws://127.0.0.1:8780` and restarts on login.
Open the UI and it connects automatically.

```bash
open ui/index.html         # quick: open the file directly
./web/serve.sh             # or serve over a real origin → http://localhost:8788
./helper/uninstall.sh      # stop + unregister (keeps chats/models in ~/.bayou_desktop)
```

**Hosting the UI for real:** deploy `ui/` to any static host (it's a single file),
then start the helper with `BAYOU_ALLOWED_ORIGINS=https://your.site` so it accepts
that origin. The UI auto-pairs (fetches the token from the helper's `/pair`).

Dev loop (no install): `BAYOU_OSS=~/projects/bayou-oss python backend/server.py`
then `open ui/index.html`. Env knobs: `BAYOU_MODEL` / `BAYOU_EXPERTS` (explicit
model dir), `BAYOU_PORT`, `BAYOU_FORCE=1` (bypass the memory guard), `BAYOU_MOCK=1`.

## Features

- **Full chat app** — conversation sidebar with saved history (persisted to
  `~/.bayou_desktop/chats/`), new chat, rename/delete, auto-titles.
- **Real tool-using agent** — web_search / web_fetch / read / grep / glob / ls run
  automatically; **bash / write / edit are gated** behind an approve-deny card.
- **Message actions** — copy, regenerate, edit-and-resend; syntax-highlighted code
  blocks with copy buttons; markdown + tables; scroll-to-bottom.
- **Attachments** — drag/drop text & code files (inlined) or **images** (OCR'd
  on-device via macOS Vision, since the model is text-only).
- **Model swap** — picker dropdown; switching re-execs the helper onto the new
  model (fresh process, no leaked Metal memory). "+ get a model" downloads more.
- **Memory-safe loading** — the helper right-sizes the model to available RAM and
  refuses-before-thrashing (from `bayou.runtime_safety`).
- **Settings** — editable system prompt + response length.

## Layout

```
ui/index.html        the entire front-end (chat, sidebar, tools, settings, downloader)
backend/server.py    the helper: MLX model + agent loop + tools + chat persistence + OCR
helper/install.sh    package the helper as an auto-starting macOS LaunchAgent
src-tauri/           (optional) Tauri shell to wrap the UI as a native app
```

## Security

A hosted site driving a local agent that can run `bash` is a real attack
surface, so the helper enforces two gates (plus the per-tool approval cards):

- **Origin allowlist.** Browsers send a truthful `Origin` on the WS handshake;
  the helper rejects any origin not allowed. Set `BAYOU_ALLOWED_ORIGINS=https://your.site`
  (comma-separated) for production — then arbitrary sites (`https://evil.com`)
  are refused, and `file://`/origin-less clients are too. With it unset the
  helper is in **dev mode** (localhost + `file://` permitted).
- **Pairing token.** A per-install secret in `~/.bayou_desktop/token`. The UI
  must present it (`?token=…`); it auto-fetches it from the origin-gated `/pair`
  endpoint (or paste it from the install log for a hosted UI). `BAYOU_NO_TOKEN=1`
  disables it for pure-local use.

Verified: no-token / bad-token / disallowed-origin connections are all rejected;
allowlisted origin + token connects. Residual: a process running **as you** can
read the token file — the token guards against other origins/users, not local
malware with your privileges.

## Deploying to production

1. **Host `ui/`** on a static host (it's one file) over HTTPS.
2. **Lock the helper to your origin:** install with
   `BAYOU_ALLOWED_ORIGINS=https://your.site ./helper/install.sh`. The helper then
   refuses every other origin (and `file://`), restricts CORS to it, and still
   requires the pairing token. Without this it runs in **dev mode** (any
   localhost/`file://` page may connect) — fine locally, not for a launch.
3. **Sign + notarize** the installer/app (an unsigned `curl|bash` that runs a
   bash-capable agent will trip Gatekeeper and scare users) — *not done yet*.
4. Remember the residual risk: a hosted page driving a local `bash` agent means
   any XSS/compromise on your site = local code execution. Keep the approval
   gates; consider tightening the tool sandbox before a wide launch.

## Status / TODO
- ✅ local helper (LaunchAgent), hosted-UI plumbing, first-run model download,
  full chat app, tool agent with approvals.
- ◻ lock CORS to a real origin + pairing token; thin-helper split (move harness
  into the site for instant updates); native Tauri packaging + notarization.
