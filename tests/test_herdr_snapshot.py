import contextlib
import io
import json
import unittest
from unittest.mock import patch

from relay import herdr_relay

herdr = herdr_relay.herdr


class HerdrSnapshotTests(unittest.TestCase):
    def setUp(self):
        herdr._herdr_too_old_logged.clear()

    def tearDown(self):
        herdr._herdr_too_old_logged.clear()

    def poll(self, answers):
        calls = []

        def run(*args, **kwargs):
            calls.append(args)
            return answers[args]

        out = io.StringIO()
        with patch.object(herdr, "run_herdr_checked", side_effect=run), contextlib.redirect_stdout(out):
            agents, probe = herdr.get_agents_from_host(host_id="old-host")
        return agents, probe, calls, out.getvalue()

    def test_a_herdr_without_api_snapshot_is_named_once_and_marked_unavailable(self):
        answers = {("api", "snapshot"): (False, ""), ("--version",): (True, "herdr 0.8.4\n")}

        agents, probe, calls, log = self.poll(answers)
        self.assertEqual([], agents)
        self.assertFalse(probe["herdr_ready"])
        self.assertTrue(probe["ssh_reachable"])
        self.assertEqual([("api", "snapshot"), ("--version",)], calls)
        self.assertIn("host old-host: herdr 0.8.4 has no `api snapshot`", log)
        self.assertIn("needs herdr 0.9.0 or later", log)

        _agents, _probe, _calls, again = self.poll(answers)
        self.assertEqual("", again)

    def test_a_recovered_host_is_named_again_if_it_regresses(self):
        old = {("api", "snapshot"): (False, ""), ("--version",): (True, "herdr 0.8.4")}
        new = {("api", "snapshot"): (True, json.dumps(
            {"result": {"type": "session_snapshot", "snapshot": {"panes": []}}}))}

        self.assertIn("0.8.4", self.poll(old)[3])
        self.assertTrue(self.poll(new)[1]["herdr_ready"])
        self.assertIn("0.8.4", self.poll(old)[3])

    def test_a_current_herdr_that_fails_is_not_called_too_old(self):
        for version in ("herdr 0.9.3", "herdr 1.0.0", "not a version"):
            with self.subTest(version=version):
                answers = {("api", "snapshot"): (False, ""), ("--version",): (True, version)}
                agents, probe, _calls, log = self.poll(answers)
                self.assertEqual([], agents)
                self.assertFalse(probe["herdr_ready"])
                self.assertEqual("", log)

    def test_a_success_without_a_snapshot_is_not_an_empty_host(self):
        for body in (
            {"result": {"panes": []}},
            {"result": {"type": "session_snapshot", "snapshot": []}},
            {"result": {"type": "session_snapshot", "snapshot": {"panes": {}}}},
            {"result": "nope"},
        ):
            with self.subTest(body=body):
                agents, probe, _calls, _log = self.poll({("api", "snapshot"): (True, json.dumps(body))})
                self.assertEqual([], agents)
                self.assertFalse(probe["herdr_ready"])
                self.assertIsNone(probe["active_agent_count"])

    def test_a_malformed_pane_row_is_skipped_not_fatal(self):
        body = {"result": {"type": "session_snapshot", "snapshot": {
            "panes": [None, "pane", {"pane_id": "a", "agent": "claude"}],
        }}}
        agents, probe, _calls, _log = self.poll({("api", "snapshot"): (True, json.dumps(body))})
        self.assertTrue(probe["herdr_ready"])
        self.assertEqual(["a"], [agent["pane_id"] for agent in agents])

    def test_completion_seq_comes_from_the_agent_rows(self):
        # Shape observed on herdr 0.9.3: the counter sits on `agents[]`, not on
        # the pane rows, and is absent while working.
        body = {"result": {"type": "session_snapshot", "snapshot": {
            "panes": [
                {"pane_id": "w1:p1", "agent": "claude", "agent_status": "idle"},
                {"pane_id": "w1:p2", "agent": "claude", "agent_status": "working"},
                {"pane_id": "w1:p3", "agent": "claude", "agent_status": "idle"},
                {"pane_id": "w1:p4", "agent": "claude", "agent_status": "idle"},
            ],
            "agents": [
                {"pane_id": "w1:p1", "completion_seq": 19, "state_change_seq": 19},
                {"pane_id": "w1:p2", "state_change_seq": 26},
                {"pane_id": "w1:p3", "completion_seq": None},
                {"pane_id": "w1:p4", "completion_seq": -3},
                "not a row",
                {"completion_seq": 7},
            ],
        }}}
        agents, probe, _calls, _log = self.poll({("api", "snapshot"): (True, json.dumps(body))})
        self.assertTrue(probe["herdr_ready"])
        self.assertEqual(
            {agent["pane_id"]: agent.get("completion_seq") for agent in agents},
            {"w1:p1": 19, "w1:p2": None, "w1:p3": None, "w1:p4": None},
        )

    def test_version_parsing(self):
        self.assertEqual((0, 9, 3), herdr._parse_herdr_version("herdr 0.9.3\n"))
        self.assertEqual((0, 8, 4), herdr._parse_herdr_version("herdr 0.8.4-dev"))
        self.assertIsNone(herdr._parse_herdr_version(""))
        self.assertIsNone(herdr._parse_herdr_version("herdr"))


if __name__ == "__main__":
    unittest.main()
