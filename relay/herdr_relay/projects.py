"""Relay-owned saved projects, migrations, and typed project operations."""
import re
import sqlite3
import time
import uuid
import os

from . import config, hosts, project_fs


PROJECT_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
HOST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
MAX_QUERY_LENGTH = 128
MAX_LABEL_LENGTH = 128


class ProjectError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _now_ms():
    return int(time.time() * 1000)


def _label(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_LABEL_LENGTH:
        raise ProjectError("INVALID_LABEL", "Project label must be 1-128 characters")
    label = value.strip()
    if any(ord(char) < 32 for char in label):
        raise ProjectError("INVALID_LABEL", "Project label contains a control character")
    return label


def _request_id(msg):
    value = msg.get("request_id") if isinstance(msg, dict) else None
    if not isinstance(value, str) or not REQUEST_ID_RE.fullmatch(value):
        raise ProjectError("INVALID_REQUEST", "request_id is required")
    return value


def _host(host_id):
    if not isinstance(host_id, str) or not HOST_ID_RE.fullmatch(host_id):
        raise ProjectError("INVALID_REQUEST", "host_id is invalid")
    host = hosts.HOSTS_BY_ID.get(host_id)
    if host is None:
        raise ProjectError("UNKNOWN_HOST", "Unknown host")
    return host


def _root(host, root_id):
    if not isinstance(root_id, str) or not root_id.startswith("root_"):
        raise ProjectError("INVALID_REQUEST", "root_id is invalid")
    root = hosts.project_root(host, root_id)
    if root is None:
        raise ProjectError("ROOT_NOT_ALLOWED", "Folder root is not configured for this host")
    return root


def _path(msg):
    if "path" not in msg:
        raise ProjectError("INVALID_PATH", "Invalid folder path")
    try:
        return project_fs.validate_components(msg.get("path", []))
    except project_fs.FilesystemError as error:
        raise ProjectError(error.code, str(error)) from error


class ProjectStore:
    """A small per-operation SQLite store so commands are safe across worker threads."""

    def __init__(self, path):
        self.path = path
        self._migrate()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _migrate(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            migrations = {
                1: """
                    CREATE TABLE projects (
                        project_id TEXT PRIMARY KEY,
                        host_id TEXT NOT NULL,
                        root_id TEXT NOT NULL,
                        canonical_path TEXT NOT NULL,
                        label TEXT NOT NULL,
                        archived INTEGER NOT NULL DEFAULT 0,
                        available INTEGER NOT NULL DEFAULT 1,
                        unavailable_reason TEXT,
                        last_launch_at INTEGER,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        UNIQUE(host_id, canonical_path)
                    );
                    CREATE INDEX projects_recent_idx
                        ON projects(archived, last_launch_at DESC, updated_at DESC);
                    """,
            }
            if version > max(migrations):
                raise RuntimeError("unsupported project database schema version")
            for target in range(version + 1, max(migrations) + 1):
                try:
                    connection.executescript(
                        f"BEGIN;\n{migrations[target]}\nPRAGMA user_version = {target};\nCOMMIT;"
                    )
                except Exception:
                    connection.rollback()
                    raise
        finally:
            connection.close()

    @staticmethod
    def _row(row):
        return dict(row) if row is not None else None

    def get(self, project_id):
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            return self._row(row)
        finally:
            connection.close()

    def list(self, query=""):
        query = query.strip().casefold()
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM projects
                ORDER BY archived ASC,
                         CASE WHEN last_launch_at IS NULL THEN 1 ELSE 0 END,
                         last_launch_at DESC,
                         updated_at DESC,
                         label COLLATE NOCASE ASC
                LIMIT 500
                """
            ).fetchall()
            if not query:
                return [self._row(row) for row in rows]
            return [
                self._row(row)
                for row in rows
                if query in row["label"].casefold() or query in row["canonical_path"].casefold()
            ]
        finally:
            connection.close()

    def save(self, host_id, root_id, canonical_path, label):
        label = _label(label)
        now = _now_ms()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT project_id FROM projects WHERE host_id = ? AND canonical_path = ?",
                (host_id, canonical_path),
            ).fetchone()
            if row:
                project_id = row[0]
                connection.execute(
                    """
                    UPDATE projects
                    SET root_id = ?, label = ?, archived = 0, available = 1,
                        unavailable_reason = NULL, updated_at = ?
                    WHERE project_id = ?
                    """,
                    (root_id, label, now, project_id),
                )
            else:
                project_id = uuid.uuid4().hex
                connection.execute(
                    """
                    INSERT INTO projects(
                        project_id, host_id, root_id, canonical_path, label,
                        archived, available, unavailable_reason, last_launch_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 0, 1, NULL, NULL, ?, ?)
                    """,
                    (project_id, host_id, root_id, canonical_path, label, now, now),
                )
            connection.commit()
            return self._row(
                connection.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,)).fetchone()
            )
        finally:
            connection.close()

    def rename(self, project_id, label):
        label = _label(label)
        now = _now_ms()
        connection = self._connect()
        try:
            updated = connection.execute(
                "UPDATE projects SET label = ?, updated_at = ? WHERE project_id = ?",
                (label, now, project_id),
            ).rowcount
            if not updated:
                raise ProjectError("PROJECT_NOT_FOUND", "Project not found")
            connection.commit()
            return self._row(connection.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,)).fetchone())
        finally:
            connection.close()

    def archive(self, project_id, archived):
        now = _now_ms()
        connection = self._connect()
        try:
            updated = connection.execute(
                "UPDATE projects SET archived = ?, updated_at = ? WHERE project_id = ?",
                (1 if archived else 0, now, project_id),
            ).rowcount
            if not updated:
                raise ProjectError("PROJECT_NOT_FOUND", "Project not found")
            connection.commit()
            return self._row(connection.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,)).fetchone())
        finally:
            connection.close()

    def mark_launch(self, project_id):
        now = _now_ms()
        connection = self._connect()
        try:
            updated = connection.execute(
                "UPDATE projects SET last_launch_at = ?, updated_at = ? WHERE project_id = ?",
                (now, now, project_id),
            ).rowcount
            if not updated:
                raise ProjectError("PROJECT_NOT_FOUND", "Project not found")
            connection.commit()
        finally:
            connection.close()

    def reconcile(self, host_ids, root_ids):
        """Keep records when configuration changes, but make unavailable state explicit."""
        connection = self._connect()
        try:
            for row in connection.execute("SELECT project_id, host_id, root_id, available, unavailable_reason FROM projects"):
                project_id, host_id, root_id = row[0], row[1], row[2]
                if host_id not in host_ids:
                    available, reason = 0, "HOST_NOT_CONFIGURED"
                elif (host_id, root_id) not in root_ids:
                    available, reason = 0, "ROOT_NOT_CONFIGURED"
                else:
                    available, reason = 1, None
                if row[3] != available or row[4] != reason:
                    connection.execute(
                        "UPDATE projects SET available = ?, unavailable_reason = ?, updated_at = ? WHERE project_id = ?",
                        (available, reason, _now_ms(), project_id),
                    )
            connection.commit()
        finally:
            connection.close()


def _configured_roots():
    configured_hosts = list(hosts.HOSTS_BY_ID.values())
    root_ids = set()
    roots = []
    for host in configured_hosts:
        for root in hosts.project_roots(host):
            root_ids.add((host["id"], root["id"]))
            roots.append({"host_id": host["id"], **root})
    return {host["id"] for host in configured_hosts}, root_ids, roots


def store():
    return ProjectStore(config.PROJECTS_DB)


def public_project(row):
    return {
        "project_id": row["project_id"],
        "host_id": row["host_id"],
        "root_id": row["root_id"],
        "label": row["label"],
        "path": row["canonical_path"],
        "canonical_path": row["canonical_path"],
        "archived": bool(row["archived"]),
        "available": bool(row["available"]),
        "unavailable_reason": row["unavailable_reason"],
        "last_launch_at": row["last_launch_at"],
    }


def public_snapshot(query="", request_id=None):
    host_ids, root_ids, configured_roots = _configured_roots()
    saved = store()
    saved.reconcile(host_ids, root_ids)
    frame = {
        "type": "projects",
        "projects": [public_project(row) for row in saved.list(query)],
        "roots": [
            {"host_id": root["host_id"], "root_id": root["id"], "label": root["label"]}
            for root in configured_roots
        ],
    }
    if request_id is not None:
        frame["request_id"] = request_id
    return frame


def _project(store_instance, project_id):
    if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
        raise ProjectError("INVALID_REQUEST", "project_id is invalid")
    row = store_instance.get(project_id)
    if row is None:
        raise ProjectError("PROJECT_NOT_FOUND", "Project not found")
    return row


def browse(msg):
    request_id = _request_id(msg)
    host = _host(msg.get("host_id"))
    root = _root(host, msg.get("root_id"))
    components = _path(msg)
    try:
        result = project_fs.browse(host, root["path"], components)
    except project_fs.FilesystemError as error:
        raise ProjectError(error.code, str(error)) from error
    return {
        "type": "folder_entries",
        "request_id": request_id,
        "host_id": host["id"],
        "root_id": root["id"],
        "path": components,
        "canonical_path": result["canonical_path"],
        "entries": result["entries"],
    }


def list_projects(msg):
    request_id = _request_id(msg)
    query = msg.get("query", "")
    if not isinstance(query, str) or len(query) > MAX_QUERY_LENGTH:
        raise ProjectError("INVALID_REQUEST", "query is too long")
    return public_snapshot(query, request_id)


def save(msg):
    request_id = _request_id(msg)
    host = _host(msg.get("host_id"))
    root = _root(host, msg.get("root_id"))
    components = _path(msg)
    try:
        result = project_fs.browse(host, root["path"], components)
    except project_fs.FilesystemError as error:
        raise ProjectError(error.code, str(error)) from error
    label = msg.get("label")
    if label is None:
        label = components[-1] if components else root["label"]
    row = store().save(host["id"], root["id"], result["canonical_path"], label)
    return {
        "type": "command_ack",
        "request_id": request_id,
        "result": {"project": public_project(row)},
    }


def rename(msg):
    request_id = _request_id(msg)
    project_id = msg.get("project_id")
    row = _project(store(), project_id)
    updated = store().rename(row["project_id"], msg.get("label"))
    return {"type": "command_ack", "request_id": request_id, "result": {"project": public_project(updated)}}


def remove(msg):
    request_id = _request_id(msg)
    row = _project(store(), msg.get("project_id"))
    updated = store().archive(row["project_id"], True)
    return {"type": "command_ack", "request_id": request_id, "result": {"project": public_project(updated)}}


def restore(msg):
    request_id = _request_id(msg)
    row = _project(store(), msg.get("project_id"))
    updated = store().archive(row["project_id"], False)
    return {"type": "command_ack", "request_id": request_id, "result": {"project": public_project(updated)}}


COMMANDS = {
    "project_browse": browse,
    "project_list": list_projects,
    "project_save": save,
    "project_rename": rename,
    "project_remove": remove,
    "project_restore": restore,
}


def handle_command(msg):
    if not isinstance(msg, dict):
        return {"type": "command_error", "request_id": None, "code": "INVALID_REQUEST", "message": "Request must be an object"}
    handler = COMMANDS.get(msg.get("type"))
    if handler is None:
        return None
    try:
        return handler(msg)
    except ProjectError as error:
        request_id = msg.get("request_id") if isinstance(msg.get("request_id"), str) else None
        return {
            "type": "command_error",
            "request_id": request_id,
            "code": error.code,
            "message": error.message,
        }
