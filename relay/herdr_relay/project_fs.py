"""Descriptor-relative project-folder access for local and SSH hosts."""
import base64
import json
import os
import shlex
import stat
import subprocess

from . import hosts


MAX_COMPONENTS = 64
MAX_COMPONENT_LENGTH = 255


class FilesystemError(Exception):
    def __init__(self, code, message="Folder is unavailable"):
        super().__init__(message)
        self.code = code


def validate_components(value):
    if not isinstance(value, list) or len(value) > MAX_COMPONENTS:
        raise FilesystemError("INVALID_PATH", "Invalid folder path")
    components = []
    for component in value:
        if (
            not isinstance(component, str)
            or not component
            or len(component) > MAX_COMPONENT_LENGTH
            or component in (".", "..")
            or "/" in component
            or "\\" in component
            or "\x00" in component
        ):
            raise FilesystemError("INVALID_PATH", "Invalid folder path")
        components.append(component)
    return components


def _within(path, root):
    try:
        return os.path.commonpath((os.path.abspath(path), os.path.abspath(root))) == os.path.abspath(root)
    except ValueError:
        return False


def _fd_path(fd, root):
    try:
        path = os.readlink(f"/proc/self/fd/{fd}")
    except (FileNotFoundError, OSError):
        path = root
    if path.endswith(" (deleted)") or not _within(path, root):
        raise FilesystemError("PATH_NOT_ALLOWED", "Folder left the configured root")
    return os.path.abspath(path)


def _open_directory(root, components):
    components = validate_components(components)
    root = os.path.abspath(os.path.normpath(root))
    if not os.path.isabs(root) or os.path.realpath(root) != root:
        raise FilesystemError("ROOT_UNSAFE", "Configured project root is unsafe")
    try:
        root_stat = os.lstat(root)
    except OSError as error:
        raise FilesystemError("ROOT_UNAVAILABLE") from error
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise FilesystemError("ROOT_UNAVAILABLE")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(root, flags)
    except OSError as error:
        raise FilesystemError("ROOT_UNAVAILABLE") from error
    try:
        for component in components:
            try:
                child = os.open(component, flags, dir_fd=fd)
            except FileNotFoundError as error:
                raise FilesystemError("FOLDER_NOT_FOUND") from error
            except OSError as error:
                raise FilesystemError("PATH_NOT_ALLOWED") from error
            os.close(fd)
            fd = child
        canonical = _fd_path(fd, root)
        return fd, canonical
    except Exception:
        os.close(fd)
        raise


def browse_local(root, components):
    """List one directory level while keeping the directory open by descriptor."""
    fd, canonical = _open_directory(root, components)
    try:
        try:
            names = os.listdir(fd)
        except OSError as error:
            raise FilesystemError("FOLDER_UNAVAILABLE") from error
        entries = []
        for name in sorted(names, key=lambda value: (value.casefold(), value)):
            try:
                entry_stat = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(entry_stat.st_mode):
                continue
            if stat.S_ISDIR(entry_stat.st_mode):
                entries.append({"name": name, "kind": "directory"})
        return {"canonical_path": canonical, "entries": entries}
    finally:
        os.close(fd)


_REMOTE_SCRIPT = r'''
import json, os, stat, sys

def fail(code):
    print(json.dumps({"ok": False, "code": code}))
    raise SystemExit(0)

def components(value):
    if not isinstance(value, list) or len(value) > 64:
        fail("INVALID_PATH")
    for item in value:
        if (not isinstance(item, str) or not item or len(item) > 255 or
                item in (".", "..") or "/" in item or "\\" in item or "\x00" in item):
            fail("INVALID_PATH")
    return value

request = json.load(sys.stdin)
root = os.path.abspath(os.path.normpath(request.get("root", "")))
parts = components(request.get("components", []))
if not os.path.isabs(root) or os.path.realpath(root) != root:
    fail("ROOT_UNSAFE")
try:
    root_stat = os.lstat(root)
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        fail("ROOT_UNAVAILABLE")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(root, flags)
    for part in parts:
        child = os.open(part, flags, dir_fd=fd)
        os.close(fd)
        fd = child
    canonical = os.readlink("/proc/self/fd/" + str(fd))
    if canonical.endswith(" (deleted)") or os.path.commonpath((canonical, root)) != root:
        fail("PATH_NOT_ALLOWED")
    entries = []
    for name in sorted(os.listdir(fd), key=lambda value: (value.casefold(), value)):
        try:
            item = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(item.st_mode) and not stat.S_ISLNK(item.st_mode):
            entries.append({"name": name, "kind": "directory"})
    os.close(fd)
    print(json.dumps({"ok": True, "canonical_path": canonical, "entries": entries}))
except FileNotFoundError:
    fail("FOLDER_NOT_FOUND")
except OSError:
    fail("PATH_NOT_ALLOWED")
'''
_REMOTE_SCRIPT_B64 = base64.urlsafe_b64encode(_REMOTE_SCRIPT.encode()).decode()
_REMOTE_PYTHON = (
    "import base64; exec(base64.urlsafe_b64decode(%r).decode())"
    % _REMOTE_SCRIPT_B64
)
_REMOTE_COMMAND = f"python3 -c {shlex.quote(_REMOTE_PYTHON)}"
_SSH_OPTIONS = (
    "-o", "ConnectTimeout=5",
    "-o", "ServerAliveInterval=3",
    "-o", "ServerAliveCountMax=2",
    "-o", "BatchMode=yes",
)


def browse_remote(host, root, components):
    target = hosts.ssh_target(host)
    if not target:
        raise FilesystemError("REMOTE_UNAVAILABLE")
    try:
        result = subprocess.run(
            ["ssh", *_SSH_OPTIONS, target, _REMOTE_COMMAND],
            input=json.dumps({"root": root, "components": validate_components(components)}),
            capture_output=True,
            text=True,
            timeout=host.get("readiness_timeout_seconds", 15),
        )
    except OSError as error:
        raise FilesystemError("REMOTE_UNAVAILABLE") from error
    if result.returncode != 0:
        raise FilesystemError("REMOTE_UNAVAILABLE")
    try:
        response = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise FilesystemError("REMOTE_UNAVAILABLE") from error
    if not response.get("ok"):
        raise FilesystemError(response.get("code", "FOLDER_UNAVAILABLE"))
    if not isinstance(response.get("canonical_path"), str) or not isinstance(response.get("entries"), list):
        raise FilesystemError("REMOTE_UNAVAILABLE")
    return {
        "canonical_path": response["canonical_path"],
        "entries": [
            entry for entry in response["entries"]
            if isinstance(entry, dict)
            and isinstance(entry.get("name"), str)
            and entry.get("kind") == "directory"
        ],
    }


def browse(host, root, components):
    return browse_remote(host, root, components) if hosts.ssh_target(host) else browse_local(root, components)
