"""herdr-remote relay — polls herdr, accepts push events over HTTP, broadcasts over WebSocket.

Start it through `relay/herdr-relay.py`, which carries the PEP 723 dependency
metadata `uv run` needs — inline metadata only works on a single file, and a
package cannot hold it.

The work lives in the siblings, roughly outermost-first: `server` owns the socket
and `main`, `transport` the two loops that produce frames, `lifecycle` the writes
a client can ask for, `protocol` the frame shapes, `herdr` the CLI calls,
`panes`/`transcripts` the parsing of what comes back, and `config`/`state` the
values everything else reads.

Between modules, references go through the module object rather than the name:
`config.AUTH_TOKEN` is read at call time and `state.known_panes` is mutated in
place, so each tunable and each map stays patchable at exactly one address. `log`
and `audit` are the exceptions worth importing by name — a logger and one write to
it, singletons no test replaces. This module re-exports the entry points so
`herdr_relay.main` and `herdr_relay.handle_client` still name the same functions
they always did; it holds no logic of its own.
"""
from . import (  # noqa: F401
    config,
    herdr,
    lifecycle,
    hosts,
    panes,
    project_fs,
    projects,
    presets,
    protocol,
    push,
    server,
    state,
    transcripts,
    transport,
)
from .audit import audit  # noqa: F401
from .config import log  # noqa: F401
from .lifecycle import (  # noqa: F401
    launch_session,
    shutdown_host,
    terminate_session,
    wake_host,
)
from .server import (  # noqa: F401
    fail_on_background_exit,
    handle_client,
    main,
    process_request,
    require_auth_token,
)
from .transport import (  # noqa: F401
    _poll_once,
    broadcast,
    event_push,
    poll_loop,
)
