"""Every call out to the herdr CLI, local or over SSH.

`run_herdr_checked` is the only place a subprocess is built, so the shape of a
remote invocation — BatchMode, the timeouts, no client-supplied argument — is
stated once.
"""
import asyncio
import json
import os
import signal
import subprocess
import time

from . import config, hosts, panes, presets, state


SSH_OPTIONS = [
    "-o", "ConnectTimeout=5",
    "-o", "ServerAliveInterval=3",
    "-o", "ServerAliveCountMax=2",
    "-o", "BatchMode=yes",
]


def _terminate_process(process):
    """Stop a process group so cancelling SSH also stops its remote helper."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                pass
        process.wait()


def run_process_checked(command, timeout=5, cancel_event=None):
    """Run a fixed command, optionally watching a durable-operation cancel event."""
    if cancel_event is None:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        output = result.stdout.strip()
        if result.returncode != 0 and not output:
            output = result.stderr.strip()
        return result.returncode == 0, output

    if cancel_event.is_set():
        return False, ""

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except Exception:
        return False, ""

    deadline = time.monotonic() + timeout
    while process.poll() is None:
        if cancel_event.is_set() or time.monotonic() >= deadline:
            _terminate_process(process)
            return False, ""
        cancel_event.wait(0.05)

    stdout, stderr = process.communicate()
    output = (stdout or "").strip()
    if process.returncode != 0 and not output:
        output = (stderr or "").strip()
    return process.returncode == 0, output


def run_ssh_checked(remote, *args, host_id=None, timeout=5, cancel_event=None):
    """Probe or invoke a fixed SSH command without logging the login target."""
    try:
        success, _output = run_process_checked(
            ["ssh", *SSH_OPTIONS, remote, *args],
            timeout=timeout,
            cancel_event=cancel_event,
        )
        return success
    except Exception:
        # The target is private routing state.  Logs identify the configured host
        # only, so an operator can diagnose a failure without leaking a login.
        print(f"ssh probe failed for host {host_id or 'configured host'}", flush=True)
        return False


def run_herdr_checked(*args, remote=None, host_id=None, command=None, timeout=15, cancel_event=None):
    try:
        command = list(command or [config.HERDR])
        if remote:
            cmd = ["ssh", *SSH_OPTIONS, remote, *command, *args]
        else:
            cmd = [*command, *args]
        return run_process_checked(cmd, timeout=timeout, cancel_event=cancel_event)
    except Exception:
        if remote:
            print(f"herdr poll failed for host {host_id or 'configured host'}", flush=True)
        return False, ""


def run_herdr(*args, remote=None):
    return run_herdr_checked(*args, remote=remote)[1]


def get_agents_from_host(remote=None, host_id=None, host=None, cancel_event=None, timeout=None):
    host = host or {
        "id": host_id or remote or "local",
        "ssh": {"target": remote} if remote else {},
        "herdr": {},
        "readiness_timeout_seconds": 180,
    }
    host_id = host["id"]
    remote = hosts.ssh_target(host)
    budget = host["readiness_timeout_seconds"] if timeout is None else timeout
    deadline = time.monotonic() + max(float(budget), 0.0)

    def remaining():
        return deadline - time.monotonic()

    if remote:
        if remaining() <= 0:
            return [], {"ssh_reachable": False, "herdr_ready": False, "active_agent_count": None}
        if not run_ssh_checked(
            remote,
            "true",
            host_id=host_id,
            timeout=max(remaining(), 0.01),
            cancel_event=cancel_event,
        ):
            return [], {"ssh_reachable": False, "herdr_ready": False, "active_agent_count": None}

    command_timeout = remaining()
    if command_timeout <= 0:
        return [], {"ssh_reachable": True, "herdr_ready": False, "active_agent_count": None}

    ready, raw = run_herdr_checked(
        "pane", "list",
        remote=remote,
        host_id=host_id,
        command=hosts.herdr_command(host),
        timeout=command_timeout,
        cancel_event=cancel_event,
    )
    host_label = host_id
    probe = {
        "ssh_reachable": True,
        "herdr_ready": False,
        "active_agent_count": None,
    }
    if not ready:
        return [], probe
    try:
        data = json.loads(raw)
        pane_list = data.get("result", {}).get("panes", [])
        if not isinstance(pane_list, list):
            return [], probe
        agents = []
        for p in pane_list:
            if not p.get("agent"):
                continue
            ref = p.get("agent_session")
            if (
                isinstance(ref, dict)
                and ref.get("kind") in ("id", "path")
                and isinstance(ref.get("value"), str)
                and ref["value"]
            ):
                state.pane_session_refs[(remote, p["pane_id"])] = {
                    "kind": ref["kind"], "value": ref["value"]
                }
            agent = {
                "pane_id": p["pane_id"],
                "agent": p.get("agent", ""),
                "label": p.get("label", ""),
                "status": p.get("agent_status", "unknown"),
                "cwd": p.get("cwd", ""),
                "project": os.path.basename(p.get("cwd", "")),
                "host": host_label,
                "remote": remote,
                "workspace_id": p.get("workspace_id", ""),
                "tab_id": p.get("tab_id", ""),
            }
            # Newer Herdr builds expose the user-facing start name explicitly.
            # Keep it internal: the relay uses it to reconcile durable starts,
            # while public frames continue to expose only the harness label.
            agent_name = p.get("agent_name") or p.get("name")
            if isinstance(agent_name, str) and agent_name:
                agent["agent_name"] = agent_name
            revision = p.get("revision")
            if isinstance(revision, int) and not isinstance(revision, bool):
                agent["output_revision"] = revision
            agents.append(agent)
    except (json.JSONDecodeError, KeyError, TypeError):
        return [], probe
    probe["herdr_ready"] = True
    probe["active_agent_count"] = len(agents)
    return agents, probe


def configured_host_records():
    """Return host definitions, with a temporary preset fallback for cutover."""
    if hosts.HOSTS:
        return list(hosts.HOSTS)
    if presets.HOST_TARGETS:
        records = []
        for host_id, remote in presets.HOST_TARGETS.items():
            roots = [
                host.get("cwd")
                for preset in presets.PRESETS
                for configured_id, host in preset.get("hosts", {}).items()
                if configured_id == host_id and host.get("cwd")
            ]
            records.append(
                {
                    "id": host_id,
                    "display_name": host_id,
                    "ssh": {"target": remote} if remote else {},
                    "project_roots": sorted(set(roots)) or ["/"],
                    "herdr": {},
                    "harnesses": [],
                    "power": {
                        "wake": {"mac": config.POWER_HOST_MAC}
                        if host_id == config.POWER_HOST_ID and config.POWER_HOST_MAC
                        else None,
                        "shutdown": host_id == config.POWER_HOST_ID,
                    },
                    "readiness_timeout_seconds": 180,
                }
            )
        return records
    return [
        {
            "id": "local",
            "display_name": "Local host",
            "ssh": {},
            "project_roots": ["/"],
            "herdr": {},
            "harnesses": [],
            "power": {"wake": None, "shutdown": False},
            "readiness_timeout_seconds": 180,
        },
        *[
            {
                "id": remote,
                "display_name": remote,
                "ssh": {"target": remote},
                "project_roots": ["/"],
                "herdr": {},
                "harnesses": [],
                "power": {"wake": None, "shutdown": False},
                "readiness_timeout_seconds": 180,
            }
            for remote in config.REMOTES
        ],
    ]


def _probe_result(value, active_agent_count):
    """Accept old test doubles while keeping the new probe shape explicit."""
    if isinstance(value, dict):
        return value
    return {
        "ssh_reachable": bool(value),
        "herdr_ready": bool(value),
        "active_agent_count": active_agent_count if value else None,
    }


async def get_all_agents():
    records = configured_host_records()
    results = await asyncio.gather(*(
        asyncio.to_thread(
            get_agents_from_host,
            remote=hosts.ssh_target(host),
            host_id=host["id"],
            host=host,
        )
        for host in records
    ))
    probes = {}
    agents = []
    for host, result in zip(records, results):
        host_agents, raw_probe = result
        probe = _probe_result(raw_probe, len(host_agents))
        probes[host["id"]] = probe
        agents.extend(host_agents)
    return agents, hosts.public_hosts(records, probes)


def _read_pane_lines(value, default=30, maximum=2000):
    """Coerce a client-supplied line count into a sane positive int.

    Anything unparseable falls back to the default rather than reaching herdr,
    which reports a bad --lines as an error string on stdout with exit code 0 —
    indistinguishable, downstream, from real terminal output.
    """
    try:
        count = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if count < 1:
        return default
    return min(count, maximum)


def read_pane(pane_id, remote=None):
    raw = run_herdr("pane", "read", pane_id, "--lines", "50", "--source", "recent", remote=remote)
    lines = [l for l in raw.splitlines() if l.strip() and not panes.CHROME_RE.search(l)]
    return "\n".join(lines[-20:])
