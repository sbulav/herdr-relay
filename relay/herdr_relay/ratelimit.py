"""Per-connection token buckets for the commands that reach a real host.

Every command metered here ends in a subprocess: a herdr CLI call, an ssh hop, or
a filesystem walk on a polled host. The relay is reachable from the public
internet behind one shared secret (#18), so a single authenticated connection
looping on `send_keys` is both a denial of service against the host and a way to
drive a live terminal faster than any human reviews what it does.

Two tiers, because the cost and the blast radius differ:

- `INPUT_COMMANDS` — keystrokes into a live terminal. Strict: a human tapping
  approvals never approaches the default, and a loop is bounded immediately.
- `HOST_COMMANDS` — everything else that shells out or walks a remote tree.
  Generous enough that a person browsing projects or paging through panes never
  notices it, tight enough that a loop cannot saturate the ssh path.

Buckets live in `handle_client`'s frame rather than in `state`: a connection that
goes away takes its buckets with it, so there is no registry to leak and nothing
to clean up in the `finally`. That also makes the limit per *connection* rather
than per token, which is the most this can be until per-device tokens land — one
shared secret means every phone is otherwise the same principal.

The rejection frame carries no retry hint. A backoff derived from a monotonic
clock cannot go in a golden contract frame, and a client that has been told to
slow down can compute its own delay from the burst it just spent.
"""
import time

from . import config, projects, protocol

# Keystrokes into a terminal a human is watching.
INPUT_COMMANDS = frozenset({"respond", "send_keys", "send_text"})

# Reads and writes that reach a host without typing into it. `project_*` comes
# from the handler table itself so a new project command is metered the day it is
# added rather than the day someone remembers this file.
HOST_COMMANDS = frozenset({
    "read_pane",
    "subscribe_pane",
    "catalog_refresh",
    "create_tab",
    "launch_session",
    "start_session",
    "cancel_start",
    "terminate_session",
    "wake_host",
    "shutdown_host",
} | set(projects.COMMANDS))

# Two error dialects coexist on this wire: typed commands answer with
# `command_error` and a code, while the older pane commands answer with a bare
# `error` and a message. A rate-limit rejection has to speak whichever dialect
# the command it rejects already spoke, or a client parses neither.
_UNTYPED_COMMANDS = frozenset({
    "respond",
    "read_pane",
    "send_keys",
    "send_text",
    "create_tab",
    "subscribe_pane",
})


class TokenBucket:
    """An allowance of `capacity` commands that refills at `per_second`.

    `now` is injectable because the only interesting assertions are about the
    passage of time, and a test that sleeps to make them is a test that
    intermittently fails on a loaded machine.

    A capacity of zero disables the bucket outright — an operator escape hatch for
    a private deployment, not a default.
    """

    def __init__(self, capacity, per_second, now=None):
        self.capacity = float(capacity)
        self.per_second = float(per_second)
        self._now = now or time.monotonic
        self._tokens = self.capacity
        self._updated = self._now()

    @property
    def disabled(self):
        return self.capacity <= 0

    def take(self):
        """Spend one token. True when the command may run, False when limited."""
        if self.disabled:
            return True
        now = self._now()
        elapsed = now - self._updated
        # A clock that went backwards would otherwise credit nothing and, worse,
        # leave `_updated` in the future so the next refill is negative too.
        if elapsed > 0 and self.per_second > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.per_second)
        self._updated = now
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True


class ConnectionLimits:
    """The buckets one client connection is metered by.

    Configuration is read here rather than at import, so a test — or an operator
    restarting the relay with new values — gets the values that were in `config`
    when the connection opened.
    """

    def __init__(self, now=None):
        self.input = TokenBucket(config.RATE_INPUT_BURST, config.RATE_INPUT_PER_SECOND, now)
        self.host = TokenBucket(config.RATE_HOST_BURST, config.RATE_HOST_PER_SECOND, now)

    def bucket_for(self, msg_type):
        """The bucket governing `msg_type`, or None when it is not metered.

        Unmetered: `agent_event` (queued, never shelled out), `unsubscribe_pane`
        (drops server-side state) and the LEGACY (#14) push subscription pair.
        """
        if msg_type in INPUT_COMMANDS:
            return self.input
        if msg_type in HOST_COMMANDS:
            return self.host
        return None

    def allows(self, msg_type):
        bucket = self.bucket_for(msg_type)
        return True if bucket is None else bucket.take()


def rejection(msg_type, request_id):
    """The frame a rate-limited command answers with, in that command's dialect."""
    if msg_type in _UNTYPED_COMMANDS:
        return {"type": "error", "message": "rate limited, slow down"}
    return protocol.command_error(request_id, "RATE_LIMITED", "Too many requests, slow down")
