"""Which store a pane's transcript comes from, and whether it may be streamed.

The guard here is the interesting part: without a session ref from herdr, cwd is
the only correlation available, so two panes running the same agent in the same
directory must stream nothing rather than each other's output.
"""
import json
import os

from .. import config, state
from . import claude, opencode, refs

def _transcript(pane_id, block_limit=config.TRANSCRIPT_BLOCK_LIMIT, max_bytes=None):
    """Resolve and parse one pane transcript, keeping the security checks in one place."""
    info = state.pane_cwd_map.get(pane_id)
    if not info:
        return None, None, None
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
        return None, None, None
    if agent == "claude":
        try:
            if usable_ref and usable_ref["kind"] == "path":
                path, body = claude.read_transcript(cwd, remote, path=usable_ref["value"], max_bytes=max_bytes)
            elif usable_ref:
                transcript_path = os.path.join(
                    config.CLAUDE_PROJECTS, claude.claude_project_dir(cwd), usable_ref["value"] + ".jsonl"
                ) if cwd else None
                path, body = claude.read_transcript(cwd, remote, path=transcript_path, max_bytes=max_bytes)
            else:
                path, body = claude.read_transcript(cwd, remote, max_bytes=max_bytes)
        except Exception:
            return None, None, None
        if not body:
            return None, None, None
        blocks = claude.cached_transcript_to_blocks(path, body, limit=block_limit)
        return blocks, hash((path, len(body), hash(body))), {"source": path, "truncated": len(body) >= int(max_bytes or config.TRANSCRIPT_MAX_BYTES)}
    document = opencode.read_opencode(
        cwd, remote, session_id=usable_ref["value"] if usable_ref else None
    )
    if not document:
        return None, None, None
    blocks = opencode.opencode_to_blocks(document, limit=block_limit)
    return blocks, hash(json.dumps(document, sort_keys=True)), {"source": document.get("session_id"), "truncated": False}


def pane_blocks(pane_id):
    """(blocks, signature) for a pane's transcript, else (None, None)."""
    blocks, signature, _meta = _transcript(pane_id)
    return blocks, signature


def paginate_blocks(blocks, limit=config.TRANSCRIPT_BLOCK_LIMIT,
                    before=None, max_bytes=config.TRANSCRIPT_PAGE_MAX_BYTES):
    """Return an oldest-first page walking backwards from ``before``.

    Cursors are block IDs, so append-only transcript growth does not invalidate a caller's
    position. The byte budget is measured on UTF-8 JSON and always yields one block when the
    newest block itself exceeds the configured budget.
    """
    if not isinstance(blocks, list):
        return [], 0, False, None
    try:
        limit = max(1, min(int(limit), config.TRANSCRIPT_HISTORY_BLOCK_LIMIT))
    except (TypeError, ValueError):
        limit = config.TRANSCRIPT_BLOCK_LIMIT
    try:
        max_bytes = max(1, min(int(max_bytes), config.TRANSCRIPT_PAGE_MAX_BYTES))
    except (TypeError, ValueError):
        max_bytes = config.TRANSCRIPT_PAGE_MAX_BYTES
    end = len(blocks)
    if isinstance(before, str) and before:
        found = False
        for index, block in enumerate(blocks):
            if isinstance(block, dict) and block.get("id") == before:
                end = index
                found = True
                break
        if not found:
            # A cursor from another session or a rewritten transcript must not
            # silently restart at the newest page, or a client can loop forever.
            return [], len(blocks), False, None
    start = end
    used = 0
    while start > 0 and end - start < limit:
        block = blocks[start - 1]
        cost = len(json.dumps(block, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if start < end and used + cost > max_bytes:
            break
        used += cost
        start -= 1
    page = blocks[start:end]
    has_more = start > 0
    return page, len(blocks), has_more, (page[0].get("id") if has_more and page else None)


def pane_block_page(pane_id, limit=config.TRANSCRIPT_BLOCK_LIMIT, before=None,
                    max_bytes=config.TRANSCRIPT_PAGE_MAX_BYTES):
    """Return a bounded transcript page and pagination metadata for native history clients."""
    blocks, signature, meta = _transcript(
        pane_id,
        block_limit=config.TRANSCRIPT_HISTORY_BLOCK_LIMIT,
        max_bytes=config.TRANSCRIPT_HISTORY_MAX_BYTES,
    )
    if blocks is None:
        return None, signature, {"total": 0, "has_more": False, "next_cursor": None, **(meta or {})}
    page, total, has_more, next_cursor = paginate_blocks(blocks, limit, before, max_bytes)
    return page, signature, {
        "total": total,
        "has_more": has_more,
        "next_cursor": next_cursor,
        **(meta or {}),
    }
