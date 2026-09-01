# Native Relay Protocol

The herdr relay serves WebSocket and HTTP traffic on the same port. The default
port is `8375`; `HERDR_RELAY_PORT` overrides it. Clients normally use `ws://`
directly or `wss://` in production when a reverse proxy terminates TLS. Each
WebSocket message is a JSON frame with a `type` field.

Clients must ignore unknown fields for forward compatibility and drop frames
whose `type` they do not recognize. The relay likewise drops client frames with
an unknown `type`. It also silently drops malformed JSON.

Additive changes stay ignorable because it is convenient, not because the
protocol has to bear unknown clients forever. There is one client and both
deploys are the same person's; a change that cannot be made additively is
allowed, and `server_info.min_client` below is how it is announced.

This document describes the **native dialect**, and the native dialect is *the*
protocol. State and terminal operations use `agents`, `blocked`, and
`pane_content` frames keyed by `pane_id`.

There was once a second, unbuilt design — "protocol v1", based on `snapshot`,
opaque `session_id`, and `dialog_id` + `revision`. It was never deployed on
either side and has been deleted (#17, and sbulav/herdr-mobile#33); the
reasoning is recorded in `ROADMAP.md` Phase 5. Nothing in this document is
transitional, and there is no second dialect to migrate toward.

One consequence is worth stating plainly rather than leaving for a client author
to discover: `pane_id` is assigned by tmux and changes when tmux renumbers, so a
pane's identity is **not stable across a restart**. A client must treat a
`pane_id` as valid only for as long as it keeps appearing in `agents` snapshots,
and must not persist one as a durable session key. If that proves too weak in
practice, the fix is an opaque session-ID layer added to this dialect on its
own — announced through `server_info.min_client`, not through a new protocol.

## Authentication

Authentication is mandatory. `HERDR_RELAY_TOKEN` must be set to a non-empty
value or the relay refuses to start. Every HTTP request handled by the shared
port is authenticated before routing — the WebSocket upgrade, event submission,
static assets, CORS preflight, and `/api/vapid-public-key` alike.

The preferred credential is an HTTP header on the WebSocket handshake or other
request:

```text
Authorization: Bearer <token>
```

If that header does not supply a token, the relay accepts the `token` query
parameter as a fallback:

```text
wss://relay.example.test/?token=<token>
```

Only the first `Authorization` header is considered, and `Bearer ` is stripped
as a prefix only. The comparison is constant-time over UTF-8 bytes, so a
non-ASCII token is a plain rejection rather than an error.

An absent or unequal token produces this HTTP response before a WebSocket is
established:

```text
HTTP/1.1 401 Unauthorized
Content-Type: text/plain

Invalid token
```

There is no unauthenticated mode.

## Rate Limiting

Every command that reaches a host is metered per connection by a refilling token
bucket: a burst available at once, then a sustained rate. There are two tiers,
because the cost differs.

| Tier | Commands | Default burst | Default sustained |
| --- | --- | --- | --- |
| Terminal input | `respond`, `send_keys`, `send_text` | 10 | 2/s |
| Host reads and writes | `read_pane`, `subscribe_pane`, `catalog_refresh`, `create_tab`, `start_session`, `cancel_start`, `terminate_session`, `wake_host`, `shutdown_host`, and every `project_*` command | 30 | 10/s |

`agent_event`, `unsubscribe_pane`, `push_subscribe`, and `push_unsubscribe` are
not metered: none of them reaches a host. Replaying a `request_id` the relay
already answered is not metered either — the reply comes from the per-connection
result cache, so the work being limited has already happened.

A rejected command **does not run**. The rejection is returned in the dialect the
rejected command already speaks: a typed command gets `command_error` with code
`RATE_LIMITED`, and a pane command gets `error` with message
`rate limited, slow down`.

Neither frame carries a retry hint. A client that has been told to slow down
knows the burst it just spent, and a backoff derived from the relay's clock
cannot be pinned by a golden frame. Back off and retry; the bucket refills at the
sustained rate whether or not the client keeps asking.

The limit is per *connection*, which is the most it can be while every client
shares one token. Per-device tokens are tracked in
[#18](https://github.com/sbulav/herdr-relay/issues/18). Operators tune the tiers
through the `HERDR_RATE_*` variables in [`deployment.md`](deployment.md).

## Server To Client

### `server_info`

The first frame on every connection, sent before the relay reads anything from
the client. Nothing else was sent at connect time previously: a client waited up
to one poll interval for the first `agents` broadcast.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"server_info"`. |
| `relay_version` | string | Required | The relay's own version, for display in a client's update prompt. |
| `min_client` | integer | Required | Oldest client protocol revision this relay works with. |
| `durable_start` | boolean | Required | Whether durable project starts, cancellation, recovery, and retry lineage are supported. |

```json
{
  "type": "server_info",
  "relay_version": "0.8.0",
  "min_client": 3,
  "durable_start": true
}
```

`min_client` is a client **protocol revision**, not an app version. It changes
only when the wire changes, so a routine client release never touches it, and a
breaking change bumps it in exactly two places: `MIN_CLIENT` here and the
client's own declared revision. A client whose revision is below `min_client`
must tell the user to update and must not attempt to interpret later frames.
Clients must also keep durable-start controls disabled when `durable_start` is
absent or false, which protects a newer client connecting to an older relay.

The relay advertises and does not enforce. It never learns the client's revision
and never refuses a connection over one: a rejected socket parks a client's
reconnect loop, which presents as an outage rather than as an instruction to
update. Blocking is the client's job.

Raising `min_client` is a release decision. For a breaking start-operation
change, deploy the relay that advertises the new minimum first, then release the
client that declares that revision. Older clients will show the update screen;
the new client will not send new frames to an older relay.

## Host Configuration

Host topology is loaded from the operator-owned JSON file named by
`HERDR_HOSTS_FILE`. The file has `schema_version: 1`; the complete schema and a
placeholder example live in `contract/host-config-v1.schema.json` and
`contract/host-config-v1.example.json`. It owns host IDs and display names,
private SSH routing, allowlisted project roots, fixed Herdr wrappers, configured
harnesses, power capabilities, and per-host readiness timeouts.

The relay validates this file at startup. A wrapper is an argv prefix, not a
shell string, and client frames can never replace it or add commands. SSH
targets, MAC addresses, wrapper paths, project roots, and power implementation
details are server-side and are never logged or broadcast. This file is the only
source of host topology: revision 3 retired the preset file that once supplied a
parallel one, so a host absent from here does not exist and has no power
capability.

### `agents`

Reports agent and host state. `_poll_once` broadcasts a complete snapshot every
poll, including an empty `agents` array when no agents remain. `event_push` can
also broadcast a partial, one-agent update after an `agent_event`; that form
omits `hosts`. Clients must distinguish a complete poll snapshot from a partial
event update by the presence of `hosts`.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"agents"`. |
| `agents` | array of agent objects | Required | Complete current list for a poll, or one partial agent for an event update. |
| `hosts` | array of host objects | Poll snapshots only | Public configured host state and capabilities. |

A complete poll agent contains all of these fields. A partial event agent
contains `pane_id`, `agent`, `status`, `cwd`, `project`, and `host` only.

| Agent field | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `pane_id` | string | Required | Native pane identifier and key used by other native frames. |
| `agent` | string | Required | Agent name reported by `herdr`, or the event's `agent`; may be empty in an event update. |
| `label` | string | Poll agents only | Pane label reported by `herdr`; defaults to an empty string. |
| `status` | string | Required | `agent_status` reported by `herdr`, defaulting to `"unknown"`, or the event's `status`, defaulting to an empty string. |
| `cwd` | string | Required | Pane working directory; defaults to an empty string. |
| `project` | string | Required | Basename of `cwd` in a poll, or the event's `project`; may be empty. |
| `host` | string | Required | Configured host ID or `"local"`; event updates default to `"local"`. |
| `workspace_id` | string | Poll agents only | Workspace identifier reported by `herdr`; defaults to an empty string. |
| `tab_id` | string | Poll agents only | Tab identifier reported by `herdr`; defaults to an empty string. |
| `attention_state` | string | Optional | Additive explicit attention state: `"working"`, `"waiting"`, `"done"`, or `"idle"`. `"waiting"` means the agent is waiting on the user. Unknown statuses are omitted rather than guessed. |
| `updated_at` | integer | Optional | Additive epoch milliseconds when this pane's status or output revision last changed. |
| `output_revision` | integer | Optional | Additive monotonic per-pane output revision reported by `herdr`; omitted when unavailable or invalid. |

The relay's own SSH routing never appears here. A pane is addressed by `host` and
`pane_id`; the SSH target behind a host is server-side state, resolved from the
host configuration file and withheld by `public_agents()`.

| Host field | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `host_id` | string | Required | Configured host identifier. |
| `display_name` | string | Required | Operator-provided name safe to show in the UI. |
| `online` | boolean | Required | Compatibility projection; true only when the host is ready. |
| `status` | string | Required | `offline`, `herdr_unavailable`, or `ready`. |
| `ssh_reachable` | boolean | Required | Whether the relay reached the host's SSH endpoint. |
| `herdr_ready` | boolean | Required | Whether the configured Herdr command returned a valid pane snapshot. |
| `active_agent_count` | integer or null | Required | Number of active agents when Herdr is ready; null when the count is unknown. |
| `capabilities` | object | Required | Public booleans for `wake` and `shutdown`; private implementation values are omitted. |
| `harnesses` | array | Required | Public configured harness IDs and display names. |
| `message` | string | Optional | Safe readiness text such as `SSH unreachable` or `Herdr unavailable`. |

```json
{
  "type": "agents",
  "agents": [
    {
      "pane_id": "pane-7",
      "agent": "claude",
      "label": "api tests",
      "status": "working",
      "cwd": "/srv/herdr-remote",
      "project": "herdr-remote",
      "host": "buildbox",
      "workspace_id": "workspace-2",
      "tab_id": "tab-4"
    }
  ],
  "hosts": [{
    "host_id": "buildbox",
    "display_name": "Build box",
    "online": true,
    "status": "ready",
    "ssh_reachable": true,
    "herdr_ready": true,
    "active_agent_count": 1,
    "capabilities": {"wake": false, "shutdown": false},
    "harnesses": []
  }],
  "operations": []
}
```

This frame is fan-out: it is broadcast to every connected WebSocket.

### `catalogs`

The relay discovers configured harnesses independently of host polling. A
complete `agents` frame includes `catalogs` and `catalog_status`; a manual
`catalog_refresh` also produces this point-in-time frame. Catalog rows are
keyed by `host_id` plus `harness_id`, and contain only public display data,
exact model IDs, and friendly labels.

Each catalog includes `available`, `disabled`, `version`, `models`, `stale`,
`last_success_at`, `age_ms`, and a safe `error`. The `models` list always
contains the no-argument `default` choice for a healthy harness. OpenCode and
Codex use fixed machine-readable model adapters; Claude uses configured aliases
until it exposes stable discovery. Catalogs are refreshed at relay startup, at
most once every 24 hours, and by the typed `catalog_refresh` command. A
transient failure retains the last-successful models and marks the row stale;
removed, disabled, or permanently missing harnesses have no selectable models.

```json
{
  "type": "catalogs",
  "catalog_status": {"state": "success", "last_refresh_at": 1700000000000, "next_refresh_at": 1700086400000, "error": null},
  "catalogs": [{
    "host_id": "buildbox", "harness_id": "opencode", "display_name": "OpenCode",
    "available": true, "disabled": false, "version": "1.2.3",
    "models": [{"id": "default", "label": "Default", "available": true}, {"id": "openai/gpt-5", "label": "GPT-5", "available": true}],
    "stale": false, "last_success_at": 1700000000000, "age_ms": 0, "error": null
  }]
}
```

The manual request is `{"type":"catalog_refresh","request_id":"req-catalog-1","host_id":"buildbox"}`;
`host_id` is optional and scopes discovery to one configured host.

### `projects`

The relay owns saved project metadata in a versioned SQLite database. A project is
identified by the host ID plus its canonical path; `project_id` is an opaque relay
identifier so clients never construct identities. `label` is editable, `archived`
is a non-destructive removal flag, and `last_launch_at` is epoch milliseconds or
`null`. Configuration changes set `available` to false and preserve the row with a
safe `unavailable_reason`; reconciliation never removes a row or its directory.

The `agents` frame carries these same two arrays when available:

```json
{
  "projects": [{
    "project_id": "0123456789abcdef0123456789abcdef",
    "host_id": "buildbox",
    "root_id": "root_95e8a4520dc48f2eacf6583c",
    "label": "Herdr relay",
    "path": "/srv/projects/herdr-relay",
    "canonical_path": "/srv/projects/herdr-relay",
    "archived": false,
    "available": true,
    "unavailable_reason": null,
    "last_launch_at": 1700000000000
  }],
  "project_roots": [{"host_id": "buildbox", "root_id": "root_95e8a4520dc48f2eacf6583c", "label": "projects"}]
}
```

`projects` is also sent after a project mutation and as the point-to-point result
of `project_list`. Root labels and opaque IDs are public; configured absolute root
paths, SSH targets, wrappers, and power data are not included in `project_roots`.

### `operations` and `operation`

`start_session` is owned by the relay after acknowledgement. The `operations`
array in each `agents` snapshot contains every non-terminal start operation plus
the 32 most recently updated terminal operations. This bounded recovery history
lets a client reconnecting after completion render the terminal result without
retaining an unbounded UI list. The relay also broadcasts an `operation` frame for
each transition, including the terminal `started`, `failed`, or `cancelled` state.
Terminal rows remain in SQLite for request-id replay and retry lineage.

Operation records contain only public IDs, the bounded harness/model selection,
the deterministic Herdr agent name, a stage, an optional session ID, and a
sanitized error. `revision` starts at zero when the row is queued and increments
for every state transition; clients must use it as the operation ordering key.
`attempt` starts at one, and `retry_of_operation_id` links later attempts to the
failed or cancelled source. They never contain SSH targets, command output, or
filesystem paths.

```json
{
  "type": "operation",
  "operation": {
    "operation_id": "op-1",
    "request_id": "req-start-1",
    "host_id": "buildbox",
    "project_id": "0123456789abcdef0123456789abcdef",
    "harness": "claude",
    "model": "default",
    "agent_name": "herdr-mobile-op-1",
    "stage": "starting_agent",
    "session_id": null,
    "error_code": null,
    "error_message": null,
    "created_at": 1700000000000,
    "updated_at": 1700000000100,
    "revision": 4,
    "retry_of_operation_id": null,
    "attempt": 1
  }
}
```

### `folder_entries`

`project_browse` accepts only a configured `host_id`, opaque `root_id`, and an
array of individual relative `path` components. An absolute path, slash-bearing
component, dot component, NUL, symlink, or traversal escape is rejected. The relay
opens each component relative to a directory descriptor with no-follow semantics,
checks containment after the open, and lists one level only. Remote hosts run the
same fixed helper over SSH; client text is JSON input, never shell text.

```json
{
  "type": "folder_entries",
  "request_id": "req-browse-1",
  "host_id": "buildbox",
  "root_id": "root_95e8a4520dc48f2eacf6583c",
  "path": ["herdr-relay"],
  "canonical_path": "/srv/projects/herdr-relay",
  "entries": [{"name": "app", "kind": "directory"}]
}
```

`project_create` takes the same parent descriptor plus one child `name`; the name
is never appended to `path`. It creates one empty directory and registers it only
after re-verifying descriptor containment. `project_save` records metadata only
after the host helper verifies an existing directory. `project_rename`,
`project_remove`, and `project_restore` take an opaque `project_id`; remove only
archives metadata and never deletes, moves, or empties the directory. All seven
operations require a request ID and return a typed `command_ack`, `command_error`,
`projects`, or `folder_entries` frame.

### `blocked`

Reports that a pane needs a response. Poll and `agent_event` paths broadcast a
full frame. A `read_pane` request can send a reduced frame containing only
`pane_id`, `prompt`, and `options` when it discovers selectable options.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"blocked"`. |
| `pane_id` | string | Required | Blocked pane identifier. |
| `agent` | string | Broadcast form only | Agent name; may be empty for an event. |
| `project` | string | Broadcast form only | Project name; may be empty for an event. |
| `host` | string | Broadcast form only | Host name or ID; event frames default to `"local"`. |
| `prompt` | string | Required | Recent pane content, truncated to 500 characters. |
| `options` | array of strings | Required | Detected choices, or the default tool choices when detection fails in a broadcast. |

```json
{
  "type": "blocked",
  "pane_id": "pane-7",
  "agent": "claude",
  "project": "herdr-remote",
  "host": "buildbox",
  "prompt": "Do you want to proceed?\n1. Yes\n2. No",
  "options": ["1. Yes", "2. No"]
}
```

Poll and event forms are fan-out. The reduced form produced by `read_pane` is
point-to-point to the requesting WebSocket.

### `pane_content`

Returns recent terminal text, structured transcript blocks, or both. A
`read_pane` response always has `content` and includes `output_blocks` when the
relay can correlate a Claude or OpenCode transcript. `subscribe_pane` and live
subscription updates contain `output_blocks` without `content`.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"pane_content"`. |
| `pane_id` | string | Required | Pane whose content is represented. |
| `content` | string | `read_pane` responses only | Output of `herdr pane read`. |
| `output_blocks` | array of output block objects | Optional | At most the requested bounded page of structured Claude or OpenCode transcript blocks. |
| `output_total` | integer | Optional | Number of parsed blocks available to the current bounded history window. |
| `output_has_more` | boolean | Optional | Whether older blocks can be requested with `before`. |
| `output_next_cursor` | string | Optional | ID of the oldest block in this page; send it as `before` to fetch older blocks. |
| `output_truncated` | boolean | Optional | The transcript source exceeded the configured history byte window. |
| `attention_state` | string | Optional | Additive explicit attention state: `"working"`, `"waiting"`, `"done"`, or `"idle"`. `"waiting"` means the agent is waiting on the user. Unknown statuses are omitted rather than guessed. |
| `updated_at` | integer | Optional | Additive epoch milliseconds when this pane's status or output revision last changed. |
| `output_revision` | integer | Optional | Additive monotonic per-pane output revision reported by `herdr`; omitted when unavailable or invalid. |

These optional fields have the same values as the latest `agents` entry for the
pane. They are omitted rather than set to `null` when the relay cannot determine
them.

Every output block has `id` and `kind`. Fields after those depend on `kind`.

| Block field | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `id` | string | Required | Stable ID derived from the provider message and content/tool identifier when available; legacy `b0`/`o0` fallback otherwise. |
| `kind` | string | Required | `"assistant_text"`, `"status"`, `"tool"`, or `"diff"`. |
| `markdown` | string | `assistant_text` and `diff` | Assistant response text or bounded unified diff content. |
| `label` | string | `status`, `tool`, and `diff` | Status source such as `"You"` or `"Thought"`, tool name, or edit tool name. |
| `text` | string | `status`, `tool`, and `diff` | Status text, one-line tool summary, or edited path. |
| `role` | string | Optional | Provider role: `user`, `assistant`, `tool`, or `reasoning`. |
| `message_id` | string | Optional | Provider message identity, used by native clients to group blocks into turns. |
| `turn_id` | string | Optional | Coarser provider turn/request identity. |
| `timestamp` | integer | Optional | Provider event time as epoch milliseconds. |
| `result` | string | Optional | Bounded folded tool-result text. |
| `diff_revision` | string | Optional | Stable identity for the file-edit diff rendering. |
| `diff_clipped` | boolean | Optional | The diff body was clipped by the relay's block budget. |

```json
{
  "type": "pane_content",
  "pane_id": "pane-7",
  "content": "Running tests...",
  "output_blocks": [
    {"id": "b0", "kind": "status", "label": "You", "text": "Run tests"},
    {"id": "b1", "kind": "tool", "label": "Bash", "text": "pytest"},
    {"id": "b2", "kind": "assistant_text", "markdown": "All tests pass."}
  ]
}
```

This frame is point-to-point. Live updates are sent only to WebSockets that
subscribed to that `pane_id`, and only when the transcript signature changes.
Internal Herdr session references are never exposed. The relay binds them to the
pane harness, validates Claude UUIDs, constrains explicit transcript paths to the
configured store root, and returns no blocks when an exact reference is missing.

### `command_ack`

Acknowledges a successful `start_session`, `cancel_start`, `terminate_session`, `wake_host`,
`shutdown_host`, `project_create`, `project_save`, `project_rename`, `project_remove`, or
`project_restore` request. The frame is point-to-point. Responses are cached
by non-empty `request_id`; repeating a cached ID returns the cached frame before
the new frame's `type` is considered.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"command_ack"`. |
| `request_id` | string | Required | ID copied from the command. |
| `result` | object | Required | Command-specific result. |
| `result.host_id` | string | Launch, wake, and shutdown acknowledgements | Host ID from the command. |
| `result.output` | string | Terminate acknowledgement | Standard output from `herdr pane close`, stripped. |
| `result.created` | boolean | Create acknowledgement | `true` for the first completion; `false` for durable replay after reconnect. |
| `result.project` | object | Project acknowledgements | Relay-owned saved-project record, including its opaque ID. |

Launch acknowledgement:

```json
{
  "type": "command_ack",
  "request_id": "req-launch-17",
  "result": {"host_id": "buildbox"}
}
```

Start acknowledgement:

```json
{
  "type": "command_ack",
  "request_id": "req-start-1",
  "result": {"operation": {
    "operation_id": "op-1",
    "request_id": "req-start-1",
    "host_id": "buildbox",
    "project_id": "0123456789abcdef0123456789abcdef",
    "harness": "claude",
    "model": "default",
    "agent_name": "herdr-mobile-op-1",
    "stage": "queued",
    "session_id": null,
    "error_code": null,
    "error_message": null,
    "created_at": 1700000000000,
    "updated_at": 1700000000000
  }}
}
```

Terminate acknowledgement:

```json
{
  "type": "command_ack",
  "request_id": "req-stop-18",
  "result": {"output": "pane closed"}
}
```

Wake acknowledgement:

```json
{
  "type": "command_ack",
  "request_id": "req-wake-19",
  "result": {"host_id": "buildbox"}
}
```

Shutdown acknowledgement:

```json
{
  "type": "command_ack",
  "request_id": "req-poweroff-20",
  "result": {"host_id": "buildbox"}
}
```

### `command_error`

Rejects one of the session, host, or project commands. It is point-to-point and is
cached under a truthy incoming `request_id` in the same way as `command_ack`.
For `INVALID_REQUEST`, the response's `request_id` is `null`. A missing or empty
incoming ID is not cached; a truthy non-string incoming ID can still cause this
response to be cached under that original value.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"command_error"`. |
| `request_id` | string or null | Required | Command ID, or `null` when it was missing or invalid. |
| `code` | string | Required | Machine-readable code listed in [Error codes](#error-codes). |
| `message` | string | Required | Exact human-readable error string. |

```json
{
  "type": "command_error",
  "request_id": "req-stale",
  "code": "STALE_SESSION",
  "message": "Session is no longer active"
}
```

### `error`

Reports validation failures for pane operations and `create_tab`. It is
point-to-point and is not correlated with `request_id`.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"error"`. |
| `message` | string | Required | One of the literal validation messages below. |

| Message | Trigger |
| --- | --- |
| `unknown pane_id` | `respond`, `read_pane`, `send_keys`, or `send_text` names a pane absent from the latest poll. |
| `response not in allowlist` | Normalized `respond.text` is not globally safe or detected for that pane. |
| `keys contain disallowed values` | At least one `send_keys.keys` value fails the key allowlist. |
| `text empty or too long` | `send_text.text` is empty or longer than 1,000 characters. |
| `workspace_id required` | `create_tab.workspace_id` is absent or empty. |
| `rate limited, slow down` | The connection exceeded its [rate limit](#rate-limiting) on a pane command. The command did not run. |

```json
{
  "type": "error",
  "message": "unknown pane_id"
}
```

### `tab_created`

Acknowledges a `create_tab` request after invoking `herdr tab create`. The
relay does not inspect the command result before acknowledging it. This frame
is point-to-point.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"tab_created"`. |
| `ok` | boolean | Required | Always `true`. |

```json
{"type": "tab_created", "ok": true}
```

### `push_subscribed`

Acknowledges `push_subscribe`, including when the subscription is absent or was
already stored. This frame is point-to-point.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"push_subscribed"`. |
| `ok` | boolean | Required | Always `true`. |

```json
{"type": "push_subscribed", "ok": true}
```

### `push_unsubscribed`

Acknowledges `push_unsubscribe`, including when the subscription is absent or
was not stored. This frame is point-to-point.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"push_unsubscribed"`. |
| `ok` | boolean | Required | Always `true`. |

```json
{"type": "push_unsubscribed", "ok": true}
```

## Client To Server

### `start_session`

Starts a saved project as a durable, idempotent operation, and is the only way
to start an agent. Revision 3 retired `launch_session`, which started an agent
immediately from a server-configured preset; a client that still sends it gets
no response beyond the relay ignoring an unknown command. The typed host,
project, harness, and model selection are supplied directly. `model` is optional
and defaults to `default`; clients send model IDs, never command text.

The request is acknowledged as soon as the operation is persisted. The relay
then checks for the operation's deterministic Herdr name before starting a
client. A repeated `request_id` returns the same operation. A retry sends only
`retry_of_operation_id` and the next `attempt`; the relay derives the original
selection, deduplicates the source/attempt pair, and records the lineage. On
relay restart, non-terminal rows are resumed and the same name is checked before
another start is attempted. The operation becomes `started` only after the named
pane is observable.

```json
{
  "type": "start_session",
  "request_id": "req-start-1",
  "project_id": "0123456789abcdef0123456789abcdef",
  "host_id": "buildbox",
  "harness": "claude",
  "model": "default"
}
```

Retry example:

```json
{
  "type": "start_session",
  "request_id": "req-retry-2",
  "retry_of_operation_id": "op-1",
  "attempt": 2
}
```

### `cancel_start`

Cancels an active durable start operation. The request is idempotent: cancelling
an already terminal operation returns its terminal record, while a worker that is
currently probing or launching observes the cancellation and terminates its
active local SSH subprocess. Cancellation never powers the host off, so a host
that was woken remains on.

```json
{
  "type": "cancel_start",
  "request_id": "req-cancel-2",
  "operation_id": "op-1"
}
```

### `project_list`

Returns a searchable `projects` frame. `query` is optional, literal, and bounded
to 128 characters; matching is against the editable label and canonical path.

```json
{"type":"project_list","request_id":"req-project-list-1","query":"relay"}
```

### `project_browse`

Lists one directory level below an opaque configured root. `path` is an array of
individual names, not a path string. The request is rejected if any component is
absolute, empty, dot-like, contains a separator or NUL, is a symlink, or leaves
the root under a concurrent filesystem change.

```json
{
  "type": "project_browse",
  "request_id": "req-browse-1",
  "host_id": "buildbox",
  "root_id": "root_95e8a4520dc48f2eacf6583c",
  "path": ["herdr-relay"]
}
```

### `project_save`

Verifies a directory with `project_browse` semantics and saves its canonical
host-scoped identity. An omitted label defaults to the selected folder name.

```json
{
  "type": "project_save",
  "request_id": "req-save-1",
  "host_id": "buildbox",
  "root_id": "root_95e8a4520dc48f2eacf6583c",
  "path": ["herdr-relay"],
  "label": "Herdr relay"
}
```

### `project_create`

Creates exactly one empty child below the selected allowlisted parent and saves
it as a project. `name` is a single field and is never joined into `path` by the
client. Empty or dot names, separators, controls, surrounding whitespace,
trailing dots, `<>:"|?*`, Windows-reserved stems, and names over 255 UTF-8 bytes
are rejected. `mkdir` is descriptor-relative and atomic; the helper re-opens the
new child with no-follow semantics and re-checks containment after creation.

The request ID is also stored in SQLite. Replaying a completed request after a
reconnect returns the same project with `created: false`; a concurrent duplicate
returns `REQUEST_IN_FLIGHT`.

```json
{
  "type": "project_create",
  "request_id": "req-create-1",
  "host_id": "buildbox",
  "root_id": "root_95e8a4520dc48f2eacf6583c",
  "path": [],
  "name": "new-service",
  "label": "New service"
}
```

### `project_rename`, `project_remove`, and `project_restore`

Each takes `request_id` and an opaque `project_id`; rename also takes a bounded,
non-empty `label`. Remove archives metadata only. Restore clears that archive
flag even when the project is currently unavailable because configuration changed.
The next `projects` frame carries the resulting state.

```json
{"type":"project_rename","request_id":"req-rename-1","project_id":"0123456789abcdef0123456789abcdef","label":"Relay"}
{"type":"project_remove","request_id":"req-remove-1","project_id":"0123456789abcdef0123456789abcdef"}
{"type":"project_restore","request_id":"req-restore-1","project_id":"0123456789abcdef0123456789abcdef"}
```

### `terminate_session`

Closes the pane mapped to an active native session key. The relay derives that
key as `legacy:<host_id>:<pane_id>` from the latest poll, so it inherits
`pane_id`'s instability: the key is not itself on the wire, so a client composes
it from the `host` and `pane_id` of the current snapshot rather than remembering
a key from an earlier one. The `legacy:` prefix is a fossil of the deleted v1 split and
carries no meaning; it stays on the wire only because renaming it would break
clients for nothing.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"terminate_session"`. |
| `request_id` | string | Required | Non-empty idempotency and response-correlation key. |
| `session_id` | string | Required | Current `legacy:<host_id>:<pane_id>` lookup key. |
| `confirmation_nonce` | string | Required | Any non-empty string; the relay checks presence but does not otherwise interpret it. |

```json
{
  "type": "terminate_session",
  "request_id": "req-stop-18",
  "session_id": "legacy:buildbox:pane-7",
  "confirmation_nonce": "confirm-stop-18"
}
```

### `wake_host`

Runs the relay's fixed Wake-on-LAN command for a configured host whose public
`capabilities.wake` value is true.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"wake_host"`. |
| `request_id` | string | Required | Non-empty idempotency and response-correlation key. |
| `host_id` | string | Required | Must name a configured host with wake capability. |

```json
{
  "type": "wake_host",
  "request_id": "req-wake-19",
  "host_id": "buildbox"
}
```

### `shutdown_host`

Runs the fixed remote command `sudo -n systemctl poweroff` for a configured host
with shutdown capability. The host must have a private SSH target.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"shutdown_host"`. |
| `request_id` | string | Required | Non-empty idempotency and response-correlation key. |
| `host_id` | string | Required | Must name a configured host with shutdown capability. |
| `confirmation_nonce` | string | Required | Any non-empty string; the relay checks presence but does not otherwise interpret it. |

```json
{
  "type": "shutdown_host",
  "request_id": "req-poweroff-20",
  "host_id": "buildbox",
  "confirmation_nonce": "confirm-poweroff-20"
}
```

### `respond`

Selects a response to a blocked agent prompt.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"respond"`. |
| `pane_id` | string | Required | Must identify a pane from the latest poll. |
| `text` | string | Required in practice | Response label. Missing input defaults to an empty string and fails the allowlist. |

The relay strips surrounding whitespace and lowercases `text` for validation.
It accepts globally safe responses or choices detected for that pane. The
global literal allowlist is `y`, `n`, `a`, `yes`, `no`, `trust`,
`yes, single permission`, `trust, always allow`, `no (tab to edit)`,
`approve all pending`, `configure individually`, and
`exit (cancel subagents)`. Detected option labels are also accepted. The relay
maps some accepted labels to key presses or shorter text before sending them.

```json
{
  "type": "respond",
  "pane_id": "pane-7",
  "text": "1. Yes"
}
```

### `agent_event`

Enqueues an agent status event. The event worker may fan out `blocked` and
partial `agents` frames. No acknowledgement is sent.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"agent_event"`. |
| `pane_id` | string | Optional | Pane identifier. A non-empty value is required for either broadcast. |
| `status` | string | Optional | Agent status; defaults to an empty string. `"blocked"` triggers a `blocked` frame. |
| `host` | string | Optional | Host label; defaults to `"local"`. |
| `prompt` | string | Optional | Fallback blocked prompt when the pane cannot be read locally or through a known remote; defaults to `"Agent is blocked"`. |
| `agent` | string | Optional | Agent name in emitted frames; defaults to an empty string. |
| `project` | string | Optional | Project name in emitted frames; defaults to an empty string. |
| `cwd` | string | Optional | Working directory in the partial `agents` frame; defaults to an empty string. |

For a blocked event, the relay reads current pane content instead of `prompt`
when the pane maps to a remote or `host` is `"local"`.

```json
{
  "type": "agent_event",
  "pane_id": "pane-7",
  "status": "blocked",
  "host": "buildbox",
  "agent": "claude",
  "project": "herdr-remote",
  "cwd": "/srv/herdr-remote",
  "prompt": "Approve this operation?"
}
```

### `read_pane`

Requests terminal output and, when available, structured blocks. The relay uses
the non-invasive `visible` source by default so automatic/mobile polling does
not make alternate-screen agents scroll. Clients that explicitly need a
history/recovery read may request `recent`.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"read_pane"`. |
| `pane_id` | string | Required | Must identify a pane from the latest poll. |
| `lines` | integer, or a string parseable as one | Optional | Line count for `herdr pane read --lines`. Defaults to `30`, floors at `1`, caps at `2000`. |
| `source` | string | Optional | Terminal source: `visible` (default, safe for live polling) or `recent` (explicit history/recovery). Other values receive an `error` response. |
| `before` | string | Optional | When present, return an older structured-output page ending before this block ID. |
| `block_limit` | integer | Optional | Maximum structured blocks in the page. Defaults to `200`, caps at `2000`. |
| `max_bytes` | integer | Optional | UTF-8 JSON byte budget for the structured page. Defaults to `65536`, capped by `HERDR_TRANSCRIPT_PAGE_MAX_BYTES`. |

Anything unparseable, zero, or negative falls back to the default instead of
reaching herdr, which reports a bad `--lines` as an error string on stdout with
exit code 0 — a client that sent `"lines": "abc"` used to get
`Error: Custom { kind: Other, error: "invalid value for --lines: abc" }`
delivered as `pane_content.content`. The relay requests `--source visible` by
default. A request with `source: "recent"` explicitly requests history/recovery
output. Both sources use the same line bound and terminal-chrome filtering.
Supplying `before`, `block_limit`, or `max_bytes` also makes the structured
output cursor-aware: the page is bounded by both block count and UTF-8 byte
budget, and `output_next_cursor` can be sent back as `before`.
An unknown or stale `before` cursor returns an empty page with
`output_has_more: false`; it never restarts at the newest page.

```json
{
  "type": "read_pane",
  "pane_id": "pane-7",
  "lines": 50
}
```

### `subscribe_pane`

Subscribes this WebSocket to structured transcript updates for one pane.
Subscribing replaces its previous pane subscription. If structured content is
already available, the relay sends it immediately.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"subscribe_pane"`. |
| `pane_id` | string | Required for an effect | Pane identifier from the latest poll with correlation metadata. |

An absent or unknown `pane_id` is silently ignored. There is no acknowledgement
other than a possible `pane_content` frame.

```json
{"type": "subscribe_pane", "pane_id": "pane-7"}
```

### `unsubscribe_pane`

Removes this WebSocket's current pane subscription and stream signature. It
does not read a `pane_id` and sends no acknowledgement.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"unsubscribe_pane"`. |

```json
{"type": "unsubscribe_pane"}
```

### `send_keys`

Sends an allowlisted key sequence to a current pane.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"send_keys"`. |
| `pane_id` | string | Required | Must identify a pane from the latest poll. |
| `keys` | array of strings | Optional | Key sequence; defaults to an empty array, which is accepted. |

Literal allowed values are `y`, `n`, `a`, `Enter`, `Tab`, `Escape`, `Space`,
`C-c`, `Ctrl+c`, `Up`, `Down`, `Left`, `Right`, `BSpace`, and the strings `1`
through `9`. The relay also accepts a case-sensitive prefix of `ctrl+` or
`shift+`, followed by a lowercase letter, `1` through `9`, or one of `Enter`,
`Tab`, `Escape`, `Space`, `Up`, `Down`, `Left`, or `Right`. Any disallowed value
rejects the whole frame.

```json
{
  "type": "send_keys",
  "pane_id": "pane-7",
  "keys": ["Ctrl+c"]
}
```

### `send_text`

Sends text verbatim to a current pane. It does not append a newline.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"send_text"`. |
| `pane_id` | string | Required | Must identify a pane from the latest poll. |
| `text` | string | Required | Non-empty text of at most 1,000 characters. |

```json
{
  "type": "send_text",
  "pane_id": "pane-7",
  "text": "pytest -q"
}
```

### `create_tab`

Creates and focuses a tab in a workspace on the relay's local herdr instance.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"create_tab"`. |
| `workspace_id` | string | Required | Non-empty workspace identifier passed to `herdr tab create --workspace`. |

```json
{"type": "create_tab", "workspace_id": "workspace-2"}
```

### `push_subscribe`

> **LEGACY (#14).** Web Push exists for the browser PWA only. herdr-mobile
> monitors the WebSocket in a foreground service and never subscribes; these two
> frames are retired together with `web/`.

Stores a Web Push subscription if it is truthy and not already present. The
relay performs no schema validation and always acknowledges the frame.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"push_subscribe"`. |
| `subscription` | any JSON value | Optional | Usually a Push API subscription object; stored as supplied when truthy. The relay imposes no schema on it — it is handed to `pywebpush` as `subscription_info`. |

```json
{
  "type": "push_subscribe",
  "subscription": {
    "endpoint": "https://push.example.test/subscriptions/abc",
    "expirationTime": null,
    "keys": {"p256dh": "base64url-key", "auth": "base64url-secret"}
  }
}
```

### `push_unsubscribe`

Removes an equal stored Web Push subscription if present. The relay performs
no schema validation and always acknowledges the frame.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"push_unsubscribe"`. |
| `subscription` | any JSON value | Optional | Compared for equality against stored subscriptions, so it must be byte-identical to what was subscribed. |

```json
{
  "type": "push_unsubscribe",
  "subscription": {
    "endpoint": "https://push.example.test/subscriptions/abc",
    "expirationTime": null,
    "keys": {"p256dh": "base64url-key", "auth": "base64url-secret"}
  }
}
```

## Error Codes

These are all code and message pairs produced through `command_error`.

| Code | Exact message | Trigger |
| --- | --- | --- |
| `INVALID_REQUEST` | `request_id is required` | Any session, host, or project command has a `request_id` that is not a non-empty string. The response's `request_id` is `null`. |
| `INVALID_PATH` | `Invalid folder path` | A project browse or save path is not a bounded list of individual relative names. |
| `INVALID_NAME` | `Folder name is reserved on some platforms` | A create name violates the portable platform rules. Other invalid-name messages describe the same code more specifically. |
| `INVALID_LABEL` | `Project label must be 1-128 characters` | A project save or rename label is empty or too long. |
| `UNKNOWN_HOST` | `Unknown host` | A project operation names a host absent from the configured host file. |
| `ROOT_NOT_ALLOWED` | `Folder root is not configured for this host` | A project operation names a root handle absent from the host configuration. |
| `FOLDER_NOT_FOUND` | `Folder is unavailable` | The requested directory disappeared before the descriptor-relative open. |
| `FOLDER_EXISTS` | `A folder with that name already exists` | Atomic create found any existing entry with the requested name. |
| `REQUEST_IN_FLIGHT` | `This folder is already being created` | Another worker owns the same durable create request. |
| `CREATE_FAILED` | `Folder could not be registered` | Filesystem creation succeeded but durable catalog registration did not. The relay attempts descriptor-relative rollback. |
| `PATH_NOT_ALLOWED` | `Folder left the configured root` | A path or symlink failed descriptor-relative containment validation. |
| `PROJECT_NOT_FOUND` | `Project not found` | A project mutation or project launch names an unknown opaque ID. |
| `PROJECT_ARCHIVED` | `Project is removed` | A launch names an archived project. |
| `PROJECT_UNAVAILABLE` | `Project configuration is unavailable` | A project launch is orphaned by host/root configuration or its folder changed. |
| `LAUNCH_FAILED` | `Herdr did not start the client` | Either half of the launch fails: `herdr tab create` does not return a pane, or `herdr agent start` fails or exits unsuccessfully. Since revision 3 this reaches a client as an operation error only, never as a `command_error`. |
| `CONFIGURATION_CHANGED` | `The selected project configuration is no longer available` | A durable start no longer has its saved host, root, folder, harness, or model configuration. |
| `HOST_OFFLINE` | `The selected host is offline` | A durable start cannot reach its configured host. |
| `HERDR_UNAVAILABLE` | `Herdr is unavailable on the selected host` | SSH responds but the configured Herdr command does not answer usably — no pane snapshot for polling, and no name-registry answer (including a `protocol_mismatch` from version skew) for a durable start. |
| `AGENT_NOT_OBSERVABLE` | `Herdr did not expose a recoverable agent identity` | Herdr accepted the launch, but neither the `agent start` reply nor `agent get` resolved the exact deterministic start name before the observation deadline. The relay does not guess from cwd, harness, timing, or pane order. |
| `DUPLICATE_AGENT` | `The host reported more than one matching agent` | Legacy terminal error retained for rows written by older relay versions. Herdr rejects a taken name at `agent start`, so a new operation can no longer observe two matching agents. |
| `READY_TIMEOUT` | `Host did not become ready before the timeout` | A waking host did not expose SSH or Herdr before its configured readiness timeout. Once launch begins, a missing exact agent identity is `AGENT_NOT_OBSERVABLE` instead. |
| `WAKE_FAILED` | `Wake-on-LAN command failed` | The durable operation's configured Wake-on-LAN process raises or exits unsuccessfully. |
| `OPERATION_NOT_FOUND` | `Start operation is no longer available` | `cancel_start.operation_id` or `start_session.retry_of_operation_id` does not identify a persisted start operation. |
| `CONFIRMATION_REQUIRED` | `confirmation_nonce is required` | `terminate_session` or `shutdown_host` lacks a non-empty string nonce. |
| `STALE_SESSION` | `Session is no longer active` | `terminate_session.session_id` is absent from the latest active session map. |
| `TERMINATE_FAILED` | `Herdr did not terminate the client` | The `herdr pane close` process fails or exits unsuccessfully. |
| `HOST_NOT_ALLOWED` | `Power control is not allowed for this host` | `wake_host.host_id` or `shutdown_host.host_id` names a host absent from the host configuration file, or one whose configuration grants no `wake` MAC or no `shutdown`. |
| `WAKE_FAILED` | `Wake-on-LAN command failed` | The configured Wake-on-LAN process raises or exits unsuccessfully. |
| `UNKNOWN_HOST` | `Power host has no SSH target` | A host configured for shutdown has no SSH target. `load_hosts` rejects that combination at startup, so this is a last-resort guard rather than a reachable configuration. |
| `SHUTDOWN_FAILED` | `Host shutdown command failed` | The fixed SSH shutdown process raises or exits unsuccessfully. |
| `RATE_LIMITED` | `Too many requests, slow down` | The connection exceeded its [rate limit](#rate-limiting) on a typed command. The command did not run. |

## Source Of Truth

This reference was derived from `relay/herdr_relay/`, especially
`handle_client`, `_poll_once`, `event_push`, `broadcast`, `process_request`,
`public_agents`, `get_agents_from_host`, `get_all_agents`, `pane_blocks`,
`transcript_to_blocks`, `opencode_to_blocks`, `command_error`,
`start_session`, `terminate_session`, `wake_host`, `shutdown_host`, and the
project store/filesystem handlers. Re-read
those functions when changing or re-verifying the native contract.
