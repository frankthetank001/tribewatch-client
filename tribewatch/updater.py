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
DEV_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/dev-latest"
ASSET_NAME_STABLE = "TribeWatch-Setup.exe"
ASSET_NAME_DEV = "TribeWatch-Dev-Setup.exe"


def _is_dev_version() -> bool:
    """Return True if running a dev build (version starts with 'dev-')."""
    return __version__.startswith("dev-")


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


async def _check_dev_update() -> dict | None:
    """Check the dev-latest pre-release for a newer dev build."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                DEV_RELEASE_API,
                headers={"Accept": "application/vnd.github.v3+json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

        # Extract the dev-XXXXXXX version from the release body/name
        body = data.get("body", "")
        remote_version = ""
        for line in body.splitlines():
            if line.startswith("**Version:**"):
                # Parse: **Version:** `dev-abc1234`
                remote_version = line.split("`")[1] if "`" in line else ""
                break

        if not remote_version or remote_version == __version__:
            return None

        # Find the installer asset
        download_url = None
        for asset in data.get("assets", []):
            if asset["name"] == ASSET_NAME_DEV:
                download_url = asset["browser_download_url"]
                break

        if not download_url:
            download_url = data.get("html_url", "")

        return {
            "version": remote_version,
            "current": __version__,
            "download_url": download_url,
            "release_url": data.get("html_url", ""),
            "body": data.get("body", ""),
            "is_installer": download_url.endswith(".exe"),
        }
    except Exception:
        log.debug("Dev update check failed", exc_info=True)
        return None


async def _check_stable_update() -> dict | None:
    """Check the latest stable release for a newer version."""
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
            if asset["name"] == ASSET_NAME_STABLE:
                download_url = asset["browser_download_url"]
                break

        if not download_url:
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


async def check_for_update() -> dict | None:
    """Check GitHub for a newer release.

    Dev builds check the dev-latest pre-release.
    Stable builds check the latest stable release.

    Returns a dict with release info if an update is available, or None.
    """
    if _is_dev_version():
        return await _check_dev_update()
    return await _check_stable_update()


async def download_and_run_installer(download_url: str) -> bool:
    """Download the installer and hand off via a wait-shim, then exit.

    Spawns a small batch shim that:
      1. Polls until our PID disappears (we exit immediately after spawn)
      2. Runs the installer with /SILENT /RESTARTAPPLICATIONS
      3. Deletes itself

    Without the shim, Inno Setup pops up a "Setup was unable to close
    all applications" dialog because the running TribeWatch process
    holds open the very files the installer wants to overwrite. The
    /CLOSEAPPLICATIONS flag uses the Windows Restart Manager, which
    only works for apps registered with RM — TribeWatch isn't.
    """
    try:
        tmp_dir = tempfile.mkdtemp(prefix="tribewatch_update_")
        asset_name = ASSET_NAME_DEV if _is_dev_version() else ASSET_NAME_STABLE
        installer_path = os.path.join(tmp_dir, asset_name)

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

        # Build the wait-then-run-installer shim
        pid = os.getpid()
        shim_path = os.path.join(tmp_dir, "run_update.bat")
        # Inno Setup forensic log lives next to the installer in the temp
        # dir. If the install ever fails (e.g. file lock issues), this is
        # the file to inspect — it lists every file Setup tried to write,
        # every Restart Manager call, and the reason for any failure.
        log_path = os.path.join(tmp_dir, "install.log")
        # Note: %~f0 = full path to this batch file (for self-delete)
        # We intentionally do NOT delete the temp dir — the install log
        # stays around for diagnosis if anything goes wrong.
        shim_body = (
            "@echo off\r\n"
            ":wait\r\n"
            f'tasklist /fi "PID eq {pid}" 2>nul | find "{pid}" >nul\r\n'
            "if not errorlevel 1 (\r\n"
            "    timeout /t 1 /nobreak >nul\r\n"
            "    goto wait\r\n"
            ")\r\n"
            f'start "" "{installer_path}" /SILENT /RESTARTAPPLICATIONS /LOG="{log_path}"\r\n'
            'del "%~f0"\r\n'
        )
        with open(shim_path, "w") as f:
            f.write(shim_body)

        log.info("Launching update shim (PID %d → installer %s)", pid, installer_path)
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        subprocess.Popen(
            ["cmd", "/c", shim_path],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
            close_fds=True,
        )

        # Give the shim a tick to start polling, then exit so it can
        # observe our PID disappear and proceed to run the installer.
        log.warning("Update queued — exiting current process so installer can replace files")
        # Use os._exit so we don't run any cleanup that might re-grab files
        import time
        time.sleep(0.3)
        os._exit(0)
    except Exception:
        log.warning("Failed to download/run installer", exc_info=True)
        return False


def is_frozen() -> bool:
    """Return True if running as a PyInstaller bundle."""
    return getattr(sys, "frozen", False)
