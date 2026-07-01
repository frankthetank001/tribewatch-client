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


def _epic_manifests_dir() -> Path:
    """Return the Epic Games Launcher manifests directory (may not exist)."""
    return (
        Path(os.environ.get("PROGRAMDATA", "C:/ProgramData"))
        / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    )


def _iter_epic_manifests() -> list[dict]:
    """Parse every Epic ``*.item`` manifest into a dict. Best-effort, never raises."""
    manifests_dir = _epic_manifests_dir()
    if not manifests_dir.exists():
        return []
    import json
    out: list[dict] = []
    for manifest in manifests_dir.glob("*.item"):
        try:
            out.append(json.loads(manifest.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _is_ark_manifest(data: dict) -> bool:
    """Return True if an Epic manifest dict describes ARK: Survival Ascended.

    Matches on install location / mandatory folder (both contain
    ``ARKSurvivalAscended``) or a "... Survival Ascended" display name.
    Deliberately does NOT match on ``AppName`` — Epic's app name for ARK SA
    is a random codename (e.g. "DroppedIcicle") that contains no "ark".
    """
    install_loc = str(data.get("InstallLocation") or "").lower().replace(" ", "")
    folder = str(data.get("MandatoryAppFolderName") or "").lower().replace(" ", "")
    display = str(data.get("DisplayName") or "").lower()
    return (
        "arksurvivalascended" in install_loc
        or "arksurvivalascended" in folder
        or ("ark" in display and "ascended" in display)
    )


def get_epic_launch_info() -> dict[str, str] | None:
    """Read the Epic manifest for ARK: Survival Ascended and return its real
    launch identifiers, or ``None`` if ARK isn't installed via Epic.

    Returns ``{"namespace", "catalog_item_id", "app_name", "install_location",
    "launch_executable"}`` — everything needed to build a launch URI or launch
    the exe directly. These come straight from the account's own installed
    manifest, so they always match what that Epic account actually owns
    (a hardcoded URI breaks for other editions/regions — Epic reissues catalog
    IDs, producing "Application Not Owned" on launch).
    """
    for data in _iter_epic_manifests():
        if not _is_ark_manifest(data):
            continue
        namespace = data.get("CatalogNamespace") or data.get("MainGameCatalogNamespace") or ""
        catalog_item_id = data.get("CatalogItemId") or data.get("MainGameCatalogItemId") or ""
        app_name = data.get("AppName") or data.get("MainGameAppName") or ""
        if namespace and catalog_item_id and app_name:
            return {
                "namespace": namespace,
                "catalog_item_id": catalog_item_id,
                "app_name": app_name,
                "install_location": str(data.get("InstallLocation") or ""),
                "launch_executable": str(data.get("LaunchExecutable") or ""),
            }
    return None


def _find_game_user_settings() -> tuple[Path | None, str]:
    """Find the GameUserSettings.ini file across Steam and Epic install paths.

    A player can own ARK on both Steam and Epic and alternate between them;
    each build keeps its own GameUserSettings.ini. Collect every candidate
    that exists and return the most recently MODIFIED one, so server-id /
    resolution detection follows whichever build they last played.

    Returns (path, launcher) where launcher is "steam", "epic", or "" if not found.
    """
    candidates: list[tuple[Path, str]] = []

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
            candidates.append((ini, _LAUNCHER_STEAM))

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
            candidates.append((ini, _LAUNCHER_EPIC))

    if not candidates:
        return None, ""

    def _mtime(item: tuple[Path, str]) -> float:
        try:
            return item[0].stat().st_mtime
        except OSError:
            return 0.0

    best = max(candidates, key=_mtime)
    if len(candidates) > 1:
        log.debug(
            "Multiple GameUserSettings.ini found (%d); using newest: %s (launcher=%s)",
            len(candidates), best[0], best[1],
        )
    return best


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
