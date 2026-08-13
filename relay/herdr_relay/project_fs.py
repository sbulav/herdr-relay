"""Descriptor-relative project-folder access for local and SSH hosts."""
import base64
import errno
import fcntl
import json
import os
import shlex
import stat
import subprocess

from . import herdr, hosts


MAX_COMPONENTS = 64
MAX_COMPONENT_LENGTH = 255
FORBIDDEN_NAME_CHARACTERS = '<>:"|?*'
RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


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


def validate_name(value):
    """Validate exactly one portable child-folder name."""
    if not isinstance(value, str) or not value or value in (".", ".."):
        raise FilesystemError("INVALID_PATH", "Invalid folder name")
    if "/" in value or "\\" in value or "\x00" in value:
        raise FilesystemError("INVALID_PATH", "Folder name must be one component")
    if len(value.encode("utf-8")) > MAX_COMPONENT_LENGTH:
        raise FilesystemError("INVALID_NAME", "Folder name is longer than 255 UTF-8 bytes")
    if value != value.strip():
        raise FilesystemError("INVALID_NAME", "Folder name cannot start or end with whitespace")
    if value.endswith("."):
        raise FilesystemError("INVALID_NAME", "Folder name cannot end with a dot")
    if any(character in FORBIDDEN_NAME_CHARACTERS or ord(character) < 32 or ord(character) == 127 for character in value):
        raise FilesystemError("INVALID_NAME", "Folder name contains a forbidden character")
    if value.split(".", 1)[0].upper() in RESERVED_NAMES:
        raise FilesystemError("INVALID_NAME", "Folder name is reserved on some platforms")
    return value


def _within(path, root):
    try:
        return os.path.commonpath((os.path.abspath(path), os.path.abspath(root))) == os.path.abspath(root)
    except ValueError:
        return False


def _fd_path(fd, root):
    try:
        path = os.readlink(f"/proc/self/fd/{fd}")
    except (FileNotFoundError, OSError):
        try:
            # macOS has no /proc, but F_GETPATH gives the opened descriptor's
            # real path. The numeric request is stable across supported macOS
            # versions and ignored on platforms where /proc worked above.
            path = fcntl.fcntl(fd, 50, b"\0" * 1024).split(b"\0", 1)[0].decode()
        except (OSError, ValueError, UnicodeDecodeError) as error:
            raise FilesystemError("PATH_NOT_ALLOWED", "Folder containment could not be verified") from error
    if path.endswith(" (deleted)") or not _within(path, root):
        raise FilesystemError("PATH_NOT_ALLOWED", "Folder left the configured root")
    return os.path.abspath(path)


def _open_directory(root, components):
    components = validate_components(components)
    configured_root = os.path.abspath(os.path.normpath(root))
    if not os.path.isabs(configured_root):
        raise FilesystemError("ROOT_UNSAFE", "Configured project root is unsafe")
    try:
        root_stat = os.lstat(configured_root)
    except OSError as error:
        raise FilesystemError("ROOT_UNAVAILABLE") from error
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise FilesystemError("ROOT_UNAVAILABLE")
    # Canonicalise ancestors such as macOS /var -> /private/var, while still
    # rejecting a configured root whose final component is itself a symlink.
    root = os.path.realpath(configured_root)
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


def create_local(root, components, name):
    """Atomically create one empty directory below an opened parent."""
    name = validate_name(name)
    parent_fd, _canonical_parent = _open_directory(root, components)
    canonical_root = os.path.realpath(os.path.abspath(os.path.normpath(root)))
    created = False
    try:
        try:
            os.mkdir(name, mode=0o755, dir_fd=parent_fd)
            created = True
        except FileExistsError as error:
            raise FilesystemError("FOLDER_EXISTS", "A folder with that name already exists") from error
        except OSError as error:
            raise FilesystemError("FOLDER_UNAVAILABLE", "Folder could not be created") from error

        # Re-read the descriptor path after mkdir. If the configured parent was
        # renamed outside the root during the call, remove through the same
        # descriptor rather than registering the escaped directory.
        _fd_path(parent_fd, canonical_root)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            child_fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError as error:
            raise FilesystemError("PATH_NOT_ALLOWED", "Created folder could not be verified") from error
        try:
            canonical = _fd_path(child_fd, canonical_root)
            if os.listdir(child_fd):
                raise FilesystemError("FOLDER_UNAVAILABLE", "New folder is not empty")
        finally:
            os.close(child_fd)
        return {"canonical_path": canonical}
    except Exception:
        if created:
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass
        raise
    finally:
        os.close(parent_fd)


def remove_empty_local(root, components, name):
    """Best-effort rollback through the allowlisted parent descriptor."""
    name = validate_name(name)
    parent_fd, _canonical_parent = _open_directory(root, components)
    try:
        try:
            os.rmdir(name, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        except OSError as error:
            if error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                raise FilesystemError("FOLDER_UNAVAILABLE", "Created folder could not be rolled back") from error
    finally:
        os.close(parent_fd)


_REMOTE_SCRIPT = r'''
import fcntl, json, os, stat, sys

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

def name(value):
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update("COM" + str(i) for i in range(1, 10))
    reserved.update("LPT" + str(i) for i in range(1, 10))
    if not isinstance(value, str) or not value or value in (".", ".."):
        fail("INVALID_PATH")
    if "/" in value or "\\" in value or "\x00" in value:
        fail("INVALID_PATH")
    if len(value.encode("utf-8")) > 255 or value != value.strip() or value.endswith("."):
        fail("INVALID_NAME")
    if any(c in '<>:"|?*' or ord(c) < 32 or ord(c) == 127 for c in value):
        fail("INVALID_NAME")
    if value.split(".", 1)[0].upper() in reserved:
        fail("INVALID_NAME")
    return value

def fd_path(fd, root):
    try:
        path = os.readlink("/proc/self/fd/" + str(fd))
    except OSError:
        path = fcntl.fcntl(fd, 50, b"\0" * 1024).split(b"\0", 1)[0].decode()
    if path.endswith(" (deleted)") or os.path.commonpath((path, root)) != root:
        fail("PATH_NOT_ALLOWED")
    return path

request = json.load(sys.stdin)
configured_root = os.path.abspath(os.path.normpath(request.get("root", "")))
parts = components(request.get("components", []))
operation = request.get("op", "browse")
if not os.path.isabs(configured_root):
    fail("ROOT_UNSAFE")
try:
    root_stat = os.lstat(configured_root)
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        fail("ROOT_UNAVAILABLE")
    root = os.path.realpath(configured_root)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(root, flags)
    for part in parts:
        child = os.open(part, flags, dir_fd=fd)
        os.close(fd)
        fd = child
    canonical = fd_path(fd, root)
    if operation == "browse":
        entries = []
        for child_name in sorted(os.listdir(fd), key=lambda value: (value.casefold(), value)):
            try:
                item = os.stat(child_name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(item.st_mode) and not stat.S_ISLNK(item.st_mode):
                entries.append({"name": child_name, "kind": "directory"})
        os.close(fd)
        print(json.dumps({"ok": True, "canonical_path": canonical, "entries": entries}))
    elif operation == "create":
        child_name = name(request.get("name"))
        created = False
        try:
            os.mkdir(child_name, 0o755, dir_fd=fd)
            created = True
            fd_path(fd, root)
            child_fd = os.open(child_name, flags, dir_fd=fd)
            try:
                child_path = fd_path(child_fd, root)
                if os.listdir(child_fd):
                    fail("FOLDER_UNAVAILABLE")
            finally:
                os.close(child_fd)
            os.close(fd)
            print(json.dumps({"ok": True, "canonical_path": child_path}))
        except FileExistsError:
            fail("FOLDER_EXISTS")
        except BaseException:
            if created:
                try:
                    os.rmdir(child_name, dir_fd=fd)
                except OSError:
                    pass
            raise
    elif operation == "remove_empty":
        child_name = name(request.get("name"))
        try:
            os.rmdir(child_name, dir_fd=fd)
        except FileNotFoundError:
            pass
        except OSError as error:
            if error.errno not in (39, 66):
                raise
        os.close(fd)
        print(json.dumps({"ok": True}))
    else:
        fail("INVALID_PATH")
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


def _remote(host, root, components, operation, name=None):
    target = hosts.ssh_target(host)
    if not target:
        raise FilesystemError("REMOTE_UNAVAILABLE")
    request = {"op": operation, "root": root, "components": validate_components(components)}
    if name is not None:
        request["name"] = validate_name(name)
    try:
        result = subprocess.run(
            # Browsing walks a tree one request at a time, so these reuse the
            # master the poll loop already keeps open to the same host (#19).
            ["ssh", *herdr.ssh_options(), target, _REMOTE_COMMAND],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=host.get("readiness_timeout_seconds", 15),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FilesystemError("REMOTE_UNAVAILABLE") from error
    if result.returncode != 0:
        raise FilesystemError("REMOTE_UNAVAILABLE")
    try:
        response = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise FilesystemError("REMOTE_UNAVAILABLE") from error
    if not response.get("ok"):
        code = response.get("code", "FOLDER_UNAVAILABLE")
        messages = {
            "FOLDER_EXISTS": "A folder with that name already exists",
            "INVALID_NAME": "Folder name is invalid on this platform",
        }
        raise FilesystemError(code, messages.get(code, "Folder is unavailable"))
    return response


def browse_remote(host, root, components):
    response = _remote(host, root, components, "browse")
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


def create_remote(host, root, components, name):
    response = _remote(host, root, components, "create", name)
    if not isinstance(response.get("canonical_path"), str):
        raise FilesystemError("REMOTE_UNAVAILABLE")
    return {"canonical_path": response["canonical_path"]}


def remove_empty_remote(host, root, components, name):
    _remote(host, root, components, "remove_empty", name)


def browse(host, root, components):
    return browse_remote(host, root, components) if hosts.ssh_target(host) else browse_local(root, components)


def create(host, root, components, name):
    return create_remote(host, root, components, name) if hosts.ssh_target(host) else create_local(root, components, name)


def remove_empty(host, root, components, name):
    if hosts.ssh_target(host):
        remove_empty_remote(host, root, components, name)
    else:
        remove_empty_local(root, components, name)
