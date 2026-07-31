import asyncio
import contextlib
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from relay import herdr_relay


class RelayLifecycleTests(unittest.TestCase):
    def test_broadcast_tolerates_disconnect_during_send(self):
        class Socket:
            async def send(self, _message):
                herdr_relay.clients.discard(self)

        async def run():
            sockets = {Socket(), Socket()}
            herdr_relay.clients.update(sockets)
            try:
                await herdr_relay.broadcast({"type": "agents", "agents": []})
            finally:
                herdr_relay.clients.clear()

        asyncio.run(run())

    def test_background_failure_reaches_main_waiter(self):
        async def run():
            stop = asyncio.get_running_loop().create_future()

            async def fail():
                raise RuntimeError("poll failed")

            task = asyncio.create_task(fail(), name="poll-loop")
            task.add_done_callback(
                lambda completed: herdr_relay.fail_on_background_exit(completed, stop)
            )
            with self.assertRaisesRegex(RuntimeError, "poll failed"):
                await stop

        asyncio.run(run())


class HostStatusTests(unittest.TestCase):
    @patch.object(herdr_relay, "HOST_TARGETS", {"mba13": "mba", "mz": "mz"})
    @patch.object(herdr_relay, "run_herdr_checked")
    def test_host_status_reflects_poll_success(self, run_herdr_checked):
        empty_result = json.dumps({"result": {"panes": []}})
        run_herdr_checked.side_effect = [
            (False, ""),
            (True, empty_result),
        ]

        agents, hosts = asyncio.run(herdr_relay.get_all_agents())

        self.assertEqual(agents, [])
        self.assertEqual(
            hosts,
            [
                {"host_id": "mba13", "online": False},
                {"host_id": "mz", "online": True},
            ],
        )

    @patch.object(herdr_relay, "HOST_TARGETS", {"host-a": "a", "host-b": "b"})
    @patch.object(herdr_relay, "get_agents_from_host")
    def test_hosts_are_polled_concurrently(self, get_agents_from_host):
        barrier = threading.Barrier(2, timeout=1)

        def poll_host(*, remote, host_id):
            barrier.wait()
            return ([{"pane_id": remote}], True)

        get_agents_from_host.side_effect = poll_host

        agents, hosts = asyncio.run(herdr_relay.get_all_agents())

        self.assertEqual(agents, [{"pane_id": "a"}, {"pane_id": "b"}])
        self.assertEqual(
            hosts,
            [
                {"host_id": "host-a", "online": True},
                {"host_id": "host-b", "online": True},
            ],
        )

    @patch.object(herdr_relay.subprocess, "run")
    def test_remote_poll_uses_keepalives(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = "ok\n"

        self.assertEqual(
            herdr_relay.run_herdr_checked("pane", "list", remote="workstation"),
            (True, "ok"),
        )
        run.assert_called_once_with(
            [
                "ssh",
                "-o", "ConnectTimeout=5",
                "-o", "ServerAliveInterval=3",
                "-o", "ServerAliveCountMax=2",
                "-o", "BatchMode=yes",
                "workstation", herdr_relay.HERDR, "pane", "list",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )

    @patch.object(herdr_relay.subprocess, "run")
    def test_remote_poll_reports_failures(self, run):
        run.side_effect = subprocess.TimeoutExpired("ssh", 15)

        with patch("builtins.print") as print_message:
            result = herdr_relay.run_herdr_checked(
                "pane", "list", remote="workstation"
            )

        self.assertEqual(result, (False, ""))
        print_message.assert_called_once()
        self.assertIn("workstation", print_message.call_args.args[0])


class EventLoopBlockingTests(unittest.TestCase):
    def test_command_handler_keeps_event_loop_running(self):
        class Socket:
            def __init__(self):
                self.requests = iter([
                    json.dumps({
                        "type": "send_text",
                        "pane_id": "pane-1",
                        "text": "hello",
                    })
                ])

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self.requests)
                except StopIteration:
                    raise StopAsyncIteration

            async def send(self, _message):
                pass

        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_command(*_args, **_kwargs):
            started.set()
            release.wait(timeout=1)
            finished.set()
            return ""

        async def run():
            made_progress = asyncio.Event()
            handler = asyncio.create_task(herdr_relay.handle_client(Socket()))

            async def observe_event_loop():
                await asyncio.to_thread(started.wait, 1)
                if not finished.is_set():
                    made_progress.set()
                release.set()

            observer = asyncio.create_task(observe_event_loop())
            try:
                await asyncio.wait_for(asyncio.gather(handler, observer), timeout=2)
            finally:
                release.set()
            self.assertTrue(made_progress.is_set())

        with (
            patch.object(herdr_relay, "known_panes", {"pane-1"}),
            patch.object(herdr_relay, "pane_remote_map", {}),
            patch.object(herdr_relay, "run_herdr", side_effect=blocking_command),
        ):
            asyncio.run(run())


class PollLoopBlockingTests(unittest.TestCase):
    def test_blocked_pane_read_keeps_event_loop_running(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_read(pane_id, remote=None):
            started.set()
            release.wait(timeout=1)
            finished.set()
            return "Do you want to proceed?"

        async def run():
            made_progress = asyncio.Event()
            poll = asyncio.create_task(herdr_relay._poll_once())

            async def observe_event_loop():
                await asyncio.to_thread(started.wait, 1)
                if not finished.is_set():
                    made_progress.set()
                release.set()

            observer = asyncio.create_task(observe_event_loop())
            try:
                await asyncio.wait_for(asyncio.gather(poll, observer), timeout=3)
            finally:
                release.set()
            self.assertTrue(made_progress.is_set())

        agents = [{
            "pane_id": "pane-1", "agent": "claude", "label": "", "project": "repo",
            "status": "blocked", "cwd": "/work/repo", "host": "local", "remote": None,
        }]

        async def fake_get_all_agents():
            return agents, []

        with (
            patch.object(herdr_relay, "get_all_agents", fake_get_all_agents),
            patch.object(herdr_relay, "read_pane", blocking_read),
            patch.object(herdr_relay, "broadcast", AsyncMock()),
            patch.object(herdr_relay, "send_web_push", AsyncMock()),
            patch.dict(herdr_relay.last_statuses, {}, clear=True),
            patch.dict(herdr_relay.subscriptions, {}, clear=True),
        ):
            asyncio.run(run())


class PaneMetadataTests(unittest.TestCase):
    def test_attention_state_mapping(self):
        cases = (
            ("idle", None, "idle"),
            ("working", None, "working"),
            ("blocked", None, "waiting"),
            ("done", None, "done"),
            ("unknown", None, None),
        )

        for status, previous_status, expected in cases:
            with self.subTest(status=status):
                self.assertEqual(
                    herdr_relay.attention_state(status, previous_status), expected
                )

        self.assertEqual(herdr_relay.attention_state("idle", "working"), "done")
        self.assertEqual(herdr_relay.attention_state("idle", "idle"), "idle")
        self.assertEqual(herdr_relay.attention_state("idle", "idle", "done"), "done")

    def test_done_survives_while_the_pane_stays_idle(self):
        snapshots = [
            [self.agent(status="working")],
            [self.agent(status="idle")],
            [self.agent(status="idle")],
            [self.agent(status="working")],
        ]
        sent = []

        async def get_all_agents():
            return snapshots.pop(0), []

        async def broadcast(frame):
            sent.append(frame)

        with self.poll_state(), patch.object(
            herdr_relay, "get_all_agents", side_effect=get_all_agents
        ), patch.object(herdr_relay, "broadcast", side_effect=broadcast), patch.object(
            herdr_relay, "now_ms", return_value=1000
        ):
            for _ in range(4):
                asyncio.run(herdr_relay._poll_once())

        # The second idle poll must not fall back to "idle": a client that
        # connects then would be told the finished turn never happened.
        self.assertEqual(
            [frame["agents"][0]["attention_state"] for frame in sent],
            ["working", "done", "done", "working"],
        )

    def test_unknown_status_omits_attention_state(self):
        for status in ("unknown", "unexpected"):
            with self.subTest(status=status):
                agents = [self.agent(status=status)]
                sent = []

                async def broadcast(frame):
                    sent.append(frame)

                with self.poll_state(), patch.object(
                    herdr_relay, "get_all_agents", return_value=(agents, [])
                ), patch.object(herdr_relay, "broadcast", side_effect=broadcast):
                    asyncio.run(herdr_relay._poll_once())

                self.assertNotIn("attention_state", sent[0]["agents"][0])

    def test_updated_at_changes_only_with_status_or_revision(self):
        snapshots = [
            [self.agent(status="working", revision=1)],
            [self.agent(status="working", revision=1)],
            [self.agent(status="blocked", revision=1)],
            [self.agent(status="blocked", revision=2)],
        ]
        sent = []

        async def get_all_agents():
            return snapshots.pop(0), []

        async def broadcast(frame):
            sent.append(frame)

        with self.poll_state(), patch.object(
            herdr_relay, "get_all_agents", side_effect=get_all_agents
        ), patch.object(herdr_relay, "broadcast", side_effect=broadcast), patch.object(
            herdr_relay, "now_ms", side_effect=[1000, 2000, 3000]
        ):
            for _ in range(4):
                asyncio.run(herdr_relay._poll_once())

        entries = [frame["agents"][0] for frame in sent if frame["type"] == "agents"]
        self.assertEqual([entry["updated_at"] for entry in entries], [1000, 1000, 2000, 3000])

    def test_missing_or_bool_revision_is_omitted(self):
        pane_list = json.dumps({"result": {"panes": [
            {"pane_id": "missing", "agent": "claude"},
            {"pane_id": "boolean", "agent": "claude", "revision": True},
        ]}})
        with patch.object(herdr_relay, "run_herdr_checked", return_value=(True, pane_list)):
            agents, _online = herdr_relay.get_agents_from_host()

        for agent in agents:
            with self.subTest(pane_id=agent["pane_id"]):
                self.assertNotIn("output_revision", agent)

    def test_vanished_panes_are_pruned_from_metadata(self):
        snapshots = [[self.agent()], []]

        async def get_all_agents():
            return snapshots.pop(0), []

        async def broadcast(_frame):
            pass

        with self.poll_state(), patch.object(
            herdr_relay, "get_all_agents", side_effect=get_all_agents
        ), patch.object(herdr_relay, "broadcast", side_effect=broadcast), patch.object(
            herdr_relay, "now_ms", return_value=1000
        ):
            asyncio.run(herdr_relay._poll_once())
            asyncio.run(herdr_relay._poll_once())
            self.assertEqual(herdr_relay.pane_activity, {})
            self.assertEqual(herdr_relay.pane_revisions, {})
            self.assertEqual(herdr_relay.pane_attention_states, {})

    @staticmethod
    def agent(status="working", revision=0):
        return {
            "pane_id": "pane-1", "agent": "claude", "label": "", "project": "repo",
            "status": status, "cwd": "/work/repo", "host": "local", "remote": None,
            "output_revision": revision,
        }

    @staticmethod
    def poll_state():
        stack = contextlib.ExitStack()
        stack.enter_context(patch.dict(herdr_relay.last_statuses, {}, clear=True))
        stack.enter_context(patch.dict(herdr_relay.pane_activity, {}, clear=True))
        stack.enter_context(patch.dict(herdr_relay.pane_revisions, {}, clear=True))
        stack.enter_context(patch.dict(herdr_relay.pane_attention_states, {}, clear=True))
        stack.enter_context(patch.dict(herdr_relay.subscriptions, {}, clear=True))
        return stack


class HostPowerTests(unittest.TestCase):
    @patch.object(herdr_relay, "POWER_HOST_ID", "mz")
    @patch.object(herdr_relay, "POWER_HOST_MAC", "34:5a:60:ba:8e:20")
    @patch.object(herdr_relay.subprocess, "run")
    def test_wake_is_a_fixed_magic_packet_command(self, run):
        run.return_value.returncode = 0

        response = herdr_relay.wake_host({"request_id": "request-1", "host_id": "mz"})

        self.assertEqual(response["type"], "command_ack")
        run.assert_called_once_with(
            [herdr_relay.WAKE_BIN, "34:5a:60:ba:8e:20"],
            capture_output=True,
            text=True,
            timeout=10,
        )

    @patch.object(herdr_relay, "POWER_HOST_ID", "mz")
    @patch.object(herdr_relay, "HOST_TARGETS", {"mz": "mz"})
    @patch.object(herdr_relay.subprocess, "run")
    def test_shutdown_is_a_fixed_non_interactive_ssh_command(self, run):
        run.return_value.returncode = 0

        response = herdr_relay.shutdown_host({
            "request_id": "request-2",
            "host_id": "mz",
            "confirmation_nonce": "nonce-1",
        })

        self.assertEqual(response["type"], "command_ack")
        run.assert_called_once_with(
            [
                "ssh",
                "-o", "ConnectTimeout=5",
                "-o", "ServerAliveInterval=3",
                "-o", "ServerAliveCountMax=2",
                "-o", "BatchMode=yes",
                "mz",
                "sudo", "-n", "systemctl", "poweroff",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )

    @patch.object(herdr_relay, "POWER_HOST_ID", "mz")
    @patch.object(herdr_relay.subprocess, "run")
    def test_power_commands_reject_other_hosts_and_missing_confirmation(self, run):
        wake = herdr_relay.wake_host({"request_id": "request-1", "host_id": "other"})
        shutdown = herdr_relay.shutdown_host({"request_id": "request-2", "host_id": "mz"})

        self.assertEqual(wake["code"], "HOST_NOT_ALLOWED")
        self.assertEqual(shutdown["code"], "CONFIRMATION_REQUIRED")
        run.assert_not_called()


class PaneChromeTests(unittest.TestCase):
    @patch.object(herdr_relay, "run_herdr")
    def test_read_pane_filters_heavy_opencode_chrome(self, run_herdr):
        run_herdr.return_value = "\n".join(
            [
                "┃ Permission required: access external directory ┃",
                "╹▀▀▀▀▀▀▀▀",
                "⬝⬝⬝⬝ esc interrupt",
            ]
        )

        self.assertEqual(
            herdr_relay.read_pane("pane-1"),
            "┃ Permission required: access external directory ┃",
        )

    def test_meaningful_status_with_footer_is_not_all_chrome(self):
        line = "┃ ┃ Build · GPT-5.6 Sol OpenAI ~/src:main ╹▀▀ ⬝⬝ esc interrupt"

        self.assertIsNone(herdr_relay.CHROME_RE.search(line))


class StructuredOutputTests(unittest.TestCase):
    def test_read_pane_returns_dialog_only_while_pane_is_blocked(self):
        class Socket:
            def __init__(self):
                self.requests = iter([
                    json.dumps({"type": "read_pane", "pane_id": "pane-1", "lines": 30})
                ])
                self.sent = []

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self.requests)
                except StopIteration:
                    raise StopAsyncIteration

            async def send(self, message):
                self.sent.append(json.loads(message))

        prompt = "△Permission required\n#Pushes mobile rendering fix to origin\n$ git push origin main"
        for status, expected_types in (
            ("blocked", ["pane_content", "blocked"]),
            ("idle", ["pane_content"]),
        ):
            socket = Socket()
            with (
                patch.dict(herdr_relay.last_statuses, {"pane-1": status}, clear=True),
                patch.object(herdr_relay, "known_panes", {"pane-1"}),
                patch.dict(herdr_relay.pane_response_options, {}, clear=True),
                patch.object(herdr_relay, "run_herdr", return_value=prompt),
                patch.object(herdr_relay, "pane_blocks", return_value=(None, None)),
            ):
                asyncio.run(herdr_relay.handle_client(socket))

            self.assertEqual(
                [message["type"] for message in socket.sent],
                expected_types,
            )
            if status == "blocked":
                self.assertEqual(socket.sent[1]["prompt"], prompt)
                self.assertEqual(socket.sent[1]["options"], herdr_relay.OPENCODE_OPTIONS)

    def test_claude_project_dir(self):
        self.assertEqual(
            herdr_relay.claude_project_dir("/Users/me/src/herdr-mobile"),
            "-Users-me-src-herdr-mobile",
        )
        self.assertEqual(
            herdr_relay.claude_project_dir("/home/me/my_app.v2"),
            "-home-me-my-app-v2",
        )

    def test_summarize_tool(self):
        self.assertEqual(
            herdr_relay.summarize_tool({"file_path": "/etc/hosts", "content": "x"}),
            "/etc/hosts",
        )
        self.assertEqual(
            herdr_relay.summarize_tool({"command": "make build\nmake test"}),
            "make build",
        )
        self.assertEqual(herdr_relay.summarize_tool(None), "")

    def test_claude_transcript_mapping(self):
        fixture = "\n".join(
            json.dumps(record)
            for record in [
                {
                    "type": "user",
                    "message": {"role": "user", "content": "Fix the login bug"},
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "thinking",
                                "thinking": "Inspect auth\nthen patch",
                            },
                            {"type": "text", "text": "I'll inspect it."},
                            {
                                "type": "tool_use",
                                "name": "Read",
                                "input": {"file_path": "auth.py"},
                            },
                        ],
                    },
                },
            ]
        )

        blocks = herdr_relay.transcript_to_blocks(fixture)

        self.assertEqual(
            [(block["kind"], block.get("label")) for block in blocks],
            [
                ("status", "You"),
                ("status", "Thought"),
                ("assistant_text", None),
                ("tool", "Read"),
            ],
        )
        self.assertEqual(blocks[1]["text"], "Inspect auth")
        self.assertEqual(blocks[2]["markdown"], "I'll inspect it.")
        self.assertEqual(blocks[3]["text"], "auth.py")

    def test_claude_transcript_tolerates_partial_tail(self):
        fixture = "partial-json\n" + json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "ok"}],
                },
            }
        )
        self.assertEqual(
            [block["kind"] for block in herdr_relay.transcript_to_blocks(fixture)],
            ["assistant_text"],
        )

    def test_claude_transcript_keeps_tail_limit(self):
        fixture = "\n".join(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": str(index)}],
                    },
                }
            )
            for index in range(250)
        )
        blocks = herdr_relay.transcript_to_blocks(fixture, limit=10)
        self.assertEqual(
            [block["markdown"] for block in blocks],
            [str(index) for index in range(240, 250)],
        )

    def test_ambiguous_claude_panes_stream_their_own_path_refs(self):
        def transcript(text):
            return json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": text}]},
            })

        with tempfile.TemporaryDirectory() as directory:
            first_path = os.path.join(directory, "first.jsonl")
            second_path = os.path.join(directory, "second.jsonl")
            with open(first_path, "w") as first:
                first.write(transcript("first conversation"))
            with open(second_path, "w") as second:
                second.write(transcript("second conversation"))
            pane_list = json.dumps({"result": {"panes": [
                {
                    "pane_id": "first", "agent": "claude", "cwd": "/work/repo",
                    "agent_session": {"kind": "path", "value": first_path},
                },
                {
                    "pane_id": "second", "agent": "claude", "cwd": "/work/repo",
                    "agent_session": {"kind": "path", "value": second_path},
                },
            ]}})
            with (
                patch.dict(herdr_relay.pane_session_refs, {}, clear=True),
                patch.dict(
                    herdr_relay.pane_cwd_map,
                    {
                        "first": ("/work/repo", "claude", None, True),
                        "second": ("/work/repo", "claude", None, True),
                    },
                    clear=True,
                ),
                patch.object(herdr_relay, "run_herdr_checked", return_value=(True, pane_list)),
            ):
                agents, _online = herdr_relay.get_agents_from_host()
                first_blocks, _first_signature = herdr_relay.pane_blocks("first")
                second_blocks, _second_signature = herdr_relay.pane_blocks("second")

            self.assertNotIn("agent_session", agents[0])
            self.assertEqual(first_blocks[0]["markdown"], "first conversation")
            self.assertEqual(second_blocks[0]["markdown"], "second conversation")
            self.assertNotEqual(first_blocks, second_blocks)

    def test_ambiguous_cwd_without_refs_is_not_streamed(self):
        with (
            patch.dict(
                herdr_relay.pane_cwd_map,
                {"ambiguous": ("/work/repo", "claude", None, True)},
                clear=True,
            ),
            patch.dict(herdr_relay.pane_session_refs, {}, clear=True),
        ):
            self.assertEqual(herdr_relay.pane_blocks("ambiguous"), (None, None))

    def test_claude_id_ref_uses_project_transcript_path(self):
        cwd = "/work/repo"
        session_id = "session-123"
        with tempfile.TemporaryDirectory() as projects:
            project = os.path.join(projects, herdr_relay.claude_project_dir(cwd))
            os.mkdir(project)
            with open(os.path.join(project, session_id + ".jsonl"), "w") as transcript:
                transcript.write(json.dumps({
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "id conversation"}]},
                }))
            with (
                patch.object(herdr_relay, "CLAUDE_PROJECTS", projects),
                patch.dict(
                    herdr_relay.pane_cwd_map,
                    {"pane-id": (cwd, "claude", None, True)},
                    clear=True,
                ),
                patch.dict(
                    herdr_relay.pane_session_refs,
                    {(None, "pane-id"): {"kind": "id", "value": session_id}},
                    clear=True,
                ),
            ):
                blocks, _signature = herdr_relay.pane_blocks("pane-id")

        self.assertEqual(blocks[0]["markdown"], "id conversation")

    def test_malformed_agent_session_refs_are_ignored(self):
        pane_list = json.dumps({"result": {"panes": [
            {
                "pane_id": "bad-kind", "agent": "claude", "cwd": "/work/repo",
                "agent_session": {"kind": "session", "value": "session-1"},
            },
            {
                "pane_id": "empty", "agent": "claude", "cwd": "/work/repo",
                "agent_session": {"kind": "id", "value": ""},
            },
            {
                "pane_id": "null", "agent": "claude", "cwd": "/work/repo",
                "agent_session": None,
            },
        ]}})
        with (
            patch.dict(herdr_relay.pane_session_refs, {}, clear=True),
            patch.dict(
                herdr_relay.pane_cwd_map,
                {pane_id: ("/work/repo", "claude", None, True)
                 for pane_id in ("bad-kind", "empty", "null")},
                clear=True,
            ),
            patch.object(herdr_relay, "run_herdr_checked", return_value=(True, pane_list)),
        ):
            herdr_relay.get_agents_from_host()
            self.assertEqual(herdr_relay.pane_session_refs, {})
            for pane_id in ("bad-kind", "empty", "null"):
                self.assertEqual(herdr_relay.pane_blocks(pane_id), (None, None))

    def test_opencode_id_ref_selects_session_instead_of_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "opencode.db")
            db = sqlite3.connect(db_path)
            try:
                db.executescript("""
                    CREATE TABLE session (id TEXT, directory TEXT, parent_id TEXT, time_updated INTEGER);
                    CREATE TABLE message (id TEXT, session_id TEXT, data TEXT, time_created INTEGER);
                    CREATE TABLE part (id TEXT, message_id TEXT, data TEXT, time_created INTEGER);
                """)
                db.execute(
                    "INSERT INTO session VALUES (?, ?, ?, ?)",
                    ("target-session", "/another/repo", None, 1),
                )
                db.execute(
                    "INSERT INTO message VALUES (?, ?, ?, ?)",
                    ("message-1", "target-session", '{"role": "assistant"}', 1),
                )
                db.execute(
                    "INSERT INTO part VALUES (?, ?, ?, ?)",
                    ("part-1", "message-1", '{"type": "text", "text": "target session"}', 1),
                )
                db.commit()
            finally:
                db.close()
            with (
                patch.object(herdr_relay, "OPENCODE_DB", db_path),
                patch.dict(
                    herdr_relay.pane_cwd_map,
                    {"opencode": ("/wrong/repo", "opencode", None, True)},
                    clear=True,
                ),
                patch.dict(
                    herdr_relay.pane_session_refs,
                    {(None, "opencode"): {"kind": "id", "value": "target-session"}},
                    clear=True,
                ),
            ):
                blocks, _signature = herdr_relay.pane_blocks("opencode")

        self.assertEqual(blocks[0]["markdown"], "target session")

    def test_opencode_mapping(self):
        document = {
            "session_id": "ses_test",
            "updated": 1,
            "rows": [
                ["user", json.dumps({"type": "text", "text": "Fix the login bug"})],
                [
                    "assistant",
                    json.dumps(
                        {"type": "reasoning", "text": "Inspect auth\nthen patch"}
                    ),
                ],
                ["assistant", json.dumps({"type": "text", "text": "I'll inspect it."})],
                [
                    "assistant",
                    json.dumps(
                        {
                            "type": "tool",
                            "tool": "read",
                            "state": {
                                "status": "completed",
                                "input": {"filePath": "auth.py"},
                            },
                        }
                    ),
                ],
                ["assistant", "not-json"],
            ],
        }

        blocks = herdr_relay.opencode_to_blocks(document)

        self.assertEqual(
            [(block["kind"], block.get("label")) for block in blocks],
            [
                ("status", "You"),
                ("status", "Thought"),
                ("assistant_text", None),
                ("tool", "read"),
            ],
        )
        self.assertEqual(blocks[1]["text"], "Inspect auth")
        self.assertEqual(blocks[2]["markdown"], "I'll inspect it.")
        self.assertEqual(blocks[3]["text"], "auth.py")

    def test_opencode_mapping_preserves_multiline_markdown(self):
        markdown = (
            "# Todos\n"
            "[•] Identify degraded state\n"
            "[ ] Correlate agent logs\n\n"
            "$ kubectl get nodes\n"
            "NODE STATE\n"
            "km1 Degraded"
        )
        document = {
            "rows": [
                ["assistant", json.dumps({"type": "text", "text": markdown})],
            ],
        }

        blocks = herdr_relay.opencode_to_blocks(document)

        self.assertEqual(blocks[0]["markdown"], markdown)


class DetectOptionsTests(unittest.TestCase):
    def test_legacy_tool_permission(self):
        text = (
            "Do you want to allow this tool call?\n\n"
            "> yes, single permission\n"
            "> trust, always allow\n"
            "> no (tab to edit)"
        )
        self.assertEqual(herdr_relay.detect_options(text), herdr_relay.TOOL_OPTIONS)

    def test_subagent_options(self):
        text = "approve all pending\nconfigure individually"
        self.assertEqual(herdr_relay.detect_options(text), herdr_relay.SUBAGENT_OPTIONS)

    def test_claude_numbered_yes_no(self):
        text = (
            "Ask rule Bash(git add *) overrides auto mode for this command.\n"
            " /permissions to let auto mode decide\n\n"
            " Do you want to proceed?\n"
            " ❯ 1. Yes\n"
            "   2. No\n"
        )
        self.assertEqual(herdr_relay.detect_options(text), ["1. Yes", "2. No"])

    def test_claude_proceed_fallback_without_numbers(self):
        text = "Do you want to proceed?\nSome other chrome"
        self.assertEqual(herdr_relay.detect_options(text), ["1. Yes", "2. No"])

    def test_claude_ask_rule_fallback(self):
        text = "Ask rule Bash(git add *) overrides auto mode for this command."
        self.assertEqual(herdr_relay.detect_options(text), ["1. Yes", "2. No"])

    def test_opencode_permission_required(self):
        text = (
            "△ Permission required\n"
            "  Bash · git status\n"
            "  Allow once   Allow always   Reject\n"
            "  ↔ select   enter confirm   esc dismiss\n"
        )
        self.assertEqual(herdr_relay.detect_options(text), herdr_relay.OPENCODE_OPTIONS)

    def test_opencode_allow_once_phrase(self):
        text = "Allow once\nAllow always\nReject\nPermission required"
        self.assertEqual(herdr_relay.detect_options(text), herdr_relay.OPENCODE_OPTIONS)

    def test_yn_style(self):
        self.assertEqual(herdr_relay.detect_options("Continue? [y/n]"), ["y", "n"])
        self.assertEqual(herdr_relay.detect_options("write to this file?\nproceed (y)"), ["y", "n"])

    def test_respond_text_numbered_label(self):
        self.assertEqual(herdr_relay.respond_text("1. Yes"), "1")
        self.assertEqual(herdr_relay.respond_text("2. No"), "2")
        self.assertEqual(
            herdr_relay.respond_text("yes, single permission"),
            "yes, single permission",
        )

    def test_respond_action_opencode_keys(self):
        self.assertEqual(herdr_relay.respond_action("Allow once"), ("keys", ["Enter"]))
        self.assertEqual(
            herdr_relay.respond_action("Allow always"),
            ("keys", ["Right", "Enter", "Enter"]),
        )
        self.assertEqual(herdr_relay.respond_action("Reject"), ("keys", ["Escape"]))
        self.assertEqual(herdr_relay.respond_action("1. Yes"), ("text", "1"))
        self.assertEqual(herdr_relay.respond_action("y"), ("text", "y"))
        # Free-text deny must not be remapped to Escape keys
        self.assertEqual(herdr_relay.respond_action("deny"), ("text", "deny"))

    def test_unknown_prompt_returns_none(self):
        self.assertIsNone(herdr_relay.detect_options("just some log output"))


class RelayInputValidationTests(unittest.TestCase):
    def test_key_allowlist_covers_the_web_keyboard(self):
        for key in ("Enter", "Space", "1", "Ctrl+c", "ctrl+d", "shift+1"):
            with self.subTest(key=key):
                self.assertTrue(herdr_relay.is_safe_key(key))

        for key in ("--help", "ctrl+;", "arbitrary"):
            with self.subTest(key=key):
                self.assertFalse(herdr_relay.is_safe_key(key))

    def test_read_pane_line_count_is_coerced_to_a_sane_int(self):
        coerce = herdr_relay._read_pane_lines
        self.assertEqual(30, coerce(None))
        self.assertEqual(30, coerce("abc"))
        self.assertEqual(30, coerce(""))
        self.assertEqual(30, coerce({"lines": 5}))
        self.assertEqual(30, coerce(0))
        self.assertEqual(30, coerce(-5))
        self.assertEqual(50, coerce(50))
        self.assertEqual(50, coerce(" 50 "))
        self.assertEqual(2000, coerce(10 ** 9))

    @patch.object(herdr_relay, "run_herdr", return_value="")
    def test_read_pane_never_forwards_a_bad_line_count_to_herdr(self, run_herdr):
        class Socket:
            def __init__(self):
                self.requests = iter([
                    json.dumps({"type": "read_pane", "pane_id": "pane-1", "lines": "abc"})
                ])
                self.sent = []

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self.requests)
                except StopIteration:
                    raise StopAsyncIteration

            async def send(self, message):
                self.sent.append(json.loads(message))

        socket = Socket()
        with (
            patch.object(herdr_relay, "known_panes", {"pane-1"}),
            patch.object(herdr_relay, "pane_blocks", return_value=(None, None)),
        ):
            asyncio.run(herdr_relay.handle_client(socket))

        # "abc" would make herdr print an error on stdout and exit 0, which the
        # relay would then serve to the client as terminal content.
        run_herdr.assert_called_once_with(
            "pane", "read", "pane-1", "--lines", "30", "--source", "recent", remote=None
        )
        self.assertEqual(["pane_content"], [frame["type"] for frame in socket.sent])

    @patch.object(herdr_relay, "run_herdr")
    def test_detected_dynamic_response_uses_its_safe_key_mapping(self, run_herdr):
        class Socket:
            def __init__(self):
                self.requests = iter([
                    json.dumps({
                        "type": "respond",
                        "pane_id": "pane-1",
                        "text": "Allow once",
                    })
                ])
                self.sent = []

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self.requests)
                except StopIteration:
                    raise StopAsyncIteration

            async def send(self, message):
                self.sent.append(json.loads(message))

        with (
            patch.object(herdr_relay, "known_panes", {"pane-1"}),
            patch.dict(
                herdr_relay.pane_response_options,
                {"pane-1": {"allow once", "allow always", "reject"}},
                clear=True,
            ),
        ):
            socket = Socket()
            asyncio.run(herdr_relay.handle_client(socket))

        self.assertEqual(socket.sent, [])
        run_herdr.assert_called_once_with(
            "pane", "send-keys", "pane-1", "Enter", remote=None
        )


class AuthTokenTests(unittest.TestCase):
    def test_require_auth_token_rejects_empty_token(self):
        with patch.object(herdr_relay, "AUTH_TOKEN", ""):
            with self.assertRaises(SystemExit):
                herdr_relay.require_auth_token()

    def test_require_auth_token_accepts_configured_token(self):
        with patch.object(herdr_relay, "AUTH_TOKEN", "token"):
            self.assertIsNone(herdr_relay.require_auth_token())

    def test_process_request_rejects_wrong_token(self):
        class Headers:
            def raw_items(self):
                return [("Authorization", "Bearer wrong")]

        class Request:
            headers = Headers()
            path = "/"

        with patch.object(herdr_relay, "AUTH_TOKEN", "token"):
            response = asyncio.run(herdr_relay.process_request(None, Request()))

        self.assertEqual(response.status_code, 401)

    def test_process_request_rejects_non_ascii_token(self):
        class Headers:
            def raw_items(self):
                return [("Authorization", "Bearer пароль")]

        class Request:
            headers = Headers()
            path = "/"

        with patch.object(herdr_relay, "AUTH_TOKEN", "token"):
            response = asyncio.run(herdr_relay.process_request(None, Request()))

        self.assertEqual(response.status_code, 401)

    def test_process_request_accepts_correct_websocket_token(self):
        class Headers:
            def raw_items(self):
                return [
                    ("Authorization", "Bearer token"),
                    ("Upgrade", "websocket"),
                ]

        class Request:
            headers = Headers()
            path = "/"

        with patch.object(herdr_relay, "AUTH_TOKEN", "token"):
            response = asyncio.run(herdr_relay.process_request(None, Request()))

        self.assertIsNone(response)


if __name__ == "__main__":
    unittest.main()
