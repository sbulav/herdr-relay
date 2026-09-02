"""The frames themselves: what the relay puts on the wire, and what it withholds."""
import time

from . import config, state

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
        public = {key: value for key, value in agent.items() if key not in {"remote", "agent_name"}}
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


def add_pane_metadata(entry, pane_id, host_id=None):
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
