"""The frames themselves: what the relay puts on the wire, and what it withholds."""
import time

from . import config, state


ACTIVITY_TITLE_MAX_CHARS = 160
DISPLAY_NAME_MAX_CHARS = 160

# Fields deliberately safe for native clients. Keep this list closed so a new
# Herdr/internal field cannot become public merely by passing through here.
PUBLIC_AGENT_FIELDS = frozenset({
    "pane_id", "host_id", "host", "agent", "label", "status", "cwd", "project",
    "workspace_id", "workspace_name", "tab_id", "tab_name", "activity_title",
    "attention_state", "updated_at", "output_revision",
})


def public_text(value, maximum=DISPLAY_NAME_MAX_CHARS):
    """Normalize bounded public display text, omitting empty/non-text values."""
    if not isinstance(value, str):
        return None
    text = " ".join(part for part in value.split() if part)
    text = "".join(char for char in text if char.isprintable())
    text = text[:maximum].rstrip()
    return text or None


def public_identifier(value):
    """Sanitize a public ID without truncating its identity."""
    if not isinstance(value, str):
        return None
    value = "".join(char for char in value if char.isprintable())
    return value or None


def server_info():
    """The first frame on every connection: who this relay is and what it requires.

    Per-connection rather than a field on the broadcast `agents` frame, so a client
    that is too old can block before it renders a single agent, and so these
    handshake values do not ride on every fan-out frame for the life of the protocol.
    """
    return {
        "type": "server_info",
        "relay_version": config.RELAY_VERSION,
        "min_client": config.MIN_CLIENT,
        "durable_start": True,
    }


def public_agents(agents):
    """Strip server-side routing state from agent entries before broadcasting.

    `remote` is the SSH `target` from the host configuration, which the relay
    resolves through `hosts.ssh_target()` and never broadcasts. No client addresses
    a pane by it (they use `host` and `pane_id`); the relay does, through
    `state.pane_remote_map`. Sending it would hand every connected phone, and every
    proxy log, a login string for the host.
    """
    result = []
    for agent in agents:
        # Keep this projection explicit: Herdr data and future internal
        # routing fields must not accidentally become public just because a
        # caller passed them through an event or test double.
        public = {
            key: value for key, value in agent.items() if key in PUBLIC_AGENT_FIELDS
        }
        for field in ("workspace_name", "tab_name", "activity_title"):
            if field in public:
                maximum = (
                    ACTIVITY_TITLE_MAX_CHARS
                    if field == "activity_title"
                    else DISPLAY_NAME_MAX_CHARS
                )
                value = public_text(public[field], maximum)
                if value is None:
                    public.pop(field, None)
                else:
                    public[field] = value
        for field in ("workspace_id", "tab_id"):
            if field in public:
                value = public_identifier(public[field])
                if value is None:
                    public.pop(field, None)
                else:
                    public[field] = value
        host_id = public.get("host_id") or public.get("host", "local")
        public["host_id"] = host_id
        result.append(public)
    return result


def session_id(host_id, pane_id):
    return f"legacy:{host_id}:{pane_id}"


def now_ms():
    return int(time.time() * 1000)


def attention_state(status, previous_status, previous_state=None):
    """Map herdr's agent_status to the client's attention vocabulary.

    Returns None for anything outside that vocabulary, including "unknown": the
    client treats an unrecognised value as no value and reads `status`
    conservatively, so omitting the key is better than guessing.
    """
    if status == "blocked":
        return "waiting"
    if status == "working":
        return "working"
    if status == "done":
        return "done"
    if status == "idle":
        # An agent that just stopped working finished a turn nobody has looked
        # at yet. The state stays "done" for as long as the pane stays idle,
        # rather than lasting a single poll: a client that connects a cycle
        # later must see the same thing as one that was already listening.
        if previous_status in ("working", "blocked") or previous_state == "done":
            return "done"
        return "idle"
    return None


def add_pane_metadata(entry, pane_id, host_id=None, status=None):
    host_id = host_id or entry.get("host_id") or entry.get("host")
    key = state.pane_key(host_id, pane_id)
    attention = state.get(state.pane_attention_states, key)
    if attention is not None:
        entry["attention_state"] = attention
    activity = state.get(state.pane_activity, key)
    if activity is not None:
        entry["updated_at"] = activity
    revision = state.get(state.pane_revisions, key)
    if isinstance(revision, int) and not isinstance(revision, bool):
        entry["output_revision"] = revision

    workspace = state.get(state.pane_workspace_map, key)
    if isinstance(workspace, tuple) and len(workspace) == 2:
        workspace_id, workspace_name = workspace
        workspace_id = public_identifier(workspace_id)
        if workspace_id:
            entry["workspace_id"] = workspace_id
        if isinstance(workspace_name, str) and workspace_name:
            entry["workspace_name"] = workspace_name
    tab = state.get(state.pane_tab_map, key)
    if isinstance(tab, tuple) and len(tab) == 2:
        tab_id, tab_name = tab
        tab_id = public_identifier(tab_id)
        if tab_id:
            entry["tab_id"] = tab_id
        if isinstance(tab_name, str) and tab_name:
            entry["tab_name"] = tab_name

    effective_status = status if isinstance(status, str) else entry.get("status")
    if not isinstance(effective_status, str):
        effective_status = state.get(state.pane_statuses, key)
    if not isinstance(effective_status, str):
        effective_status = state.get(state.last_statuses, key)
    title = state.get(state.pane_activity_titles, key)
    if effective_status == "working" and isinstance(title, str) and title:
        normalized = public_text(title, ACTIVITY_TITLE_MAX_CHARS)
        if normalized:
            entry["activity_title"] = normalized


def command_error(request_id, code, message):
    return {"type": "command_error", "request_id": request_id, "code": code, "message": message}


def command_ack(request_id, result=None):
    """Build a point-to-point acknowledgement for a typed command."""
    return {"type": "command_ack", "request_id": request_id, "result": result or {}}


def error(message):
    """A refusal in the dialect the pane commands speak.

    `respond`, `read_pane`, `send_keys` and `send_text` carry no `request_id`, so
    their refusals cannot name what they are refusing: all a client gets is a
    message. Kept here beside [command_error] so both dialects have one source
    and the contract golden pins the shape rather than one caller's wording (#63).
    """
    return {"type": "error", "message": message}
