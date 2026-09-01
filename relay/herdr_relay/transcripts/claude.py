"""Claude Code's own JSONL transcript, read straight from the session store.

Claude Code persists a fully structured transcript per project at
~/.claude/projects/<escaped-cwd>/<session-uuid>.jsonl. Reading it gives real
output blocks — assistant prose, tool calls, thinking, prompts — with none of the
ANSI, box-drawing and spinner guesswork that scraping a pane would need, and
without any change to `herdr` itself.
"""
import glob
import json
import os
import re
import shlex
import subprocess
import threading
from collections import OrderedDict
from datetime import datetime
import difflib

from .. import config


_PARSE_CACHE = OrderedDict()
_PARSE_CACHE_LOCK = threading.Lock()
_PARSE_CACHE_SIZE = 8
_TEXT_LIMIT = 4000
_RESULT_LIMIT = 4000
_DIFF_MAX_LINES = 80
_DIFF_MAX_CHARS = 12000


def _timestamp(value):
    """Return transcript timestamps in the integer epoch-millisecond wire form."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value if value > 10_000_000_000 else value * 1000)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(parsed.timestamp() * 1000)


def _metadata(row, role, message_id=None, turn_id=None):
    """Fields shared by all blocks, omitted for old/incomplete transcript rows."""
    fields = {"role": role}
    row_id = row.get("uuid")
    message = row.get("message")
    provider_message_id = message.get("id") if isinstance(message, dict) else None
    if isinstance(message_id, str) and message_id:
        fields["message_id"] = message_id
    elif isinstance(provider_message_id, str) and provider_message_id:
        fields["message_id"] = provider_message_id
    elif isinstance(row_id, str) and row_id:
        fields["message_id"] = row_id
    chosen_turn = turn_id or row.get("turnId") or row.get("requestId") or row.get("parentUuid")
    if isinstance(chosen_turn, str) and chosen_turn:
        fields["turn_id"] = chosen_turn
    stamp = _timestamp(row.get("timestamp"))
    if stamp is not None:
        fields["timestamp"] = stamp
    return fields


def _stable_id(row, index, child_id=None, prefix="b", fallback_index=None):
    row_id = row.get("uuid")
    if isinstance(row_id, str) and row_id:
        suffix = child_id if isinstance(child_id, str) and child_id else str(index)
        return f"{prefix}:{row_id}:{suffix}"
    return f"{prefix}{fallback_index if fallback_index is not None else index}"


def _diff(old, new):
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm=""))[2:]
    if not lines:
        return None
    clipped = len(lines) > _DIFF_MAX_LINES
    lines = lines[:_DIFF_MAX_LINES]
    text = "\n".join(lines)
    if len(text) > _DIFF_MAX_CHARS:
        text = text[:_DIFF_MAX_CHARS]
        clipped = True
    return text, clipped


def _edit_diff(name, args):
    if not isinstance(args, dict):
        return None
    path = args.get("file_path") or args.get("path") or "file"
    if not isinstance(path, str) or not path:
        path = "file"
    display_path = path.lstrip("/")
    headers = [f"--- a/{display_path}", f"+++ b/{display_path}"]
    if name == "Edit" and isinstance(args.get("old_string"), str):
        result = _diff(args["old_string"], args.get("new_string") or "")
        if result:
            text, clipped = result
            return "\n".join(headers + [text]), clipped
    if name == "MultiEdit" and isinstance(args.get("edits"), list):
        chunks = []
        for edit in args["edits"]:
            if not isinstance(edit, dict) or not isinstance(edit.get("old_string"), str):
                continue
            result = _diff(edit["old_string"], edit.get("new_string") or "")
            if result:
                if chunks:
                    chunks.append("...")
                chunks.append(result[0])
        if chunks:
            text = "\n".join(headers + chunks)
            return text[:_DIFF_MAX_CHARS], len(text) > _DIFF_MAX_CHARS
    if name == "Write" and isinstance(args.get("content"), str):
        lines = ["+" + line for line in args["content"].splitlines()]
        if not lines:
            return None
        clipped = len(lines) > _DIFF_MAX_LINES
        hunk = f"@@ -0,0 +1,{len(lines)} @@"
        text = "\n".join(headers + [hunk] + lines[:_DIFF_MAX_LINES])
        if len(text) > _DIFF_MAX_CHARS:
            text = text[:_DIFF_MAX_CHARS]
            clipped = True
        return text, clipped
    return None


def _local_transcript_path(path):
    """Resolve an explicit transcript only when it stays below the configured root."""
    expanded = os.path.abspath(os.path.expanduser(path))
    root = os.path.realpath(os.path.expanduser(config.CLAUDE_PROJECTS))
    resolved = os.path.realpath(expanded)
    try:
        contained = os.path.commonpath((root, resolved)) == root
    except ValueError:
        contained = False
    if not contained or os.path.islink(expanded) or not resolved.endswith(".jsonl"):
        return None
    return resolved

def claude_project_dir(cwd):
    """Escape a cwd the way Claude Code names its per-project transcript dir."""
    return re.sub(r"[/._]", "-", cwd)


def read_transcript(cwd, remote=None, path=None, max_bytes=None):
    """Return (path, jsonl_text) for the newest transcript in cwd, or (None, None).

    Reads only the trailing `config.TRANSCRIPT_MAX_BYTES` so a long session stays cheap
    to poll; the (possibly partial) first line is tolerated by the parser.
    """
    if not path and not cwd:
        return None, None
    byte_limit = max_bytes or config.TRANSCRIPT_MAX_BYTES
    if remote:
        if path:
            script = (
                'f=$1; root=$2; '
                'case "$f" in "~/"*) f="$HOME/${f#~/}" ;; esac; '
                'case "$root" in "~/"*) root="$HOME/${root#~/}" ;; esac; '
                'case "$f" in *.jsonl) ;; *) exit 0 ;; esac; '
                'root=$(cd -P "$root" 2>/dev/null && pwd -P) || exit 0; '
                'dir=$(cd -P "$(dirname "$f")" 2>/dev/null && pwd -P) || exit 0; '
                'case "$dir/" in "$root/"*) ;; *) exit 0 ;; esac; '
                'f="$dir/$(basename "$f")"; [ ! -L "$f" ] || exit 0; '
                '[ -f "$f" ] || exit 0; '
                'printf "%s\\n" "$f"; '
                f'tail -c {int(byte_limit)} "$f"'
            )
            remote_cmd = (
                "sh -c " + shlex.quote(script) + " sh "
                + shlex.quote(path) + " " + shlex.quote(config.CLAUDE_PROJECTS)
            )
        else:
            proj = claude_project_dir(cwd)
            root = config.CLAUDE_PROJECTS.replace("~", "$HOME")
            script = (
                f'd="{root}/$1"; '
                'f=$(ls -t "$d"/*.jsonl 2>/dev/null | head -1); '
                '[ -n "$f" ] || exit 0; '
                'printf "%s\\n" "$f"; '
                f'tail -c {int(byte_limit)} "$f"'
            )
            remote_cmd = "sh -c " + shlex.quote(script) + " sh " + shlex.quote(proj)
        try:
            r = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", remote, remote_cmd],
                capture_output=True, text=True, timeout=15)
        except Exception:
            return None, None
        if r.returncode != 0 or not r.stdout:
            return None, None
        path, _, body = r.stdout.partition("\n")
        return (path.strip() or None), body
    # local
    try:
        if path:
            path = _local_transcript_path(path)
            if path is None:
                return None, None
        else:
            proj = claude_project_dir(cwd)
            d = os.path.join(os.path.expanduser(config.CLAUDE_PROJECTS), proj)
            files = glob.glob(os.path.join(d, "*.jsonl"))
            if not files:
                return None, None
            path = max(files, key=os.path.getmtime)
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - int(byte_limit)))
            body = fh.read().decode("utf-8", "replace")
        return path, body
    except Exception:
        return None, None


def summarize_tool(inp):
    """Pick the most descriptive single line from a tool_use input dict."""
    if not isinstance(inp, dict):
        return ""
    for key in ("file_path", "filePath", "command", "pattern", "path", "url", "query", "description", "prompt"):
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().splitlines()[0][:200]
    return ""


def transcript_to_blocks(jsonl_text, limit=config.TRANSCRIPT_BLOCK_LIMIT):
    """Map a Claude Code JSONL transcript into a list of OutputBlock dicts."""
    blocks = []
    tool_blocks = {}
    seen_rows = set()

    def add(kind, row, index, child_id=None, **kw):
        kw["id"] = _stable_id(row, index, child_id, fallback_index=len(blocks))
        kw["kind"] = kind
        blocks.append(kw)
        return kw

    for line in jsonl_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue  # partial first line or non-JSON meta
        if not isinstance(rec, dict) or rec.get("isMeta") or rec.get("isSidechain"):
            continue
        row_id = rec.get("uuid")
        if isinstance(row_id, str) and row_id:
            if row_id in seen_rows:
                continue
            seen_rows.add(row_id)
        rtype = rec.get("type")
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        if rtype == "assistant":
            content = msg.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            for content_index, b in enumerate(content or []):
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text" and (b.get("text") or "").strip():
                    add(
                        "assistant_text", rec, content_index,
                        markdown=b["text"], **_metadata(rec, "assistant"),
                    )
                elif bt == "thinking" and (b.get("thinking") or "").strip():
                    add(
                        "status", rec, content_index,
                        label="Thought", text=b["thinking"].strip().splitlines()[0][:200],
                        **_metadata(rec, "reasoning"),
                    )
                elif bt == "tool_use":
                    name = b.get("name") or "tool"
                    args = b.get("input")
                    summary = summarize_tool(args)
                    diff = _edit_diff(name, args)
                    fields = _metadata(rec, "tool")
                    if diff:
                        diff_text, clipped = diff
                        fields.update({
                            "label": name,
                            "text": summary,
                            "markdown": diff_text,
                            "tool": name,
                            "diff_revision": f"{b.get('id') or rec.get('uuid') or len(blocks)}",
                        })
                        if clipped:
                            fields["diff_clipped"] = True
                        block = add("diff", rec, content_index, b.get("id"), **fields)
                    else:
                        block = add(
                            "tool", rec, content_index, b.get("id"),
                            label=name, text=summary, tool=name, **fields,
                        )
                    tool_id = b.get("id")
                    if isinstance(tool_id, str) and tool_id:
                        tool_blocks[tool_id] = block
        elif rtype == "user":
            content = msg.get("content")
            if isinstance(content, str):
                t = content.strip()
                if t and not t.startswith("<command-") and "<command-name>" not in t:
                    add("status", rec, 0, label="You", text=t[:2000], **_metadata(rec, "user"))
            elif isinstance(content, list):
                spoken = []
                spoken_index = 0
                for index, item in enumerate(content):
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text" and isinstance(item.get("text"), str):
                        if item["text"].strip():
                            if not spoken:
                                spoken_index = index
                            spoken.append(item["text"].strip())
                    elif item.get("type") == "tool_result":
                        tool = tool_blocks.get(item.get("tool_use_id"))
                        body = item.get("content")
                        if isinstance(body, list):
                            body = "\n".join(
                                p.get("text", "") for p in body
                                if isinstance(p, dict) and isinstance(p.get("text"), str)
                            )
                        if not isinstance(body, str):
                            body = ""
                        body = body.strip()
                        if tool is not None and body:
                            tool["result"] = body[:_RESULT_LIMIT]
                            tool["result_truncated"] = len(body) > _RESULT_LIMIT
                            marker = "!" if item.get("is_error") else "→"
                            preview = body.splitlines()[0][:200]
                            tool["text"] = f"{tool.get('text', '')} {marker} {preview}"[:200]
                            if item.get("is_error"):
                                tool["error"] = True
                        elif body or item.get("is_error"):
                            # A tail-bounded transcript can contain a result after its tool_use
                            # fell outside the window. Keep that evidence as a standalone block
                            # instead of silently losing the agent's response.
                            preview = body.splitlines()[0][:200] if body else "error"
                            result_id = item.get("tool_use_id")
                            orphan = add(
                                "tool", rec, index, f"result:{result_id}" if result_id else None,
                                label="Tool result", text=preview, result=body[:_RESULT_LIMIT],
                                result_truncated=len(body) > _RESULT_LIMIT,
                                **_metadata(rec, "tool"),
                            )
                            if item.get("is_error"):
                                orphan["error"] = True
                if spoken:
                    add(
                        "status", rec, spoken_index, label="You", text="\n".join(spoken)[:2000],
                        **_metadata(rec, "user"),
                    )
    return blocks[-limit:]


def cached_transcript_to_blocks(path, jsonl_text, limit=config.TRANSCRIPT_BLOCK_LIMIT):
    """Parse a transcript once per path/content fingerprint, with bounded eviction."""
    key = (path or "", len(jsonl_text), hash(jsonl_text), int(limit))
    with _PARSE_CACHE_LOCK:
        cached = _PARSE_CACHE.get(key)
        if cached is not None:
            _PARSE_CACHE.move_to_end(key)
            return cached
    parsed = transcript_to_blocks(jsonl_text, limit=limit)
    with _PARSE_CACHE_LOCK:
        _PARSE_CACHE[key] = parsed
        _PARSE_CACHE.move_to_end(key)
        while len(_PARSE_CACHE) > _PARSE_CACHE_SIZE:
            _PARSE_CACHE.popitem(last=False)
    return parsed


def clear_parse_cache():
    with _PARSE_CACHE_LOCK:
        _PARSE_CACHE.clear()
