import json
import tempfile
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


if __name__ == "__main__":
    unittest.main()
