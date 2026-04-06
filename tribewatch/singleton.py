"""Single-instance enforcement via a PID lockfile.

On startup, checks for an existing TribeWatch process and kills it
before acquiring the lock. This prevents two clients from fighting
over the same screen captures.
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_LOCK_FILE = Path(os.environ.get("APPDATA", Path.home())) / "TribeWatch" / "tribewatch.pid"


def _pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _kill_pid(pid: int) -> bool:
    """Attempt to kill a process. Returns True if it was killed."""
    try:
        if sys.platform == "win32":
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
        log.info("Killed existing TribeWatch process (PID %d)", pid)
        return True
    except OSError:
        return False


def _remove_lock() -> None:
    try:
        _LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def ensure_single_instance() -> None:
    """Kill any existing TribeWatch instance and acquire the PID lock."""
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

    if _LOCK_FILE.exists():
        try:
            old_pid = int(_LOCK_FILE.read_text().strip())
        except (ValueError, OSError):
            old_pid = None

        if old_pid and old_pid != os.getpid() and _pid_alive(old_pid):
            log.info("Found existing TribeWatch instance (PID %d), killing it", old_pid)
            _kill_pid(old_pid)
            # Give it a moment to die
            import time
            time.sleep(1)

    # Write our PID
    _LOCK_FILE.write_text(str(os.getpid()))
    atexit.register(_remove_lock)
