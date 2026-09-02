"""Every call out to the herdr CLI, local or over SSH.

`run_herdr_checked` is the only place a subprocess is built, so the shape of a
remote invocation — BatchMode, the timeouts, the quoting that keeps client text
a single argument — is stated once.
"""
import asyncio
import json
import os
import shlex
import signal
import subprocess
import time

from . import config, hosts, panes, state
from .transcripts import refs


SSH_OPTIONS = [
    "-o", "ConnectTimeout=5",
    "-o", "ServerAliveInterval=3",
    "-o", "ServerAliveCountMax=2",
    "-o", "BatchMode=yes",
]

# `%C` is a SHA1 of the connection tuple, so a control path is always the
# directory plus this many characters.
_CONTROL_TOKEN_LENGTH = 40
# sockaddr_un.sun_path. Linux allows 108, darwin 104; take the smaller.
_CONTROL_PATH_LIMIT = 104
_control_dir_state = None  # (directory, usable) — resolved once per directory


def _control_path(directory):
    return os.path.join(directory, "%C")


def _control_dir_usable(directory):
    """Create the control directory, or report why multiplexing is off.

    Cached per directory: this runs on the way to every SSH call, and both the
    length arithmetic and the failure log should happen once, not twice a second.
    """
    global _control_dir_state
    if _control_dir_state is not None and _control_dir_state[0] == directory:
        return _control_dir_state[1]

    usable = True
    # `>=`, not `>`: sun_path is a C string, so 104 bytes hold 103 characters
    # plus the terminator, and ssh rejects a path at exactly the limit
    # ("ControlPath too long ('...' >= 104 bytes)").
    if len(directory) + 1 + _CONTROL_TOKEN_LENGTH >= _CONTROL_PATH_LIMIT:
        # Refusing is the safe branch. An over-long ControlPath makes ssh exit
        # immediately instead of connecting, so shipping one would turn a
        # latency optimisation into a total loss of every remote host.
        config.log.warning(
            "SSH control path under %s would exceed %d bytes; multiplexing disabled. "
            "Set HERDR_SSH_CONTROL_DIR to a shorter directory to enable it.",
            directory, _CONTROL_PATH_LIMIT,
        )
        usable = False
    else:
        try:
            # 0700: anyone who can write here can offer a socket our SSH calls
            # would then speak to.
            os.makedirs(directory, mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
        except OSError as error:
            config.log.warning(
                "cannot use SSH control directory %s (%s); multiplexing disabled", directory, error
            )
            usable = False
    _control_dir_state = (directory, usable)
    return usable


def ssh_options():
    """The fixed options for every SSH invocation, multiplexing included.

    A function rather than a constant because the control directory is
    configuration, and because it has to exist before ssh is handed a path
    inside it — ssh creates the socket but not the directory holding it.
    """
    # Expanded here rather than trusted as configured: ssh expands `~` in a
    # ControlPath itself, so an unexpanded one would have us create the
    # directory somewhere ssh never looks — and ssh does not fall back when the
    # socket's directory is missing, it fails the call.
    directory = os.path.expanduser(config.SSH_CONTROL_DIR or "")
    if not directory or not _control_dir_usable(directory):
        return list(SSH_OPTIONS)
    return [
        *SSH_OPTIONS,
        # `auto` opens a master when there is no socket yet and reuses one when
        # there is. `no` would never open the first one, and nothing else here
        # does; the master that `auto` backgrounds does not hold this process's
        # stdout, so a captured call still returns as soon as its own command does.
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={_control_path(directory)}",
        "-o", f"ControlPersist={config.SSH_CONTROL_PERSIST}",
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
            ["ssh", *ssh_options(), remote, *args],
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
            # ssh hands the remote side one string and the login shell re-splits
            # it, so argument boundaries only survive quoting here. `args` is
            # where client text rides (`pane send-text`, `respond`), which makes
            # this quoting also the line between a prompt containing `; id` and
            # that command running on the host. `command` stays unquoted: it is
            # operator configuration and may rely on the shell (`~/bin/herdr`).
            cmd = ["ssh", *ssh_options(), remote, *command, *(shlex.quote(arg) for arg in args)]
        else:
            cmd = [*command, *args]
        return run_process_checked(cmd, timeout=timeout, cancel_event=cancel_event)
    except Exception:
        if remote:
            print(f"herdr poll failed for host {host_id or 'configured host'}", flush=True)
        return False, ""


def run_herdr(*args, remote=None, host_id=None, command=None):
    kwargs = {"remote": remote}
    if host_id is not None:
        kwargs.update({"host_id": host_id, "command": command or command_for_host(host_id)})
    elif command is not None:
        kwargs["command"] = command
    return run_herdr_checked(*args, **kwargs)[1]


def command_for_host(host_id=None):
    """Return the configured Herdr executable/wrapper for one host.

    ``remote`` selects the SSH destination; this selects the command that runs
    there. They are intentionally independent because two host IDs may share a
    target while exposing different Herdr installations.
    """
    if host_id is None:
        return [config.HERDR]
    for host in configured_host_records():
        if host.get("id") == host_id:
            return hosts.herdr_command(host)
    # A relay without a host file can still observe synthetic/legacy host IDs
    # supplied by older pollers. Their pane state already selected `remote`, so
    # retain the historical default command rather than making a valid pane
    # unreadable solely because no per-host wrapper is configured.
    return [config.HERDR]


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
            # A pane can relaunch under another harness, or stop reporting a
            # session altogether. Do not let a previous poll's ref survive
            # either transition when this helper is called directly.
            pane_key = state.pane_key(host_id, p["pane_id"])
            state.pane_session_refs.pop(pane_key, None)
            raw_ref = p.get("agent_session")
            ref = refs.from_pane(p.get("agent"), raw_ref)
            if raw_ref is not None:
                # Preserve the distinction between an omitted ref (where an
                # unambiguous cwd fallback is safe) and an invalid supplied
                # ref (which must never silently select another transcript).
                state.pane_session_refs[pane_key] = ref
            agent = {
                "pane_id": p["pane_id"],
                "host_id": host_id,
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


def _parse_response(raw):
    """Decode one CLI JSON response object, or None when it is not one."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def response_error_code(raw):
    """The `error.code` of a failed CLI response, or None."""
    data = _parse_response(raw)
    if data is None:
        return None
    error = data.get("error")
    return error.get("code") if isinstance(error, dict) else None


def agent_started_pane_id(raw):
    """The pane an `agent start` created, straight from its own response.

    `agent_started` carries the full agent record, so a launch on a ready host
    is correlated by the reply itself — no window exists in which the agent is
    running but cannot be observed.
    """
    data = _parse_response(raw)
    if data is None:
        return None
    result = data.get("result")
    if not isinstance(result, dict) or result.get("type") != "agent_started":
        return None
    agent = result.get("agent")
    pane_id = agent.get("pane_id") if isinstance(agent, dict) else None
    return pane_id if isinstance(pane_id, str) and pane_id else None


def tab_created_ids(raw):
    """The pane and tab a `tab create` made, straight from its own response.

    Herdr 0.8 needs a pane at a shell prompt before an agent can be attached to
    it, so a launch is two calls; this reads the handle the first one returns.
    The tab id comes back too because a launch that fails afterwards leaves the
    tab behind, and only its id can close it.
    """
    data = _parse_response(raw)
    if data is None:
        return None, None
    result = data.get("result")
    if not isinstance(result, dict) or result.get("type") != "tab_created":
        return None, None
    pane = result.get("root_pane")
    pane_id = pane.get("pane_id") if isinstance(pane, dict) else None
    if not isinstance(pane_id, str) or not pane_id:
        return None, None
    tab = result.get("tab")
    tab_id = tab.get("tab_id") if isinstance(tab, dict) else None
    return pane_id, tab_id if isinstance(tab_id, str) and tab_id else None


def get_agent_by_name(name, host, cancel_event=None, timeout=None):
    """Resolve an exact agent name through `herdr agent get`.

    Returns (matches, probe) shaped like `get_agents_from_host`, but `matches`
    holds at most one agent: Herdr owns the name registry and refuses a taken
    name at start (`agent_name_taken`), so two panes cannot answer to one name.

    Readiness is claimed only on the two answers that prove it — the agent
    record, or `agent_not_found`, which only a running server can rule. Any
    other error (connection failure, `protocol_mismatch` from a version skew)
    leaves `herdr_ready` false rather than letting a launch proceed against a
    server that could not honor the identity contract.
    """
    host_id = host["id"]
    remote = hosts.ssh_target(host)
    budget = host.get("readiness_timeout_seconds", 180) if timeout is None else timeout
    deadline = time.monotonic() + max(float(budget), 0.0)

    def remaining():
        return deadline - time.monotonic()

    if remote:
        if remaining() <= 0 or not run_ssh_checked(
            remote,
            "true",
            host_id=host_id,
            timeout=max(remaining(), 0.01),
            cancel_event=cancel_event,
        ):
            return [], {"ssh_reachable": False, "herdr_ready": False}

    probe = {"ssh_reachable": True, "herdr_ready": False}
    command_timeout = remaining()
    if command_timeout <= 0:
        return [], probe
    # The error JSON lands on stderr with a nonzero exit; run_process_checked
    # already returns it as the output, so the reply is parsed regardless of
    # the exit status.
    _success, raw = run_herdr_checked(
        "agent", "get", name,
        remote=remote,
        host_id=host_id,
        command=hosts.herdr_command(host),
        timeout=command_timeout,
        cancel_event=cancel_event,
    )
    data = _parse_response(raw)
    if data is None:
        return [], probe
    if response_error_code(raw) == "agent_not_found":
        probe["herdr_ready"] = True
        return [], probe
    result = data.get("result")
    agent = result.get("agent") if isinstance(result, dict) else None
    pane_id = agent.get("pane_id") if isinstance(agent, dict) else None
    if not isinstance(pane_id, str) or not pane_id:
        return [], probe
    probe["herdr_ready"] = True
    return [{"pane_id": pane_id, "agent_name": name}], probe


def configured_host_records():
    """Return host definitions: the configured topology, or a bare fallback.

    The fallback keeps a relay with no host file usable — the local host plus
    whatever `HERDR_REMOTES` names — with `/` as the only project root and no
    power capability, because nothing here knows a MAC or a wrapper path.
    """
    if hosts.HOSTS:
        return list(hosts.HOSTS)
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


READ_PANE_SOURCES = frozenset(("visible", "recent"))
DEFAULT_READ_PANE_SOURCE = "visible"


def _read_pane_source(value, default=DEFAULT_READ_PANE_SOURCE):
    """Return a documented pane source or raise for an ambiguous request."""
    if value is None:
        return default
    if not isinstance(value, str) or value not in READ_PANE_SOURCES:
        raise ValueError("invalid pane source")
    return value


def read_pane(pane_id, remote=None, lines=30, source=DEFAULT_READ_PANE_SOURCE, host_id=None):
    """Read and normalize pane output without changing the operator's screen.

    ``visible`` is the safe default for automatic reads. ``recent`` remains
    available to explicit/manual history requests. Source validation lives here
    as well as at the protocol boundary so internal consumers cannot silently
    reintroduce an unsafe or misspelled source.
    """
    source = _read_pane_source(source)
    count = _read_pane_lines(lines)
    kwargs = {"remote": remote}
    if host_id is not None:
        kwargs.update({"host_id": host_id, "command": command_for_host(host_id)})
    raw = run_herdr(
        "pane", "read", pane_id, "--lines", str(count), "--source", source,
        **kwargs,
    )
    meaningful_lines = [
        line for line in raw.splitlines()
        if line.strip() and not panes.CHROME_RE.search(line)
    ]
    return "\n".join(meaningful_lines[-count:])
