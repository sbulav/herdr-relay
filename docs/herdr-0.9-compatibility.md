# Herdr 0.9 compatibility

Checked 2026-10-05 against relay commit `dadf51c12faede3963679ba9963de6ddc81b3132`
and upstream Herdr tags `v0.8.0`, `v0.9.0`, and `v0.9.3`.

The relay should remain compatible with Herdr 0.9.3 after upgrading the
running Herdr server as well as its CLI. This is a source compatibility
assessment; a live 0.9.3 integration test has not been performed.

## Status (2026-10-13)

Deployed. The relay host and the workstation it polls both run Herdr 0.9.3
with server protocol 22 (`herdr status --json` reports `compatible: true`),
and the live relay, 0.8.11 at the commit above, reports that host `ready` with
its agents listed. A host that was offline during the upgrade still needs its
own server restarted once it comes back; until then it reads
`herdr_unavailable`. The general upgrade and rollback procedure is in
[`deployment.md`](deployment.md#upgrading-and-rolling-back).

## Polling uses `api snapshot` (herdr 0.9.0+)

Since #84 the relay reads each host with one `herdr api snapshot` per poll
instead of `pane list`, `workspace list`, and a `tab list` per workspace. The
snapshot's `panes` rows carry the same fields as `pane list` rows, and its
`workspaces` and `tabs` rows carry the labels, all read at one instant. The
command does not exist before 0.9.0, so 0.9.0 is now the floor in both plugin
manifests and the README. A host still on 0.8 reads `herdr_unavailable`; after
a failed snapshot the relay asks `herdr --version` and, if it is older than
0.9.0, logs the version once per host until the host recovers. A success
response without a `result.snapshot` object is treated as Herdr unavailable,
not as a host with no agents. Sources: `relay/herdr_relay/herdr.py`,
`tests/test_herdr_snapshot.py`.

## Required upgrade step

The local `herdr --version` and `herdr status --json` both reported 0.8.0;
the running server reported protocol 19. Herdr 0.9.0 through 0.9.3 use
protocol 22. CLI commands check exact protocol equality and return
`protocol_mismatch` when the new binary talks to the old server. Consequently,
upgrading only the binary would make the relay's pane polling fail and mark
Herdr unavailable, and would prevent terminal interaction and new starts.
Sources: the two local commands;
[0.8.0 protocol](https://github.com/herdrdev/herdr/blob/v0.8.0/src/protocol/wire.rs#L16),
[0.9.3 protocol](https://github.com/herdrdev/herdr/blob/v0.9.3/src/protocol/wire.rs#L20),
[CLI compatibility check](https://github.com/herdrdev/herdr/blob/v0.9.3/src/cli.rs#L760),
[mismatch response](https://github.com/herdrdev/herdr/blob/v0.9.3/src/cli/protocol_guard.rs),
and the relay's `get_agents_from_host` in `relay/herdr_relay/herdr.py`.

Finish active work before restarting each upgraded host's server: stopping
Herdr exits pane processes. The default session uses `herdr server stop`
followed by starting Herdr again; named sessions and socket overrides require
their corresponding commands. Check `herdr status --json` afterward for the
server version and protocol. Source:
[session restart guidance](https://github.com/herdrdev/herdr/blob/v0.9.3/src/session.rs#L110).

Verify the relay's configured executable (`HERDR_BIN`, NixOS `herdrBin`, or
the per-host `herdr.binary`/wrapper) points to the upgraded installation.
A fixed old Nix store path remains old after a profile upgrade. Sources:
`relay/herdr_relay/config.py`, `relay/herdr_relay/hosts.py`,
`nix/module.nix`, and the binary path reported by local `herdr status --json`.

## Relay interfaces

The upstream schema diff from 0.8.0 to 0.9.3 preserves the pane polling
fields, agent records, launch response envelopes, and status event schema
that the relay consumes. The launch remains `tab create --cwd ... --no-focus`
then `agent start NAME --kind KIND --pane ID --timeout MS`, with harness
arguments after `--`. Pane reads still accept `--source visible|recent`
and `--lines`; input still uses `pane send-keys` and `pane send-text`.
Sources: `relay/herdr_relay/herdr.py`, `relay/herdr_relay/operations.py`,
`relay/herdr_relay/server.py`, and upstream
[pane CLI](https://github.com/herdrdev/herdr/blob/v0.9.3/src/cli/pane.rs),
[agent CLI](https://github.com/herdrdev/herdr/blob/v0.9.3/src/cli/agent.rs),
[tab CLI](https://github.com/herdrdev/herdr/blob/v0.9.3/src/cli/tab.rs),
[API schemas](https://github.com/herdrdev/herdr/tree/v0.9.3/src/api/schema).

A pane's `agent` is Herdr's effective agent label, and it is the only field
the relay takes as "an agent runs here". `agent_session` is not a substitute:
when the detected process stops matching the reporting hook — the agent quit
and left a shell — Herdr drops `agent` but keeps the session ref as a
persisted session for resume, so a pane carrying `agent_session` without
`agent` is a shell. Upstream herdr-remote 15eaf30 reads `agent_session.agent`
as a fallback; this fork deliberately does not, and
`test_pane_with_only_an_agent_session_is_not_an_agent` pins that. Sources:
`effective_agent_label` and the hook-conflict branch of the detection update in
[terminal state](https://github.com/herdrdev/herdr/blob/v0.9.3/src/terminal/state.rs),
and `pane_info` / `terminal_agent_session_info` in
[creation.rs](https://github.com/herdrdev/herdr/blob/v0.9.3/src/app/creation.rs).

The plugin runner still supplies `HERDR_PLUGIN_EVENT_JSON`, and the relay
subscribes to the retained `pane.agent_status_changed` event. Sources:
`herdr-plugin.toml`, `relay/on_event.py`,
[plugin runtime](https://github.com/herdrdev/herdr/blob/v0.9.3/src/app/api/plugins/runtime.rs#L61),
[event schema](https://github.com/herdrdev/herdr/blob/v0.9.3/src/api/schema/events.rs).
Replacing that plugin with an event subscription over SSH: [`event-bridge.md`](event-bridge.md).

0.9.2 removes the custom pane graphics API, which this relay does not call.
0.9.0 removes `--no-session`, which this relay does not use. Sources:
[0.9.2 release](https://github.com/herdrdev/herdr/releases/tag/v0.9.2),
[0.9.0 changelog](https://github.com/herdrdev/herdr/blob/v0.9.3/CHANGELOG.md),
and the CLI calls in `relay/herdr_relay/`.

## Verification and limits

`nix develop --command make check` passed lint and all 329 tests. These tests
exercise relay behavior with faked Herdr responses, so they do not prove
live upstream compatibility. No Herdr installation, running server, relay
configuration, or application code was changed during this assessment.

Remote host versions and deployed relay executable paths were not inspected.
Future 0.9.x releases require checking their own changes; this assessment is
bounded to the tagged versions above.
