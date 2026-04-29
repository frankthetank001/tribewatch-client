"""Shared server ID / server name / resolution extraction from GameUserSettings.ini.

Supports both Steam and Epic Games Store installations.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

# --- Launcher detection ---

_LAUNCHER_STEAM = "steam"
_LAUNCHER_EPIC = "epic"


def get_steam_library_paths() -> list[Path]:
    """Return Steam library folder paths from libraryfolders.vdf + default."""
    paths: list[Path] = []

    # Default Steam install path
    default = Path("C:/Program Files (x86)/Steam")
    if default.exists():
        paths.append(default)

    vdf = default / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        try:
            text = vdf.read_text(encoding="utf-8")
            for m in re.finditer(r'"path"\s+"([^"]+)"', text):
                p = Path(m.group(1).replace("\\\\", "\\"))
                if p.exists() and p not in paths:
                    paths.append(p)
        except Exception:
            log.debug("Failed to parse libraryfolders.vdf", exc_info=True)

    return paths


def _get_epic_install_paths() -> list[Path]:
    """Return possible Epic Games ARK install paths."""
    paths: list[Path] = []

    # Default Epic Games install location
    default = Path("C:/Program Files/Epic Games/ARKSurvivalAscended")
    if default.exists():
        paths.append(default)

    # Check other drives for Epic Games folder
    for drive in "DEFGH":
        p = Path(f"{drive}:/Epic Games/ARKSurvivalAscended")
        if p.exists() and p not in paths:
            paths.append(p)
        p = Path(f"{drive}:/Program Files/Epic Games/ARKSurvivalAscended")
        if p.exists() and p not in paths:
            paths.append(p)

    # Check Epic's manifest files for custom install paths
    manifests_dir = Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    if manifests_dir.exists():
        try:
            import json
            for manifest in manifests_dir.glob("*.item"):
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                    install_loc = data.get("InstallLocation", "")
                    app_name = data.get("AppName", "")
                    # ARK SA catalog item / app name
                    if install_loc and ("ark" in app_name.lower() or "ark" in install_loc.lower()):
                        p = Path(install_loc)
                        if p.exists() and p not in paths:
                            paths.append(p)
                except Exception:
                    continue
        except Exception:
            log.debug("Failed to read Epic manifests", exc_info=True)

    return paths


def _find_game_user_settings() -> tuple[Path | None, str]:
    """Find the GameUserSettings.ini file across Steam and Epic install paths.

    Returns (path, launcher) where launcher is "steam", "epic", or "" if not found.
    """
    # Check Steam first
    for lib_path in get_steam_library_paths():
        ini = (
            lib_path
            / "steamapps"
            / "common"
            / "ARK Survival Ascended"
            / "ShooterGame"
            / "Saved"
            / "Config"
            / "Windows"
            / "GameUserSettings.ini"
        )
        if ini.exists():
            return ini, _LAUNCHER_STEAM

    # Check Epic
    for epic_path in _get_epic_install_paths():
        ini = (
            epic_path
            / "ShooterGame"
            / "Saved"
            / "Config"
            / "Windows"
            / "GameUserSettings.ini"
        )
        if ini.exists():
            return ini, _LAUNCHER_EPIC

    return None, ""


def detect_launcher() -> str:
    """Detect which launcher ARK is installed through.

    Returns "steam", "epic", or "" if not detected.
    """
    _, launcher = _find_game_user_settings()
    return launcher


def _read_game_user_settings() -> str | None:
    """Read and return the contents of GameUserSettings.ini, or None."""
    ini, _ = _find_game_user_settings()
    if ini is None:
        return None
    try:
        return ini.read_text(encoding="utf-8", errors="replace")
    except Exception:
        log.debug("Failed to read %s", ini, exc_info=True)
        return None


def get_server_info() -> dict[str, str]:
    """Extract server ID and server name from GameUserSettings.ini.

    Returns ``{"server_id": "9664", "server_name": "OC-PVP-SmallTribes-LostColony9664"}``
    or ``{"server_id": "", "server_name": ""}`` if not found.
    """
    ini_path, launcher = _find_game_user_settings()
    if ini_path is None:
        log.warning("Server ID: GameUserSettings.ini not found (checked Steam + Epic paths)")
        return {"server_id": "", "server_name": ""}

    log.debug("Server ID: found config at %s (launcher=%s)", ini_path, launcher)

    try:
        text = ini_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        log.warning("Server ID: failed to read %s", ini_path, exc_info=True)
        return {"server_id": "", "server_name": ""}

    for line in text.splitlines():
        if not line.strip().startswith("LastJoinedSessionPerCategory"):
            continue
        _, _, rhs = line.partition("=")
        rhs = rhs.strip().strip('"')
        if not rhs or rhs.strip() == "":
            log.info("Server ID: LastJoinedSessionPerCategory found but value is empty")
            continue
        # Strip version suffix like " - (v83.25)"
        cleaned = re.sub(r"\s*-\s*\(v[\d.]+\)\s*$", "", rhs)
        m = re.search(r"(\d{3,})$", cleaned)
        if m:
            log.debug("Server ID: detected %s from '%s'", m.group(1), cleaned)
            return {
                "server_id": m.group(1),
                "server_name": cleaned,
            }
        log.warning("Server ID: LastJoinedSessionPerCategory='%s' but no numeric ID found", rhs)
        return {"server_id": "", "server_name": ""}

    log.warning("Server ID: LastJoinedSessionPerCategory not found in %s", ini_path)
    return {"server_id": "", "server_name": ""}


def get_fullscreen_mode() -> int | None:
    """Read FullscreenMode from GameUserSettings.ini.

    Unreal Engine values:
        0 = Fullscreen (exclusive — breaks PrintWindow capture and
            blocks our overlay)
        1 = Fullscreen Windowed (borderless — recommended)
        2 = Windowed

    Returns the int or ``None`` if the file can't be read or the key
    is missing.
    """
    text = _read_game_user_settings()
    if text is None:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("FullscreenMode="):
            try:
                return int(stripped.split("=", 1)[1].strip())
            except (ValueError, IndexError):
                return None
    return None


def get_game_resolution() -> tuple[int, int] | None:
    """Read ResolutionSizeX/ResolutionSizeY from GameUserSettings.ini.

    Returns ``(width, height)`` or ``None`` if not found.
    """
    text = _read_game_user_settings()
    if text is None:
        return None

    width: int | None = None
    height: int | None = None

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("ResolutionSizeX="):
            try:
                width = int(stripped.split("=", 1)[1].strip())
            except (ValueError, IndexError):
                pass
        elif stripped.startswith("ResolutionSizeY="):
            try:
                height = int(stripped.split("=", 1)[1].strip())
            except (ValueError, IndexError):
                pass
        if width is not None and height is not None:
            return (width, height)

    return None
