"""OpenCode's message parts, read out of its SQLite session store.

Same idea as the Claude path next door: the parts are already structured, so no
pane scraping. Remote hosts run a small stdin-free python3 one-liner over SSH
because the database is on the far side.
"""
import json
import os
import shlex
import sqlite3
import subprocess

from .. import config
from . import claude

OPENCODE_PART_QUERY = """
SELECT json_extract(m.data, '$.role'), m.id, m.time_created, p.id, p.data
FROM message m
JOIN part p ON p.message_id = m.id
WHERE m.session_id = ?
ORDER BY m.time_created DESC, p.time_created DESC, p.id DESC
LIMIT ?
"""


def _read_opencode_local(db_path, cwd, session_id=None):
    """Return the newest top-level OpenCode session and its recent parts."""
    db_uri = "file:" + os.path.expanduser(db_path) + "?mode=ro"
    db = sqlite3.connect(db_uri, uri=True, timeout=2)
    try:
        if session_id:
            session = db.execute(
                "SELECT id, time_updated FROM session WHERE id = ?", (session_id,)
            ).fetchone()
        else:
            session = db.execute(
                "SELECT id, time_updated FROM session "
                "WHERE directory = ? AND parent_id IS NULL "
                "ORDER BY time_updated DESC LIMIT 1", (cwd,)
            ).fetchone()
        if not session:
            return None
        session_id, updated = session
        rows = db.execute(
            OPENCODE_PART_QUERY, (session_id, config.TRANSCRIPT_BLOCK_LIMIT * 4)
        ).fetchall()
    finally:
        db.close()
    rows.reverse()
    return {"session_id": session_id, "updated": updated, "rows": rows}


def read_opencode(cwd, remote=None, session_id=None):
    """Read bounded structured parts for the newest OpenCode session in cwd."""
    if not cwd and not session_id:
        return None
    if not remote:
        try:
            return _read_opencode_local(config.OPENCODE_DB, cwd, session_id)
        except Exception:
            return None
    script = """
import json, os, sqlite3, sys
db = sqlite3.connect("file:" + os.path.expanduser(sys.argv[1]) + "?mode=ro", uri=True, timeout=2)
if sys.argv[5]:
    session = db.execute("SELECT id, time_updated FROM session WHERE id = ?", (sys.argv[5],)).fetchone()
else:
    session = db.execute("SELECT id, time_updated FROM session WHERE directory = ? AND parent_id IS NULL ORDER BY time_updated DESC LIMIT 1", (sys.argv[2],)).fetchone()
if session:
    rows = db.execute(sys.argv[3], (session[0], int(sys.argv[4]))).fetchall()
    rows.reverse()
    print(json.dumps({"session_id": session[0], "updated": session[1], "rows": rows}))
"""
    remote_cmd = " ".join([
        "python3", "-c", shlex.quote(script), shlex.quote(config.OPENCODE_DB),
        shlex.quote(cwd), shlex.quote(OPENCODE_PART_QUERY),
        str(config.TRANSCRIPT_BLOCK_LIMIT * 4), shlex.quote(session_id or ""),
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
