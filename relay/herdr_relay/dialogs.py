"""Relay-owned identity and capabilities for blocked-agent dialogs.

The terminal text is only an observation.  A response must be tied to the
particular observed prompt that produced it, otherwise a delayed phone tap can
answer a different question after the agent has redrawn its screen.
"""
import hashlib
import json

from . import panes, state


def _prompt_key(prompt, choices, *, agent, project, host):
    return json.dumps(
        # Agent/project labels are display metadata and may be absent on a
        # pushed hook event. Host plus pane identity (used by the caller) and
        # the full prompt/choices define the answerable observation.
        [host, prompt, choices],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _valid_observation(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def ensure(
    pane_id, prompt, choices, *, agent="", project="", host="local", observation=None
):
    """Return the current dialog, creating a new revision when it changed.

    Dialog IDs remain stable while the observed prompt and choices remain the
    same.  Counters survive pane cleanup so a pane ID reused by a later agent
    cannot accidentally receive an old dialog ID.
    """
    display_prompt = (prompt or "")[:500]
    # An undetected prompt has no relay-verified answer.  The legacy wire can
    # still display its historical fallback, but typed responses must not turn
    # that fallback into a claimed capability.
    normalized_choices = list(choices or [])
    observation = _valid_observation(observation)
    prompt_key = _prompt_key(
        prompt or "", normalized_choices, agent=agent, project=project, host=host
    )
    key = state.pane_key(host, pane_id)
    current = state.get(state.pane_dialogs, key)
    if current is not None and current["prompt_key"] == prompt_key:
        current_observation = _valid_observation(current.get("observation"))
        observation_changed = (
            current_observation is not None
            and observation is not None
            and current_observation != observation
        )
        if not observation_changed or not current["consumed"]:
            # Do not treat a missing output revision as evidence that a new
            # prompt appeared. In particular, a consumed dialog remains
            # consumed until a concrete revision proves a new observation.
            if not current["consumed"] and observation is not None:
                current["observation"] = observation
            if not current["agent"] and agent:
                current["agent"] = agent
            if not current["project"] and project:
                current["project"] = project
            return current

    revision = state.get(state.pane_dialog_revisions, key, 0) + 1
    state.pane_dialog_revisions[key] = revision
    digest = hashlib.sha256(f"{pane_id}\0{revision}\0{prompt_key}".encode("utf-8")).hexdigest()[:24]
    dialog = {
        "dialog_id": f"dlg-{digest}",
        "revision": revision,
        "pane_id": pane_id,
        "prompt": display_prompt,
        "prompt_key": prompt_key,
        "observation": observation,
        "choices": normalized_choices,
        # The relay currently only supports allowlisted labels.  It must not
        # claim that arbitrary text can be delivered to a blocked TUI.
        "raw_input_allowed": False,
        "agent": agent,
        "project": project,
        "host": host,
        "consumed": False,
        "response_in_flight": False,
    }
    state.pane_dialogs[key] = dialog
    legacy_choices = normalized_choices or panes.TOOL_OPTIONS
    state.pane_response_options[key] = {choice.lower() for choice in legacy_choices}
    return dialog


def frame(dialog, *, reduced=False):
    """Build a wire frame from one dialog state for every emission path."""
    result = {
        "type": "blocked",
        "pane_id": dialog["pane_id"],
        "prompt": dialog["prompt"],
        # `options` is the existing native field; `choices` is the explicit
        # dialog-owned name used by new clients.
        "options": list(dialog["choices"] or panes.TOOL_OPTIONS),
        "choices": list(dialog["choices"]),
        "dialog_id": dialog["dialog_id"],
        "revision": dialog["revision"],
        "raw_input_allowed": dialog["raw_input_allowed"],
        "host_id": dialog.get("host", "local"),
    }
    if not reduced:
        result.update({
            "agent": dialog["agent"],
            "project": dialog["project"],
            "host": dialog["host"],
        })
    return result


def clear(pane_id):
    """Forget active dialog and choices when a pane is no longer blocked."""
    if isinstance(pane_id, tuple):
        key = pane_id
        pane_id = key[1]
    else:
        key = pane_id
    state.pop(state.pane_dialogs, key)
    state.pop(state.pane_response_options, key)


def response_allowed(dialog, text):
    """Check a response against this exact dialog's choices.

    Legacy ``respond`` retains its global safe-response fallback. The typed
    command deliberately does not: even a globally safe ``yes`` is stale when
    the current dialog only offers a different choice set.
    """
    normalized = text.strip().lower()
    return normalized in {
        choice.lower() for choice in dialog["choices"]
    }
