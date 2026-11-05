"""Codex's rollout JSONL, read straight from its session store.

Codex records each session as `<sessions>/YYYY/MM/DD/rollout-<time>-<uuid>.jsonl`
(older releases wrote them flat under `<sessions>`). A rollout mirrors every turn
twice — as a canonical `response_item` and as streaming `event_msg` rows — so only
the `response_item` rows are read: user and assistant messages, tool calls, and
the outputs that answer them by `call_id`. Reasoning, developer and system text,
and session metadata are not turns and never become blocks.

The rollout is found from herdr's session ref alone, never from a pane's cwd or a
client-supplied path.
"""
import glob
import hashlib
import json
import os
import re
import shlex
import subprocess

from .. import config
from . import claude

_RESULT_LIMIT = 4000
# Context Codex injects as user messages; the reader typed none of it.
_INJECTED_PREFIXES = ("<environment_context>", "<user_instructions>", "# AGENTS.md instructions")
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.M)
# The header newer shell tools put above the command output.
_SHELL_HEADER_RE = re.compile(
    r"(?:Chunk ID|Wall time|Process exited with code|Process running with session ID"
    r"|Exit code|Original token count|Total output lines)\b"
)
_EXIT_RE = re.compile(r"(?:Process exited with code|Exit code:)\s*(-?\d+)")


def sessions_root():
    """The local sessions root: `HERDR_CODEX_SESSIONS`, else Codex's own default."""
    if config.CODEX_SESSIONS:
        return config.CODEX_SESSIONS
    return os.path.join(os.environ.get("CODEX_HOME") or "~/.codex", "sessions")


def _local_rollout(session_id):
    """The newest rollout for a session that stays below the root, else None."""
    root = os.path.realpath(os.path.expanduser(sessions_root()))
    found = []
    for prefix in (os.path.join("*", "*", "*"), ""):
        pattern = os.path.join(glob.escape(root), prefix, f"rollout-*-{session_id}.jsonl")
        for path in glob.glob(pattern):
            resolved = os.path.realpath(path)
            try:
                contained = os.path.commonpath((root, resolved)) == root
            except ValueError:
                contained = False
            if contained and not os.path.islink(path) and os.path.isfile(resolved):
                found.append(resolved)
    return max(found, key=os.path.getmtime) if found else None


# An empty root means the remote user's own Codex default. The newest match is
# taken, then held to the same rules as a local one: no link, and below the root.
_REMOTE_SCRIPT = (
    'id=$1; root=$2; '
    '[ -n "$root" ] || root="${CODEX_HOME:-$HOME/.codex}/sessions"; '
    'case "$root" in "~/"*) root="$HOME/${root#~/}" ;; esac; '
    'root=$(cd -P "$root" 2>/dev/null && pwd -P) || exit 0; '
    'f=$(ls -t "$root"/*/*/*/rollout-*-"$id".jsonl "$root"/rollout-*-"$id".jsonl 2>/dev/null | head -1); '
    '[ -n "$f" ] || exit 0; '
    '[ ! -L "$f" ] || exit 0; '
    'dir=$(cd -P "$(dirname "$f")" 2>/dev/null && pwd -P) || exit 0; '
    'case "$dir/" in "$root/"*) ;; *) exit 0 ;; esac; '
    'f="$dir/$(basename "$f")"; '
    '[ -f "$f" ] || exit 0; '
    'printf "%s\\n" "$f"; '
)


def read_rollout(session_id, remote=None, max_bytes=None):
    """Return (path, jsonl_text) for a session's rollout tail, or (None, None).

    `session_id` must already be a validated UUID (see `refs.validated`).
    """
    byte_limit = int(max_bytes or config.TRANSCRIPT_MAX_BYTES)
    if remote:
        script = _REMOTE_SCRIPT + f'tail -c {byte_limit} "$f"'
        remote_cmd = (
            "sh -c " + shlex.quote(script) + " sh "
            + shlex.quote(session_id) + " " + shlex.quote(config.CODEX_SESSIONS)
        )
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
    try:
        path = _local_rollout(session_id)
        if path is None:
            return None, None
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - byte_limit))
            body = fh.read().decode("utf-8", "replace")
        return path, body
    except Exception:
        return None, None


def _text(content):
    """Join a message's text parts; images and unknown parts are left out."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        part["text"] for part in content
        if isinstance(part, dict) and part.get("type") in ("input_text", "output_text")
        and isinstance(part.get("text"), str) and part["text"]
    )


def _arguments(item):
    """A function call's arguments as a dict; Codex sends them JSON-encoded."""
    args = item.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return {}
    return args if isinstance(args, dict) else {}


def _command(value):
    """The shell text a `cmd`/`command` argument runs, without its `bash -lc` wrapper."""
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        if len(value) == 3 and value[1] in ("-c", "-lc"):
            return value[2]
        return " ".join(value)
    return value if isinstance(value, str) else ""


def _tool_summary(name, item):
    if item.get("type") == "custom_tool_call":
        body = item.get("input") if isinstance(item.get("input"), str) else ""
        changed = _PATCH_FILE_RE.findall(body) if name == "apply_patch" else []
        summary = ", ".join(changed) if changed else body.strip()
    else:
        args = _arguments(item)
        summary = _command(args.get("cmd")) or _command(args.get("command"))
        if not summary and name == "apply_patch" and isinstance(args.get("input"), str):
            summary = ", ".join(_PATCH_FILE_RE.findall(args["input"]))
        summary = summary or claude.summarize_tool(args)
    summary = summary.strip()
    return summary.splitlines()[0][:200] if summary else ""


def _shell_output(text):
    """Split a newer shell tool's header from its output: (output, exit code)."""
    lines = text.split("\n")
    exit_code = None
    for index, line in enumerate(lines[:10]):
        if line == "Output:":
            return "\n".join(lines[index + 1:]), exit_code
        if not _SHELL_HEADER_RE.match(line):
            break
        match = _EXIT_RE.match(line)
        if match:
            exit_code = int(match.group(1))
    return text, None


def _output(item):
    """(text, failed) for a call output in any shape Codex has written."""
    output = item.get("output")
    failed = False
    if isinstance(output, str):
        try:
            decoded = json.loads(output)
        except ValueError:
            decoded = None
        # Older shell tools wrapped the output and its exit code in a JSON string.
        if isinstance(decoded, dict) and isinstance(decoded.get("output"), str):
            output = decoded
    if isinstance(output, dict):
        metadata = output.get("metadata")
        exit_code = metadata.get("exit_code") if isinstance(metadata, dict) else None
        failed = (
            isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0
        ) or output.get("success") is False
        output = output.get("output") if "output" in output else output.get("content")
    if isinstance(output, list):
        output = _text(output)
    if not isinstance(output, str):
        return "", failed
    output, exit_code = _shell_output(output)
    if exit_code:
        failed = True
    return output.strip(), failed


def _row_key(item, line):
    """The item's own id, else a hash of its row: a tail read never renumbers."""
    item_id = item.get("id")
    if isinstance(item_id, str) and item_id:
        return item_id
    return hashlib.sha256(line.encode("utf-8")).hexdigest()[:24]


def rollout_to_blocks(jsonl_text, limit=config.TRANSCRIPT_BLOCK_LIMIT):
    """Map a Codex rollout into a list of OutputBlock dicts."""
    blocks = []
    tool_blocks = {}
    seen = set()

    def add(kind, row, key, role, **fields):
        block = {"id": f"c:{key}", "kind": kind, **fields, "role": role, "message_id": key}
        stamp = claude._timestamp(row.get("timestamp"))
        if stamp is not None:
            block["timestamp"] = stamp
        blocks.append(block)
        return block

    for line in jsonl_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue  # the cut first line of a tail, or a row still being written
        if not isinstance(row, dict) or row.get("type") != "response_item":
            continue
        item = row.get("payload")
        if not isinstance(item, dict):
            continue
        key = _row_key(item, line)
        if key in seen:
            continue  # a resumed session replays the items it started from
        seen.add(key)
        kind = item.get("type")
        if kind == "message":
            role = item.get("role")
            text = _text(item.get("content")).strip()
            if not text:
                continue
            if role == "user" and not text.startswith(_INJECTED_PREFIXES):
                add("status", row, key, "user", label="You", text=text[:2000])
            elif role == "assistant" and item.get("channel") != "analysis":
                add("assistant_text", row, key, "assistant", markdown=text)
        elif kind in ("function_call", "custom_tool_call"):
            name = item.get("name") if isinstance(item.get("name"), str) and item["name"] else "tool"
            block = add("tool", row, key, "tool", label=name, text=_tool_summary(name, item), tool=name)
            call_id = item.get("call_id")
            if isinstance(call_id, str) and call_id:
                tool_blocks[call_id] = block
        elif kind in ("function_call_output", "custom_tool_call_output"):
            body, failed = _output(item)
            call_id = item.get("call_id")
            tool = tool_blocks.get(call_id) if isinstance(call_id, str) else None
            if tool is not None:
                if body:
                    tool["result"] = body[:_RESULT_LIMIT]
                    tool["result_truncated"] = len(body) > _RESULT_LIMIT
                    marker = "!" if failed else "→"
                    tool["text"] = f"{tool.get('text', '')} {marker} {body.splitlines()[0][:200]}"[:200]
                if failed:
                    tool["error"] = True
            elif body or failed:
                # The call fell outside a tail-bounded read; keep its answer.
                orphan = add(
                    "tool", row, key, "tool", label="Tool result",
                    text=body.splitlines()[0][:200] if body else "error",
                    result=body[:_RESULT_LIMIT], result_truncated=len(body) > _RESULT_LIMIT,
                )
                if failed:
                    orphan["error"] = True
    return blocks[-limit:]
