"""Claude Code menus parsed whole, and free-text answers to their questions (#72).

The fixtures are Claude Code 2.1 screens: an approval menu with a wrapped
option, and an AskUserQuestion menu whose "Type something." row is a text
field. The field's behaviour was taken from a real session: a row digit moves
focus to its row, a digit pressed while the field has focus is typed into it,
and the footer names ctrl+g exactly while it has focus.
"""
import asyncio
import json
import unittest
from unittest.mock import call, patch

from relay import herdr_relay

from tests.test_structured_output import after_handshake


CLAUDE_MENU = """\
● Creating empty test file
  ⎿  $ touch /tmp/herdr-perm-test.txt
────────────────────────────────────────────────────────────────────
 Bash command
   touch /tmp/herdr-perm-test.txt
   Create empty test file
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and always allow access to /tmp from this project
   3. Yes, and switch to auto mode · auto mode handles these prompts
      for you
   4. No
 Esc to cancel · Tab to amend
"""

CLAUDE_QUESTION_MENU = """\
● I'll ask which way to go.
────────────────────────────────────────
 ☐ Way

 Which way?

 ❯ 1. First
     the first one
   2. Second
     the second one
   3. Type something.
────────────────────────────────────────
   4. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
"""

# The same question after the cursor moved onto the field: the footer names
# the reader's editor, which is how focus is told apart.
CLAUDE_QUESTION_FIELD = (
    CLAUDE_QUESTION_MENU
    .replace(" ❯ 1. First", "   1. First")
    .replace("   3. Type something.", " ❯ 3. Type something.")
    .replace(
        "Enter to select · ↑/↓ to navigate · Esc to cancel",
        "Enter to select · ↑/↓ to navigate · ctrl+g to edit in Nvim · Esc to cancel",
    )
)

QUESTION_CHOICES = [
    "1. First the first one",
    "2. Second the second one",
    "3. Type something.",
    "4. Chat about this",
]


class NumberedMenuTests(unittest.TestCase):
    def test_approval_menu_offers_every_row_with_wrapped_labels_joined(self):
        self.assertEqual([
            "1. Yes",
            "2. Yes, and always allow access to /tmp from this project",
            "3. Yes, and switch to auto mode · auto mode handles these prompts for you",
            "4. No",
        ], herdr_relay.panes.detect_options(CLAUDE_MENU))

    def test_question_menu_steps_over_its_rule_and_stops_at_the_footer(self):
        self.assertEqual(QUESTION_CHOICES, herdr_relay.panes.detect_options(CLAUDE_QUESTION_MENU))

    def test_the_cursor_marker_does_not_change_the_menu(self):
        self.assertEqual(QUESTION_CHOICES, herdr_relay.panes.detect_options(CLAUDE_QUESTION_FIELD))

    def test_parenthesised_numbers_are_offered_in_dot_form(self):
        self.assertEqual(
            ["1. Alpha", "2. Beta"],
            herdr_relay.panes.detect_options("Pick one\n› 1) Alpha\n  2) Beta\n"),
        )

    def test_the_last_menu_wins_over_a_numbered_list_above_it(self):
        screen = (
            "Plan:\n1. Read the code\n2. Write the fix\n3. Run the tests\n"
            "Shall I go ahead?\n❯ 1. Yes\n  2. No\n"
        )
        self.assertEqual(["1. Yes", "2. No"], herdr_relay.panes.detect_options(screen))

    def test_prose_between_rows_breaks_the_run(self):
        self.assertEqual([], herdr_relay.panes.numbered_menu("1. Yes\nsomething else\n2. No\n"))

    def test_a_run_must_start_at_one(self):
        self.assertEqual([], herdr_relay.panes.numbered_menu("2. Yes\n3. No\n"))

    def test_question_block_reaches_the_question_across_the_menu_rule(self):
        block = herdr_relay.panes.question_block(CLAUDE_QUESTION_MENU, QUESTION_CHOICES)
        self.assertIn("Which way?", block)
        self.assertIn("4. Chat about this", block)
        self.assertNotIn("I'll ask which way to go.", block)
        self.assertNotIn("Enter to select", block)


class FreeTextCapabilityTests(unittest.TestCase):
    def test_free_text_row_is_the_type_something_row(self):
        self.assertEqual(3, herdr_relay.panes.free_text_row(CLAUDE_QUESTION_MENU, QUESTION_CHOICES))

    def test_no_free_text_row_without_the_question_footer(self):
        screen = CLAUDE_QUESTION_MENU.replace("Enter to select", "Enter to confirm")
        self.assertIsNone(herdr_relay.panes.free_text_row(screen, QUESTION_CHOICES))

    def test_no_free_text_row_on_an_approval_menu(self):
        choices = herdr_relay.panes.detect_options(CLAUDE_MENU)
        self.assertIsNone(herdr_relay.panes.free_text_row(CLAUDE_MENU, choices))

    def test_focus_is_read_from_the_footer(self):
        self.assertFalse(herdr_relay.panes.text_field_focused(CLAUDE_QUESTION_MENU))
        self.assertTrue(herdr_relay.panes.text_field_focused(CLAUDE_QUESTION_FIELD))

    def test_raw_input_is_allowed_only_for_the_question_shape(self):
        with (
            patch.dict(herdr_relay.state.pane_dialogs, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialog_revisions, {}, clear=True),
        ):
            question = herdr_relay.dialogs.ensure(
                "pane-1", CLAUDE_QUESTION_MENU, herdr_relay.panes.detect_options(CLAUDE_QUESTION_MENU)
            )
            approval = herdr_relay.dialogs.ensure(
                "pane-2", CLAUDE_MENU, herdr_relay.panes.detect_options(CLAUDE_MENU)
            )
            undetected = herdr_relay.dialogs.ensure("pane-3", "Waiting", None)

        self.assertTrue(herdr_relay.dialogs.frame(question)["raw_input_allowed"])
        self.assertFalse(herdr_relay.dialogs.frame(approval)["raw_input_allowed"])
        self.assertFalse(herdr_relay.dialogs.frame(undetected)["raw_input_allowed"])

    def test_moving_focus_keeps_the_dialog_and_updates_its_focus(self):
        with (
            patch.dict(herdr_relay.state.pane_dialogs, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialog_revisions, {}, clear=True),
        ):
            first = herdr_relay.dialogs.ensure("pane-1", CLAUDE_QUESTION_MENU, QUESTION_CHOICES)
            self.assertFalse(first["text_field_focused"])
            focused = herdr_relay.dialogs.ensure("pane-1", CLAUDE_QUESTION_FIELD, QUESTION_CHOICES)

        self.assertIs(first, focused)
        self.assertTrue(focused["text_field_focused"])
        self.assertNotIn("text_field_focused", herdr_relay.dialogs.frame(focused))
        self.assertNotIn("free_text_row", herdr_relay.dialogs.frame(focused))

    def test_free_text_must_be_one_printable_line_and_not_a_choice(self):
        dialog = {"raw_input_allowed": True, "choices": QUESTION_CHOICES}
        self.assertTrue(herdr_relay.dialogs.free_text_allowed(dialog, "Mango, ripe"))
        self.assertFalse(herdr_relay.dialogs.free_text_allowed(dialog, "1. First the first one"))
        self.assertFalse(herdr_relay.dialogs.free_text_allowed(dialog, "two\nlines"))
        self.assertFalse(herdr_relay.dialogs.free_text_allowed(dialog, "a\x1b[Bb"))
        self.assertFalse(herdr_relay.dialogs.free_text_allowed(
            {**dialog, "raw_input_allowed": False}, "Mango, ripe"
        ))


class FreeTextDeliveryTests(unittest.TestCase):
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

    def answer(self, screen, texts, *, results=None):
        """Answer the dialog `screen` raises with each of `texts`, in order.

        Returns the frames sent back and the herdr calls made. Each answer is
        its own request; a failed one is not remembered, so a retry of it may
        reuse its id or not.
        """
        checked_kwargs = {"side_effect": results} if results else {"return_value": (True, "")}
        with (
            patch.object(herdr_relay.state, "known_panes", {"pane-1"}),
            patch.object(herdr_relay.state, "known_pane_keys", {("local", "pane-1")}),
            patch.dict(herdr_relay.state.pane_hosts, {"pane-1": {"local"}}, clear=True),
            patch.dict(herdr_relay.state.last_statuses, {}, clear=True),
            patch.dict(herdr_relay.state.pane_remote_map, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialogs, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialog_revisions, {}, clear=True),
            patch.object(herdr_relay.herdr, "run_herdr_checked", **checked_kwargs) as checked,
        ):
            dialog = herdr_relay.dialogs.ensure(
                "pane-1", screen, herdr_relay.panes.detect_options(screen)
            )
            socket = self.socket([{
                "type": "respond_dialog", "request_id": f"answer-{index}", "pane_id": "pane-1",
                "dialog_id": dialog["dialog_id"], "revision": dialog["revision"], "text": text,
            } for index, text in enumerate(texts)])
            asyncio.run(herdr_relay.handle_client(socket))
        return after_handshake(socket.sent), checked.call_args_list

    @staticmethod
    def herdr_call(*args):
        return call(
            "pane", *args, remote=None, host_id="local", command=[herdr_relay.config.HERDR]
        )

    def test_free_text_focuses_the_field_types_and_submits(self):
        frames, calls = self.answer(CLAUDE_QUESTION_MENU, ["Mango, ripe"])

        self.assertEqual("command_ack", frames[0]["type"])
        self.assertEqual([
            self.herdr_call("send-keys", "pane-1", "3"),
            self.herdr_call("send-text", "pane-1", "Mango, ripe"),
            self.herdr_call("send-keys", "pane-1", "Enter"),
        ], calls)

    def test_free_text_into_a_focused_field_skips_the_digit(self):
        frames, calls = self.answer(CLAUDE_QUESTION_FIELD, ["Mango, ripe"])

        self.assertEqual("command_ack", frames[0]["type"])
        self.assertEqual([
            self.herdr_call("send-text", "pane-1", "Mango, ripe"),
            self.herdr_call("send-keys", "pane-1", "Enter"),
        ], calls)

    def test_a_listed_choice_leaves_a_focused_field_first(self):
        frames, calls = self.answer(CLAUDE_QUESTION_FIELD, ["2. Second the second one"])

        self.assertEqual("command_ack", frames[0]["type"])
        self.assertEqual([
            self.herdr_call("send-keys", "pane-1", "Up"),
            self.herdr_call("send-text", "pane-1", "2\n"),
        ], calls)

    def test_a_listed_choice_on_an_unfocused_menu_is_unchanged(self):
        _frames, calls = self.answer(CLAUDE_QUESTION_MENU, ["2. Second the second one"])

        self.assertEqual([self.herdr_call("send-text", "pane-1", "2\n")], calls)

    def test_free_text_is_refused_where_the_dialog_has_no_field(self):
        frames, calls = self.answer(CLAUDE_MENU, ["Mango, ripe"])

        self.assertEqual("RESPONSE_NOT_ALLOWED", frames[0]["code"])
        self.assertEqual([], calls)

    def test_free_text_that_is_not_one_printable_line_is_refused(self):
        frames, calls = self.answer(CLAUDE_QUESTION_MENU, ["two\nlines", "esc\x1b[B"])

        self.assertEqual(["RESPONSE_NOT_ALLOWED"] * 2, [frame["code"] for frame in frames])
        self.assertEqual([], calls)

    def test_free_text_over_the_prompt_limit_is_refused(self):
        frames, calls = self.answer(
            CLAUDE_QUESTION_MENU, ["x" * (herdr_relay.config.MAX_PROMPT_CHARS + 1)]
        )

        self.assertEqual("INVALID_REQUEST", frames[0]["code"])
        self.assertEqual([], calls)

    def test_a_failed_step_stops_delivery_and_a_retry_does_not_repeat_the_digit(self):
        frames, calls = self.answer(
            CLAUDE_QUESTION_MENU,
            ["Mango, ripe", "Mango, ripe"],
            results=[(True, ""), (False, "gone"), (True, ""), (True, "")],
        )

        self.assertEqual("HERDR_FAILED", frames[0]["code"])
        self.assertEqual("command_ack", frames[1]["type"])
        self.assertEqual([
            self.herdr_call("send-keys", "pane-1", "3"),
            self.herdr_call("send-text", "pane-1", "Mango, ripe"),
            self.herdr_call("send-text", "pane-1", "Mango, ripe"),
            self.herdr_call("send-keys", "pane-1", "Enter"),
        ], calls)

    def test_an_answered_question_takes_no_second_answer(self):
        frames, calls = self.answer(CLAUDE_QUESTION_MENU, ["Mango, ripe", "Pear"])

        self.assertEqual("command_ack", frames[0]["type"])
        self.assertEqual("DIALOG_ALREADY_ANSWERED", frames[1]["code"])
        self.assertEqual(3, len(calls))


if __name__ == "__main__":
    unittest.main()
