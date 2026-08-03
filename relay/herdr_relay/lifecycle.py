"""Starting and stopping agents, and turning a host on and off.

Every function here is a write a client asked for, so every one of them answers
with a `command_ack` or a `command_error` and nothing else. The two destructive
ones — `terminate_session` and `shutdown_host` — require a confirmation nonce,
and the two host-power ones act only on the single id `HERDR_POWER_HOST_ID`
names. That allowlist and that gate are the whole safety story; `HostPowerTests`
is what checks they are still here.
"""
import subprocess
import uuid

from . import config, herdr, presets, protocol, state


def launch_session(msg):
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
    remote = host.get("target")
    agent = preset["agent"]
    argv = [agent]
    if preset["model"] != "default":
        argv.extend(["--model", preset["model"]])
    name = f"mobile-{preset['id']}-{uuid.uuid4().hex[:8]}"
    success, output = herdr.run_herdr_checked("agent", "start", name, "--cwd", host["cwd"], "--no-focus", "--", *argv, remote=remote)
    if not success:
        return protocol.command_error(request_id, "LAUNCH_FAILED", "Herdr did not start the client")
    return {"type": "command_ack", "request_id": request_id, "result": {"host_id": host_id}}


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


def wake_host(msg):
    request_id = msg.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return protocol.command_error(None, "INVALID_REQUEST", "request_id is required")
    host_id = msg.get("host_id")
    if host_id != config.POWER_HOST_ID or not config.POWER_HOST_MAC:
        return protocol.command_error(request_id, "HOST_NOT_ALLOWED", "Power control is not allowed for this host")
    try:
        result = subprocess.run(
            [config.WAKE_BIN, config.POWER_HOST_MAC],
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
    if host_id != config.POWER_HOST_ID:
        return protocol.command_error(request_id, "HOST_NOT_ALLOWED", "Power control is not allowed for this host")
    target = presets.HOST_TARGETS.get(host_id)
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
