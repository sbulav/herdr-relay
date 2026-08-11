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


# The LEGACY (#14) static routes serve web/ from the repo root. Resolve it once,
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

RELAY_VERSION = "0.7.0"  # this relay's own version; shown to a client that must update
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
MIN_CLIENT = 2
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
PRESETS_FILE = os.environ.get("HERDR_PRESETS_FILE", "")
HOSTS_FILE = os.environ.get("HERDR_HOSTS_FILE", "")
PROJECTS_DB = os.environ.get(
    "HERDR_PROJECTS_DB",
    os.path.expanduser("~/.local/state/herdr-relay/projects.sqlite3"),
)
POWER_HOST_ID = os.environ.get("HERDR_POWER_HOST_ID", "")
POWER_HOST_MAC = os.environ.get("HERDR_POWER_HOST_MAC", "")
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
