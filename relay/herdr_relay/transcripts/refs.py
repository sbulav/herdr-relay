"""Validate Herdr's opaque agent-session references before they reach a store."""
import re


CLAUDE_SESSION_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
OPENCODE_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
MAX_PATH_LENGTH = 4096


def validated(agent, ref):
    """Return a harness-bound reference, or ``None`` for an unsafe shape."""
    if not isinstance(agent, str) or not isinstance(ref, dict):
        return None
    ref_agent = ref.get("agent")
    kind = ref.get("kind")
    value = ref.get("value")
    if ref_agent != agent or not isinstance(value, str) or not value:
        return None
    if agent == "claude":
        if kind == "id" and CLAUDE_SESSION_ID_RE.fullmatch(value):
            return {"agent": agent, "kind": kind, "value": value}
        if (
            kind == "path"
            and len(value) <= MAX_PATH_LENGTH
            and "\0" not in value
            and value.endswith(".jsonl")
        ):
            return {"agent": agent, "kind": kind, "value": value}
        return None
    if agent == "opencode" and kind == "id" and OPENCODE_SESSION_ID_RE.fullmatch(value):
        return {"agent": agent, "kind": kind, "value": value}
    return None


def from_pane(agent, ref):
    """Bind a raw pane-list reference to the harness that supplied it."""
    if not isinstance(ref, dict):
        return None
    candidate = {"agent": agent, "kind": ref.get("kind"), "value": ref.get("value")}
    declared_agent = ref.get("agent")
    if declared_agent is not None and declared_agent != agent:
        return None
    return validated(agent, candidate)
