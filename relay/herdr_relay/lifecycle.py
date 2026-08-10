"""Starting and stopping agents, and turning a host on and off.

Every function here is a write a client asked for, so every one of them answers
with a `command_ack` or a `command_error` and nothing else. The two destructive
ones — `terminate_session` and `shutdown_host` — require a confirmation nonce.
Host power is allowlisted by the versioned host configuration, with the old
environment pair retained only for preset-mode compatibility. `HostPowerTests`
checks that the allowlist and gate remain here.
"""
import subprocess
import uuid
import os

from . import catalogs, config, herdr, hosts, operations, presets, project_fs, projects, protocol, state


def start_session(msg):
    """Queue a durable project start and return before host work completes."""
    return operations.begin_start(msg)


def cancel_start(msg):
    request_id = msg.get("request_id")
    if not isinstance(request_id, str) or not projects.REQUEST_ID_RE.fullmatch(request_id):
        return protocol.command_error(None, "INVALID_REQUEST", "request_id is required")
    operation_id = msg.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 128:
        return protocol.command_error(request_id, "INVALID_REQUEST", "operation_id is invalid")
    operation = operations.cancel_start(operation_id)
    if operation is None:
        return protocol.command_error(request_id, "OPERATION_NOT_FOUND", "Start operation is no longer available")
    return {
        "type": "command_ack",
        "request_id": request_id,
        "result": {"operation": operations.public_operation(operation)},
    }


def recover_start_operations():
    operations.recover_active()


def launch_session(msg):
    if msg.get("project_id") is not None:
        return _launch_project(msg)
    request_id = msg.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return protocol.command_error(None, "INVALID_REQUEST", "request_id is required")
    preset = presets.PRESETS_BY_ID.get(msg.get("preset_id"))
    if not preset:
        return protocol.command_error(request_id, "UNKNOWN_PRESET", "Unknown preset")
    host_id = msg.get("host_id")
    host = preset["hosts"].get(host_id)
    if not host:
        return protocol.command_error(request_id, "HOST_NOT_ALLOWED", "Preset is not allowed on this host")
    configured_host = hosts.HOSTS_BY_ID.get(host_id)
    if hosts.HOSTS and configured_host is None:
        return protocol.command_error(request_id, "HOST_NOT_ALLOWED", "Preset is not allowed on this host")
    if configured_host and not hosts.project_root_allows(configured_host, host.get("cwd")):
        return protocol.command_error(
            request_id,
            "PROJECT_NOT_ALLOWED",
            "Preset cwd is outside the configured project roots",
        )
    remote = hosts.ssh_target(configured_host) if configured_host else host.get("target")
    command = hosts.herdr_command(configured_host) if configured_host else [config.HERDR]
    timeout = configured_host.get("readiness_timeout_seconds", 15) if configured_host else 15
    agent = preset["agent"]
    argv = [agent]
    if preset["model"] != "default":
        argv.extend(["--model", preset["model"]])
    name = f"mobile-{preset['id']}-{uuid.uuid4().hex[:8]}"
    success, output = herdr.run_herdr_checked(
        "agent", "start", name, "--cwd", host["cwd"], "--no-focus", "--", *argv,
        remote=remote,
        host_id=host_id,
        command=command,
        timeout=timeout,
    )
    if not success:
        return protocol.command_error(request_id, "LAUNCH_FAILED", "Herdr did not start the client")
    return {"type": "command_ack", "request_id": request_id, "result": {"host_id": host_id}}


def _launch_project(msg):
    request_id = msg.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return protocol.command_error(None, "INVALID_REQUEST", "request_id is required")
    project_id = msg.get("project_id")
    if not isinstance(project_id, str) or not projects.PROJECT_ID_RE.fullmatch(project_id):
        return protocol.command_error(request_id, "INVALID_REQUEST", "project_id is invalid")
    saved = projects.store().get(project_id)
    if saved is None:
        return protocol.command_error(request_id, "PROJECT_NOT_FOUND", "Project not found")
    if saved["archived"]:
        return protocol.command_error(request_id, "PROJECT_ARCHIVED", "Project is removed")
    host_id = msg.get("host_id")
    configured_host = hosts.HOSTS_BY_ID.get(host_id)
    if configured_host is None or saved["host_id"] != host_id:
        return protocol.command_error(request_id, "HOST_NOT_ALLOWED", "Project is not available on this host")
    root = hosts.project_root(configured_host, saved["root_id"])
    if root is None or not saved["available"]:
        return protocol.command_error(request_id, "PROJECT_UNAVAILABLE", "Project configuration is unavailable")
    try:
        relative = os.path.relpath(saved["canonical_path"], root["path"])
        components = [] if relative == "." else relative.split(os.sep)
        current = project_fs.browse(configured_host, root["path"], components)["canonical_path"]
    except (ValueError, project_fs.FilesystemError):
        return protocol.command_error(request_id, "PROJECT_UNAVAILABLE", "Project folder is unavailable")
    if current != saved["canonical_path"]:
        return protocol.command_error(request_id, "PROJECT_UNAVAILABLE", "Project folder changed")
    harness = msg.get("harness", "claude")
    model = msg.get("model", "default")
    if (
        not isinstance(harness, str)
        or not harness
        or len(harness) > 64
        or not all(char.isalnum() or char in "._-" for char in harness)
        or not isinstance(model, str)
        or not model
        or len(model) > 256
        or any(ord(char) < 32 or ord(char) == 127 for char in model)
    ):
        return protocol.command_error(request_id, "INVALID_REQUEST", "Invalid harness or model")
    configured_harnesses = {item["id"] for item in configured_host.get("harnesses", [])}
    if configured_harnesses and harness not in configured_harnesses:
        return protocol.command_error(request_id, "HARNESS_NOT_ALLOWED", "Harness is not configured on this host")
    if configured_harnesses:
        catalog = catalogs.CatalogStore().get(host_id, harness)
        if not catalog or catalog.get("disabled") or not catalog.get("available"):
            return protocol.command_error(request_id, "HARNESS_UNAVAILABLE", "Harness is not available on this host")
        if model != "default" and model not in {
            item.get("id") for item in catalog.get("models", []) if isinstance(item, dict)
        }:
            return protocol.command_error(request_id, "MODEL_NOT_AVAILABLE", "Model is not available on this host")
    remote = hosts.ssh_target(configured_host)
    command = hosts.herdr_command(configured_host)
    argv = [harness]
    if model != "default":
        argv.extend(["--model", model])
    name = f"mobile-project-{project_id[:12]}-{uuid.uuid4().hex[:8]}"
    success, _output = herdr.run_herdr_checked(
        "agent", "start", name, "--cwd", saved["canonical_path"], "--no-focus", "--", *argv,
        remote=remote,
        host_id=host_id,
        command=command,
        timeout=configured_host.get("readiness_timeout_seconds", 15),
    )
    if not success:
        return protocol.command_error(request_id, "LAUNCH_FAILED", "Herdr did not start the client")
    projects.store().mark_launch(project_id)
    return {
        "type": "command_ack",
        "request_id": request_id,
        "result": {"host_id": host_id, "project_id": project_id},
    }


def terminate_session(msg):
    request_id = msg.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return protocol.command_error(None, "INVALID_REQUEST", "request_id is required")
    if not isinstance(msg.get("confirmation_nonce"), str) or not msg["confirmation_nonce"]:
        return protocol.command_error(request_id, "CONFIRMATION_REQUIRED", "confirmation_nonce is required")
    target = state.session_target_map.get(msg.get("session_id"))
    if not target:
        return protocol.command_error(request_id, "STALE_SESSION", "Session is no longer active")
    pane_id, remote = target
    success, output = herdr.run_herdr_checked("pane", "close", pane_id, remote=remote)
    if not success:
        return protocol.command_error(request_id, "TERMINATE_FAILED", "Herdr did not terminate the client")
    state.session_target_map.pop(msg["session_id"], None)
    return {"type": "command_ack", "request_id": request_id, "result": {"output": output}}


def _power_host(host_id):
    """Resolve data-driven power settings without exposing their private values."""
    configured = hosts.HOSTS_BY_ID.get(host_id)
    if configured:
        power = configured.get("power") or {}
        wake = power.get("wake") if isinstance(power.get("wake"), dict) else None
        return {
            "wake_mac": wake.get("mac") if wake else None,
            "shutdown": power.get("shutdown") is True,
            "target": hosts.ssh_target(configured),
        }
    if host_id != config.POWER_HOST_ID:
        return None
    return {
        "wake_mac": config.POWER_HOST_MAC or None,
        "shutdown": True,
        "target": presets.HOST_TARGETS.get(host_id),
    }


def wake_host(msg):
    request_id = msg.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return protocol.command_error(None, "INVALID_REQUEST", "request_id is required")
    host_id = msg.get("host_id")
    power = _power_host(host_id)
    if not power or not power["wake_mac"]:
        return protocol.command_error(request_id, "HOST_NOT_ALLOWED", "Power control is not allowed for this host")
    try:
        result = subprocess.run(
            [config.WAKE_BIN, power["wake_mac"]],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return protocol.command_error(request_id, "WAKE_FAILED", "Wake-on-LAN command failed")
    if result.returncode != 0:
        return protocol.command_error(request_id, "WAKE_FAILED", "Wake-on-LAN command failed")
    return {"type": "command_ack", "request_id": request_id, "result": {"host_id": host_id}}


def shutdown_host(msg):
    request_id = msg.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return protocol.command_error(None, "INVALID_REQUEST", "request_id is required")
    if not isinstance(msg.get("confirmation_nonce"), str) or not msg["confirmation_nonce"]:
        return protocol.command_error(request_id, "CONFIRMATION_REQUIRED", "confirmation_nonce is required")
    host_id = msg.get("host_id")
    power = _power_host(host_id)
    if not power or not power["shutdown"]:
        return protocol.command_error(request_id, "HOST_NOT_ALLOWED", "Power control is not allowed for this host")
    target = power["target"]
    if not target:
        return protocol.command_error(request_id, "UNKNOWN_HOST", "Power host has no SSH target")
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o", "ConnectTimeout=5",
                "-o", "ServerAliveInterval=3",
                "-o", "ServerAliveCountMax=2",
                "-o", "BatchMode=yes",
                target,
                "sudo", "-n", "systemctl", "poweroff",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return protocol.command_error(request_id, "SHUTDOWN_FAILED", "Host shutdown command failed")
    if result.returncode != 0:
        return protocol.command_error(request_id, "SHUTDOWN_FAILED", "Host shutdown command failed")
    return {"type": "command_ack", "request_id": request_id, "result": {"host_id": host_id}}
