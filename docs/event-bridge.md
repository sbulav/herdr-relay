# Event bridge: transport and subscription lifecycle

This records the decisions for issue #87. Events wake the poll loop sooner. They
never replace it. Polling stays the source of truth and keeps running as a
backstop at `HERDR_POLL_INTERVAL_MAX`. Everything below assumes Herdr 0.9.3
(protocol 22). It also assumes two things from sibling work: one
`herdr api snapshot` per host per poll (#84), and `done` derived from
`snapshot.agents[].completion_seq` (#85). Upstream links point at tag v0.9.3
(commit `7b116c0`).

## Transport: `herdr remote-api-bridge` over the existing SSH connection

For each remote host, the relay keeps one long-lived process:

    ssh <ssh_options()> <target> <herdr_command...> remote-api-bridge

For the local host, it runs `<herdr_command...> remote-api-bridge` directly.
The relay writes one `events.subscribe` request line to the process's stdin,
then reads newline-delimited JSON frames from its stdout.

`remote-api-bridge` is a hidden subcommand of the herdr binary. It joins
stdin/stdout to the session's API socket. It is the same bridge Herdr's own
`--machine` forwarding starts over ssh. `herdr remote-api-bridge --check`
prints `herdr-api-bridge-v1` and exits 0. Herdr runs that same check before it
trusts a remote. Sources:
[remote.rs](https://github.com/herdrdev/herdr/blob/v0.9.3/src/remote.rs)
(`run_remote_api_bridge`),
[unix_common.rs](https://github.com/herdrdev/herdr/blob/v0.9.3/src/platform/unix_common.rs)
(stdio forwarding),
[attach.rs](https://github.com/herdrdev/herdr/blob/v0.9.3/src/remote/attach.rs)
(Herdr's own ssh invocation and capability check). The subcommand first
appears in 0.9.1, together with `--machine`
([CHANGELOG 0.9.1](https://github.com/herdrdev/herdr/blob/v0.9.3/CHANGELOG.md)).

Why this transport:

- **No new connection or authentication.** `ssh_options()` already sets
  `ControlMaster=auto`, `ControlPath` and `ControlPersist`
  (`relay/herdr_relay/herdr.py`). The bridge becomes one more session on the
  master the poll loop already holds. Each host gets one ssh mux client on the
  relay and one bridge process on the host. A reconnect costs one channel
  open, not a TCP and key exchange. While the bridge is open, the master stays
  up, so `ControlPersist` stops expiring between polls.
- **No socket-path discovery.** Herdr resolves its own socket:
  `$XDG_CONFIG_HOME/herdr/herdr.sock` or `~/.config/herdr/herdr.sock`,
  `sessions/<name>/herdr.sock` for a named session, or `HERDR_SOCKET_PATH`. It
  resolves the path the same way on Linux and macOS. Any wrapper in the host's
  `herdr.command` applies to the bridge exactly as it applies to polling.
  Sources:
  [session.rs](https://github.com/herdrdev/herdr/blob/v0.9.3/src/session.rs),
  [config/io.rs](https://github.com/herdrdev/herdr/blob/v0.9.3/src/config/io.rs).
- **Nothing shell-specific, and no client data.** The remote command is the
  operator-configured `herdr_command(host)` plus one literal word. It contains
  no quoting, pipes or redirections, so it means the same under a fish login
  shell on macOS as under sh. The request line travels over stdin, never
  through a shell. The relay builds it from host-side pane ids that came from
  a snapshot. No text from a client ever reaches it.
- **No new listening ports, no secrets on hosts, no extra packages.** Herdr
  already ships the bridge. Hosts need nothing beyond the herdr binary.
- **It fits the hardened unit.** The relay already runs `ssh` under
  `DynamicUser`, with the key and `known_hosts` taken from systemd credentials
  (`nix/module.nix`). The bridge reuses that ssh config. For the local host,
  it needs the same socket access that polling already needs.

The cost: the subcommand is hidden and undocumented. Herdr itself depends on
it through a versioned capability string, so the relay gates on
`--check` == `herdr-api-bridge-v1`. When the check fails, the relay polls only
and logs once. Polling keeps working for hosts older than 0.9.1.

### Rejected

- **`ssh -L` stream-local forward.** It needs the absolute remote socket path,
  which depends on `$HOME`, the XDG setting, the session name and the wrapper
  env. It needs `AllowStreamLocalForwarding` on every sshd. It leaves a local
  socket file per host to create and clean up. It also bypasses
  `herdr.command`.
- **`ssh host socat - UNIX-CONNECT:<path>`.** It needs the same path. socat is
  not installed on stock macOS. The path argument would need quoting that has
  to survive fish.
- **A custom remote helper.** It does what `remote-api-bridge` already does,
  but has to be deployed and versioned on every host.
- **The herdr plugin (`relay/on_event.py`).** Every host needs a network path
  back to the relay and a copy of `HERDR_RELAY_TOKEN`. The host identity is
  self-asserted through `HERDR_HOST_ID`. Herdr spawns a Python process per
  event
  ([plugins/runtime.rs](https://github.com/herdrdev/herdr/blob/v0.9.3/src/app/api/plugins/runtime.rs)).
  The `cwd`, `tab_id`, `tab_name` and `workspace_name` that the script reads
  from the event data are always empty, because Herdr's status event does not
  carry them (`herdr api schema --json`).

## Subscription set

A connection carries exactly one `events.subscribe` request. The server reads
one request line per connection and then streams
(`handle_connection_with_stop` and `stream_subscriptions` in
[api/server.rs](https://github.com/herdrdev/herdr/blob/v0.9.3/src/api/server.rs)).
The set is fixed for the life of the stream. Each stream subscribes to:

- the global lifecycle events, which take no pane id: `pane.created`,
  `pane.closed`, `pane.exited`, `pane.moved`, `pane.updated`,
  `pane.agent_detected`, `tab.closed`, `workspace.closed`;
- one `pane.agent_status_changed` with `pane_id` for each pane that the last
  snapshot reported as an agent.

Status subscriptions require a `pane_id`. Without one, the server answers
`invalid_request: missing field pane_id`. The relay limits them to agent panes
because each one makes Herdr run a `pane_get` on every 100 ms tick
(`poll_batch` in
[api/subscriptions.rs](https://github.com/herdrdev/herdr/blob/v0.9.3/src/api/subscriptions.rs),
`CONNECTION_POLL_INTERVAL` in api/server.rs).

A status frame looks like this (captured locally):

    {"event":"pane.agent_status_changed","data":{"pane_id":"w5G:p1","workspace_id":"w5G","agent":"claude","agent_status":"working"}}

It has no tab, cwd or sequence. So an event only marks its host dirty and
calls `wake_poll_loop()`. A `blocked` status additionally feeds the existing
pushed-event path (`read_pane` then `dialogs.ensure` in
`relay/herdr_relay/transport.py`). The next snapshot supplies everything else.

## Resubscribing as panes appear and disappear

The set is fixed per connection, so changing it means opening a new stream.
The relay reopens a host's stream when that host's set of agent panes changes.
That happens on `pane.created`, `pane.agent_detected` or `pane.moved` (a move
gives the pane a new id: the event carries `previous_pane_id` and a new
`pane`), or when a poll shows an agent that the stream does not cover.
Reopens are debounced (about 1 s) and done make-before-break:

1. Open the new stream.
2. Wait for `{"result":{"type":"subscription_started"}}`.
3. Close the old stream.
4. Wake the poll loop.

A disappearing pane does not force a reopen. Its stale status subscription
stays silent: `poll_batch` swallows the `pane_get` error. Setup is different.
At setup, a missing pane fails the whole request with the request id, and the
stream ends. A local check confirmed this:
`{"id":"probe:1","error":{"code":"pane_not_found",...}}` followed by EOF
([test](https://github.com/herdrdev/herdr/blob/v0.9.3/src/api/server.rs)
`subscription_setup_errors_preserve_request_id_and_reject_entire_stream`).
After a `pane_not_found` at setup, the relay takes a fresh snapshot and
reopens with the new set. It does not retry the old one.

The relay never writes after the request line and never closes the bridge's
stdin while it wants events. Herdr treats any readable byte, or EOF, from the
client as "closed" (`probe_stream_closed` in
[ipc.rs](https://github.com/herdrdev/herdr/blob/v0.9.3/src/ipc.rs)). The
bridge turns stdin EOF into a write shutdown on the socket. Locally, closing
stdin ended the stream about 0.1 s after `subscription_started`. Closing
stdin is therefore how the relay stops an old stream.

## Closed tabs and their panes

Closing a tab emits `tab_closed`. Closing a workspace's last tab also emits
`workspace_closed`. Neither emits `pane_closed` for the panes inside
([tabs.rs](https://github.com/herdrdev/herdr/blob/v0.9.3/src/app/api/tabs.rs)).
UI keybindings go through the same API methods, so they emit the same events
([shell/actions.rs](https://github.com/herdrdev/herdr/blob/v0.9.3/src/client/shell/actions.rs)).
A pane closed on its own emits `pane_closed`
([panes.rs](https://github.com/herdrdev/herdr/blob/v0.9.3/src/app/api/panes.rs)).
A pane whose process died emits `pane_exited`, not `pane_closed`
([app/api.rs](https://github.com/herdrdev/herdr/blob/v0.9.3/src/app/api.rs)).
Moving out a tab's last pane closes the source tab or workspace with the same
events.

The relay keeps a pane → tab and pane → workspace map from each snapshot.
Each of these events drops the matching agents at once and wakes the poll:

- `tab_closed`: every pane of that tab;
- `workspace_closed`: every pane of that workspace;
- `pane_closed` and `pane_exited`: that pane;
- `pane_moved`: the `previous_pane_id`.

The snapshot that follows remains the authority. If an event is missed, the
next poll removes the pane anyway.

## Ordering, `events_lost` and dropped connections

Lifecycle subscriptions start at the current hub sequence and replay nothing
([CHANGELOG 0.9.0](https://github.com/herdrdev/herdr/blob/v0.9.3/CHANGELOG.md),
`events.subscribe`). The snapshot carries no hub sequence to align with. So the
order is always:

1. Subscribe.
2. Wait for `subscription_started`.
3. Snapshot.

A change that lands between steps 1 and 3 then shows up in the snapshot, in
an event, or in both.

The hub retains 512 events (`MAX_EVENTS` in
[event_hub.rs](https://github.com/herdrdev/herdr/blob/v0.9.3/src/api/event_hub.rs)).
A subscriber that falls behind gets an `events_lost` error ("resubscribe and
resync with session.snapshot"), and its stream ends
(`subscription_events_after` in api/subscriptions.rs; CHANGELOG 0.9.2). Herdr
also drops a subscriber whose writes stall for 5 s — it stopped reading
and the send buffer filled (`STREAM_WRITE_TIMEOUT`, a socket send timeout).
A quiet stream with no events is never dropped.

On `events_lost`, EOF, a bridge exit, or ssh's own `ServerAlive` timeout, the
relay does the same thing:

1. Mark the host's stream down.
2. Reconnect with backoff: 1 s, doubling, capped at `HERDR_POLL_INTERVAL_MAX`.
3. Run the subscribe → `subscription_started` → snapshot sequence.

While the stream is down, the host is polled exactly as today. The longest
loss is therefore one poll interval. The reader must drain stdout
continuously, so that Herdr's write timeout never fires.

## Fate of `relay/on_event.py` and the manifests

Once the bridge ships, the relay retires the plugin path:

- `relay/on_event.py`;
- the root `herdr-plugin.toml` and `relay/herdr-plugin.toml`;
- the copy installed by `nix/package.nix`;
- the plugin install section of `README.md`;
- the `?d=` HTTP route (`process_request` in `relay/herdr_relay/server.py`);
- the native `agent_event` client message (`docs/native-protocol.md`);
- `_discover_pushed_pane`, which creates panes before any snapshot has seen
  them;
- the tests that pin these.

`state.event_queue` stays, because `operations.py` still pushes
`operation_event` through it. Removing `agent_event` is a native-protocol
change and needs its own note in `docs/native-protocol.md`. Hosts that still
have the plugin installed will hit a route that answers 404. That costs
nothing but a log line. The README's install section is replaced by one line
telling operators to run `herdr plugin uninstall herdr-remote.relay`.

## Not verified

This design was checked only against a local Herdr 0.9.3. No remote host was
used, and no pane was created, closed or moved. Open points:

- How a fish login shell on macOS runs the remote command. If it keeps a
  parent process alive, each host has two processes instead of one.
- The sshd `MaxSessions` limit (default 10) when many make-before-break reopens
  overlap with polls on one master.
- Herdr's CPU cost of one `pane_get` per agent pane every 100 ms on hosts with
  many agents.
- `events_lost`, `tab_closed`, `workspace_closed`, `pane_exited` and
  `pane_moved` frames observed live. They are read from source only.
- Whether the hidden subcommand stays stable after 0.9.3.
- How `ControlPersist` behaves when a bridge session holds the master open
  under `DynamicUser`.

## Build outline

1. Bridge stream per host: run `remote-api-bridge` over the ControlMaster, gate on `--check`, read frames, reconnect with capped backoff.
2. Subscribe → `subscription_started` → snapshot ordering; any event marks its host dirty and calls `wake_poll_loop()`.
3. Per-agent-pane status subscriptions, with debounced make-before-break reopen on agent-set changes and re-snapshot after `pane_not_found`.
4. Close detection from `tab_closed`, `workspace_closed`, `pane_closed`, `pane_exited` and `pane_moved`, using the snapshot's pane maps.
5. Recovery from `events_lost` and EOF, with tests against a fake bridge process.
6. Route `blocked` status events into the existing `read_pane` / `dialogs.ensure` path.
7. Retire `on_event.py`, both manifests, the `?d=` route, `agent_event` and `_discover_pushed_pane`, along with their docs, packaging and tests.
8. Deployment docs: Herdr ≥ 0.9.1 needed for events, one extra ssh session per host, and polling-only fallback.
