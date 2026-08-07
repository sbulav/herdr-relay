"""Versioned host configuration and the public host-health projection.

The configuration is deliberately server-side data.  In particular, SSH targets,
Wake-on-LAN addresses, wrapper commands, and project roots never pass through
``public_host``.  Clients only need a stable id, a display name, readiness, the
active-agent count, and the capabilities they may offer to the user.
"""
import json
import hashlib
import os
import re

from . import config


HOST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
MAC_RE = re.compile(r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\Z")
MAX_TIMEOUT_SECONDS = 300


def load_hosts(path=None):
    """Load and validate the operator-owned host file.

    An unset path means the relay is running in its legacy preset-only mode.  An
    explicit path is strict: a malformed file must stop startup rather than
    silently falling back to a different host topology.
    """
    path = config.HOSTS_FILE if path is None else path
    if not path:
        return []
    with open(path, encoding="utf-8") as stream:
        document = json.load(stream)
    if document.get("schema_version") != 1:
        raise ValueError("unsupported host configuration schema version")
    raw_hosts = document.get("hosts")
    if not isinstance(raw_hosts, list):
        raise ValueError("hosts must be a list")

    hosts = []
    seen = set()
    for raw in raw_hosts:
        if not isinstance(raw, dict):
            raise ValueError("each host must be an object")
        host_id = raw.get("id", "")
        if not isinstance(host_id, str) or not HOST_ID_RE.fullmatch(host_id) or host_id in seen:
            raise ValueError(f"invalid or duplicate host id: {host_id}")
        seen.add(host_id)

        display_name = raw.get("display_name")
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValueError(f"missing display_name for host {host_id}")

        ssh = raw.get("ssh", {})
        if ssh is None:
            ssh = {}
        if not isinstance(ssh, dict):
            raise ValueError(f"invalid ssh configuration for host {host_id}")
        target = ssh.get("target")
        if target is not None and (not isinstance(target, str) or not target.strip()):
            raise ValueError(f"invalid ssh target for host {host_id}")

        roots = raw.get("project_roots", [])
        if not isinstance(roots, list) or not roots or any(
            not isinstance(root, str) or not os.path.isabs(root) for root in roots
        ):
            raise ValueError(f"project_roots must contain absolute paths for host {host_id}")
        roots = [os.path.abspath(os.path.normpath(root)) for root in roots]
        if len(set(roots)) != len(roots):
            raise ValueError(f"duplicate project root for host {host_id}")

        herdr = raw.get("herdr", {})
        if not isinstance(herdr, dict):
            raise ValueError(f"invalid herdr configuration for host {host_id}")
        binary = herdr.get("binary")
        if binary is not None and (not isinstance(binary, str) or not binary.strip()):
            raise ValueError(f"invalid herdr binary for host {host_id}")
        wrapper = herdr.get("wrapper", [])
        if not isinstance(wrapper, list) or any(not isinstance(part, str) or not part for part in wrapper):
            raise ValueError(f"herdr wrapper must be a non-empty-string list for host {host_id}")

        harnesses = raw.get("harnesses", [])
        if not isinstance(harnesses, list):
            raise ValueError(f"harnesses must be a list for host {host_id}")
        harness_ids = set()
        normalized_harnesses = []
        for harness in harnesses:
            if not isinstance(harness, dict):
                raise ValueError(f"invalid harness for host {host_id}")
            harness_id = harness.get("id")
            if not isinstance(harness_id, str) or not HOST_ID_RE.fullmatch(harness_id) or harness_id in harness_ids:
                raise ValueError(f"invalid or duplicate harness for host {host_id}")
            harness_ids.add(harness_id)
            label = harness.get("display_name", harness_id)
            if not isinstance(label, str) or not label.strip():
                raise ValueError(f"invalid harness display name for host {host_id}")
            enabled = harness.get("enabled", True)
            if not isinstance(enabled, bool):
                raise ValueError(f"harness enabled must be boolean for host {host_id}")
            command = harness.get("command", [harness_id])
            if isinstance(command, str):
                command = [command]
            if not isinstance(command, list) or any(not isinstance(part, str) or not part for part in command):
                raise ValueError(f"invalid harness command for host {host_id}")
            aliases = harness.get("model_aliases", harness.get("aliases", []))
            if not isinstance(aliases, list):
                raise ValueError(f"model_aliases must be a list for host {host_id}")
            normalized_aliases = []
            alias_ids = set()
            for alias in aliases:
                if isinstance(alias, str):
                    alias = {"id": alias, "display_name": alias}
                if not isinstance(alias, dict):
                    raise ValueError(f"invalid model alias for host {host_id}")
                alias_id = alias.get("id")
                alias_label = alias.get("display_name", alias_id)
                if (
                    not isinstance(alias_id, str)
                    or not HOST_ID_RE.fullmatch(alias_id)
                    or alias_id in alias_ids
                    or not isinstance(alias_label, str)
                    or not alias_label.strip()
                ):
                    raise ValueError(f"invalid model alias for host {host_id}")
                alias_ids.add(alias_id)
                normalized_aliases.append({"id": alias_id, "display_name": alias_label.strip()})
            normalized_harnesses.append({
                "id": harness_id,
                "display_name": label.strip(),
                "enabled": enabled,
                "command": list(command),
                "model_aliases": normalized_aliases,
            })

        power = raw.get("power", {})
        if not isinstance(power, dict):
            raise ValueError(f"invalid power configuration for host {host_id}")
        wake = power.get("wake")
        if wake is not None:
            if not isinstance(wake, dict) or not MAC_RE.fullmatch(str(wake.get("mac", ""))):
                raise ValueError(f"invalid wake configuration for host {host_id}")
        shutdown = power.get("shutdown", False)
        if not isinstance(shutdown, bool):
            raise ValueError(f"shutdown capability must be boolean for host {host_id}")
        if shutdown and not target:
            raise ValueError(f"shutdown capability requires an SSH target for host {host_id}")

        timeout = raw.get("readiness_timeout_seconds", 15)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
            raise ValueError(f"invalid readiness timeout for host {host_id}")

        hosts.append(
            {
                "id": host_id,
                "display_name": display_name.strip(),
                "ssh": {"target": target} if target else {},
                "project_roots": list(roots),
                "herdr": {
                    "binary": binary,
                    "wrapper": list(wrapper),
                },
                "harnesses": normalized_harnesses,
                "power": {
                    "wake": {"mac": wake["mac"]} if wake is not None else None,
                    "shutdown": shutdown,
                },
                "readiness_timeout_seconds": timeout,
            }
        )
    return hosts


HOSTS = load_hosts()
HOSTS_BY_ID = {host["id"]: host for host in HOSTS}


def ssh_target(host):
    return (host.get("ssh") or {}).get("target")


def herdr_command(host):
    settings = host.get("herdr") or {}
    binary = settings.get("binary") or config.HERDR
    return [*settings.get("wrapper", []), binary]


def project_root_allows(host, cwd):
    """Check a preset cwd against the host's lexical project-root allowlist."""
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return False
    candidate = os.path.normpath(cwd)
    for root in host.get("project_roots", []):
        normalized_root = os.path.normpath(root)
        if candidate == normalized_root or candidate.startswith(normalized_root.rstrip(os.sep) + os.sep):
            return True
    return False


def project_roots(host):
    """Return stable opaque root handles and their private filesystem paths."""
    roots = []
    for raw_root in host.get("project_roots", []):
        path = os.path.abspath(os.path.normpath(raw_root))
        digest = hashlib.sha256(f"{host['id']}\0{os.path.realpath(path)}".encode()).hexdigest()[:24]
        roots.append({
            "id": f"root_{digest}",
            "path": path,
            "label": os.path.basename(path) or path,
        })
    return roots


def project_root(host, root_id):
    return next((root for root in project_roots(host) if root["id"] == root_id), None)


def power_capabilities(host):
    power = host.get("power") or {}
    return {
        "wake": isinstance(power.get("wake"), dict),
        "shutdown": power.get("shutdown") is True and bool(ssh_target(host)),
    }


def public_host(host, probe):
    """Return only the host state and capabilities safe for a client."""
    ssh_reachable = probe.get("ssh_reachable") is True
    herdr_ready = probe.get("herdr_ready") is True
    if not ssh_reachable:
        status = "offline"
        message = "SSH unreachable"
    elif not herdr_ready:
        status = "herdr_unavailable"
        message = "Herdr unavailable"
    else:
        status = "ready"
        message = None
    count = probe.get("active_agent_count") if herdr_ready else None
    return {
        "host_id": host["id"],
        "display_name": host["display_name"],
        "online": ssh_reachable and herdr_ready,
        "status": status,
        "ssh_reachable": ssh_reachable,
        "herdr_ready": herdr_ready,
        "active_agent_count": count,
        "capabilities": power_capabilities(host),
        # Harness commands and configured aliases are server-side inputs.  The
        # catalog frame carries only the public display identity and discovery
        # result for each harness.
        "harnesses": [
            {"id": harness["id"], "display_name": harness["display_name"]}
            for harness in host.get("harnesses", [])
        ],
        **({"message": message} if message else {}),
    }


def public_hosts(hosts, probes):
    return [public_host(host, probes.get(host["id"], {})) for host in hosts]
