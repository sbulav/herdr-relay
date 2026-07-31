#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=14.0", "zeroconf>=0.80.0", "pywebpush>=2.0.0", "py-vapid>=1.9.0"]
# ///
"""herdr-remote relay — polls herdr, accepts push events (HTTP POST + WebSocket + UDP), broadcasts to clients."""
import asyncio, glob, hmac, json, logging, os, re, shlex, shutil, signal, socket, sqlite3, subprocess, time, uuid

try:
    from websockets.asyncio.server import serve
except ImportError:
    from websockets.server import serve
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from logging.handlers import RotatingFileHandler
import sys

def _get_log_dir():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Logs/herdr-remote")
    if os.path.isdir("/var/log") and os.access("/var/log", os.W_OK):
        return "/var/log/herdr-remote"
    return os.path.expanduser("~/.local/state/herdr-remote/log")

LOG_DIR = os.environ.get("HERDR_LOG_DIR", _get_log_dir())
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "relay.log")
AUDIT_FILE = os.path.join(LOG_DIR, "audit.log")

_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
_file_handler.setFormatter(_formatter)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_formatter)

log = logging.getLogger("herdr-relay")
log.setLevel(logging.INFO)
log.addHandler(_file_handler)
log.addHandler(_console_handler)
logging.getLogger("websockets").setLevel(logging.WARNING)

HERDR = os.environ.get("HERDR_BIN") or shutil.which("herdr") or "/opt/homebrew/bin/herdr"
WS_PORT = int(os.environ.get("HERDR_RELAY_PORT", "8375"))
POLL_INTERVAL = 2
AUTH_TOKEN = os.environ.get("HERDR_RELAY_TOKEN", "")  # Shared secret for relay auth
PRESETS_FILE = os.environ.get("HERDR_PRESETS_FILE", "")
POWER_HOST_ID = os.environ.get("HERDR_POWER_HOST_ID", "")
POWER_HOST_MAC = os.environ.get("HERDR_POWER_HOST_MAC", "")
WAKE_BIN = os.environ.get("HERDR_WAKE_BIN", "wakeonlan")
# Native structured stores used by supported coding agents.
CLAUDE_PROJECTS = os.environ.get("HERDR_CLAUDE_PROJECTS", "~/.claude/projects")
OPENCODE_DB = os.environ.get("HERDR_OPENCODE_DB", "~/.local/share/opencode/opencode-stable.db")
TRANSCRIPT_MAX_BYTES = 262144  # tail window read per poll — bounds ssh transfer
TRANSCRIPT_BLOCK_LIMIT = 200   # most recent blocks kept per session

# VAPID Web Push
VAPID_PUBLIC_KEY = os.environ.get("HERDR_VAPID_PUBLIC", "")
VAPID_PRIVATE_KEY = os.environ.get("HERDR_VAPID_PRIVATE", "")
VAPID_SUBJECT = os.environ.get("HERDR_VAPID_SUBJECT", "mailto:herdr@localhost")
push_subscriptions = []  # list of PushSubscription dicts
PUSH_SUBS_FILE = os.path.join(LOG_DIR, "push_subs.json")

# Remote hosts: comma-separated SSH targets
REMOTES = [r.strip() for r in os.environ.get("HERDR_REMOTES", "").split(",") if r.strip()]

# Kiro CLI free-text permission menus
TOOL_OPTIONS = ["yes, single permission", "trust, always allow", "no (tab to edit)"]
SUBAGENT_OPTIONS = ["approve all pending", "configure individually", "exit (cancel subagents)"]
# OpenCode TUI: left/right + enter (default selection = Allow once)
OPENCODE_OPTIONS = ["Allow once", "Allow always", "Reject"]
# Claude Code numbered selection menus: "❯ 1. Yes" / "  2. No"
CLAUDE_YES_NO = ["1. Yes", "2. No"]
NUMBERED_OPT_RE = re.compile(r"(?:^|\n)[ \t]*[❯>]?[ \t]*(\d+)\.\s+(\S[^\n]*)")
# Bullet-style free-text options: "> yes, single permission" or "• Allow once"
BULLET_OPT_RE = re.compile(
    r"(?:^|\n)[ \t]*(?:[❯>•*-]|\[\s?\])[ \t]+([A-Za-z][^\n]{0,80})"
)
CHROME_RE = re.compile(
    r"^[\s\u2500-\u259f⬝_—|◔◑◕●]+$"
    r"|^[\s\u2500-\u259f⬝]*(?i:esc\s+interrupt)\s*$"
    r"|Kiro\s[·•]"
    r"|esc to cancel"
    r"|type to queue"
    r"|^\s*[◔◑◕●]\s+(Shell|Bash)"
)

clients = set()
last_statuses = {}
event_queue = asyncio.Queue()
pane_remote_map = {}
session_target_map = {}
request_results = {}
pane_cwd_map = {}      # pane_id -> (cwd, agent, remote, ambiguous agent/cwd)
subscriptions = {}     # ws -> pane_id the client is currently viewing
stream_sigs = {}       # (id(ws), pane_id) -> signature of the last blocks pushed
known_panes = set()
pane_response_options = {}

SAFE_RESPONSES = {"y", "n", "a", "yes", "no", "trust", "yes, single permission", "trust, always allow", "no (tab to edit)", "approve all pending", "configure individually", "exit (cancel subagents)"}
SAFE_KEYS = {"y", "n", "a", "Enter", "Tab", "Escape", "Space", "C-c", "Ctrl+c", "Up", "Down", "Left", "Right", "BSpace", *map(str, range(1, 10))}


def is_safe_key(key):
    if key in SAFE_KEYS:
        return True
    return bool(re.fullmatch(r"(?:ctrl|shift)\+(?:[a-z]|[1-9]|Enter|Tab|Escape|Space|Up|Down|Left|Right)", key))

# --- Audit logging ---
_audit_handler = RotatingFileHandler(AUDIT_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
_audit_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
audit_log = logging.getLogger("herdr-audit")
audit_log.setLevel(logging.INFO)
audit_log.addHandler(_audit_handler)
audit_log.propagate = False


def audit(action: str, ip: str, device: str, pane_id: str, detail: str = ""):
    """Append a write action to the audit log as structured JSONL."""
    import datetime
    entry = {
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "action": action,
        "paneId": pane_id,
        "ip": ip,
        "device": device,
    }
    if detail:
        entry["detail"] = detail[:120]  # truncate like collie
    audit_log.info(json.dumps(entry, separators=(",", ":")))


# --- Web Push helpers ---
# LEGACY (#14): browser-PWA only. herdr-mobile monitors the WebSocket in a
# foreground service and never subscribes. This stack, the four HERDR_VAPID_*
# vars, and the push_subscribe/push_unsubscribe handlers go once web/ does.
def _load_push_subs():
    global push_subscriptions
    if os.path.isfile(PUSH_SUBS_FILE):
        try:
            with open(PUSH_SUBS_FILE) as f:
                push_subscriptions = json.load(f)
        except Exception:
            push_subscriptions = []


def _save_push_subs():
    with open(PUSH_SUBS_FILE, "w") as f:
        json.dump(push_subscriptions, f)


async def send_web_push(title: str, body: str, url: str = "/", clear: bool = False):
    """Send push notification to all registered subscriptions.

    Uses collapse topic + TTL so offline devices get only the latest.
    If clear=True, sends a clear instruction instead of showing a notification.
    """
    if not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_KEY:
        return
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        log.warning("pywebpush not installed, skipping push")
        return
    if clear:
        payload = json.dumps({"type": "clear", "tag": "herdr-blocked"})
    else:
        payload = json.dumps({"title": title, "body": body, "url": url})
    headers = {"Topic": "herdr-herd", "TTL": "21600"}  # 6h TTL, collapse key
    dead = []
    for i, sub in enumerate(push_subscriptions):
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
                headers=headers,
            )
        except Exception as e:
            log.warning("Push failed for sub %d: %s", i, e)
            if "410" in str(e) or "404" in str(e):
                dead.append(i)
    if dead:
        for i in reversed(dead):
            push_subscriptions.pop(i)
        _save_push_subs()

_load_push_subs()


def load_presets():
    if not PRESETS_FILE:
        return []
    with open(PRESETS_FILE, encoding="utf-8") as f:
        document = json.load(f)
    if document.get("schema_version") != 1:
        raise ValueError("unsupported preset schema version")
    presets = document.get("presets")
    if not isinstance(presets, list):
        raise ValueError("presets must be a list")
    seen = set()
    for preset in presets:
        preset_id = preset.get("id", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", preset_id) or preset_id in seen:
            raise ValueError(f"invalid or duplicate preset id: {preset_id}")
        seen.add(preset_id)
        if not isinstance(preset.get("repository"), str) or not preset["repository"]:
            raise ValueError(f"missing repository in preset {preset_id}")
        if preset.get("agent") not in ("claude", "opencode", "codex"):
            raise ValueError(f"unsupported agent in preset {preset_id}")
        if not isinstance(preset.get("model"), str) or not preset["model"]:
            raise ValueError(f"missing model in preset {preset_id}")
        hosts = preset.get("hosts")
        if not isinstance(hosts, dict) or not hosts:
            raise ValueError(f"missing hosts in preset {preset_id}")
        for host_id, host in hosts.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", host_id):
                raise ValueError(f"invalid host id: {host_id}")
            if not os.path.isabs(host.get("cwd", "")):
                raise ValueError(f"cwd must be absolute for {preset_id}@{host_id}")
            if host.get("target") is not None and not isinstance(host.get("target"), str):
                raise ValueError(f"invalid target for {preset_id}@{host_id}")
    return presets


PRESETS = load_presets()
PRESETS_BY_ID = {preset["id"]: preset for preset in PRESETS}
HOST_TARGETS = {
    host_id: host.get("target")
    for preset in PRESETS
    for host_id, host in preset["hosts"].items()
}


def public_presets():
    return [
        {
            "id": preset["id"], "label": preset["label"],
            "repository": preset["repository"],
            "agent": preset["agent"], "model": preset["model"],
            "hosts": {host_id: {"cwd": host["cwd"]} for host_id, host in preset["hosts"].items()},
        }
        for preset in PRESETS
    ]


def session_id(host_id, pane_id):
    return f"legacy:{host_id}:{pane_id}"


def run_herdr_checked(*args, remote=None):
    try:
        if remote:
            cmd = [
                "ssh",
                "-o", "ConnectTimeout=5",
                "-o", "ServerAliveInterval=3",
                "-o", "ServerAliveCountMax=2",
                "-o", "BatchMode=yes",
                remote, HERDR, *args,
            ]
        else:
            cmd = [HERDR, *args]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return r.returncode == 0, r.stdout.strip()
    except Exception as exc:
        if remote:
            print(f"herdr poll failed for {remote}: {exc!r}", flush=True)
        return False, ""


def run_herdr(*args, remote=None):
    return run_herdr_checked(*args, remote=remote)[1]


def get_agents_from_host(remote=None, host_id=None):
    online, raw = run_herdr_checked("pane", "list", remote=remote)
    host_label = host_id or remote or "local"
    try:
        data = json.loads(raw)
        panes = data.get("result", {}).get("panes", [])
        agents = [
            {
                "pane_id": p["pane_id"],
                "agent": p.get("agent", ""),
                "label": p.get("label", ""),
                "status": p.get("agent_status", "unknown"),
                "cwd": p.get("cwd", ""),
                "project": os.path.basename(p.get("cwd", "")),
                "host": host_label,
                "remote": remote,
                "workspace_id": p.get("workspace_id", ""),
                "tab_id": p.get("tab_id", ""),
            }
            for p in panes if p.get("agent")
        ]
    except (json.JSONDecodeError, KeyError):
        agents = []
    return agents, online


async def get_all_agents():
    if HOST_TARGETS:
        targets = list(HOST_TARGETS.items())
        results = await asyncio.gather(*(
            asyncio.to_thread(
                get_agents_from_host,
                remote=remote,
                host_id=host_id,
            )
            for host_id, remote in targets
        ))
        hosts = [
            {"host_id": host_id, "online": online}
            for (host_id, _remote), (_host_agents, online) in zip(targets, results)
        ]
    else:
        targets = [(None, None), *((None, remote) for remote in REMOTES)]
        results = await asyncio.gather(*(
            asyncio.to_thread(get_agents_from_host, remote=remote)
            for _host_id, remote in targets
        ))
        hosts = []
    agents = [
        agent
        for host_agents, _online in results
        for agent in host_agents
    ]
    return agents, hosts


def _read_pane_lines(value, default=30, maximum=2000):
    """Coerce a client-supplied line count into a sane positive int.

    Anything unparseable falls back to the default rather than reaching herdr,
    which reports a bad --lines as an error string on stdout with exit code 0 —
    indistinguishable, downstream, from real terminal output.
    """
    try:
        count = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if count < 1:
        return default
    return min(count, maximum)


def read_pane(pane_id, remote=None):
    raw = run_herdr("pane", "read", pane_id, "--lines", "50", "--source", "recent", remote=remote)
    lines = [l for l in raw.splitlines() if l.strip() and not CHROME_RE.search(l)]
    return "\n".join(lines[-20:])


def _numbered_options(text):
    numbered = NUMBERED_OPT_RE.findall(text)
    if len(numbered) < 2:
        return None
    seen = {}
    for num, label in numbered:
        if num not in seen:
            seen[num] = f"{num}. {label.strip()}"
    opts = [seen[k] for k in sorted(seen, key=int)]
    return opts if len(opts) >= 2 else None


def _bullet_options(text):
    labels = []
    seen = set()
    for label in BULLET_OPT_RE.findall(text):
        cleaned = label.strip().rstrip(".,;")
        key = cleaned.lower()
        if key in seen or len(cleaned) < 2:
            continue
        # Skip chrome / prose that looks like a bullet but isn't a choice.
        if any(x in key for x in ("esc to", "tab to", "ctrl+", "type to", "press ")):
            continue
        seen.add(key)
        labels.append(cleaned)
    return labels if len(labels) >= 2 else None


def detect_options(text):
    """Return selectable response labels for a blocked-agent prompt, or None.

    Labels are what clients display. respond_action() maps a chosen label to
    either free-text (send-text) or a key sequence (send-keys) for the agent TUI.
    """
    if not text:
        return None
    lower = text.lower()

    # --- Known free-text menus (exact option strings the agent reads) ---
    if "yes, single permission" in lower:
        return TOOL_OPTIONS
    if "approve all pending" in lower or "pending from subagents" in lower:
        return SUBAGENT_OPTIONS

    # OpenCode: "Permission required" with Allow once / Allow always / Reject
    if "permission required" in lower or (
        "allow once" in lower and "allow always" in lower and "reject" in lower
    ):
        return list(OPENCODE_OPTIONS)

    # --- Numbered menus (Claude Code and similar) ---
    numbered = _numbered_options(text)
    if numbered:
        return numbered

    # Bullet-style free-text options (> / • / -)
    bullets = _bullet_options(text)
    if bullets:
        return bullets

    # Claude "Do you want to proceed?" without captured numbers
    if (
        "do you want to proceed" in lower
        or "do you want to allow" in lower
        or "ask rule" in lower
        or "/permissions to let auto mode decide" in lower
    ):
        return list(CLAUDE_YES_NO)

    # Codex / simple y/n
    if "[y/n]" in lower or "yes (y)" in lower or "proceed (y)" in lower:
        return ["y", "n"]

    # Cursor-style write approval
    if "write to this file?" in lower and "proceed (y)" in lower:
        return ["y", "n"]

    # Hermes / generic allow once | session | deny
    if "allow once" in lower and ("deny" in lower or "allow for this session" in lower):
        return ["allow once", "allow for this session", "deny"]

    return None


def respond_action(text):
    """Map a client option label to a send action.

    Returns ("text", payload) for pane send-text, or ("keys", [key...]) for
    pane send-keys. OpenCode uses left/right + enter; Claude uses digits.
    """
    if not text:
        return "text", text
    raw = text.strip()
    lower = raw.lower()

    # Numbered menu label -> digit
    m = re.match(r"^(\d+)\.\s+", raw)
    if m:
        return "text", m.group(1)

    # OpenCode permission dialog (default selection = first = Allow once).
    # Only exact OpenCode labels map to keys — free-text "deny"/"always" stay text.
    if lower == "allow once":
        return "keys", ["Enter"]
    if lower in ("allow always", "always allow"):
        # move right to "Allow always", enter, then confirm stage
        return "keys", ["Right", "Enter", "Enter"]
    if lower == "reject":
        return "keys", ["Escape"]

    # y/n style
    if lower in ("y", "yes"):
        return "text", "y"
    if lower in ("n", "no"):
        return "text", "n"

    return "text", raw


def respond_text(text):
    """Backward-compatible: return free-text payload only (no key sequences)."""
    kind, payload = respond_action(text)
    if kind == "text":
        return payload
    # Callers that only support text fall back to first meaningful token.
    return text


# ---------------------------------------------------------------------------
# Structured output from Claude Code and OpenCode native session stores.
#
# Claude Code persists a fully structured JSONL transcript per project at
# ~/.claude/projects/<escaped-cwd>/<session-uuid>.jsonl. Reading it directly
# gives us real OutputBlocks (assistant prose, tool calls, thinking, prompts)
# with no ANSI/box-drawing/spinner guesswork. The relay already knows each
# OpenCode stores equivalent message parts in SQLite. The relay already knows
# each pane's cwd, so neither path needs a change to `herdr` itself.
# ---------------------------------------------------------------------------

def claude_project_dir(cwd):
    """Escape a cwd the way Claude Code names its per-project transcript dir."""
    return re.sub(r"[/._]", "-", cwd)


def read_transcript(cwd, remote=None):
    """Return (path, jsonl_text) for the newest transcript in cwd, or (None, None).

    Reads only the trailing TRANSCRIPT_MAX_BYTES so a long session stays cheap
    to poll; the (possibly partial) first line is tolerated by the parser.
    """
    if not cwd:
        return None, None
    proj = claude_project_dir(cwd)
    if remote:
        root = CLAUDE_PROJECTS.replace("~", "$HOME")
        script = (
            f'd="{root}/$1"; '
            'f=$(ls -t "$d"/*.jsonl 2>/dev/null | head -1); '
            '[ -n "$f" ] || exit 0; '
            'printf "%s\\n" "$f"; '
            f'tail -c {TRANSCRIPT_MAX_BYTES} "$f"'
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
        d = os.path.join(os.path.expanduser(CLAUDE_PROJECTS), proj)
        files = glob.glob(os.path.join(d, "*.jsonl"))
        if not files:
            return None, None
        path = max(files, key=os.path.getmtime)
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TRANSCRIPT_MAX_BYTES))
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


def transcript_to_blocks(jsonl_text, limit=TRANSCRIPT_BLOCK_LIMIT):
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


OPENCODE_PART_QUERY = """
SELECT json_extract(m.data, '$.role'), p.data
FROM message m
JOIN part p ON p.message_id = m.id
WHERE m.session_id = ?
ORDER BY m.time_created DESC, p.time_created DESC, p.id DESC
LIMIT ?
"""


def _read_opencode_local(db_path, cwd):
    """Return the newest top-level OpenCode session and its recent parts."""
    db_uri = "file:" + os.path.expanduser(db_path) + "?mode=ro"
    with sqlite3.connect(db_uri, uri=True, timeout=2) as db:
        session = db.execute(
            "SELECT id, time_updated FROM session "
            "WHERE directory = ? AND parent_id IS NULL "
            "ORDER BY time_updated DESC LIMIT 1", (cwd,)
        ).fetchone()
        if not session:
            return None
        session_id, updated = session
        rows = db.execute(
            OPENCODE_PART_QUERY, (session_id, TRANSCRIPT_BLOCK_LIMIT * 4)
        ).fetchall()
    rows.reverse()
    return {"session_id": session_id, "updated": updated, "rows": rows}


def read_opencode(cwd, remote=None):
    """Read bounded structured parts for the newest OpenCode session in cwd."""
    if not cwd:
        return None
    if not remote:
        try:
            return _read_opencode_local(OPENCODE_DB, cwd)
        except Exception:
            return None
    script = """
import json, os, sqlite3, sys
db = sqlite3.connect("file:" + os.path.expanduser(sys.argv[1]) + "?mode=ro", uri=True, timeout=2)
session = db.execute("SELECT id, time_updated FROM session WHERE directory = ? AND parent_id IS NULL ORDER BY time_updated DESC LIMIT 1", (sys.argv[2],)).fetchone()
if session:
    rows = db.execute(sys.argv[3], (session[0], int(sys.argv[4]))).fetchall()
    rows.reverse()
    print(json.dumps({"session_id": session[0], "updated": session[1], "rows": rows}))
"""
    remote_cmd = " ".join([
        "python3", "-c", shlex.quote(script), shlex.quote(OPENCODE_DB),
        shlex.quote(cwd), shlex.quote(OPENCODE_PART_QUERY),
        str(TRANSCRIPT_BLOCK_LIMIT * 4),
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


def opencode_to_blocks(document, limit=TRANSCRIPT_BLOCK_LIMIT):
    """Map OpenCode message parts into OutputBlock dictionaries."""
    if not isinstance(document, dict):
        return []
    blocks = []

    def add(kind, **kw):
        kw["id"] = f"o{len(blocks)}"
        kw["kind"] = kind
        blocks.append(kw)

    for row in document.get("rows") or []:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            continue
        role, raw_part = row
        try:
            part = json.loads(raw_part) if isinstance(raw_part, str) else raw_part
        except Exception:
            continue
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        text = part.get("text")
        if role == "user" and part_type == "text" and isinstance(text, str) and text.strip():
            add("status", label="You", text=text.strip()[:2000])
        elif role == "assistant" and part_type == "text" and isinstance(text, str) and text.strip():
            add("assistant_text", markdown=text)
        elif role == "assistant" and part_type == "reasoning" and isinstance(text, str) and text.strip():
            add("status", label="Thought", text=text.strip().splitlines()[0][:200])
        elif role == "assistant" and part_type == "tool":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            summary = summarize_tool(state.get("input")) or str(state.get("title") or "")[:200]
            add("tool", label=part.get("tool") or "tool", text=summary)
    return blocks[-limit:]


def pane_blocks(pane_id):
    """(blocks, signature) for a Claude pane's transcript, else (None, None)."""
    info = pane_cwd_map.get(pane_id)
    if not info:
        return None, None
    cwd, agent, remote, ambiguous = info
    # Without an agent session id/path, cwd is the only correlation available.
    # Refuse structured output when multiple live same-agent panes share it rather
    # than risk showing one conversation in another pane.
    if agent not in ("claude", "opencode") or not cwd or ambiguous:
        return None, None
    if agent == "claude":
        try:
            path, body = read_transcript(cwd, remote)
        except Exception:
            return None, None
        if not body:
            return None, None
        return transcript_to_blocks(body), hash((path, body))
    document = read_opencode(cwd, remote)
    if not document:
        return None, None
    blocks = opencode_to_blocks(document)
    return blocks, hash(json.dumps(document, sort_keys=True))


async def broadcast(msg):
    data = json.dumps(msg)
    dead = set()
    # Disconnect cleanup mutates clients while send() yields to the event loop.
    for ws in clients.copy():
        try:
            await ws.send(data)
        except (ConnectionClosedError, ConnectionClosedOK):
            dead.add(ws)
        except Exception:
            dead.add(ws)
    if dead:
        log.debug("Removed %d dead client(s)", len(dead))
    clients.difference_update(dead)


def fail_on_background_exit(task, stop):
    if stop.done():
        return
    if task.cancelled():
        stop.set_exception(RuntimeError(f"{task.get_name()} was cancelled"))
        return
    exception = task.exception()
    if exception is None:
        exception = RuntimeError(f"{task.get_name()} exited unexpectedly")
    stop.set_exception(exception)


async def poll_loop():
    while True:
        try:
            await _poll_once()
        except Exception:
            log.exception("poll cycle failed; retrying")
        await asyncio.sleep(POLL_INTERVAL)


async def _poll_once():
    agents, hosts = await get_all_agents()
    current_pane_ids = {a["pane_id"] for a in agents}
    pane_remote_map.clear()
    session_target_map.clear()
    pane_cwd_map.clear()
    known_panes.clear()
    known_panes.update(current_pane_ids)
    agent_cwd_counts = {}
    for a in agents:
        if a.get("agent") in ("claude", "opencode") and a.get("cwd"):
            cwd_key = (a.get("remote"), a["cwd"], a["agent"])
            agent_cwd_counts[cwd_key] = agent_cwd_counts.get(cwd_key, 0) + 1
    for a in agents:
        pane_remote_map[a["pane_id"]] = a.get("remote")
        session_target_map[session_id(a["host"], a["pane_id"])] = (a["pane_id"], a.get("remote"))
        cwd_key = (a.get("remote"), a.get("cwd", ""), a.get("agent", ""))
        pane_cwd_map[a["pane_id"]] = (
            a.get("cwd", ""), a.get("agent", ""), a.get("remote"),
            agent_cwd_counts.get(cwd_key, 0) > 1,
        )

    # Always send a complete snapshot. In particular, an empty snapshot
    # removes stale agents after every remote host goes offline.
    await broadcast({
        "type": "agents", "agents": agents,
        "presets": public_presets(),
        "hosts": hosts,
    })
    for a in agents:
        pid, status = a["pane_id"], a["status"]
        if status == "blocked" and last_statuses.get(pid) != "blocked":
            content = read_pane(pid, remote=a.get("remote"))
            options = detect_options(content)
            pane_response_options[pid] = {
                option.lower() for option in (options or TOOL_OPTIONS)
            }
            await broadcast({
                "type": "blocked", "pane_id": pid,
                "agent": a["agent"], "project": a["project"],
                "host": a.get("host", "local"),
                "prompt": content[:500],
                "options": options or TOOL_OPTIONS
            })
            await send_web_push(
                title=f"🐑 {a['project']} blocked",
                body=content[:120],
                url=f"/?pane={pid}",
            )
        if status != "blocked" and last_statuses.get(pid) == "blocked":
            pane_response_options.pop(pid, None)
            await send_web_push("", "", clear=True)
        last_statuses[pid] = status
    for pid in set(last_statuses) - current_pane_ids:
        del last_statuses[pid]
        pane_response_options.pop(pid, None)

    # Live-stream structured transcript blocks to subscribed clients. Only
    # watched Claude panes are read; a changed signature (path or content)
    # triggers a push. Failures are swallowed so one bad host can't stall.
    watchers = {
        pid: [ws for ws, subscribed_pid in list(subscriptions.items()) if subscribed_pid == pid]
        for pid in set(subscriptions.values()) if pid in current_pane_ids
    }
    pane_results = await asyncio.gather(
        *(asyncio.to_thread(pane_blocks, pid) for pid in watchers),
        return_exceptions=True,
    )
    for pid, result in zip(watchers, pane_results):
        if isinstance(result, Exception):
            continue
        blocks, sig = result
        if blocks is None:
            continue
        payload = json.dumps({"type": "pane_content", "pane_id": pid, "output_blocks": blocks})
        for ws in watchers[pid]:
            key = (id(ws), pid)
            if stream_sigs.get(key) == sig:
                continue
            stream_sigs[key] = sig
            try:
                await ws.send(payload)
            except Exception:
                pass


async def event_push():
    while True:
        event = await event_queue.get()
        pane_id = event.get("pane_id", "")
        status = event.get("status", "")
        host = event.get("host", "local")

        if status == "blocked" and pane_id:
            remote = pane_remote_map.get(pane_id)
            if remote or host == "local":
                content = read_pane(pane_id, remote=remote)
            else:
                content = event.get("prompt", "Agent is blocked")
            options = detect_options(content)
            await broadcast({
                "type": "blocked", "pane_id": pane_id,
                "agent": event.get("agent", ""),
                "project": event.get("project", ""),
                "host": host,
                "prompt": content[:500],
                "options": options or TOOL_OPTIONS
            })

        if pane_id and event.get("type") == "agent_event":
            await broadcast({
                "type": "agents", "agents": [{
                    "pane_id": pane_id,
                    "agent": event.get("agent", ""),
                    "status": status,
                    "cwd": event.get("cwd", ""),
                    "project": event.get("project", ""),
                    "host": host,
                }]
            })


async def process_request(connection, request):
    """Handle HTTP POST on the same port as WebSocket."""
    from websockets.http11 import Response
    from websockets.datastructures import Headers

    # Token auth
    token = None
    for key, value in request.headers.raw_items():
        if key.lower() == "authorization":
            token = value.removeprefix("Bearer ").strip()
            break
    # LEGACY (#14): query-param auth exists because a browser cannot set headers
    # on a WebSocket handshake. It leaks the token into every proxy access log,
    # so it is retired with web/. Native clients send Authorization.
    # Also check query param ?token=
    if not token and "token=" in (request.path or ""):
        import urllib.parse
        _, qs = request.path.split("?", 1) if "?" in request.path else (request.path, "")
        params = urllib.parse.parse_qs(qs)
        token = params.get("token", [None])[0]
    # compare_digest raises TypeError on non-ASCII str, so compare encoded bytes.
    if not token or not hmac.compare_digest(token.encode("utf-8", "replace"), AUTH_TOKEN.encode("utf-8", "replace")):
        headers = Headers([("Content-Type", "text/plain")])
        return Response(401, "Unauthorized", headers, b"Invalid token\n")

    # Check if this is a WebSocket upgrade
    upgrade = None
    for key, value in request.headers.raw_items():
        if key.lower() == "upgrade":
            upgrade = value.lower()
    if upgrade == "websocket":
        return None  # proceed with WebSocket handshake

    # For CORS preflight
    if request.path and "OPTIONS" in str(request.headers):
        headers = Headers([
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type"),
        ])
        return Response(204, "No Content", headers, b"")

    # LEGACY (#14): the four static routes below (/, /index.html, /sw.js,
    # /logo.svg) and /api/vapid-public-key serve the browser PWA out of web/.
    # They are deleted together with web/ once a herdr-mobile build without the
    # WebView fallback is actually installed on the phone.
    # Serve web app for GET / or GET /index.html
    path = (request.path or "/").split("?")[0]
    if path in ("/", "/index.html"):
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
        index_path = os.path.join(web_dir, "index.html")
        if os.path.isfile(index_path):
            with open(index_path, "rb") as f:
                body = f.read()
            headers = Headers([
                ("Content-Type", "text/html; charset=utf-8"),
                ("Cache-Control", "no-cache"),
            ])
            return Response(200, "OK", headers, body)

    # Serve service worker
    if path == "/sw.js":
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
        sw_path = os.path.join(web_dir, "sw.js")
        if os.path.isfile(sw_path):
            with open(sw_path, "rb") as f:
                body = f.read()
            headers = Headers([
                ("Content-Type", "application/javascript"),
                ("Cache-Control", "no-cache"),
                ("Service-Worker-Allowed", "/"),
            ])
            return Response(200, "OK", headers, body)

    # Serve VAPID public key
    if path == "/api/vapid-public-key":
        body = json.dumps({"publicKey": VAPID_PUBLIC_KEY}).encode()
        headers = Headers([
            ("Content-Type", "application/json"),
            ("Access-Control-Allow-Origin", "*"),
        ])
        return Response(200, "OK", headers, body)

    # Serve logo.svg
    if path == "/logo.svg":
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
        svg_path = os.path.join(web_dir, "logo.svg")
        if os.path.isfile(svg_path):
            with open(svg_path, "rb") as f:
                body = f.read()
            headers = Headers([("Content-Type", "image/svg+xml")])
            return Response(200, "OK", headers, body)

    # HTTP POST — parse event from URL query params as fallback
    import urllib.parse
    if "?" in (request.path or ""):
        _, qs = request.path.split("?", 1)
        params = urllib.parse.parse_qs(qs)
        if "d" in params:
            try:
                event = json.loads(urllib.parse.unquote(params["d"][0]))
                event_queue.put_nowait(event)
            except Exception:
                pass

    headers = Headers([("Access-Control-Allow-Origin", "*")])
    return Response(200, "OK", headers, b"ok\n")


async def handle_client(ws):
    remote_addr = getattr(ws, "remote_address", None)
    ip = remote_addr[0] if remote_addr else "unknown"
    request = getattr(ws, "request", None)
    headers = request.headers if request else getattr(ws, "request_headers", {})
    ua = headers.get("User-Agent", "unknown")
    origin = headers.get("Origin", "")

    device = "unknown"
    ua_lower = ua.lower()
    if "iphone" in ua_lower or "ipad" in ua_lower:
        device = "iOS"
    elif "android" in ua_lower:
        device = "Android"
    elif "macintosh" in ua_lower or "mac os" in ua_lower:
        device = "macOS"
    elif "windows" in ua_lower:
        device = "Windows"
    elif "linux" in ua_lower:
        device = "Linux"
    elif "telegram" in ua_lower or "bot" in ua_lower:
        device = "bot"
    elif "python" in ua_lower:
        device = "script"

    log.info("Client connected: ip=%s device=%s origin=%s", ip, device, origin or "-")
    clients.add(ws)
    connected_at = time.monotonic()
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg_type = msg.get("type")
            request_id = msg.get("request_id")
            if request_id and request_id in request_results:
                await ws.send(json.dumps(request_results[request_id]))
            elif msg_type == "launch_session":
                response = await asyncio.to_thread(launch_session, msg)
                if request_id:
                    request_results[request_id] = response
                    if len(request_results) > 512:
                        request_results.pop(next(iter(request_results)))
                await ws.send(json.dumps(response))
            elif msg_type == "terminate_session":
                response = await asyncio.to_thread(terminate_session, msg)
                if request_id:
                    request_results[request_id] = response
                    if len(request_results) > 512:
                        request_results.pop(next(iter(request_results)))
                await ws.send(json.dumps(response))
            elif msg_type == "wake_host":
                response = await asyncio.to_thread(wake_host, msg)
                if request_id:
                    request_results[request_id] = response
                    if len(request_results) > 512:
                        request_results.pop(next(iter(request_results)))
                await ws.send(json.dumps(response))
            elif msg_type == "shutdown_host":
                response = await asyncio.to_thread(shutdown_host, msg)
                if request_id:
                    request_results[request_id] = response
                    if len(request_results) > 512:
                        request_results.pop(next(iter(request_results)))
                await ws.send(json.dumps(response))
            elif msg_type == "respond":
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                text = msg.get("text", "")
                normalized_text = text.strip().lower()
                allowed_responses = SAFE_RESPONSES | pane_response_options.get(pane_id, set())
                if normalized_text not in allowed_responses:
                    await ws.send(json.dumps({"type": "error", "message": "response not in allowlist"}))
                    continue
                remote = pane_remote_map.get(pane_id)
                log.info("Response from %s (%s): pane=%s text=%r", ip, device, pane_id, text)
                audit("respond", ip, device, pane_id, f"text={text!r}")
                kind, payload = respond_action(text)
                if kind == "keys":
                    await asyncio.to_thread(run_herdr, "pane", "send-keys", pane_id, *payload, remote=remote)
                else:
                    await asyncio.to_thread(run_herdr, "pane", "send-text", pane_id, payload + "\n", remote=remote)
            elif msg_type == "agent_event":
                event_queue.put_nowait(msg)
            elif msg_type == "read_pane":
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                # herdr rejects a non-numeric --lines by printing an error and
                # exiting 0, which would reach the client as pane content.
                lines = _read_pane_lines(msg.get("lines"))
                remote = pane_remote_map.get(pane_id)
                content = await asyncio.to_thread(run_herdr, "pane", "read", pane_id, "--lines", str(lines), "--source", "recent", remote=remote)
                payload = {"type": "pane_content", "pane_id": pane_id, "content": content}
                # Include structured blocks on demand without changing which pane
                # this client explicitly subscribed to for live updates.
                try:
                    blocks, sig = await asyncio.to_thread(pane_blocks, pane_id)
                except Exception:
                    blocks, sig = None, None
                if blocks is not None:
                    payload["output_blocks"] = blocks
                    if subscriptions.get(ws) == pane_id:
                        stream_sigs[(id(ws), pane_id)] = sig
                await ws.send(json.dumps(payload))
                options = detect_options(content) if last_statuses.get(pane_id) == "blocked" else None
                if options:
                    pane_response_options[pane_id] = {option.lower() for option in options}
                    await ws.send(json.dumps({
                        "type": "blocked",
                        "pane_id": pane_id,
                        "prompt": content[:500],
                        "options": options,
                    }))
            elif msg_type == "subscribe_pane":
                pane_id = msg.get("pane_id")
                if pane_id in pane_cwd_map:
                    previous = subscriptions.get(ws)
                    subscriptions[ws] = pane_id
                    if previous is not None:
                        stream_sigs.pop((id(ws), previous), None)
                    try:
                        blocks, sig = await asyncio.to_thread(pane_blocks, pane_id)
                    except Exception:
                        blocks, sig = None, None
                    if blocks is not None:
                        stream_sigs[(id(ws), pane_id)] = sig
                        await ws.send(json.dumps({"type": "pane_content", "pane_id": pane_id, "output_blocks": blocks}))
            elif msg_type == "unsubscribe_pane":
                previous = subscriptions.pop(ws, None)
                if previous is not None:
                    stream_sigs.pop((id(ws), previous), None)
            elif msg_type == "send_keys":
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                keys = msg.get("keys", [])
                if not all(is_safe_key(k) for k in keys):
                    await ws.send(json.dumps({"type": "error", "message": "keys contain disallowed values"}))
                    continue
                remote = pane_remote_map.get(pane_id)
                log.info("Keys from %s (%s): pane=%s keys=%s", ip, device, pane_id, keys)
                audit("send_keys", ip, device, pane_id, f"keys={keys}")
                await asyncio.to_thread(run_herdr, "pane", "send-keys", pane_id, *keys, remote=remote)
            elif msg_type == "send_text":
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                text = msg.get("text", "")
                if not text or len(text) > 1000:
                    await ws.send(json.dumps({"type": "error", "message": "text empty or too long"}))
                    continue
                remote = pane_remote_map.get(pane_id)
                log.info("Text from %s (%s): pane=%s text=%r", ip, device, pane_id, text)
                audit("send_text", ip, device, pane_id, f"text={text!r}")
                await asyncio.to_thread(run_herdr, "pane", "send-text", pane_id, text, remote=remote)
            elif msg_type == "create_tab":
                workspace_id = msg.get("workspace_id", "")
                if workspace_id:
                    log.info("Create tab from %s (%s): workspace=%s", ip, device, workspace_id)
                    audit("create_tab", ip, device, "", f"workspace={workspace_id}")
                    await asyncio.to_thread(run_herdr, "tab", "create", "--workspace", workspace_id, "--focus")
                    await ws.send(json.dumps({"type": "tab_created", "ok": True}))
                else:
                    await ws.send(json.dumps({"type": "error", "message": "workspace_id required"}))
            # LEGACY (#14): browser-PWA only, retired with web/.
            elif msg_type == "push_subscribe":
                sub = msg.get("subscription")
                if sub and sub not in push_subscriptions:
                    push_subscriptions.append(sub)
                    _save_push_subs()
                    log.info("Push subscription added from %s (%s)", ip, device)
                await ws.send(json.dumps({"type": "push_subscribed", "ok": True}))
            elif msg_type == "push_unsubscribe":
                sub = msg.get("subscription")
                if sub and sub in push_subscriptions:
                    push_subscriptions.remove(sub)
                    _save_push_subs()
                await ws.send(json.dumps({"type": "push_unsubscribed", "ok": True}))
    except (ConnectionClosedError, ConnectionClosedOK):
        pass
    finally:
        duration = int(time.monotonic() - connected_at)
        log.info("Client disconnected: ip=%s device=%s duration=%ds", ip, device, duration)
        clients.discard(ws)
        subscriptions.pop(ws, None)
        for key in [k for k in stream_sigs if k[0] == id(ws)]:
            stream_sigs.pop(key, None)


def command_error(request_id, code, message):
    return {"type": "command_error", "request_id": request_id, "code": code, "message": message}


def launch_session(msg):
    request_id = msg.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return command_error(None, "INVALID_REQUEST", "request_id is required")
    preset = PRESETS_BY_ID.get(msg.get("preset_id"))
    if not preset:
        return command_error(request_id, "UNKNOWN_PRESET", "Unknown preset")
    host_id = msg.get("host_id")
    host = preset["hosts"].get(host_id)
    if not host:
        return command_error(request_id, "HOST_NOT_ALLOWED", "Preset is not allowed on this host")
    remote = host.get("target")
    agent = preset["agent"]
    argv = [agent]
    if preset["model"] != "default":
        argv.extend(["--model", preset["model"]])
    name = f"mobile-{preset['id']}-{uuid.uuid4().hex[:8]}"
    success, output = run_herdr_checked("agent", "start", name, "--cwd", host["cwd"], "--no-focus", "--", *argv, remote=remote)
    if not success:
        return command_error(request_id, "LAUNCH_FAILED", "Herdr did not start the client")
    return {"type": "command_ack", "request_id": request_id, "result": {"host_id": host_id}}


def terminate_session(msg):
    request_id = msg.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return command_error(None, "INVALID_REQUEST", "request_id is required")
    if not isinstance(msg.get("confirmation_nonce"), str) or not msg["confirmation_nonce"]:
        return command_error(request_id, "CONFIRMATION_REQUIRED", "confirmation_nonce is required")
    target = session_target_map.get(msg.get("session_id"))
    if not target:
        return command_error(request_id, "STALE_SESSION", "Session is no longer active")
    pane_id, remote = target
    success, output = run_herdr_checked("pane", "close", pane_id, remote=remote)
    if not success:
        return command_error(request_id, "TERMINATE_FAILED", "Herdr did not terminate the client")
    session_target_map.pop(msg["session_id"], None)
    return {"type": "command_ack", "request_id": request_id, "result": {"output": output}}


def wake_host(msg):
    request_id = msg.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return command_error(None, "INVALID_REQUEST", "request_id is required")
    host_id = msg.get("host_id")
    if host_id != POWER_HOST_ID or not POWER_HOST_MAC:
        return command_error(request_id, "HOST_NOT_ALLOWED", "Power control is not allowed for this host")
    try:
        result = subprocess.run(
            [WAKE_BIN, POWER_HOST_MAC],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return command_error(request_id, "WAKE_FAILED", "Wake-on-LAN command failed")
    if result.returncode != 0:
        return command_error(request_id, "WAKE_FAILED", "Wake-on-LAN command failed")
    return {"type": "command_ack", "request_id": request_id, "result": {"host_id": host_id}}


def shutdown_host(msg):
    request_id = msg.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return command_error(None, "INVALID_REQUEST", "request_id is required")
    if not isinstance(msg.get("confirmation_nonce"), str) or not msg["confirmation_nonce"]:
        return command_error(request_id, "CONFIRMATION_REQUIRED", "confirmation_nonce is required")
    host_id = msg.get("host_id")
    if host_id != POWER_HOST_ID:
        return command_error(request_id, "HOST_NOT_ALLOWED", "Power control is not allowed for this host")
    target = HOST_TARGETS.get(host_id)
    if not target:
        return command_error(request_id, "UNKNOWN_HOST", "Power host has no SSH target")
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o", "ConnectTimeout=5",
                "-o", "ServerAliveInterval=3",
                "-o", "ServerAliveCountMax=2",
                "-o", "BatchMode=yes",
                target,
                "sudo", "-n", "systemctl", "poweroff",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return command_error(request_id, "SHUTDOWN_FAILED", "Host shutdown command failed")
    if result.returncode != 0:
        return command_error(request_id, "SHUTDOWN_FAILED", "Host shutdown command failed")
    return {"type": "command_ack", "request_id": request_id, "result": {"host_id": host_id}}


class UDPPlugin(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        try:
            event_queue.put_nowait(json.loads(data.decode()))
        except Exception:
            pass


def start_mdns():
    try:
        from zeroconf import Zeroconf, ServiceInfo
        import socket as sock_mod
        import threading
        ip = sock_mod.gethostbyname(sock_mod.gethostname())
        info = ServiceInfo(
            "_herdr-remote._tcp.local.", "herdr-remote._herdr-remote._tcp.local.",
            addresses=[sock_mod.inet_aton(ip)], port=WS_PORT,
        )
        zc = Zeroconf()
        threading.Thread(target=zc.register_service, args=(info,), daemon=True).start()
        log.info("mDNS registering at %s", ip)
        return zc, info
    except Exception as e:
        log.warning("mDNS skipped: %s", e)
        return None, None


def require_auth_token():
    if not AUTH_TOKEN:
        raise SystemExit("HERDR_RELAY_TOKEN is required; set it before starting the relay")


async def main():
    require_auth_token()
    zc, info = start_mdns()
    loop = asyncio.get_running_loop()
    try:
        await loop.create_datagram_endpoint(UDPPlugin, local_addr=("127.0.0.1", 8376))
    except OSError:
        log.warning("UDP 8376 in use, plugin push disabled")
    server = await serve(handle_client, "0.0.0.0", WS_PORT, process_request=process_request)
    background_tasks = [
        asyncio.create_task(poll_loop(), name="poll-loop"),
        asyncio.create_task(event_push(), name="event-push"),
    ]
    hosts = ["local"] + REMOTES
    log.info("herdr-remote relay on :%d (WebSocket + HTTP POST)", WS_PORT)
    log.info("Polling: %s", ", ".join(hosts))
    stop = loop.create_future()
    for task in background_tasks:
        task.add_done_callback(lambda completed: fail_on_background_exit(completed, stop))
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set_result, None)
    try:
        await stop
    finally:
        server.close()
        await server.wait_closed()
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        if zc and info:
            zc.unregister_service(info)
            zc.close()


if __name__ == "__main__":
    asyncio.run(main())
