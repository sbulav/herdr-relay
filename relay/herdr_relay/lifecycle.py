"""Starting and stopping agents, and turning a host on and off.

Every function here is a write a client asked for, so every one of them answers
with a `command_ack` or a `command_error` and nothing else. The two destructive
ones — `terminate_session` and `shutdown_host` — require a confirmation nonce.
Host power is allowlisted by the versioned host configuration and by nothing
else (#45): a host absent from that file has no power capability at all.
`HostPowerTests` checks that the allowlist and gate remain here.

Starting an agent is not here. It is durable, so it lives in `operations` and
reaches a client through `start_session` below; the old immediate
`launch_session` was retired with the presets it launched from.
"""
import subprocess

from . import config, herdr, hosts, operations, projects, protocol, state


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


def terminate_session(msg):
    request_id = msg.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return protocol.command_error(None, "INVALID_REQUEST", "request_id is required")
    if not isinstance(msg.get("confirmation_nonce"), str) or not msg["confirmation_nonce"]:
        return protocol.command_error(request_id, "CONFIRMATION_REQUIRED", "confirmation_nonce is required")
    target = state.session_target_map.get(msg.get("session_id"))
    if not target:
        return protocol.command_error(request_id, "STALE_SESSION", "Session is no longer active")
    if not isinstance(target, tuple) or len(target) != 3:
        return protocol.command_error(request_id, "STALE_SESSION", "Session is no longer active")
    host_id, pane_id, remote = target
    success, output = herdr.run_herdr_checked(
        "pane", "close", pane_id, remote=remote, host_id=host_id,
        command=herdr.command_for_host(host_id),
    )
    if not success:
        return protocol.command_error(request_id, "TERMINATE_FAILED", "Herdr did not terminate the client")
    state.session_target_map.pop(msg["session_id"], None)
    return {"type": "command_ack", "request_id": request_id, "result": {"output": output}}


def _power_host(host_id):
    """Resolve data-driven power settings without exposing their private values.

    The host configuration file is the only source (#45). An unconfigured host
    returns None, and both callers turn that into HOST_NOT_ALLOWED — powering a
    machine off is not something to infer from a host id the relay half-knows.
    """
    configured = hosts.HOSTS_BY_ID.get(host_id)
    if not configured:
        return None
    power = configured.get("power") or {}
    wake = power.get("wake") if isinstance(power.get("wake"), dict) else None
    return {
        "wake_mac": wake.get("mac") if wake else None,
        "shutdown": power.get("shutdown") is True,
        "target": hosts.ssh_target(configured),
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
            ["ssh", *herdr.ssh_options(), target, "sudo", "-n", "systemctl", "poweroff"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return protocol.command_error(request_id, "SHUTDOWN_FAILED", "Host shutdown command failed")
    if result.returncode != 0:
        return protocol.command_error(request_id, "SHUTDOWN_FAILED", "Host shutdown command failed")
    return {"type": "command_ack", "request_id": request_id, "result": {"host_id": host_id}}
