import asyncio
import contextlib
import json
import os
import shlex
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from unittest.mock import AsyncMock, patch

from relay import herdr_relay


def after_handshake(frames):
    """Drop the `server_info` frame that every connection now opens with.

    Explicit rather than shifted indices: these tests are about what a request
    produces, and the handshake has its own ordering test and its own golden. The
    assertion means a real frame going missing here still fails loudly.
    """
    assert frames and frames[0]["type"] == "server_info", frames
    return frames[1:]


class RelayLifecycleTests(unittest.TestCase):
    def test_broadcast_tolerates_disconnect_during_send(self):
        class Socket:
            async def send(self, _message):
                herdr_relay.state.clients.discard(self)

        async def run():
            sockets = {Socket(), Socket()}
            herdr_relay.state.clients.update(sockets)
            try:
                await herdr_relay.broadcast({"type": "agents", "agents": []})
            finally:
                herdr_relay.state.clients.clear()

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


def host_record(host_id, target=None, **overrides):
    """A minimal record in the shape `hosts.load_hosts()` produces.

    Tests that only care about topology should not have to spell out a whole
    validated host document; anything they do care about goes in `overrides`.
    """
    record = {
        "id": host_id,
        "display_name": host_id,
        "ssh": {"target": target} if target else {},
        "project_roots": ["/"],
        "herdr": {},
        "harnesses": [],
        "power": {"wake": None, "shutdown": False},
        "readiness_timeout_seconds": 180,
    }
    record.update(overrides)
    return record


def configured_hosts(*records):
    """Patch both host lookups at once — they must never disagree."""
    return (
        patch.object(herdr_relay.hosts, "HOSTS", list(records)),
        patch.object(herdr_relay.hosts, "HOSTS_BY_ID", {record["id"]: record for record in records}),
    )


class HostStatusTests(unittest.TestCase):
    @patch.object(
        herdr_relay.hosts, "HOSTS",
        [host_record("workstation-a", "target-a"), host_record("workstation-b", "target-b")],
    )
    @patch.object(herdr_relay.herdr, "run_ssh_checked", return_value=True)
    @patch.object(herdr_relay.herdr, "run_herdr_checked")
    def test_host_status_reflects_ssh_and_herdr_readiness(self, run_herdr_checked, run_ssh_checked):
        empty_result = json.dumps({"result": {"panes": []}})
        run_herdr_checked.side_effect = [
            (False, ""),
            (True, empty_result),
        ]

        agents, hosts = asyncio.run(herdr_relay.herdr.get_all_agents())

        self.assertEqual(agents, [])
        self.assertEqual(
            hosts,
            [
                {
                    "host_id": "workstation-a",
                    "display_name": "workstation-a",
                    "online": False,
                    "status": "herdr_unavailable",
                    "ssh_reachable": True,
                    "herdr_ready": False,
                    "active_agent_count": None,
                    "capabilities": {"wake": False, "shutdown": False},
                    "harnesses": [],
                    "message": "Herdr unavailable",
                },
                {
                    "host_id": "workstation-b",
                    "display_name": "workstation-b",
                    "online": True,
                    "status": "ready",
                    "ssh_reachable": True,
                    "herdr_ready": True,
                    "active_agent_count": 0,
                    "capabilities": {"wake": False, "shutdown": False},
                    "harnesses": [],
                },
            ],
        )

    @patch.object(herdr_relay.hosts, "HOSTS", [host_record("host-a", "a"), host_record("host-b", "b")])
    @patch.object(herdr_relay.herdr, "get_agents_from_host")
    def test_hosts_are_polled_concurrently(self, get_agents_from_host):
        barrier = threading.Barrier(2, timeout=1)

        def poll_host(*, remote, host_id, host):
            barrier.wait()
            return ([{"pane_id": remote}], True)

        get_agents_from_host.side_effect = poll_host

        agents, hosts = asyncio.run(herdr_relay.herdr.get_all_agents())

        self.assertEqual(agents, [{"pane_id": "a"}, {"pane_id": "b"}])
        self.assertEqual(
            hosts,
            [
                {
                    "host_id": "host-a", "display_name": "host-a", "online": True,
                    "status": "ready", "ssh_reachable": True, "herdr_ready": True,
                    "active_agent_count": 1, "capabilities": {"wake": False, "shutdown": False},
                    "harnesses": [],
                },
                {
                    "host_id": "host-b", "display_name": "host-b", "online": True,
                    "status": "ready", "ssh_reachable": True, "herdr_ready": True,
                    "active_agent_count": 1, "capabilities": {"wake": False, "shutdown": False},
                    "harnesses": [],
                },
            ],
        )

    @patch.object(herdr_relay.lifecycle.subprocess, "run")
    def test_remote_poll_uses_keepalives(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = "ok\n"

        with tempfile.TemporaryDirectory() as control_dir:
            with patch.object(herdr_relay.config, "SSH_CONTROL_DIR", control_dir):
                self.assertEqual(
                    herdr_relay.herdr.run_herdr_checked("pane", "list", remote="workstation"),
                    (True, "ok"),
                )
            run.assert_called_once_with(
                [
                    "ssh",
                    "-o", "ConnectTimeout=5",
                    "-o", "ServerAliveInterval=3",
                    "-o", "ServerAliveCountMax=2",
                    "-o", "BatchMode=yes",
                    "-o", "ControlMaster=auto",
                    "-o", f"ControlPath={control_dir}/%C",
                    "-o", "ControlPersist=60",
                    "workstation", herdr_relay.config.HERDR, "pane", "list",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )

    @patch.object(herdr_relay.lifecycle.subprocess, "run")
    def test_remote_poll_reports_failures(self, run):
        run.side_effect = subprocess.TimeoutExpired("ssh", 15)

        with patch("builtins.print") as print_message:
            result = herdr_relay.herdr.run_herdr_checked(
                "pane", "list", remote="workstation"
            )

        self.assertEqual(result, (False, ""))
        print_message.assert_called_once()
        self.assertIn("configured host", print_message.call_args.args[0])


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
            patch.object(herdr_relay.state, "known_panes", {"pane-1"}),
            patch.object(herdr_relay.state, "known_pane_keys", {("local", "pane-1")}),
            patch.dict(herdr_relay.state.pane_hosts, {"pane-1": {"local"}}, clear=True),
            patch.object(herdr_relay.state, "pane_remote_map", {}),
            patch.object(herdr_relay.herdr, "run_herdr", side_effect=blocking_command),
        ):
            asyncio.run(run())


class PollLoopBlockingTests(unittest.TestCase):
    def test_blocked_pane_read_keeps_event_loop_running(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_read(pane_id, remote=None, source=None, host_id=None):
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
            patch.object(herdr_relay.herdr, "get_all_agents", fake_get_all_agents),
            patch.object(herdr_relay.herdr, "read_pane", blocking_read),
            patch.object(herdr_relay.transport, "broadcast", AsyncMock()),
            patch.object(herdr_relay.push, "send_web_push", AsyncMock()),
            patch.dict(herdr_relay.state.last_statuses, {}, clear=True),
            patch.dict(herdr_relay.state.subscriptions, {}, clear=True),
        ):
            asyncio.run(run())

    def test_pushed_blocked_event_keeps_event_loop_running(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_read(pane_id, remote=None, source=None, host_id=None):
            started.set()
            release.wait(timeout=1)
            finished.set()
            return "Do you want to proceed?"

        async def run():
            made_progress = asyncio.Event()
            herdr_relay.state.event_queue = asyncio.Queue()
            herdr_relay.state.event_queue.put_nowait({
                "pane_id": "pane-1", "status": "blocked", "host": "local",
            })
            pusher = asyncio.create_task(herdr_relay.event_push())

            async def observe_event_loop():
                await asyncio.to_thread(started.wait, 1)
                if not finished.is_set():
                    made_progress.set()
                release.set()

            try:
                await asyncio.wait_for(observe_event_loop(), timeout=3)
            finally:
                release.set()
                pusher.cancel()
            self.assertTrue(made_progress.is_set())

        original_queue = herdr_relay.state.event_queue
        try:
            with (
                patch.object(herdr_relay.herdr, "read_pane", blocking_read),
                patch.object(herdr_relay.transport, "broadcast", AsyncMock()),
                patch.object(herdr_relay.push, "send_web_push", AsyncMock()),
                patch.dict(herdr_relay.state.pane_remote_map, {}, clear=True),
                patch.dict(herdr_relay.state.pane_response_options, {}, clear=True),
            ):
                asyncio.run(run())
        finally:
            herdr_relay.state.event_queue = original_queue

    def test_unknown_pushed_blocked_event_is_not_actionable(self):
        async def run():
            with patch.object(herdr_relay.transport, "broadcast", new_callable=AsyncMock) as broadcast:
                await herdr_relay.transport._handle_pushed_event({
                    "type": "agent_event", "pane_id": "not-known", "status": "blocked",
                    "host": "buildbox", "prompt": "Do you want to proceed?\n1. Yes\n2. No",
                })
                broadcast.assert_not_awaited()

        with (
            patch.object(herdr_relay.state, "known_panes", set()),
            patch.dict(herdr_relay.state.pane_dialogs, {}, clear=True),
            patch.dict(herdr_relay.state.pane_response_options, {}, clear=True),
        ):
            asyncio.run(run())
        self.assertNotIn("not-known", herdr_relay.state.pane_dialogs)


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
                    herdr_relay.protocol.attention_state(status, previous_status), expected
                )

        self.assertEqual(herdr_relay.protocol.attention_state("idle", "working"), "done")
        self.assertEqual(herdr_relay.protocol.attention_state("idle", "idle"), "idle")
        self.assertEqual(herdr_relay.protocol.attention_state("idle", "idle", "done"), "done")

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
            herdr_relay.herdr, "get_all_agents", side_effect=get_all_agents
        ), patch.object(herdr_relay.transport, "broadcast", side_effect=broadcast), patch.object(
            herdr_relay.protocol, "now_ms", return_value=1000
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
                    herdr_relay.herdr, "get_all_agents", return_value=(agents, [])
                ), patch.object(herdr_relay.transport, "broadcast", side_effect=broadcast):
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
            herdr_relay.herdr, "get_all_agents", side_effect=get_all_agents
        ), patch.object(herdr_relay.transport, "broadcast", side_effect=broadcast), patch.object(
            herdr_relay.protocol, "now_ms", side_effect=[1000, 2000, 3000]
        ):
            for _ in range(4):
                asyncio.run(herdr_relay._poll_once())

        entries = [frame["agents"][0] for frame in sent if frame["type"] == "agents"]
        self.assertEqual([entry["updated_at"] for entry in entries], [1000, 1000, 2000, 3000])

    def test_blocked_prompt_change_creates_new_dialog_revision(self):
        agents = [self.agent(status="blocked", revision=1)]
        snapshots = [agents, agents, agents]
        prompts = iter([
            "Do you want to proceed?\n1. Yes\n2. No",
            "Do you want to continue?\n1. Yes\n2. No",
            "Do you want to continue?\n1. Yes\n2. No",
        ])
        sent = []

        async def get_all_agents():
            return snapshots.pop(0), []

        async def broadcast(frame):
            sent.append(frame)

        with self.poll_state(), patch.object(
            herdr_relay.herdr, "get_all_agents", side_effect=get_all_agents
        ), patch.object(
            herdr_relay.herdr, "read_pane", side_effect=lambda *args, **kwargs: next(prompts)
        ), patch.object(
            herdr_relay.transport, "broadcast", side_effect=broadcast
        ), patch.object(
            herdr_relay.push, "send_web_push", new_callable=AsyncMock
        ):
            for _ in range(3):
                asyncio.run(herdr_relay._poll_once())

        blocked = [frame for frame in sent if frame["type"] == "blocked"]
        self.assertEqual(2, len(blocked))
        self.assertNotEqual(blocked[0]["dialog_id"], blocked[1]["dialog_id"])
        self.assertEqual(1, blocked[0]["revision"])
        self.assertEqual(2, blocked[1]["revision"])

    def test_consumed_identical_prompt_renews_only_for_changed_output_revision(self):
        agents = [self.agent(status="blocked", revision=1)]
        snapshots = [agents, agents, [self.agent(status="blocked", revision=2)]]
        sent = []

        async def get_all_agents():
            return snapshots.pop(0), []

        async def broadcast(frame):
            sent.append(frame)

        with self.poll_state(), patch.object(
            herdr_relay.herdr, "get_all_agents", side_effect=get_all_agents
        ), patch.object(
            herdr_relay.herdr, "read_pane", return_value="Do you want to proceed?\n1. Yes\n2. No"
        ), patch.object(
            herdr_relay.transport, "broadcast", side_effect=broadcast
        ), patch.object(
            herdr_relay.push, "send_web_push", new_callable=AsyncMock
        ):
            asyncio.run(herdr_relay._poll_once())
            herdr_relay.state.pane_dialogs[("local", "pane-1")]["consumed"] = True
            asyncio.run(herdr_relay._poll_once())
            asyncio.run(herdr_relay._poll_once())

        blocked = [frame for frame in sent if frame["type"] == "blocked"]
        self.assertEqual(2, len(blocked))
        self.assertEqual([1, 2], [frame["revision"] for frame in blocked])

    def test_missing_or_bool_revision_is_omitted(self):
        pane_list = json.dumps({"result": {"panes": [
            {"pane_id": "missing", "agent": "claude"},
            {"pane_id": "boolean", "agent": "claude", "revision": True},
        ]}})
        with patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, pane_list)):
            agents, _online = herdr_relay.herdr.get_agents_from_host()

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
            herdr_relay.herdr, "get_all_agents", side_effect=get_all_agents
        ), patch.object(herdr_relay.transport, "broadcast", side_effect=broadcast), patch.object(
            herdr_relay.protocol, "now_ms", return_value=1000
        ):
            asyncio.run(herdr_relay._poll_once())
            asyncio.run(herdr_relay._poll_once())
            self.assertEqual(herdr_relay.state.pane_activity, {})
            self.assertEqual(herdr_relay.state.pane_revisions, {})
            self.assertEqual(herdr_relay.state.pane_attention_states, {})

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
        stack.enter_context(patch.dict(herdr_relay.state.last_statuses, {}, clear=True))
        stack.enter_context(patch.dict(herdr_relay.state.pane_activity, {}, clear=True))
        stack.enter_context(patch.dict(herdr_relay.state.pane_revisions, {}, clear=True))
        stack.enter_context(patch.dict(herdr_relay.state.pane_attention_states, {}, clear=True))
        stack.enter_context(patch.dict(herdr_relay.state.pane_dialogs, {}, clear=True))
        stack.enter_context(patch.dict(herdr_relay.state.pane_dialog_revisions, {}, clear=True))
        stack.enter_context(patch.dict(herdr_relay.state.subscriptions, {}, clear=True))
        return stack


class HostPowerTests(unittest.TestCase):
    """Power is allowlisted by the host configuration and by nothing else (#45)."""

    @staticmethod
    def powered_host(**overrides):
        return host_record(
            "workstation",
            "ssh-target",
            power={"wake": {"mac": "34:5a:60:ba:8e:20"}, "shutdown": True},
            **overrides,
        )

    def configure(self, *records):
        # `TestCase.enterContext` is 3.11+ and the relay supports 3.10, so
        # enter each patch and register its unwind by hand.
        for context in configured_hosts(*records):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    @patch.object(herdr_relay.lifecycle.subprocess, "run")
    def test_wake_is_a_fixed_magic_packet_command(self, run):
        self.configure(self.powered_host())
        run.return_value.returncode = 0

        response = herdr_relay.wake_host({"request_id": "request-1", "host_id": "workstation"})

        self.assertEqual(response["type"], "command_ack")
        run.assert_called_once_with(
            [herdr_relay.config.WAKE_BIN, "34:5a:60:ba:8e:20"],
            capture_output=True,
            text=True,
            timeout=10,
        )

    @patch.object(herdr_relay.lifecycle.subprocess, "run")
    def test_shutdown_is_a_fixed_non_interactive_ssh_command(self, run):
        self.configure(self.powered_host())
        run.return_value.returncode = 0

        with tempfile.TemporaryDirectory() as control_dir:
            with patch.object(herdr_relay.config, "SSH_CONTROL_DIR", control_dir):
                response = herdr_relay.shutdown_host({
                    "request_id": "request-2",
                    "host_id": "workstation",
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
                    "-o", "ControlMaster=auto",
                    "-o", f"ControlPath={control_dir}/%C",
                    "-o", "ControlPersist=60",
                    "ssh-target",
                    "sudo", "-n", "systemctl", "poweroff",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )

    @patch.object(herdr_relay.lifecycle.subprocess, "run")
    def test_power_commands_reject_other_hosts_and_missing_confirmation(self, run):
        self.configure(self.powered_host())

        wake = herdr_relay.wake_host({"request_id": "request-1", "host_id": "other"})
        shutdown = herdr_relay.shutdown_host({"request_id": "request-2", "host_id": "workstation"})

        self.assertEqual(wake["code"], "HOST_NOT_ALLOWED")
        self.assertEqual(shutdown["code"], "CONFIRMATION_REQUIRED")
        run.assert_not_called()

    @patch.object(herdr_relay.lifecycle.subprocess, "run")
    def test_an_unconfigured_host_has_no_power_at_all(self, run):
        """#45 removed the HERDR_POWER_HOST_* pair; nothing outside the file grants power."""
        self.configure(host_record("workstation", "ssh-target"))

        wake = herdr_relay.wake_host({"request_id": "request-1", "host_id": "unlisted"})
        shutdown = herdr_relay.shutdown_host({
            "request_id": "request-2", "host_id": "unlisted", "confirmation_nonce": "nonce-1",
        })

        self.assertEqual(wake["code"], "HOST_NOT_ALLOWED")
        self.assertEqual(shutdown["code"], "HOST_NOT_ALLOWED")
        run.assert_not_called()

    @patch.object(herdr_relay.lifecycle.subprocess, "run")
    def test_a_configured_host_without_the_capability_is_still_refused(self, run):
        self.configure(host_record("workstation", "ssh-target"))

        wake = herdr_relay.wake_host({"request_id": "request-1", "host_id": "workstation"})
        shutdown = herdr_relay.shutdown_host({
            "request_id": "request-2", "host_id": "workstation", "confirmation_nonce": "nonce-1",
        })

        self.assertEqual(wake["code"], "HOST_NOT_ALLOWED")
        self.assertEqual(shutdown["code"], "HOST_NOT_ALLOWED")
        run.assert_not_called()


class RemoteArgumentQuotingTests(unittest.TestCase):
    """A remote argument must reach `herdr` as the one argument it left as.

    ssh flattens the remote command into a single string and the login shell
    re-splits it. Unquoted, a `send_text` containing a space fell apart into
    extra arguments the CLI refused — silently, since `run_herdr` drops the
    exit code — and shell metacharacters in a prompt executed on the host.
    """

    @patch.object(herdr_relay.herdr, "run_process_checked", return_value=(True, ""))
    def test_remote_arguments_survive_the_login_shell(self, run):
        text = "fix the login bug; echo $(uname)"
        herdr_relay.herdr.run_herdr_checked(
            "pane", "send-text", "%5", text, remote="user@host",
        )

        cmd = run.call_args.args[0]
        # What ssh does: join to one string, then let the remote shell split it.
        remote_command = " ".join(cmd[cmd.index("user@host") + 1:])
        self.assertEqual(
            shlex.split(remote_command),
            [herdr_relay.config.HERDR, "pane", "send-text", "%5", text],
        )

    @patch.object(herdr_relay.herdr, "run_process_checked", return_value=(True, ""))
    def test_the_configured_command_is_left_to_the_shell(self, run):
        # The wrapper and binary are operator configuration, not client input;
        # quoting them would break a path that leans on expansion (`~/bin/herdr`).
        herdr_relay.herdr.run_herdr_checked(
            "pane", "read", "%5", remote="user@host", command=["~/bin/herdr"],
        )

        cmd = run.call_args.args[0]
        self.assertIn("~/bin/herdr", cmd)

    @patch.object(herdr_relay.herdr, "run_process_checked", return_value=(True, ""))
    def test_local_arguments_are_passed_exec_style(self, run):
        herdr_relay.herdr.run_herdr_checked("pane", "send-text", "%5", "two words")

        self.assertEqual(
            run.call_args.args[0],
            [herdr_relay.config.HERDR, "pane", "send-text", "%5", "two words"],
        )


class SshMultiplexingTests(unittest.TestCase):
    """#19: one shared connection per host instead of one per poll."""

    def setUp(self):
        # The usability verdict is cached per directory, and these cases reuse
        # directory names across tests.
        herdr_relay.herdr._control_dir_state = None
        self.addCleanup(setattr, herdr_relay.herdr, "_control_dir_state", None)

    def test_control_directory_is_created_private(self):
        with tempfile.TemporaryDirectory() as parent:
            control_dir = os.path.join(parent, "ssh")
            with patch.object(herdr_relay.config, "SSH_CONTROL_DIR", control_dir):
                options = herdr_relay.herdr.ssh_options()

            self.assertIn(f"ControlPath={control_dir}/%C", options)
            # ssh creates the socket, never the directory holding it, so an
            # absent directory means every multiplexed call fails.
            self.assertTrue(os.path.isdir(control_dir))
            self.assertEqual(os.stat(control_dir).st_mode & 0o777, 0o700)

    def test_overlong_control_path_disables_multiplexing_rather_than_failing(self):
        # An over-long ControlPath does not degrade to a direct connection —
        # ssh exits immediately — so every remote host would go dark.
        with tempfile.TemporaryDirectory() as parent:
            control_dir = os.path.join(parent, "d" * 100)
            with patch.object(herdr_relay.config, "SSH_CONTROL_DIR", control_dir):
                options = herdr_relay.herdr.ssh_options()

        self.assertEqual(options, herdr_relay.herdr.SSH_OPTIONS)
        self.assertNotIn("ControlMaster=auto", options)

    def test_a_control_path_at_the_limit_is_refused(self):
        # ssh compares `>= 104`, sun_path being a C string. A directory that
        # produces exactly 104 characters passes a `> 104` check and then fails
        # every call — the one length this has to get right.
        with tempfile.TemporaryDirectory() as parent:
            padding = 104 - len(parent) - len("/") - len("/") - 40
            self.assertGreater(padding, 0, "tempdir too long for this case")
            control_dir = os.path.join(parent, "d" * padding)
            with patch.object(herdr_relay.config, "SSH_CONTROL_DIR", control_dir):
                options = herdr_relay.herdr.ssh_options()

        self.assertEqual(len(f"{control_dir}/{'c' * 40}"), 104)
        self.assertEqual(options, herdr_relay.herdr.SSH_OPTIONS)

    def test_a_configured_tilde_is_expanded_before_the_directory_is_made(self):
        # ssh expands `~` in a ControlPath itself. Handing it one we never
        # expanded means creating the directory somewhere ssh does not look,
        # and a missing directory fails the call rather than degrading.
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {"HOME": home}), patch.object(
                herdr_relay.config, "SSH_CONTROL_DIR", "~/sock"
            ):
                options = herdr_relay.herdr.ssh_options()

            self.assertIn(f"ControlPath={home}/sock/%C", options)
            self.assertTrue(os.path.isdir(os.path.join(home, "sock")))

    def test_unusable_control_directory_disables_multiplexing(self):
        with tempfile.TemporaryDirectory() as parent:
            # A regular file where the directory should be: makedirs raises,
            # and the fallback must be a working relay, not an exception.
            control_dir = os.path.join(parent, "occupied")
            with open(control_dir, "w") as handle:
                handle.write("")
            with patch.object(herdr_relay.config, "SSH_CONTROL_DIR", control_dir):
                options = herdr_relay.herdr.ssh_options()

        self.assertEqual(options, herdr_relay.herdr.SSH_OPTIONS)

    def test_folder_browsing_reuses_the_masters_the_poll_loop_opens(self):
        # project_fs used to carry its own copy of the option list, so every
        # browse opened a second connection beside the master already up.
        with tempfile.TemporaryDirectory() as control_dir:
            with patch.object(
                herdr_relay.config, "SSH_CONTROL_DIR", control_dir
            ), patch.object(
                herdr_relay.project_fs.hosts, "ssh_target", return_value="user@host"
            ), patch.object(herdr_relay.project_fs.subprocess, "run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = json.dumps(
                    {"ok": True, "canonical_path": "/work", "entries": []}
                )
                herdr_relay.project_fs.browse_remote({"host_id": "h"}, "/work", [])

            argv = run.call_args[0][0]
            self.assertEqual(argv[0], "ssh")
            self.assertIn("ControlMaster=auto", argv)
            self.assertIn(f"ControlPath={control_dir}/%C", argv)


class PollPacingTests(unittest.TestCase):
    """#19: the poll loop costs less when nothing is happening."""

    def setUp(self):
        herdr_relay.state.poll_idle_streak = 0
        herdr_relay.state.poll_wakeup.clear()
        self.addCleanup(setattr, herdr_relay.state, "poll_idle_streak", 0)
        self.addCleanup(herdr_relay.state.poll_wakeup.clear)
        # Entered once per test, not once per poll: a pane the loop has never
        # seen is itself a change, so clearing between two polls of the same
        # test would make every cycle look busy and prove nothing.
        stack = PaneMetadataTests.poll_state()
        stack.enter_context(patch.object(herdr_relay.state, "clients", set()))
        stack.__enter__()
        self.addCleanup(stack.close)

    def test_interval_climbs_geometrically_to_the_ceiling(self):
        interval = herdr_relay.transport.poll_interval
        self.assertEqual(interval(0), herdr_relay.config.POLL_INTERVAL)
        self.assertEqual(interval(1), 3.0)
        self.assertEqual(interval(2), 4.5)
        # Clamped, and never past the ceiling however long the quiet lasts.
        self.assertEqual(interval(50), herdr_relay.config.POLL_INTERVAL_MAX)
        # A negative streak is not a shorter-than-floor poll.
        self.assertEqual(interval(-1), herdr_relay.config.POLL_INTERVAL)

    def test_a_very_long_quiet_spell_does_not_kill_the_loop(self):
        # The streak is unbounded, and `1.5 ** 2000` raises OverflowError rather
        # than saturating. poll_interval supplies the timeout of the wait, which
        # sits outside the try guarding a failed cycle, so raising here stops
        # the relay polling until it is restarted.
        interval = herdr_relay.transport.poll_interval
        self.assertEqual(interval(10_000), herdr_relay.config.POLL_INTERVAL_MAX)
        with patch.object(herdr_relay.config, "POLL_BACKOFF_FACTOR", 1e300):
            self.assertEqual(interval(3), herdr_relay.config.POLL_INTERVAL_MAX)

    @staticmethod
    def run_poll(agents, clients=(), operations=()):
        async def get_all_agents():
            return agents, []

        async def broadcast(_frame):
            return None

        with patch.object(
            herdr_relay.herdr, "get_all_agents", side_effect=get_all_agents
        ), patch.object(
            herdr_relay.transport, "broadcast", side_effect=broadcast
        ), patch.object(
            herdr_relay.operations, "public_recovery", return_value=list(operations)
        ):
            herdr_relay.state.clients.update(clients)
            asyncio.run(herdr_relay._poll_once())
        return herdr_relay.state.poll_idle_streak

    def test_quiet_cycles_accumulate_and_a_busy_one_resets(self):
        idle = [PaneMetadataTests.agent(status="idle")]
        # The first poll of a pane is itself a change, so the streak starts at 0.
        self.assertEqual(self.run_poll(idle), 0)
        herdr_relay.state.poll_idle_streak = 3
        self.assertEqual(self.run_poll(idle), 4)
        self.assertEqual(
            self.run_poll([PaneMetadataTests.agent(status="working")]), 0
        )

    def test_a_working_agent_holds_the_loop_at_its_floor_while_unchanged(self):
        # A long tool call reports "working" poll after poll with no new output.
        # Nothing has changed, but the turn is live and its end must land fast.
        working = [PaneMetadataTests.agent(status="working")]
        self.run_poll(working)  # First sight of a pane is a change; get past it.
        herdr_relay.state.poll_idle_streak = 4
        self.assertEqual(self.run_poll(working), 0)

    def test_a_connected_client_holds_the_loop_at_its_floor(self):
        # Not "a subscribed client": the agent list is what a client sees before
        # it subscribes to anything, and a status change there is what it opened
        # the app for. Backing off behind a connected client trades one poll for
        # up to POLL_INTERVAL_MAX of stale dashboard.
        idle = [PaneMetadataTests.agent(status="idle")]
        self.run_poll(idle)  # First sight of a pane is a change; get past it.
        herdr_relay.state.poll_idle_streak = 4
        self.assertEqual(self.run_poll(idle), 5, "nobody connected: expected backoff")
        self.assertEqual(self.run_poll(idle, clients=[object()]), 0)

    def test_an_active_operation_holds_the_loop_at_its_floor(self):
        idle = [PaneMetadataTests.agent(status="idle")]
        self.run_poll(idle)  # First sight of a pane is a change; get past it.
        # A terminal operation in the recovery frame is history, not work.
        herdr_relay.state.poll_idle_streak = 2
        self.assertEqual(self.run_poll(idle, operations=[{"stage": "started"}]), 3)
        herdr_relay.state.poll_idle_streak = 2
        self.assertEqual(self.run_poll(idle, operations=[{"stage": "waiting_for_host"}]), 0)

    def test_changed_output_holds_the_loop_at_its_floor(self):
        self.run_poll([PaneMetadataTests.agent(status="idle", revision=1)])
        herdr_relay.state.poll_idle_streak = 5
        # Same status, new output: the pane is producing, so drop back to the
        # floor even though its status never moved.
        streak = self.run_poll([PaneMetadataTests.agent(status="idle", revision=2)])
        self.assertEqual(streak, 0)

    def test_an_edge_cuts_the_backoff_short(self):
        async def scenario():
            herdr_relay.state.poll_idle_streak = 50  # would wait the full ceiling
            polls = 0

            async def poll_once():
                nonlocal polls
                polls += 1
                if polls == 1:
                    # Arrives while the cycle is still running: it must still
                    # be seen, not cleared out from under the wait below.
                    herdr_relay.transport.wake_poll_loop()

            with patch.object(herdr_relay.transport, "_poll_once", side_effect=poll_once):
                task = asyncio.create_task(herdr_relay.poll_loop())
                await asyncio.sleep(0.05)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            return polls

        # Two polls inside 50ms only happens if the 10s backoff was interrupted.
        self.assertGreaterEqual(asyncio.run(scenario()), 2)

    def test_a_pushed_event_wakes_the_loop(self):
        async def scenario():
            queue = asyncio.Queue()
            await queue.put({"type": "operation_event", "operation": {"stage": "queued"}})
            with patch.object(herdr_relay.state, "event_queue", queue), patch.object(
                herdr_relay.transport, "broadcast", new=AsyncMock()
            ):
                task = asyncio.create_task(herdr_relay.event_push())
                await asyncio.sleep(0.05)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            return herdr_relay.state.poll_wakeup.is_set()

        self.assertTrue(asyncio.run(scenario()))


class PaneChromeTests(unittest.TestCase):
    @patch.object(herdr_relay.herdr, "run_herdr")
    def test_read_pane_filters_heavy_opencode_chrome(self, run_herdr):
        run_herdr.return_value = "\n".join(
            [
                "┃ Permission required: access external directory ┃",
                "╹▀▀▀▀▀▀▀▀",
                "⬝⬝⬝⬝ esc interrupt",
            ]
        )

        self.assertEqual(
            herdr_relay.herdr.read_pane("pane-1"),
            "┃ Permission required: access external directory ┃",
        )

    def test_meaningful_status_with_footer_is_not_all_chrome(self):
        line = "┃ ┃ Build · GPT-5.6 Sol OpenAI ~/src:main ╹▀▀ ⬝⬝ esc interrupt"

        self.assertIsNone(herdr_relay.panes.CHROME_RE.search(line))

    @patch.object(herdr_relay.herdr, "run_herdr", return_value="first\nsecond\nthird")
    def test_read_pane_uses_visible_by_default_and_honors_recent(self, run_herdr):
        self.assertEqual("first\nsecond\nthird", herdr_relay.herdr.read_pane("pane-1"))
        run_herdr.assert_called_once_with(
            "pane", "read", "pane-1", "--lines", "30", "--source", "visible",
            remote=None
        )

        run_herdr.reset_mock()
        self.assertEqual(
            "second\nthird",
            herdr_relay.herdr.read_pane("pane-1", lines=2, source="recent"),
        )
        run_herdr.assert_called_once_with(
            "pane", "read", "pane-1", "--lines", "2", "--source", "recent", remote=None
        )

    def test_read_pane_rejects_unknown_source(self):
        with self.assertRaisesRegex(ValueError, "invalid pane source"):
            herdr_relay.herdr.read_pane("pane-1", source="all")


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
                patch.dict(herdr_relay.state.last_statuses, {("local", "pane-1"): status}, clear=True),
                patch.object(herdr_relay.state, "known_panes", {"pane-1"}),
                patch.object(herdr_relay.state, "known_pane_keys", {("local", "pane-1")}),
                patch.dict(herdr_relay.state.pane_hosts, {"pane-1": {"local"}}, clear=True),
                patch.dict(herdr_relay.state.pane_response_options, {}, clear=True),
                patch.dict(herdr_relay.state.pane_dialogs, {}, clear=True),
                patch.object(herdr_relay.herdr, "run_herdr", return_value=prompt),
                patch.object(herdr_relay.transcripts.blocks, "pane_blocks", return_value=(None, None)),
            ):
                asyncio.run(herdr_relay.handle_client(socket))

            frames = after_handshake(socket.sent)
            self.assertEqual(
                [message["type"] for message in frames],
                expected_types,
            )
            if status == "blocked":
                self.assertEqual(frames[1]["prompt"], prompt)
                self.assertEqual(frames[1]["options"], herdr_relay.panes.OPENCODE_OPTIONS)

    def test_read_pane_keeps_remote_dialog_identity(self):
        class Socket:
            request_headers = {}

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

        socket = Socket()
        with (
            patch.dict(herdr_relay.state.last_statuses, {("buildbox", "pane-1"): "blocked"}, clear=True),
            patch.object(herdr_relay.state, "known_panes", {"pane-1"}),
            patch.object(herdr_relay.state, "known_pane_keys", {("buildbox", "pane-1")}),
            patch.dict(herdr_relay.state.pane_hosts, {"pane-1": {"buildbox"}}, clear=True),
            patch.dict(herdr_relay.state.pane_remote_map, {("buildbox", "pane-1"): "deploy@buildbox"}, clear=True),
            patch.dict(herdr_relay.state.pane_cwd_map, {
                ("buildbox", "pane-1"): ("/srv/repo", "claude", "deploy@buildbox", False)
            }, clear=True),
            patch.dict(herdr_relay.state.pane_host_map, {("buildbox", "pane-1"): "buildbox"}, clear=True),
            patch.dict(herdr_relay.state.pane_project_map, {("buildbox", "pane-1"): "repo"}, clear=True),
            patch.dict(herdr_relay.state.pane_dialogs, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialog_revisions, {}, clear=True),
            patch.object(herdr_relay.herdr, "run_herdr", return_value="Do you want to proceed?\n1. Yes\n2. No"),
            patch.object(herdr_relay.transcripts.blocks, "pane_blocks", return_value=(None, None)),
        ):
            herdr_relay.dialogs.ensure(
                "pane-1", "old prompt", ["yes", "no"],
                agent="claude", project="repo", host="buildbox",
            )
            asyncio.run(herdr_relay.handle_client(socket))

        frame = after_handshake(socket.sent)[1]
        self.assertEqual("buildbox", frame["host"])
        self.assertEqual("claude", frame["agent"])
        self.assertEqual("repo", frame["project"])

    def test_claude_project_dir(self):
        self.assertEqual(
            herdr_relay.transcripts.claude.claude_project_dir("/Users/me/src/herdr-mobile"),
            "-Users-me-src-herdr-mobile",
        )
        self.assertEqual(
            herdr_relay.transcripts.claude.claude_project_dir("/home/me/my_app.v2"),
            "-home-me-my-app-v2",
        )

    def test_summarize_tool(self):
        self.assertEqual(
            herdr_relay.transcripts.claude.summarize_tool({"file_path": "/etc/hosts", "content": "x"}),
            "/etc/hosts",
        )
        self.assertEqual(
            herdr_relay.transcripts.claude.summarize_tool({"command": "make build\nmake test"}),
            "make build",
        )
        self.assertEqual(herdr_relay.transcripts.claude.summarize_tool(None), "")

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

        blocks = herdr_relay.transcripts.claude.transcript_to_blocks(fixture)

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

    def test_claude_assistant_string_content_is_preserved(self):
        fixture = json.dumps({
            "type": "assistant", "uuid": "assistant-string",
            "message": {"role": "assistant", "content": "plain assistant text"},
        })
        blocks = herdr_relay.transcripts.claude.transcript_to_blocks(fixture)
        self.assertEqual(blocks[0]["kind"], "assistant_text")
        self.assertEqual(blocks[0]["markdown"], "plain assistant text")

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
            [block["kind"] for block in herdr_relay.transcripts.claude.transcript_to_blocks(fixture)],
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
        blocks = herdr_relay.transcripts.claude.transcript_to_blocks(fixture, limit=10)
        self.assertEqual(
            [block["markdown"] for block in blocks],
            [str(index) for index in range(240, 250)],
        )

    def test_claude_ids_and_metadata_survive_appends(self):
        first = {
            "type": "assistant", "uuid": "assistant-1",
            "timestamp": "2026-09-01T00:00:00Z",
            "message": {"id": "provider-message-1", "content": [{"type": "text", "text": "first"}]},
        }
        appended = {
            "type": "assistant", "uuid": "assistant-2",
            "timestamp": "2026-09-01T00:00:01Z",
            "message": {"content": [{"type": "text", "text": "second"}]},
        }
        before = herdr_relay.transcripts.claude.transcript_to_blocks(json.dumps(first))
        after = herdr_relay.transcripts.claude.transcript_to_blocks(
            "\n".join((json.dumps(first), json.dumps(appended)))
        )
        self.assertEqual(before[0]["id"], "b:assistant-1:0")
        self.assertEqual(after[0]["id"], before[0]["id"])
        self.assertEqual(after[0]["role"], "assistant")
        self.assertEqual(after[0]["message_id"], "provider-message-1")
        self.assertEqual(after[0]["timestamp"], 1788220800000)

    def test_uuid_block_ids_survive_an_older_tail_window_record_disappearing(self):
        older = {
            "type": "assistant", "uuid": "older-row",
            "message": {"content": [{"type": "text", "text": "old"}]},
        }
        survivor = {
            "type": "assistant", "uuid": "survivor-row",
            "message": {"content": [{"type": "text", "text": "survives"}]},
        }
        newer = {
            "type": "assistant", "uuid": "newer-row",
            "message": {"content": [{"type": "text", "text": "newer"}]},
        }
        full = herdr_relay.transcripts.claude.transcript_to_blocks(
            "\n".join((json.dumps(older), json.dumps(survivor), json.dumps(newer))), limit=2
        )
        tail = herdr_relay.transcripts.claude.transcript_to_blocks(
            "\n".join((json.dumps(survivor), json.dumps(newer))), limit=2
        )
        self.assertEqual([block["id"] for block in full], ["b:survivor-row:0", "b:newer-row:0"])
        self.assertEqual([block["id"] for block in tail], [block["id"] for block in full])
        full_page = herdr_relay.transcripts.blocks.paginate_blocks(full, limit=1, max_bytes=10000)
        tail_page = herdr_relay.transcripts.blocks.paginate_blocks(tail, limit=1, max_bytes=10000)
        self.assertEqual(full_page[0], tail_page[0])
        self.assertEqual(full_page[3], tail_page[3])
        self.assertEqual(
            herdr_relay.transcripts.blocks.paginate_blocks(full, before="stale"),
            ([], 2, False, None),
        )

    def test_claude_tool_result_and_edit_are_structured_and_bounded(self):
        fixture = "\n".join(json.dumps(record) for record in [
            {"type": "assistant", "uuid": "a1", "message": {"content": [{
                "type": "tool_use", "id": "tool-1", "name": "Edit",
                "input": {"file_path": "app.py", "old_string": "old\n", "new_string": "new\n"},
            }]}},
            {"type": "user", "uuid": "u1", "message": {"content": [{
                "type": "tool_result", "tool_use_id": "tool-1", "content": "result\nmore",
            }]}},
        ])
        blocks = herdr_relay.transcripts.claude.transcript_to_blocks(fixture)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["kind"], "diff")
        self.assertEqual(blocks[0]["role"], "tool")
        self.assertRegex(blocks[0]["markdown"], r"^--- a/app\.py\n\+\+\+ b/app\.py\n@@ ")
        self.assertIn("-old", blocks[0]["markdown"])
        self.assertIn("+new", blocks[0]["markdown"])
        self.assertEqual(blocks[0]["result"], "result\nmore")

    def test_claude_write_diff_has_a_consumable_hunk_header(self):
        fixture = json.dumps({
            "type": "assistant", "uuid": "write-row", "message": {"content": [{
                "type": "tool_use", "id": "write-1", "name": "Write",
                "input": {"file_path": "new.py", "content": "one\ntwo\n"},
            }]},
        })
        block = herdr_relay.transcripts.claude.transcript_to_blocks(fixture)[0]
        self.assertEqual(block["kind"], "diff")
        self.assertIn("@@ -0,0 +1,2 @@", block["markdown"])

    def test_orphan_tool_result_is_not_dropped(self):
        fixture = json.dumps({
            "type": "user", "uuid": "result-row", "message": {"content": [{
                "type": "tool_result", "tool_use_id": "outside-tail",
                "content": "orphan result",
            }]},
        })
        blocks = herdr_relay.transcripts.claude.transcript_to_blocks(fixture)
        self.assertEqual(blocks[0]["kind"], "tool")
        self.assertEqual(blocks[0]["label"], "Tool result")
        self.assertEqual(blocks[0]["result"], "orphan result")

    def test_opencode_part_ids_are_stable_without_changing_legacy_kinds(self):
        document = {
            "rows": [
                ["user", "message-1", 1700000000000, "part-1",
                 json.dumps({"type": "text", "text": "hello"})],
                ["assistant", "message-2", 1700000001000, "part-2",
                 json.dumps({"type": "text", "text": "world"})],
            ],
        }
        blocks = herdr_relay.transcripts.opencode.opencode_to_blocks(document)
        self.assertEqual([block["id"] for block in blocks], ["o:part-1", "o:part-2"])
        self.assertEqual([block["kind"] for block in blocks], ["status", "assistant_text"])
        self.assertEqual(blocks[1]["message_id"], "message-2")
        self.assertEqual(blocks[1]["timestamp"], 1700000001000)

    def test_transcript_cache_skips_unchanged_parse(self):
        herdr_relay.transcripts.claude.clear_parse_cache()
        with patch.object(
            herdr_relay.transcripts.claude,
            "transcript_to_blocks",
            wraps=herdr_relay.transcripts.claude.transcript_to_blocks,
        ) as parse:
            body = json.dumps({
                "type": "assistant", "uuid": "a1",
                "message": {"content": [{"type": "text", "text": "cached"}]},
            })
            first = herdr_relay.transcripts.claude.cached_transcript_to_blocks("x", body)
            second = herdr_relay.transcripts.claude.cached_transcript_to_blocks("x", body)
        self.assertIs(first, second)
        parse.assert_called_once()

    def test_structured_pagination_is_cursor_and_utf8_byte_bounded(self):
        blocks = [
            {"id": f"b:{index}", "kind": "status", "text": "я" * 20}
            for index in range(5)
        ]
        page, total, has_more, cursor = herdr_relay.transcripts.blocks.paginate_blocks(
            blocks, limit=5, max_bytes=150
        )
        self.assertEqual(total, 5)
        self.assertTrue(has_more)
        self.assertEqual(cursor, page[0]["id"])
        self.assertLessEqual(
            sum(len(json.dumps(block, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) for block in page),
            150,
        )
        older, _, _, _ = herdr_relay.transcripts.blocks.paginate_blocks(
            blocks, limit=5, before=cursor, max_bytes=10000
        )
        self.assertNotIn(cursor, [block["id"] for block in older])
        self.assertLess(int(older[-1]["id"].split(":")[-1]), int(cursor.split(":")[-1]))

    def test_unknown_structured_cursor_returns_end_without_restarting(self):
        blocks = [{"id": "b:1", "kind": "status", "text": "one"}]
        page, total, has_more, cursor = herdr_relay.transcripts.blocks.paginate_blocks(
            blocks, before="stale-cursor"
        )
        self.assertEqual((page, total, has_more, cursor), ([], 1, False, None))

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
                patch.object(herdr_relay.config, "CLAUDE_PROJECTS", directory),
                patch.dict(herdr_relay.state.pane_session_refs, {}, clear=True),
                patch.object(herdr_relay.state, "known_pane_keys", {("local", "first"), ("local", "second")}),
                patch.dict(herdr_relay.state.pane_hosts, {"first": {"local"}, "second": {"local"}}, clear=True),
                patch.dict(
                    herdr_relay.state.pane_cwd_map,
                    {
                        ("local", "first"): ("/work/repo", "claude", None, True),
                        ("local", "second"): ("/work/repo", "claude", None, True),
                    },
                    clear=True,
                ),
                patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, pane_list)),
            ):
                agents, _online = herdr_relay.herdr.get_agents_from_host()
                first_blocks, _first_signature = herdr_relay.transcripts.blocks.pane_blocks("first")
                second_blocks, _second_signature = herdr_relay.transcripts.blocks.pane_blocks("second")

            self.assertNotIn("agent_session", agents[0])
            self.assertEqual(first_blocks[0]["markdown"], "first conversation")
            self.assertEqual(second_blocks[0]["markdown"], "second conversation")
            self.assertNotEqual(first_blocks, second_blocks)

    def test_ambiguous_cwd_without_refs_is_not_streamed(self):
        with (
            patch.dict(
                herdr_relay.state.pane_cwd_map,
                {("local", "ambiguous"): ("/work/repo", "claude", None, True)},
                clear=True,
            ),
            patch.dict(herdr_relay.state.pane_session_refs, {}, clear=True),
        ):
            self.assertEqual(herdr_relay.transcripts.blocks.pane_blocks("ambiguous"), (None, None))

    def test_claude_id_ref_uses_project_transcript_path(self):
        cwd = "/work/repo"
        session_id = "123e4567-e89b-42d3-a456-426614174000"
        with tempfile.TemporaryDirectory() as projects:
            project = os.path.join(projects, herdr_relay.transcripts.claude.claude_project_dir(cwd))
            os.mkdir(project)
            with open(os.path.join(project, session_id + ".jsonl"), "w") as transcript:
                transcript.write(json.dumps({
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "id conversation"}]},
                }))
            with (
                patch.object(herdr_relay.config, "CLAUDE_PROJECTS", projects),
                patch.object(herdr_relay.state, "known_pane_keys", {("local", "pane-id")}),
                patch.dict(herdr_relay.state.pane_hosts, {"pane-id": {"local"}}, clear=True),
                patch.dict(
                    herdr_relay.state.pane_cwd_map,
                    {("local", "pane-id"): (cwd, "claude", None, True)},
                    clear=True,
                ),
                patch.dict(
                    herdr_relay.state.pane_session_refs,
                    {("local", "pane-id"): {
                        "agent": "claude", "kind": "id", "value": session_id,
                    }},
                    clear=True,
                ),
            ):
                blocks, _signature = herdr_relay.transcripts.blocks.pane_blocks("pane-id")

                self.assertEqual(blocks[0]["markdown"], "id conversation")

    def test_claude_id_ref_with_newer_uuid_version_is_accepted(self):
        cwd = "/work/repo"
        session_id = "123e4567-e89b-62d3-a456-426614174000"
        with tempfile.TemporaryDirectory() as projects:
            project = os.path.join(projects, herdr_relay.transcripts.claude.claude_project_dir(cwd))
            os.mkdir(project)
            with open(os.path.join(project, session_id + ".jsonl"), "w") as transcript:
                transcript.write(json.dumps({
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "new UUID"}]},
                }))
            with (
                patch.object(herdr_relay.config, "CLAUDE_PROJECTS", projects),
                patch.object(herdr_relay.state, "known_pane_keys", {("local", "pane-id")}),
                patch.dict(herdr_relay.state.pane_hosts, {"pane-id": {"local"}}, clear=True),
                patch.dict(
                    herdr_relay.state.pane_cwd_map,
                    {("local", "pane-id"): (cwd, "claude", None, True)},
                    clear=True,
                ),
                patch.dict(
                    herdr_relay.state.pane_session_refs,
                    {("local", "pane-id"): {
                        "agent": "claude", "kind": "id", "value": session_id,
                    }},
                    clear=True,
                ),
            ):
                blocks, _signature = herdr_relay.transcripts.blocks.pane_blocks("pane-id")
                self.assertEqual(blocks[0]["markdown"], "new UUID")

    def test_unknown_cwd_with_exact_claude_id_returns_no_blocks(self):
        with (
            patch.dict(
                herdr_relay.state.pane_cwd_map,
                {("local", "pane-id"): ("", "claude", None, False)},
                clear=True,
            ),
            patch.dict(
                herdr_relay.state.pane_session_refs,
                {("local", "pane-id"): {
                    "agent": "claude", "kind": "id",
                    "value": "123e4567-e89b-62d3-a456-426614174000",
                }},
                clear=True,
            ),
        ):
            self.assertEqual(herdr_relay.transcripts.blocks.pane_blocks("pane-id"), (None, None))

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
                "pane_id": "invalid-id", "agent": "claude", "cwd": "/work/repo",
                "agent_session": {"kind": "id", "value": "session-1"},
            },
            {
                "pane_id": "wrong-agent", "agent": "claude", "cwd": "/work/repo",
                "agent_session": {"agent": "opencode", "kind": "id", "value": "ses_1"},
            },
            {
                "pane_id": "null", "agent": "claude", "cwd": "/work/repo",
                "agent_session": None,
            },
        ]}})
        with (
            patch.dict(herdr_relay.state.pane_session_refs, {}, clear=True),
            patch.dict(
                herdr_relay.state.pane_cwd_map,
                {("local", pane_id): ("/work/repo", "claude", None, True)
                 for pane_id in ("bad-kind", "empty", "invalid-id", "wrong-agent", "null")},
                clear=True,
            ),
            patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, pane_list)),
        ):
            herdr_relay.herdr.get_agents_from_host()
            self.assertEqual(
                set(herdr_relay.state.pane_session_refs),
                {("local", pane_id) for pane_id in
                 ("bad-kind", "empty", "invalid-id", "wrong-agent")},
            )
            self.assertTrue(all(
                ref is None for ref in herdr_relay.state.pane_session_refs.values()
            ))
            for pane_id in ("bad-kind", "empty", "invalid-id", "wrong-agent", "null"):
                self.assertEqual(herdr_relay.transcripts.blocks.pane_blocks(pane_id), (None, None))

    def test_invalid_ref_does_not_fall_back_to_unambiguous_cwd(self):
        cwd = "/work/repo"
        with tempfile.TemporaryDirectory() as projects:
            project = os.path.join(projects, herdr_relay.transcripts.claude.claude_project_dir(cwd))
            os.mkdir(project)
            transcript_path = os.path.join(project, "latest.jsonl")
            with open(transcript_path, "w") as transcript:
                transcript.write(json.dumps({
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "must not leak"}]},
                }))
            with (
                patch.object(herdr_relay.config, "CLAUDE_PROJECTS", projects),
                patch.dict(
                    herdr_relay.state.pane_cwd_map,
                    {("local", "pane-id"): (cwd, "claude", None, False)},
                    clear=True,
                ),
                patch.dict(
                    herdr_relay.state.pane_session_refs,
                    {("local", "pane-id"): None},
                    clear=True,
                ),
            ):
                self.assertEqual(herdr_relay.transcripts.blocks.pane_blocks("pane-id"), (None, None))

    def test_refresh_drops_a_previous_session_ref_when_pane_omits_it(self):
        pane_id = "pane-id"
        pane_list = json.dumps({"result": {"panes": [{
            "pane_id": pane_id, "agent": "claude", "cwd": "/work/repo",
        }]}})
        with (
            patch.dict(
                herdr_relay.state.pane_session_refs,
                {("local", pane_id): {
                    "agent": "claude", "kind": "id",
                    "value": "123e4567-e89b-42d3-a456-426614174000",
                }},
                clear=True,
            ),
            patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, pane_list)),
        ):
            herdr_relay.herdr.get_agents_from_host()
            self.assertNotIn(("local", pane_id), herdr_relay.state.pane_session_refs)

    def test_claude_ref_must_match_pane_agent(self):
        with (
            patch.dict(
                herdr_relay.state.pane_cwd_map,
                {("local", "pane-id"): ("/work/repo", "claude", None, True)},
                clear=True,
            ),
            patch.dict(
                herdr_relay.state.pane_session_refs,
                {("local", "pane-id"): {
                    "agent": "opencode", "kind": "id", "value": "ses_target",
                }},
                clear=True,
            ),
        ):
            self.assertEqual(herdr_relay.transcripts.blocks.pane_blocks("pane-id"), (None, None))

    def test_missing_exact_claude_ref_does_not_fall_back_to_newest(self):
        cwd = "/work/repo"
        missing_id = "123e4567-e89b-42d3-a456-426614174000"
        other_id = "123e4567-e89b-42d3-a456-426614174001"
        with tempfile.TemporaryDirectory() as projects:
            project = os.path.join(projects, herdr_relay.transcripts.claude.claude_project_dir(cwd))
            os.mkdir(project)
            with open(os.path.join(project, other_id + ".jsonl"), "w") as transcript:
                transcript.write(json.dumps({
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "wrong conversation"}]},
                }))
            with (
                patch.object(herdr_relay.config, "CLAUDE_PROJECTS", projects),
                patch.dict(
                    herdr_relay.state.pane_cwd_map,
                    {("local", "pane-id"): (cwd, "claude", None, True)},
                    clear=True,
                ),
                patch.dict(
                    herdr_relay.state.pane_session_refs,
                    {("local", "pane-id"): {
                        "agent": "claude", "kind": "id", "value": missing_id,
                    }},
                    clear=True,
                ),
            ):
                self.assertEqual(herdr_relay.transcripts.blocks.pane_blocks("pane-id"), (None, None))

    def test_explicit_claude_path_stays_inside_configured_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = os.path.join(directory, "root")
            os.mkdir(root)
            outside = os.path.join(directory, "outside.jsonl")
            with open(outside, "w") as transcript:
                transcript.write("outside")
            symlink = os.path.join(root, "linked.jsonl")
            os.symlink(outside, symlink)
            with patch.object(herdr_relay.config, "CLAUDE_PROJECTS", root):
                self.assertEqual(
                    herdr_relay.transcripts.claude.read_transcript("/work/repo", path=outside),
                    (None, None),
                )
                self.assertEqual(
                    herdr_relay.transcripts.claude.read_transcript(
                        "/work/repo", path=os.path.join(root, "..", "outside.jsonl")
                    ),
                    (None, None),
                )
                self.assertEqual(
                    herdr_relay.transcripts.claude.read_transcript("/work/repo", path=symlink),
                    (None, None),
                )

    def test_remote_claude_path_stays_inside_configured_root(self):
        real_run = subprocess.run

        def run_remote_locally(argv, **_kwargs):
            return real_run(["sh", "-c", argv[-1]], capture_output=True, text=True, timeout=5)

        with tempfile.TemporaryDirectory() as directory:
            root = os.path.join(directory, "root")
            os.mkdir(root)
            allowed = os.path.join(root, "allowed.jsonl")
            outside = os.path.join(directory, "outside.jsonl")
            with open(allowed, "w") as transcript:
                transcript.write("allowed")
            with open(outside, "w") as transcript:
                transcript.write("outside")
            remote_symlink = os.path.join(root, "remote-linked.jsonl")
            os.symlink(outside, remote_symlink)
            with (
                patch.object(herdr_relay.config, "CLAUDE_PROJECTS", root),
                patch.object(herdr_relay.transcripts.claude.subprocess, "run", run_remote_locally),
            ):
                self.assertEqual(
                    herdr_relay.transcripts.claude.read_transcript(
                        "/work/repo", remote="host", path=allowed
                    ),
                    (os.path.realpath(allowed), "allowed"),
                )
                self.assertEqual(
                    herdr_relay.transcripts.claude.read_transcript(
                        "/work/repo", remote="host", path=outside
                    ),
                    (None, None),
                )
                self.assertEqual(
                    herdr_relay.transcripts.claude.read_transcript(
                        "/work/repo", remote="host",
                        path=os.path.join(root, "..", "outside.jsonl"),
                    ),
                    (None, None),
                )
                self.assertEqual(
                    herdr_relay.transcripts.claude.read_transcript(
                        "/work/repo", remote="host", path=remote_symlink,
                    ),
                    (None, None),
                )

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
                patch.object(herdr_relay.config, "OPENCODE_DB", db_path),
                patch.object(herdr_relay.state, "known_pane_keys", {("local", "opencode")}),
                patch.dict(herdr_relay.state.pane_hosts, {"opencode": {"local"}}, clear=True),
                patch.dict(
                    herdr_relay.state.pane_cwd_map,
                    {("local", "opencode"): ("/wrong/repo", "opencode", None, True)},
                    clear=True,
                ),
                patch.dict(
                    herdr_relay.state.pane_session_refs,
                    {("local", "opencode"): {
                        "agent": "opencode", "kind": "id", "value": "target-session",
                    }},
                    clear=True,
                ),
            ):
                blocks, _signature = herdr_relay.transcripts.blocks.pane_blocks("opencode")

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

        blocks = herdr_relay.transcripts.opencode.opencode_to_blocks(document)

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

        blocks = herdr_relay.transcripts.opencode.opencode_to_blocks(document)

        self.assertEqual(blocks[0]["markdown"], markdown)


class DetectOptionsTests(unittest.TestCase):
    def test_legacy_tool_permission(self):
        text = (
            "Do you want to allow this tool call?\n\n"
            "> yes, single permission\n"
            "> trust, always allow\n"
            "> no (tab to edit)"
        )
        self.assertEqual(herdr_relay.panes.detect_options(text), herdr_relay.panes.TOOL_OPTIONS)

    def test_subagent_options(self):
        text = "approve all pending\nconfigure individually"
        self.assertEqual(herdr_relay.panes.detect_options(text), herdr_relay.panes.SUBAGENT_OPTIONS)

    def test_claude_numbered_yes_no(self):
        text = (
            "Ask rule Bash(git add *) overrides auto mode for this command.\n"
            " /permissions to let auto mode decide\n\n"
            " Do you want to proceed?\n"
            " ❯ 1. Yes\n"
            "   2. No\n"
        )
        self.assertEqual(herdr_relay.panes.detect_options(text), ["1. Yes", "2. No"])

    def test_claude_proceed_fallback_without_numbers(self):
        text = "Do you want to proceed?\nSome other chrome"
        self.assertEqual(herdr_relay.panes.detect_options(text), ["1. Yes", "2. No"])

    def test_claude_ask_rule_fallback(self):
        text = "Ask rule Bash(git add *) overrides auto mode for this command."
        self.assertEqual(herdr_relay.panes.detect_options(text), ["1. Yes", "2. No"])

    def test_opencode_permission_required(self):
        text = (
            "△ Permission required\n"
            "  Bash · git status\n"
            "  Allow once   Allow always   Reject\n"
            "  ↔ select   enter confirm   esc dismiss\n"
        )
        self.assertEqual(herdr_relay.panes.detect_options(text), herdr_relay.panes.OPENCODE_OPTIONS)

    def test_opencode_allow_once_phrase(self):
        text = "Allow once\nAllow always\nReject\nPermission required"
        self.assertEqual(herdr_relay.panes.detect_options(text), herdr_relay.panes.OPENCODE_OPTIONS)

    def test_yn_style(self):
        self.assertEqual(herdr_relay.panes.detect_options("Continue? [y/n]"), ["y", "n"])
        self.assertEqual(herdr_relay.panes.detect_options("write to this file?\nproceed (y)"), ["y", "n"])

    def test_respond_text_numbered_label(self):
        self.assertEqual(herdr_relay.panes.respond_text("1. Yes"), "1")
        self.assertEqual(herdr_relay.panes.respond_text("2. No"), "2")
        self.assertEqual(
            herdr_relay.panes.respond_text("yes, single permission"),
            "yes, single permission",
        )

    def test_respond_action_opencode_keys(self):
        self.assertEqual(herdr_relay.panes.respond_action("Allow once"), ("keys", ["Enter"]))
        self.assertEqual(
            herdr_relay.panes.respond_action("Allow always"),
            ("keys", ["Right", "Enter", "Enter"]),
        )
        self.assertEqual(herdr_relay.panes.respond_action("Reject"), ("keys", ["Escape"]))
        self.assertEqual(herdr_relay.panes.respond_action("1. Yes"), ("text", "1"))
        self.assertEqual(herdr_relay.panes.respond_action("y"), ("text", "y"))
        # Free-text deny must not be remapped to Escape keys
        self.assertEqual(herdr_relay.panes.respond_action("deny"), ("text", "deny"))

    def test_unknown_prompt_returns_none(self):
        self.assertIsNone(herdr_relay.panes.detect_options("just some log output"))


class RelayInputValidationTests(unittest.TestCase):
    def test_key_allowlist_covers_the_web_keyboard(self):
        for key in ("Enter", "Space", "1", "Ctrl+c", "ctrl+d", "shift+1"):
            with self.subTest(key=key):
                self.assertTrue(herdr_relay.panes.is_safe_key(key))

        for key in ("--help", "ctrl+;", "arbitrary"):
            with self.subTest(key=key):
                self.assertFalse(herdr_relay.panes.is_safe_key(key))

    def test_read_pane_line_count_is_coerced_to_a_sane_int(self):
        coerce = herdr_relay.herdr._read_pane_lines
        self.assertEqual(30, coerce(None))
        self.assertEqual(30, coerce("abc"))
        self.assertEqual(30, coerce(""))
        self.assertEqual(30, coerce({"lines": 5}))
        self.assertEqual(30, coerce(0))
        self.assertEqual(30, coerce(-5))
        self.assertEqual(50, coerce(50))
        self.assertEqual(50, coerce(" 50 "))
        self.assertEqual(2000, coerce(10 ** 9))

    @patch.object(herdr_relay.herdr, "run_herdr", return_value="")
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
            patch.object(herdr_relay.state, "known_panes", {"pane-1"}),
            patch.object(herdr_relay.state, "known_pane_keys", {("local", "pane-1")}),
            patch.dict(herdr_relay.state.pane_hosts, {"pane-1": {"local"}}, clear=True),
            patch.dict(herdr_relay.state.pane_dialogs, {}, clear=True),
            patch.object(herdr_relay.transcripts.blocks, "pane_blocks", return_value=(None, None)),
        ):
            asyncio.run(herdr_relay.handle_client(socket))

        # "abc" would make herdr print an error on stdout and exit 0, which the
        # relay would then serve to the client as terminal content.
        run_herdr.assert_called_once_with(
            "pane", "read", "pane-1", "--lines", "30", "--source", "visible",
            remote=None, host_id="local", command=[herdr_relay.config.HERDR]
        )
        self.assertEqual(
            ["pane_content"], [frame["type"] for frame in after_handshake(socket.sent)]
        )

    def test_read_pane_rejects_invalid_source_before_running_herdr(self):
        class Socket:
            def __init__(self):
                self.requests = iter([
                    json.dumps({
                        "type": "read_pane",
                        "pane_id": "pane-1",
                        "source": "all",
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

        socket = Socket()
        with (
            patch.object(herdr_relay.state, "known_panes", {"pane-1"}),
            patch.object(herdr_relay.state, "known_pane_keys", {("local", "pane-1")}),
            patch.dict(herdr_relay.state.pane_hosts, {"pane-1": {"local"}}, clear=True),
            patch.object(herdr_relay.herdr, "run_herdr") as run_herdr,
        ):
            asyncio.run(herdr_relay.handle_client(socket))

        frames = after_handshake(socket.sent)
        self.assertEqual(["error"], [frame["type"] for frame in frames])
        self.assertEqual("invalid pane source", frames[0]["message"])
        run_herdr.assert_not_called()

    @patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, ""))
    def test_detected_dynamic_response_uses_its_safe_key_mapping(self, run_herdr_checked):
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
            patch.object(herdr_relay.state, "known_panes", {"pane-1"}),
            patch.object(herdr_relay.state, "known_pane_keys", {("local", "pane-1")}),
            patch.dict(herdr_relay.state.pane_hosts, {"pane-1": {"local"}}, clear=True),
            patch.dict(
                herdr_relay.state.pane_response_options,
                {("local", "pane-1"): {"allow once", "allow always", "reject"}},
                clear=True,
            ),
        ):
            socket = Socket()
            asyncio.run(herdr_relay.handle_client(socket))

        self.assertEqual(after_handshake(socket.sent), [])
        run_herdr_checked.assert_called_once_with(
            "pane", "send-keys", "pane-1", "Enter", remote=None, host_id="local",
            command=[herdr_relay.config.HERDR]
        )


class RequestCacheTests(unittest.TestCase):
    def test_request_id_reuse_across_host_pane_fingerprints_is_rejected(self):
        class Socket:
            request_headers = {}

            def __init__(self):
                self.requests = iter([
                    json.dumps({
                        "type": "send_prompt", "request_id": "same-request",
                        "host_id": "alpha", "pane_id": "pane-a", "text": "first",
                    }),
                    json.dumps({
                        "type": "send_prompt", "request_id": "same-request",
                        "host_id": "beta", "pane_id": "pane-b", "text": "second",
                    }),
                ])
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

        records = [host_record("alpha", "shared-target"), host_record("beta", "shared-target")]
        socket = Socket()
        with (
            patch.object(herdr_relay.state, "known_panes", {"pane-a", "pane-b"}),
            patch.object(
                herdr_relay.state, "known_pane_keys", {("alpha", "pane-a"), ("beta", "pane-b")}
            ),
            patch.dict(
                herdr_relay.state.pane_hosts,
                {"pane-a": {"alpha"}, "pane-b": {"beta"}},
                clear=True,
            ),
            patch.dict(
                herdr_relay.state.pane_remote_map,
                {("alpha", "pane-a"): "shared-target", ("beta", "pane-b"): "shared-target"},
                clear=True,
            ),
            patch.object(herdr_relay.herdr, "configured_host_records", return_value=records),
            patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, "")) as checked,
        ):
            asyncio.run(herdr_relay.handle_client(socket))

        frames = after_handshake(socket.sent)
        self.assertEqual("command_ack", frames[0]["type"])
        self.assertEqual("REQUEST_ID_REUSED", frames[1]["code"])
        checked.assert_called_once()

    def test_request_replay_cache_is_scoped_to_each_connection(self):
        class Socket:
            request_headers = {}

            def __init__(self):
                self.requests = iter([
                    json.dumps({"type": "project_list", "request_id": "same-request", "query": ""})
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

        responses = iter([
            {"type": "command_ack", "request_id": "same-request", "result": {"ordinal": 1}},
            {"type": "command_ack", "request_id": "same-request", "result": {"ordinal": 2}},
        ])
        first = Socket()
        second = Socket()
        with patch.object(herdr_relay.projects, "handle_command", side_effect=lambda _msg: next(responses)) as command:
            asyncio.run(herdr_relay.handle_client(first))
            asyncio.run(herdr_relay.handle_client(second))

        self.assertEqual(2, command.call_count)
        self.assertEqual(1, first.sent[-1]["result"]["ordinal"])
        self.assertEqual(2, second.sent[-1]["result"]["ordinal"])


class SendPromptTests(unittest.TestCase):
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

    def drive(self, messages, checked_return=(True, ""), configured_hosts=None):
        socket = self.socket(messages)
        with (
            patch.object(herdr_relay.state, "known_panes", {"pane-1"}),
            patch.object(herdr_relay.state, "known_pane_keys", {("local", "pane-1")}),
            patch.dict(herdr_relay.state.pane_hosts, {"pane-1": {"local"}}, clear=True),
            patch.dict(herdr_relay.state.pane_remote_map, {}, clear=True),
            patch.object(
                herdr_relay.herdr,
                "configured_host_records",
                return_value=configured_hosts or [host_record("local")],
            ),
            patch.object(
                herdr_relay.herdr,
                "run_herdr_checked",
                return_value=checked_return,
            ) as checked,
        ):
            asyncio.run(herdr_relay.handle_client(socket))
        return after_handshake(socket.sent), checked

    def test_success_is_one_checked_agent_prompt_and_acknowledged(self):
        frames, checked = self.drive([{
            "type": "send_prompt",
            "request_id": "prompt-1",
            "pane_id": "pane-1",
            "text": "run tests",
        }])

        self.assertEqual([{
            "type": "command_ack",
            "request_id": "prompt-1",
            "result": {"pane_id": "pane-1", "host_id": "local"},
        }], frames)
        checked.assert_called_once_with(
            "agent", "prompt", "pane-1", "run tests", remote=None, host_id="local",
            command=[herdr_relay.config.HERDR]
        )

    def test_retry_replays_ack_without_submitting_again(self):
        messages = [{
            "type": "send_prompt", "request_id": "prompt-1",
            "pane_id": "pane-1", "text": "run tests",
        }] * 2
        frames, checked = self.drive(messages)

        self.assertEqual(frames[0], frames[1])
        checked.assert_called_once()

    def test_invalid_request_id_is_typed_and_not_submitted(self):
        frames, checked = self.drive([{
            "type": "send_prompt", "pane_id": "pane-1", "text": "run tests",
        }])

        self.assertEqual({
            "type": "command_error", "request_id": None,
            "code": "INVALID_REQUEST", "message": "request_id is required",
        }, frames[0])
        checked.assert_not_called()

    def test_unknown_host_and_pane_are_typed(self):
        host_frames, checked = self.drive([{
            "type": "send_prompt", "request_id": "prompt-host",
            "host_id": "missing", "pane_id": "pane-1", "text": "run tests",
        }])
        self.assertEqual("UNKNOWN_HOST", host_frames[0]["code"])
        self.assertEqual("prompt-host", host_frames[0]["request_id"])
        checked.assert_not_called()

        pane_frames, checked = self.drive([{
            "type": "send_prompt", "request_id": "prompt-pane",
            "pane_id": "missing", "text": "run tests",
        }])
        self.assertEqual("UNKNOWN_PANE", pane_frames[0]["code"])
        self.assertEqual("prompt-pane", pane_frames[0]["request_id"])
        checked.assert_not_called()

    def test_herdr_failure_is_typed_and_replayed(self):
        messages = [{
            "type": "send_prompt", "request_id": "prompt-fail",
            "pane_id": "pane-1", "text": "run tests",
        }] * 2
        frames, checked = self.drive(messages, checked_return=(False, "host error"))

        self.assertEqual(frames[0], frames[1])
        self.assertEqual("HERDR_FAILED", frames[0]["code"])
        self.assertEqual("prompt-fail", frames[0]["request_id"])
        checked.assert_called_once()

    def test_text_validation_matches_legacy_send_text_limit(self):
        frames, checked = self.drive([{
            "type": "send_prompt", "request_id": "prompt-empty",
            "pane_id": "pane-1", "text": "",
        }])
        self.assertEqual("INVALID_REQUEST", frames[0]["code"])
        checked.assert_not_called()


class DialogResponseTests(unittest.TestCase):
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

    def test_dialog_frame_is_stable_and_capability_is_truthful(self):
        with (
            patch.dict(herdr_relay.state.pane_dialogs, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialog_revisions, {}, clear=True),
        ):
            first = herdr_relay.dialogs.ensure(
                "pane-1", "Choose", ["yes", "no"]
            )
            second = herdr_relay.dialogs.ensure(
                "pane-1", "Choose", ["yes", "no"], agent="claude", project="repo"
            )
            self.assertEqual(first["dialog_id"], second["dialog_id"])
            self.assertEqual(first["revision"], second["revision"])
            self.assertEqual("claude", second["agent"])
            self.assertEqual("repo", second["project"])
            frame = herdr_relay.dialogs.frame(first)

        self.assertEqual(["yes", "no"], frame["choices"])
        self.assertFalse(frame["raw_input_allowed"])

    def test_undetected_dialog_has_no_typed_choices(self):
        with (
            patch.dict(herdr_relay.state.pane_dialogs, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialog_revisions, {}, clear=True),
        ):
            dialog = herdr_relay.dialogs.ensure("pane-1", "Waiting", None)
            frame = herdr_relay.dialogs.frame(dialog)

        self.assertEqual([], frame["choices"])
        self.assertEqual(herdr_relay.panes.TOOL_OPTIONS, frame["options"])
        self.assertEqual(
            {choice.lower() for choice in herdr_relay.panes.TOOL_OPTIONS},
            herdr_relay.state.pane_response_options[("local", "pane-1")],
        )

    def test_missing_observation_revision_does_not_recreate_consumed_dialog(self):
        with (
            patch.dict(herdr_relay.state.pane_dialogs, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialog_revisions, {}, clear=True),
        ):
            first = herdr_relay.dialogs.ensure("pane-1", "Choose", ["yes", "no"], observation=7)
            first["consumed"] = True
            missing = herdr_relay.dialogs.ensure("pane-1", "Choose", ["yes", "no"])
            same = herdr_relay.dialogs.ensure("pane-1", "Choose", ["yes", "no"], observation=7)
            changed = herdr_relay.dialogs.ensure("pane-1", "Choose", ["yes", "no"], observation=8)

        self.assertIs(first, missing)
        self.assertIs(first, same)
        self.assertFalse(changed["consumed"])
        self.assertEqual(2, changed["revision"])

    def test_unanswered_dialog_keeps_identity_across_changed_observation(self):
        with (
            patch.dict(herdr_relay.state.pane_dialogs, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialog_revisions, {}, clear=True),
        ):
            first = herdr_relay.dialogs.ensure("pane-1", "Choose", ["yes", "no"], observation=7)
            updated = herdr_relay.dialogs.ensure("pane-1", "Choose", ["yes", "no"], observation=8)
            updated["consumed"] = True
            renewed = herdr_relay.dialogs.ensure("pane-1", "Choose", ["yes", "no"], observation=9)

        self.assertIs(first, updated)
        self.assertEqual(first["dialog_id"], updated["dialog_id"])
        self.assertEqual(8, updated["observation"])
        self.assertNotEqual(updated["dialog_id"], renewed["dialog_id"])
        self.assertEqual(2, renewed["revision"])

    def test_response_is_one_winner_and_same_request_replays(self):
        with (
            patch.object(herdr_relay.state, "known_panes", {"pane-1"}),
            patch.object(herdr_relay.state, "known_pane_keys", {("local", "pane-1")}),
            patch.dict(herdr_relay.state.pane_hosts, {"pane-1": {"local"}}, clear=True),
            patch.dict(herdr_relay.state.pane_remote_map, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialogs, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialog_revisions, {}, clear=True),
            patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, "")) as checked,
        ):
            dialog = herdr_relay.dialogs.ensure("pane-1", "Choose", ["yes", "no"])
            messages = [{
                "type": "respond_dialog", "request_id": "answer-1", "pane_id": "pane-1",
                "dialog_id": dialog["dialog_id"], "revision": dialog["revision"], "text": "yes",
            }] * 2 + [{
                "type": "respond_dialog", "request_id": "answer-2", "pane_id": "pane-1",
                "dialog_id": dialog["dialog_id"], "revision": dialog["revision"], "text": "yes",
            }]
            socket = self.socket(messages)
            asyncio.run(herdr_relay.handle_client(socket))

        frames = after_handshake(socket.sent)
        self.assertEqual(frames[0], frames[1])
        self.assertEqual("command_ack", frames[0]["type"])
        self.assertEqual("DIALOG_ALREADY_ANSWERED", frames[2]["code"])
        checked.assert_called_once_with(
            "pane", "send-text", "pane-1", "y\n", remote=None, host_id="local",
            command=[herdr_relay.config.HERDR]
        )

    def test_concurrent_sockets_have_one_dialog_winner(self):
        started = threading.Event()
        release = threading.Event()

        def checked(*_args, **_kwargs):
            started.set()
            release.wait(timeout=1)
            return True, ""

        async def run(socket_one, socket_two):
            first = asyncio.create_task(herdr_relay.handle_client(socket_one))
            await asyncio.to_thread(started.wait, 1)
            second = asyncio.create_task(herdr_relay.handle_client(socket_two))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)

        with (
            patch.object(herdr_relay.state, "known_panes", {"pane-1"}),
            patch.object(herdr_relay.state, "known_pane_keys", {("local", "pane-1")}),
            patch.dict(herdr_relay.state.pane_hosts, {"pane-1": {"local"}}, clear=True),
            patch.dict(herdr_relay.state.last_statuses, {}, clear=True),
            patch.dict(herdr_relay.state.pane_remote_map, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialogs, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialog_revisions, {}, clear=True),
            patch.object(herdr_relay.herdr, "run_herdr_checked", side_effect=checked) as submit,
        ):
            dialog = herdr_relay.dialogs.ensure("pane-1", "Choose", ["yes", "no"])
            message = {
                "type": "respond_dialog", "pane_id": "pane-1",
                "dialog_id": dialog["dialog_id"], "revision": dialog["revision"], "text": "yes",
            }
            socket_one = self.socket([{**message, "request_id": "answer-one"}])
            socket_two = self.socket([{**message, "request_id": "answer-two"}])
            asyncio.run(run(socket_one, socket_two))

        responses = [after_handshake(socket_one.sent)[0], after_handshake(socket_two.sent)[0]]
        self.assertCountEqual(["command_ack", "command_error"], [r["type"] for r in responses])
        self.assertIn("DIALOG_ALREADY_ANSWERED", [r.get("code") for r in responses])
        submit.assert_called_once()

    def test_herdr_failure_is_retryable_for_same_request(self):
        with (
            patch.object(herdr_relay.state, "known_panes", {"pane-1"}),
            patch.object(herdr_relay.state, "known_pane_keys", {("local", "pane-1")}),
            patch.dict(herdr_relay.state.pane_hosts, {"pane-1": {"local"}}, clear=True),
            patch.dict(herdr_relay.state.last_statuses, {}, clear=True),
            patch.dict(herdr_relay.state.pane_remote_map, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialogs, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialog_revisions, {}, clear=True),
            patch.object(
                herdr_relay.herdr,
                "run_herdr_checked",
                side_effect=[(False, "temporary"), (True, "" )],
            ) as checked,
        ):
            dialog = herdr_relay.dialogs.ensure("pane-1", "Choose", ["yes", "no"])
            socket = self.socket([{
                "type": "respond_dialog", "request_id": "answer-retry", "pane_id": "pane-1",
                "dialog_id": dialog["dialog_id"], "revision": dialog["revision"], "text": "yes",
            }] * 2)
            asyncio.run(herdr_relay.handle_client(socket))

        frames = after_handshake(socket.sent)
        self.assertEqual("HERDR_FAILED", frames[0]["code"])
        self.assertEqual("command_ack", frames[1]["type"])
        self.assertEqual(2, checked.call_count)

    def test_legacy_response_consumes_dialog_for_typed_client(self):
        with (
            patch.object(herdr_relay.state, "known_panes", {"pane-1"}),
            patch.object(herdr_relay.state, "known_pane_keys", {("local", "pane-1")}),
            patch.dict(herdr_relay.state.pane_hosts, {"pane-1": {"local"}}, clear=True),
            patch.dict(herdr_relay.state.last_statuses, {"pane-1": "blocked"}, clear=True),
            patch.dict(herdr_relay.state.pane_remote_map, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialogs, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialog_revisions, {}, clear=True),
            patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, "")) as checked,
        ):
            dialog = herdr_relay.dialogs.ensure("pane-1", "Choose", ["yes", "no"])
            legacy = {
                "type": "respond", "pane_id": "pane-1", "text": "yes",
            }
            typed = {
                "type": "respond_dialog", "request_id": "typed-after-legacy",
                "pane_id": "pane-1", "dialog_id": dialog["dialog_id"],
                "revision": dialog["revision"], "text": "yes",
            }
            socket = self.socket([legacy, typed])
            asyncio.run(herdr_relay.handle_client(socket))

        frames = after_handshake(socket.sent)
        self.assertEqual(["DIALOG_ALREADY_ANSWERED"], [frame["code"] for frame in frames])
        self.assertTrue(dialog["consumed"])
        checked.assert_called_once_with(
            "pane", "send-text", "pane-1", "y\n", remote=None, host_id="local",
            command=[herdr_relay.config.HERDR]
        )

    def test_failed_legacy_response_leaves_dialog_retryable(self):
        with (
            patch.object(herdr_relay.state, "known_panes", {"pane-1"}),
            patch.object(herdr_relay.state, "known_pane_keys", {("local", "pane-1")}),
            patch.dict(herdr_relay.state.pane_hosts, {"pane-1": {"local"}}, clear=True),
            patch.dict(herdr_relay.state.last_statuses, {("local", "pane-1"): "blocked"}, clear=True),
            patch.dict(herdr_relay.state.pane_remote_map, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialogs, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialog_revisions, {}, clear=True),
            patch.object(
                herdr_relay.herdr,
                "run_herdr_checked",
                side_effect=[(False, "temporary"), (True, "")],
            ) as checked,
        ):
            dialog = herdr_relay.dialogs.ensure("pane-1", "Choose", ["yes", "no"])
            legacy = {
                "type": "respond", "pane_id": "pane-1", "text": "yes",
            }
            typed = {
                "type": "respond_dialog", "request_id": "typed-after-failure",
                "pane_id": "pane-1", "dialog_id": dialog["dialog_id"],
                "revision": dialog["revision"], "text": "yes",
            }
            socket = self.socket([legacy, typed])
            asyncio.run(herdr_relay.handle_client(socket))

        frames = after_handshake(socket.sent)
        self.assertEqual("command_ack", frames[0]["type"])
        self.assertTrue(dialog["consumed"])
        self.assertEqual(2, checked.call_count)

    def test_push_fast_dialog_can_be_answered_before_poll_catches_up(self):
        async def run():
            with (
                patch.object(herdr_relay.herdr, "read_pane", return_value="Choose\n1. Yes\n2. No"),
                patch.object(herdr_relay.transport, "broadcast", new_callable=AsyncMock),
                patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, "")) as checked,
            ):
                await herdr_relay.transport._handle_pushed_event({
                    "type": "agent_event", "pane_id": "pane-1", "status": "blocked",
                    "host_id": "buildbox", "host": "buildbox", "agent": "claude", "project": "repo",
                    "prompt": "Choose\n1. Yes\n2. No",
                })
                dialog = herdr_relay.state.pane_dialogs[("buildbox", "pane-1")]
                socket = self.socket([{
                    "type": "respond_dialog", "request_id": "push-answer",
                    "pane_id": "pane-1", "dialog_id": dialog["dialog_id"],
                    "revision": dialog["revision"], "text": "1. Yes",
                }])
                await herdr_relay.handle_client(socket)
                return socket, checked

        with (
            patch.object(herdr_relay.state, "known_panes", {"pane-1"}),
            patch.object(herdr_relay.state, "known_pane_keys", {("buildbox", "pane-1")}),
            patch.dict(herdr_relay.state.pane_hosts, {"pane-1": {"buildbox"}}, clear=True),
            patch.dict(herdr_relay.state.last_statuses, {("buildbox", "pane-1"): "working"}, clear=True),
            patch.dict(herdr_relay.state.pane_remote_map, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialogs, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialog_revisions, {}, clear=True),
            patch.dict(herdr_relay.state.pane_host_map, {}, clear=True),
            patch.dict(herdr_relay.state.pane_project_map, {}, clear=True),
        ):
            socket, checked = asyncio.run(run())

        frame = after_handshake(socket.sent)[0]
        self.assertEqual("command_ack", frame["type"])
        checked.assert_called_once()

    def test_push_fast_dialog_survives_read_before_poll_catches_up(self):
        class Socket:
            request_headers = {}

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

        async def run():
            with (
                patch.object(herdr_relay.herdr, "read_pane", return_value="Choose\n1. Yes\n2. No"),
                patch.object(herdr_relay.transport, "broadcast", new_callable=AsyncMock),
                patch.object(herdr_relay.transcripts.blocks, "pane_blocks", return_value=(None, None)),
            ):
                await herdr_relay.transport._handle_pushed_event({
                    "type": "agent_event", "pane_id": "pane-1", "status": "blocked",
                    "host_id": "buildbox", "host": "buildbox", "agent": "claude", "project": "repo",
                })
                socket = Socket()
                await herdr_relay.handle_client(socket)
                return socket

        with (
            patch.object(herdr_relay.state, "known_panes", {"pane-1"}),
            patch.object(herdr_relay.state, "known_pane_keys", {("buildbox", "pane-1")}),
            patch.dict(herdr_relay.state.pane_hosts, {"pane-1": {"buildbox"}}, clear=True),
            patch.dict(herdr_relay.state.last_statuses, {("buildbox", "pane-1"): "working"}, clear=True),
            patch.dict(herdr_relay.state.pane_remote_map, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialogs, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialog_revisions, {}, clear=True),
            patch.dict(herdr_relay.state.pane_host_map, {}, clear=True),
            patch.dict(herdr_relay.state.pane_project_map, {}, clear=True),
        ):
            socket = asyncio.run(run())

        frames = after_handshake(socket.sent)
        self.assertEqual(["pane_content", "blocked"], [frame["type"] for frame in frames])
        self.assertEqual("buildbox", frames[1]["host"])

    def test_changed_prompt_rejects_old_dialog(self):
        with (
            patch.object(herdr_relay.state, "known_panes", {"pane-1"}),
            patch.object(herdr_relay.state, "known_pane_keys", {("local", "pane-1")}),
            patch.dict(herdr_relay.state.pane_hosts, {"pane-1": {"local"}}, clear=True),
            patch.dict(herdr_relay.state.pane_remote_map, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialogs, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialog_revisions, {}, clear=True),
            patch.object(herdr_relay.herdr, "run_herdr_checked") as checked,
        ):
            old = herdr_relay.dialogs.ensure("pane-1", "Choose", ["yes", "no"])
            current = herdr_relay.dialogs.ensure("pane-1", "Choose again", ["yes", "no"])
            socket = self.socket([{
                "type": "respond_dialog", "request_id": "answer-stale", "pane_id": "pane-1",
                "dialog_id": old["dialog_id"], "revision": old["revision"], "text": "yes",
            }])
            asyncio.run(herdr_relay.handle_client(socket))

        frame = after_handshake(socket.sent)[0]
        self.assertEqual("STALE_DIALOG", frame["code"])
        self.assertNotEqual(old["dialog_id"], current["dialog_id"])
        checked.assert_not_called()

    def test_clear_removes_actionable_dialog(self):
        with (
            patch.dict(herdr_relay.state.pane_dialogs, {}, clear=True),
            patch.dict(herdr_relay.state.pane_response_options, {}, clear=True),
            patch.dict(herdr_relay.state.pane_dialog_revisions, {}, clear=True),
        ):
            herdr_relay.dialogs.ensure("pane-1", "Choose", ["yes", "no"])
            herdr_relay.dialogs.clear("pane-1")
            self.assertNotIn("pane-1", herdr_relay.state.pane_dialogs)
            self.assertNotIn("pane-1", herdr_relay.state.pane_response_options)


class HandshakeTests(unittest.TestCase):
    def test_server_info_precedes_anything_the_client_sends(self):
        """An update-required screen is useless if it arrives after the agent list.

        Ordering is the whole feature, so assert it directly: record how many
        frames had gone out by the time the relay first tried to read from the
        socket. Asserting only on sent[0] would still pass if the frame were
        moved after the read loop.
        """
        sent = []
        frames_before_first_read = []

        class FakeWS:
            remote_address = ("203.0.113.7", 54321)
            request = type("Request", (), {"headers": {"User-Agent": "okhttp/4.12 herdr-mobile"}})()

            async def send(self, raw):
                sent.append(json.loads(raw))

            def __aiter__(self):
                return self

            async def __anext__(self):
                frames_before_first_read.append(len(sent))
                raise StopAsyncIteration

        asyncio.run(herdr_relay.handle_client(FakeWS()))

        self.assertEqual([1], frames_before_first_read)
        self.assertEqual("server_info", sent[0]["type"])
        self.assertEqual(herdr_relay.config.MIN_CLIENT, sent[0]["min_client"])
        self.assertEqual(herdr_relay.config.RELAY_VERSION, sent[0]["relay_version"])

    def test_a_socket_is_not_broadcast_to_until_its_handshake_is_written(self):
        """`broadcast` fans out over `clients`, so membership means reachable.

        A socket registered before its `server_info` is written is one an
        `agents` frame could reach first — the single thing this frame exists to
        prevent. Checked from inside `send`, because the moment the frame is
        going out is the only point at which the window is observable.
        """
        registered_during_handshake = []

        class FakeWS:
            remote_address = ("203.0.113.9", 54323)
            request = type("Request", (), {"headers": {}})()

            async def send(inner, raw):
                # `inner`, not `self`: the enclosing test's `self` stays reachable.
                if json.loads(raw)["type"] == "server_info":
                    registered_during_handshake.append(inner in herdr_relay.state.clients)

            def __aiter__(inner):
                return inner

            async def __anext__(inner):
                raise StopAsyncIteration

        asyncio.run(herdr_relay.handle_client(FakeWS()))
        self.assertEqual([False], registered_during_handshake)

    def test_a_client_that_vanishes_mid_handshake_is_unregistered(self):
        """The handshake send is inside the try, and the cleanup discards.

        So a socket that dies before it was ever registered cleans up without a
        KeyError, and one registered after the handshake is still removed.
        """

        class DeadWS:
            remote_address = ("203.0.113.8", 54322)
            request = type("Request", (), {"headers": {}})()

            async def send(self, raw):
                raise herdr_relay.server.ConnectionClosedOK(None, None)

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        dead = DeadWS()
        asyncio.run(herdr_relay.handle_client(dead))
        self.assertNotIn(dead, herdr_relay.state.clients)


class AuthTokenTests(unittest.TestCase):
    def test_require_auth_token_rejects_empty_token(self):
        with patch.object(herdr_relay.config, "AUTH_TOKEN", ""):
            with self.assertRaises(SystemExit):
                herdr_relay.require_auth_token()

    def test_require_auth_token_accepts_configured_token(self):
        with patch.object(herdr_relay.config, "AUTH_TOKEN", "token"):
            self.assertIsNone(herdr_relay.require_auth_token())

    def test_process_request_rejects_wrong_token(self):
        class Headers:
            def raw_items(self):
                return [("Authorization", "Bearer wrong")]

        class Request:
            headers = Headers()
            path = "/"

        with patch.object(herdr_relay.config, "AUTH_TOKEN", "token"):
            response = asyncio.run(herdr_relay.process_request(None, Request()))

        self.assertEqual(response.status_code, 401)

    def test_process_request_rejects_non_ascii_token(self):
        class Headers:
            def raw_items(self):
                return [("Authorization", "Bearer пароль")]

        class Request:
            headers = Headers()
            path = "/"

        with patch.object(herdr_relay.config, "AUTH_TOKEN", "token"):
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

        with patch.object(herdr_relay.config, "AUTH_TOKEN", "token"):
            response = asyncio.run(herdr_relay.process_request(None, Request()))

        self.assertIsNone(response)


class EventPushRouteTests(unittest.TestCase):
    """The plugin hook's route — a ?d= query on an ordinary authenticated GET."""

    @staticmethod
    def _request(path):
        class Headers:
            def raw_items(self):
                return [("Authorization", "Bearer token")]

        class Request:
            headers = Headers()

        Request.path = path
        return Request()

    def _push(self, path):
        queue = asyncio.Queue()
        with patch.object(herdr_relay.config, "AUTH_TOKEN", "token"), \
                patch.object(herdr_relay.state, "event_queue", queue):
            response = asyncio.run(herdr_relay.process_request(None, self._request(path)))
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        return response, events

    def _query(self, event):
        return "?" + urllib.parse.urlencode({"d": json.dumps(event)})

    def test_event_is_queued_on_the_hooks_default_path(self):
        event = {"type": "agent_event", "pane_id": "%1", "status": "blocked"}
        response, events = self._push("/event" + self._query(event))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(events, [event])

    def test_event_on_root_beats_the_static_pwa_route(self):
        # Regression: "/" matched the LEGACY (#14) index.html route first, so the
        # relay answered 200 with the PWA and dropped the event silently.
        event = {"type": "agent_event", "pane_id": "%2", "status": "idle"}
        response, events = self._push("/" + self._query(event))

        self.assertEqual(events, [event])
        self.assertEqual(response.body, b"ok\n")

    def test_pane_id_that_looks_like_an_escape_survives(self):
        # tmux numbers panes "%N", so pane 22 is "%22" — decoding the query value
        # a second time turned that into a bare quote and the JSON failed to
        # parse. %20 was worse: it became a space and the event was accepted with
        # a corrupted id.
        event = {"type": "agent_event", "pane_id": "%22", "status": "blocked"}
        _, events = self._push("/event" + self._query(event))
        self.assertEqual(events, [event])

        event = {"type": "agent_event", "pane_id": "%20", "status": "idle"}
        _, events = self._push("/event" + self._query(event))
        self.assertEqual(events, [event])

    def test_malformed_event_answers_200_and_queues_nothing(self):
        # The hook must not retry or block a status change on our parse failure.
        response, events = self._push("/event?d=not-json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(events, [])

    def test_request_without_an_event_query_still_serves_static_routes(self):
        response, events = self._push("/api/vapid-public-key")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(events, [])
        self.assertIn(b"publicKey", response.body)


if __name__ == "__main__":
    unittest.main()
