import asyncio
import json
import os
import runpy
import unittest
import urllib.parse
from unittest.mock import AsyncMock, patch

from relay import herdr_relay


class Issue61MetadataTests(unittest.TestCase):
    def setUp(self):
        for name in (
            "pane_workspace_map", "pane_tab_map", "pane_activity_titles",
            "pane_statuses",
            "pane_attention_states", "pane_activity", "pane_revisions",
            "last_statuses", "pane_dialogs", "pane_dialog_revisions",
        ):
            getattr(herdr_relay.state, name).clear()

    def tearDown(self):
        self.setUp()

    def test_poll_preserves_workspace_tab_labels_and_bounds_working_title(self):
        long_title = "  Run   focused tests " + ("x" * 200)
        responses = iter((
            (True, json.dumps({"result": {"panes": [
                {
                    "pane_id": "pane-7", "agent": "claude",
                    "agent_status": "working", "cwd": "/srv/relay",
                    "workspace_id": "ws-1", "tab_id": "ws-1:t1",
                    "title": long_title,
                },
            ]}})),
            (True, json.dumps({"result": {"workspaces": [
                {"workspace_id": "ws-1", "label": "Relay"},
            ]}})),
            (True, json.dumps({"result": {"tabs": [
                {"tab_id": "ws-1:t1", "label": "API"},
            ]}})),
        ))
        with patch.object(herdr_relay.herdr, "run_herdr_checked", side_effect=lambda *a, **k: next(responses)):
            agents, _probe = herdr_relay.herdr.get_agents_from_host()

        self.assertEqual(agents[0]["workspace_id"], "ws-1")
        self.assertEqual(agents[0]["workspace_name"], "Relay")
        self.assertEqual(agents[0]["tab_id"], "ws-1:t1")
        self.assertEqual(agents[0]["tab_name"], "API")
        self.assertEqual(agents[0]["activity_title"], ("Run focused tests " + "x" * 200)[:160])

    def test_missing_ids_names_and_nonworking_title_are_omitted(self):
        pane_list = json.dumps({"result": {"panes": [
            {"pane_id": "blocked", "agent": "claude", "agent_status": "blocked", "title": "do not emit"},
            {"pane_id": "idle", "agent": "claude", "agent_status": "idle", "workspace_id": "", "tab_id": None},
        ]}})
        with patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, pane_list)):
            agents, _probe = herdr_relay.herdr.get_agents_from_host()

        self.assertNotIn("workspace_id", agents[0])
        self.assertNotIn("tab_id", agents[0])
        self.assertNotIn("activity_title", agents[0])
        self.assertNotIn("workspace_id", agents[1])
        self.assertNotIn("tab_id", agents[1])

    def test_public_projection_drops_routing_fields(self):
        public = herdr_relay.protocol.public_agents([{
            "pane_id": "pane-7", "host_id": "buildbox", "host": "buildbox",
            "workspace_id": "ws-1", "workspace_name": "Relay",
            "tab_id": "ws-1:t1", "tab_name": "API",
            "activity_title": "Run tests", "remote": "deploy@buildbox",
            "ssh": {"target": "deploy@buildbox"}, "routing": "private",
            "agent_name": "internal-start-name",
        }])[0]
        encoded = json.dumps(public)
        self.assertNotIn("deploy@buildbox", encoded)
        self.assertNotIn("ssh", public)
        self.assertNotIn("routing", public)
        self.assertNotIn("agent_name", public)
        self.assertEqual(public["workspace_name"], "Relay")

    def test_pane_content_and_blocked_frames_share_known_grouping_metadata(self):
        key = ("buildbox", "pane-7")
        herdr_relay.state.pane_workspace_map[key] = ("ws-1", "Relay")
        herdr_relay.state.pane_tab_map[key] = ("ws-1:t1", "API")
        herdr_relay.state.pane_activity_titles[key] = "Run tests"
        herdr_relay.state.last_statuses[key] = "working"
        pane = {"type": "pane_content", "pane_id": "pane-7", "host_id": "buildbox"}
        herdr_relay.protocol.add_pane_metadata(pane, "pane-7", "buildbox")
        dialog = herdr_relay.dialogs.ensure(
            "pane-7", "Approve?", ["Yes"], host="buildbox",
            workspace_id="ws-1", workspace_name="Relay",
            tab_id="ws-1:t1", tab_name="API",
        )
        blocked = herdr_relay.dialogs.frame(dialog)
        for field in ("workspace_id", "workspace_name", "tab_id", "tab_name"):
            self.assertEqual(pane[field], blocked[field])
        self.assertNotIn("activity_title", blocked)
        self.assertEqual(pane["activity_title"], "Run tests")

    def test_list_names_uses_one_deadline_across_workspace_scopes(self):
        calls = []
        clock = iter((0.0, 0.0, 0.7, 0.7, 0.9, 1.1))

        def run(*args, **kwargs):
            calls.append((args, kwargs["timeout"]))
            return True, json.dumps({"result": {"tabs": []}})

        with (
            patch.object(herdr_relay.herdr.time, "monotonic", side_effect=lambda: next(clock)),
            patch.object(herdr_relay.herdr, "run_herdr_checked", side_effect=run),
        ):
            names = herdr_relay.herdr._list_names(
                "tab", remote=None, host_id="local", host={}, timeout=1.0,
                workspace_ids={"ws-a", "ws-b"}, cancel_event=None,
            )

        self.assertEqual({}, names)
        self.assertEqual(2, len(calls))
        self.assertEqual(("tab", "list", "--workspace", "ws-a"), calls[0][0])
        self.assertEqual(("tab", "list", "--workspace", "ws-b"), calls[1][0])
        self.assertLessEqual(sum(timeout for _args, timeout in calls), 1.0)

    def test_pushed_labels_are_retained_only_for_the_same_identity(self):
        key = ("buildbox", "pane-7")
        herdr_relay.state.pane_workspace_map[key] = ("ws-old", "Old")
        herdr_relay.state.pane_tab_map[key] = ("tab-old", "Old tab")
        herdr_relay.transport._merge_pushed_identity(
            herdr_relay.state.pane_workspace_map, key, "ws-old", "",
        )
        herdr_relay.transport._merge_pushed_identity(
            herdr_relay.state.pane_tab_map, key, "tab-new", "",
        )
        self.assertEqual(("ws-old", "Old"), herdr_relay.state.pane_workspace_map[key])
        self.assertEqual(("tab-new", ""), herdr_relay.state.pane_tab_map[key])

    def test_dialog_metadata_is_sanitized_and_bounded(self):
        value = "  ws\x00-1\n" + ("x" * 300)
        dialog = herdr_relay.dialogs.ensure(
            "pane-7", "Approve?", ["Yes"], workspace_id=value,
            workspace_name=value, tab_id=value, tab_name=value,
        )
        frame = herdr_relay.dialogs.frame(dialog)
        for field in ("workspace_id", "tab_id"):
            self.assertNotIn("\x00", frame[field])
            self.assertNotIn("\n", frame[field])
            self.assertGreater(len(frame[field]), 160)
        for field in ("workspace_name", "tab_name"):
            self.assertNotIn("\x00", frame[field])
            self.assertNotIn("\n", frame[field])
            self.assertLessEqual(len(frame[field]), 160)

    def test_long_ids_match_across_pane_content_and_blocked_frames(self):
        long_id = "workspace-" + ("x" * 300)
        key = ("local", "pane-7")
        herdr_relay.state.pane_workspace_map[key] = (long_id, "Relay")
        pane = {"type": "pane_content", "pane_id": "pane-7", "host_id": "local"}
        herdr_relay.protocol.add_pane_metadata(pane, "pane-7", "local")
        dialog = herdr_relay.dialogs.ensure(
            "pane-7", "Approve?", ["Yes"], host="local",
            workspace_id=long_id, workspace_name="Relay",
        )
        self.assertEqual(long_id, pane["workspace_id"])
        self.assertEqual(long_id, herdr_relay.dialogs.frame(dialog)["workspace_id"])

    def test_poll_broadcasts_blocked_metadata_rename_and_disappearance(self):
        agents = {
            "first": [{
                "pane_id": "pane-7", "host_id": "local", "agent": "claude",
                "status": "blocked", "cwd": "/srv/repo", "project": "repo",
                "workspace_id": "ws-1", "workspace_name": "Relay",
                "tab_id": "tab-1", "tab_name": "API",
            }],
            "second": [{
                "pane_id": "pane-7", "host_id": "local", "agent": "claude",
                "status": "blocked", "cwd": "/srv/repo", "project": "repo",
                "workspace_id": "ws-1", "workspace_name": "Renamed",
                "tab_id": "tab-1",
            }],
        }
        sent = []

        async def capture(frame):
            sent.append(frame)

        with (
            patch.object(herdr_relay.herdr, "get_all_agents", side_effect=[(agents["first"], []), (agents["second"], [])]),
            patch.object(herdr_relay.herdr, "read_pane", return_value="1. Yes"),
            patch.object(herdr_relay.transport, "broadcast", side_effect=capture),
            patch.object(herdr_relay.projects, "public_snapshot", return_value={"projects": [], "roots": []}),
            patch.object(herdr_relay.operations, "public_recovery", return_value=[]),
            patch.object(herdr_relay.catalogs, "public_frame", return_value={"catalogs": [], "catalog_status": {}}),
            patch.object(herdr_relay.push, "send_web_push", new=AsyncMock()),
        ):
            asyncio.run(herdr_relay.transport._poll_once())
            asyncio.run(herdr_relay.transport._poll_once())

        blocked = [frame for frame in sent if frame.get("type") == "blocked"]
        self.assertEqual(2, len(blocked))
        self.assertEqual("Relay", blocked[0]["workspace_name"])
        self.assertEqual("Renamed", blocked[1]["workspace_name"])
        self.assertNotIn("tab_name", blocked[1])
        self.assertEqual(blocked[0]["dialog_id"], blocked[1]["dialog_id"])

    def test_on_event_forwards_identity_and_title_fields(self):
        class Opened:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class Opener:
            def __init__(self):
                self.request = None

            def open(self, request, timeout):
                self.request = request
                return Opened()

        opener = Opener()
        event = {"data": {
            "pane_id": "pane-7", "agent_status": "working", "agent": "claude",
            "cwd": "/srv/repo", "workspace_id": "ws-1", "workspace_name": "Relay",
            "tab_id": "tab-1", "tab_name": "API", "title": "Run tests",
        }}
        with (
            patch.dict(os.environ, {
                "HERDR_PLUGIN_EVENT_JSON": json.dumps(event),
                "HERDR_HOST_ID": "buildbox", "HERDR_RELAY_TOKEN": "secret",
            }, clear=False),
            patch("urllib.request.build_opener", return_value=opener),
        ):
            runpy.run_path("relay/on_event.py", run_name="__main__")
        payload = json.loads(urllib.parse.parse_qs(urllib.parse.urlsplit(opener.request.full_url).query)["d"][0])
        self.assertEqual("ws-1", payload["workspace_id"])
        self.assertEqual("API", payload["tab_name"])
        self.assertEqual("Run tests", payload["activity_title"])


if __name__ == "__main__":
    unittest.main()
