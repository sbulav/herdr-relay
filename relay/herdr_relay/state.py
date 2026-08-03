"""Everything the relay remembers between polls, in one namespace.

A module, not a class: the read sites want `state.pane_activity[pane_id]`, and an
instance would buy nothing a module attribute does not already give — including
`patch.object(state, ...)` at a single address per name.

Nothing here is a configuration value and nothing here is derived from one; this
is live state, rebuilt from scratch on every start.
"""
import asyncio

clients = set()
last_statuses = {}
pane_activity = {}      # pane_id -> epoch milliseconds of the last status/output change
pane_revisions = {}     # pane_id -> output revision, or None when unavailable
pane_attention_states = {}  # pane_id -> last emitted attention state
event_queue = asyncio.Queue()
pane_remote_map = {}
session_target_map = {}
pane_session_refs = {}  # (remote, pane_id) -> {kind, value}; never sent to clients
request_results = {}
pane_cwd_map = {}      # pane_id -> (cwd, agent, remote, ambiguous agent/cwd)
subscriptions = {}     # ws -> pane_id the client is currently viewing
stream_sigs = {}       # (id(ws), pane_id) -> signature of the last blocks pushed
known_panes = set()
pane_response_options = {}
