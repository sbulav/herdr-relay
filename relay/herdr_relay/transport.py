"""The two loops that produce frames, and the one function that sends them.

`broadcast` is the fan-out over `state.clients`; `poll_loop` asks herdr what
changed and `event_push` drains events the plugin hook pushed in. Both loops call
`broadcast` by plain name — they are the only callers, and a test that wants to
capture frames patches it here.
"""
import asyncio
import json

from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from . import catalogs, config, dialogs, herdr, operations, panes, projects, protocol, push, state, transcripts
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
    changed = False
    current_pane_ids = {a["pane_id"] for a in agents}
    state.pane_remote_map.clear()
    state.session_target_map.clear()
    state.pane_cwd_map.clear()
    state.pane_host_map.clear()
    state.pane_project_map.clear()
    state.known_panes.clear()
    state.known_panes.update(current_pane_ids)
    agent_cwd_counts = {}
    for a in agents:
        if a.get("agent") in ("claude", "opencode") and a.get("cwd"):
            cwd_key = (a.get("remote"), a["cwd"], a["agent"])
            agent_cwd_counts[cwd_key] = agent_cwd_counts.get(cwd_key, 0) + 1
    for a in agents:
        state.pane_remote_map[a["pane_id"]] = a.get("remote")
        state.session_target_map[protocol.session_id(a["host"], a["pane_id"])] = (a["pane_id"], a.get("remote"))
        cwd_key = (a.get("remote"), a.get("cwd", ""), a.get("agent", ""))
        state.pane_cwd_map[a["pane_id"]] = (
            a.get("cwd", ""), a.get("agent", ""), a.get("remote"),
            agent_cwd_counts.get(cwd_key, 0) > 1,
        )
        state.pane_host_map[a["pane_id"]] = a.get("host", "local")
        state.pane_project_map[a["pane_id"]] = a.get("project", "")
        pid, status = a["pane_id"], a["status"]
        # Snapshot broadcast precedes the `state.last_statuses` update below, so this
        # still reads the prior poll's status for idle-after-work detection.
        previous_status = state.last_statuses.get(pid)
        attention = protocol.attention_state(status, previous_status, state.pane_attention_states.get(pid))
        if attention is None:
            state.pane_attention_states.pop(pid, None)
        else:
            state.pane_attention_states[pid] = attention
        revision = a.get("output_revision")
        previous_revision = state.pane_revisions.get(pid)
        if (
            pid not in state.pane_activity
            or status != previous_status
            or revision != previous_revision
        ):
            state.pane_activity[pid] = protocol.now_ms()
            changed = True
        state.pane_revisions[pid] = revision
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
        (a["pane_id"] for a in blocked_agents),
        await asyncio.gather(*(
            asyncio.to_thread(
                herdr.read_pane,
                a["pane_id"],
                remote=a.get("remote"),
                source="visible",
            )
            for a in blocked_agents
        )),
    ))
    for a in agents:
        pid, status = a["pane_id"], a["status"]
        if status == "blocked":
            content = blocked_content.get(pid, "")
            options = panes.detect_options(content)
            previous_dialog = state.pane_dialogs.get(pid)
            dialog = dialogs.ensure(
                pid,
                content,
                options,
                agent=a.get("agent", ""),
                project=a.get("project", ""),
                host=a.get("host", "local"),
                observation=a.get("output_revision"),
            )
            if dialog is not previous_dialog:
                await broadcast(dialogs.frame(dialog))
            if state.last_statuses.get(pid) != "blocked":
                await push.send_web_push(
                    title=f"🐑 {a['project']} blocked",
                    body=content[:120],
                    url=f"/?pane={pid}",
                )
        if status != "blocked" and state.last_statuses.get(pid) == "blocked":
            dialogs.clear(pid)
            await push.send_web_push("", "", clear=True)
        state.last_statuses[pid] = status
    for pid in set(state.last_statuses) - current_pane_ids:
        del state.last_statuses[pid]
        dialogs.clear(pid)
        state.pane_activity.pop(pid, None)
        state.pane_revisions.pop(pid, None)
        state.pane_attention_states.pop(pid, None)
        state.pane_host_map.pop(pid, None)
        state.pane_project_map.pop(pid, None)

    # Live-stream structured transcript blocks to subscribed clients. Only
    # watched Claude panes are read; a changed signature (path or content)
    # triggers a push. Failures are swallowed so one bad host can't stall.
    watchers = {
        pid: [ws for ws, subscribed_pid in list(state.subscriptions.items()) if subscribed_pid == pid]
        for pid in set(state.subscriptions.values()) if pid in current_pane_ids
    }
    pane_results = await asyncio.gather(
        *(asyncio.to_thread(transcripts.blocks.pane_blocks, pid) for pid in watchers),
        return_exceptions=True,
    )
    for pid, result in zip(watchers, pane_results):
        if isinstance(result, Exception):
            continue
        blocks, sig = result
        if blocks is None:
            continue
        frame = {"type": "pane_content", "pane_id": pid, "output_blocks": blocks}
        protocol.add_pane_metadata(frame, pid)
        payload = json.dumps(frame)
        for ws in watchers[pid]:
            key = (id(ws), pid)
            if state.stream_sigs.get(key) == sig:
                continue
            state.stream_sigs[key] = sig
            try:
                await ws.send(payload)
            except Exception:
                pass


async def _handle_pushed_event(event):
    pane_id = event.get("pane_id", "")
    status = event.get("status", "")
    host = event.get("host", "local")

    if status == "blocked" and pane_id:
        # A hook can race the next snapshot. Do not create an actionable
        # dialog for a pane the relay cannot currently route; the event loop
        # has already woken polling, which will publish it once observable.
        if pane_id not in state.known_panes:
            return
        remote = state.pane_remote_map.get(pane_id)
        if remote or host == "local":
            # Same 15s ssh-backed call the poll loop offloads (#26). Handle it
            # in a child task so it cannot delay operation transitions behind it.
            content = await asyncio.to_thread(
                herdr.read_pane,
                pane_id,
                remote=remote,
                source="visible",
            )
        else:
            content = event.get("prompt", "Agent is blocked")
        # Keep the exact detector result as the typed capability. `frame()`
        # supplies the legacy fallback in `options` only; an undetected prompt
        # must not become answerable through `choices`.
        options = panes.detect_options(content)
        current = state.pane_dialogs.get(pane_id)
        event_host = event.get("host")
        observation = event.get("output_revision")
        if not isinstance(observation, int) or isinstance(observation, bool):
            observation = event.get("revision")
        if not isinstance(observation, int) or isinstance(observation, bool):
            observation = state.pane_revisions.get(pane_id)
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
                current["project"] if current else state.pane_project_map.get(pane_id, "")
            ),
            host=event_host or (current["host"] if current else state.pane_host_map.get(pane_id, host)),
            observation=observation,
        )
        await broadcast(dialogs.frame(dialog))
    elif pane_id and status:
        dialogs.clear(pane_id)

    if pane_id and event.get("type") == "agent_event":
        await broadcast({
            "type": "agents", "agents": [{
                "pane_id": pane_id,
                "agent": event.get("agent", ""),
                "status": status,
                "cwd": event.get("cwd", ""),
                "project": event.get("project", ""),
                "host": host,
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
