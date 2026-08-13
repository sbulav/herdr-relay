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
   relay (:8375)  ←── reverse proxy (public wss://)
        │
        ▼
   herdr CLI (local or SSH to HERDR_REMOTES)
```

The relay (`relay/herdr_relay/`) is the central hub: it polls herdr for agent state, accepts push events over authenticated HTTP, and broadcasts to connected WebSocket clients. Clients send `respond`, `read_pane`, `send_keys`, and `send_text` messages back through the relay to control agents.

## Components

| Path | What | Language |
|------|------|----------|
| `relay/herdr-relay.py` | Launcher `uv run` reads PEP 723 metadata from | Python |
| `relay/herdr_relay/` | WebSocket+HTTP relay server | Python (websockets) |
| `relay/herdr_telegram.py` | Telegram bot client | Python (python-telegram-bot) |
| `web/index.html` | Mobile/desktop web app (single file) | HTML/CSS/JS |
| `docs/native-protocol.md` | The wire contract clients are written against | Markdown |
| `docs/deployment.md` | The deployment contract: ports, paths, proxy requirements, env | Markdown |
| `contract/native/` | Golden frames the relay generates and both repos assert | JSON |
| `nix/package.nix`, `nix/module.nix` | Package output and a generic NixOS module | Nix |
| `contrib/herdr-relay.service` | The same unit for hosts without Nix | systemd |

## Running Components

All Python scripts use [PEP 723 inline metadata](https://peps.python.org/pep-0723/) — `uv run` handles dependency installation automatically.

```bash
# Relay (main server)
uv run relay/herdr-relay.py

# Packaged, as a real host runs it
nix run .#herdr-relay

# Telegram bot
HERDR_TG_TOKEN="..." HERDR_TG_CHAT_ID="..." uv run relay/herdr_telegram.py
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
`pane_id`; the SSH target behind a host (`remote` on an agent entry) is a login
string and is stripped at the broadcast boundary by `public_agents()`. The host
configuration file is the same kind of state: MAC addresses and wrapper paths
never leave it — a client sees only the `capabilities` booleans `public_hosts()`
derives from them — and a project root leaves it only as the opaque handle and
label built by `hosts.project_roots()`, never as a filesystem path. Adding a
field to an outbound frame means checking it is not one of these.

## Deployment

[`docs/deployment.md`](docs/deployment.md) is the deployment contract: ports,
which paths mean what, what a reverse proxy must do, and every environment
variable. Keep it current when any of those change.

- Relay: `packages.herdr-relay` plus `nixosModules.herdr-relay` from the flake,
  or `contrib/herdr-relay.service` without Nix
- Web app: Cloudflare Pages (push to main deploys `web/`). A supported second
  client, not a transitional one — herdr-mobile#37 replaced #14's plan to retire
  it with keeping the two at parity.

**What does not belong in this repo.** Hostnames, TLS certificates, proxy or
tunnel instance configuration, secret material, and the identity of any polled
host all live in the operator's own configuration repo. The module and unit here
carry the shape and no values: every secret is a file path handed to systemd as a
credential, which is also what makes them work under `DynamicUser`. Do not add a
real hostname or secret to make an example concrete.

Note that a WebSocket upgrade is accepted on *any* path — `process_request`
checks for the `Upgrade` header before it ever looks at the path — so a public
route like `/native/ws` needs no proxy rewrite.
