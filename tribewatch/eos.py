"""Async EOS (Epic Online Services) client for querying ARK: ASA server info."""

from __future__ import annotations

import base64
import logging
import re
import time

import aiohttp

log = logging.getLogger(__name__)

# ARK: Survival Ascended EOS credentials (public, embedded in game client)
CLIENT_ID = "xyza7891muomRmynIIHaJB9COBKkwj6n"
CLIENT_SECRET = "PP5UGxysEieNfSrEicaD1N2Bb3TdXuD7xHYcsdUHZ7s"
DEPLOYMENT_ID = "ad9a8feffb3b4b2ca315546f038c3ae2"

AUTH_URL = "https://api.epicgames.dev/auth/v1/oauth/token"
MATCHMAKING_URL = (
    f"https://api.epicgames.dev/wildcard/matchmaking/v1/{DEPLOYMENT_ID}/filter"
)


class AsyncEOSClient:
    """Async EOS matchmaking client using aiohttp."""

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._token_expires_at: float = 0

    async def _ensure_auth(self, session: aiohttp.ClientSession) -> None:
        """Authenticate if token is missing or expired."""
        if self._access_token and time.time() < self._token_expires_at:
            return
        credentials = base64.b64encode(
            f"{CLIENT_ID}:{CLIENT_SECRET}".encode()
        ).decode()
        async with session.post(
            AUTH_URL,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "client_credentials",
                "deployment_id": DEPLOYMENT_ID,
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        self._access_token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 3600) - 60
        log.debug("EOS auth token acquired, expires in %ds", data.get("expires_in", 0))

    async def query_servers(
        self,
        criteria: list[dict] | None = None,
        max_results: int = 10,
    ) -> list[dict]:
        """Query the EOS matchmaking API. Returns list of session dicts."""
        try:
            async with aiohttp.ClientSession() as session:
                await self._ensure_auth(session)
                body: dict = {"criteria": criteria or []}
                if max_results:
                    body["maxResults"] = max_results
                async with session.post(
                    MATCHMAKING_URL,
                    headers={
                        "Authorization": f"Bearer {self._access_token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                return data.get("sessions", [])
        except Exception:
            log.warning("EOS query_servers failed", exc_info=True)
            return []

    async def get_server_by_name(self, name: str) -> dict | None:
        """Find server matching the exact name, picking the session with the most players.

        EOS returns duplicate sessions per server with different player counts.
        The highest ``totalPlayers`` best matches what the in-game browser shows.
        """
        criteria = [
            {"key": "attributes.CUSTOMSERVERNAME_s", "op": "EQUAL", "value": name}
        ]
        sessions = await self.query_servers(criteria, max_results=10)
        if not sessions:
            return None
        return max(sessions, key=lambda s: s.get("totalPlayers", 0))


class BattleMetricsClient:
    """Fallback server query via BattleMetrics public API (no auth required)."""

    SEARCH_URL = "https://api.battlemetrics.com/servers"

    async def get_server_by_name(self, name: str) -> dict | None:
        """Find an ARK:SA server by name. Returns server dict or None."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.SEARCH_URL,
                    params={
                        "filter[game]": "arksa",
                        "filter[search]": name,
                        "page[size]": "5",
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        log.debug("BattleMetrics API returned %d", resp.status)
                        return None
                    data = await resp.json()
                    servers = data.get("data", [])
                    if not servers:
                        return None
                    # Find exact name match, prefer online
                    for s in servers:
                        if s.get("attributes", {}).get("name") == name:
                            return s
                    return servers[0]  # best guess
        except Exception:
            log.debug("BattleMetrics query failed", exc_info=True)
            return None


def extract_battlemetrics_info(server: dict) -> dict:
    """Pull structured info from a BattleMetrics server dict."""
    attrs = server.get("attributes", {})
    details = attrs.get("details", {})
    return {
        "day": details.get("time_i"),
        "total_players": attrs.get("players", 0),
        "max_players": attrs.get("maxPlayers", 0),
        "map_name": details.get("map", ""),
        "server_name": attrs.get("name", ""),
        "status": attrs.get("status", ""),
        "source": "battlemetrics",
    }


def parse_eos_daytime(raw: str) -> int | None:
    """Parse DAYTIME_s into an in-game day number, or None.

    Despite the name, EOS ``DAYTIME_s`` is the in-game **day number**
    (e.g. ``"1153"`` for Day 1153).  The ``_s`` suffix is the EOS type
    tag for string, not "seconds".
    """
    if not raw:
        return None
    try:
        day = int(raw)
        if day >= 0:
            log.debug("Parsed EOS DAYTIME_s=%r → Day %d", raw, day)
            return day
    except (ValueError, TypeError):
        pass

    log.warning("Could not parse EOS DAYTIME_s=%r", raw)
    return None


def extract_server_info(session: dict) -> dict:
    """Pull structured info from a raw EOS session dict.

    Returns a dict with server metadata suitable for UI display and dedup.
    """
    attrs = session.get("attributes", {})
    settings = session.get("settings", {})

    daytime_raw = str(attrs.get("DAYTIME_s", ""))
    day = parse_eos_daytime(daytime_raw)

    return {
        "daytime_raw": daytime_raw,
        "day": day,
        "total_players": session.get("totalPlayers", 0),
        "max_players": settings.get("maxPublicPlayers", 0),
        "map_name": attrs.get("MAPNAME_s", ""),
        "is_pve": not attrs.get("SESSIONISPVE_l", 0) == 0
        if "SESSIONISPVE_l" in attrs
        else None,
        "server_name": attrs.get("CUSTOMSERVERNAME_s", ""),
        "build_id": attrs.get("BUILDID_s", ""),
        "cluster_id": attrs.get("CLUSTERID_s", ""),
        "platform": attrs.get("SERVERPLATFORMTYPE_s", ""),
        "ping": attrs.get("EOSSERVERPING_l"),
    }
