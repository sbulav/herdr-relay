"""Harness probes and durable, host-scoped model catalogs.

Catalog discovery is deliberately kept outside the launch path.  A relay can
therefore keep serving the last known choices while a host is asleep or a CLI
is being upgraded.  Only the configured command and fixed adapter arguments
ever reach a host; clients can select IDs but cannot supply commands.
"""
import json
import re
import sqlite3
import threading
import time

from . import config, herdr, hosts


REFRESH_INTERVAL_MS = 24 * 60 * 60 * 1000
MAX_MODELS = 500
MODEL_ID_RE = re.compile(r"[^\x00-\x1f]{1,256}\Z")

_refresh_lock = threading.Lock()


def _now_ms():
    return int(time.time() * 1000)


def _connection(path=None):
    connection = sqlite3.connect(path or config.PROJECTS_DB, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _ensure_schema(connection):
    # ProjectStore owns PRAGMA user_version and creates this table in its v3
    # migration.  IF NOT EXISTS keeps the catalog module independently usable in
    # focused tests and during a rolling upgrade from an older database.
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS model_catalogs (
            host_id TEXT NOT NULL,
            harness_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            available INTEGER NOT NULL DEFAULT 0,
            version TEXT,
            models_json TEXT NOT NULL DEFAULT '[]',
            stale INTEGER NOT NULL DEFAULT 1,
            disabled INTEGER NOT NULL DEFAULT 0,
            last_success_at INTEGER,
            updated_at INTEGER NOT NULL,
            error TEXT,
            PRIMARY KEY(host_id, harness_id)
        );
        CREATE TABLE IF NOT EXISTS catalog_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )


class CatalogStore:
    def __init__(self, path=None):
        self.path = path or config.PROJECTS_DB
        connection = _connection(self.path)
        try:
            _ensure_schema(connection)
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _row(row):
        if row is None:
            return None
        result = dict(row)
        try:
            result["models"] = json.loads(result.pop("models_json"))
        except (TypeError, json.JSONDecodeError):
            result["models"] = []
        return result

    def get(self, host_id, harness_id):
        connection = _connection(self.path)
        try:
            return self._row(connection.execute(
                "SELECT * FROM model_catalogs WHERE host_id = ? AND harness_id = ?",
                (host_id, harness_id),
            ).fetchone())
        finally:
            connection.close()

    def list(self):
        connection = _connection(self.path)
        try:
            return [self._row(row) for row in connection.execute(
                "SELECT * FROM model_catalogs ORDER BY host_id, harness_id"
            ).fetchall()]
        finally:
            connection.close()

    def metadata(self):
        connection = _connection(self.path)
        try:
            return {
                row[0]: row[1]
                for row in connection.execute("SELECT key, value FROM catalog_meta")
            }
        finally:
            connection.close()

    def set_metadata(self, values):
        connection = _connection(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for key, value in values.items():
                connection.execute(
                    "INSERT INTO catalog_meta(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(value) if value is not None else None),
                )
            connection.commit()
        finally:
            connection.close()

    def reconcile(self, configured_keys):
        connection = _connection(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for row in connection.execute("SELECT host_id, harness_id FROM model_catalogs").fetchall():
                key = (row[0], row[1])
                if key not in configured_keys:
                    connection.execute(
                        "UPDATE model_catalogs SET disabled = 1, available = 0, stale = 0, "
                        "models_json = '[]', error = 'HARNESS_REMOVED: Harness is no longer configured', updated_at = ? "
                        "WHERE host_id = ? AND harness_id = ?",
                        (_now_ms(), row[0], row[1]),
                    )
            connection.commit()
        finally:
            connection.close()

    def success(self, host_id, harness, version, models, now=None):
        now = now or _now_ms()
        connection = _connection(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO model_catalogs(
                    host_id, harness_id, display_name, available, version,
                    models_json, stale, disabled, last_success_at, updated_at, error
                ) VALUES (?, ?, ?, 1, ?, ?, 0, 0, ?, ?, NULL)
                ON CONFLICT(host_id, harness_id) DO UPDATE SET
                    display_name = excluded.display_name, disabled = 0, available = 1,
                    version = excluded.version, models_json = excluded.models_json,
                    stale = 0, last_success_at = excluded.last_success_at,
                    updated_at = excluded.updated_at, error = NULL""",
                (host_id, harness["id"], harness["display_name"], version,
                 json.dumps(models, separators=(",", ":")), now, now),
            )
            connection.commit()
        finally:
            connection.close()

    def failure(self, host_id, harness, code, message, permanent=False, now=None):
        now = now or _now_ms()
        previous = self.get(host_id, harness["id"])
        models = [] if permanent else (previous or {}).get("models", [])
        connection = _connection(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO model_catalogs(
                    host_id, harness_id, display_name, available, version,
                    models_json, stale, disabled, last_success_at, updated_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(host_id, harness_id) DO UPDATE SET
                    display_name = excluded.display_name, disabled = excluded.disabled,
                    available = excluded.available, version = COALESCE(excluded.version, model_catalogs.version),
                    models_json = excluded.models_json, stale = excluded.stale,
                    last_success_at = COALESCE(model_catalogs.last_success_at, excluded.last_success_at),
                    updated_at = excluded.updated_at, error = excluded.error""",
                (host_id, harness["id"], harness["display_name"],
                 0 if permanent else bool(models),
                 (previous or {}).get("version"), json.dumps(models, separators=(",", ":")),
                 0 if permanent else 1, 1 if permanent else 0, (previous or {}).get("last_success_at"), now,
                 f"{code}: {message}"),
            )
            connection.commit()
        finally:
            connection.close()


def normalize_models(values, available=True):
    """Return stable model entries and keep the no-argument default selectable."""
    models = _catalog_models(values)
    for model in models:
        model["available"] = available
    if "default" not in {model["id"] for model in models}:
        models.insert(0, {"id": "default", "label": "Default", "available": available})
    return models


def _catalog_models(values):
    """Normalize adapter output to exact IDs plus friendly labels."""
    if isinstance(values, dict):
        values = values.get("models") or values.get("data") or []
    if not isinstance(values, list):
        return []
    result = []
    seen = set()
    for value in values:
        if isinstance(value, str):
            model_id, label = value.strip(), value.strip()
        elif isinstance(value, dict):
            model_id = value.get("id") or value.get("name") or value.get("model")
            label = value.get("display_name") or value.get("label") or value.get("name") or model_id
        else:
            continue
        if not isinstance(model_id, str) or not MODEL_ID_RE.fullmatch(model_id.strip()):
            continue
        model_id = model_id.strip()
        if model_id in seen:
            continue
        seen.add(model_id)
        result.append({"id": model_id, "label": str(label or model_id).strip() or model_id, "available": True})
        if len(result) >= MAX_MODELS:
            break
    return result


def _version(output):
    line = (output or "").splitlines()[0].strip() if output else ""
    match = re.search(r"\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)?", line)
    return match.group(0)[:128] if match else line[:128] or None


def _output_is_missing(output):
    lowered = (output or "").lower()
    return any(marker in lowered for marker in (
        "command not found", "no such file", "not recognized", "cannot execute",
    ))


def _harness_command(harness):
    configured = harness.get("command")
    if isinstance(configured, list) and configured and all(isinstance(part, str) and part for part in configured):
        return list(configured)
    if isinstance(configured, str) and configured.strip():
        return [configured.strip()]
    return [harness["id"]]


def _configured_aliases(harness):
    return _catalog_models(harness.get("model_aliases") or harness.get("aliases") or [])


def _discover_json_models(host, command, timeout):
    remote = hosts.ssh_target(host)
    return herdr.run_herdr_checked(
        "models", "--json", remote=remote, host_id=host["id"], command=command, timeout=timeout,
    )


# Keep the adapters named even though OpenCode and Codex currently expose the
# same stable JSON-shaped command. Their parsers can diverge independently as
# either CLI changes without changing the wire contract or cache.
MODEL_ADAPTERS = {
    "opencode": _discover_json_models,
    "codex": _discover_json_models,
}


def discover_harness(host, harness):
    """Probe one configured harness using an adapter with fixed argv."""
    remote = hosts.ssh_target(host)
    timeout = host.get("readiness_timeout_seconds", 15)
    command = _harness_command(harness)
    ok, version_output = herdr.run_herdr_checked(
        "--version", remote=remote, host_id=host["id"], command=command, timeout=timeout,
    )
    if not ok:
        return {"ok": False, "code": "HARNESS_MISSING" if _output_is_missing(version_output) else "HARNESS_UNAVAILABLE",
                "message": "Harness command is missing" if _output_is_missing(version_output) else "Harness did not respond",
                "permanent": _output_is_missing(version_output)}

    version = _version(version_output)
    harness_id = harness["id"].lower()
    if harness_id == "claude":
        models = _configured_aliases(harness)
    else:
        # Both adapters expose a machine-readable model listing.  The parser is
        # intentionally tolerant because CLI releases have used both a bare
        # array and a {models: [...]} envelope.
        adapter = MODEL_ADAPTERS.get(harness_id, _discover_json_models)
        ok, output = adapter(host, command, timeout)
        if not ok:
            if _output_is_missing(output):
                return {"ok": False, "code": "HARNESS_MISSING", "message": "Harness command is missing", "permanent": True}
            return {"ok": False, "code": "MODEL_DISCOVERY_FAILED", "message": "Model discovery failed", "permanent": False}
        try:
            models = _catalog_models(json.loads(output))
        except (json.JSONDecodeError, TypeError):
            return {"ok": False, "code": "MODEL_DISCOVERY_INVALID", "message": "Harness returned invalid model data", "permanent": False}

    # `default` is always a valid no-argument launch when the harness itself is
    # healthy.  Keep it first so an empty or changed provider list remains usable.
    if "default" not in {model["id"] for model in models}:
        models.insert(0, {"id": "default", "label": "Default", "available": True})
    return {"ok": True, "version": version, "models": models}


def configured_harnesses(hosts_records=None):
    records = hosts_records if hosts_records is not None else herdr.configured_host_records()
    result = []
    for host in records:
        for harness in host.get("harnesses", []):
            result.append((host, harness))
    return result


def public_catalog(row, now=None):
    now = now or _now_ms()
    last_success = row.get("last_success_at")
    age = max(0, now - last_success) if isinstance(last_success, int) else None
    disabled = bool(row.get("disabled"))
    models = [
        {"id": model.get("id", ""), "label": model.get("label", model.get("id", "")),
         "available": bool(model.get("available", True)) and not disabled}
        for model in row.get("models", []) if isinstance(model, dict) and model.get("id")
    ]
    return {
        "host_id": row["host_id"],
        "harness_id": row["harness_id"],
        "display_name": row["display_name"],
        "available": bool(row.get("available")) and not disabled,
        "disabled": disabled,
        "version": row.get("version"),
        "models": models,
        "stale": bool(row.get("stale")),
        "last_success_at": last_success,
        "age_ms": age,
        "error": row.get("error"),
    }


def public_status(now=None):
    now = now or _now_ms()
    metadata = CatalogStore().metadata()
    last = int(metadata["last_refresh_at"]) if metadata.get("last_refresh_at", "").isdigit() else None
    return {
        "state": metadata.get("state", "idle"),
        "last_refresh_at": last,
        "next_refresh_at": last + REFRESH_INTERVAL_MS if last is not None else None,
        "error": metadata.get("error"),
        "age_ms": max(0, now - last) if last is not None else None,
    }


def public_frame():
    store = CatalogStore()
    configured = {(host["id"], harness["id"]) for host, harness in configured_harnesses()}
    store.reconcile(configured)
    rows = [row for row in store.list() if (row["host_id"], row["harness_id"]) in configured]
    return {"catalogs": [public_catalog(row) for row in rows], "catalog_status": public_status()}


def refresh_all(hosts_records=None, host_id=None):
    """Refresh every configured harness, retaining cache entries on transient errors."""
    if not _refresh_lock.acquire(blocking=False):
        return public_frame()
    store = CatalogStore()
    records = hosts_records if hosts_records is not None else herdr.configured_host_records()
    configured = {(host["id"], harness["id"]) for host, harness in configured_harnesses(records)}
    store.reconcile(configured)
    now = _now_ms()
    if host_id is None:
        store.set_metadata({"state": "refreshing", "error": None})
    errors = []
    try:
        for host, harness in configured_harnesses(records):
            if host_id is not None and host["id"] != host_id:
                continue
            if harness.get("enabled", True) is False:
                store.failure(host["id"], harness, "HARNESS_DISABLED", "Harness is disabled", permanent=True, now=now)
                continue
            result = discover_harness(host, harness)
            if result["ok"]:
                store.success(host["id"], harness, result.get("version"), result["models"], now=now)
            else:
                store.failure(host["id"], harness, result["code"], result["message"], result.get("permanent", False), now=now)
                errors.append(f"{host['id']}/{harness['id']}: {result['message']}")
        if host_id is None:
            metadata = {
                "state": "error" if errors else "success",
                "error": "; ".join(errors) if errors else None,
            }
            # Only a full refresh advances the global 24-hour schedule.
            metadata["last_refresh_at"] = now
            store.set_metadata(metadata)
        else:
            # Targeted refresh results are kept separately so a manual check
            # cannot hide the last full-refresh status from other hosts.
            store.set_metadata({
                "targeted_state": "error" if errors else "success",
                "targeted_error": "; ".join(errors) if errors else None,
                "last_targeted_refresh_at": now,
            })
    finally:
        _refresh_lock.release()
    return public_frame()


def refresh_due():
    status = public_status()
    return status["last_refresh_at"] is None or _now_ms() - status["last_refresh_at"] >= REFRESH_INTERVAL_MS


# Kept as the descriptive name used by the background loop and tests.
def needs_refresh():
    return refresh_due()
