"""Auto-updater — checks GitHub Releases for new versions and prompts to update."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile

import aiohttp

from tribewatch import __version__

log = logging.getLogger(__name__)

GITHUB_REPO = "frankthetank001/tribewatch-client"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
ASSET_NAME = "TribeWatch-Setup.exe"


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse version string like 'v0.2.0' or '0.2.0' into comparable tuple."""
    v = v.lstrip("vV")
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts)


async def check_for_update() -> dict | None:
    """Check GitHub for a newer release.

    Returns a dict with release info if an update is available, or None.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                RELEASES_API,
                headers={"Accept": "application/vnd.github.v3+json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

        tag = data.get("tag_name", "")
        remote_ver = _parse_version(tag)
        local_ver = _parse_version(__version__)

        if not remote_ver or remote_ver <= local_ver:
            return None

        # Find the installer asset
        download_url = None
        for asset in data.get("assets", []):
            if asset["name"] == ASSET_NAME:
                download_url = asset["browser_download_url"]
                break

        if not download_url:
            # Fall back to the release page
            download_url = data.get("html_url", "")

        return {
            "version": tag,
            "current": __version__,
            "download_url": download_url,
            "release_url": data.get("html_url", ""),
            "body": data.get("body", ""),
            "is_installer": download_url.endswith(".exe"),
        }
    except Exception:
        log.debug("Update check failed", exc_info=True)
        return None


async def download_and_run_installer(download_url: str) -> bool:
    """Download the installer to a temp file and launch it."""
    try:
        tmp_dir = tempfile.mkdtemp(prefix="tribewatch_update_")
        installer_path = os.path.join(tmp_dir, ASSET_NAME)

        log.info("Downloading update from %s", download_url)
        async with aiohttp.ClientSession() as session:
            async with session.get(
                download_url,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as resp:
                resp.raise_for_status()
                with open(installer_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(8192):
                        f.write(chunk)

        log.info("Launching installer: %s", installer_path)
        subprocess.Popen(
            [installer_path, "/SILENT", "/CLOSEAPPLICATIONS"],
            creationflags=subprocess.DETACHED_PROCESS,
        )
        return True
    except Exception:
        log.warning("Failed to download/run installer", exc_info=True)
        return False


def is_frozen() -> bool:
    """Return True if running as a PyInstaller bundle."""
    return getattr(sys, "frozen", False)
