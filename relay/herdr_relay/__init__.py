"""herdr-remote relay — polls herdr, accepts push events over HTTP, broadcasts over WebSocket.

Start it through `relay/herdr-relay.py`, which carries the PEP 723 dependency
metadata `uv run` needs — inline metadata only works on a single file, and a
package cannot hold it.
"""
import asyncio, hmac, json, os, signal, subprocess, time, uuid

try:
    from websockets.asyncio.server import serve
except ImportError:
    from websockets.server import serve
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

# Cross-module references go through the module object rather than the name:
# `config.AUTH_TOKEN` is read at call time and `state.known_panes` is mutated in
# place, so each tunable and each map stays patchable at exactly one address as the
# rest of this module moves out into siblings. `log` and `audit` are the exceptions
# worth importing by name — a logger and one write to it, singletons no test
# replaces.
from . import config, herdr, panes, presets, protocol, push, state, transcripts
from .audit import audit
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


def fail_on_background_exit(task, stop):
    if stop.done():
        return
    if task.cancelled():
        stop.set_exception(RuntimeError(f"{task.get_name()} was cancelled"))
        return
    exception = task.exception()
    if exception is None:
        exception = RuntimeError(f"{task.get_name()} exited unexpectedly")
    stop.set_exception(exception)


async def poll_loop():
    while True:
        try:
            await _poll_once()
        except Exception:
            log.exception("poll cycle failed; retrying")
        await asyncio.sleep(config.POLL_INTERVAL)


async def _poll_once():
    state.pane_session_refs.clear()  # Host threads populate refs while herdr.get_all_agents() awaits.
    agents, hosts = await herdr.get_all_agents()
    current_pane_ids = {a["pane_id"] for a in agents}
    state.pane_remote_map.clear()
    state.session_target_map.clear()
    state.pane_cwd_map.clear()
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
        state.pane_revisions[pid] = revision
        protocol.add_pane_metadata(a, pid)

    # Always send a complete snapshot. In particular, an empty snapshot
    # removes stale agents after every remote host goes offline.
    await broadcast({
        "type": "agents", "agents": protocol.public_agents(agents),
        "presets": presets.public_presets(),
        "hosts": hosts,
    })
    # Read every newly blocked pane off the event loop, and all of them at once:
    # `herdr pane read` shells out (over ssh for remote hosts) with a 15s timeout,
    # and a herd of agents tends to block together. Done inline, one slow host
    # freezes every client on exactly the event they care about most.
    newly_blocked = [
        a for a in agents
        if a["status"] == "blocked" and state.last_statuses.get(a["pane_id"]) != "blocked"
    ]
    blocked_content = dict(zip(
        (a["pane_id"] for a in newly_blocked),
        await asyncio.gather(*(
            asyncio.to_thread(herdr.read_pane, a["pane_id"], remote=a.get("remote"))
            for a in newly_blocked
        )),
    ))
    for a in agents:
        pid, status = a["pane_id"], a["status"]
        if status == "blocked" and state.last_statuses.get(pid) != "blocked":
            content = blocked_content.get(pid, "")
            options = panes.detect_options(content)
            state.pane_response_options[pid] = {
                option.lower() for option in (options or panes.TOOL_OPTIONS)
            }
            await broadcast({
                "type": "blocked", "pane_id": pid,
                "agent": a["agent"], "project": a["project"],
                "host": a.get("host", "local"),
                "prompt": content[:500],
                "options": options or panes.TOOL_OPTIONS
            })
            await push.send_web_push(
                title=f"🐑 {a['project']} blocked",
                body=content[:120],
                url=f"/?pane={pid}",
            )
        if status != "blocked" and state.last_statuses.get(pid) == "blocked":
            state.pane_response_options.pop(pid, None)
            await push.send_web_push("", "", clear=True)
        state.last_statuses[pid] = status
    for pid in set(state.last_statuses) - current_pane_ids:
        del state.last_statuses[pid]
        state.pane_response_options.pop(pid, None)
        state.pane_activity.pop(pid, None)
        state.pane_revisions.pop(pid, None)
        state.pane_attention_states.pop(pid, None)

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


async def event_push():
    while True:
        event = await state.event_queue.get()
        pane_id = event.get("pane_id", "")
        status = event.get("status", "")
        host = event.get("host", "local")

        if status == "blocked" and pane_id:
            remote = state.pane_remote_map.get(pane_id)
            if remote or host == "local":
                # Same 15s ssh-backed call the poll loop offloads (#26); inline
                # here it would stall every client on a pushed blocked event.
                content = await asyncio.to_thread(herdr.read_pane, pane_id, remote=remote)
            else:
                content = event.get("prompt", "Agent is blocked")
            options = panes.detect_options(content)
            await broadcast({
                "type": "blocked", "pane_id": pane_id,
                "agent": event.get("agent", ""),
                "project": event.get("project", ""),
                "host": host,
                "prompt": content[:500],
                "options": options or panes.TOOL_OPTIONS
            })

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


async def process_request(connection, request):
    """Serve plain HTTP on the same port as WebSocket.

    Everything here is a GET: the websockets library's request parser accepts no
    other method and rejects any request carrying a body, so an event arrives as
    a `?d=<url-encoded JSON>` query rather than a POST payload.
    """
    from websockets.http11 import Response
    from websockets.datastructures import Headers

    # Token auth
    token = None
    for key, value in request.headers.raw_items():
        if key.lower() == "authorization":
            token = value.removeprefix("Bearer ").strip()
            break
    # LEGACY (#14): query-param auth exists because a browser cannot set headers
    # on a WebSocket handshake. It leaks the token into every proxy access log,
    # so it is retired with web/. Native clients send Authorization.
    # Also check query param ?token=
    if not token and "token=" in (request.path or ""):
        import urllib.parse
        _, qs = request.path.split("?", 1) if "?" in request.path else (request.path, "")
        params = urllib.parse.parse_qs(qs)
        token = params.get("token", [None])[0]
    # compare_digest raises TypeError on non-ASCII str, so compare encoded bytes.
    if not token or not hmac.compare_digest(token.encode("utf-8", "replace"), config.AUTH_TOKEN.encode("utf-8", "replace")):
        headers = Headers([("Content-Type", "text/plain")])
        return Response(401, "Unauthorized", headers, b"Invalid token\n")

    # Check if this is a WebSocket upgrade
    upgrade = None
    for key, value in request.headers.raw_items():
        if key.lower() == "upgrade":
            upgrade = value.lower()
    if upgrade == "websocket":
        return None  # proceed with WebSocket handshake

    # Event push — checked before the static routes below, because those match on
    # path and the plugin hook's relay URL is usually just the host, i.e. "/".
    # A `d=` query is unambiguous: nothing that serves web/ carries one.
    import urllib.parse
    if "?" in (request.path or ""):
        _, qs = request.path.split("?", 1)
        params = urllib.parse.parse_qs(qs)
        if "d" in params:
            try:
                # parse_qs has already decoded this. Decoding it a second time
                # would eat any pane id that looks like an escape: tmux pane 22
                # is "%22", which unquote turns into a bare double quote.
                event = json.loads(params["d"][0])
                state.event_queue.put_nowait(event)
            except Exception:
                log.warning("Discarded malformed pushed event")
            headers = Headers([("Access-Control-Allow-Origin", "*")])
            return Response(200, "OK", headers, b"ok\n")

    # For CORS preflight
    if request.path and "OPTIONS" in str(request.headers):
        headers = Headers([
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type"),
        ])
        return Response(204, "No Content", headers, b"")

    # LEGACY (#14): the four static routes below (/, /index.html, /sw.js,
    # /logo.svg) and /api/vapid-public-key serve the browser PWA out of web/.
    # They are deleted together with web/ once a herdr-mobile build without the
    # WebView fallback is actually installed on the phone.
    # Serve web app for GET / or GET /index.html
    path = (request.path or "/").split("?")[0]
    if path in ("/", "/index.html"):
        index_path = os.path.join(config.WEB_DIR, "index.html")
        if os.path.isfile(index_path):
            with open(index_path, "rb") as f:
                body = f.read()
            headers = Headers([
                ("Content-Type", "text/html; charset=utf-8"),
                ("Cache-Control", "no-cache"),
            ])
            return Response(200, "OK", headers, body)

    # Serve service worker
    if path == "/sw.js":
        sw_path = os.path.join(config.WEB_DIR, "sw.js")
        if os.path.isfile(sw_path):
            with open(sw_path, "rb") as f:
                body = f.read()
            headers = Headers([
                ("Content-Type", "application/javascript"),
                ("Cache-Control", "no-cache"),
                ("Service-Worker-Allowed", "/"),
            ])
            return Response(200, "OK", headers, body)

    # Serve VAPID public key
    if path == "/api/vapid-public-key":
        body = json.dumps({"publicKey": config.VAPID_PUBLIC_KEY}).encode()
        headers = Headers([
            ("Content-Type", "application/json"),
            ("Access-Control-Allow-Origin", "*"),
        ])
        return Response(200, "OK", headers, body)

    # Serve logo.svg
    if path == "/logo.svg":
        svg_path = os.path.join(config.WEB_DIR, "logo.svg")
        if os.path.isfile(svg_path):
            with open(svg_path, "rb") as f:
                body = f.read()
            headers = Headers([("Content-Type", "image/svg+xml")])
            return Response(200, "OK", headers, body)

    headers = Headers([("Access-Control-Allow-Origin", "*")])
    return Response(200, "OK", headers, b"ok\n")


async def handle_client(ws):
    remote_addr = getattr(ws, "remote_address", None)
    ip = remote_addr[0] if remote_addr else "unknown"
    request = getattr(ws, "request", None)
    headers = request.headers if request else getattr(ws, "request_headers", {})
    ua = headers.get("User-Agent", "unknown")
    origin = headers.get("Origin", "")

    device = "unknown"
    ua_lower = ua.lower()
    if "iphone" in ua_lower or "ipad" in ua_lower:
        device = "iOS"
    elif "android" in ua_lower:
        device = "Android"
    elif "macintosh" in ua_lower or "mac os" in ua_lower:
        device = "macOS"
    elif "windows" in ua_lower:
        device = "Windows"
    elif "linux" in ua_lower:
        device = "Linux"
    elif "telegram" in ua_lower or "bot" in ua_lower:
        device = "bot"
    elif "python" in ua_lower:
        device = "script"

    log.info("Client connected: ip=%s device=%s origin=%s", ip, device, origin or "-")
    connected_at = time.monotonic()
    try:
        # Handshake before registering, so the ordering this frame exists to
        # guarantee is a property of this function rather than of the library.
        # `broadcast` fans out to everything in `state.clients`; a socket in that set
        # before its `server_info` is written is one an `agents` frame could
        # reach first. Today it cannot — websockets writes a str frame to the
        # transport before its first await, so there is no suspension point
        # between the two lines below — but that is an internal detail.
        #
        # Still inside the try: a client that dies during the handshake is a
        # closed connection like any other, and the finally below discards, so
        # cleaning up a socket that was never registered is a no-op.
        await ws.send(json.dumps(protocol.server_info()))
        state.clients.add(ws)
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg_type = msg.get("type")
            request_id = msg.get("request_id")
            if request_id and request_id in state.request_results:
                await ws.send(json.dumps(state.request_results[request_id]))
            elif msg_type == "launch_session":
                response = await asyncio.to_thread(launch_session, msg)
                if request_id:
                    state.request_results[request_id] = response
                    if len(state.request_results) > 512:
                        state.request_results.pop(next(iter(state.request_results)))
                await ws.send(json.dumps(response))
            elif msg_type == "terminate_session":
                response = await asyncio.to_thread(terminate_session, msg)
                if request_id:
                    state.request_results[request_id] = response
                    if len(state.request_results) > 512:
                        state.request_results.pop(next(iter(state.request_results)))
                await ws.send(json.dumps(response))
            elif msg_type == "wake_host":
                response = await asyncio.to_thread(wake_host, msg)
                if request_id:
                    state.request_results[request_id] = response
                    if len(state.request_results) > 512:
                        state.request_results.pop(next(iter(state.request_results)))
                await ws.send(json.dumps(response))
            elif msg_type == "shutdown_host":
                response = await asyncio.to_thread(shutdown_host, msg)
                if request_id:
                    state.request_results[request_id] = response
                    if len(state.request_results) > 512:
                        state.request_results.pop(next(iter(state.request_results)))
                await ws.send(json.dumps(response))
            elif msg_type == "respond":
                pane_id = msg["pane_id"]
                if pane_id not in state.known_panes:
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                text = msg.get("text", "")
                normalized_text = text.strip().lower()
                allowed_responses = panes.SAFE_RESPONSES | state.pane_response_options.get(pane_id, set())
                if normalized_text not in allowed_responses:
                    await ws.send(json.dumps({"type": "error", "message": "response not in allowlist"}))
                    continue
                remote = state.pane_remote_map.get(pane_id)
                log.info("Response from %s (%s): pane=%s text=%r", ip, device, pane_id, text)
                audit("respond", ip, device, pane_id, f"text={text!r}")
                kind, payload = panes.respond_action(text)
                if kind == "keys":
                    await asyncio.to_thread(herdr.run_herdr, "pane", "send-keys", pane_id, *payload, remote=remote)
                else:
                    await asyncio.to_thread(herdr.run_herdr, "pane", "send-text", pane_id, payload + "\n", remote=remote)
            elif msg_type == "agent_event":
                state.event_queue.put_nowait(msg)
            elif msg_type == "read_pane":
                pane_id = msg["pane_id"]
                if pane_id not in state.known_panes:
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                # herdr rejects a non-numeric --lines by printing an error and
                # exiting 0, which would reach the client as pane content.
                lines = herdr._read_pane_lines(msg.get("lines"))
                remote = state.pane_remote_map.get(pane_id)
                content = await asyncio.to_thread(herdr.run_herdr, "pane", "read", pane_id, "--lines", str(lines), "--source", "recent", remote=remote)
                payload = {"type": "pane_content", "pane_id": pane_id, "content": content}
                protocol.add_pane_metadata(payload, pane_id)
                # Include structured blocks on demand without changing which pane
                # this client explicitly subscribed to for live updates.
                try:
                    blocks, sig = await asyncio.to_thread(transcripts.blocks.pane_blocks, pane_id)
                except Exception:
                    blocks, sig = None, None
                if blocks is not None:
                    payload["output_blocks"] = blocks
                    if state.subscriptions.get(ws) == pane_id:
                        state.stream_sigs[(id(ws), pane_id)] = sig
                await ws.send(json.dumps(payload))
                options = panes.detect_options(content) if state.last_statuses.get(pane_id) == "blocked" else None
                if options:
                    state.pane_response_options[pane_id] = {option.lower() for option in options}
                    await ws.send(json.dumps({
                        "type": "blocked",
                        "pane_id": pane_id,
                        "prompt": content[:500],
                        "options": options,
                    }))
            elif msg_type == "subscribe_pane":
                pane_id = msg.get("pane_id")
                if pane_id in state.pane_cwd_map:
                    previous = state.subscriptions.get(ws)
                    state.subscriptions[ws] = pane_id
                    if previous is not None:
                        state.stream_sigs.pop((id(ws), previous), None)
                    try:
                        blocks, sig = await asyncio.to_thread(transcripts.blocks.pane_blocks, pane_id)
                    except Exception:
                        blocks, sig = None, None
                    if blocks is not None:
                        state.stream_sigs[(id(ws), pane_id)] = sig
                        payload = {"type": "pane_content", "pane_id": pane_id, "output_blocks": blocks}
                        protocol.add_pane_metadata(payload, pane_id)
                        await ws.send(json.dumps(payload))
            elif msg_type == "unsubscribe_pane":
                previous = state.subscriptions.pop(ws, None)
                if previous is not None:
                    state.stream_sigs.pop((id(ws), previous), None)
            elif msg_type == "send_keys":
                pane_id = msg["pane_id"]
                if pane_id not in state.known_panes:
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                keys = msg.get("keys", [])
                if not all(panes.is_safe_key(k) for k in keys):
                    await ws.send(json.dumps({"type": "error", "message": "keys contain disallowed values"}))
                    continue
                remote = state.pane_remote_map.get(pane_id)
                log.info("Keys from %s (%s): pane=%s keys=%s", ip, device, pane_id, keys)
                audit("send_keys", ip, device, pane_id, f"keys={keys}")
                await asyncio.to_thread(herdr.run_herdr, "pane", "send-keys", pane_id, *keys, remote=remote)
            elif msg_type == "send_text":
                pane_id = msg["pane_id"]
                if pane_id not in state.known_panes:
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                text = msg.get("text", "")
                if not text or len(text) > 1000:
                    await ws.send(json.dumps({"type": "error", "message": "text empty or too long"}))
                    continue
                remote = state.pane_remote_map.get(pane_id)
                log.info("Text from %s (%s): pane=%s text=%r", ip, device, pane_id, text)
                audit("send_text", ip, device, pane_id, f"text={text!r}")
                await asyncio.to_thread(herdr.run_herdr, "pane", "send-text", pane_id, text, remote=remote)
            elif msg_type == "create_tab":
                workspace_id = msg.get("workspace_id", "")
                if workspace_id:
                    log.info("Create tab from %s (%s): workspace=%s", ip, device, workspace_id)
                    audit("create_tab", ip, device, "", f"workspace={workspace_id}")
                    await asyncio.to_thread(herdr.run_herdr, "tab", "create", "--workspace", workspace_id, "--focus")
                    await ws.send(json.dumps({"type": "tab_created", "ok": True}))
                else:
                    await ws.send(json.dumps({"type": "error", "message": "workspace_id required"}))
            # LEGACY (#14): browser-PWA only, retired with web/.
            elif msg_type == "push_subscribe":
                if push.subscribe(msg.get("subscription")):
                    log.info("Push subscription added from %s (%s)", ip, device)
                await ws.send(json.dumps({"type": "push_subscribed", "ok": True}))
            elif msg_type == "push_unsubscribe":
                push.unsubscribe(msg.get("subscription"))
                await ws.send(json.dumps({"type": "push_unsubscribed", "ok": True}))
    except (ConnectionClosedError, ConnectionClosedOK):
        pass
    finally:
        duration = int(time.monotonic() - connected_at)
        log.info("Client disconnected: ip=%s device=%s duration=%ds", ip, device, duration)
        state.clients.discard(ws)
        state.subscriptions.pop(ws, None)
        for key in [k for k in state.stream_sigs if k[0] == id(ws)]:
            state.stream_sigs.pop(key, None)


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


def require_auth_token():
    if not config.AUTH_TOKEN:
        raise SystemExit("HERDR_RELAY_TOKEN is required; set it before starting the relay")


async def main():
    require_auth_token()
    loop = asyncio.get_running_loop()
    server = await serve(handle_client, "0.0.0.0", config.WS_PORT, process_request=process_request)
    background_tasks = [
        asyncio.create_task(poll_loop(), name="poll-loop"),
        asyncio.create_task(event_push(), name="event-push"),
    ]
    hosts = ["local"] + config.REMOTES
    log.info("herdr-remote relay on :%d (WebSocket + HTTP)", config.WS_PORT)
    log.info("Polling: %s", ", ".join(hosts))
    stop = loop.create_future()
    for task in background_tasks:
        task.add_done_callback(lambda completed: fail_on_background_exit(completed, stop))
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set_result, None)
    try:
        await stop
    finally:
        server.close()
        await server.wait_closed()
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
