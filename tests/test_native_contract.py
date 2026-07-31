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
        hosts = [{"host_id": "buildbox", "online": True}]
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
            patch.object(herdr_relay, "get_all_agents", return_value=(agents, hosts)),
            patch.object(herdr_relay, "PRESETS", presets),
            patch.object(herdr_relay, "PRESETS_BY_ID", {"review": presets[0]}),
            patch.object(herdr_relay, "broadcast", side_effect=broadcast),
            patch.dict(herdr_relay.last_statuses, {}, clear=True),
            patch.dict(herdr_relay.pane_activity, {}, clear=True),
            patch.dict(herdr_relay.pane_revisions, {}, clear=True),
            patch.dict(herdr_relay.pane_attention_states, {}, clear=True),
            patch.object(herdr_relay, "now_ms", return_value=1700000000000),
            patch.dict(herdr_relay.pane_remote_map, {}, clear=True),
            patch.dict(herdr_relay.session_target_map, {}, clear=True),
            patch.dict(herdr_relay.pane_cwd_map, {}, clear=True),
            patch.dict(herdr_relay.known_panes, set(), clear=True),
        ):
            asyncio.run(herdr_relay._poll_once())
            # Read inside the patch: patch.dict restores the map on exit.
            routed_target = herdr_relay.pane_remote_map.get("pane-7")

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
            patch.object(herdr_relay, "get_all_agents", return_value=(agents, [])),
            patch.object(herdr_relay, "broadcast", side_effect=broadcast),
            patch.object(herdr_relay, "read_pane", return_value=prompt),
            patch.object(herdr_relay, "send_web_push", side_effect=send_web_push),
            patch.dict(herdr_relay.last_statuses, {}, clear=True),
            patch.dict(herdr_relay.pane_activity, {}, clear=True),
            patch.dict(herdr_relay.pane_revisions, {}, clear=True),
            patch.dict(herdr_relay.pane_attention_states, {}, clear=True),
            patch.object(herdr_relay, "now_ms", return_value=1700000000000),
            patch.dict(herdr_relay.pane_response_options, {}, clear=True),
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
            patch.object(herdr_relay, "get_all_agents", return_value=(agents, [])),
            patch.object(herdr_relay, "broadcast", side_effect=broadcast),
            patch.object(herdr_relay, "pane_blocks", return_value=(blocks, "stable-signature")),
            patch.dict(herdr_relay.subscriptions, {socket: "pane-7"}, clear=True),
            patch.dict(herdr_relay.stream_sigs, {}, clear=True),
            patch.dict(herdr_relay.last_statuses, {}, clear=True),
            patch.dict(herdr_relay.pane_activity, {}, clear=True),
            patch.dict(herdr_relay.pane_revisions, {}, clear=True),
            patch.dict(herdr_relay.pane_attention_states, {}, clear=True),
            patch.object(herdr_relay, "now_ms", return_value=1700000000000),
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
            patch.object(herdr_relay, "PRESETS_BY_ID", {"review": preset}),
            patch.object(herdr_relay.uuid, "uuid4", return_value=UUID()),
            patch.object(herdr_relay, "run_herdr_checked", return_value=(True, "started")) as run,
        ):
            frame = herdr_relay.launch_session({
                "request_id": "req-launch-17",
                "preset_id": "review",
                "host_id": "buildbox",
            })

        self.assert_contract("command_ack_launch_session", frame)
        run.assert_called_once_with(
            "agent", "start", "mobile-review-01234567", "--cwd", "/srv/herdr-remote",
            "--no-focus", "--", "claude", "--model", "sonnet", remote="deploy@buildbox",
        )

    def test_terminate_session_ack(self):
        session = "legacy:buildbox:pane-7"
        with (
            patch.dict(
                herdr_relay.session_target_map,
                {session: ("pane-7", "deploy@buildbox")},
                clear=True,
            ),
            patch.object(herdr_relay, "run_herdr_checked", return_value=(True, "pane closed")),
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
        with patch.object(herdr_relay, "PRESETS_BY_ID", {"review": preset}):
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
            with patch.object(herdr_relay, "run_herdr_checked", return_value=(False, "")):
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
        with patch.dict(herdr_relay.session_target_map, {}, clear=True):
            self.assert_contract(
                "command_error_stale_session",
                herdr_relay.terminate_session({
                    "request_id": "req-stale", "session_id": "legacy:buildbox:pane-7",
                    "confirmation_nonce": "confirm",
                }),
            )
        with (
            patch.dict(
                herdr_relay.session_target_map,
                {"legacy:buildbox:pane-7": ("pane-7", "deploy@buildbox")},
                clear=True,
            ),
            patch.object(herdr_relay, "run_herdr_checked", return_value=(False, "")),
        ):
            self.assert_contract(
                "command_error_terminate_failed",
                herdr_relay.terminate_session({
                    "request_id": "req-terminate", "session_id": "legacy:buildbox:pane-7",
                    "confirmation_nonce": "confirm",
                }),
            )

        with (
            patch.object(herdr_relay, "POWER_HOST_ID", "buildbox"),
            patch.object(herdr_relay, "POWER_HOST_MAC", "00:11:22:33:44:55"),
            patch.object(herdr_relay.subprocess, "run", return_value=failed_process),
        ):
            self.assert_contract(
                "command_error_wake_failed",
                herdr_relay.wake_host({"request_id": "req-wake", "host_id": "buildbox"}),
            )
        # HOST_NOT_ALLOWED is emitted by two different subsystems with two
        # different messages. Both are on the wire, so both are pinned.
        with patch.object(herdr_relay, "POWER_HOST_ID", "buildbox"):
            self.assert_contract(
                "command_error_host_not_allowed_power",
                herdr_relay.wake_host({"request_id": "req-wake", "host_id": "laptop"}),
            )
        with (
            patch.object(herdr_relay, "POWER_HOST_ID", "buildbox"),
            patch.object(herdr_relay, "HOST_TARGETS", {}),
        ):
            self.assert_contract(
                "command_error_unknown_host",
                herdr_relay.shutdown_host({
                    "request_id": "req-host", "host_id": "buildbox", "confirmation_nonce": "confirm"
                }),
            )
        with (
            patch.object(herdr_relay, "POWER_HOST_ID", "buildbox"),
            patch.object(herdr_relay, "HOST_TARGETS", {"buildbox": "deploy@buildbox"}),
            patch.object(herdr_relay.subprocess, "run", return_value=failed_process),
        ):
            self.assert_contract(
                "command_error_shutdown_failed",
                herdr_relay.shutdown_host({
                    "request_id": "req-shutdown", "host_id": "buildbox", "confirmation_nonce": "confirm"
                }),
            )


if __name__ == "__main__":
    unittest.main()
