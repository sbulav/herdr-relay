import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
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
            self.assertEqual(max(herdr_relay.projects.MIGRATIONS), connection.execute("PRAGMA user_version").fetchone()[0])
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
        self.assertEqual(os.path.realpath(self.root / "nested"), json.loads(stdout)["canonical_path"])

    def test_migration_is_safe_when_stores_start_concurrently(self):
        barrier = threading.Barrier(4, timeout=2)

        def open_store(_index):
            barrier.wait()
            return herdr_relay.projects.ProjectStore(str(self.db)).list()

        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual([[], [], [], []], list(pool.map(open_store, range(4))))
        connection = sqlite3.connect(self.db)
        try:
            self.assertEqual(max(herdr_relay.projects.MIGRATIONS), connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(
                {"projects", "project_requests"},
                {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('projects', 'project_requests')"
                    )
                },
            )
        finally:
            connection.close()

    def test_name_validation_is_portable_and_does_not_over_reject(self):
        invalid_paths = ["", ".", "..", "a/b", "a\\b"]
        invalid_names = [" lead", "trail ", "trail.", "a?b", "CON", "com1.txt", "x" * 256]
        for value in invalid_paths:
            with self.subTest(value=value), self.assertRaises(herdr_relay.project_fs.FilesystemError) as error:
                herdr_relay.project_fs.validate_name(value)
            self.assertEqual("INVALID_PATH", error.exception.code)
        for value in invalid_names:
            with self.subTest(value=value), self.assertRaises(herdr_relay.project_fs.FilesystemError) as error:
                herdr_relay.project_fs.validate_name(value)
            self.assertEqual("INVALID_NAME", error.exception.code)
        for value in ["my-project", "проект", ".hidden", "CONSOLE", "COM10", "two words"]:
            self.assertEqual(value, herdr_relay.project_fs.validate_name(value))

    def test_local_create_makes_exactly_one_empty_folder_and_rejects_collision(self):
        result = herdr_relay.project_fs.create_local(str(self.root), ["nested"], "fresh")

        created = self.root / "nested" / "fresh"
        self.assertTrue(created.is_dir())
        self.assertEqual([], list(created.iterdir()))
        self.assertEqual(os.path.realpath(created), result["canonical_path"])
        with self.assertRaises(herdr_relay.project_fs.FilesystemError) as collision:
            herdr_relay.project_fs.create_local(str(self.root), ["nested"], "fresh")
        self.assertEqual("FOLDER_EXISTS", collision.exception.code)

    def test_create_rejects_symlink_parent_and_rolls_back_on_containment_race(self):
        outside = Path(self.directory.name) / "outside-create"
        outside.mkdir()
        os.symlink(outside, self.root / "escape-create", target_is_directory=True)
        with self.assertRaises(herdr_relay.project_fs.FilesystemError) as symlink:
            herdr_relay.project_fs.create_local(str(self.root), ["escape-create"], "fresh")
        self.assertEqual("PATH_NOT_ALLOWED", symlink.exception.code)
        self.assertFalse((outside / "fresh").exists())

        with patch.object(
            herdr_relay.project_fs,
            "_fd_path",
            side_effect=[os.path.realpath(self.root), herdr_relay.project_fs.FilesystemError("PATH_NOT_ALLOWED")],
        ):
            with self.assertRaises(herdr_relay.project_fs.FilesystemError):
                herdr_relay.project_fs.create_local(str(self.root), [], "raced")
        self.assertFalse((self.root / "raced").exists())

    @patch.object(herdr_relay.project_fs.subprocess, "run")
    @patch.object(herdr_relay.project_fs.hosts, "ssh_target", return_value="deploy@workstation")
    def test_remote_create_uses_the_same_fixed_helper(self, _ssh_target, run):
        run.return_value = subprocess.CompletedProcess(
            [], 0, json.dumps({"ok": True, "canonical_path": "/srv/projects/fresh"}), ""
        )
        result = herdr_relay.project_fs.create_remote(self.host, "/srv/projects", [], "fresh")

        self.assertEqual("/srv/projects/fresh", result["canonical_path"])
        command = run.call_args.args[0]
        helper = subprocess.Popen(
            ["sh", "-c", command[-1]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = helper.communicate(
            json.dumps({"op": "create", "root": str(self.root), "components": ["nested"], "name": "remote-fresh"})
        )
        self.assertEqual(0, helper.returncode, stderr)
        self.assertTrue(json.loads(stdout)["ok"])
        self.assertTrue((self.root / "nested" / "remote-fresh").is_dir())

    def test_create_is_durable_and_idempotent_across_store_instances(self):
        root = herdr_relay.hosts.project_roots(self.host)[0]
        message = {
            "type": "project_create",
            "request_id": "req-create-1",
            "host_id": "workstation",
            "root_id": root["id"],
            "path": ["nested"],
            "name": "durable",
        }
        with (
            patch.object(herdr_relay.config, "PROJECTS_DB", str(self.db)),
            patch.object(herdr_relay.hosts, "HOSTS_BY_ID", {"workstation": self.host}),
        ):
            first = herdr_relay.projects.handle_command(message)
            replay = herdr_relay.projects.handle_command(message)
            collision = herdr_relay.projects.handle_command({**message, "request_id": "req-create-2"})

        self.assertTrue(first["result"]["created"])
        self.assertFalse(replay["result"]["created"])
        self.assertEqual(first["result"]["project"]["project_id"], replay["result"]["project"]["project_id"])
        self.assertEqual("FOLDER_EXISTS", collision["code"])
        self.assertEqual([], list((self.root / "nested" / "durable").iterdir()))
        connection = sqlite3.connect(self.db)
        try:
            self.assertEqual(1, connection.execute("SELECT count(*) FROM projects").fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT count(*) FROM project_requests WHERE status = 'completed'").fetchone()[0])
        finally:
            connection.close()

    def test_concurrent_duplicate_request_is_reported_in_flight(self):
        saved = herdr_relay.projects.ProjectStore(str(self.db))
        self.assertIsNone(saved.begin_create("req-concurrent"))
        with self.assertRaises(herdr_relay.projects.ProjectError) as duplicate:
            herdr_relay.projects.ProjectStore(str(self.db)).begin_create("req-concurrent")
        self.assertEqual("REQUEST_IN_FLIGHT", duplicate.exception.code)

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
