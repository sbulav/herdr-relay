"""Herdr's ids are checked where they enter the relay, and never repaired (#98).

`pane_id`, `workspace_id` and `tab_id` arrive from every polled host and from
pushed events, and are then cached and broadcast to every client.
"""
import asyncio
import json
import unittest
from unittest.mock import patch

from relay import herdr_relay

protocol = herdr_relay.protocol
state = herdr_relay.state

# What a hostile or broken host might send, and why each is refused.
MALFORMED = {
    "a trailing newline": "w1-p1\n",
    "a quote": "x');alert(1)//",
    "an over-long id": "p" * 129,
    "a space": "w1 p1",
    "a control character": "w1\x00p1",
    "a non-ASCII letter": "w1-pé",
}


def snapshot(*panes):
    return json.dumps({"result": {"snapshot": {
        "version": "0.9.2", "panes": list(panes), "workspaces": [], "tabs": [], "agents": [],
    }}})


def pane(pane_id, **fields):
    return {"pane_id": pane_id, "agent": "claude", "agent_status": "idle", "cwd": "/w", **fields}


class HerdrIdTests(unittest.TestCase):
    def setUp(self):
        protocol._refused_ids.clear()

    def test_herdrs_own_ids_pass_unchanged(self):
        for value in ("w1-p2", "workspace-2", "ws-a:t1", "%22", "pane_7.1", "p" * 128):
            self.assertEqual(value, protocol.herdr_id(value))

    def test_malformed_ids_are_refused_not_repaired(self):
        for why, value in MALFORMED.items():
            with self.subTest(why):
                self.assertIsNone(protocol.herdr_id(value))
                self.assertIsNone(protocol.public_identifier(value))

    def test_absent_and_non_string_ids(self):
        with self.assertNoLogs(herdr_relay.config.log):
            self.assertIsNone(protocol.herdr_id(None))
            self.assertIsNone(protocol.herdr_id(""))
        for value in (7, ["w1"], {"id": "w1"}, True):
            self.assertIsNone(protocol.herdr_id(value))

    def test_each_refusal_is_logged_once(self):
        with self.assertLogs(herdr_relay.config.log, "WARNING") as logs:
            for _ in range(3):
                protocol.herdr_id("x');alert(1)//", "pane_id")
            protocol.herdr_id("x');alert(1)//", "tab_id")
        self.assertEqual(2, len(logs.records))
        self.assertIn("pane_id", logs.output[0])

    def test_the_refusal_memory_is_bounded(self):
        with patch.object(protocol, "REFUSED_ID_MEMORY", 4), self.assertLogs(herdr_relay.config.log):
            for index in range(10):
                protocol.herdr_id(f"bad id {index}")
                self.assertLessEqual(len(protocol._refused_ids), 4)

    def test_a_logged_value_is_bounded(self):
        with self.assertLogs(herdr_relay.config.log) as logs:
            protocol.herdr_id("\n" * 10_000)
        self.assertLess(len(logs.output[0]), 300)


class PollTests(unittest.TestCase):
    def setUp(self):
        protocol._refused_ids.clear()

    def poll(self, *panes):
        with patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, snapshot(*panes))), \
                self.assertLogs(herdr_relay.config.log):
            agents, probe = herdr_relay.herdr.get_agents_from_host()
        self.assertTrue(probe["herdr_ready"])
        return agents

    def test_a_row_with_a_malformed_pane_id_is_dropped(self):
        for why, value in MALFORMED.items():
            with self.subTest(why):
                agents = self.poll(pane(value), pane("w1-p2"))
                self.assertEqual(["w1-p2"], [agent["pane_id"] for agent in agents])

    def test_a_malformed_workspace_or_tab_id_is_blanked_and_the_pane_kept(self):
        for why, value in MALFORMED.items():
            with self.subTest(why):
                protocol._refused_ids.clear()
                agents = self.poll(pane(
                    "w1-p2", workspace_id=value, tab_id=value,
                    workspace={"label": "Relay"}, tab={"label": "Main"},
                ))
                self.assertEqual("w1-p2", agents[0]["pane_id"])
                for field in ("workspace_id", "workspace_name", "tab_id", "tab_name"):
                    self.assertNotIn(field, agents[0])


class PushedEventTests(unittest.TestCase):
    def setUp(self):
        protocol._refused_ids.clear()
        self.key = ("local", "w1-p2")
        state.known_panes.add("w1-p2")
        state.known_pane_keys.add(self.key)
        state.pane_hosts.setdefault("w1-p2", set()).add("local")
        state.pane_host_map[self.key] = "local"
        self.addCleanup(self.forget)

    def forget(self):
        state.known_panes.discard("w1-p2")
        state.known_pane_keys.discard(self.key)
        state.pane_hosts.pop("w1-p2", None)
        state.pane_host_map.pop(self.key, None)
        state.pane_workspace_map.pop(self.key, None)
        state.pane_tab_map.pop(self.key, None)

    def push(self, event):
        sent = []

        async def broadcast(message):
            sent.append(message)

        with patch.object(herdr_relay.transport, "broadcast", broadcast):
            asyncio.run(herdr_relay.transport._handle_pushed_event(event))
        return sent

    def test_an_event_for_a_malformed_pane_is_dropped(self):
        for why, value in MALFORMED.items():
            with self.subTest(why), self.assertLogs(herdr_relay.config.log):
                protocol._refused_ids.clear()
                self.assertEqual([], self.push({
                    "type": "agent_event", "pane_id": value, "host_id": "local", "status": "working",
                }))

    def test_a_malformed_workspace_id_keeps_what_was_known(self):
        state.pane_workspace_map[self.key] = ("ws-a", "Relay")
        with self.assertLogs(herdr_relay.config.log):
            sent = self.push({
                "type": "agent_event", "pane_id": "w1-p2", "host_id": "local", "status": "working",
                "workspace_id": "x');alert(1)//", "workspace_name": "Evil", "tab_id": "t1\n",
            })
        self.assertEqual(("ws-a", "Relay"), state.pane_workspace_map[self.key])
        self.assertNotIn(self.key, state.pane_tab_map)
        agent = sent[-1]["agents"][0]
        self.assertEqual("ws-a", agent["workspace_id"])
        self.assertNotIn("tab_id", agent)

    def test_a_tmux_pane_id_is_still_a_pane_id(self):
        self.assertEqual("%22", protocol.herdr_id("%22"))

    def test_an_event_that_is_not_an_object_does_not_stop_the_consumer(self):
        async def run():
            queue = asyncio.Queue()
            handled = []

            async def handle(event):
                handled.append(event)

            with patch.object(state, "event_queue", queue), \
                    patch.object(herdr_relay.transport, "_handle_pushed_event", handle), \
                    patch.object(herdr_relay.transport, "wake_poll_loop", lambda: None):
                consumer = asyncio.create_task(herdr_relay.transport.event_push())
                for event in ([1], "text", None, {"type": "agent_event", "pane_id": "w1-p2"}):
                    queue.put_nowait(event)
                for _ in range(20):
                    await asyncio.sleep(0)
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
            return handled

        self.assertEqual([{"type": "agent_event", "pane_id": "w1-p2"}], asyncio.run(run()))


class CreateTabTests(unittest.TestCase):
    def setUp(self):
        protocol._refused_ids.clear()

    def test_a_malformed_workspace_id_never_reaches_herdr(self):
        for why, value in MALFORMED.items():
            with self.subTest(why), \
                    patch.object(herdr_relay.herdr, "run_herdr_checked") as run, \
                    self.assertLogs(herdr_relay.config.log):
                protocol._refused_ids.clear()
                reply = asyncio.run(herdr_relay.server._create_tab(
                    {"type": "create_tab", "workspace_id": value}, "127.0.0.1", "test",
                ))
                self.assertEqual(protocol.error("workspace_id required"), reply)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
