"""OpenCode's message parts, read out of its SQLite session store.

Same idea as the Claude path next door: the parts are already structured, so no
pane scraping. Remote hosts run the same reader as a stdin-free python3
one-liner over SSH because the database is on the far side.
"""
import inspect
import json
import os
import shlex
import sqlite3
import subprocess

from .. import config
from . import claude


def read_session(db, cwd, session_id, limit):
    """The newest top-level OpenCode session in cwd (or session_id) and its last parts.

    Rows are ``[role, message_id, time_created, part_id, part]``, oldest first.
    OpenCode 2.x keeps sessions in ``session_v2`` and each message's parts in
    its ``data`` (a user message has only ``text``; an assistant one a
    ``content`` list), or from 2.2.2 in ``session_part`` rows, which win when a
    message has any. An upgraded install keeps the 1.x ``session``/``message``/
    ``part`` tables but never writes them again, so they are read only when the
    2.x ones are missing.

    The source of this function is also the script remote hosts run, so it
    may use nothing but its arguments and the standard library.
    """
    import json

    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    v2 = "session_v2" in tables
    session_table = "session_v2" if v2 else "session"
    if session_id:
        session = db.execute(
            "SELECT id, time_updated FROM " + session_table + " WHERE id = ?", (session_id,)
        ).fetchone()
    else:
        session = db.execute(
            "SELECT id, time_updated FROM " + session_table + " "
            "WHERE directory = ? AND parent_id IS NULL "
            "ORDER BY time_updated DESC LIMIT 1", (cwd,)
        ).fetchone()
    if not session:
        return None
    session_id, updated = session

    if not v2:
        rows = [list(row) for row in db.execute(
            "SELECT json_extract(m.data, '$.role'), m.id, m.time_created, p.id, p.data "
            "FROM message m JOIN part p ON p.message_id = m.id "
            "WHERE m.session_id = ? "
            "ORDER BY m.time_created DESC, p.time_created DESC, p.id DESC LIMIT ?",
            (session_id, limit),
        )]
        rows.reverse()
        return {"session_id": session_id, "updated": updated, "rows": rows}

    # Each message holds several parts, so the newest `limit` messages always
    # cover the newest `limit` parts.
    recent = (
        "SELECT id FROM session_message WHERE session_id = ? AND type IN ('user', 'assistant') "
        "ORDER BY seq DESC LIMIT ?"
    )
    messages = db.execute(
        "SELECT id, type, time_created, data FROM session_message WHERE id IN (" + recent + ") "
        "ORDER BY seq", (session_id, limit),
    ).fetchall()
    stored_parts = {}
    if "session_part" in tables:
        for message_id, data in db.execute(
            "SELECT message_id, data FROM session_part "
            "WHERE session_id = ? AND message_id IN (" + recent + ") "
            "ORDER BY message_id, ordinal", (session_id, session_id, limit),
        ):
            stored_parts.setdefault(message_id, []).append(data)

    def decode(value):
        try:
            value = json.loads(value) if isinstance(value, str) else value
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    rows = []
    for message_id, role, created, raw in messages:
        data = decode(raw) or {}
        if role == "user":
            parts = [{"type": "text", "text": data.get("text")}]
        elif message_id in stored_parts:
            parts = [decode(part) for part in stored_parts[message_id]]
        else:
            parts = data.get("content") if isinstance(data.get("content"), list) else []
        for ordinal, part in enumerate(parts):
            if not isinstance(part, dict):
                continue
            # A tool keeps its call id wherever its part is stored; everything
            # else is addressed by its place in the message.
            call_id = part.get("id") if part.get("type") == "tool" else None
            part_id = call_id if isinstance(call_id, str) and call_id else message_id + ":" + str(ordinal)
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            # Only what the blocks show crosses SSH; a tool's output can be large.
            rows.append([role, message_id, created, part_id, {
                "type": part.get("type"), "text": part.get("text"),
                "tool": part.get("name"), "state": {"input": state.get("input")},
            }])
    return {"session_id": session_id, "updated": updated, "rows": rows[-limit:]}


def _read_opencode_local(db_path, cwd, session_id=None):
    db_uri = "file:" + os.path.expanduser(db_path) + "?mode=ro"
    db = sqlite3.connect(db_uri, uri=True, timeout=2)
    try:
        return read_session(db, cwd, session_id, config.TRANSCRIPT_BLOCK_LIMIT * 4)
    finally:
        db.close()


REMOTE_SCRIPT = (
    "import json, os, sqlite3, sys\n"
    + inspect.getsource(read_session)
    + "\ndb = sqlite3.connect('file:' + os.path.expanduser(sys.argv[1]) + '?mode=ro', uri=True, timeout=2)\n"
    "document = read_session(db, sys.argv[2], sys.argv[4], int(sys.argv[3]))\n"
    "if document:\n"
    "    print(json.dumps(document))\n"
)


def read_opencode(cwd, remote=None, session_id=None):
    """Read bounded structured parts for the newest OpenCode session in cwd."""
    if not cwd and not session_id:
        return None
    if not remote:
        try:
            return _read_opencode_local(config.OPENCODE_DB, cwd, session_id)
        except Exception:
            return None
    remote_cmd = " ".join([
        "python3", "-c", shlex.quote(REMOTE_SCRIPT), shlex.quote(config.OPENCODE_DB),
        shlex.quote(cwd or ""), str(config.TRANSCRIPT_BLOCK_LIMIT * 4), shlex.quote(session_id or ""),
    ])
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", remote, remote_cmd],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return json.loads(result.stdout)
    except Exception:
        return None


def opencode_to_blocks(document, limit=config.TRANSCRIPT_BLOCK_LIMIT):
    """Map OpenCode message parts into OutputBlock dictionaries."""
    if not isinstance(document, dict):
        return []
    blocks = []

    def add(kind, **kw):
        stable_id = kw.pop("_stable_id", None)
        kw = {key: value for key, value in kw.items() if value is not None}
        kw["id"] = stable_id or f"o{len(blocks)}"
        kw["kind"] = kind
        blocks.append(kw)

    for row in document.get("rows") or []:
        if not isinstance(row, (list, tuple)) or len(row) not in (2, 5):
            continue
        if len(row) == 5:
            role, message_id, timestamp, part_id, raw_part = row
        else:
            role, raw_part = row
            message_id = timestamp = part_id = None
        timestamp = claude._timestamp(timestamp)
        try:
            part = json.loads(raw_part) if isinstance(raw_part, str) else raw_part
        except Exception:
            continue
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        text = part.get("text")
        if role == "user" and part_type == "text" and isinstance(text, str) and text.strip():
            add("status", label="You", text=text.strip()[:2000], role="user",
                message_id=message_id, timestamp=timestamp,
                _stable_id=f"o:{part_id}" if part_id else None)
        elif role == "assistant" and part_type == "text" and isinstance(text, str) and text.strip():
            add("assistant_text", markdown=text, role="assistant", message_id=message_id,
                timestamp=timestamp, _stable_id=f"o:{part_id}" if part_id else None)
        elif role == "assistant" and part_type == "reasoning" and isinstance(text, str) and text.strip():
            add("status", label="Thought", text=text.strip().splitlines()[0][:200], role="reasoning",
                message_id=message_id, timestamp=timestamp,
                _stable_id=f"o:{part_id}" if part_id else None)
        elif role == "assistant" and part_type == "tool":
            tool_state = part.get("state") if isinstance(part.get("state"), dict) else {}
            # OpenCode tool parts carry the same input dict shape as Claude's tool_use.
            summary = claude.summarize_tool(tool_state.get("input")) or str(tool_state.get("title") or "")[:200]
            add("tool", label=part.get("tool") or "tool", text=summary, role="tool",
                message_id=message_id, timestamp=timestamp,
                _stable_id=f"o:{part_id}" if part_id else None)
    return blocks[-limit:]
