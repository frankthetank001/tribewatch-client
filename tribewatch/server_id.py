"""Shared server ID / server name / resolution extraction from GameUserSettings.ini."""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)


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


def _find_game_user_settings() -> Path | None:
    """Find the GameUserSettings.ini file across Steam library paths."""
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
            return ini
    return None


def _read_game_user_settings() -> str | None:
    """Read and return the contents of GameUserSettings.ini, or None."""
    ini = _find_game_user_settings()
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
    text = _read_game_user_settings()
    if text is None:
        return {"server_id": "", "server_name": ""}

    for line in text.splitlines():
        if not line.strip().startswith("LastJoinedSessionPerCategory"):
            continue
        _, _, rhs = line.partition("=")
        rhs = rhs.strip().strip('"')
        if not rhs or rhs.strip() == "":
            continue
        # Strip version suffix like " - (v83.25)"
        cleaned = re.sub(r"\s*-\s*\(v[\d.]+\)\s*$", "", rhs)
        m = re.search(r"(\d{3,})$", cleaned)
        if m:
            return {
                "server_id": m.group(1),
                "server_name": cleaned,
            }

    log.debug("LastJoinedSessionPerCategory not found in GameUserSettings.ini")
    return {"server_id": "", "server_name": ""}


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
