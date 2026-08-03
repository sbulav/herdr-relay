"""Every call out to the herdr CLI, local or over SSH.

`run_herdr_checked` is the only place a subprocess is built, so the shape of a
remote invocation — BatchMode, the timeouts, no client-supplied argument — is
stated once.
"""
import asyncio
import json
import os
import subprocess

from . import config, panes, presets, state

def run_herdr_checked(*args, remote=None):
    try:
        if remote:
            cmd = [
                "ssh",
                "-o", "ConnectTimeout=5",
                "-o", "ServerAliveInterval=3",
                "-o", "ServerAliveCountMax=2",
                "-o", "BatchMode=yes",
                remote, config.HERDR, *args,
            ]
        else:
            cmd = [config.HERDR, *args]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return r.returncode == 0, r.stdout.strip()
    except Exception as exc:
        if remote:
            print(f"herdr poll failed for {remote}: {exc!r}", flush=True)
        return False, ""


def run_herdr(*args, remote=None):
    return run_herdr_checked(*args, remote=remote)[1]


def get_agents_from_host(remote=None, host_id=None):
    online, raw = run_herdr_checked("pane", "list", remote=remote)
    host_label = host_id or remote or "local"
    try:
        data = json.loads(raw)
        pane_list = data.get("result", {}).get("panes", [])
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
            revision = p.get("revision")
            if isinstance(revision, int) and not isinstance(revision, bool):
                agent["output_revision"] = revision
            agents.append(agent)
    except (json.JSONDecodeError, KeyError):
        agents = []
    return agents, online


async def get_all_agents():
    if presets.HOST_TARGETS:
        targets = list(presets.HOST_TARGETS.items())
        results = await asyncio.gather(*(
            asyncio.to_thread(
                get_agents_from_host,
                remote=remote,
                host_id=host_id,
            )
            for host_id, remote in targets
        ))
        hosts = [
            {"host_id": host_id, "online": online}
            for (host_id, _remote), (_host_agents, online) in zip(targets, results)
        ]
    else:
        targets = [(None, None), *((None, remote) for remote in config.REMOTES)]
        results = await asyncio.gather(*(
            asyncio.to_thread(get_agents_from_host, remote=remote)
            for _host_id, remote in targets
        ))
        hosts = []
    agents = [
        agent
        for host_agents, _online in results
        for agent in host_agents
    ]
    return agents, hosts


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
