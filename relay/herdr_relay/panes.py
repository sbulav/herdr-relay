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
# The highlighted row carries a cursor marker: `❯` in Claude Code, `›` in Codex.
# One row of a numbered menu: an optional cursor marker, then `N.` or `N)`.
# Group 1 is everything before the number, whose width is the menu's number
# column; a deeper-indented line that follows continues the row's label.
NUMBERED_OPTION_RE = re.compile(r"^(\s*(?:[❯>›»▶]\s*)?)(\d{1,2})[.)]\s+(\S.*?)\s*$")
# A horizontal rule drawn inside a menu: box-drawing or ASCII dashes only.
MENU_RULE_RE = re.compile(r"^[\u2500-\u257f\u2014\u2013\-=_]{3,}$")
# The row Claude Code appends to every AskUserQuestion menu. Choosing it turns
# the row into an inline text field; the menu itself stays on screen.
FREE_TEXT_ROW_RE = re.compile(r"^(\d{1,2})\. type something\.?$", re.IGNORECASE)
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


def numbered_menu(text):
    """The last complete `1.`..`N.` menu on screen, as (number, label) pairs.

    A run starts at 1 and counts up by one per row. A line indented deeper
    than the number column continues the previous label (Claude wraps long
    options, and draws a question's descriptions, that way); a rule is
    stepped over, because Claude draws one between a question's answers and
    the rows it always appends. Any other non-blank line -- the footer, which
    sits left of the numbers -- ends the run. The last run of two or more
    wins: a blocked pane draws its menu under whatever the agent printed, and
    that may be a numbered list too.
    """
    best = []
    current = []
    number_col = None
    for line in text.splitlines():
        match = NUMBERED_OPTION_RE.match(line)
        if match:
            number = int(match.group(2))
            if number == 1:
                current = [match.group(3)]
                number_col = len(match.group(1))
            elif current and number == len(current) + 1:
                current.append(match.group(3))
            else:
                current = []
                number_col = None
            if len(current) >= 2:
                best = list(current)
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if current and MENU_RULE_RE.match(stripped):
            continue
        indent = len(line) - len(line.lstrip())
        if current and number_col is not None and indent > number_col:
            current[-1] = f"{current[-1]} {stripped}"
            if len(current) >= 2:
                best = list(current)
            continue
        current = []
        number_col = None
    return list(enumerate(best, 1))


def _numbered_options(text):
    # A `N)` row is offered as `N. label`, the form respond_action maps to N.
    options = [f"{number}. {label}" for number, label in numbered_menu(text)]
    return options or None


def free_text_row(text, choices):
    """The row number of a Claude Code question's "Type something." field, or None.

    The field is offered only on the question menu itself: the row must be
    one of the detected `choices` and the menu's own "Enter to select" footer
    must be on screen. Once text is typed into the field the row shows that
    text instead, so a half-typed answer is not offered as free text again.
    """
    if "enter to select" not in (text or "").lower():
        return None
    for choice in choices or ():
        match = FREE_TEXT_ROW_RE.match(choice.strip())
        if match:
            return int(match.group(1))
    return None


def text_field_focused(text):
    """True while a Claude Code question's text field has focus.

    The footer gains "ctrl+g to edit in <editor>" exactly then; the editor
    name is the reader's $EDITOR, so only the invariant half is matched. With
    the field focused a digit is typed into it instead of picking a row.
    """
    return "ctrl+g to edit in" in (text or "").lower()


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


# How far above the last option a question block may reach, in lines.
QUESTION_BLOCK_LINES = 16
CURSOR_PREFIX_RE = re.compile(r"^[❯›>][ \t]*")
# A box-drawing line (a rule, or a box's edge) ends the region a TUI draws its
# question in: Claude Code rules its permission prompt off from the transcript.
BOX_LINE_RE = re.compile(r"^[\u2500-\u257f]")


def _block_line(line):
    """One screen line with its indentation and cursor marker removed."""
    return CURSOR_PREFIX_RE.sub("", line.strip())


def _offers(line, labels):
    """True when one screen line draws one of `labels`.

    A numbered row is compared in its `N. label` form, and matches when it is
    the start of a label too: a label joined from wrapped or described rows
    is longer than its first screen line.
    """
    cleaned = _block_line(line).lower()
    row = NUMBERED_OPTION_RE.match(line)
    if row:
        cleaned = f"{row.group(2)}. {row.group(3)}".lower()
    return any(
        cleaned.startswith(label) or (row is not None and label.startswith(cleaned))
        for label in labels
    )


def question_block(text, choices):
    """Return the part of a blocked screen that states the question, or None.

    Anchored on the last line that offers one of `choices`, it reaches upward
    to the nearest rule or box edge, a run of two blank lines, or
    QUESTION_BLOCK_LINES above the menu's first row, whichever is closest. A
    rule drawn inside the menu, directly under one of its rows, does not end
    it. Everything a TUI redraws while the question stands still -- the
    transcript and its spinners and timers above, footer hints below, the
    moving cursor marker, rotating tips, blank lines -- is left out.

    None when no line offers a choice (a pushed prompt, a bare question): the
    caller then has nothing narrower than the whole text.
    """
    labels = [choice.lower() for choice in choices or () if len(choice) >= 2]
    lines = text.split("\n")
    anchor = next((
        index for index in range(len(lines) - 1, -1, -1)
        if _offers(lines[index], labels)
    ), None)
    if anchor is None:
        return None
    start = top = anchor
    while start > 0 and top - start < QUESTION_BLOCK_LINES:
        above = lines[start - 1].strip()
        if BOX_LINE_RE.match(above):
            nearest = next((
                lines[index] for index in range(start - 2, -1, -1) if lines[index].strip()
            ), None)
            if nearest is None or not _offers(nearest, labels):
                break
        elif _offers(lines[start - 1], labels):
            top = start - 1
        if not above and start > 1 and not lines[start - 2].strip():
            break
        start -= 1
    kept = []
    for line in lines[start:anchor + 1]:
        cleaned = _block_line(line)
        if not cleaned or CHROME_RE.search(cleaned) or cleaned.lower().startswith("tip:"):
            continue
        kept.append(cleaned)
    return "\n".join(kept)


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
