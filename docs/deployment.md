# Deploying herdr-relay

What the relay needs from the machine and the network in front of it. This is
the deployment *contract* — deliberately generic. No hostname, TLS certificate,
tunnel, or secret from any particular deployment lives in this repo; those
belong in whatever configuration repo owns the host.

Read this alongside [`native-protocol.md`](native-protocol.md), which specifies
the frames. This document specifies the socket they arrive on.

## What the relay serves

One process, one listener, one transport:

| Transport | Bind | Purpose |
|-----------|------|---------|
| TCP `HERDR_RELAY_PORT` (default 8375) | `0.0.0.0` | WebSocket **and** HTTP, same port |

The relay speaks plain HTTP. It terminates no TLS and knows nothing about its
public name, so anything reachable from the internet needs a proxy in front.

### Authentication

Every request — including the WebSocket handshake — must carry the shared
secret, or the relay answers `401`:

```
Authorization: Bearer $HERDR_RELAY_TOKEN
```

`HERDR_RELAY_TOKEN` is mandatory; the relay exits at startup without it, and
compares it in constant time. A `?token=` query parameter is also accepted, but
that is a browser fallback — it exists because a browser cannot set headers on a
WebSocket handshake, and it leaks the token into every proxy access log. Native
clients send the header and must keep doing so.

### Paths

**A WebSocket upgrade is accepted on any path.** The relay checks for
`Upgrade: websocket` before it looks at the path, so it never matches one
(`process_request` in `relay/herdr_relay/`). `wss://host/native/ws`, `wss://host/`, and
`wss://host/anything` all reach the same handler and return `101`.

This matters because it means **no proxy rewrite is required** to serve the relay
under a path like `/native/ws`. If a deployment has such a rewrite, it is
inherited, not load-bearing. Route the public path straight through, untouched.

Which path is not free, though. herdr-mobile validates its relay URL before it
dials and rejects anything else: the scheme must be `wss` and the path must be
exactly `/native/ws` (`EndpointPolicy.normalizeNative` in the app's
`SettingsStore.kt`). So the relay ignoring the path is what makes the deployment
simple, not what makes it optional — the edge must still answer on
`wss://<host>/native/ws`, and serving only `/` locks the app out.

Non-upgrade requests do match on path:

| Method | Path | Response |
|--------|------|----------|
| `GET` | `/`, `/index.html` | the browser client from `web/` |
| `GET` | `/sw.js`, `/logo.svg` | browser client assets |
| `GET` | `/api/vapid-public-key` | Web Push key, browser client only |
| `GET` | any, with `?d=<url-encoded JSON>` | queues a push event, answers `200 ok` |

**Every HTTP request is a `GET`.** The websockets library parses the request line
before the relay sees it and accepts no other method, and rejects any request
carrying `Content-Length` — so a push event travels in the query string, not a
request body. A proxy that rewrites methods or strips query strings breaks it.

The event route is how `relay/on_event.py` — the herdr plugin hook registered by
`relay/herdr-plugin.toml` — reports a status change without waiting for the next
poll. Because it is an ordinary authenticated request, every host running the hook
needs `HERDR_RELAY` pointing at the relay and `HERDR_RELAY_TOKEN` in the hook's
environment. The hook targets `/event` when `HERDR_RELAY` carries no path of its
own; the relay accepts the event on any path, and checks for `?d=` before the
static routes so that `/` works too. Push is an optimisation, not a requirement:
without it a status change surfaces on the next 2 s poll instead of immediately.

## What a reverse proxy must do

Requirements, not a configuration:

- **Terminate TLS.** Clients connect over `wss://`; herdr-mobile refuses a
  plaintext `ws://` native endpoint outright.
- **Forward the upgrade.** `Upgrade` and `Connection` headers must reach the
  relay, with HTTP/1.1 upstream.
- **Forward `Authorization`.** Stripping it turns every request into a `401`.
- **Serve `/native/ws`** and do not rewrite it (see above), or add or drop a
  trailing slash — the app's URL validation is exact.
- **Allow long-lived idle connections.** The relay's WebSocket library pings
  every 20 s by default, so an idle socket stays warm, but set the read timeout
  to at least 60 s — a proxy default of 30 s or less will still cut it.
- **Do not buffer.** Responses are streamed frames, not documents.

### nginx

```nginx
# Substitute your own server_name and certificates.
server {
    listen 443 ssl;
    http2 on;
    server_name relay.example.com;

    ssl_certificate     /etc/ssl/example/fullchain.pem;
    ssl_certificate_key /etc/ssl/example/key.pem;

    location / {
        proxy_pass http://127.0.0.1:8375;
        proxy_http_version 1.1;

        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host       $host;

        proxy_read_timeout  3600s;
        proxy_send_timeout  3600s;
        proxy_buffering     off;
    }
}
```

To serve the relay under a subpath, `location /native/ws { proxy_pass
http://127.0.0.1:8375; ... }` is enough — no `rewrite`, because the relay
ignores the path on upgrade.

### Caddy

```caddyfile
# Substitute your own site address.
relay.example.com {
    reverse_proxy 127.0.0.1:8375
}
```

Caddy forwards WebSocket upgrades and `Authorization` unchanged and has no short
read timeout, so nothing further is required.

## Environment

Everything the relay reads. Only the first is mandatory.

| Variable | Default | Purpose |
|----------|---------|---------|
| `HERDR_RELAY_TOKEN` | — | **Required.** Shared secret; the relay exits without it |
| `HERDR_RELAY_PORT` | `8375` | TCP port for WebSocket + HTTP |
| `HERDR_RATE_INPUT_BURST` | `10` | Per-connection burst for `respond`, `send_keys`, `send_text`, `send_prompt`. `0` disables the tier |
| `HERDR_RATE_INPUT_PER_SECOND` | `2` | Sustained refill for the same tier |
| `HERDR_RATE_HOST_BURST` | `30` | Per-connection burst for the other host-reaching commands. `0` disables the tier |
| `HERDR_RATE_HOST_PER_SECOND` | `10` | Sustained refill for the same tier |
| `HERDR_BIN` | `herdr` on `PATH`, else `/opt/homebrew/bin/herdr` | herdr binary. The fallback does not exist off macOS, so set it explicitly or every local poll reports the host offline |
| `HERDR_LOG_DIR` | `/var/log/herdr-remote` if writable, else `~/.local/state/herdr-remote/log` (`~/Library/Logs/herdr-remote` on macOS) | Written at import, before anything else. Set it, or a hardened unit picks an unwritable path |
| `HERDR_REMOTES` | — | Comma-separated SSH targets to poll alongside the local host |
| `HERDR_HOSTS_FILE` | — | Versioned host configuration JSON; owns host IDs, SSH routing, project roots, Herdr wrappers, harnesses, power capabilities, and readiness timeouts |
| `HERDR_PROJECTS_DB` | `~/.local/state/herdr-relay/projects.sqlite3` | Writable SQLite database for versioned saved-project metadata and migrations |
| `HERDR_WAKE_BIN` | `wakeonlan` | Wake-on-LAN binary |
| `HERDR_POLL_INTERVAL_MAX` | `10` | Seconds the poll loop backs off to while nothing is happening (#19). The floor stays 2s and returns on the first edge |
| `HERDR_POLL_BACKOFF_FACTOR` | `1.5` | Geometric step from the 2s floor toward that ceiling, one factor per consecutive quiet cycle |
| `HERDR_SSH_CONTROL_DIR` | `~/.local/state/herdr-relay/ssh` | Directory holding SSH multiplexing sockets (#19). Created `0700`. Keep it short: the socket path must fit `sockaddr_un` (104 bytes on macOS) or multiplexing is skipped |
| `HERDR_SSH_CONTROL_PERSIST` | `60` | `ControlPersist` seconds — how long an idle master connection lingers. Keep it above `HERDR_POLL_INTERVAL_MAX` |
| `HERDR_CLAUDE_PROJECTS` | `~/.claude/projects` | Root of the Claude Code session store. Explicit session paths are accepted only below this root |
| `HERDR_OPENCODE_DB` | `~/.local/share/opencode/opencode-stable.db` | OpenCode session store |
| `HERDR_TRANSCRIPT_HISTORY_MAX_BYTES` | `8388608` | Maximum transcript bytes read for an explicit structured history page |
| `HERDR_TRANSCRIPT_HISTORY_BLOCK_LIMIT` | `2000` | Maximum parsed blocks retained for explicit history paging |
| `HERDR_TRANSCRIPT_PAGE_MAX_BYTES` | `65536` | Default UTF-8 JSON byte budget for one structured history page |
| `HERDR_VAPID_PUBLIC` | — | Web Push key; the browser client subscribes, herdr-mobile does not |
| `HERDR_VAPID_PRIVATE` | — | Web Push key; browser client only |
| `HERDR_VAPID_SUBJECT` | `mailto:herdr@localhost` | Web Push contact; browser client only |

`HERDR_RELAY` is a *client* variable — the URL a client or the herdr push plugin
dials. The relay itself never reads it.

The `HERDR_RATE_*` defaults are chosen so that no human-driven session reaches
them, and they are per connection rather than per token — one shared secret means
every client is otherwise the same principal. Raise them for a deployment with
many simultaneous viewers of one relay; a rejection is logged and written to the
audit trail with the device and address, so the log says whether a limit is too
tight or a client is looping. [`native-protocol.md`](native-protocol.md#rate-limiting)
specifies which commands each tier covers and what a rejected client receives.

`HERDR_HOSTS_FILE` is the preferred multi-host configuration. Its schema is
`contract/host-config-v1.schema.json`; keep the file in the operator's private
configuration repository because it contains SSH targets, wrapper paths, and
power addresses. The relay publishes only the host ID, display name, readiness,
agent count, configured harness names, and boolean power capabilities.

`HERDR_PROJECTS_DB` must be on writable relay state storage. The database contains
opaque project IDs, host IDs, canonical paths, editable labels, archive state,
availability, and launch timestamps. It never contains or accepts client shell
commands. A service deployment should point it at its state directory so the
database survives relay restarts without becoming part of the source checkout.

The `HERDR_POLL_*` and `HERDR_SSH_CONTROL_*` defaults exist because polling used
to cost the same whether or not anyone was looking (#19). Two SSH connections per
remote host per cycle now share one master connection, and the cycle itself slows
down while no client is connected, no agent is working or blocked, no durable
operation is in flight, and nothing changed. Any of those — including the herdr
hook pushing an event — restores the 2s floor immediately, so the ceiling is only
ever paid by a relay nobody is watching. A connected client is enough on its own:
the agent list is the screen it sees before subscribing to any pane.

`HERDR_SSH_CONTROL_DIR` is the one that can bite. `ControlPath` is a Unix socket
path, and an over-long one makes ssh fail the call outright rather than fall back
to a direct connection, which would take every remote host offline. The relay
therefore measures the path first and logs a warning and runs unmultiplexed
instead of risking that. If the log says multiplexing is disabled, point the
variable at a shorter directory.

The Telegram bot (`relay/herdr_telegram.py`) is a separate process with its own
variables, `HERDR_TG_TOKEN` and `HERDR_TG_CHAT_ID`. It is not packaged here.

## Running it

### NixOS

The flake exports `packages.herdr-relay`, an `overlays.default` that adds it to
`pkgs`, and `nixosModules.herdr-relay`. The module is generic: it hardcodes no
hostname, opens no firewall port by default, and takes every secret as a file
path that it hands to systemd as a credential — which is what makes it work
under `DynamicUser`, where no dynamic uid could own a file in `/run/secrets`.

```nix
{
  imports = [ inputs.herdr-relay.nixosModules.herdr-relay ];

  services.herdr-relay = {
    enable = true;
    tokenFile = config.sops.secrets.herdr-relay-token.path;
    herdrBin = "/run/current-system/sw/bin/herdr";

    remotes = [ "herdr@builder" ];
    ssh.identityFile = config.sops.secrets.herdr-relay-ssh-key.path;
    ssh.knownHostsFile = "/etc/ssh/herdr_known_hosts";
  };
}
```

Notable defaults: `DynamicUser`, `ProtectSystem=strict`, an empty capability
bounding set, `HOME` in the state directory, logs in `/var/log/herdr-relay`, and
`openFirewall = false` — a proxy on the same host needs no open port.
`nix flake check` renders the unit and forces the module's assertions, so a
missing token or a remote without an SSH identity fails evaluation rather than
at runtime.

The module builds the relay from its own sources against the host's nixpkgs by
default. Set `services.herdr-relay.package` to
`inputs.herdr-relay.packages.${system}.herdr-relay` to use the version this
repo's flake pins instead.

### Anything else

[`contrib/herdr-relay.service`](../contrib/herdr-relay.service) is the same unit
without Nix: an `EnvironmentFile` for configuration, `DynamicUser`, and the same
hardening. Adjust its `ExecStart`, keeping the repo layout intact —
the relay resolves `web/` relative to the package directory.

Dependencies are `websockets` (required) and `pywebpush` plus `py-vapid` (Web
Push — the relay logs a warning and carries on without them).
`uv run relay/herdr-relay.py` installs them from that launcher's PEP 723 metadata.

## SSH access to remotes and hosts

Polling `HERDR_REMOTES` or SSH targets declared by `HERDR_HOSTS_FILE` shells out
to plain `ssh` with `BatchMode=yes` and no `-i`, so it needs, in the service
user's `$HOME`:

- a private key `ssh` will find, and
- a `known_hosts` entry for every remote — `BatchMode` cannot prompt, so an
  unverified host key fails the poll silently rather than asking.

The NixOS module handles this by pointing `HOME` at the state directory and
writing `~/.ssh/config` from systemd credentials on each start. On the remote
side an unprivileged account with the herdr binary is enough, plus
`sudo -n systemctl poweroff` if `shutdown_host` is used. The relay never sends
client-supplied shell text to a remote: `wake_host` and `shutdown_host` are
allowlisted against the configured power host and run fixed commands.

## Hardened units and local transcripts

`ProtectHome` and `DynamicUser` both cut the relay off from a human user's
`~/.claude/projects` and OpenCode database, so the local host's transcript blocks
come back empty even though pane state still works. Either point
`HERDR_CLAUDE_PROJECTS` and `HERDR_OPENCODE_DB` at readable copies, or relax
`ProtectHome` deliberately — `systemd.services.herdr-relay.serviceConfig
.ProtectHome = lib.mkForce "read-only"` — and grant access on purpose. Remotes
are unaffected: those transcripts are read over SSH as the remote user.

Herdr session references are treated as untrusted correlation hints. Claude IDs
must be UUIDs, references are bound to the pane's harness, and explicit local or
remote paths must resolve below `HERDR_CLAUDE_PROJECTS`. A missing exact session
returns no structured blocks; it is never replaced with the newest transcript
from the same working directory.

## What this repo does not contain

By design: your public hostname, TLS certificates, proxy or tunnel instance
configuration, secret material, and the identity of any host you poll. The
module and the unit here are the shape; the values are yours.
