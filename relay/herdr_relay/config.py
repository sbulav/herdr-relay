"""Every tunable the relay reads from the environment, plus the loggers.

Read through the module object — `config.AUTH_TOKEN`, not
`from .config import AUTH_TOKEN` — so a test patching one of these patches it for
every caller. `log` is the one exception worth importing by name: it is a
singleton the tests never replace, and `logging.getLogger` hands back the same
object either way.

Every variable here is documented in docs/deployment.md; keep the two in step.
"""
import logging
import os
import shutil
import sys
from logging.handlers import RotatingFileHandler


def _get_log_dir():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Logs/herdr-remote")
    if os.path.isdir("/var/log") and os.access("/var/log", os.W_OK):
        return "/var/log/herdr-remote"
    return os.path.expanduser("~/.local/state/herdr-remote/log")


# The static routes serve web/ from the repo root. Resolve it once,
# here, rather than from __file__ at each route: this file sits inside the package
# rather than beside web/, and three separate copies of the same ".." arithmetic
# is how that kind of move breaks quietly.
WEB_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "web")
)

LOG_DIR = os.environ.get("HERDR_LOG_DIR", _get_log_dir())
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "relay.log")
AUDIT_FILE = os.path.join(LOG_DIR, "audit.log")

_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
_file_handler.setFormatter(_formatter)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_formatter)

log = logging.getLogger("herdr-relay")
log.setLevel(logging.INFO)
log.addHandler(_file_handler)
log.addHandler(_console_handler)
logging.getLogger("websockets").setLevel(logging.WARNING)

HERDR = os.environ.get("HERDR_BIN") or shutil.which("herdr") or "/opt/homebrew/bin/herdr"
WS_PORT = int(os.environ.get("HERDR_RELAY_PORT", "8375"))
POLL_INTERVAL = 2
# The ceiling the poll loop backs off to when nothing is happening — no client
# connected, no agent working or blocked, no durable operation in flight,
# nothing changed since the last cycle (#19). The floor stays
# POLL_INTERVAL and is restored on the first edge, so this trades latency only
# in the state where no one is looking at the result.
POLL_INTERVAL_MAX = float(os.environ.get("HERDR_POLL_INTERVAL_MAX", "10"))
# Geometric rather than a step to the ceiling: a single quiet cycle between two
# busy ones should cost a fraction of a second, not the full backoff.
POLL_BACKOFF_FACTOR = float(os.environ.get("HERDR_POLL_BACKOFF_FACTOR", "1.5"))

# SSH connection multiplexing (#19). Polling opens two SSH connections per
# remote host per cycle — a `true` reachability probe and `herdr pane list` —
# and each one pays a TCP handshake, a key exchange and an authentication.
# A shared master connection pays that once per ControlPersist window instead.
#
# ControlPath goes through a Unix socket, so the whole path must fit in
# sockaddr_un: 104 bytes on darwin. Exceeding it does not degrade to an
# unmultiplexed connection, it fails the call outright, which would take every
# remote host offline — hence the length check in herdr.ssh_options() rather
# than a bare string here.
SSH_CONTROL_DIR = os.environ.get(
    "HERDR_SSH_CONTROL_DIR",
    "~/.local/state/herdr-relay/ssh",
)  # `~` expanded at the point of use, so a configured one is expanded too
# Seconds an idle master lingers. Comfortably longer than POLL_INTERVAL_MAX so
# a backed-off poll loop still finds the connection it opened last cycle.
SSH_CONTROL_PERSIST = os.environ.get("HERDR_SSH_CONTROL_PERSIST", "60")

RELAY_VERSION = "0.8.0"  # this relay's own version; shown to a client that must update
# The oldest client protocol revision this relay will work with. Deliberately not
# an app version: a client's protocol revision changes only when the wire changes,
# so a routine release never has to touch it, and a breaking change bumps it in
# exactly two places (here and the app's CLIENT_PROTOCOL).
#
# Raising this locks out every older build, so it is a release decision: deploy
# the relay that advertises the new revision first, then release the client that
# declares it. The relay does not enforce it — it advertises, and the client
# blocks itself. Rejecting the socket would park the app's reconnect loop, which
# looks like an outage rather than an instruction to update.
# Revision 3 (#45) removed `launch_session` and the `presets` key from the agents
# snapshot. A revision-2 client can still read every frame it gets, but its only
# way to start an agent is gone, so it is told to update rather than left with a
# composer whose button does nothing.
MIN_CLIENT = 3
AUTH_TOKEN = os.environ.get("HERDR_RELAY_TOKEN", "")  # Shared secret for relay auth
# Public-edge rate limiting (#18), applied per connection. Each tier is a token
# bucket: BURST commands available at once, refilling at PER_SECOND. The defaults
# are set so no human-driven session reaches them — tapping approvals or paging
# through panes stays well under — while a client that loops is bounded within a
# second. Set a BURST to 0 to disable that tier.
RATE_INPUT_BURST = int(os.environ.get("HERDR_RATE_INPUT_BURST", "10"))
RATE_INPUT_PER_SECOND = float(os.environ.get("HERDR_RATE_INPUT_PER_SECOND", "2"))
RATE_HOST_BURST = int(os.environ.get("HERDR_RATE_HOST_BURST", "30"))
RATE_HOST_PER_SECOND = float(os.environ.get("HERDR_RATE_HOST_PER_SECOND", "10"))
HOSTS_FILE = os.environ.get("HERDR_HOSTS_FILE", "")
PROJECTS_DB = os.environ.get(
    "HERDR_PROJECTS_DB",
    os.path.expanduser("~/.local/state/herdr-relay/projects.sqlite3"),
)
# Which hosts may be woken or shut down, and by what MAC, comes from the host
# configuration file alone (#45) — there is no environment fallback naming one
# host, because a power capability the relay cannot tie to a configured host is
# a capability nobody reviewed.
WAKE_BIN = os.environ.get("HERDR_WAKE_BIN", "wakeonlan")
# Native structured stores used by supported coding agents.
CLAUDE_PROJECTS = os.environ.get("HERDR_CLAUDE_PROJECTS", "~/.claude/projects")
OPENCODE_DB = os.environ.get("HERDR_OPENCODE_DB", "~/.local/share/opencode/opencode-stable.db")
TRANSCRIPT_MAX_BYTES = 262144  # tail window read per poll — bounds ssh transfer
TRANSCRIPT_BLOCK_LIMIT = 200   # most recent blocks kept per session

# VAPID Web Push
VAPID_PUBLIC_KEY = os.environ.get("HERDR_VAPID_PUBLIC", "")
VAPID_PRIVATE_KEY = os.environ.get("HERDR_VAPID_PRIVATE", "")
VAPID_SUBJECT = os.environ.get("HERDR_VAPID_SUBJECT", "mailto:herdr@localhost")
PUSH_SUBS_FILE = os.path.join(LOG_DIR, "push_subs.json")

# Remote hosts: comma-separated SSH targets
REMOTES = [r.strip() for r in os.environ.get("HERDR_REMOTES", "").split(",") if r.strip()]
