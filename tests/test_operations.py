import asyncio
import queue
import os
import json
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from relay import herdr_relay


class StartOperationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Path(self.directory.name) / "projects.sqlite3"
        self.root = Path(self.directory.name) / "projects"
        self.root.mkdir()
        self.host = {
            "id": "workstation",
            "display_name": "Workstation",
            "ssh": {},
            "project_roots": [os.path.realpath(self.root)],
            "herdr": {"wrapper": []},
            "harnesses": [],
            "power": {"wake": None, "shutdown": False},
            "readiness_timeout_seconds": 1,
        }
        self.hosts_patch = patch.object(herdr_relay.hosts, "HOSTS_BY_ID", {"workstation": self.host})
        self.db_patch = patch.object(herdr_relay.config, "PROJECTS_DB", str(self.db))
        self.hosts_patch.start()
        self.db_patch.start()
        root_id = herdr_relay.hosts.project_roots(self.host)[0]["id"]
        self.project = herdr_relay.projects.ProjectStore(str(self.db)).save(
            "workstation", root_id, os.path.realpath(self.root), "Herdr relay"
        )

    def tearDown(self):
        self.db_patch.stop()
        self.hosts_patch.stop()
        self.directory.cleanup()

    def operation_message(self, request_id="start-1"):
        return {
            "type": "start_session",
            "request_id": request_id,
            "project_id": self.project["project_id"],
            "host_id": "workstation",
            "harness": "claude",
            "model": "default",
        }

    def begin_operation(self, request_id="start-1"):
        operation, created = herdr_relay.operations.OperationStore(str(self.db)).begin(
            request_id,
            "workstation",
            self.project["project_id"],
            "claude",
            "default",
        )
        self.assertTrue(created)
        return operation

    def test_repeated_request_id_returns_one_persisted_operation_and_name(self):
        with patch.object(herdr_relay.operations, "ensure_worker") as ensure_worker:
            first = herdr_relay.lifecycle.start_session(self.operation_message())
            replay = herdr_relay.lifecycle.start_session(self.operation_message())

        self.assertEqual("command_ack", first["type"])
        self.assertEqual(first["result"], replay["result"])
        self.assertEqual(first["result"]["operation"]["operation_id"], replay["result"]["operation"]["operation_id"])
        self.assertEqual(
            herdr_relay.operations.deterministic_agent_name(first["result"]["operation"]["operation_id"]),
            first["result"]["operation"]["agent_name"],
        )
        self.assertEqual(2, ensure_worker.call_count)
        self.assertEqual(1, len(herdr_relay.operations.OperationStore(str(self.db)).active()))

    def test_operation_revision_increments_for_each_transition(self):
        store = herdr_relay.operations.OperationStore(str(self.db))
        operation = self.begin_operation()

        checking = store.update(operation["operation_id"], "checking_herdr")
        failed = store.update(
            operation["operation_id"],
            "failed",
            error_code="READY_TIMEOUT",
            error_message="Host did not become ready before the timeout",
        )

        self.assertEqual(0, operation["revision"])
        self.assertEqual(1, checking["revision"])
        self.assertEqual(2, failed["revision"])

    def test_recovery_snapshot_includes_recent_terminal_operations(self):
        operation = self.begin_operation()
        store = herdr_relay.operations.OperationStore(str(self.db))
        store.update(
            operation["operation_id"],
            "failed",
            error_code="READY_TIMEOUT",
            error_message="Host did not become ready before the timeout",
        )

        recovered = herdr_relay.operations.public_recovery()

        self.assertEqual(["failed"], [item["stage"] for item in recovered])

    def test_retry_attempt_is_idempotent_and_traces_its_source(self):
        source = self.begin_operation()
        store = herdr_relay.operations.OperationStore(str(self.db))
        store.update(
            source["operation_id"],
            "failed",
            error_code="READY_TIMEOUT",
            error_message="Host did not become ready before the timeout",
        )
        retry = self.operation_message("retry-1")
        retry.pop("project_id")
        retry.pop("host_id")
        retry.pop("harness")
        retry.pop("model")
        retry.update({"retry_of_operation_id": source["operation_id"], "attempt": 2})

        with patch.object(herdr_relay.operations, "ensure_worker") as ensure_worker:
            first = herdr_relay.lifecycle.start_session(retry)
            duplicate = herdr_relay.lifecycle.start_session({**retry, "request_id": "retry-2"})

        first_operation = first["result"]["operation"]
        duplicate_operation = duplicate["result"]["operation"]
        self.assertEqual(first_operation, duplicate_operation)
        self.assertNotEqual(source["operation_id"], first_operation["operation_id"])
        self.assertEqual(source["operation_id"], first_operation["retry_of_operation_id"])
        self.assertEqual(2, first_operation["attempt"])
        self.assertEqual(2, ensure_worker.call_count)

    def test_restart_reconciles_existing_named_pane_without_starting_another(self):
        operation = self.begin_operation()
        events = queue.Queue()
        ready = {"ssh_reachable": True, "herdr_ready": True}
        with (
            patch.object(herdr_relay.state, "event_queue", events),
            patch.object(
                herdr_relay.herdr,
                "get_agent_by_name",
                return_value=([{"pane_id": "pane-7", "agent_name": operation["agent_name"]}], ready),
            ),
            patch.object(herdr_relay.herdr, "run_herdr_checked") as run,
        ):
            herdr_relay.operations._start_operation(operation["operation_id"])

        result = herdr_relay.operations.OperationStore(str(self.db)).get(operation["operation_id"])
        self.assertEqual("started", result["stage"])
        self.assertEqual("legacy:workstation:pane-7", result["session_id"])
        run.assert_not_called()
        self.assertEqual("checking_herdr", events.get_nowait()["operation"]["stage"])
        self.assertEqual("started", events.get_nowait()["operation"]["stage"])

    def test_ready_host_start_correlates_from_the_launch_reply(self):
        operation = self.begin_operation()
        ready = {"ssh_reachable": True, "herdr_ready": True}
        started = json.dumps({
            "id": "cli:agent:start",
            "result": {
                "type": "agent_started",
                "agent": {"pane_id": "pane-9", "name": operation["agent_name"]},
                "argv": ["claude"],
            },
        })
        with (
            patch.object(herdr_relay.state, "event_queue", queue.Queue()),
            patch.object(herdr_relay.herdr, "get_agent_by_name", return_value=([], ready)) as probe,
            patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, started)) as run,
        ):
            herdr_relay.operations._start_operation(operation["operation_id"])

        result = herdr_relay.operations.OperationStore(str(self.db)).get(operation["operation_id"])
        self.assertEqual("started", result["stage"])
        self.assertEqual("legacy:workstation:pane-9", result["session_id"])
        self.assertEqual(operation["agent_name"], run.call_args.args[2])
        # The reply named the pane; nothing was polled after launch.
        probe.assert_called_once()

    def test_name_taken_launch_reconciles_the_existing_agent(self):
        operation = self.begin_operation()
        ready = {"ssh_reachable": True, "herdr_ready": True}
        taken = json.dumps({
            "id": "cli:agent:start",
            "error": {"code": "agent_name_taken", "message": "agent name is already used"},
        })
        observed = [
            ([], ready),
            ([{"pane_id": "pane-3", "agent_name": operation["agent_name"]}], ready),
        ]
        with (
            patch.object(herdr_relay.state, "event_queue", queue.Queue()),
            patch.object(herdr_relay.herdr, "get_agent_by_name", side_effect=observed),
            patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(False, taken)),
            patch.object(herdr_relay.operations.time, "monotonic", side_effect=[0, 0]),
        ):
            herdr_relay.operations._start_operation(operation["operation_id"])

        result = herdr_relay.operations.OperationStore(str(self.db)).get(operation["operation_id"])
        self.assertEqual("started", result["stage"])
        self.assertEqual("legacy:workstation:pane-3", result["session_id"])
        self.assertIsNone(result["error_code"])

    def test_start_waits_for_the_same_named_pane_before_started(self):
        operation = self.begin_operation()
        ready = {"ssh_reachable": True, "herdr_ready": True}
        observed = [
            ([], ready),
            ([{"pane_id": "pane-8", "agent_name": operation["agent_name"]}], ready),
        ]
        with (
            patch.object(herdr_relay.state, "event_queue", queue.Queue()),
            patch.object(herdr_relay.herdr, "get_agent_by_name", side_effect=observed),
            patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, "")) as run,
            patch.object(herdr_relay.operations.time, "monotonic", side_effect=[0, 0]),
        ):
            herdr_relay.operations._start_operation(operation["operation_id"])

        result = herdr_relay.operations.OperationStore(str(self.db)).get(operation["operation_id"])
        self.assertEqual("started", result["stage"])
        self.assertEqual("legacy:workstation:pane-8", result["session_id"])
        self.assertEqual(operation["agent_name"], run.call_args.args[2])
        self.assertIn("--cwd", run.call_args.args)
        self.assertNotIn("--model", run.call_args.args)

    def test_ready_host_launch_without_exact_name_is_not_a_readiness_timeout(self):
        operation = self.begin_operation()
        ready = {"ssh_reachable": True, "herdr_ready": True}
        with (
            patch.object(herdr_relay.state, "event_queue", queue.Queue()),
            patch.object(herdr_relay.herdr, "get_agent_by_name", return_value=([], ready)),
            patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, "")),
            patch.object(herdr_relay.operations.time, "monotonic", side_effect=[0, 2]),
            self.assertLogs(herdr_relay.config.log, level="WARNING") as logs,
        ):
            herdr_relay.operations._start_operation(operation["operation_id"])

        result = herdr_relay.operations.OperationStore(str(self.db)).get(operation["operation_id"])
        self.assertEqual("failed", result["stage"])
        self.assertEqual("AGENT_NOT_OBSERVABLE", result["error_code"])
        self.assertEqual(
            "Herdr did not expose a recoverable agent identity",
            result["error_message"],
        )
        self.assertIn("failed at starting_agent: AGENT_NOT_OBSERVABLE", logs.output[0])

    def test_offline_wol_start_reports_wake_and_readiness_stages(self):
        self.host["power"] = {"wake": {"mac": "00:11:22:33:44:55"}, "shutdown": False}
        ready = {"ssh_reachable": True, "herdr_ready": True}
        operation = self.begin_operation()
        observed = [
            ([], {"ssh_reachable": False, "herdr_ready": False}),
            ([], ready),
        ]
        started = json.dumps({
            "result": {
                "type": "agent_started",
                "agent": {"pane_id": "pane-wol", "name": operation["agent_name"]},
            },
        })
        events = queue.Queue()
        with (
            patch.object(herdr_relay.state, "event_queue", events),
            patch.object(herdr_relay.operations, "_wake_host", return_value=True) as wake,
            patch.object(herdr_relay.operations, "_probe_existing", side_effect=observed),
            patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, started)) as run,
        ):
            herdr_relay.operations._start_operation(operation["operation_id"])
            result = herdr_relay.operations.OperationStore(str(self.db)).get(operation["operation_id"])

            self.assertEqual("started", result["stage"])
            wake.assert_called_once()
            run.assert_called_once()

        stages = []
        while not events.empty():
            stages.append(events.get_nowait()["operation"]["stage"])
        self.assertEqual(
            ["checking_herdr", "sending_wake", "waiting_for_host", "checking_herdr", "starting_agent", "started"],
            stages,
        )

    def test_offline_wol_does_not_validate_remote_folder_before_wake(self):
        self.host["ssh"] = {"target": "deploy@workstation"}
        self.host["power"] = {"wake": {"mac": "00:11:22:33:44:55"}, "shutdown": False}
        operation = self.begin_operation()
        observed = [
            ([], {"ssh_reachable": False, "herdr_ready": False}),
            ([], {"ssh_reachable": True, "herdr_ready": True}),
        ]
        started = json.dumps({
            "result": {
                "type": "agent_started",
                "agent": {"pane_id": "pane-wol", "name": operation["agent_name"]},
            },
        })
        browses = []

        def browse(*_args, **_kwargs):
            browses.append(True)
            return {"canonical_path": os.path.realpath(self.root), "entries": []}

        def wake(*_args, **_kwargs):
            self.assertEqual([], browses)
            return True

        with (
            patch.object(herdr_relay.state, "event_queue", queue.Queue()),
            patch.object(herdr_relay.operations, "_probe_existing", side_effect=observed),
            patch.object(herdr_relay.operations, "_wake_host", side_effect=wake),
            patch.object(herdr_relay.project_fs, "browse", side_effect=browse),
            patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, started)),
        ):
            herdr_relay.operations._start_operation(operation["operation_id"])

        result = herdr_relay.operations.OperationStore(str(self.db)).get(operation["operation_id"])
        self.assertEqual("started", result["stage"])
        self.assertEqual([True], browses)

    def test_wol_readiness_timeout_is_terminal_and_does_not_launch(self):
        self.host["power"] = {"wake": {"mac": "00:11:22:33:44:55"}, "shutdown": False}
        self.host["readiness_timeout_seconds"] = 1
        operation = self.begin_operation()
        with (
            patch.object(herdr_relay.state, "event_queue", queue.Queue()),
            patch.object(herdr_relay.operations, "_wake_host", return_value=True),
            patch.object(
                herdr_relay.operations,
                "_probe_existing",
                return_value=([], {"ssh_reachable": False, "herdr_ready": False}),
            ),
            patch.object(herdr_relay.operations.time, "monotonic", side_effect=[0, 2]),
            patch.object(herdr_relay.herdr, "run_herdr_checked") as run,
        ):
            herdr_relay.operations._start_operation(operation["operation_id"])

        result = herdr_relay.operations.OperationStore(str(self.db)).get(operation["operation_id"])
        self.assertEqual("failed", result["stage"])
        self.assertEqual("READY_TIMEOUT", result["error_code"])
        run.assert_not_called()

    def test_cancelled_operation_never_wakes_or_launches(self):
        operation = self.begin_operation()
        events = queue.Queue()
        with patch.object(herdr_relay.state, "event_queue", events):
            cancelled = herdr_relay.operations.cancel_start(operation["operation_id"])
            self.assertEqual("cancelled", cancelled["stage"])
            with (
                patch.object(herdr_relay.operations, "_wake_host") as wake,
                patch.object(herdr_relay.operations, "_probe_existing") as probe,
                patch.object(herdr_relay.herdr, "run_herdr_checked") as run,
            ):
                herdr_relay.operations._start_operation(operation["operation_id"])

        wake.assert_not_called()
        probe.assert_not_called()
        run.assert_not_called()

    def test_already_cancelled_process_does_not_spawn(self):
        cancel_event = threading.Event()
        cancel_event.set()
        with patch.object(herdr_relay.herdr.subprocess, "Popen") as popen:
            success, _output = herdr_relay.herdr.run_process_checked(["wakeonlan", "00:11:22:33:44:55"], cancel_event=cancel_event)

        self.assertFalse(success)
        popen.assert_not_called()

    def test_cancel_start_command_returns_the_cancelled_operation(self):
        operation = self.begin_operation()
        with patch.object(herdr_relay.state, "event_queue", queue.Queue()):
            response = herdr_relay.cancel_start({
                "type": "cancel_start",
                "request_id": "cancel-1",
                "operation_id": operation["operation_id"],
            })

        self.assertEqual("command_ack", response["type"])
        self.assertEqual("cancel-1", response["request_id"])
        self.assertEqual("cancelled", response["result"]["operation"]["stage"])

    def test_configuration_removal_becomes_a_stable_sanitized_failure(self):
        operation = self.begin_operation()
        herdr_relay.projects.ProjectStore(str(self.db)).reconcile(set(), set())
        with (
            patch.object(herdr_relay.state, "event_queue", queue.Queue()),
            patch.object(herdr_relay.herdr, "get_agent_by_name") as probe,
        ):
            herdr_relay.operations._start_operation(operation["operation_id"])

        result = herdr_relay.operations.OperationStore(str(self.db)).get(operation["operation_id"])
        self.assertEqual("CONFIGURATION_CHANGED", result["error_code"])
        self.assertEqual(
            "The selected project configuration is no longer available",
            result["error_message"],
        )
        probe.assert_not_called()

    def test_a_project_path_outside_its_root_never_launches(self):
        """The allowlist that guarded preset launches now guards durable starts (#45).

        The stored path is edited underneath the operation, which is what a moved
        symlink or a hand-edited database looks like from here. `_path_is_within`
        is the only thing standing between that and a herdr start in /etc.
        """
        operation = self.begin_operation()
        connection = herdr_relay.projects.ProjectStore(str(self.db))._connect()
        try:
            connection.execute(
                "UPDATE projects SET canonical_path = ? WHERE project_id = ?",
                (str(Path(self.directory.name) / "outside"), self.project["project_id"]),
            )
            connection.commit()
        finally:
            connection.close()

        with (
            patch.object(herdr_relay.state, "event_queue", queue.Queue()),
            patch.object(herdr_relay.herdr, "get_agent_by_name") as probe,
            patch.object(herdr_relay.herdr, "run_herdr_checked") as run,
        ):
            herdr_relay.operations._start_operation(operation["operation_id"])

        result = herdr_relay.operations.OperationStore(str(self.db)).get(operation["operation_id"])
        self.assertEqual("CONFIGURATION_CHANGED", result["error_code"])
        probe.assert_not_called()
        run.assert_not_called()

    def test_pane_observability_contract_keeps_start_name_internal(self):
        pane = {
            "pane_id": "pane-named",
            "agent": "claude",
            "agent_name": "herdr-mobile-op-1",
            "agent_status": "working",
            "cwd": str(self.root),
        }
        with patch.object(
            herdr_relay.herdr,
            "run_herdr_checked",
            return_value=(True, json.dumps({"result": {"panes": [pane]}})),
        ):
            agents, probe = herdr_relay.herdr.get_agents_from_host(host=self.host)

        self.assertTrue(probe["herdr_ready"])
        self.assertEqual("herdr-mobile-op-1", agents[0]["agent_name"])
        self.assertNotIn("agent_name", herdr_relay.protocol.public_agents(agents)[0])

    def test_agent_get_reply_with_agent_info_is_a_ready_exact_match(self):
        reply = json.dumps({
            "result": {
                "type": "agent_info",
                "agent": {"name": "herdr-mobile-op-1", "pane_id": "pane-1"},
            },
        })
        with patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, reply)):
            matches, probe = herdr_relay.herdr.get_agent_by_name("herdr-mobile-op-1", self.host)

        self.assertEqual({"ssh_reachable": True, "herdr_ready": True}, probe)
        self.assertEqual([{"pane_id": "pane-1", "agent_name": "herdr-mobile-op-1"}], matches)

    def test_agent_not_found_proves_herdr_ready_without_a_match(self):
        reply = json.dumps({"error": {"code": "agent_not_found", "message": "no such agent"}})
        with patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(False, reply)):
            matches, probe = herdr_relay.herdr.get_agent_by_name("herdr-mobile-op-1", self.host)

        self.assertEqual({"ssh_reachable": True, "herdr_ready": True}, probe)
        self.assertEqual([], matches)

    def test_any_other_agent_get_error_is_not_herdr_ready(self):
        # Version skew answers every command with protocol_mismatch. That is
        # not a name-registry answer, so treating it as ready would misfile a
        # later failure as AGENT_NOT_OBSERVABLE instead of HERDR_UNAVAILABLE.
        reply = json.dumps({"error": {"code": "protocol_mismatch", "message": "client speaks 19, server 16"}})
        with patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(False, reply)):
            matches, probe = herdr_relay.herdr.get_agent_by_name("herdr-mobile-op-1", self.host)

        self.assertEqual({"ssh_reachable": True, "herdr_ready": False}, probe)
        self.assertEqual([], matches)

    def test_operation_transition_is_not_held_behind_a_blocked_pane_read(self):
        release = threading.Event()
        operation = {"operation_id": "op-1", "stage": "starting"}
        frames = []

        def blocking_read(_pane_id, remote=None):
            release.wait(timeout=1)
            return "Approve?"

        async def run():
            event_queue = herdr_relay.state.event_queue
            event_queue.put_nowait({"type": "agent_event", "pane_id": "pane-1", "status": "blocked", "host": "local"})
            event_queue.put_nowait({"type": "operation_event", "operation": operation})
            pusher = asyncio.create_task(herdr_relay.event_push())
            try:
                for _ in range(20):
                    if frames:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual("operation", frames[0]["type"])
            finally:
                release.set()
                pusher.cancel()
                await asyncio.gather(pusher, return_exceptions=True)

        original_queue = herdr_relay.state.event_queue
        try:
            with (
                patch.object(herdr_relay.state, "event_queue", asyncio.Queue()),
                patch.object(herdr_relay.herdr, "read_pane", blocking_read),
                patch.object(herdr_relay.transport, "broadcast", AsyncMock(side_effect=lambda frame: frames.append(frame))),
                patch.dict(herdr_relay.state.pane_remote_map, {}, clear=True),
            ):
                # The test coroutine uses the patched queue through state.
                asyncio.run(run())
        finally:
            herdr_relay.state.event_queue = original_queue
