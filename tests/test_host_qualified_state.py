import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from relay import herdr_relay


class HostQualifiedStateTests(unittest.TestCase):
    def setUp(self):
        self.maps = (
            herdr_relay.state.known_panes,
            herdr_relay.state.known_pane_keys,
            herdr_relay.state.pane_hosts,
            herdr_relay.state.pane_remote_map,
            herdr_relay.state.pane_cwd_map,
            herdr_relay.state.pane_dialogs,
            herdr_relay.state.pane_response_options,
            herdr_relay.state.last_statuses,
            herdr_relay.state.pane_activity,
            herdr_relay.state.pane_revisions,
            herdr_relay.state.pane_attention_states,
            herdr_relay.state.pane_host_map,
            herdr_relay.state.pane_project_map,
        )
        for mapping in self.maps:
            mapping.clear()

    def tearDown(self):
        for mapping in self.maps:
            mapping.clear()

    def expose(self, *hosts):
        pane_id = "pane-7"
        herdr_relay.state.known_panes.add(pane_id)
        for host_id, remote in hosts:
            key = herdr_relay.state.pane_key(host_id, pane_id)
            herdr_relay.state.known_pane_keys.add(key)
            herdr_relay.state.pane_hosts.setdefault(pane_id, set()).add(host_id)
            herdr_relay.state.pane_remote_map[key] = remote
            herdr_relay.state.pane_cwd_map[key] = ("/work/" + host_id, "claude", remote, False)

    def test_omitted_host_is_rejected_when_pane_is_duplicated(self):
        self.expose(("alpha", "alpha@example"), ("beta", "beta@example"))
        self.assertIs(herdr_relay.state.resolve(None, "pane-7"), herdr_relay.state.AMBIGUOUS)

    def test_explicit_host_is_authoritative(self):
        self.expose(("alpha", "alpha@example"), ("beta", "beta@example"))
        self.assertEqual(herdr_relay.state.resolve("alpha", "pane-7"), ("alpha", "pane-7"))
        self.assertIsNone(herdr_relay.state.resolve("wrong", "pane-7"))

    def test_single_host_legacy_resolution(self):
        self.expose(("alpha", "alpha@example"))
        self.assertEqual(herdr_relay.state.resolve(None, "pane-7"), ("alpha", "pane-7"))

    def test_omitted_host_becomes_unique_after_other_host_disappears(self):
        self.expose(("alpha", "alpha@example"), ("beta", "beta@example"))
        self.assertIs(herdr_relay.state.resolve(None, "pane-7"), herdr_relay.state.AMBIGUOUS)
        herdr_relay.state.pane_hosts["pane-7"].remove("beta")
        herdr_relay.state.known_pane_keys.remove(("beta", "pane-7"))
        self.assertEqual(herdr_relay.state.resolve(None, "pane-7"), ("alpha", "pane-7"))
        self.assertIsNone(herdr_relay.state.resolve("beta", "pane-7"))

    def test_dialogs_are_isolated_by_host(self):
        self.expose(("alpha", "alpha@example"), ("beta", "beta@example"))
        first = herdr_relay.dialogs.ensure("pane-7", "Approve alpha", ["Yes"], host="alpha")
        second = herdr_relay.dialogs.ensure("pane-7", "Approve beta", ["Yes"], host="beta")
        self.assertNotEqual(first["dialog_id"], second["dialog_id"])
        self.assertEqual(set(herdr_relay.state.pane_dialogs), {
            ("alpha", "pane-7"), ("beta", "pane-7")
        })

    def test_transcript_lookup_uses_host_key(self):
        self.expose(("alpha", None), ("beta", None))
        with patch.object(herdr_relay.transcripts.blocks, "_transcript", return_value=([], "sig", {})) as read:
            herdr_relay.transcripts.blocks.pane_blocks("pane-7", host_id="beta")
        read.assert_called_once_with("pane-7", host_id="beta")

    def test_read_pane_forwards_canonical_host_to_herdr(self):
        self.expose(("alpha", "same"), ("beta", "same"))
        hosts = [{"id": "alpha", "ssh": {"target": "same"}, "herdr": {"binary": "/opt/a/herdr"}},
                 {"id": "beta", "ssh": {"target": "same"}, "herdr": {"binary": "/opt/b/herdr", "wrapper": ["env", "X=1"]}}]
        with (
            patch.object(herdr_relay.herdr, "configured_host_records", return_value=hosts),
            patch.object(herdr_relay.herdr, "run_herdr", return_value="output") as run,
        ):
            self.assertEqual(
                "output",
                herdr_relay.herdr.read_pane(
                    "pane-7", remote="same", host_id="beta"
                ),
            )
        run.assert_called_once_with(
            "pane", "read", "pane-7", "--lines", "30", "--source", "visible",
            remote="same", host_id="beta", command=["env", "X=1", "/opt/b/herdr"],
        )

    def test_explicit_host_event_discovers_pane_before_poll(self):
        host = {
            "id": "beta",
            "ssh": {"target": "deploy@beta"},
            "project_roots": ["/"],
        }
        event = {
            "type": "agent_event",
            "pane_id": "new-pane",
            "host_id": "beta",
            "status": "working",
            "agent": "claude",
            "project": "repo",
            "cwd": "/srv/repo",
        }
        broadcast = AsyncMock()
        with (
            patch.object(herdr_relay.herdr, "configured_host_records", return_value=[host]),
            patch.object(herdr_relay.transport, "broadcast", broadcast),
        ):
            asyncio.run(herdr_relay.transport._handle_pushed_event(event))

        key = ("beta", "new-pane")
        self.assertIn(key, herdr_relay.state.known_pane_keys)
        self.assertEqual("deploy@beta", herdr_relay.state.pane_remote_map[key])
        broadcast.assert_awaited_once()
        self.assertEqual("beta", broadcast.await_args.args[0]["agents"][0]["host_id"])

    def test_omitted_ambiguous_event_does_not_discover_or_broadcast(self):
        self.expose(("alpha", "alpha@example"), ("beta", "beta@example"))
        broadcast = AsyncMock()
        with patch.object(herdr_relay.transport, "broadcast", broadcast):
            asyncio.run(herdr_relay.transport._handle_pushed_event({
                "type": "agent_event", "pane_id": "pane-7", "status": "working",
            }))
        broadcast.assert_not_awaited()

    def test_poll_keeps_duplicate_panes_separate(self):
        agents = [
            {"pane_id": "pane-7", "agent": "claude", "status": "working",
             "cwd": "/work/alpha", "project": "alpha", "host": "alpha", "remote": "alpha@example"},
            {"pane_id": "pane-7", "agent": "claude", "status": "working",
             "cwd": "/work/beta", "project": "beta", "host": "beta", "remote": "beta@example"},
        ]
        sent = []

        async def capture(frame):
            sent.append(frame)

        with (
            patch.object(herdr_relay.herdr, "get_all_agents", return_value=(agents, [])),
            patch.object(herdr_relay.transport, "broadcast", side_effect=capture),
            patch.object(herdr_relay.projects, "public_snapshot", return_value={"projects": [], "roots": []}),
            patch.object(herdr_relay.operations, "public_recovery", return_value=[]),
            patch.object(herdr_relay.catalogs, "public_frame", return_value={"catalogs": [], "catalog_status": {}}),
        ):
            asyncio.run(herdr_relay.transport._poll_once())

        self.assertEqual({("alpha", "pane-7"), ("beta", "pane-7")}, herdr_relay.state.known_pane_keys)
        self.assertEqual("alpha@example", herdr_relay.state.pane_remote_map[("alpha", "pane-7")])
        self.assertEqual("beta@example", herdr_relay.state.pane_remote_map[("beta", "pane-7")])
        self.assertIs(herdr_relay.state.resolve(None, "pane-7"), herdr_relay.state.AMBIGUOUS)
        self.assertEqual({"alpha", "beta"}, {a["host_id"] for a in sent[0]["agents"]})

    def test_poll_rejects_duplicate_canonical_rows_atomically(self):
        herdr_relay.state.known_pane_keys.add(("old", "pane-7"))
        herdr_relay.state.pane_hosts["pane-7"] = {"old"}
        herdr_relay.state.pane_remote_map[("old", "pane-7")] = "old@example"
        sent = []

        async def capture(frame):
            sent.append(frame)

        agents = [
            {"pane_id": "pane-7", "host_id": "alpha", "status": "working"},
            {"pane_id": "pane-7", "host_id": "alpha", "status": "done"},
        ]
        with (
            patch.object(herdr_relay.herdr, "get_all_agents", return_value=(agents, [])),
            patch.object(herdr_relay.transport, "broadcast", side_effect=capture),
        ):
            asyncio.run(herdr_relay.transport._poll_once())

        self.assertEqual(sent, [])
        self.assertEqual(herdr_relay.state.known_pane_keys, {('old', 'pane-7')})
        self.assertEqual(herdr_relay.state.pane_remote_map, {('old', 'pane-7'): 'old@example'})


if __name__ == "__main__":
    unittest.main()
