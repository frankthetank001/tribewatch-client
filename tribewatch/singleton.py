"""Single-instance enforcement via PID lockfile + process scan.

On startup, scans the running process list for any other TribeWatch
instances and kills them before acquiring the lock. The process scan
catches leftover zombies that the lockfile alone misses (e.g. when a
previous run crashed/was force-killed and never ran its atexit).
"""

from __future__ import annotations

import atexit
import logging
import os
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

def _own_exe_name() -> str | None:
    """Return the lowercased basename of the current executable.

    Used by the process scan to filter — a TribeWatch.exe install
    only kills other TribeWatch.exe processes, and a TribeWatch-Dev.exe
    install only kills other TribeWatch-Dev.exe processes. This lets
    dev + stable installs coexist on the same machine without the
    singleton enforcement killing each other.

    Returns None when running from source (`python -m tribewatch`),
    in which case the scan falls back to matching ANY frozen exe AND
    other python.exe processes with `tribewatch` in argv.
    """
    if not getattr(sys, "frozen", False):
        return None
    try:
        return Path(sys.executable).name.lower()
    except Exception:
        return None


def _install_namespace() -> str:
    """Return a per-install lockfile namespace.

    Frozen exe → derived from the install dir name (e.g. "TribeWatch"
    or "TribeWatch-Dev"), so dev and stable installs have separate
    PID lockfiles. Source runs share the default "TribeWatch" namespace.
    """
    if not getattr(sys, "frozen", False):
        return "TribeWatch"
    try:
        # PyInstaller's sys.executable lives at <install>/TribeWatch.exe
        return Path(sys.executable).parent.name
    except Exception:
        return "TribeWatch"


_LOCK_FILE = Path(os.environ.get("APPDATA", Path.home())) / _install_namespace() / "tribewatch.pid"

# Process names that indicate another TribeWatch instance. Frozen
# instances scope to their own name only via _own_exe_name(); source
# runs scan for both frozen variants AND python with -m tribewatch.
_FROZEN_EXE_NAMES = {"tribewatch.exe", "tribewatch-dev.exe"}
# When running from source, we look for python.exe with "tribewatch" in argv
_PYTHON_EXE_NAMES = {"python.exe", "pythonw.exe", "python3.exe"}


def _find_other_tribewatch_pids() -> list[int]:
    """Enumerate every running TribeWatch instance other than ourselves.

    Frozen install → only matches processes with the SAME exe name as
    ourselves (TribeWatch.exe or TribeWatch-Dev.exe), so dev and stable
    installs can coexist. Source run → matches both frozen variants
    AND python with -m tribewatch in argv (so launching the dev source
    will tear down any stale frozen instance for testing).
    """
    try:
        import psutil
    except ImportError:
        return []

    self_pid = os.getpid()
    own_name = _own_exe_name()  # None when running from source
    matches: list[int] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            pid = proc.info["pid"]
            if pid == self_pid:
                continue
            name = (proc.info.get("name") or "").lower()
            if own_name is not None:
                # Frozen — only kill siblings of the same install
                if name == own_name:
                    matches.append(pid)
                continue
            # Source run — broader sweep
            if name in _FROZEN_EXE_NAMES:
                matches.append(pid)
                continue
            if name in _PYTHON_EXE_NAMES:
                cmdline = proc.info.get("cmdline") or []
                joined = " ".join(cmdline).lower()
                # Match `python -m tribewatch` and similar
                if "tribewatch" in joined and (
                    "-m tribewatch" in joined
                    or "tribewatch\\__main__" in joined
                    or "tribewatch/__main__" in joined
                ):
                    matches.append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return matches


def _kill_pid_and_wait(pid: int, timeout: float = 5.0) -> bool:
    """Terminate a process and wait for it to actually exit.

    Falls back from terminate() to kill() if the process doesn't exit
    within *timeout* seconds. Returns True if the process is gone after
    we're done, False if it's still alive.
    """
    try:
        import psutil
    except ImportError:
        # No psutil — best-effort terminate without verification
        try:
            import signal as _sig
            os.kill(pid, _sig.SIGTERM)
            time.sleep(1)
            return True
        except OSError:
            return False

    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True

    try:
        proc.terminate()
        try:
            proc.wait(timeout=timeout / 2)
            log.info("Terminated existing TribeWatch process (PID %d)", pid)
            return True
        except psutil.TimeoutExpired:
            log.warning(
                "PID %d did not exit on terminate(), escalating to kill()", pid,
            )
            proc.kill()
            try:
                proc.wait(timeout=timeout / 2)
                log.info("Force-killed existing TribeWatch process (PID %d)", pid)
                return True
            except psutil.TimeoutExpired:
                log.error("PID %d survived kill() — giving up", pid)
                return False
    except psutil.NoSuchProcess:
        return True
    except psutil.AccessDenied:
        log.error("Access denied terminating PID %d", pid)
        return False


def _remove_lock() -> None:
    try:
        _LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def ensure_single_instance() -> None:
    """Kill every other TribeWatch instance and acquire the PID lock.

    Two-tier check:
      1. Scan the running process list for any other TribeWatch
         instance (frozen exe or python -m tribewatch). Kills each
         and verifies it actually exited.
      2. Then write our own PID to the lockfile as a secondary
         signal — useful for diagnostic tooling and the soft-restart
         path which preserves PID and re-acquires the same file.
    """
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Tier 1 — process scan
    others = _find_other_tribewatch_pids()
    if others:
        log.warning(
            "Found %d other TribeWatch process(es) running: %s — terminating",
            len(others), others,
        )
        for pid in others:
            _kill_pid_and_wait(pid, timeout=5.0)
        # Re-scan to confirm we're alone now
        leftover = _find_other_tribewatch_pids()
        if leftover:
            log.error(
                "Could not terminate all leftover TribeWatch processes: %s",
                leftover,
            )

    # Tier 2 — also honour the lockfile (catches edge cases like a
    # process whose name doesn't match the pattern, e.g. installed
    # under a custom name).
    if _LOCK_FILE.exists():
        try:
            old_pid = int(_LOCK_FILE.read_text().strip())
        except (ValueError, OSError):
            old_pid = None
        if old_pid and old_pid != os.getpid() and old_pid not in others:
            try:
                import psutil
                if psutil.pid_exists(old_pid):
                    log.info("Lockfile PID %d still alive, terminating", old_pid)
                    _kill_pid_and_wait(old_pid, timeout=5.0)
            except ImportError:
                pass

    # Write our PID
    _LOCK_FILE.write_text(str(os.getpid()))
    atexit.register(_remove_lock)
