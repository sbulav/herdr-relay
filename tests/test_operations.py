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


def tab_created(pane_id="pane-new", tab_id="tab-new"):
    return json.dumps({
        "id": "cli:tab:create",
        "result": {
            "type": "tab_created",
            "root_pane": {"pane_id": pane_id, "tab_id": tab_id},
            "tab": {"tab_id": tab_id, "title": "shell"},
        },
    })


def herdr_cli(start_reply, tab_reply=None):
    """Stand in for the Herdr CLI across a whole two-step launch.

    Herdr 0.8 needs `tab create` before `agent start`, so a single canned reply
    can no longer answer a launch: this dispatches on the subcommand and lets a
    test speak only about the step it is testing.
    """
    def run(*args, **_kwargs):
        if args[:2] == ("tab", "create"):
            return (True, tab_created()) if tab_reply is None else tab_reply
        if args[:2] == ("tab", "close"):
            return True, json.dumps({"result": {"type": "ok"}})
        return start_reply
    return run


def call_args(run, *prefix):
    """The arguments of the one CLI call that starts with `prefix`."""
    matches = [call.args for call in run.call_args_list if call.args[:len(prefix)] == prefix]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one {' '.join(prefix)} call, got {len(matches)}")
    return matches[0]


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
            patch.object(herdr_relay.herdr, "run_herdr_checked", side_effect=herdr_cli((True, started))) as run,
        ):
            herdr_relay.operations._start_operation(operation["operation_id"])

        result = herdr_relay.operations.OperationStore(str(self.db)).get(operation["operation_id"])
        self.assertEqual("started", result["stage"])
        self.assertEqual("legacy:workstation:pane-9", result["session_id"])
        start = call_args(run, "agent", "start")
        self.assertEqual(operation["agent_name"], start[2])
        # The reply named the pane; nothing was polled after launch.
        probe.assert_called_once()

    def test_launch_creates_the_pane_then_attaches_the_agent_to_it(self):
        """Herdr 0.8 removed `agent start --cwd`: the pane carries the directory.

        `agent start` now attaches to a pane that already exists, so the project
        path belongs to `tab create` and the pane it returns is what the agent
        must be pointed at. Passing the old flags is an argument error, which is
        how every launch broke on the 0.8 upgrade.
        """
        operation = self.begin_operation()
        ready = {"ssh_reachable": True, "herdr_ready": True}
        started = json.dumps({
            "result": {
                "type": "agent_started",
                "agent": {"pane_id": "pane-new", "name": operation["agent_name"]},
            },
        })
        with (
            patch.object(herdr_relay.state, "event_queue", queue.Queue()),
            patch.object(herdr_relay.herdr, "get_agent_by_name", return_value=([], ready)),
            patch.object(herdr_relay.herdr, "run_herdr_checked", side_effect=herdr_cli((True, started))) as run,
        ):
            herdr_relay.operations._start_operation(operation["operation_id"])

        result = herdr_relay.operations.OperationStore(str(self.db)).get(operation["operation_id"])
        self.assertEqual("started", result["stage"])
        self.assertEqual("legacy:workstation:pane-new", result["session_id"])

        create = call_args(run, "tab", "create")
        self.assertEqual(("--cwd", os.path.realpath(self.root)), create[2:4])
        self.assertIn("--no-focus", create)

        start = call_args(run, "agent", "start")
        self.assertEqual(["--kind", "claude"], list(start[3:5]))
        self.assertEqual(["--pane", "pane-new"], list(start[5:7]))
        self.assertNotIn("--cwd", start)
        self.assertNotIn("--no-focus", start)
        # Herdr caps its own prompt wait at 300s and defaults to 30s; the
        # operation's readiness budget is what should govern it.
        self.assertEqual("1000", start[start.index("--timeout") + 1])

    def test_model_selection_is_passed_to_the_harness_not_to_herdr(self):
        operation, _created = herdr_relay.operations.OperationStore(str(self.db)).begin(
            "start-model", "workstation", self.project["project_id"], "claude", "opus"
        )
        ready = {"ssh_reachable": True, "herdr_ready": True}
        started = json.dumps({
            "result": {
                "type": "agent_started",
                "agent": {"pane_id": "pane-new", "name": operation["agent_name"]},
                "argv": ["claude", "--model", "opus"],
            },
        })
        with (
            patch.object(herdr_relay.state, "event_queue", queue.Queue()),
            patch.object(herdr_relay.herdr, "get_agent_by_name", return_value=([], ready)),
            patch.object(herdr_relay.herdr, "run_herdr_checked", side_effect=herdr_cli((True, started))) as run,
        ):
            herdr_relay.operations._start_operation(operation["operation_id"])

        result = herdr_relay.operations.OperationStore(str(self.db)).get(operation["operation_id"])
        self.assertEqual("started", result["stage"])
        start = call_args(run, "agent", "start")
        # `--kind` names the harness now, so everything past `--` is its own.
        self.assertEqual(["--", "--model", "opus"], list(start[-3:]))

    def test_the_deterministic_name_fits_what_herdr_accepts(self):
        """Herdr refuses a name over 32 characters, which failed every launch.

        `herdr-mobile-` plus a full 32-hex operation id is 44 characters, and
        Herdr answered `invalid_agent_name` before it looked at anything else.
        The name is this operation's only recovery key, so it has to be both
        legal and derived from the id alone.
        """
        operation = self.begin_operation()
        name = operation["agent_name"]
        self.assertLessEqual(len(name), 32)
        self.assertRegex(name, r"^[a-z][a-z0-9_-]{0,31}$")
        self.assertTrue(name.startswith("herdr-mobile-"))
        self.assertEqual(
            name, herdr_relay.operations.deterministic_agent_name(operation["operation_id"])
        )
        # Distinct operations still get distinct names.
        self.assertNotEqual(name, self.begin_operation("start-2")["agent_name"])

    def test_an_invalid_name_is_reported_with_herdr_s_own_code(self):
        """A refused launch has to say which call refused it and why.

        `LAUNCH_FAILED` alone is the same row whether Herdr rejected the name,
        the kind, or the pane — the code is what makes the next breakage one log
        line to diagnose instead of a bisect.
        """
        operation = self.begin_operation()
        ready = {"ssh_reachable": True, "herdr_ready": True}
        refused = json.dumps({
            "error": {"code": "invalid_agent_name", "message": "agent name must start with..."},
        })
        with (
            patch.object(herdr_relay.state, "event_queue", queue.Queue()),
            patch.object(herdr_relay.herdr, "get_agent_by_name", return_value=([], ready)),
            patch.object(herdr_relay.herdr, "run_herdr_checked", side_effect=herdr_cli((False, refused))),
            self.assertLogs(herdr_relay.config.log, level="WARNING") as logs,
        ):
            herdr_relay.operations._start_operation(operation["operation_id"])

        self.assertTrue(
            any("agent start on host workstation refused: invalid_agent_name" in line for line in logs.output),
            logs.output,
        )

    def test_harness_kind_follows_the_configured_command(self):
        """A configured harness declares the executable Herdr names its kind after."""
        self.host["harnesses"] = [{"id": "claude", "command": ["/usr/local/bin/claude"]}]
        self.assertEqual("claude", herdr_relay.operations._harness_kind(self.host, "claude"))
        # An unconfigured host has no command to read; the id is the same name.
        self.assertEqual("opencode", herdr_relay.operations._harness_kind(self.host, "opencode"))

    def test_a_failed_launch_does_not_leave_an_empty_tab_behind(self):
        operation = self.begin_operation()
        ready = {"ssh_reachable": True, "herdr_ready": True}
        refused = json.dumps({"error": {"code": "invalid_argument", "message": "unknown kind"}})
        with (
            patch.object(herdr_relay.state, "event_queue", queue.Queue()),
            patch.object(herdr_relay.herdr, "get_agent_by_name", return_value=([], ready)),
            patch.object(herdr_relay.herdr, "run_herdr_checked", side_effect=herdr_cli((False, refused))) as run,
            self.assertLogs(herdr_relay.config.log, level="WARNING"),
        ):
            herdr_relay.operations._start_operation(operation["operation_id"])

        result = herdr_relay.operations.OperationStore(str(self.db)).get(operation["operation_id"])
        self.assertEqual("LAUNCH_FAILED", result["error_code"])
        self.assertEqual(("tab", "close", "tab-new"), call_args(run, "tab", "close"))

    def test_a_tab_that_never_opened_fails_the_launch_without_starting(self):
        operation = self.begin_operation()
        ready = {"ssh_reachable": True, "herdr_ready": True}
        with (
            patch.object(herdr_relay.state, "event_queue", queue.Queue()),
            patch.object(herdr_relay.herdr, "get_agent_by_name", return_value=([], ready)),
            patch.object(
                herdr_relay.herdr,
                "run_herdr_checked",
                side_effect=herdr_cli((True, ""), tab_reply=(False, json.dumps({"error": {"code": "no_server"}}))),
            ) as run,
            self.assertLogs(herdr_relay.config.log, level="WARNING"),
        ):
            herdr_relay.operations._start_operation(operation["operation_id"])

        result = herdr_relay.operations.OperationStore(str(self.db)).get(operation["operation_id"])
        self.assertEqual("LAUNCH_FAILED", result["error_code"])
        self.assertEqual([("tab", "create")], [call.args[:2] for call in run.call_args_list])

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
            patch.object(herdr_relay.herdr, "run_herdr_checked", side_effect=herdr_cli((False, taken))) as run,
            patch.object(herdr_relay.operations.time, "monotonic", side_effect=[0, 0]),
        ):
            herdr_relay.operations._start_operation(operation["operation_id"])

        result = herdr_relay.operations.OperationStore(str(self.db)).get(operation["operation_id"])
        self.assertEqual("started", result["stage"])
        self.assertEqual("legacy:workstation:pane-3", result["session_id"])
        self.assertIsNone(result["error_code"])
        # The agent was already running elsewhere, so the tab opened for it is
        # an orphan — reconciling to the old pane must not litter a new one.
        self.assertEqual(("tab", "close", "tab-new"), call_args(run, "tab", "close"))

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
            patch.object(herdr_relay.herdr, "run_herdr_checked", side_effect=herdr_cli((True, ""))) as run,
            patch.object(herdr_relay.operations.time, "monotonic", side_effect=[0, 0]),
        ):
            herdr_relay.operations._start_operation(operation["operation_id"])

        result = herdr_relay.operations.OperationStore(str(self.db)).get(operation["operation_id"])
        self.assertEqual("started", result["stage"])
        self.assertEqual("legacy:workstation:pane-8", result["session_id"])
        start = call_args(run, "agent", "start")
        self.assertEqual(operation["agent_name"], start[2])
        self.assertNotIn("--model", start)
        # The start reply said nothing usable, so the agent may well be running
        # in the new tab — closing it would kill the launch this is recovering.
        self.assertNotIn(("tab", "close"), [call.args[:2] for call in run.call_args_list])

    def test_ready_host_launch_without_exact_name_is_not_a_readiness_timeout(self):
        operation = self.begin_operation()
        ready = {"ssh_reachable": True, "herdr_ready": True}
        with (
            patch.object(herdr_relay.state, "event_queue", queue.Queue()),
            patch.object(herdr_relay.herdr, "get_agent_by_name", return_value=([], ready)),
            patch.object(herdr_relay.herdr, "run_herdr_checked", side_effect=herdr_cli((True, ""))),
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
            patch.object(herdr_relay.herdr, "run_herdr_checked", side_effect=herdr_cli((True, started))) as run,
        ):
            herdr_relay.operations._start_operation(operation["operation_id"])
            result = herdr_relay.operations.OperationStore(str(self.db)).get(operation["operation_id"])

            self.assertEqual("started", result["stage"])
            wake.assert_called_once()
            self.assertEqual(
                [("tab", "create"), ("agent", "start")],
                [call.args[:2] for call in run.call_args_list],
            )

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
            patch.object(herdr_relay.herdr, "run_herdr_checked", side_effect=herdr_cli((True, started))),
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

        def blocking_read(_pane_id, remote=None, source=None):
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
