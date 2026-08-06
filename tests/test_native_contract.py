import asyncio
import json
import os
import pathlib
import unittest
from unittest.mock import patch

from relay import herdr_relay


CONTRACT_DIR = pathlib.Path(__file__).resolve().parent.parent / "contract" / "native"


class NativeContractTests(unittest.TestCase):
    def assert_contract(self, name, frame):
        """Compare an emitted frame against its committed golden.

        Set UPDATE_CONTRACT=1 to rewrite the goldens after an intentional
        protocol change; the diff is then reviewed like any other change.
        """
        path = CONTRACT_DIR / f"{name}.json"
        actual = json.dumps(frame, indent=2, sort_keys=True) + "\n"
        if os.environ.get("UPDATE_CONTRACT"):
            CONTRACT_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(actual)
            return
        if not path.exists():
            self.fail(f"Missing contract golden {path}; set UPDATE_CONTRACT=1 to create it")
        expected = path.read_text()
        self.assertEqual(expected, actual)

    def test_server_info(self):
        # The first frame on every connection, pinned like any other.
        self.assert_contract("server_info", herdr_relay.protocol.server_info())

    def test_agents_snapshot(self):
        agents = [{
            "pane_id": "pane-7",
            "agent": "claude",
            "label": "api tests",
            "status": "working",
            "cwd": "/srv/herdr-remote",
            "project": "herdr-remote",
            "host": "buildbox",
            "remote": "deploy@buildbox",
            "workspace_id": "workspace-2",
            "tab_id": "tab-4",
            "output_revision": 7,
        }]
        hosts = [{
            "host_id": "buildbox",
            "display_name": "Build box",
            "status": "ready",
            "online": True,
            "ssh_reachable": True,
            "herdr_ready": True,
            "active_agent_count": 1,
            "capabilities": {"wake": False, "shutdown": False},
            "harnesses": [],
        }]
        presets = [{
            "id": "review",
            "label": "Review",
            "repository": "dcolinmorgan/herdr-remote",
            "agent": "claude",
            "model": "sonnet",
            "hosts": {"buildbox": {"cwd": "/srv/herdr-remote", "target": "deploy@buildbox"}},
        }]
        sent = []

        async def broadcast(frame):
            sent.append(frame)

        with (
            patch.object(herdr_relay.herdr, "get_all_agents", return_value=(agents, hosts)),
            patch.object(herdr_relay.presets, "PRESETS", presets),
            patch.object(herdr_relay.presets, "PRESETS_BY_ID", {"review": presets[0]}),
            patch.object(herdr_relay.transport, "broadcast", side_effect=broadcast),
            patch.dict(herdr_relay.state.last_statuses, {}, clear=True),
            patch.dict(herdr_relay.state.pane_activity, {}, clear=True),
            patch.dict(herdr_relay.state.pane_revisions, {}, clear=True),
            patch.dict(herdr_relay.state.pane_attention_states, {}, clear=True),
            patch.object(herdr_relay.protocol, "now_ms", return_value=1700000000000),
            patch.dict(herdr_relay.state.pane_remote_map, {}, clear=True),
            patch.dict(herdr_relay.state.session_target_map, {}, clear=True),
            patch.dict(herdr_relay.state.pane_cwd_map, {}, clear=True),
            patch.dict(herdr_relay.state.known_panes, set(), clear=True),
        ):
            asyncio.run(herdr_relay._poll_once())
            # Read inside the patch: patch.dict restores the map on exit.
            routed_target = herdr_relay.state.pane_remote_map.get("pane-7")

        self.assert_contract("agents", sent[0])
        # The app's whole launch UI is driven by these two keys; a snapshot
        # without them looks valid and silently empties the picker.
        self.assertIn("presets", sent[0])
        self.assertIn("hosts", sent[0])
        # public_presets() must strip the SSH target. Broadcasting it would hand
        # every connected phone a login string for the build host.
        self.assertNotIn("target", json.dumps(sent[0]["presets"]))
        # The same value must not escape one key over, on the agent entries.
        # It did, as "remote", while this test guarded only the presets.
        self.assertNotIn("deploy@buildbox", json.dumps(sent[0]))
        self.assertNotIn("remote", sent[0]["agents"][0])
        # Routing still works internally; only the wire is cleaned.
        self.assertEqual(routed_target, "deploy@buildbox")

    def test_projects_snapshot(self):
        row = {
            "project_id": "0123456789abcdef0123456789abcdef",
            "host_id": "buildbox",
            "root_id": "root_95e8a4520dc48f2eacf6583c",
            "label": "Herdr relay",
            "canonical_path": "/srv/projects/herdr-relay",
            "archived": 0,
            "available": 1,
            "unavailable_reason": None,
            "last_launch_at": 1700000000000,
        }

        class Store:
            def reconcile(self, _hosts, _roots):
                pass

            def list(self, _query):
                return [row]

        with (
            patch.object(herdr_relay.projects, "store", return_value=Store()),
            patch.object(
                herdr_relay.projects,
                "_configured_roots",
                return_value=(
                    {"buildbox"},
                    {("buildbox", "root_95e8a4520dc48f2eacf6583c")},
                    [{"host_id": "buildbox", "id": "root_95e8a4520dc48f2eacf6583c", "label": "projects"}],
                ),
            ),
        ):
            self.assert_contract("projects", herdr_relay.projects.public_snapshot())

    def test_folder_entries_frame(self):
        host = {
            "id": "buildbox",
            "ssh": {},
            "project_roots": ["/srv/projects"],
        }
        with (
            patch.object(herdr_relay.hosts, "HOSTS_BY_ID", {"buildbox": host}),
            patch.object(
                herdr_relay.project_fs,
                "browse",
                return_value={"canonical_path": "/srv/projects/herdr-relay", "entries": [{"name": "app", "kind": "directory"}]},
            ),
        ):
            root_id = herdr_relay.hosts.project_roots(host)[0]["id"]
            self.assert_contract(
                "folder_entries",
                herdr_relay.projects.browse({
                    "type": "project_browse",
                    "request_id": "req-browse-1",
                    "host_id": "buildbox",
                    "root_id": root_id,
                    "path": ["herdr-relay"],
                }),
            )

    def test_project_create_ack(self):
        host = {"id": "buildbox", "ssh": {}, "project_roots": ["/srv/projects"]}
        row = {
            "project_id": "0123456789abcdef0123456789abcdef",
            "host_id": "buildbox",
            "root_id": "root_95e8a4520dc48f2eacf6583c",
            "label": "New service",
            "canonical_path": "/srv/projects/new-service",
            "archived": 0,
            "available": 1,
            "unavailable_reason": None,
            "last_launch_at": None,
        }

        class Store:
            def begin_create(self, _request_id):
                return None

            def complete_create(self, *_args):
                return row

        with (
            patch.object(herdr_relay.hosts, "HOSTS_BY_ID", {"buildbox": host}),
            patch.object(herdr_relay.projects, "store", return_value=Store()),
            patch.object(
                herdr_relay.project_fs,
                "create",
                return_value={"canonical_path": "/srv/projects/new-service"},
            ),
        ):
            self.assert_contract(
                "command_ack_project_create",
                herdr_relay.projects.create({
                    "type": "project_create",
                    "request_id": "req-create-1",
                    "host_id": "buildbox",
                    "root_id": "root_95e8a4520dc48f2eacf6583c",
                    "path": [],
                    "name": "new-service",
                    "label": "New service",
                }),
            )

    def test_project_create_errors(self):
        host = {"id": "buildbox", "ssh": {}, "project_roots": ["/srv/projects"]}

        class Store:
            def begin_create(self, _request_id):
                return None

            def cancel_create(self, _request_id):
                pass

        class BusyStore:
            def begin_create(self, _request_id):
                raise herdr_relay.projects.ProjectError(
                    "REQUEST_IN_FLIGHT", "This folder is already being created"
                )

        base = {
            "type": "project_create",
            "request_id": "req-create-1",
            "host_id": "buildbox",
            "root_id": "root_95e8a4520dc48f2eacf6583c",
            "path": [],
        }
        with patch.object(herdr_relay.hosts, "HOSTS_BY_ID", {"buildbox": host}):
            self.assert_contract(
                "command_error_invalid_name",
                herdr_relay.projects.handle_command({**base, "name": "CON"}),
            )
            with (
                patch.object(herdr_relay.projects, "store", return_value=Store()),
                patch.object(
                    herdr_relay.project_fs,
                    "create",
                    side_effect=herdr_relay.project_fs.FilesystemError(
                        "FOLDER_EXISTS", "A folder with that name already exists"
                    ),
                ),
            ):
                self.assert_contract(
                    "command_error_folder_exists",
                    herdr_relay.projects.handle_command({**base, "name": "new-service"}),
                )
            with patch.object(herdr_relay.projects, "store", return_value=BusyStore()):
                self.assert_contract(
                    "command_error_request_in_flight",
                    herdr_relay.projects.handle_command({**base, "name": "new-service"}),
                )

    def test_blocked_transition(self):
        agents = [{
            "pane_id": "pane-7",
            "agent": "claude",
            "label": "api tests",
            "status": "blocked",
            "cwd": "/srv/herdr-remote",
            "project": "herdr-remote",
            "host": "buildbox",
            "remote": "deploy@buildbox",
            "workspace_id": "workspace-2",
            "tab_id": "tab-4",
        }]
        sent = []

        async def broadcast(frame):
            sent.append(frame)

        async def send_web_push(*_args, **_kwargs):
            pass

        prompt = "Do you want to proceed?\n1. Yes\n2. No"
        with (
            patch.object(herdr_relay.herdr, "get_all_agents", return_value=(agents, [])),
            patch.object(herdr_relay.transport, "broadcast", side_effect=broadcast),
            patch.object(herdr_relay.herdr, "read_pane", return_value=prompt),
            patch.object(herdr_relay.push, "send_web_push", side_effect=send_web_push),
            patch.dict(herdr_relay.state.last_statuses, {}, clear=True),
            patch.dict(herdr_relay.state.pane_activity, {}, clear=True),
            patch.dict(herdr_relay.state.pane_revisions, {}, clear=True),
            patch.dict(herdr_relay.state.pane_attention_states, {}, clear=True),
            patch.object(herdr_relay.protocol, "now_ms", return_value=1700000000000),
            patch.dict(herdr_relay.state.pane_response_options, {}, clear=True),
        ):
            asyncio.run(herdr_relay._poll_once())

        self.assert_contract("blocked", sent[1])

    def test_pane_content_stream(self):
        class Socket:
            def __init__(self):
                self.sent = []

            async def send(self, message):
                self.sent.append(json.loads(message))

        socket = Socket()
        agents = [{
            "pane_id": "pane-7",
            "agent": "claude",
            "label": "api tests",
            "status": "working",
            "cwd": "/srv/herdr-remote",
            "project": "herdr-remote",
            "host": "buildbox",
            "remote": "deploy@buildbox",
            "workspace_id": "workspace-2",
            "tab_id": "tab-4",
            "output_revision": 7,
        }]
        blocks = [
            {"id": "b0", "kind": "status", "label": "You", "text": "Run tests"},
            {"id": "b1", "kind": "tool", "label": "Bash", "text": "pytest"},
            {"id": "b2", "kind": "assistant_text", "markdown": "All tests pass."},
        ]

        async def broadcast(_frame):
            pass

        with (
            patch.object(herdr_relay.herdr, "get_all_agents", return_value=(agents, [])),
            patch.object(herdr_relay.transport, "broadcast", side_effect=broadcast),
            patch.object(herdr_relay.transcripts.blocks, "pane_blocks", return_value=(blocks, "stable-signature")),
            patch.dict(herdr_relay.state.subscriptions, {socket: "pane-7"}, clear=True),
            patch.dict(herdr_relay.state.stream_sigs, {}, clear=True),
            patch.dict(herdr_relay.state.last_statuses, {}, clear=True),
            patch.dict(herdr_relay.state.pane_activity, {}, clear=True),
            patch.dict(herdr_relay.state.pane_revisions, {}, clear=True),
            patch.dict(herdr_relay.state.pane_attention_states, {}, clear=True),
            patch.object(herdr_relay.protocol, "now_ms", return_value=1700000000000),
        ):
            asyncio.run(herdr_relay._poll_once())

        self.assert_contract("pane_content", socket.sent[0])

    def test_launch_session_ack_and_argv(self):
        class UUID:
            hex = "0123456789abcdef"

        preset = {
            "id": "review",
            "label": "Review",
            "repository": "dcolinmorgan/herdr-remote",
            "agent": "claude",
            "model": "sonnet",
            "hosts": {"buildbox": {"cwd": "/srv/herdr-remote", "target": "deploy@buildbox"}},
        }
        with (
            patch.object(herdr_relay.presets, "PRESETS_BY_ID", {"review": preset}),
            patch.object(herdr_relay.lifecycle.uuid, "uuid4", return_value=UUID()),
            patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, "started")) as run,
        ):
            frame = herdr_relay.launch_session({
                "request_id": "req-launch-17",
                "preset_id": "review",
                "host_id": "buildbox",
            })

        self.assert_contract("command_ack_launch_session", frame)
        run.assert_called_once_with(
            "agent", "start", "mobile-review-01234567", "--cwd", "/srv/herdr-remote",
            "--no-focus", "--", "claude", "--model", "sonnet",
            remote="deploy@buildbox", host_id="buildbox", command=[herdr_relay.config.HERDR], timeout=15,
        )

    def test_terminate_session_ack(self):
        session = "legacy:buildbox:pane-7"
        with (
            patch.dict(
                herdr_relay.state.session_target_map,
                {session: ("pane-7", "deploy@buildbox")},
                clear=True,
            ),
            patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, "pane closed")),
        ):
            frame = herdr_relay.terminate_session({
                "request_id": "req-stop-18",
                "session_id": session,
                "confirmation_nonce": "confirm-stop-18",
            })

        self.assert_contract("command_ack_terminate_session", frame)

    def test_command_errors(self):
        preset = {
            "id": "review",
            "agent": "claude",
            "model": "default",
            "hosts": {"buildbox": {"cwd": "/srv/herdr-remote", "target": "deploy@buildbox"}},
        }
        failed_process = type("Process", (), {"returncode": 1})()
        with patch.object(herdr_relay.presets, "PRESETS_BY_ID", {"review": preset}):
            self.assert_contract(
                "command_error_invalid_request", herdr_relay.launch_session({})
            )
            self.assert_contract(
                "command_error_unknown_preset",
                herdr_relay.launch_session({"request_id": "req-unknown", "preset_id": "missing"}),
            )
            self.assert_contract(
                "command_error_host_not_allowed",
                herdr_relay.launch_session({
                    "request_id": "req-host", "preset_id": "review", "host_id": "other"
                }),
            )
            with patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(False, "")):
                self.assert_contract(
                    "command_error_launch_failed",
                    herdr_relay.launch_session({
                        "request_id": "req-launch", "preset_id": "review", "host_id": "buildbox"
                    }),
                )

        self.assert_contract(
            "command_error_confirmation_required",
            herdr_relay.terminate_session({"request_id": "req-confirm"}),
        )
        with patch.dict(herdr_relay.state.session_target_map, {}, clear=True):
            self.assert_contract(
                "command_error_stale_session",
                herdr_relay.terminate_session({
                    "request_id": "req-stale", "session_id": "legacy:buildbox:pane-7",
                    "confirmation_nonce": "confirm",
                }),
            )
        with (
            patch.dict(
                herdr_relay.state.session_target_map,
                {"legacy:buildbox:pane-7": ("pane-7", "deploy@buildbox")},
                clear=True,
            ),
            patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(False, "")),
        ):
            self.assert_contract(
                "command_error_terminate_failed",
                herdr_relay.terminate_session({
                    "request_id": "req-terminate", "session_id": "legacy:buildbox:pane-7",
                    "confirmation_nonce": "confirm",
                }),
            )

        with (
            patch.object(herdr_relay.config, "POWER_HOST_ID", "buildbox"),
            patch.object(herdr_relay.config, "POWER_HOST_MAC", "00:11:22:33:44:55"),
            patch.object(herdr_relay.lifecycle.subprocess, "run", return_value=failed_process),
        ):
            self.assert_contract(
                "command_error_wake_failed",
                herdr_relay.wake_host({"request_id": "req-wake", "host_id": "buildbox"}),
            )
        # HOST_NOT_ALLOWED is emitted by two different subsystems with two
        # different messages. Both are on the wire, so both are pinned.
        with patch.object(herdr_relay.config, "POWER_HOST_ID", "buildbox"):
            self.assert_contract(
                "command_error_host_not_allowed_power",
                herdr_relay.wake_host({"request_id": "req-wake", "host_id": "laptop"}),
            )
        with (
            patch.object(herdr_relay.config, "POWER_HOST_ID", "buildbox"),
            patch.object(herdr_relay.presets, "HOST_TARGETS", {}),
        ):
            self.assert_contract(
                "command_error_unknown_host",
                herdr_relay.shutdown_host({
                    "request_id": "req-host", "host_id": "buildbox", "confirmation_nonce": "confirm"
                }),
            )
        with (
            patch.object(herdr_relay.config, "POWER_HOST_ID", "buildbox"),
            patch.object(herdr_relay.presets, "HOST_TARGETS", {"buildbox": "deploy@buildbox"}),
            patch.object(herdr_relay.lifecycle.subprocess, "run", return_value=failed_process),
        ):
            self.assert_contract(
                "command_error_shutdown_failed",
                herdr_relay.shutdown_host({
                    "request_id": "req-shutdown", "host_id": "buildbox", "confirmation_nonce": "confirm"
                }),
            )


if __name__ == "__main__":
    unittest.main()
