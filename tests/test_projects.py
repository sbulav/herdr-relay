import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from relay import herdr_relay


class ProjectStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Path(self.directory.name) / "projects.sqlite3"
        self.root = Path(self.directory.name) / "projects"
        self.root.mkdir()
        (self.root / "nested" / "app").mkdir(parents=True)
        (self.root / "other").mkdir()
        self.host = {
            "id": "workstation",
            "display_name": "Workstation",
            "ssh": {},
            "project_roots": [str(self.root)],
            "herdr": {"wrapper": []},
            "harnesses": [],
            "power": {"wake": None, "shutdown": False},
            "readiness_timeout_seconds": 15,
        }

    def tearDown(self):
        self.directory.cleanup()

    def test_migration_is_versioned_and_project_identity_is_unique(self):
        store = herdr_relay.projects.ProjectStore(str(self.db))
        connection = sqlite3.connect(self.db)
        try:
            self.assertEqual(1, connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()
        root = herdr_relay.hosts.project_roots(self.host)[0]
        first = store.save("workstation", root["id"], str(self.root / "nested"), "Nested")
        archived = store.archive(first["project_id"], True)
        restored = store.save("workstation", root["id"], str(self.root / "nested"), "Renamed")

        self.assertEqual(first["project_id"], restored["project_id"])
        self.assertEqual("Renamed", restored["label"])
        self.assertEqual(0, restored["archived"])
        self.assertEqual(1, archived["archived"])

    def test_reconciliation_marks_configuration_orphans_without_deleting_rows(self):
        store = herdr_relay.projects.ProjectStore(str(self.db))
        root = herdr_relay.hosts.project_roots(self.host)[0]
        project = store.save("workstation", root["id"], str(self.root / "nested"), "Nested")

        store.reconcile(set(), set())

        row = store.get(project["project_id"])
        self.assertEqual(0, row["available"])
        self.assertEqual("HOST_NOT_CONFIGURED", row["unavailable_reason"])
        self.assertIsNotNone(row)

    def test_safe_browse_is_nested_and_rejects_symlink_escape(self):
        outside = Path(self.directory.name) / "outside"
        outside.mkdir()
        (outside / "secret").mkdir()
        os.symlink(outside, self.root / "escape", target_is_directory=True)

        nested = herdr_relay.project_fs.browse_local(str(self.root), ["nested"])
        self.assertEqual(["app"], [entry["name"] for entry in nested["entries"]])
        self.assertEqual([], [entry["name"] for entry in herdr_relay.project_fs.browse_local(str(self.root), [])["entries"] if entry["name"] == "escape"])
        with self.assertRaises(herdr_relay.project_fs.FilesystemError) as error:
            herdr_relay.project_fs.browse_local(str(self.root), ["escape", "secret"])
        self.assertEqual("PATH_NOT_ALLOWED", error.exception.code)
        with self.assertRaises(herdr_relay.project_fs.FilesystemError):
            herdr_relay.project_fs.browse_local(str(self.root), [".."])

    @patch.object(herdr_relay.project_fs.subprocess, "run")
    @patch.object(herdr_relay.project_fs.hosts, "ssh_target", return_value="deploy@workstation")
    def test_remote_browse_executes_the_decoded_helper(self, ssh_target, run):
        run.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps({"ok": True, "canonical_path": "/srv/projects/nested", "entries": []}), ""
        )

        result = herdr_relay.project_fs.browse_remote(self.host, "/srv/projects", ["nested"])

        self.assertEqual("/srv/projects/nested", result["canonical_path"])
        command = run.call_args.args[0]
        self.assertIn("python3 -c", command[-1])
        self.assertIn("urlsafe_b64decode", command[-1])
        helper = subprocess.Popen(
            ["sh", "-c", command[-1]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = helper.communicate(
            json.dumps({"root": str(self.root), "components": ["nested"]})
        )
        self.assertEqual(0, helper.returncode, stderr)
        self.assertEqual("/".join((str(self.root), "nested")), json.loads(stdout)["canonical_path"])

    def test_project_launch_names_are_unique(self):
        root = herdr_relay.hosts.project_roots(self.host)[0]
        canonical_path = str(self.root / "nested")
        project = {
            "project_id": "0123456789abcdef0123456789abcdef",
            "host_id": "workstation",
            "root_id": root["id"],
            "canonical_path": canonical_path,
            "archived": 0,
            "available": 1,
        }
        store = Mock()
        store.get.return_value = project
        with (
            patch.object(herdr_relay.hosts, "HOSTS_BY_ID", {"workstation": self.host}),
            patch.object(herdr_relay.projects, "store", return_value=store),
            patch.object(herdr_relay.project_fs, "browse", return_value={"canonical_path": canonical_path}),
            patch.object(herdr_relay.herdr, "run_herdr_checked", return_value=(True, "")) as run,
        ):
            first = herdr_relay.lifecycle._launch_project({
                "type": "launch_session",
                "request_id": "launch-1",
                "project_id": project["project_id"],
                "host_id": "workstation",
            })
            second = herdr_relay.lifecycle._launch_project({
                "type": "launch_session",
                "request_id": "launch-2",
                "project_id": project["project_id"],
                "host_id": "workstation",
            })

        self.assertEqual("command_ack", first["type"])
        self.assertEqual("command_ack", second["type"])
        names = [call.args[2] for call in run.call_args_list]
        self.assertEqual(2, len(names))
        self.assertNotEqual(names[0], names[1])
        self.assertTrue(names[0].startswith("mobile-project-0123456789ab-"))

    def test_typed_save_and_remove_never_delete_directory(self):
        root = herdr_relay.hosts.project_roots(self.host)[0]
        with (
            patch.object(herdr_relay.config, "PROJECTS_DB", str(self.db)),
            patch.object(herdr_relay.hosts, "HOSTS_BY_ID", {"workstation": self.host}),
        ):
            saved = herdr_relay.projects.handle_command({
                "type": "project_save",
                "request_id": "save-1",
                "host_id": "workstation",
                "root_id": root["id"],
                "path": ["nested", "app"],
                "label": "App",
            })
            project_id = saved["result"]["project"]["project_id"]
            removed = herdr_relay.projects.handle_command({
                "type": "project_remove",
                "request_id": "remove-1",
                "project_id": project_id,
            })

        self.assertEqual("command_ack", removed["type"])
        self.assertTrue((self.root / "nested" / "app").is_dir())
        self.assertTrue(removed["result"]["project"]["archived"])

    def test_public_snapshot_withholds_root_paths(self):
        root = herdr_relay.hosts.project_roots(self.host)[0]
        with (
            patch.object(herdr_relay.config, "PROJECTS_DB", str(self.db)),
            patch.object(herdr_relay.hosts, "HOSTS_BY_ID", {"workstation": self.host}),
        ):
            herdr_relay.projects.ProjectStore(str(self.db)).save(
                "workstation", root["id"], str(self.root / "nested"), "Nested"
            )
            frame = herdr_relay.projects.public_snapshot()

        encoded = json.dumps(frame)
        self.assertIn("projects", frame)
        self.assertIn(root["id"], encoded)
        self.assertNotIn(str(self.root), json.dumps(frame["roots"]))


if __name__ == "__main__":
    unittest.main()
