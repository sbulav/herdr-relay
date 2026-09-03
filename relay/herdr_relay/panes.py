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
# --- Herdr 0.8 key grammar -------------------------------------------------
#
# Pinned by probing herdr 0.8.0 itself, not inferred from tmux or crossterm:
#   * names match case-insensitively, so `Ctrl+d` and `ctrl+d` both reach it;
#   * `BSpace` is not a Herdr key name at all -- `Backspace` and `BS` are;
#   * `C-c` is special-cased, and is the only `-` chord Herdr knows (`C-a` and
#     `C-x` are rejected), so every other chord must use the `+` form;
#   * Herdr has no name for Home, End, PageUp or PageDown -- it rejects those
#     names, and rejects `ctrl+Home` too, which is why NAV_SEQUENCES exists:
#     those four reach a pane as CSI text instead.
# Herdr is wider than this allowlist in four ways, all deliberately withheld:
# it accepts `alt+`/`meta+` chords, F0-F255, uppercase `Y`/`N`/`A`, and `c-c`
# alongside `C-c` (`C-C` it rejects). The allowlist stays at the spellings
# clients are actually offered, because it is a security boundary and nothing
# here needs widening.

# Named keys Herdr accepts as `pane send-keys` arguments, lowercase to match.
SAFE_KEY_NAMES = frozenset({
    "enter", "return", "tab", "escape", "esc", "space",
    "backspace", "bs", "up", "down", "left", "right",
})
# Single characters a blocked-prompt answer needs: y/n/a and menu digits.
SAFE_KEY_CHARS = frozenset("yna0123456789")
# The one tmux-style chord Herdr special-cases; kept for existing clients.
# Herdr takes `C-c` and `c-c` but not `C-C`, so its `-` chord is not simply
# case-insensitive; rather than model that, the relay allows the one spelling
# clients send. `ctrl+c` is the case-insensitive route to the same key.
SAFE_KEY_CHORDS = frozenset({"C-c"})
SAFE_MODIFIERS = frozenset({"ctrl", "shift"})
FUNCTION_KEY_RE = re.compile(r"f(?:[1-9]|1[0-2])")
# A modifier may be applied to any ASCII letter or digit, not just y/n/a.
CHORD_BASE_RE = re.compile(r"[a-z0-9]")

# Keys Herdr 0.8 has no name for. The relay generates the xterm CSI sequence
# and delivers it with `pane send-text`; a client still cannot put escape
# bytes of its own into `send_keys`.
NAV_SEQUENCES = {
    "home": "\x1b[H",
    "end": "\x1b[F",
    "pageup": "\x1b[5~",
    "pagedown": "\x1b[6~",
}


def _is_bare_key(key):
    """True for a key a client may send on its own.

    Names are matched case-insensitively because Herdr matches them that way.
    A literal -- a single character, or the `C-c` special case -- is matched
    exactly. Herdr would take `Y` and `c-c` as well; the allowlist keeps the
    spellings clients are offered rather than every spelling Herdr tolerates.
    """
    lowered = key.lower()
    return (
        key in SAFE_KEY_CHARS
        or key in SAFE_KEY_CHORDS
        or lowered in SAFE_KEY_NAMES
        or lowered in NAV_SEQUENCES
        or bool(FUNCTION_KEY_RE.fullmatch(lowered))
    )


def _is_chord_base(lowered):
    """True for a key a modifier may be applied to."""
    return (
        lowered in SAFE_KEY_NAMES
        or bool(CHORD_BASE_RE.fullmatch(lowered))
        or bool(FUNCTION_KEY_RE.fullmatch(lowered))
    )


def is_safe_key(key):
    """True when a client may send `key` to a pane.

    Accepts what Herdr 0.8 accepts, minus the chords and function keys the
    relay withholds, plus the four navigation keys the relay turns into CSI
    text. Anything else -- a raw escape sequence included -- is rejected,
    which rejects the whole frame.
    """
    if not isinstance(key, str) or not key:
        return False
    if _is_bare_key(key):
        return True
    *modifiers, base = key.lower().split("+")
    if not modifiers or len(set(modifiers)) != len(modifiers):
        return False
    if any(modifier not in SAFE_MODIFIERS for modifier in modifiers):
        return False
    return _is_chord_base(base)


def key_action(key):
    """Map one client key to how it reaches the pane.

    Returns ("keys", argument) for a name Herdr 0.8 takes as a send-keys
    argument, or ("text", sequence) for a navigation key it has no name for.
    """
    sequence = NAV_SEQUENCES.get(key.lower())
    if sequence is None:
        return "keys", key
    return "text", sequence


def key_runs(keys):
    """Group `keys` into the ordered Herdr calls that deliver them.

    Adjacent send-keys arguments share one call; each navigation key becomes
    its own send-text call, because `pane send-text` carries one payload.
    The client's order is preserved, so Home followed by Enter still arrives
    in that order.
    """
    runs = []
    for key in keys:
        kind, payload = key_action(key)
        if kind == "keys" and runs and runs[-1][0] == "keys":
            runs[-1][1].append(payload)
        else:
            runs.append((kind, [payload]))
    return runs


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
