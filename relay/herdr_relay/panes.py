"""Reading a terminal pane's prompt and turning a chosen label into keystrokes.

`detect_options` decides what a client is offered when an agent blocks;
`respond_action` decides what actually reaches the TUI. The allowlists here are
the reason a client cannot send arbitrary text or keys to a pane.
"""
import re

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


SAFE_RESPONSES = {"y", "n", "a", "yes", "no", "trust", "yes, single permission", "trust, always allow", "no (tab to edit)", "approve all pending", "configure individually", "exit (cancel subagents)"}
SAFE_KEYS = {"y", "n", "a", "Enter", "Tab", "Escape", "Space", "C-c", "Ctrl+c", "Up", "Down", "Left", "Right", "BSpace", *map(str, range(1, 10))}


def is_safe_key(key):
    if key in SAFE_KEYS:
        return True
    return bool(re.fullmatch(r"(?:ctrl|shift)\+(?:[a-z]|[1-9]|Enter|Tab|Escape|Space|Up|Down|Left|Right)", key))


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
