import asyncio
import json
import unittest
from unittest.mock import patch

from relay import herdr_relay
from tests.test_structured_output import after_handshake


BUILDBOX = {
    "id": "buildbox",
    "display_name": "Build box",
    "ssh": {"target": "deploy@buildbox"},
    "herdr": {"wrapper": ["nix", "run", "--"], "binary": "/opt/herdr"},
}
LOCAL = {"id": "local", "display_name": "Local host", "ssh": {}, "herdr": {"wrapper": []}}


class CreateTabTests(unittest.TestCase):
    """`create_tab` through the real dispatch loop (#67)."""

    def drive(self, messages, results=None):
        sent = []

        class Socket:
            request_headers = {}

            def __aiter__(inner):
                inner._pending = iter(messages)
                return inner

            async def __anext__(inner):
                try:
                    return json.dumps(next(inner._pending))
                except StopIteration:
                    raise StopAsyncIteration

            async def send(inner, raw):
                sent.append(json.loads(raw))

        with (
            patch.object(herdr_relay.herdr, "configured_host_records", return_value=[LOCAL, BUILDBOX]),
            patch.object(
                herdr_relay.herdr, "run_herdr_checked",
                side_effect=list(results or [(True, "")] * len(messages)),
            ) as checked,
            patch.object(herdr_relay.server, "audit"),
        ):
            asyncio.run(herdr_relay.handle_client(Socket()))
        return after_handshake(sent), checked

    def create(self, request_id="tab-1", **fields):
        return {"type": "create_tab", "request_id": request_id, "workspace_id": "workspace-2", **fields}

    def test_remote_host_runs_over_its_ssh_target_with_its_herdr(self):
        frames, checked = self.drive([self.create(host_id="buildbox")])

        self.assertEqual(
            [{"type": "tab_created", "ok": True, "workspace_id": "workspace-2",
              "host_id": "buildbox", "request_id": "tab-1"}],
            frames,
        )
        self.assertEqual(("tab", "create", "--workspace", "workspace-2", "--focus"), checked.call_args.args)
        self.assertEqual(
            {"remote": "deploy@buildbox", "host_id": "buildbox", "command": ["nix", "run", "--", "/opt/herdr"]},
            checked.call_args.kwargs,
        )
        self.assertNotIn("deploy@buildbox", json.dumps(frames))

    def test_unknown_host_is_refused_without_reaching_herdr(self):
        frames, checked = self.drive([self.create(host_id="elsewhere")])

        self.assertEqual(
            [{"type": "command_error", "request_id": "tab-1", "code": "UNKNOWN_HOST", "message": "Unknown host"}],
            frames,
        )
        checked.assert_not_called()

    def test_non_string_host_is_unknown(self):
        frames, checked = self.drive([self.create(host_id=["buildbox"])])

        self.assertEqual("UNKNOWN_HOST", frames[0]["code"])
        checked.assert_not_called()

    def test_herdr_failure_is_reported_and_a_retry_runs_again(self):
        frames, checked = self.drive(
            [self.create(host_id="buildbox")] * 2, results=[(False, "no such workspace"), (True, "")]
        )

        self.assertEqual(
            {"type": "command_error", "request_id": "tab-1", "code": "HERDR_FAILED",
             "message": "Herdr did not create the tab"},
            frames[0],
        )
        # A failure made no tab, so it is not remembered: the retry reaches Herdr.
        self.assertEqual("tab_created", frames[1]["type"])
        self.assertEqual(2, checked.call_count)

    def test_a_replayed_success_makes_one_tab(self):
        frames, checked = self.drive([self.create(host_id="buildbox")] * 2)

        self.assertEqual(frames[0], frames[1])
        self.assertEqual(1, checked.call_count)

    def test_reused_request_id_for_another_workspace_is_refused(self):
        frames, checked = self.drive([
            self.create(host_id="buildbox"),
            {**self.create(host_id="buildbox"), "workspace_id": "workspace-3"},
        ])

        self.assertEqual("REQUEST_ID_REUSED", frames[1]["code"])
        self.assertEqual(1, checked.call_count)

    def test_frame_without_host_keeps_the_relays_own_herdr(self):
        frames, checked = self.drive([{"type": "create_tab", "workspace_id": "workspace-2"}])

        self.assertEqual([{"type": "tab_created", "ok": True, "workspace_id": "workspace-2"}], frames)
        self.assertEqual(
            {"remote": None, "host_id": None, "command": [herdr_relay.config.HERDR]},
            checked.call_args.kwargs,
        )

    def test_missing_workspace_keeps_the_bare_error(self):
        frames, checked = self.drive([{"type": "create_tab", "request_id": "tab-1", "host_id": "buildbox"}])

        self.assertEqual([{"type": "error", "message": "workspace_id required"}], frames)
        checked.assert_not_called()

    def test_malformed_request_id_is_invalid(self):
        frames, checked = self.drive([self.create(request_id="bad id!", host_id="buildbox")])

        self.assertEqual(
            [{"type": "command_error", "request_id": None, "code": "INVALID_REQUEST",
              "message": "request_id is required"}],
            frames,
        )
        checked.assert_not_called()

    def test_rate_limited_create_answers_in_the_typed_dialect(self):
        with (
            patch.object(herdr_relay.config, "RATE_HOST_BURST", 1),
            patch.object(herdr_relay.config, "RATE_HOST_PER_SECOND", 0),
        ):
            frames, checked = self.drive(
                [self.create(host_id="buildbox"), self.create(request_id="tab-2", host_id="buildbox")]
            )

        self.assertEqual(
            {"type": "command_error", "request_id": "tab-2", "code": "RATE_LIMITED",
             "message": "Too many requests, slow down"},
            frames[1],
        )
        self.assertEqual(1, checked.call_count)


if __name__ == "__main__":
    unittest.main()
