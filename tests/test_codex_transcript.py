"""Codex rollouts become structured blocks, found only from herdr's session ref (#99).

The fixtures are synthetic rows in the shapes Codex's rollout recorder writes:
`response_item` rows carrying messages, tool calls and their outputs, beside the
`event_msg` mirrors and metadata the reader must ignore.
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from relay import herdr_relay

codex = herdr_relay.transcripts.codex
SESSION = "019a1b2c-3d4e-7f80-9a1b-2c3d4e5f6a7b"
OTHER = "019a1b2c-3d4e-7f80-9a1b-2c3d4e5f6a7c"


def item(kind, timestamp="2026-10-23T12:00:00.000Z", **payload):
    return {"timestamp": timestamp, "type": "response_item", "payload": {"type": kind, **payload}}


def message(role, text, **payload):
    part = "input_text" if role in ("user", "developer") else "output_text"
    return item("message", role=role, content=[{"type": part, "text": text}], **payload)


def call(call_id, command, name="shell", **payload):
    return item("function_call", name=name, call_id=call_id, arguments=json.dumps({"command": command}), **payload)


def rollout(*rows):
    return "".join(json.dumps(row) + "\n" for row in rows)


def write(root, rows, session=SESSION, dated=True, stamp="2026-10-23T12-00-00"):
    directory = Path(root, "2026", "10", "23") if dated else Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-{stamp}-{session}.jsonl"
    path.write_text(rollout(*rows), encoding="utf-8")
    return str(path)


class ParseTests(unittest.TestCase):
    def parse(self, *rows):
        return codex.rollout_to_blocks(rollout(*rows))

    def test_an_exchange_becomes_timestamped_blocks(self):
        blocks = self.parse(message("user", "does this work?", id="u1"), message("assistant", "it does", id="a1"))
        self.assertEqual(
            [(b["id"], b["kind"], b.get("text") or b.get("markdown")) for b in blocks],
            [("c:u1", "status", "does this work?"), ("c:a1", "assistant_text", "it does")],
        )
        self.assertEqual(blocks[0]["label"], "You")
        self.assertEqual(blocks[0]["role"], "user")
        self.assertEqual(blocks[0]["message_id"], "u1")
        self.assertEqual(blocks[0]["timestamp"], 1792756800000)

    def test_only_response_items_that_are_turns_are_read(self):
        blocks = self.parse(
            {"type": "session_meta", "payload": {"id": SESSION, "instructions": "private"}},
            {"type": "turn_context", "payload": {"cwd": "/private"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "hello"}},
            message("user", "<environment_context>\n  <cwd>/private</cwd>\n</environment_context>"),
            message("user", "<user_instructions>\nprivate\n</user_instructions>"),
            message("user", "# AGENTS.md instructions for /private\n\nprivate"),
            message("user", "hello"),
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "hi"}},
            message("assistant", "hi"),
            message("developer", "private developer text"),
            message("system", "private system text"),
            item("reasoning", summary=[{"type": "summary_text", "text": "private"}], encrypted_content="x"),
            message("assistant", "private analysis", channel="analysis"),
            item("web_search_call", action={"query": "x"}),
        )
        self.assertEqual([b.get("text") or b.get("markdown") for b in blocks], ["hello", "hi"])
        self.assertNotIn("private", json.dumps(blocks))

    def test_images_are_left_out_and_text_parts_joined(self):
        blocks = self.parse(item("message", role="user", content=[
            {"type": "input_text", "text": "look"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            {"type": "input_text", "text": "at this"},
        ]))
        self.assertEqual(blocks[0]["text"], "look\nat this")
        self.assertNotIn("base64", json.dumps(blocks))

    def test_a_call_and_its_new_shape_shell_output_share_one_block(self):
        blocks = self.parse(
            call("call_1", ["bash", "-lc", "echo hello"], id="fc1"),
            item("function_call_output", call_id="call_1", output=(
                "Chunk ID: 4f2a\nWall time: 0.0123 seconds\nProcess exited with code 0\n"
                "Original token count: 3\nOutput:\nhello\nsecond line"
            )),
        )
        self.assertEqual(len(blocks), 1)
        tool = blocks[0]
        self.assertEqual((tool["id"], tool["kind"], tool["label"], tool["role"]), ("c:fc1", "tool", "shell", "tool"))
        self.assertEqual(tool["text"], "echo hello → hello")
        self.assertEqual(tool["result"], "hello\nsecond line")
        self.assertNotIn("error", tool)

    def test_a_new_shape_nonzero_exit_marks_the_call_failed(self):
        blocks = self.parse(
            item("function_call", name="exec_command", call_id="call_1", arguments=json.dumps({"cmd": "false"})),
            item("function_call_output", call_id="call_1", output="Exit code: 1\nWall time: 0 seconds\nOutput:\nnope"),
        )
        self.assertTrue(blocks[0]["error"])
        self.assertEqual(blocks[0]["text"], "false ! nope")

    def test_an_old_shape_json_output_marks_a_failed_call(self):
        blocks = self.parse(
            call("call_1", ["bash", "-lc", "cat missing"]),
            item("function_call_output", call_id="call_1", output=json.dumps({
                "output": "cat: missing: No such file\n", "metadata": {"exit_code": 1, "duration_seconds": 0.1},
            })),
        )
        self.assertTrue(blocks[0]["error"])
        self.assertEqual(blocks[0]["result"], "cat: missing: No such file")
        self.assertEqual(blocks[0]["text"], "cat missing ! cat: missing: No such file")

    def test_an_old_shape_json_output_that_succeeded(self):
        blocks = self.parse(
            call("call_1", ["ls"]),
            item("function_call_output", call_id="call_1", output=json.dumps({
                "output": "a\nb", "metadata": {"exit_code": 0},
            })),
        )
        self.assertEqual(blocks[0]["text"], "ls → a")
        self.assertNotIn("error", blocks[0])

    def test_output_that_merely_looks_like_json_is_kept_whole(self):
        blocks = self.parse(
            call("call_1", ["cat", "x.json"]),
            item("function_call_output", call_id="call_1", output='{"name": "x"}'),
        )
        self.assertEqual(blocks[0]["result"], '{"name": "x"}')

    def test_a_patch_names_the_files_it_changes(self):
        patch_text = "*** Begin Patch\n*** Update File: src/a.py\n@@\n-a\n+b\n*** Add File: src/b.py\n+c\n*** End Patch"
        blocks = self.parse(
            item("custom_tool_call", name="apply_patch", call_id="call_1", input=patch_text, id="ctc1"),
            item("custom_tool_call_output", call_id="call_1", output=[
                {"type": "input_text", "text": "Success. Updated the following files:\nM src/a.py"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            ]),
        )
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["label"], "apply_patch")
        self.assertTrue(blocks[0]["text"].startswith("src/a.py, src/b.py → Success."))
        self.assertNotIn("base64", json.dumps(blocks))

    def test_tool_summaries_and_results_are_bounded(self):
        blocks = self.parse(
            item("custom_tool_call", name="exec", call_id="call_1", input="x" * 1000),
            item("custom_tool_call_output", call_id="call_1", output="y" * 5000),
        )
        self.assertLessEqual(len(blocks[0]["text"]), 200)
        self.assertEqual(len(blocks[0]["result"]), 4000)
        self.assertTrue(blocks[0]["result_truncated"])

    def test_an_answer_whose_call_fell_outside_the_tail_is_kept(self):
        blocks = self.parse(item("function_call_output", call_id="gone", output="late\nmore", id="o1"))
        self.assertEqual(
            (blocks[0]["id"], blocks[0]["label"], blocks[0]["text"], blocks[0]["result"]),
            ("c:o1", "Tool result", "late", "late\nmore"),
        )

    def test_a_partly_written_last_line_and_a_cut_first_line_are_skipped(self):
        text = (
            '_text","text":"cut off"}]}}\n'
            + rollout(message("user", "intact", id="u1"))
            + json.dumps(message("assistant", "still being written", id="a1"))[:40]
        )
        self.assertEqual([b["text"] for b in codex.rollout_to_blocks(text)], ["intact"])

    def test_malformed_rows_do_not_break_a_page(self):
        text = "\n".join([
            "not json", "[]", "null", json.dumps({"type": "response_item", "payload": []}),
            json.dumps(item("message", role="user", content=None)),
            json.dumps(item("function_call", name=7, call_id=["x"], arguments="{not json")),
            json.dumps(item("function_call_output", call_id="x", output={"weird": True})),
            json.dumps(item("function_call_output", call_id=["x"], output="unhashable id")),
            json.dumps(message("user", "intact")),
        ])
        blocks = codex.rollout_to_blocks(text)
        self.assertEqual([b["label"] for b in blocks], ["tool", "Tool result", "You"])

    def test_replayed_items_are_read_once(self):
        user = message("user", "once", id="item_1")
        idless = message("assistant", "no id", timestamp="2026-10-23T12:00:01.000Z")
        blocks = self.parse(user, idless, user, idless, message("assistant", "twice", id="item_2"))
        self.assertEqual([b.get("text") or b.get("markdown") for b in blocks], ["once", "no id", "twice"])

    def test_idless_ids_are_unique_and_survive_a_shorter_tail(self):
        rows = [
            message("user", "first", timestamp="2026-10-23T12:00:00.000Z"),
            message("assistant", "second", timestamp="2026-10-23T12:00:01.000Z"),
            call("call_1", ["ls"]),
        ]
        full, tail = self.parse(*rows), self.parse(*rows[1:])
        self.assertEqual(len({b["id"] for b in full}), 3)
        self.assertEqual([b["id"] for b in full[1:]], [b["id"] for b in tail])

    def test_the_newest_blocks_are_kept(self):
        rows = [message("user", f"m{n}", id=f"u{n}") for n in range(5)]
        self.assertEqual([b["text"] for b in codex.rollout_to_blocks(rollout(*rows), limit=2)], ["m3", "m4"])


class RefTests(unittest.TestCase):
    def test_a_codex_ref_is_a_session_uuid(self):
        refs = herdr_relay.transcripts.refs
        self.assertEqual(
            refs.from_pane("codex", {"kind": "id", "value": SESSION}),
            {"agent": "codex", "kind": "id", "value": SESSION},
        )
        for ref in (
            {"kind": "id", "value": os.path.join(os.pardir, SESSION)},
            {"kind": "id", "value": "*"},
            {"kind": "path", "value": "/home/u/.codex/sessions/rollout-x.jsonl"},
            {"kind": "id", "value": SESSION, "agent": "claude"},
        ):
            with self.subTest(ref=ref):
                self.assertIsNone(refs.from_pane("codex", ref))


class LocalStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.root = os.path.join(self.directory, "sessions")
        os.mkdir(self.root)
        self.enterContext(patch.object(herdr_relay.config, "CODEX_SESSIONS", self.root))

    def test_a_dated_rollout_is_read_by_its_session(self):
        path = write(self.root, [message("user", "dated")])
        write(self.root, [message("user", "another session")], session=OTHER)
        self.assertEqual(codex.read_rollout(SESSION), (os.path.realpath(path), rollout(message("user", "dated"))))

    def test_an_older_flat_rollout_is_read(self):
        path = write(self.root, [message("user", "flat")], dated=False)
        self.assertEqual(codex.read_rollout(SESSION)[0], os.path.realpath(path))

    def test_the_newest_of_two_rollouts_for_one_session_wins(self):
        old = write(self.root, [message("user", "old")], dated=False, stamp="2026-10-22T12-00-00")
        new = write(self.root, [message("user", "new")])
        os.utime(old, (1, 1))
        self.assertEqual(codex.read_rollout(SESSION)[0], os.path.realpath(new))

    def test_only_the_tail_is_read(self):
        write(self.root, [message("user", "x" * 100)])
        path, body = codex.read_rollout(SESSION, max_bytes=20)
        self.assertEqual(len(body), 20)

    def test_a_linked_rollout_is_refused(self):
        outside = write(self.directory, [message("user", "outside")], dated=False)
        os.symlink(outside, os.path.join(self.root, os.path.basename(outside)))
        self.assertEqual(codex.read_rollout(SESSION), (None, None))

    def test_a_linked_date_directory_leading_out_of_the_root_is_refused(self):
        elsewhere = os.path.join(self.directory, "elsewhere")
        write(elsewhere, [message("user", "outside")])
        os.symlink(os.path.join(elsewhere, "2026"), os.path.join(self.root, "2026"))
        self.assertEqual(codex.read_rollout(SESSION), (None, None))

    def test_a_missing_session_reads_nothing(self):
        write(self.root, [message("user", "x")], session=OTHER)
        self.assertEqual(codex.read_rollout(SESSION), (None, None))

    def test_the_default_root_follows_codex_home(self):
        with patch.object(herdr_relay.config, "CODEX_SESSIONS", ""), \
                patch.dict(os.environ, {"CODEX_HOME": self.directory}):
            path = write(self.root, [message("user", "home")])
            self.assertEqual(codex.read_rollout(SESSION)[0], os.path.realpath(path))


class RemoteStoreTests(unittest.TestCase):
    """The remote script, run by a local shell in place of ssh."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.root = os.path.join(self.directory, "sessions")
        os.mkdir(self.root)
        self.env = {**os.environ, "HOME": self.directory, "CODEX_HOME": ""}
        self.commands = []
        real_run = subprocess.run

        def run_remote_locally(argv, **_kwargs):
            self.commands.append(argv)
            return real_run(["sh", "-c", argv[-1]], capture_output=True, text=True, timeout=5, env=self.env)

        self.enterContext(patch.object(codex.subprocess, "run", run_remote_locally))

    def read(self, root=None):
        with patch.object(herdr_relay.config, "CODEX_SESSIONS", self.root if root is None else root):
            return codex.read_rollout(SESSION, remote="buildbox")

    def test_a_dated_rollout_is_read_over_ssh(self):
        path = write(self.root, [message("user", "remote")])
        self.assertEqual(self.read(), (os.path.realpath(path), rollout(message("user", "remote"))))
        self.assertEqual(self.commands[0][:5], ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes"])
        self.assertEqual(self.commands[0][5], "buildbox")

    def test_an_older_flat_rollout_is_read_over_ssh(self):
        path = write(self.root, [message("user", "flat")], dated=False)
        self.assertEqual(self.read()[0], os.path.realpath(path))

    def test_an_empty_root_is_the_remote_users_codex_default(self):
        path = write(os.path.join(self.directory, ".codex", "sessions"), [message("user", "default")])
        self.assertEqual(self.read(root="")[0], os.path.realpath(path))
        self.env["CODEX_HOME"] = os.path.join(self.directory, "elsewhere")
        path = write(os.path.join(self.env["CODEX_HOME"], "sessions"), [message("user", "codex home")])
        self.assertEqual(self.read(root="")[0], os.path.realpath(path))

    def test_a_linked_rollout_is_refused_over_ssh(self):
        outside = write(self.directory, [message("user", "outside")], dated=False)
        os.symlink(outside, os.path.join(self.root, os.path.basename(outside)))
        self.assertEqual(self.read(), (None, None))

    def test_a_linked_date_directory_is_refused_over_ssh(self):
        elsewhere = os.path.join(self.directory, "elsewhere")
        write(elsewhere, [message("user", "outside")])
        os.symlink(os.path.join(elsewhere, "2026"), os.path.join(self.root, "2026"))
        self.assertEqual(self.read(), (None, None))

    def test_a_missing_session_reads_nothing_over_ssh(self):
        self.assertEqual(self.read(), (None, None))


class PaneBlocksTests(unittest.TestCase):
    def pane_blocks(self, ref, cwd="/work/repo", ambiguous=False):
        key = ("local", "pane-cx")
        refs = {} if ref is None else {key: ref}
        with tempfile.TemporaryDirectory() as root, \
                patch.object(herdr_relay.config, "CODEX_SESSIONS", root), \
                patch.object(herdr_relay.state, "known_pane_keys", {key}), \
                patch.dict(herdr_relay.state.pane_hosts, {"pane-cx": {"local"}}, clear=True), \
                patch.dict(herdr_relay.state.pane_cwd_map, {key: (cwd, "codex", None, ambiguous)}, clear=True), \
                patch.dict(herdr_relay.state.pane_session_refs, refs, clear=True):
            write(root, [message("user", "run tests", id="u1"), message("assistant", "done", id="a1")])
            return herdr_relay.transcripts.blocks.pane_blocks("pane-cx")

    def test_a_codex_pane_with_its_session_ref_streams_blocks(self):
        blocks, signature = self.pane_blocks({"agent": "codex", "kind": "id", "value": SESSION}, ambiguous=True)
        self.assertEqual([b["id"] for b in blocks], ["c:u1", "c:a1"])
        self.assertIsNotNone(signature)

    def test_a_codex_pane_without_a_ref_streams_nothing(self):
        self.assertEqual(self.pane_blocks(None), (None, None))

    def test_a_ref_to_another_session_streams_nothing(self):
        self.assertEqual(self.pane_blocks({"agent": "codex", "kind": "id", "value": OTHER}), (None, None))


if __name__ == "__main__":
    unittest.main()
