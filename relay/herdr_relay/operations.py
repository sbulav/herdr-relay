"""Durable, idempotent start operations.

The relay owns the operation rather than the WebSocket connection. A request
may therefore be acknowledged, lose its client, and continue to a single
deterministic Herdr agent. The SQLite row is also the recovery journal: a
relay restart checks for that agent name before it ever sends another start.
"""
import os
import sqlite3
import threading
import time
import uuid

from . import catalogs, config, herdr, hosts, project_fs, projects, protocol, state


ACTIVE_STAGES = {"queued", "checking", "starting"}
TERMINAL_STAGES = {"started", "failed", "cancelled"}
MAX_HARNESS_LENGTH = 64
MAX_MODEL_LENGTH = 256
_worker_lock = threading.Lock()
_running = set()


class OperationStore:
    """SQLite access for one start operation at a time per transaction."""

    def __init__(self, path=None):
        # ProjectStore owns the shared, versioned migration lock. Calling it
        # here also makes an operations-only process safe on an empty database.
        self.path = path or config.PROJECTS_DB
        projects.ProjectStore(self.path)

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _row(row):
        return dict(row) if row is not None else None

    def get(self, operation_id):
        connection = self._connect()
        try:
            return self._row(connection.execute(
                "SELECT * FROM start_operations WHERE operation_id = ?", (operation_id,)
            ).fetchone())
        finally:
            connection.close()

    def by_request(self, request_id):
        connection = self._connect()
        try:
            return self._row(connection.execute(
                "SELECT * FROM start_operations WHERE request_id = ?", (request_id,)
            ).fetchone())
        finally:
            connection.close()

    def active(self):
        connection = self._connect()
        try:
            return [self._row(row) for row in connection.execute(
                "SELECT * FROM start_operations WHERE stage IN ('queued', 'checking', 'starting') "
                "ORDER BY created_at ASC"
            ).fetchall()]
        finally:
            connection.close()

    def begin(self, request_id, host_id, project_id, harness, model):
        now = _now_ms()
        operation_id = uuid.uuid4().hex
        agent_name = deterministic_agent_name(operation_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM start_operations WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._row(existing), False
            connection.execute(
                """
                INSERT INTO start_operations(
                    operation_id, request_id, host_id, project_id, harness, model,
                    agent_name, stage, session_id, error_code, error_message,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', NULL, NULL, NULL, ?, ?)
                """,
                (operation_id, request_id, host_id, project_id, harness, model, agent_name, now, now),
            )
            connection.commit()
            return self.get(operation_id), True
        except sqlite3.IntegrityError:
            connection.rollback()
            existing = self._row(connection.execute(
                "SELECT * FROM start_operations WHERE request_id = ?", (request_id,)
            ).fetchone())
            if existing is not None:
                return existing, False
            raise
        finally:
            connection.close()

    def update(self, operation_id, stage, session_id=None, error_code=None, error_message=None):
        now = _now_ms()
        connection = self._connect()
        try:
            connection.execute(
                """
                UPDATE start_operations
                SET stage = ?, session_id = COALESCE(?, session_id),
                    error_code = ?, error_message = ?, updated_at = ?
                WHERE operation_id = ?
                """,
                (stage, session_id, error_code, error_message, now, operation_id),
            )
            connection.commit()
            return self._row(connection.execute(
                "SELECT * FROM start_operations WHERE operation_id = ?", (operation_id,)
            ).fetchone())
        finally:
            connection.close()


def _now_ms():
    return int(time.time() * 1000)


def deterministic_agent_name(operation_id):
    """Return the only Herdr name an operation is ever allowed to use."""
    return f"herdr-mobile-{operation_id}"


def public_operation(row):
    """Project an operation without subprocess output or private paths."""
    return {
        "operation_id": row["operation_id"],
        "request_id": row["request_id"],
        "host_id": row["host_id"],
        "project_id": row["project_id"],
        "harness": row["harness"],
        "model": row["model"],
        "agent_name": row["agent_name"],
        "stage": row["stage"],
        "session_id": row["session_id"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def public_active():
    return [public_operation(row) for row in OperationStore().active()]


def _emit(row):
    # `event_queue` is also the relay's plugin queue. Keeping operation changes
    # there gives both thread workers and the asyncio transport one bounded
    # hand-off point.
    event = {"type": "operation_event", "operation": public_operation(row)}
    loop = state.event_loop
    if loop is not None and not loop.is_closed():
        loop.call_soon_threadsafe(state.event_queue.put_nowait, event)
    else:
        state.event_queue.put_nowait(event)


def _transition(store, operation_id, stage, **kwargs):
    row = store.update(operation_id, stage, **kwargs)
    if row is not None:
        _emit(row)
    return row


ERROR_MESSAGES = {
    "CONFIGURATION_CHANGED": "The selected project configuration is no longer available",
    "HOST_OFFLINE": "The selected host is offline",
    "HERDR_UNAVAILABLE": "Herdr is unavailable on the selected host",
    "LAUNCH_FAILED": "Herdr did not start the client",
    "AGENT_NOT_OBSERVABLE": "Herdr did not expose a recoverable agent identity",
    "DUPLICATE_AGENT": "The host reported more than one matching agent",
}


def _error(store, operation_id, code):
    return _transition(
        store,
        operation_id,
        "failed",
        error_code=code,
        error_message=ERROR_MESSAGES[code],
    )


def _valid_selection(harness, model):
    return (
        isinstance(harness, str)
        and 1 <= len(harness) <= MAX_HARNESS_LENGTH
        and all(char.isalnum() or char in "._-" for char in harness)
        and isinstance(model, str)
        and 1 <= len(model) <= MAX_MODEL_LENGTH
        and not any(ord(char) < 32 or ord(char) == 127 for char in model)
    )


def _project_context(operation):
    saved = projects.store().get(operation["project_id"])
    configured_host = hosts.HOSTS_BY_ID.get(operation["host_id"])
    if (
        saved is None
        or saved["archived"]
        or not saved["available"]
        or configured_host is None
        or saved["host_id"] != operation["host_id"]
    ):
        return None
    root = hosts.project_root(configured_host, saved["root_id"])
    if root is None:
        return None
    try:
        relative = os.path.relpath(saved["canonical_path"], root["path"])
        components = [] if relative == "." else relative.split(os.sep)
        current = project_fs.browse(configured_host, root["path"], components)["canonical_path"]
    except (ValueError, project_fs.FilesystemError):
        return None
    if current != saved["canonical_path"]:
        return None
    configured_harnesses = {item["id"]: item for item in configured_host.get("harnesses", [])}
    harness = configured_harnesses.get(operation["harness"])
    if configured_harnesses and harness is None:
        return None
    if configured_harnesses:
        catalog = catalogs.CatalogStore().get(operation["host_id"], operation["harness"])
        if not catalog or catalog.get("disabled") or not catalog.get("available"):
            return None
        if operation["model"] != "default" and operation["model"] not in {
            item.get("id") for item in catalog.get("models", []) if isinstance(item, dict)
        }:
            return None
    return saved, configured_host


def _probe_existing(operation, host):
    result = herdr.get_agents_from_host(host=host)
    if not isinstance(result, tuple) or len(result) != 2:
        return [], {"ssh_reachable": False, "herdr_ready": False}
    agents, probe = result
    matches = [
        agent for agent in agents
        if agent.get("agent_name") == operation["agent_name"]
        or agent.get("name") == operation["agent_name"]
    ]
    return matches, probe


def _start_operation(operation_id):
    store = OperationStore()
    operation = store.get(operation_id)
    if operation is None or operation["stage"] in TERMINAL_STAGES:
        return

    context = _project_context(operation)
    if context is None:
        _error(store, operation_id, "CONFIGURATION_CHANGED")
        return
    saved, host = context
    _transition(store, operation_id, "checking")
    matches, probe = _probe_existing(operation, host)
    if len(matches) > 1:
        _error(store, operation_id, "DUPLICATE_AGENT")
        return
    if len(matches) == 1:
        session_id = protocol.session_id(host["id"], matches[0]["pane_id"])
        projects.store().mark_launch(operation["project_id"])
        _transition(store, operation_id, "started", session_id=session_id)
        return
    if not probe.get("ssh_reachable"):
        _error(store, operation_id, "HOST_OFFLINE")
        return
    if not probe.get("herdr_ready"):
        _error(store, operation_id, "HERDR_UNAVAILABLE")
        return

    _transition(store, operation_id, "starting")
    remote = hosts.ssh_target(host)
    command = hosts.herdr_command(host)
    argv = [operation["harness"]]
    if operation["model"] != "default":
        argv.extend(["--model", operation["model"]])
    success, _output = herdr.run_herdr_checked(
        "agent", "start", operation["agent_name"],
        "--cwd", saved["canonical_path"], "--no-focus", "--", *argv,
        remote=remote,
        host_id=host["id"],
        command=command,
        timeout=host.get("readiness_timeout_seconds", 15),
    )
    if not success:
        _error(store, operation_id, "LAUNCH_FAILED")
        return

    # A successful process exit is not enough. The pane must expose the
    # deterministic name before the operation can become started; otherwise a
    # relay restart could not tell an existing agent from a second one.
    deadline = time.monotonic() + host.get("readiness_timeout_seconds", 15)
    while time.monotonic() < deadline:
        matches, _probe = _probe_existing(operation, host)
        if len(matches) > 1:
            _error(store, operation_id, "DUPLICATE_AGENT")
            return
        if len(matches) == 1:
            session_id = protocol.session_id(host["id"], matches[0]["pane_id"])
            projects.store().mark_launch(operation["project_id"])
            _transition(store, operation_id, "started", session_id=session_id)
            return
        time.sleep(0.1)
    _error(store, operation_id, "AGENT_NOT_OBSERVABLE")


def ensure_worker(operation_id):
    with _worker_lock:
        if operation_id in _running:
            return False
        _running.add(operation_id)
    thread = threading.Thread(
        target=_run_worker,
        args=(operation_id,),
        daemon=True,
        name=f"start-{operation_id[:8]}",
    )
    thread.start()
    return True


def _run_worker(operation_id):
    try:
        _start_operation(operation_id)
    except Exception:
        # The public operation never carries exception text. A failure in
        # validation, SQLite, or a host helper gets the same stable message as
        # any other failed start.
        try:
            _error(OperationStore(), operation_id, "LAUNCH_FAILED")
        except Exception:
            pass
    finally:
        with _worker_lock:
            _running.discard(operation_id)


def recover_active():
    for row in OperationStore().active():
        ensure_worker(row["operation_id"])


def begin_start(msg):
    request_id = msg.get("request_id")
    if not isinstance(request_id, str) or not projects.REQUEST_ID_RE.fullmatch(request_id):
        return protocol.command_error(None, "INVALID_REQUEST", "request_id is required")
    existing = OperationStore().by_request(request_id)
    if existing is not None:
        ensure_worker(existing["operation_id"])
        return {
            "type": "command_ack",
            "request_id": request_id,
            "result": {"operation": public_operation(existing)},
        }
    project_id = msg.get("project_id")
    host_id = msg.get("host_id")
    harness = msg.get("harness")
    model = msg.get("model", "default")
    if not isinstance(project_id, str) or not projects.PROJECT_ID_RE.fullmatch(project_id):
        return protocol.command_error(request_id, "INVALID_REQUEST", "project_id is invalid")
    if not isinstance(host_id, str) or not projects.HOST_ID_RE.fullmatch(host_id):
        return protocol.command_error(request_id, "INVALID_REQUEST", "host_id is invalid")
    if hosts.HOSTS_BY_ID.get(host_id) is None:
        return protocol.command_error(request_id, "UNKNOWN_HOST", "Unknown host")
    if not _valid_selection(harness, model):
        return protocol.command_error(request_id, "INVALID_REQUEST", "Invalid harness or model")
    if _project_context({"project_id": project_id, "host_id": host_id, "harness": harness, "model": model}) is None:
        return protocol.command_error(
            request_id,
            "CONFIGURATION_CHANGED",
            ERROR_MESSAGES["CONFIGURATION_CHANGED"],
        )
    operation, _created = OperationStore().begin(request_id, host_id, project_id, harness, model)
    ensure_worker(operation["operation_id"])
    return {
        "type": "command_ack",
        "request_id": request_id,
        "result": {"operation": public_operation(operation)},
    }
