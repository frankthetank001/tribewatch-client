"""Thin HTTP client for the desktop client to call TribeWatch server APIs.

The client already speaks WebSocket via :mod:`tribewatch.relay`, but a
handful of operations (intentional tribe creation, rename) are
plain REST endpoints. This module wraps them with the bearer-token
authentication that ``require_auth`` accepts on the server side.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

log = logging.getLogger(__name__)


def _http_base(server_url: str) -> str:
    """Convert a ws(s):// or http(s):// URL to its http(s):// base."""
    url = server_url.rstrip("/")
    if url.endswith("/ws/relay"):
        url = url[: -len("/ws/relay")]
    if url.startswith("wss://"):
        url = "https://" + url[len("wss://"):]
    elif url.startswith("ws://"):
        url = "http://" + url[len("ws://"):]
    return url


async def claim_tribe(
    server_url: str, client_token: str, *, name: str, server_id: str,
) -> dict[str, Any]:
    """Create (or claim) a tribe owned by the calling user.

    Calls ``POST /api/tribe/tribes/claim``. Raises ``RuntimeError`` on
    non-2xx responses.
    """
    base = _http_base(server_url)
    url = f"{base}/api/tribe/tribes/claim"
    headers = {"Authorization": f"Bearer {client_token}"}
    payload = {"name": name, "server_id": server_id}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            text = await resp.text()
            if resp.status >= 300:
                raise RuntimeError(
                    f"claim_tribe failed: HTTP {resp.status}: {text[:200]}"
                )
            try:
                return await resp.json(content_type=None)
            except Exception:
                return {"raw": text}


async def rename_tribe(
    server_url: str, client_token: str, *, tribe_id: int, new_name: str,
) -> dict[str, Any]:
    """Rename a tribe via ``POST /api/tribe/tribes/{tribe_id}/rename``."""
    base = _http_base(server_url)
    url = f"{base}/api/tribe/tribes/{tribe_id}/rename"
    headers = {"Authorization": f"Bearer {client_token}"}
    payload = {"new_name": new_name}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            text = await resp.text()
            if resp.status >= 300:
                raise RuntimeError(
                    f"rename_tribe failed: HTTP {resp.status}: {text[:200]}"
                )
            try:
                return await resp.json(content_type=None)
            except Exception:
                return {"raw": text}
