import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from relay import herdr_relay


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = str(Path(self.directory.name) / "state.sqlite3")
        self.host = {
            "id": "buildbox",
            "display_name": "Build box",
            "ssh": {},
            "project_roots": ["/srv/projects"],
            "herdr": {"wrapper": [], "binary": "/usr/bin/herdr"},
            "harnesses": [
                {"id": "opencode", "display_name": "OpenCode", "command": ["opencode"], "enabled": True, "model_aliases": []},
                {"id": "claude", "display_name": "Claude Code", "command": ["claude"], "enabled": True, "model_aliases": [{"id": "sonnet", "display_name": "Sonnet"}]},
            ],
            "power": {"wake": None, "shutdown": False},
            "readiness_timeout_seconds": 5,
        }

    def tearDown(self):
        self.directory.cleanup()

    def records(self):
        return [self.host]

    def test_opencode_adapter_persists_exact_ids_and_default(self):
        with (
            patch.object(herdr_relay.config, "PROJECTS_DB", self.db),
            patch.object(herdr_relay.herdr, "configured_host_records", return_value=self.records()),
            patch.object(herdr_relay.herdr, "run_herdr_checked", side_effect=[
                (True, "opencode 1.2.3"),
                (True, json.dumps({"models": [{"id": "openai/gpt-5", "name": "GPT-5"}]})),
                (True, "claude 1.0"),
            ]) as run,
        ):
            frame = herdr_relay.catalogs.refresh_all()

        catalog = next(item for item in frame["catalogs"] if item["harness_id"] == "opencode")
        self.assertEqual(["default", "openai/gpt-5"], [item["id"] for item in catalog["models"]])
        self.assertEqual("1.2.3", catalog["version"])
        self.assertEqual(3, run.call_count)

    def test_opencode_is_probed_without_json_flag_and_parses_one_id_per_line(self):
        # OpenCode rejects `models --json` with usage text and exit 1, which
        # reached clients as MODEL_DISCOVERY_FAILED and made the harness
        # unlaunchable. The argv is the fix, so pin it here.
        listing = "opencode/big-pickle\nanthropic/claude-opus-4-8\nhhdev-grok/grok-4.6\n"
        with (
            patch.object(herdr_relay.config, "PROJECTS_DB", self.db),
            patch.object(herdr_relay.herdr, "configured_host_records", return_value=self.records()),
            patch.object(herdr_relay.herdr, "run_herdr_checked", side_effect=[
                (True, "opencode 1.18.4"),
                (True, listing),
                (True, "claude 1.0"),
            ]) as run,
        ):
            frame = herdr_relay.catalogs.refresh_all()

        model_calls = [call for call in run.call_args_list if call.args and call.args[0] == "models"]
        self.assertEqual([("models",)], [call.args for call in model_calls])
        catalog = next(item for item in frame["catalogs"] if item["harness_id"] == "opencode")
        self.assertTrue(catalog["available"])
        self.assertEqual(
            ["default", "opencode/big-pickle", "anthropic/claude-opus-4-8", "hhdev-grok/grok-4.6"],
            [item["id"] for item in catalog["models"]],
        )
        self.assertEqual("1.18.4", catalog["version"])

    def test_opencode_listing_keeps_ids_and_drops_interleaved_noise(self):
        listing = "warning: config key is deprecated\nanthropic/claude-opus-4-8\n\n"
        with (
            patch.object(herdr_relay.config, "PROJECTS_DB", self.db),
            patch.object(herdr_relay.herdr, "configured_host_records", return_value=self.records()),
            patch.object(herdr_relay.herdr, "run_herdr_checked", side_effect=[
                (True, "opencode 1.18.4"),
                (True, listing),
                (True, "claude 1.0"),
            ]),
        ):
            frame = herdr_relay.catalogs.refresh_all()

        catalog = next(item for item in frame["catalogs"] if item["harness_id"] == "opencode")
        self.assertEqual(["default", "anthropic/claude-opus-4-8"], [item["id"] for item in catalog["models"]])

    def test_opencode_listing_in_an_unknown_shape_is_not_an_empty_catalog(self):
        with (
            patch.object(herdr_relay.config, "PROJECTS_DB", self.db),
            patch.object(herdr_relay.herdr, "configured_host_records", return_value=self.records()),
            patch.object(herdr_relay.herdr, "run_herdr_checked", side_effect=[
                (True, "opencode 1.18.4"),
                (True, "Usage: opencode models [provider]"),
                (True, "claude 1.0"),
            ]),
        ):
            frame = herdr_relay.catalogs.refresh_all()

        catalog = next(item for item in frame["catalogs"] if item["harness_id"] == "opencode")
        self.assertFalse(catalog["available"])
        self.assertFalse(catalog["disabled"])
        self.assertTrue(catalog["error"].startswith("MODEL_DISCOVERY_INVALID:"))

    def test_claude_uses_configured_aliases_without_model_probe(self):
        with (
            patch.object(herdr_relay.config, "PROJECTS_DB", self.db),
            patch.object(herdr_relay.herdr, "configured_host_records", return_value=self.records()),
            patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, "claude 1.0")) as run,
        ):
            frame = herdr_relay.catalogs.refresh_all()

        catalog = next(item for item in frame["catalogs"] if item["harness_id"] == "claude")
        self.assertEqual(["default", "sonnet"], [item["id"] for item in catalog["models"]])
        # OpenCode also probes its model command; Claude only probes --version.
        claude_calls = [call for call in run.call_args_list if call.args and call.args[0] == "--version"]
        self.assertEqual(2, len(claude_calls))

    def test_transient_failure_keeps_last_successful_models_as_stale(self):
        with (
            patch.object(herdr_relay.config, "PROJECTS_DB", self.db),
            patch.object(herdr_relay.herdr, "configured_host_records", return_value=self.records()),
            patch.object(herdr_relay.herdr, "run_herdr_checked", side_effect=[
                (True, "opencode 1.2.3"), (True, '[{"id":"openai/gpt-5"}]'),
                (False, "timeout"), (False, "timeout"), (False, "timeout"),
            ]),
        ):
            first = herdr_relay.catalogs.refresh_all()
            second = herdr_relay.catalogs.refresh_all()

        catalog = next(item for item in second["catalogs"] if item["harness_id"] == "opencode")
        self.assertTrue(catalog["stale"])
        self.assertTrue(catalog["available"])
        self.assertIn("openai/gpt-5", [item["id"] for item in catalog["models"]])
        self.assertEqual("HARNESS_UNAVAILABLE: Harness did not respond", catalog["error"])
        self.assertFalse(next(item for item in first["catalogs"] if item["harness_id"] == "opencode")["stale"])

    def test_missing_command_is_permanently_disabled(self):
        with (
            patch.object(herdr_relay.config, "PROJECTS_DB", self.db),
            patch.object(herdr_relay.herdr, "configured_host_records", return_value=self.records()),
            patch.object(herdr_relay.herdr, "run_herdr_checked", side_effect=[
                (False, "opencode: command not found"),
                (True, "claude 1.0"),
            ]),
        ):
            frame = herdr_relay.catalogs.refresh_all()

        catalog = next(item for item in frame["catalogs"] if item["harness_id"] == "opencode")
        self.assertFalse(catalog["available"])
        self.assertTrue(catalog["disabled"])
        self.assertEqual([], catalog["models"])
        self.assertTrue(catalog["error"].startswith("HARNESS_MISSING:"))

    def test_targeted_refresh_does_not_reset_global_schedule(self):
        with (
            patch.object(herdr_relay.config, "PROJECTS_DB", self.db),
            patch.object(herdr_relay.herdr, "configured_host_records", return_value=self.records()),
            patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, "claude 1.0")),
        ):
            herdr_relay.catalogs.refresh_all()
            herdr_relay.catalogs.CatalogStore(self.db).set_metadata({"last_refresh_at": 1000})
            herdr_relay.catalogs.refresh_all(host_id="buildbox")
            status = herdr_relay.catalogs.public_status(now=2000)
            self.assertEqual(1000, status["last_refresh_at"])
            self.assertEqual(1000 + herdr_relay.catalogs.REFRESH_INTERVAL_MS, status["next_refresh_at"])

    def test_targeted_refresh_does_not_clobber_global_status(self):
        with (
            patch.object(herdr_relay.config, "PROJECTS_DB", self.db),
            patch.object(herdr_relay.herdr, "configured_host_records", return_value=self.records()),
            patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, "claude 1.0")),
        ):
            herdr_relay.catalogs.CatalogStore(self.db).set_metadata({
                "state": "error",
                "error": "other-host/opencode: Harness did not respond",
                "last_refresh_at": 1000,
            })
            herdr_relay.catalogs.refresh_all(host_id="buildbox")
            status = herdr_relay.catalogs.public_status(now=2000)

        self.assertEqual("error", status["state"])
        self.assertEqual("other-host/opencode: Harness did not respond", status["error"])
        self.assertEqual(1000, status["last_refresh_at"])

    def harness(self, harness_id):
        return next(item for item in self.host["harnesses"] if item["id"] == harness_id)

    def test_a_host_whose_catalog_went_stale_is_probed_again(self):
        # herdr-mobile#45: a host asleep during the daily refresh keeps a stale
        # catalog until the next one unless its wake triggers a refresh.
        store = herdr_relay.catalogs.CatalogStore(self.db)
        with patch.object(herdr_relay.config, "PROJECTS_DB", self.db):
            store.success("buildbox", self.harness("opencode"), "1.2.3", [{"id": "openai/gpt-5"}])
            store.failure("buildbox", self.harness("opencode"), "HARNESS_UNAVAILABLE", "Harness did not respond")
            store.success("buildbox", self.harness("claude"), "1.0", [])
            self.assertTrue(herdr_relay.catalogs.host_catalog_stale(self.host, store))
            with (
                patch.object(herdr_relay.herdr, "configured_host_records", return_value=self.records()),
                patch.object(herdr_relay.herdr, "run_herdr_checked", side_effect=[
                    (True, "opencode 1.2.4"),
                    (True, "openai/gpt-5\nopenai/gpt-6\n"),
                    (True, "claude 1.0"),
                ]) as run,
            ):
                frame = herdr_relay.catalogs.refresh_stale_host("buildbox")

        self.assertEqual(3, run.call_count)
        catalog = next(item for item in frame["catalogs"] if item["harness_id"] == "opencode")
        self.assertFalse(catalog["stale"])
        self.assertIsNone(catalog["error"])
        self.assertIn("openai/gpt-6", [item["id"] for item in catalog["models"]])

    def test_a_host_never_catalogued_counts_as_stale(self):
        with patch.object(herdr_relay.config, "PROJECTS_DB", self.db):
            self.assertTrue(herdr_relay.catalogs.host_catalog_stale(self.host))

    def test_a_disabled_harness_does_not_make_a_host_stale(self):
        store = herdr_relay.catalogs.CatalogStore(self.db)
        self.harness("opencode")["enabled"] = False
        with patch.object(herdr_relay.config, "PROJECTS_DB", self.db):
            store.success("buildbox", self.harness("claude"), "1.0", [])
            self.assertFalse(herdr_relay.catalogs.host_catalog_stale(self.host, store))

    def test_a_fresh_host_is_not_probed_on_wake(self):
        store = herdr_relay.catalogs.CatalogStore(self.db)
        with (
            patch.object(herdr_relay.config, "PROJECTS_DB", self.db),
            patch.object(herdr_relay.herdr, "configured_host_records", return_value=self.records()),
            patch.object(herdr_relay.herdr, "run_herdr_checked") as run,
        ):
            store.success("buildbox", self.harness("opencode"), "1.2.3", [{"id": "openai/gpt-5"}])
            store.success("buildbox", self.harness("claude"), "1.0", [])
            self.assertIsNone(herdr_relay.catalogs.refresh_stale_host("buildbox"))
            self.assertIsNone(herdr_relay.catalogs.refresh_stale_host("not-configured"))
        run.assert_not_called()

    def test_a_wake_refresh_waits_for_a_running_refresh_and_does_not_repeat_it(self):
        # The daily refresh drops a request that finds the lock busy; a wake
        # refresh must not, or a host that wakes mid-refresh is never re-read.
        # It also must not probe again what the running refresh just fixed.
        store = herdr_relay.catalogs.CatalogStore(self.db)
        result = {}
        with (
            patch.object(herdr_relay.config, "PROJECTS_DB", self.db),
            patch.object(herdr_relay.herdr, "configured_host_records", return_value=self.records()),
            patch.object(herdr_relay.herdr, "run_herdr_checked") as run,
        ):
            herdr_relay.catalogs._refresh_lock.acquire()
            try:
                waiter = threading.Thread(
                    target=lambda: result.setdefault("frame", herdr_relay.catalogs.refresh_stale_host("buildbox"))
                )
                waiter.start()
                waiter.join(0.2)
                self.assertTrue(waiter.is_alive())
                store.success("buildbox", self.harness("opencode"), "1.2.3", [{"id": "openai/gpt-5"}])
                store.success("buildbox", self.harness("claude"), "1.0", [])
            finally:
                herdr_relay.catalogs._refresh_lock.release()
            waiter.join(5)

        self.assertFalse(waiter.is_alive())
        self.assertIsNone(result["frame"])
        run.assert_not_called()


class CatalogWakeTests(unittest.TestCase):
    """herdr-mobile#45: a host that comes back refreshes its stale catalog."""

    def setUp(self):
        for name in ("host_ready", "catalog_wake_refresh_at", "catalog_refresh_tasks"):
            stack = patch.object(herdr_relay.state, name, {})
            stack.start()
            self.addCleanup(stack.stop)
        self.refreshed = []
        self.broadcasts = []

    def refresh_stale_host(self, host_id):
        self.refreshed.append(host_id)
        return {"catalogs": [{"host_id": host_id}], "catalog_status": {"state": "success"}}

    async def observe(self, *snapshots, now=1000.0):
        async def broadcast(frame):
            self.broadcasts.append(frame)

        with (
            patch.object(herdr_relay.catalogs, "refresh_stale_host", side_effect=self.refresh_stale_host),
            patch.object(herdr_relay.transport, "broadcast", side_effect=broadcast),
        ):
            for snapshot in snapshots:
                herdr_relay.transport._refresh_woken_catalogs(snapshot, now=now)
                await asyncio.gather(*herdr_relay.state.catalog_refresh_tasks.values())

    @staticmethod
    def hosts(**statuses):
        return [{"host_id": host_id, "status": status} for host_id, status in statuses.items()]

    def test_an_offline_host_coming_back_refreshes_its_catalog_once(self):
        asyncio.run(self.observe(
            self.hosts(buildbox="offline", laptop="ready"),
            self.hosts(buildbox="ready", laptop="ready"),
            self.hosts(buildbox="ready", laptop="ready"),
        ))
        self.assertEqual(["buildbox"], self.refreshed)
        self.assertEqual(
            [{"type": "catalogs", "catalogs": [{"host_id": "buildbox"}], "catalog_status": {"state": "success"}}],
            self.broadcasts,
        )

    def test_a_host_whose_herdr_comes_back_refreshes_its_catalog(self):
        asyncio.run(self.observe(
            self.hosts(buildbox="herdr_unavailable"),
            self.hosts(buildbox="ready"),
        ))
        self.assertEqual(["buildbox"], self.refreshed)

    def test_a_host_ready_at_first_sight_is_left_to_the_startup_refresh(self):
        asyncio.run(self.observe(self.hosts(buildbox="ready"), self.hosts(buildbox="ready")))
        self.assertEqual([], self.refreshed)

    def test_a_fresh_catalog_broadcasts_nothing(self):
        self.refresh_stale_host = lambda host_id: self.refreshed.append(host_id)
        asyncio.run(self.observe(self.hosts(buildbox="offline"), self.hosts(buildbox="ready")))
        self.assertEqual(["buildbox"], self.refreshed)
        self.assertEqual([], self.broadcasts)

    def test_a_flapping_host_is_refreshed_at_most_once_per_interval(self):
        interval = herdr_relay.catalogs.WAKE_REFRESH_INTERVAL_SECONDS
        flap = (self.hosts(buildbox="offline"), self.hosts(buildbox="ready"))

        async def scenario():
            await self.observe(*flap, now=1000.0)
            await self.observe(*flap, now=1000.0 + interval - 1)
            await self.observe(*flap, now=1000.0 + interval)

        asyncio.run(scenario())
        self.assertEqual(["buildbox", "buildbox"], self.refreshed)

    def test_a_wake_seen_while_its_refresh_runs_is_not_queued_twice(self):
        release = asyncio.Event

        async def scenario():
            gate = release()
            started = []

            def slow_refresh(host_id):
                started.append(host_id)
                return None

            async def to_thread(function, *args):
                await gate.wait()
                return function(*args)

            with (
                patch.object(herdr_relay.catalogs, "refresh_stale_host", side_effect=slow_refresh),
                patch.object(herdr_relay.transport.asyncio, "to_thread", side_effect=to_thread),
            ):
                interval = herdr_relay.catalogs.WAKE_REFRESH_INTERVAL_SECONDS
                transitions = (self.hosts(buildbox="offline"), self.hosts(buildbox="ready"))
                for snapshot in transitions:
                    herdr_relay.transport._refresh_woken_catalogs(snapshot, now=1000.0)
                for snapshot in transitions:
                    herdr_relay.transport._refresh_woken_catalogs(snapshot, now=1000.0 + interval)
                gate.set()
                await asyncio.gather(*herdr_relay.state.catalog_refresh_tasks.values())
            return started

        self.assertEqual(["buildbox"], asyncio.run(scenario()))

    def test_the_poll_loop_reports_host_transitions(self):
        async def scenario():
            seen = []
            snapshots = iter([self.hosts(buildbox="offline"), self.hosts(buildbox="ready")])

            async def get_all_agents():
                return [], next(snapshots)

            async def broadcast(_frame):
                return None

            with (
                patch.object(herdr_relay.herdr, "get_all_agents", side_effect=get_all_agents),
                patch.object(herdr_relay.transport, "broadcast", side_effect=broadcast),
                patch.object(herdr_relay.operations, "public_recovery", return_value=[]),
                patch.object(herdr_relay.catalogs, "public_frame", return_value={}),
                patch.object(herdr_relay.catalogs, "refresh_stale_host", side_effect=lambda host_id: seen.append(host_id)),
            ):
                await herdr_relay._poll_once()
                await herdr_relay._poll_once()
                await asyncio.gather(*herdr_relay.state.catalog_refresh_tasks.values())
            return seen

        self.assertEqual(["buildbox"], asyncio.run(scenario()))


if __name__ == "__main__":
    unittest.main()
