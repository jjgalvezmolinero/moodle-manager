"""Host path <-> internal path translation.

The manager runs in a Linux container and talks to the host Docker daemon.
Paths passed to docker compose (bind mount sources) must be paths the daemon
understands, and Python must be able to open the same paths inside this
container. Both conditions hold when the host directory is mounted inside the
manager container at the exact path the daemon uses.

  - Linux/macOS: host paths are used as-is (/home is mounted at /home).
  - Windows (Docker Desktop, WSL2): the daemon sees drive C: as
    /run/desktop/mnt/host/c, so C:\\Users is mounted there and every
    Windows path entered by the user is translated before being used.

Instances store paths as the user typed them (host form); translation to the
internal form happens at the point of use.
"""
import os
import re

HOST_OS = os.environ.get("HOST_OS", "linux").lower()
IS_WINDOWS = HOST_OS == "windows"

# Where Docker Desktop exposes Windows drives to Linux containers.
WINDOWS_MOUNT_PREFIX = os.environ.get("WINDOWS_MOUNT_PREFIX", "/run/desktop/mnt/host").rstrip("/")

# Directory the folder browser opens by default, in host form.
HOST_BROWSE_ROOT = os.environ.get("HOST_BROWSE_ROOT") or ("C:\\Users" if IS_WINDOWS else "/home")

_WIN_PATH_RE = re.compile(r"^([A-Za-z]):(?:[\\/](.*))?$")


def to_internal(path: str | None) -> str | None:
    """Translate a host path to the path usable inside the container and by the daemon."""
    if not path or not IS_WINDOWS:
        return path
    path = path.strip()
    m = _WIN_PATH_RE.match(path)
    if not m:
        return path  # already internal (or relative)
    drive, rest = m.group(1).lower(), (m.group(2) or "")
    rest = rest.replace("\\", "/").strip("/")
    return f"{WINDOWS_MOUNT_PREFIX}/{drive}" + (f"/{rest}" if rest else "")


def to_host(path: str | None) -> str | None:
    """Translate an internal path back to the form shown to the user."""
    if not path or not IS_WINDOWS:
        return path
    prefix = WINDOWS_MOUNT_PREFIX + "/"
    if not path.startswith(prefix):
        return path
    drive, _, rest = path[len(prefix):].partition("/")
    if len(drive) != 1:
        return path
    return f"{drive.upper()}:\\" + rest.replace("/", "\\")


def is_drive_root(internal_path: str) -> bool:
    """True if the internal path is a drive root (C:\\), above which there is nothing to browse."""
    if not IS_WINDOWS:
        return False
    rel = internal_path[len(WINDOWS_MOUNT_PREFIX):].strip("/")
    return internal_path.startswith(WINDOWS_MOUNT_PREFIX + "/") and "/" not in rel


def join_host(parent: str, name: str) -> str:
    """Join a host-form parent path and a child name with the host separator."""
    sep = "\\" if IS_WINDOWS else "/"
    return parent.rstrip("\\/") + sep + name
