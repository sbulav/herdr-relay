"""The listening socket: one port serving both HTTP and WebSocket, and `main`."""
import asyncio
import hmac
import json
import os
import signal
import time

try:
    from websockets.asyncio.server import serve
except ImportError:
    from websockets.server import serve
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from . import catalogs, config, herdr, lifecycle, panes, projects, protocol, push, state, transcripts, transport
from .audit import audit
from .config import log


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
    request_results = {}
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
            if not isinstance(msg, dict):
                await ws.send(json.dumps(protocol.command_error(None, "INVALID_REQUEST", "Request must be an object")))
                continue
            msg_type = msg.get("type")
            request_id = msg.get("request_id")
            if request_id and request_id in request_results:
                await ws.send(json.dumps(request_results[request_id]))
            elif msg_type in projects.COMMANDS:
                response = await asyncio.to_thread(projects.handle_command, msg)
                if response is None:
                    continue
                _remember_response(request_results, request_id, response)
                await ws.send(json.dumps(response))
                if msg_type in {"project_create", "project_save", "project_rename", "project_remove", "project_restore"} and response.get("type") == "command_ack":
                    audit(msg_type, ip, device, "", f"project_id={msg.get('project_id', '')}")
                    await transport.broadcast(projects.public_snapshot())
            elif msg_type == "catalog_refresh":
                if not isinstance(request_id, str) or not projects.REQUEST_ID_RE.fullmatch(request_id):
                    response = protocol.command_error(request_id, "INVALID_REQUEST", "request_id is required")
                else:
                    requested_host = msg.get("host_id")
                    if requested_host is not None and (
                        not isinstance(requested_host, str)
                        or not projects.HOST_ID_RE.fullmatch(requested_host)
                        or requested_host not in {host["id"] for host in herdr.configured_host_records()}
                    ):
                        response = protocol.command_error(request_id, "UNKNOWN_HOST", "Unknown host")
                    else:
                        response = {
                            "type": "command_ack",
                            "request_id": request_id,
                            "result": {"catalog_status": (await asyncio.to_thread(catalogs.refresh_all, host_id=requested_host))["catalog_status"]},
                        }
                _remember_response(request_results, request_id, response)
                await ws.send(json.dumps(response))
                if response.get("type") == "command_ack":
                    await transport.broadcast({"type": "catalogs", **catalogs.public_frame()})
            elif msg_type == "launch_session":
                response = await asyncio.to_thread(lifecycle.launch_session, msg)
                _remember_response(request_results, request_id, response)
                await ws.send(json.dumps(response))
            elif msg_type == "start_session":
                response = await asyncio.to_thread(lifecycle.start_session, msg)
                _remember_response(request_results, request_id, response)
                await ws.send(json.dumps(response))
            elif msg_type == "terminate_session":
                response = await asyncio.to_thread(lifecycle.terminate_session, msg)
                _remember_response(request_results, request_id, response)
                await ws.send(json.dumps(response))
            elif msg_type == "wake_host":
                response = await asyncio.to_thread(lifecycle.wake_host, msg)
                _remember_response(request_results, request_id, response)
                await ws.send(json.dumps(response))
            elif msg_type == "shutdown_host":
                response = await asyncio.to_thread(lifecycle.shutdown_host, msg)
                _remember_response(request_results, request_id, response)
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


def require_auth_token():
    if not config.AUTH_TOKEN:
        raise SystemExit("HERDR_RELAY_TOKEN is required; set it before starting the relay")


def _remember_response(request_results, request_id, response):
    if not request_id:
        return
    request_results[request_id] = response
    if len(request_results) > 512:
        request_results.pop(next(iter(request_results)))


async def main():
    require_auth_token()
    loop = asyncio.get_running_loop()
    state.event_loop = loop
    # Reconcile before accepting clients so an orphaned bookmark is never briefly
    # presented as available after a host configuration change.
    await asyncio.to_thread(projects.public_snapshot)
    await asyncio.to_thread(catalogs.refresh_all)
    await asyncio.to_thread(lifecycle.recover_start_operations)
    server = await serve(handle_client, "0.0.0.0", config.WS_PORT, process_request=process_request)
    background_tasks = [
        asyncio.create_task(transport.poll_loop(), name="poll-loop"),
        asyncio.create_task(transport.event_push(), name="event-push"),
        asyncio.create_task(transport.catalog_loop(), name="catalog-loop"),
    ]
    host_ids = [host["id"] for host in herdr.configured_host_records()]
    log.info("herdr-remote relay on :%d (WebSocket + HTTP)", config.WS_PORT)
    log.info("Polling configured hosts: %s", ", ".join(host_ids))
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
