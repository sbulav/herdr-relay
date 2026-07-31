# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

herdr-relay is a WebSocket relay for monitoring and approving [herdr](https://herdr.dev) AI agents remotely. It bridges the herdr CLI with the [herdr-mobile](https://github.com/sbulav/herdr-mobile) Android app, a single-file web PWA, and Telegram.

**herdr-mobile is the client that matters.** It is the one under active
development, and it is the reason a change here is worth making. The web PWA is
kept only until the app's WebView mode goes away (#14); code that exists solely
for it is marked `LEGACY (#14)`.

This repo is a hard fork of [dcolinmorgan/herdr-remote](https://github.com/dcolinmorgan/herdr-remote)
(AGPL-3.0-or-later). There is no upstream remote and no merge target — do not
try to preserve compatibility with upstream's shape, and do not restore code
because upstream has it.

## Architecture

```
Clients (android/web/telegram)
        │ WebSocket
        ▼
   relay (:8375)  ←── Cloudflare tunnel (public wss://)
        │
        ▼
   herdr CLI (local or SSH to HERDR_REMOTES)
```

The relay (`relay/herdr_relay.py`) is the central hub: it polls herdr for agent state, accepts push events via HTTP POST and UDP, and broadcasts to connected WebSocket clients. Clients send `respond`, `read_pane`, `send_keys`, and `send_text` messages back through the relay to control agents.

## Components

| Path | What | Language |
|------|------|----------|
| `relay/herdr_relay.py` | WebSocket+HTTP relay server | Python (websockets, zeroconf) |
| `relay/herdr_telegram.py` | Telegram bot client | Python (python-telegram-bot) |
| `web/index.html` | Mobile/desktop web app (single file) | HTML/CSS/JS |
| `docs/native-protocol.md` | The wire contract clients are written against | Markdown |
| `contract/native/` | Golden frames the relay generates and both repos assert | JSON |

## Running Components

All Python scripts use [PEP 723 inline metadata](https://peps.python.org/pep-0723/) — `uv run` handles dependency installation automatically.

```bash
# Relay (main server)
uv run relay/herdr_relay.py

# Full setup with Cloudflare tunnel
relay/start.sh

# Telegram bot
HERDI_TG_TOKEN="..." HERDI_TG_CHAT_ID="..." uv run relay/herdr_telegram.py
```

## Key Environment Variables

| Variable | Purpose |
|----------|---------|
| `HERDR_RELAY_PORT` | Relay WebSocket port (default: 8375) |
| `HERDR_RELAY_TOKEN` | Shared secret for auth — **required**, the relay refuses to start without it |
| `HERDR_REMOTES` | Comma-separated SSH targets to poll |
| `HERDR_BIN` | Path to herdr binary (default: `/opt/homebrew/bin/herdr`) |
| `HERDR_RELAY` | Relay URL used by clients (default: `ws://127.0.0.1:8375`) |

## Web App

The web app is a single self-contained HTML file (`web/index.html`) with inline CSS and JS — no build step. It's deployed to Cloudflare Pages. It includes 11 color themes, a mobile terminal keyboard, PWA support, and agent-icon detection.

## WebSocket Protocol

Messages are JSON with a `type` field:

**Server → Client:** `agents` (state list), `blocked` (approval prompt), `pane_content` (terminal read)

**Client → Server:** `respond` (send text to agent), `read_pane` (request terminal content), `send_keys` (send key sequences), `send_text` (raw text without newline)

Every frame is specified in [`docs/native-protocol.md`](docs/native-protocol.md) — that document is the contract clients are written against.

### Changing a frame

Frames are pinned by golden files in `contract/native/`, asserted here by
`tests/test_native_contract.py` and byte-for-byte in herdr-mobile's
`protocol-fixtures/native/`. Any wire change is therefore three steps:

```bash
UPDATE_CONTRACT=1 make test   # regenerate
make check                    # ruff + unittest
```

then copy the changed goldens into herdr-mobile and update
`docs/native-protocol.md`. Goldens must be reproducible — never let a wall
clock, hostname, or random value into one; freeze it in the test.

### What never goes on the wire

Server-side routing state stays on the server. A pane is addressed by `host` and
`pane_id`; the SSH target behind a host (`remote` on an agent entry, `target` on
a preset) is a login string and is stripped at the broadcast boundary by
`public_agents()` and `public_presets()`. Adding a field to an outbound frame
means checking it is not one of these.

## Deployment

- Web app: Cloudflare Pages (push to main deploys `web/`)
