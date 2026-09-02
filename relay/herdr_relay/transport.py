"""The two loops that produce frames, and the one function that sends them.

`broadcast` is the fan-out over `state.clients`; `poll_loop` asks herdr what
changed and `event_push` drains events the plugin hook pushed in. Both loops call
`broadcast` by plain name — they are the only callers, and a test that wants to
capture frames patches it here.
"""
import asyncio
import json

from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from . import catalogs, config, dialogs, herdr, hosts, operations, panes, projects, protocol, push, state, transcripts
from .config import log


async def broadcast(msg):
    data = json.dumps(msg)
    dead = set()
    # Disconnect cleanup mutates state.clients while send() yields to the event loop.
    for ws in state.clients.copy():
        try:
            await ws.send(data)
        except (ConnectionClosedError, ConnectionClosedOK):
            dead.add(ws)
        except Exception:
            dead.add(ws)
    if dead:
        log.debug("Removed %d dead client(s)", len(dead))
    state.clients.difference_update(dead)


def poll_interval(idle_streak):
    """How long to wait before the next poll, given consecutive quiet cycles.

    Kept separate from the loop, and a pure function of the streak, because the
    interesting part is the curve: floor on the first quiet cycle, ceiling once
    the host has clearly gone to sleep, and nothing in between that a test
    cannot state exactly.
    """
    if idle_streak <= 0:
        return config.POLL_INTERVAL
    # The streak has no upper bound — a relay nobody connects to over a weekend
    # reaches five figures — and `1.5 ** 2000` raises OverflowError rather than
    # returning inf. Every overflow means "far past the ceiling", which is the
    # ceiling. Raising from here would kill poll_loop outright: this supplies
    # the timeout of the wait, outside the try that guards a failed cycle.
    try:
        interval = config.POLL_INTERVAL * (config.POLL_BACKOFF_FACTOR ** idle_streak)
    except OverflowError:
        return config.POLL_INTERVAL_MAX
    return min(interval, config.POLL_INTERVAL_MAX)


def wake_poll_loop():
    """Cut the current backoff short — something happened worth polling for.

    Safe to call from anywhere on the event loop, and cheap when the loop is
    already at its floor: setting a set Event is a no-op.
    """
    state.poll_wakeup.set()


async def poll_loop():
    while True:
        try:
            await _poll_once()
        except Exception:
            # A failed cycle tells us nothing about how busy the host is, so it
            # neither extends nor resets the streak — the previous pace stands.
            log.exception("poll cycle failed; retrying")
        try:
            await asyncio.wait_for(
                state.poll_wakeup.wait(), timeout=poll_interval(state.poll_idle_streak)
            )
        except asyncio.TimeoutError:
            pass
        # Cleared after the wait rather than before it: an edge that arrives
        # while _poll_once is still running must still be seen, and clearing
        # here means that set flag short-circuits the very next wait.
        state.poll_wakeup.clear()


async def catalog_loop():
    """Refresh model catalogs at most once per day and fan out the result."""
    while True:
        try:
            if catalogs.needs_refresh():
                frame = await asyncio.to_thread(catalogs.refresh_all)
                await broadcast({"type": "catalogs", **frame})
        except Exception:
            log.exception("catalog refresh failed; retaining last-successful catalogs")
        await asyncio.sleep(60)


async def _poll_once():
    state.pane_session_refs.clear()  # Host threads populate refs while herdr.get_all_agents() awaits.
    agents, hosts = await herdr.get_all_agents()
    seen_keys = set()
    for agent in agents:
        if not isinstance(agent, dict) or not isinstance(agent.get("pane_id"), str):
            log.warning("Rejecting malformed pane observation atomically")
            state.pane_session_refs.clear()
            return
        key = state.pane_key(agent.get("host_id") or agent.get("host", "local"), agent["pane_id"])
        if key in seen_keys:
            log.warning("Rejecting duplicate pane observation atomically for host=%s pane=%s", key[0], key[1])
            state.pane_session_refs.clear()
            return
        seen_keys.add(key)
    changed = False
    current_pane_keys = {
        state.pane_key(a.get("host_id") or a.get("host", "local"), a["pane_id"])
        for a in agents
    }
    current_pane_ids = {key[1] for key in current_pane_keys}
    state.pane_remote_map.clear()
    state.session_target_map.clear()
    state.pane_cwd_map.clear()
    state.pane_host_map.clear()
    state.pane_project_map.clear()
    state.known_panes.clear()
    state.known_pane_keys.clear()
    state.pane_hosts.clear()
    state.known_panes.update(current_pane_ids)
    agent_cwd_counts = {}
    for a in agents:
        if a.get("agent") in ("claude", "opencode") and a.get("cwd"):
            cwd_key = (a.get("remote"), a["cwd"], a["agent"])
            agent_cwd_counts[cwd_key] = agent_cwd_counts.get(cwd_key, 0) + 1
    for a in agents:
        pid = a["pane_id"]
        host_id = a.get("host_id") or a.get("host", "local")
        key = state.pane_key(host_id, pid)
        state.known_pane_keys.add(key)
        state.pane_hosts.setdefault(pid, set()).add(host_id)
        state.pane_remote_map[key] = a.get("remote")
        state.session_target_map[protocol.session_id(host_id, pid)] = (host_id, pid, a.get("remote"))
        cwd_key = (a.get("remote"), a.get("cwd", ""), a.get("agent", ""))
        state.pane_cwd_map[key] = (
            a.get("cwd", ""), a.get("agent", ""), a.get("remote"),
            agent_cwd_counts.get(cwd_key, 0) > 1,
        )
        state.pane_host_map[key] = host_id
        state.pane_project_map[key] = a.get("project", "")
        status = a["status"]
        # Snapshot broadcast precedes the `state.last_statuses` update below, so this
        # still reads the prior poll's status for idle-after-work detection.
        previous_status = state.get(state.last_statuses, key)
        attention = protocol.attention_state(status, previous_status, state.get(state.pane_attention_states, key))
        if attention is None:
            state.pop(state.pane_attention_states, key)
        else:
            state.pane_attention_states[key] = attention
        revision = a.get("output_revision")
        previous_revision = state.get(state.pane_revisions, key)
        if (
            key not in state.pane_activity
            or status != previous_status
            or revision != previous_revision
        ):
            state.pane_activity[key] = protocol.now_ms()
            changed = True
        state.pane_revisions[key] = revision
        protocol.add_pane_metadata(a, pid)

    # Always send a complete snapshot. In particular, an empty snapshot
    # removes stale agents after every remote host goes offline.
    project_frame = projects.public_snapshot()
    recovery = operations.public_recovery()
    await broadcast({
        "type": "agents", "agents": protocol.public_agents(agents),
        "hosts": hosts,
        "projects": project_frame["projects"],
        "project_roots": project_frame["roots"],
        "operations": recovery,
        **catalogs.public_frame(),
    })
    # Pace the next cycle (#19). Any of these means the next two seconds could
    # carry something a client needs promptly; none of them means nobody is
    # watching and nothing is moving, so the loop may cost less. Filtered from
    # the recovery frame already in hand rather than queried again — the frame
    # carries terminal operations too, and only the active ones need reconciling.
    #
    # `state.clients`, not `state.subscriptions`: the agent list is the screen a
    # client sees before it subscribes to anything, and a status change there is
    # exactly what it opened the app for. Backing off behind a connected client
    # would trade a poll for up to POLL_INTERVAL_MAX of stale dashboard.
    busy = (
        changed
        or bool(state.clients)
        or any(a["status"] in ("working", "blocked") for a in agents)
        or any(op["stage"] in operations.ACTIVE_STAGES for op in recovery)
    )
    state.poll_idle_streak = 0 if busy else state.poll_idle_streak + 1
    # Read every blocked pane off the event loop, and all of them at once:
    # `herdr pane read` shells out (over ssh for remote hosts) with a 15s timeout,
    # and a herd of agents tends to block together. Done inline, one slow host
    # freezes every client on exactly the event they care about most.
    # Herdr's output revision is useful evidence, but not sufficient on its own:
    # a pane can redraw a changed prompt without changing that counter. Reading
    # each blocked pane lets `dialogs.ensure` compare the actual observation and
    # keeps an unchanged dialog stable.
    blocked_agents = [a for a in agents if a["status"] == "blocked"]
    blocked_content = dict(zip(
        (state.pane_key(a.get("host_id") or a.get("host", "local"), a["pane_id"]) for a in blocked_agents),
        await asyncio.gather(*(
            asyncio.to_thread(
                herdr.read_pane,
                a["pane_id"],
                remote=a.get("remote"),
                source="visible",
                host_id=a.get("host_id") or a.get("host", "local"),
            )
            for a in blocked_agents
        )),
    ))
    for a in agents:
        pid, status = a["pane_id"], a["status"]
        host_id = a.get("host_id") or a.get("host", "local")
        key = state.pane_key(host_id, pid)
        if status == "blocked":
            content = blocked_content.get(key, "")
            options = panes.detect_options(content)
            previous_dialog = state.get(state.pane_dialogs, key)
            dialog = dialogs.ensure(
                pid,
                content,
                options,
                agent=a.get("agent", ""),
                project=a.get("project", ""),
                host=host_id,
                observation=a.get("output_revision"),
            )
            if dialog is not previous_dialog:
                await broadcast(dialogs.frame(dialog))
            if state.get(state.last_statuses, key) != "blocked":
                await push.send_web_push(
                    title=f"🐑 {a['project']} blocked",
                    body=content[:120],
                    url=f"/?pane={pid}&host_id={host_id}",
                    tag=f"herdr-blocked:{host_id}:{pid}",
                )
        if status != "blocked" and state.get(state.last_statuses, key) == "blocked":
            dialogs.clear(key)
            await push.send_web_push(
                "", "", url=f"/?pane={pid}&host_id={host_id}",
                tag=f"herdr-blocked:{host_id}:{pid}", clear=True,
            )
        state.last_statuses[key] = status
    for key in set(state.last_statuses) - current_pane_keys:
        if isinstance(key, tuple):
            state.last_statuses.pop(key, None)
            dialogs.clear(key)
            state.pop(state.pane_activity, key)
            state.pop(state.pane_revisions, key)
            state.pop(state.pane_attention_states, key)
            state.pop(state.pane_host_map, key)
            state.pop(state.pane_project_map, key)

    # Live-stream structured transcript blocks to subscribed clients. Only
    # watched Claude panes are read; a changed signature (path or content)
    # triggers a push. Failures are swallowed so one bad host can't stall.
    watchers = {
        key: [ws for ws, subscribed_key in list(state.subscriptions.items()) if subscribed_key == key]
        for key in set(state.subscriptions.values()) if key in current_pane_keys
    }
    pane_results = await asyncio.gather(
        *(asyncio.to_thread(transcripts.blocks.pane_blocks, key[1], host_id=key[0]) for key in watchers),
        return_exceptions=True,
    )
    for key, result in zip(watchers, pane_results):
        if isinstance(result, Exception):
            continue
        blocks, sig = result
        if blocks is None:
            continue
        pid = key[1]
        frame = {"type": "pane_content", "pane_id": pid, "host_id": key[0], "output_blocks": blocks}
        protocol.add_pane_metadata(frame, pid, key[0])
        payload = json.dumps(frame)
        for ws in watchers[key]:
            stream_key = (id(ws), key[0], key[1])
            if state.stream_sigs.get(stream_key) == sig:
                continue
            state.stream_sigs[stream_key] = sig
            try:
                await ws.send(payload)
            except Exception:
                pass


def _discover_pushed_pane(event, host_id, pane_id):
    """Register an explicitly host-qualified event before the next poll.

    A pushed event is safe to discover early only when its host ID is one of
    the relay's configured canonical identities.  Omitted host IDs retain the
    old lookup behavior and therefore cannot create a pane that might belong
    to an unknown or ambiguous host.
    """
    if not isinstance(host_id, str) or not host_id or not isinstance(pane_id, str) or not pane_id:
        return None
    configured = next(
        (record for record in herdr.configured_host_records() if record.get("id") == host_id),
        None,
    )
    if configured is None:
        return None
    key = state.resolve(host_id, pane_id)
    if key is not None:
        return key
    key = state.pane_key(host_id, pane_id)
    state.known_panes.add(pane_id)
    state.known_pane_keys.add(key)
    state.pane_hosts.setdefault(pane_id, set()).add(host_id)
    remote = hosts.ssh_target(configured)
    state.pane_remote_map[key] = remote
    state.pane_host_map[key] = host_id
    state.pane_project_map[key] = event.get("project", "")
    state.pane_cwd_map[key] = (
        event.get("cwd", ""), event.get("agent", ""), remote, False,
    )
    return key


async def _handle_pushed_event(event):
    pane_id = event.get("pane_id", "")
    status = event.get("status", "")
    host = event.get("host_id")
    # Old local hooks had no host_id; retain only the unambiguous local
    # compatibility case. A non-canonical host field is never accepted as a
    # routing authority.
    if host is None and event.get("host") == "local":
        host = "local"
    key = state.resolve(host, pane_id)
    # A canonical host-qualified hook event can safely add a pane before the
    # poll catches up. Never let an omitted or unknown host become a routing
    # authority: those events remain ignored until a snapshot proves identity.
    if key is None and event.get("host_id") is not None:
        key = _discover_pushed_pane(event, event.get("host_id"), pane_id)

    if status == "blocked" and pane_id:
        # A hook can race the next snapshot. Do not create an actionable
        # dialog for a pane the relay cannot currently route; the event loop
        # has already woken polling, which will publish it once observable.
        if key is None or key is state.AMBIGUOUS:
            return
        remote = state.get(state.pane_remote_map, key)
        if remote or host == "local":
            # Same 15s ssh-backed call the poll loop offloads (#26). Handle it
            # in a child task so it cannot delay operation transitions behind it.
            content = await asyncio.to_thread(
                herdr.read_pane,
                pane_id,
                remote=remote,
                source="visible",
                host_id=key[0],
            )
        else:
            content = event.get("prompt", "Agent is blocked")
        # Keep the exact detector result as the typed capability. `frame()`
        # supplies the legacy fallback in `options` only; an undetected prompt
        # must not become answerable through `choices`.
        options = panes.detect_options(content)
        current = state.get(state.pane_dialogs, key)
        event_host = key[0]
        observation = event.get("output_revision")
        if not isinstance(observation, int) or isinstance(observation, bool):
            observation = event.get("revision")
        if not isinstance(observation, int) or isinstance(observation, bool):
            observation = state.get(state.pane_revisions, key)
        if not isinstance(observation, int) or isinstance(observation, bool):
            observation = current.get("observation") if current else None
        dialog = dialogs.ensure(
            pane_id,
            content,
            options,
            # Push hooks may omit display metadata. Preserve the identity that
            # poll/read already established instead of replacing a remote host
            # with the event handler's local default.
            agent=event.get("agent") or (current["agent"] if current else ""),
            project=event.get("project") or (
                current["project"] if current else state.get(state.pane_project_map, key, "")
            ),
            host=event_host or (current["host"] if current else state.get(state.pane_host_map, key, host)),
            observation=observation,
        )
        await broadcast(dialogs.frame(dialog))
    elif pane_id and status:
        key = state.resolve(host, pane_id)
        if key is None or key is state.AMBIGUOUS:
            return
        dialogs.clear(key)

    if pane_id and event.get("type") == "agent_event" and key is not None and key is not state.AMBIGUOUS:
        await broadcast({
            "type": "agents", "agents": [{
                "pane_id": pane_id,
                "host_id": key[0] if key else host,
                "agent": event.get("agent", ""),
                "status": status,
                "cwd": event.get("cwd", ""),
                "project": event.get("project", ""),
                "host": key[0] if key else host,
            }]
        })


async def event_push():
    tasks = set()

    def finished(task):
        tasks.discard(task)
        if not task.cancelled():
            task.exception()

    try:
        while True:
            event = await state.event_queue.get()
            # An event is the edge the backoff exists to be interrupted by: the
            # herdr hook reports a status change the next poll would otherwise
            # discover up to POLL_INTERVAL_MAX later.
            wake_poll_loop()
            if event.get("type") == "operation_event":
                await broadcast({"type": "operation", "operation": event.get("operation")})
                continue
            task = asyncio.create_task(_handle_pushed_event(event))
            tasks.add(task)
            task.add_done_callback(finished)
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
