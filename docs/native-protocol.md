# Native Relay Protocol

The herdr relay serves WebSocket and HTTP traffic on the same port. The default
port is `8375`; `HERDR_RELAY_PORT` overrides it. Clients normally use `ws://`
directly or `wss://` in production when a reverse proxy terminates TLS. Each
WebSocket message is a JSON frame with a `type` field.

Clients must ignore unknown fields for forward compatibility and drop frames
whose `type` they do not recognize. The relay likewise drops client frames with
an unknown `type`. It also silently drops malformed JSON.

This document describes the **native dialect** spoken by the production relay
today. It is distinct from the aspirational "protocol v1" design based on
`snapshot`, `session_id`, and `dialog_id`. The relay does not speak protocol v1;
its native state and terminal operations use `agents`, `blocked`, and
`pane_content` frames keyed by `pane_id`.

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

## Server To Client

### `agents`

Reports agent and host state. `_poll_once` broadcasts a complete snapshot every
poll, including an empty `agents` array when no agents remain. `event_push` can
also broadcast a partial, one-agent update after an `agent_event`; that form
omits `presets` and `hosts`. Clients must distinguish a complete poll snapshot
from a partial event update by the presence of `presets` and `hosts`.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"agents"`. |
| `agents` | array of agent objects | Required | Complete current list for a poll, or one partial agent for an event update. |
| `presets` | array of preset objects | Poll snapshots only | Public launch presets. |
| `hosts` | array of host objects | Poll snapshots only | Configured preset host availability; empty when preset host targets are not configured. |

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
| `host` | string | Required | Configured host ID, remote SSH target, or `"local"`; event updates default to `"local"`. |
| `remote` | string or null | Poll agents only | SSH target used by the relay, or `null` for a local pane. |
| `workspace_id` | string | Poll agents only | Workspace identifier reported by `herdr`; defaults to an empty string. |
| `tab_id` | string | Poll agents only | Tab identifier reported by `herdr`; defaults to an empty string. |
| `attention_state` | string | Optional | Additive explicit attention state: `"working"`, `"waiting"`, `"done"`, or `"idle"`. `"waiting"` means the agent is waiting on the user. Unknown statuses are omitted rather than guessed. |
| `updated_at` | integer | Optional | Additive epoch milliseconds when this pane's status or output revision last changed. |
| `output_revision` | integer | Optional | Additive monotonic per-pane output revision reported by `herdr`; omitted when unavailable or invalid. |

Each public preset has this shape:

| Preset field | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `id` | string | Required | Preset identifier. |
| `label` | string | Required | Display label from the preset file. |
| `repository` | string | Required | Preset repository string. |
| `agent` | string | Required | One of `"claude"`, `"opencode"`, or `"codex"`. |
| `model` | string | Required | Model configured for the preset. |
| `hosts` | object | Required | Map from host ID to a public host configuration. |
| `hosts.<host_id>.cwd` | string | Required | Absolute working directory used when launching on that host. |

The relay removes each preset host's private `target` field before broadcast.

| Host field | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `host_id` | string | Required | Configured host identifier. |
| `online` | boolean | Required | Whether `herdr pane list` succeeded for that host. |

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
      "remote": "deploy@buildbox",
      "workspace_id": "workspace-2",
      "tab_id": "tab-4"
    }
  ],
  "presets": [
    {
      "id": "review",
      "label": "Review",
      "repository": "dcolinmorgan/herdr-remote",
      "agent": "claude",
      "model": "default",
      "hosts": {
        "buildbox": {"cwd": "/srv/herdr-remote"}
      }
    }
  ],
  "hosts": [
    {"host_id": "buildbox", "online": true}
  ]
}
```

This frame is fan-out: it is broadcast to every connected WebSocket.

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
| `output_blocks` | array of output block objects | Optional | At most 200 recent structured Claude or OpenCode transcript blocks. |
| `attention_state` | string | Optional | Additive explicit attention state: `"working"`, `"waiting"`, `"done"`, or `"idle"`. `"waiting"` means the agent is waiting on the user. Unknown statuses are omitted rather than guessed. |
| `updated_at` | integer | Optional | Additive epoch milliseconds when this pane's status or output revision last changed. |
| `output_revision` | integer | Optional | Additive monotonic per-pane output revision reported by `herdr`; omitted when unavailable or invalid. |

These optional fields have the same values as the latest `agents` entry for the
pane. They are omitted rather than set to `null` when the relay cannot determine
them.

Every output block has `id` and `kind`. Fields after those depend on `kind`.

| Block field | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `id` | string | Required | Relay-generated block ID such as `b0` or `o0`. |
| `kind` | string | Required | `"assistant_text"`, `"status"`, or `"tool"`. |
| `markdown` | string | `assistant_text` only | Assistant response text. |
| `label` | string | `status` and `tool` only | Status source such as `"You"` or `"Thought"`, or tool name. |
| `text` | string | `status` and `tool` only | Status text or a one-line tool summary. |

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

### `command_ack`

Acknowledges a successful `launch_session`, `terminate_session`, `wake_host`,
or `shutdown_host` request. The frame is point-to-point. Responses are cached
by non-empty `request_id`; repeating a cached ID returns the cached frame before
the new frame's `type` is considered.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"command_ack"`. |
| `request_id` | string | Required | ID copied from the command. |
| `result` | object | Required | Command-specific result. |
| `result.host_id` | string | Launch, wake, and shutdown acknowledgements | Host ID from the command. |
| `result.output` | string | Terminate acknowledgement | Standard output from `herdr pane close`, stripped. |

Launch acknowledgement:

```json
{
  "type": "command_ack",
  "request_id": "req-launch-17",
  "result": {"host_id": "buildbox"}
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

Rejects one of the four session or host commands. It is point-to-point and is
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
  "request_id": "req-launch-17",
  "code": "UNKNOWN_PRESET",
  "message": "Unknown preset"
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

### `launch_session`

Starts an agent from a server-configured preset on an allowed host.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"launch_session"`. |
| `request_id` | string | Required | Non-empty idempotency and response-correlation key. |
| `preset_id` | string | Required | ID in the relay's configured presets. |
| `host_id` | string | Required | Host allowed by that preset. |

The preset controls the agent, model, working directory, and SSH target. The
client cannot supply command text. Unknown presets and disallowed hosts are
rejected.

```json
{
  "type": "launch_session",
  "request_id": "req-launch-17",
  "preset_id": "review",
  "host_id": "buildbox"
}
```

### `terminate_session`

Closes the pane mapped to an active native session key. Although this command
uses a field named `session_id`, it is not protocol v1. The relay creates this
internal key as `legacy:<host_id>:<pane_id>` from the latest poll.

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

Runs the relay's fixed Wake-on-LAN command for the one configured power host.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"wake_host"`. |
| `request_id` | string | Required | Non-empty idempotency and response-correlation key. |
| `host_id` | string | Required | Must equal `HERDR_POWER_HOST_ID`; `HERDR_POWER_HOST_MAC` must also be configured. |

```json
{
  "type": "wake_host",
  "request_id": "req-wake-19",
  "host_id": "buildbox"
}
```

### `shutdown_host`

Runs the fixed remote command `sudo -n systemctl poweroff` for the configured
power host. The host must also have a non-empty preset SSH target.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"shutdown_host"`. |
| `request_id` | string | Required | Non-empty idempotency and response-correlation key. |
| `host_id` | string | Required | Must equal `HERDR_POWER_HOST_ID`. |
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

Requests recent terminal output and, when available, structured blocks.

| Name | Type | Presence | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required | Always `"read_pane"`. |
| `pane_id` | string | Required | Must identify a pane from the latest poll. |
| `lines` | integer, or a string parseable as one | Optional | Line count for `herdr pane read --lines`. Defaults to `30`, floors at `1`, caps at `2000`. |

Anything unparseable, zero, or negative falls back to the default instead of
reaching herdr, which reports a bad `--lines` as an error string on stdout with
exit code 0 — a client that sent `"lines": "abc"` used to get
`Error: Custom { kind: Other, error: "invalid value for --lines: abc" }`
delivered as `pane_content.content`. The relay requests `--source recent`.

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
| `INVALID_REQUEST` | `request_id is required` | Any session or host command has a `request_id` that is not a non-empty string. The response's `request_id` is `null`. |
| `UNKNOWN_PRESET` | `Unknown preset` | `launch_session.preset_id` is not configured. |
| `HOST_NOT_ALLOWED` | `Preset is not allowed on this host` | `launch_session.host_id` is not in the selected preset. |
| `LAUNCH_FAILED` | `Herdr did not start the client` | The `herdr agent start` process fails or exits unsuccessfully. |
| `CONFIRMATION_REQUIRED` | `confirmation_nonce is required` | `terminate_session` or `shutdown_host` lacks a non-empty string nonce. |
| `STALE_SESSION` | `Session is no longer active` | `terminate_session.session_id` is absent from the latest active session map. |
| `TERMINATE_FAILED` | `Herdr did not terminate the client` | The `herdr pane close` process fails or exits unsuccessfully. |
| `HOST_NOT_ALLOWED` | `Power control is not allowed for this host` | `wake_host.host_id` is not the power host, its MAC is unconfigured, or `shutdown_host.host_id` is not the power host. |
| `WAKE_FAILED` | `Wake-on-LAN command failed` | The configured Wake-on-LAN process raises or exits unsuccessfully. |
| `UNKNOWN_HOST` | `Power host has no SSH target` | The shutdown power host has no truthy preset SSH target. |
| `SHUTDOWN_FAILED` | `Host shutdown command failed` | The fixed SSH shutdown process raises or exits unsuccessfully. |

## Source Of Truth

This reference was derived from `relay/herdr_relay.py`, especially
`handle_client`, `_poll_once`, `event_push`, `broadcast`, `process_request`,
`public_presets`, `get_agents_from_host`, `get_all_agents`, `pane_blocks`,
`transcript_to_blocks`, `opencode_to_blocks`, `command_error`,
`launch_session`, `terminate_session`, `wake_host`, and `shutdown_host`. Re-read
those functions when changing or re-verifying the native contract.
