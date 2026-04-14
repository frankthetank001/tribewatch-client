"""Client-side WebSocket relay for sending events/status to a remote server."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import deque
from typing import Any, Callable

import aiohttp

log = logging.getLogger(__name__)


class ServerRelay:
    """Maintains a WebSocket connection to a remote TribeWatch server.

    Sends events, status heartbeats, and ping updates.
    Receives control commands and config updates from the server.
    Auto-reconnects with configurable delay.
    """

    def __init__(
        self,
        server_url: str,
        auth_token: str = "",
        client_token: str = "",
        reconnect_delay: float = 5.0,
        on_control: Callable[[str, str], Any] | None = None,
        on_config_update: Callable[[str, dict, str], Any] | None = None,
        on_auth_expired: Callable[[], Any] | None = None,
        on_tribe_unknown: Callable[[dict], Any] | None = None,
        on_connect: Callable[[], Any] | None = None,
    ) -> None:
        self._server_url = self._normalize_url(server_url)
        self._auth_token = auth_token
        self._client_token = client_token
        self._on_auth_expired = on_auth_expired
        self._reconnect_delay = reconnect_delay
        self._on_control = on_control  # (command, msg_id) -> ...
        self._on_config_update = on_config_update  # (section, data, msg_id) -> ...
        self._on_tribe_unknown = on_tribe_unknown  # (msg) -> ...
        self._on_connect = on_connect  # () -> ...

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._connected = False
        self._running = False
        self._auth_gate: asyncio.Event | None = None  # blocks reconnect during re-auth

        # Pending ack futures: msg_id -> Future
        self._pending_acks: dict[str, asyncio.Future] = {}

        # Event buffer for offline periods
        self._event_buffer: deque[list[dict]] = deque(maxlen=100)

        # Latest config snapshot — sent on every (re)connection
        self._pending_config: dict | None = None

        # Background tasks
        self._connect_task: asyncio.Task | None = None
        self._listen_task: asyncio.Task | None = None

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Normalize server URL: http(s) → ws(s), ensure /ws/relay path."""
        url = url.rstrip("/")
        if url.startswith("https://"):
            url = "wss://" + url[len("https://"):]
        elif url.startswith("http://"):
            url = "ws://" + url[len("http://"):]
        if not url.endswith("/ws/relay"):
            url += "/ws/relay"
        return url

    @property
    def connected(self) -> bool:
        return self._connected

    def set_client_token(self, token: str) -> None:
        """Update the client token and unblock reconnection."""
        self._client_token = token
        if self._auth_gate:
            self._auth_gate.set()
            self._auth_gate = None

    async def start(self) -> None:
        """Start the relay connection loop."""
        self._running = True
        self._session = aiohttp.ClientSession()
        self._connect_task = asyncio.create_task(self._connection_loop())

    async def stop(self) -> None:
        """Stop the relay and clean up."""
        self._running = False
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._connect_task:
            self._connect_task.cancel()
            try:
                await self._connect_task
            except (asyncio.CancelledError, Exception):
                pass
        # Send clean shutdown notification before closing
        if self._ws and not self._ws.closed:
            try:
                await self._ws.send_json({"type": "shutdown"})
            except Exception:
                pass
            await self._ws.close()
        if self._session:
            await self._session.close()
        self._connected = False

    async def _connection_loop(self) -> None:
        """Connect and reconnect in a loop."""
        while self._running:
            try:
                await self._connect()
            except asyncio.CancelledError:
                return
            except Exception:
                log.debug("Relay connection error", exc_info=True)
            self._connected = False
            # If re-auth is in progress, wait for it before reconnecting
            if self._auth_gate:
                log.info("Waiting for re-authentication before reconnecting...")
                await self._auth_gate.wait()
            if self._running:
                log.info("Relay disconnected, reconnecting in %.0fs...", self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)

    async def _connect(self) -> None:
        """Establish WebSocket connection, authenticate, and start listening."""
        assert self._session is not None
        log.info("Connecting to relay server: %s", self._server_url)
        self._ws = await self._session.ws_connect(self._server_url)

        # Authenticate — prefer client_token (Discord OAuth signed token),
        # fall back to legacy auth_token (shared secret)
        auth_msg: dict[str, str] = {"type": "auth"}
        if self._client_token:
            auth_msg["client_token"] = self._client_token
        else:
            auth_msg["token"] = self._auth_token
        await self._ws.send_json(auth_msg)
        auth_resp = await self._ws.receive_json()
        if auth_resp.get("type") == "auth_expired":
            log.warning("Client token expired — re-authenticating via Discord OAuth")
            await self._ws.close()
            # Block reconnection until re-auth completes (set_client_token unblocks)
            self._auth_gate = asyncio.Event()
            if self._on_auth_expired:
                try:
                    self._on_auth_expired()
                except Exception:
                    log.exception("Auth expired callback error")
            return
        if auth_resp.get("type") != "auth_ok":
            log.error("Relay authentication failed: %s", auth_resp)
            await self._ws.close()
            return

        self._connected = True
        log.info("Relay connected and authenticated")

        # Send pending config snapshot (tribe_name etc.)
        if self._pending_config is not None:
            try:
                await self._ws.send_json({"type": "config", "data": self._pending_config})
            except Exception:
                log.debug("Failed to send config on connect", exc_info=True)

        # Fire on-connect callback (e.g. send reconnect history)
        if self._on_connect:
            try:
                result = self._on_connect()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                log.debug("on_connect callback error", exc_info=True)

        # Flush buffered events
        await self._flush_buffer()

        # Listen for server messages
        self._listen_task = asyncio.create_task(self._listen())
        try:
            await self._listen_task
        except asyncio.CancelledError:
            pass

    async def _listen(self) -> None:
        """Listen for messages from the server."""
        assert self._ws is not None
        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    await self._handle_message(data)
                except json.JSONDecodeError:
                    log.debug("Invalid JSON from server: %s", msg.data[:100])
            elif msg.type == aiohttp.WSMsgType.ERROR:
                exc = self._ws.exception()
                log.warning("Relay WebSocket error: %s", exc)
                break
            elif msg.type == aiohttp.WSMsgType.CLOSED:
                close_code = getattr(self._ws, "close_code", None)
                log.warning("Relay WebSocket closed by server (code=%s)", close_code)
                break
            elif msg.type == aiohttp.WSMsgType.CLOSE:
                log.warning("Relay WebSocket received CLOSE frame")
                break

    async def _handle_message(self, data: dict) -> None:
        """Handle an incoming message from the server."""
        msg_type = data.get("type")

        if msg_type == "events_ack":
            msg_id = data.get("msg_id")
            if msg_id and msg_id in self._pending_acks:
                self._pending_acks[msg_id].set_result(data.get("ids", []))

        elif msg_type == "events_nack":
            msg_id = data.get("msg_id")
            err = data.get("error", "unknown server error")
            if msg_id and msg_id in self._pending_acks:
                self._pending_acks[msg_id].set_exception(RuntimeError(f"server rejected batch: {err}"))

        elif msg_type == "control":
            command = data.get("command", "")
            msg_id = data.get("msg_id", "")
            log.info("Received control command from server: %s", command)
            ok = True
            if self._on_control:
                try:
                    self._on_control(command, msg_id)
                except Exception:
                    log.exception("Control callback error")
                    ok = False
            # Send acknowledgement
            if self._ws and not self._ws.closed:
                await self._ws.send_json({
                    "type": "control_ack",
                    "msg_id": msg_id,
                    "ok": ok,
                })

        elif msg_type == "tribe_unknown":
            # Server doesn't recognise (tribe_name, server_id) for this user.
            # Surface so the operator can rename or create a new tribe via
            # the dashboard. We don't auto-act here — that's an explicit
            # user choice.
            detected = data.get("detected_name", "")
            server_id = data.get("server_id", "")
            candidates = data.get("candidates", [])
            cand_str = ", ".join(
                f"{c.get('tribe_name','?')} (id={c.get('tribe_id','?')})"
                for c in candidates
            ) or "none"
            log.warning(
                "Server reported tribe unknown: detected=%r server_id=%r — "
                "existing tribes for this account on this server: %s.",
                detected, server_id, cand_str,
            )
            if self._on_tribe_unknown:
                try:
                    res = self._on_tribe_unknown(data)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    log.exception("on_tribe_unknown callback error")

        elif msg_type == "config_update":
            section = data.get("section", "")
            payload = data.get("data", {})
            msg_id = data.get("msg_id", "")
            log.info("Received config update from server: section=%s", section)
            ok = True
            if self._on_config_update:
                try:
                    self._on_config_update(section, payload, msg_id)
                except Exception:
                    log.exception("Config update callback error")
                    ok = False
            if self._ws and not self._ws.closed:
                await self._ws.send_json({
                    "type": "control_ack",
                    "msg_id": msg_id,
                    "ok": ok,
                })

    async def send_events(self, event_dicts: list[dict]) -> list[int]:
        """Send events to the server, returns assigned DB IDs.

        If disconnected, buffers events and returns [0] * len(event_dicts).
        """
        if not self._connected or not self._ws or self._ws.closed:
            self._event_buffer.append(event_dicts)
            return [0] * len(event_dicts)

        msg_id = str(uuid.uuid4())
        future: asyncio.Future[list[int]] = asyncio.get_event_loop().create_future()
        self._pending_acks[msg_id] = future

        try:
            await self._ws.send_json({
                "type": "events",
                "msg_id": msg_id,
                "events": event_dicts,
            })
            ids = await asyncio.wait_for(future, timeout=10.0)
            return ids
        except (asyncio.TimeoutError, Exception):
            log.debug("Failed to send events, buffering")
            self._event_buffer.append(event_dicts)
            return [0] * len(event_dicts)
        finally:
            self._pending_acks.pop(msg_id, None)

    async def send_status(self, data: dict) -> None:
        """Send a status heartbeat to the server (fire-and-forget)."""
        if not self._connected or not self._ws or self._ws.closed:
            return
        try:
            await self._ws.send_json({"type": "status", "data": data})
        except Exception:
            log.debug("Failed to send status", exc_info=True)

    async def send_tribe_info(self, data: dict) -> None:
        """Send tribe info to the server (fire-and-forget)."""
        if not self._connected or not self._ws or self._ws.closed:
            return
        try:
            await self._ws.send_json({"type": "tribe_info", "data": data})
        except Exception:
            log.debug("Failed to send tribe info", exc_info=True)

    async def send_join_leave(self, events: list[dict]) -> None:
        """Send join/leave events to the server for member status resolution."""
        if not self._connected or not self._ws or self._ws.closed:
            return
        try:
            await self._ws.send_json({"type": "join_leave", "events": events})
        except Exception:
            log.debug("Failed to send join/leave events", exc_info=True)

    async def send_server_joins(self, events: list[dict]) -> None:
        """Send non-tribemate server join events to the server (fire-and-forget)."""
        if not self._connected or not self._ws or self._ws.closed:
            return
        try:
            await self._ws.send_json({"type": "server_joins", "events": events})
        except Exception:
            log.debug("Failed to send server join events", exc_info=True)

    async def send_tribe_window_lost(self, tribe_name: str) -> None:
        """Notify the server that the tribe window has been lost past grace period."""
        if not self._connected or not self._ws or self._ws.closed:
            return
        try:
            await self._ws.send_json({
                "type": "tribe_window_lost",
                "tribe_name": tribe_name,
            })
        except Exception:
            log.debug("Failed to send tribe_window_lost", exc_info=True)

    async def send_log_dump(self, lines: list[str], msg_id: str = "") -> None:
        """Send buffered log lines to the server."""
        if not self._connected or not self._ws or self._ws.closed:
            return
        try:
            await self._ws.send_json({
                "type": "log_dump",
                "msg_id": msg_id,
                "lines": lines,
            })
        except Exception:
            log.debug("Failed to send log dump", exc_info=True)

    async def send_log_line(self, line: str) -> None:
        """Stream a single log line to the server (fire-and-forget)."""
        if not self._connected or not self._ws or self._ws.closed:
            return
        try:
            await self._ws.send_json({
                "type": "log_line",
                "line": line,
            })
        except Exception:
            pass  # never let streaming break the app

    async def send_screenshot_response(self, msg_id: str, image_b64: str) -> None:
        """Send a screenshot response to the server (fire-and-forget)."""
        if not self._connected or not self._ws or self._ws.closed:
            return
        try:
            await self._ws.send_json({
                "type": "screenshot_response",
                "msg_id": msg_id,
                "image": image_b64,
            })
        except Exception:
            log.debug("Failed to send screenshot response", exc_info=True)

    async def send_character_death(self) -> None:
        """Notify the server that the character death screen was detected."""
        if not self._connected or not self._ws or self._ws.closed:
            return
        try:
            await self._ws.send_json({"type": "character_death"})
        except Exception:
            log.debug("Failed to send character_death", exc_info=True)

    async def send_reconnect_record(self, record: dict) -> None:
        """Send a completed reconnect audit record to the server."""
        if not self._connected or not self._ws or self._ws.closed:
            return
        try:
            await self._ws.send_json({"type": "reconnect_record", "record": record})
        except Exception:
            log.debug("Failed to send reconnect record", exc_info=True)

    async def send_reconnect_history(self, records: list[dict]) -> None:
        """Send recent reconnect history to the server (e.g. on connect)."""
        if not self._connected or not self._ws or self._ws.closed:
            return
        try:
            await self._ws.send_json({"type": "reconnect_history", "records": records})
        except Exception:
            log.debug("Failed to send reconnect history", exc_info=True)

    async def send_reconnect_status(
        self, stage: str, message: str, image: str = "", auto: bool = False,
    ) -> None:
        """Send a reconnect status update to the server (fire-and-forget).

        *image* is an optional base64-encoded JPEG screenshot for this stage.
        *auto* indicates whether this was triggered automatically (vs manual).
        """
        if not self._connected or not self._ws or self._ws.closed:
            return
        try:
            payload: dict = {
                "type": "reconnect_status",
                "stage": stage,
                "message": message,
                "auto": auto,
            }
            if image:
                payload["image"] = image
            await self._ws.send_json(payload)
        except Exception:
            log.debug("Failed to send reconnect status", exc_info=True)

    async def send_config(self, data: dict) -> None:
        """Send a config snapshot to the server.

        Always stores the latest config so it can be (re-)sent on
        each connection — this avoids the race where ``send_config``
        is called before the relay has connected.
        """
        self._pending_config = data
        if not self._connected or not self._ws or self._ws.closed:
            return
        try:
            await self._ws.send_json({"type": "config", "data": data})
        except Exception:
            log.debug("Failed to send config", exc_info=True)

    async def _flush_buffer(self) -> None:
        """Flush buffered events after reconnection, waiting for acks."""
        while self._event_buffer and self._connected:
            batch = self._event_buffer.popleft()
            msg_id = str(uuid.uuid4())
            future: asyncio.Future[list[int]] = asyncio.get_event_loop().create_future()
            self._pending_acks[msg_id] = future
            try:
                assert self._ws is not None
                await self._ws.send_json({
                    "type": "events",
                    "msg_id": msg_id,
                    "events": batch,
                })
                await asyncio.wait_for(future, timeout=10.0)
                log.info("Flushed %d buffered events to server", len(batch))
            except asyncio.TimeoutError:
                self._event_buffer.appendleft(batch)
                log.warning(
                    "Failed to flush %d buffered events: server ack timeout after 10s — will retry on next connect",
                    len(batch),
                )
                break
            except Exception as e:
                self._event_buffer.appendleft(batch)
                log.warning(
                    "Failed to flush %d buffered events: %s: %s — will retry on next connect",
                    len(batch), type(e).__name__, e,
                )
                break
            finally:
                self._pending_acks.pop(msg_id, None)
