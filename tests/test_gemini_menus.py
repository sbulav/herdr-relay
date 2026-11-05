"""gemini-cli numbered menus: a `●` cursor, and often a box around them (#97).

The three screens come from dcolinmorgan/herdr-remote@d7a439e: two gemini-cli
render snapshots and a live sign-in screen. Its expected labels, given here in
the relay's `N. label` form, are the ones upstream's relay produced.
"""
import unittest

from relay import herdr_relay

panes = herdr_relay.panes

# The first row keeps the trailing blanks gemini-cli draws after it.
GEMINI_APPROVAL = """\
MCP Server: test-server
Tool: test-tool
Allow execution of MCP tool "test-tool" from server "test-server"?

● 1. Allow once
  2. Allow tool for this session
  3. Allow all server tools for this session
  4. No, suggest changes (esc)
"""

GEMINI_BOXED_APPROVAL = """\
╭──────────────────────────────────────────────────────────────────────────╮
│ ?  test-tool a test tool                                               ← │
│                                                                          │
│ ... first 49 lines hidden ...                                            │
│ 50  line 50                                                              │
│ Apply this change?                                                       │
│                                                                          │
│ ● 1. Allow once                                                          │
│   2. Allow for this session                                              │
│   3. Modify with external editor                                         │
│   4. No, suggest changes (esc)                                           │
│                                                                          │
╰──────────────────────────────────────────────────────────────────────────╯
 Press ctrl-o to show more lines
"""

GEMINI_SIGN_IN = """\
╭───────────────────────────────────────────────────────────────────────────╮
│ Get started                                                               │
│                                                                           │
│ How would you like to authenticate for this project?                      │
│                                                                           │
│   ● 1. Sign in with Google                                                │
│     2. Use Gemini API Key                                                 │
│     3. Vertex AI                                                          │
│                                                                           │
│ (Use Enter to select)                                                     │
╰───────────────────────────────────────────────────────────────────────────╯
"""


def unbox(screen):
    return "\n".join(panes.unboxed(line) for line in screen.split("\n"))


class GeminiMenuTests(unittest.TestCase):
    def test_reads_the_row_that_carries_the_dot_cursor(self):
        self.assertEqual([
            "1. Allow once",
            "2. Allow tool for this session",
            "3. Allow all server tools for this session",
            "4. No, suggest changes (esc)",
        ], panes.detect_options(GEMINI_APPROVAL))

    def test_reads_a_menu_drawn_inside_a_box(self):
        self.assertEqual([
            "1. Allow once",
            "2. Allow for this session",
            "3. Modify with external editor",
            "4. No, suggest changes (esc)",
        ], panes.detect_options(GEMINI_BOXED_APPROVAL))

    def test_reads_the_sign_in_menu(self):
        self.assertEqual(
            ["1. Sign in with Google", "2. Use Gemini API Key", "3. Vertex AI"],
            panes.detect_options(GEMINI_SIGN_IN),
        )

    def test_a_boxed_menu_reads_as_the_same_menu_unboxed(self):
        for screen in (GEMINI_BOXED_APPROVAL, GEMINI_SIGN_IN):
            self.assertEqual(panes.detect_options(unbox(screen)), panes.detect_options(screen))

    def test_the_dot_reads_as_claudes_cursor_does(self):
        claude = GEMINI_APPROVAL.replace("● 1.", "❯ 1.")
        self.assertEqual(panes.detect_options(claude), panes.detect_options(GEMINI_APPROVAL))

    def test_joins_an_option_that_wraps_inside_the_box(self):
        screen = (
            "│ ● 1. Allow once                  │\n"
            "│   2. Allow this command for all  │\n"
            "│      future sessions             │\n"
            "│   3. No, suggest changes (esc)   │\n"
        )
        self.assertEqual(
            "2. Allow this command for all future sessions", panes.detect_options(screen)[1],
        )

    def test_a_single_boxed_number_is_not_a_menu(self):
        self.assertEqual([], panes.numbered_menu("│ 1. only one │\n│ text │"))

    def test_the_question_block_is_the_boxed_question(self):
        choices = panes.detect_options(GEMINI_BOXED_APPROVAL)
        block = panes.question_block(GEMINI_BOXED_APPROVAL, choices)
        self.assertIn("Apply this change?", block)
        self.assertIn("1. Allow once", block)
        self.assertIn("4. No, suggest changes (esc)", block)
        self.assertNotIn("│", block)
        self.assertNotIn("ctrl-o", block)

    def test_moving_the_dot_keeps_the_question_block(self):
        moved = (
            GEMINI_SIGN_IN
            .replace("│   ● 1. Sign in with Google", "│     1. Sign in with Google")
            .replace("│     2. Use Gemini API Key  ", "│   ● 2. Use Gemini API Key  ")
        )
        self.assertNotEqual(moved, GEMINI_SIGN_IN)
        choices = panes.detect_options(GEMINI_SIGN_IN)
        self.assertEqual(choices, panes.detect_options(moved))
        self.assertEqual(
            panes.question_block(GEMINI_SIGN_IN, choices), panes.question_block(moved, choices),
        )

    def test_two_boxed_questions_with_the_same_choices_have_different_blocks(self):
        other = GEMINI_BOXED_APPROVAL.replace("Apply this change?", "Apply that change?")
        choices = panes.detect_options(GEMINI_BOXED_APPROVAL)
        self.assertNotEqual(
            panes.question_block(GEMINI_BOXED_APPROVAL, choices),
            panes.question_block(other, choices),
        )

    def test_a_boxed_menu_below_opencode_text_is_not_an_opencode_dialog(self):
        screen = "△ Permission required\n" + GEMINI_BOXED_APPROVAL
        self.assertEqual("1. Allow once", panes.detect_options(screen)[0])


class BoxedTextIsNotAMenuTests(unittest.TestCase):
    """A row inside someone else's box stays text unless it is a numbered row."""

    def test_a_bullet_outside_a_numbered_row_is_still_a_bullet(self):
        self.assertEqual("● Shell ls", panes._block_line("● Shell ls"))
        self.assertEqual("1. Yes", panes._block_line("● 1. Yes"))

    def test_claudes_welcome_box_offers_nothing(self):
        screen = (
            "╭───────────────────────────────────────────╮\n"
            "│ ✻ Welcome to Claude Code!                 │\n"
            "│                                           │\n"
            "│   /help for help, /status for your setup  │\n"
            "╰───────────────────────────────────────────╯\n"
        )
        self.assertIsNone(panes.detect_options(screen))

    def test_text_in_the_box_after_the_menu_does_not_join_the_last_label(self):
        self.assertEqual("3. Vertex AI", panes.detect_options(GEMINI_SIGN_IN)[-1])

    def test_numbered_table_rows_are_not_a_menu(self):
        screen = (
            "┌────┬──────────┐\n"
            "│ 1. │ Read     │\n"
            "│ 2. │ Write    │\n"
            "└────┴──────────┘\n"
        )
        self.assertEqual([], panes.numbered_menu(screen))
        self.assertEqual("│ 1. │ Read │", panes.unboxed("│ 1. │ Read │"))

    def test_a_numbered_list_in_a_box_above_the_menu_loses_to_the_menu(self):
        screen = (
            "╭─────────────────────────────╮\n"
            "│ Here is Claude's plan:      │\n"
            "│ 1. Read the code            │\n"
            "│ 2. Write the fix            │\n"
            "╰─────────────────────────────╯\n"
            " Would you like to proceed?\n"
            " ❯ 1. Yes\n"
            "   2. No, keep planning\n"
        )
        self.assertEqual(["1. Yes", "2. No, keep planning"], panes.detect_options(screen))

    def test_opencodes_heavy_bar_is_not_a_box(self):
        line = "┃  1. Allow once"
        self.assertEqual(line, panes.unboxed(line))


if __name__ == "__main__":
    unittest.main()
