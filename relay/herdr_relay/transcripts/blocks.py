"""Which store a pane's transcript comes from, and whether it may be streamed.

The guard here is the interesting part: without a session ref from herdr, cwd is
the only correlation available, so two panes running the same agent in the same
directory must stream nothing rather than each other's output.
"""
import json
import os

from .. import config, state
from . import claude, opencode, refs

def pane_blocks(pane_id):
    """(blocks, signature) for a Claude pane's transcript, else (None, None)."""
    info = state.pane_cwd_map.get(pane_id)
    if not info:
        return None, None
    cwd, agent, remote, ambiguous = info
    ref_key = (remote, pane_id)
    ref = state.pane_session_refs.get(ref_key)
    usable_ref = refs.validated(agent, ref)
    # A session ref correlates a pane directly; without one cwd remains the
    # fallback and ambiguous same-agent panes must not stream each other's output.
    if (
        agent not in ("claude", "opencode")
        or (ref_key in state.pane_session_refs and not usable_ref)
        or (ref_key not in state.pane_session_refs and (not cwd or ambiguous))
    ):
        return None, None
    if agent == "claude":
        try:
            if usable_ref and usable_ref["kind"] == "path":
                path, body = claude.read_transcript(cwd, remote, path=usable_ref["value"])
            elif usable_ref:
                transcript_path = os.path.join(
                    config.CLAUDE_PROJECTS, claude.claude_project_dir(cwd), usable_ref["value"] + ".jsonl"
                ) if cwd else None
                path, body = claude.read_transcript(cwd, remote, path=transcript_path)
            else:
                path, body = claude.read_transcript(cwd, remote)
        except Exception:
            return None, None
        if not body:
            return None, None
        return claude.transcript_to_blocks(body), hash((path, body))
    document = opencode.read_opencode(
        cwd, remote, session_id=usable_ref["value"] if usable_ref else None
    )
    if not document:
        return None, None
    blocks = opencode.opencode_to_blocks(document)
    return blocks, hash(json.dumps(document, sort_keys=True))
