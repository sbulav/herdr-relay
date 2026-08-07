"""The frames themselves: what the relay puts on the wire, and what it withholds."""
import time

from . import config, state

def server_info():
    """The first frame on every connection: who this relay is and what it requires.

    Per-connection rather than a field on the broadcast `agents` frame, so a client
    that is too old can block before it renders a single agent, and so these two
    values do not ride on every fan-out frame for the life of the protocol.
    """
    return {"type": "server_info", "relay_version": config.RELAY_VERSION, "min_client": config.MIN_CLIENT}


def public_agents(agents):
    """Strip server-side routing state from agent entries before broadcasting.

    `remote` is the preset's SSH `target` — the same value `presets.public_presets()`
    deliberately withholds. No client addresses a pane by it (they use `host` and
    `pane_id`); the relay does, through `state.pane_remote_map`. Sending it would hand
    every connected phone, and every proxy log, a login string for the host.
    """
    return [
        {key: value for key, value in agent.items() if key not in {"remote", "agent_name"}}
        for agent in agents
    ]


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


def add_pane_metadata(entry, pane_id):
    attention = state.pane_attention_states.get(pane_id)
    if attention is not None:
        entry["attention_state"] = attention
    if pane_id in state.pane_activity:
        entry["updated_at"] = state.pane_activity[pane_id]
    revision = state.pane_revisions.get(pane_id)
    if isinstance(revision, int) and not isinstance(revision, bool):
        entry["output_revision"] = revision



def command_error(request_id, code, message):
    return {"type": "command_error", "request_id": request_id, "code": code, "message": message}
