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
                    # 403 typically = anonymous rate limit (60/h per IP).
                    # Surface this loudly so a rate-limited box doesn't
                    # silently report "up to date" for hours/days.
                    log.warning(
                        "Dev update check: GitHub API returned status %d "
                        "for %s — treating as 'no update available' "
                        "(check would resume on next launch)",
                        resp.status, DEV_RELEASE_API,
                    )
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

        if not remote_version:
            log.warning(
                "Dev update check: could not parse '**Version:** `dev-...`' "
                "from release body — body shape may have changed",
            )
            return None
        if remote_version == __version__:
            log.info("Dev update check: up to date (current=%s)", __version__)
            return None
        log.info(
            "Dev update check: NEW version available remote=%s current=%s",
            remote_version, __version__,
        )

        # Find the installer asset. Prefer the API endpoint URL over
        # browser_download_url — the latter routes through GitHub's
        # filename-based redirect which can return 404 even when the
        # asset is healthy (we hit this on dev-latest after multiple
        # re-uploads). The API URL with Accept: application/octet-stream
        # is the GitHub-recommended download path and bypasses the
        # broken redirect.
        download_url = None
        is_installer = False
        for asset in data.get("assets", []):
            if asset["name"] == ASSET_NAME_DEV:
                download_url = asset.get("url") or asset.get("browser_download_url")
                is_installer = True
                break

        if not download_url:
            download_url = data.get("html_url", "")

        return {
            "version": remote_version,
            "current": __version__,
            "download_url": download_url,
            "release_url": data.get("html_url", ""),
            "body": data.get("body", ""),
            "is_installer": is_installer,
        }
    except Exception:
        log.warning("Dev update check failed with exception", exc_info=True)
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
                    log.warning(
                        "Stable update check: GitHub API returned status %d "
                        "for %s — treating as 'no update available'",
                        resp.status, RELEASES_API,
                    )
                    return None
                data = await resp.json()

        tag = data.get("tag_name", "")
        remote_ver = _parse_version(tag)
        local_ver = _parse_version(__version__)

        if not remote_ver:
            log.warning(
                "Stable update check: could not parse tag_name=%r", tag,
            )
            return None
        if remote_ver <= local_ver:
            log.info(
                "Stable update check: up to date (current=%s, remote=%s)",
                __version__, tag,
            )
            return None
        log.info(
            "Stable update check: NEW version available remote=%s current=%s",
            tag, __version__,
        )

        # Find the installer asset (use API URL — see _check_dev_update)
        download_url = None
        is_installer = False
        for asset in data.get("assets", []):
            if asset["name"] == ASSET_NAME_STABLE:
                download_url = asset.get("url") or asset.get("browser_download_url")
                is_installer = True
                break

        if not download_url:
            download_url = data.get("html_url", "")

        return {
            "version": tag,
            "current": __version__,
            "download_url": download_url,
            "release_url": data.get("html_url", ""),
            "body": data.get("body", ""),
            "is_installer": is_installer,
        }
    except Exception:
        log.warning("Stable update check failed with exception", exc_info=True)
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
    """Download the installer to a temp file and launch it."""
    try:
        tmp_dir = tempfile.mkdtemp(prefix="tribewatch_update_")
        asset_name = ASSET_NAME_DEV if _is_dev_version() else ASSET_NAME_STABLE
        installer_path = os.path.join(tmp_dir, asset_name)

        # GitHub release-asset API endpoint requires
        # Accept: application/octet-stream to return the binary;
        # otherwise it returns the asset metadata JSON. The
        # browser_download_url accepts anything but is unreliable.
        headers = {"Accept": "application/octet-stream"}

        log.info("Downloading update from %s", download_url)
        async with aiohttp.ClientSession() as session:
            async with session.get(
                download_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as resp:
                resp.raise_for_status()
                with open(installer_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(8192):
                        f.write(chunk)

        log.info("Launching installer: %s", installer_path)
        subprocess.Popen(
            [installer_path, "/SILENT", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"],
            creationflags=subprocess.DETACHED_PROCESS,
        )
        return True
    except Exception:
        log.warning("Failed to download/run installer", exc_info=True)
        return False


def is_frozen() -> bool:
    """Return True if running as a PyInstaller bundle."""
    return getattr(sys, "frozen", False)
