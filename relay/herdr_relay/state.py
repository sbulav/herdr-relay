"""Everything the relay remembers between polls, in one namespace.

A module, not a class: the read sites want stable module-level maps keyed by
`(host_id, pane_id)`, and an instance would buy nothing a module attribute does not already give — including
`patch.object(state, ...)` at a single address per name.

Nothing here is a configuration value and nothing here is derived from one; this
is live state, rebuilt from scratch on every start.
"""
import asyncio

clients = set()
last_statuses = {}
pane_activity = {}      # (host_id, pane_id) -> epoch milliseconds of the last change
pane_revisions = {}     # (host_id, pane_id) -> output revision, or None when unavailable
pane_attention_states = {}  # (host_id, pane_id) -> last emitted attention state
event_queue = asyncio.Queue()
# Set by the asyncio server before any durable-operation worker can emit. A
# worker thread must schedule queue writes onto the loop; asyncio queues are
# intentionally not thread-safe.
event_loop = None
pane_remote_map = {}
session_target_map = {}
pane_session_refs = {}  # (host_id, pane_id) -> valid ref or None for an invalid supplied ref
pane_cwd_map = {}      # (host_id, pane_id) -> (cwd, agent, remote, ambiguous agent/cwd)
subscriptions = {}     # ws -> (host_id, pane_id) the client is currently viewing
stream_sigs = {}       # (id(ws), host_id, pane_id) -> signature of the last blocks pushed
known_panes = set()    # legacy pane-id index; authoritative keys live in known_pane_keys
known_pane_keys = set()
pane_hosts = {}        # pane_id -> set of host IDs currently exposing it
pane_response_options = {}
# The current relay-owned blocked dialog per pane. Counters intentionally remain
# after `clear()` so a reused pane ID cannot recycle an old dialog identity.
pane_dialogs = {}      # (host_id, pane_id) -> dialog
pane_dialog_revisions = {}
pane_host_map = {}     # (host_id, pane_id) -> public host ID
pane_project_map = {}  # (host_id, pane_id) -> public project label
# Additive display metadata learned from Herdr.  These maps deliberately use
# the host-qualified pane identity so identical pane IDs on two hosts cannot
# borrow one another's labels.
pane_workspace_map = {}  # (host_id, pane_id) -> (workspace_id, workspace_name)
pane_tab_map = {}        # (host_id, pane_id) -> (tab_id, tab_name)
pane_activity_titles = {}  # (host_id, pane_id) -> bounded current activity title
pane_statuses = {}  # (host_id, pane_id) -> latest observed status for live metadata


AMBIGUOUS = object()


def pane_key(host_id, pane_id):
    """Return the canonical in-memory identity for a pane."""
    return (host_id or "local", pane_id)


def get(mapping, key, default=None):
    """Read state by its canonical key.

    Bare pane IDs are deliberately never aliases: a pane ID is only unique
    within a host, and retaining a stale alias after a host disappears can
    route a command to the wrong machine.
    """
    return mapping.get(key, default)


def pop(mapping, key, default=None):
    return mapping.pop(key, default)


def resolve(host_id, pane_id):
    """Resolve a wire pane reference, rejecting ambiguous legacy IDs."""
    if not isinstance(pane_id, str) or not pane_id:
        return None
    if host_id is not None:
        if not isinstance(host_id, str) or not host_id:
            return None
        key = pane_key(host_id, pane_id)
        return key if key in known_pane_keys else None
    hosts = pane_hosts.get(pane_id)
    if not hosts:
        return None
    if len(hosts) != 1:
        return AMBIGUOUS
    key = pane_key(next(iter(hosts)), pane_id)
    return key if key in known_pane_keys else None
# Poll pacing (#19). `poll_idle_streak` counts consecutive cycles that saw
# nothing worth staying fast for; `poll_wakeup` is how an edge — a client
# connecting or subscribing, an event pushed by the herdr hook — cuts the
# current backoff short instead of waiting it out.
poll_idle_streak = 0
poll_wakeup = asyncio.Event()
