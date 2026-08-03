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

from .. import config

def claude_project_dir(cwd):
    """Escape a cwd the way Claude Code names its per-project transcript dir."""
    return re.sub(r"[/._]", "-", cwd)


def read_transcript(cwd, remote=None, path=None):
    """Return (path, jsonl_text) for the newest transcript in cwd, or (None, None).

    Reads only the trailing `config.TRANSCRIPT_MAX_BYTES` so a long session stays cheap
    to poll; the (possibly partial) first line is tolerated by the parser.
    """
    if not path and not cwd:
        return None, None
    if remote:
        if path:
            script = (
                'f=$1; case "$f" in "~/"*) f="$HOME/${f#~/}" ;; esac; '
                '[ -f "$f" ] || exit 0; '
                'printf "%s\\n" "$f"; '
                f'tail -c {config.TRANSCRIPT_MAX_BYTES} "$f"'
            )
            remote_cmd = "sh -c " + shlex.quote(script) + " sh " + shlex.quote(path)
        else:
            proj = claude_project_dir(cwd)
            root = config.CLAUDE_PROJECTS.replace("~", "$HOME")
            script = (
                f'd="{root}/$1"; '
                'f=$(ls -t "$d"/*.jsonl 2>/dev/null | head -1); '
                '[ -n "$f" ] || exit 0; '
                'printf "%s\\n" "$f"; '
                f'tail -c {config.TRANSCRIPT_MAX_BYTES} "$f"'
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
            path = os.path.expanduser(path)
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
            fh.seek(max(0, size - config.TRANSCRIPT_MAX_BYTES))
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

    def add(kind, **kw):
        kw["id"] = f"b{len(blocks)}"
        kw["kind"] = kind
        blocks.append(kw)

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
        rtype = rec.get("type")
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        if rtype == "assistant":
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text" and (b.get("text") or "").strip():
                    add("assistant_text", markdown=b["text"])
                elif bt == "thinking" and (b.get("thinking") or "").strip():
                    add("status", label="Thought", text=b["thinking"].strip().splitlines()[0][:200])
                elif bt == "tool_use":
                    add("tool", label=b.get("name") or "tool", text=summarize_tool(b.get("input")))
        elif rtype == "user":
            content = msg.get("content")
            if isinstance(content, str):
                t = content.strip()
                if t and not t.startswith("<command-") and "<command-name>" not in t:
                    add("status", label="You", text=t[:2000])
            # list content (tool_result / multimodal) is skipped in v1
    return blocks[-limit:]
