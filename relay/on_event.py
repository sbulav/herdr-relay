#!/usr/bin/env python3
"""Local plugin hook — pushes a herdr agent event to the relay over HTTP.

Runs under herdr's plugin runner as plain `python3` with no virtualenv, so this
stays stdlib-only.

This used to send a UDP datagram to 127.0.0.1:8376. HTTP replaces it for two
reasons: the relay authenticates every HTTP request, where the UDP listener
accepted anything that reached the socket, and loopback UDP only ever worked when
the relay ran on the same machine as the agent. A remote host's hook now reaches
the relay like any other client, so pushed events work there too.

Environment:
  HERDR_RELAY        relay URL, ws/wss accepted and mapped to http/https
                     (default ws://127.0.0.1:8375)
  HERDR_RELAY_TOKEN  shared secret; without it the relay answers 401
  HERDR_HOST_ID      configured relay host ID; required when it differs from
                     the machine hostname
"""
import json
import os
import socket
import sys
import urllib.parse
import urllib.request

event = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON", "{}"))
data = event.get("data", {})
# The hook runs on an agent host, but the machine hostname is not necessarily
# the relay's configured identity. HERDR_HOST_ID is therefore the authority;
# event data cannot override it.
host_id = os.environ.get("HERDR_HOST_ID") or socket.gethostname().split(".")[0]

payload = {
    "type": "agent_event",
    "pane_id": data.get("pane_id", ""),
    "status": (data.get("agent_status") or "").lower(),
    "agent": (data.get("agent") or data.get("display_agent") or "").lower(),
    "project": os.path.basename(data.get("cwd", "")),
    "cwd": data.get("cwd", ""),
    "host_id": host_id,
    "host": socket.gethostname().split(".")[0],
}

relay = os.environ.get("HERDR_RELAY", "ws://127.0.0.1:8375")
parts = urllib.parse.urlsplit(relay)
scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme or "http")
# GET with the event on the query string, not POST with a body: the relay's HTTP
# surface is served by the websockets library, whose request parser accepts only
# GET and rejects any request carrying Content-Length before the relay's own
# handler sees it. The path does not matter — see docs/deployment.md.
url = urllib.parse.urlunsplit((
    scheme, parts.netloc, parts.path or "/event",
    urllib.parse.urlencode({"d": json.dumps(payload)}), "",
))

request = urllib.request.Request(url)
token = os.environ.get("HERDR_RELAY_TOKEN", "")
if token:
    request.add_header("Authorization", "Bearer " + token)

# A proxy must never swallow a loopback push. HTTP_PROXY set without a matching
# no_proxy is common enough, and the relay's default target is this machine, so
# a forward proxy would answer for it and the event would vanish. Remote relays
# still go through the proxy, which is what a proxy is for.
opener = urllib.request.build_opener()
if (urllib.parse.urlsplit(url).hostname or "") in ("localhost", "127.0.0.1", "::1"):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

try:
    # Short timeout on purpose: this hook runs inside herdr's status-change path,
    # and a slow or unreachable relay must not hold up the agent.
    with opener.open(request, timeout=2):
        pass
except Exception as exc:
    print(f"herdr-relay push failed: {exc!r}", file=sys.stderr)
