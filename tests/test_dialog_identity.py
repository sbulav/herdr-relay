"""Dialog identity is the question on screen, not the whole screen (#65).

The screens below are sanitised captures of real blocked panes: Claude Code
2.1.283 and Codex 0.146.0 asked to run one command, read with
`herdr pane read --source visible`. Only the lines each test varies are
parameters; everything else is what the harness actually drew.
"""
import asyncio
import contextlib
import json
import unittest
from unittest.mock import AsyncMock, patch

from relay import herdr_relay

CLAUDE_TIP = 'Tip: auto mode handles these prompts for you — choose "switch to auto mode" below'
CLAUDE_OPTIONS = (
    "1. Yes",
    "2. Yes, and always allow access to /srv/repo from this project",
    "3. Yes, and switch to auto mode · auto mode handles these prompts for you",
    "4. No",
)
CODEX_OPTIONS = (
    "1. Yes, proceed (y)",
    "2. Yes, and don't ask again for commands that start with `touch screen-a` (p)",
    "3. No, and tell Codex what to do differently (esc)",
)


def claude_screen(
    *, activity="  Creating empty file screen-a", cursor=1, tip=CLAUDE_TIP,
    question="Do you want to proceed?", command="touch screen-a",
    options=CLAUDE_OPTIONS, footer="Esc to cancel · Tab to amend",
):
    lines = [
        "",
        " ▐▛███▜▌   Claude Code v2.1.283",
        "▝▜█████▛▘  Opus 5.5 with high effort · Claude Team",
        "  ▘▘ ▝▝    /srv/repo",
        "",
        "❯ Use the Bash tool to run exactly this command and nothing else: touch screen-a",
        "",
        activity,
        "  ⎿  $ touch screen-a",
        "",
        "─" * 80,
        " Bash command",
        f" {tip}",
        "",
        f"   {command}",
        "   Create empty file screen-a",
        "",
        f" {question}",
    ]
    lines += [
        (" ❯ " if number == cursor else "   ") + option
        for number, option in enumerate(options, 1)
    ]
    lines += ["", f" {footer}"]
    return "\n".join(lines)


def codex_screen(*, activity="• Running touch screen-a", cursor=1, banner=True):
    lines = [
        "╭──────────────────────────────────────────────────────╮",
        "│ >_ OpenAI Codex (v0.146.0)                           │",
        "│                                                      │",
        "│ model:     gpt-5.6-luna medium   /model to change    │",
        "│ directory: /srv/repo                                 │",
        "╰──────────────────────────────────────────────────────╯",
        "",
        "  Tip: NEW: Prevent sleep while running is now available in /experimental.",
        "",
    ]
    if banner:
        lines += ["• You have 1 usage limit reset available. Run /usage to use one.", "", ""]
    lines += [
        "› Use your shell tool to run exactly this command and nothing else: touch screen-a",
        "",
        "",
        activity,
        "",
        "",
        "  Would you like to run the following command?",
        "",
        "  Environment: local",
        "",
        "  $ touch screen-a",
        "",
    ]
    lines += [
        ("› " if number == cursor else "  ") + option
        for number, option in enumerate(CODEX_OPTIONS, 1)
    ]
    lines += ["", "  Press enter to confirm or esc to cancel"]
    return "\n".join(lines)


def isolated_state():
    stack = contextlib.ExitStack()
    for name in (
        "last_statuses", "pane_activity", "pane_revisions", "pane_attention_states",
        "pane_dialogs", "pane_dialog_revisions", "pane_response_options", "subscriptions",
        "pane_hosts", "pane_remote_map",
    ):
        stack.enter_context(patch.dict(getattr(herdr_relay.state, name), {}, clear=True))
    return stack


def observe(screen, pane_id="pane-1", host="local"):
    """Run one capture through the same two calls every read path makes."""
    return herdr_relay.dialogs.ensure(
        pane_id, screen, herdr_relay.panes.detect_options(screen), host=host,
    )


class RedrawKeepsIdentityTests(unittest.TestCase):
    """Captures that differ outside the question are one dialog."""

    def assertSameDialog(self, first_screen, second_screen):
        with isolated_state():
            first = dict(observe(first_screen))
            second = observe(second_screen)
        self.assertEqual(first["dialog_id"], second["dialog_id"])
        self.assertEqual(first["revision"], second["revision"])

    def test_claude_transcript_marker_blink(self):
        # The two real captures of one Claude prompt differed only in this line.
        self.assertSameDialog(
            claude_screen(activity="  Creating empty file screen-a"),
            claude_screen(activity="● Creating empty file screen-a"),
        )

    def test_claude_spinner_and_elapsed_timer(self):
        self.assertSameDialog(
            claude_screen(activity="✻ Crafting… (3s · esc to interrupt)"),
            claude_screen(activity="✶ Crafting… (1m 9s · esc to interrupt)"),
        )

    def test_claude_cursor_moves(self):
        self.assertSameDialog(claude_screen(cursor=1), claude_screen(cursor=4))

    def test_claude_rotating_tip(self):
        self.assertSameDialog(
            claude_screen(tip=CLAUDE_TIP),
            claude_screen(tip="Tip: press Tab to amend the command before it runs"),
        )

    def test_claude_footer_hint(self):
        self.assertSameDialog(
            claude_screen(footer="Esc to cancel · Tab to amend"),
            claude_screen(footer="Esc to cancel · Tab to amend · ctrl+e to explain"),
        )

    def test_codex_spinner_timer_and_banner(self):
        self.assertSameDialog(
            codex_screen(activity="• Working (3s • esc to interrupt)", banner=True),
            codex_screen(activity="• Working (41s • esc to interrupt)", banner=False),
        )

    def test_codex_cursor_moves(self):
        self.assertSameDialog(codex_screen(cursor=1), codex_screen(cursor=3))

    def test_redraw_does_not_replace_displayed_prompt(self):
        # The frame is what clients diff; a redraw must not change any of it.
        with isolated_state():
            first = herdr_relay.dialogs.frame(observe(claude_screen(cursor=1)))
            second = herdr_relay.dialogs.frame(observe(claude_screen(cursor=2)))
        self.assertEqual(first, second)


class QuestionChangeRenewsIdentityTests(unittest.TestCase):
    def assertNewDialog(self, first_screen, second_screen):
        with isolated_state():
            first = dict(observe(first_screen))
            second = observe(second_screen)
        self.assertNotEqual(first["dialog_id"], second["dialog_id"])
        self.assertEqual(first["revision"] + 1, second["revision"])

    def test_changed_question(self):
        self.assertNewDialog(
            claude_screen(question="Do you want to proceed?"),
            claude_screen(question="Do you want to make this edit?"),
        )

    def test_changed_command_in_question_block(self):
        self.assertNewDialog(
            claude_screen(command="touch screen-a"),
            claude_screen(command="rm -rf screen-a"),
        )

    def test_changed_option_set(self):
        self.assertNewDialog(
            claude_screen(),
            claude_screen(options=(CLAUDE_OPTIONS[0], CLAUDE_OPTIONS[3].replace("4.", "2."))),
        )

    def test_same_question_on_another_host_is_another_dialog(self):
        screen = claude_screen()
        with isolated_state():
            local = dict(observe(screen, host="local"))
            remote = observe(screen, host="buildbox")
        self.assertNotEqual(local["dialog_id"], remote["dialog_id"])


class CodexCursorMarkerTests(unittest.TestCase):
    def test_highlighted_codex_option_is_detected(self):
        for cursor in (1, 2, 3):
            with self.subTest(cursor=cursor):
                self.assertEqual(
                    list(CODEX_OPTIONS),
                    herdr_relay.panes.detect_options(codex_screen(cursor=cursor)),
                )


class QuestionOnlyPromptTests(unittest.TestCase):
    def test_text_without_option_lines_keys_on_all_of_it(self):
        # Pushed prompts and bare questions carry no option lines to anchor on;
        # their identity is the text itself, exactly as before #65.
        key = herdr_relay.dialogs._prompt_key(
            "Choose\nwisely", ["yes", "no"], agent="", project="", host="local",
        )
        self.assertEqual(json.dumps(["local", "Choose\nwisely", ["yes", "no"]], ensure_ascii=False,
                                    separators=(",", ":")), key)

    def test_question_only_screen_keeps_its_pre_65_key(self):
        # The `blocked` golden's dialog_id depends on this staying byte-identical.
        text = "Do you want to proceed?\n1. Yes\n2. No"
        choices = ["1. Yes", "2. No"]
        key = herdr_relay.dialogs._prompt_key(text, choices, agent="", project="", host="buildbox")
        self.assertEqual(json.dumps(["buildbox", text, choices], ensure_ascii=False,
                                    separators=(",", ":")), key)


class ResponseAfterRedrawTests(unittest.TestCase):
    @staticmethod
    def socket(messages):
        class Socket:
            request_headers = {}

            def __init__(self):
                self.requests = iter([json.dumps(message) for message in messages])
                self.sent = []

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self.requests)
                except StopIteration:
                    raise StopAsyncIteration

            async def send(self, raw):
                self.sent.append(json.loads(raw))

        return Socket()

    def test_answer_built_from_earlier_capture_is_accepted(self):
        with (
            isolated_state(),
            patch.object(herdr_relay.state, "known_panes", {"pane-1"}),
            patch.object(herdr_relay.state, "known_pane_keys", {("local", "pane-1")}),
            patch.dict(herdr_relay.state.pane_hosts, {"pane-1": {"local"}}),
            patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, "")) as checked,
        ):
            # The phone renders this capture; the agent then redraws its
            # spinner and cursor before the tap arrives.
            seen = dict(observe(claude_screen(activity="  Creating empty file screen-a", cursor=1)))
            observe(claude_screen(activity="● Creating empty file screen-a", cursor=3))
            socket = self.socket([{
                "type": "respond_dialog", "request_id": "answer-1", "pane_id": "pane-1",
                "dialog_id": seen["dialog_id"], "revision": seen["revision"], "text": "1. Yes",
            }])
            asyncio.run(herdr_relay.handle_client(socket))

        frames = [frame for frame in socket.sent if frame["type"] != "server_info"]
        self.assertEqual("command_ack", frames[0]["type"], frames[0])
        checked.assert_called_once()


class PollRedrawTests(unittest.TestCase):
    def test_redraw_between_polls_neither_rebroadcasts_nor_renotifies(self):
        agent = {
            "pane_id": "pane-1", "agent": "claude", "label": "", "project": "repo",
            "status": "blocked", "cwd": "/srv/repo", "host": "local", "remote": None,
            "output_revision": 1,
        }
        screens = iter([
            claude_screen(activity="  Creating empty file screen-a", cursor=1),
            claude_screen(activity="● Creating empty file screen-a", cursor=2),
            claude_screen(activity="✻ Crafting… (12s · esc to interrupt)", cursor=4),
        ])
        sent = []

        async def broadcast(frame):
            sent.append(frame)

        with (
            isolated_state(),
            patch.object(herdr_relay.herdr, "get_all_agents", return_value=([dict(agent)], [])),
            patch.object(herdr_relay.herdr, "read_pane", side_effect=lambda *a, **k: next(screens)),
            patch.object(herdr_relay.transport, "broadcast", side_effect=broadcast),
            patch.object(herdr_relay.push, "send_web_push", new_callable=AsyncMock) as web_push,
        ):
            for _ in range(3):
                asyncio.run(herdr_relay.transport._poll_once())

        blocked = [frame for frame in sent if frame["type"] == "blocked"]
        self.assertEqual(1, len(blocked))
        self.assertEqual(1, blocked[0]["revision"])
        web_push.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
