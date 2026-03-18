"""Main application orchestrator — capture → OCR → parse → dedup → Discord."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from tribewatch.capture import ScreenCapture
from tribewatch.config import AlertRule, ParasaurAlertSettings, TribeWatchConfig
from tribewatch.dedup import DedupStore
from tribewatch.ocr_engine import recognize
from tribewatch.parser import EVENT_TYPE_LABELS, EventType, JoinLeaveEvent, ServerJoinEvent, Severity, TribeInfo, TribeLogEvent, extract_member_name, parse_events, parse_join_leave_notifications, parse_parasaur_notification, parse_server_join_notifications, parse_tribe_window
from tribewatch.fuzzy import edit_distance, fuzzy_threshold, names_match
from tribewatch.webhook import WebhookDispatcher, resolve_mention

if TYPE_CHECKING:
    from tribewatch.relay import ServerRelay
    from tribewatch.web.event_store import EventStore
    from tribewatch.web.server import WebSocketManager
    from tribewatch.web.tribe_store import TribeStore

log = logging.getLogger(__name__)


async def _discover_tribe_name_async(config: TribeWatchConfig) -> str:
    """Async implementation of tribe name discovery via OCR."""
    if not config.tribe.bbox:
        return ""

    from tribewatch.ocr_engine import preprocess_tribe_window

    cap = ScreenCapture(
        bbox=config.tribe.bbox,
        monitor=config.general.monitor,
        window_title=config.general.window_title,
    )
    img = cap.grab()
    cap.close()

    if img is None:
        return ""

    engine = config.tribe.ocr_engine or config.tribe_log.ocr_engine
    threshold = 180 if engine == "tesseract" else 0
    preprocessed = preprocess_tribe_window(img, binary_threshold=threshold)
    text = await recognize(
        preprocessed,
        engine=engine,
        upscale=1,  # already upscaled by preprocess_tribe_window
        tesseract_path=config.tribe_log.tesseract_path,
        retries=0,
        preprocess=False,
    )

    if not text.strip():
        return ""

    info = parse_tribe_window(text)
    if info is None:
        return ""

    return info.tribe_name


def discover_tribe_name(config: TribeWatchConfig) -> str:
    """Capture the tribe window once and return the detected tribe name.

    This is a synchronous wrapper intended to be called before the async
    event loop starts.  Returns empty string if the tribe window is
    unreadable or tribe bbox is not configured.
    """
    return asyncio.run(_discover_tribe_name_async(config))


_DEFAULT_ACTIONS: dict[str, str] = {
    "dino_killed": "critical",
    "structure_destroyed": "critical",
    "tribe_member_killed": "critical",
    "demolished": "batch",
    "anti_meshed": "critical",
    "enemy_player_killed": "batch",
    "enemy_structure_destroyed": "batch",
}

_PARASAUR_BRIEF_MAP: dict[str, str] = {
    "parasaur_detection": "parasaur_brief",
    "parasaur_babies": "parasaur_brief_babies",
}


def resolve_event_action(
    event: TribeLogEvent, config: TribeWatchConfig
) -> tuple[str, str | None, bool, bool, str, bool]:
    """Resolve the action for an event based on alert rules.

    Returns (action, severity_override, ping, discord, ping_target, ping_member) where:
    - action: "critical", "batch", or "ignore"
    - discord: whether to send to Discord at all
    - ping: whether to include @mention
    - ping_target: role/user ID to ping (empty = use global); ``!owner`` resolved
    - ping_member: whether to @mention the specific tribe member involved
    """
    from tribewatch.webhook import resolve_mention

    owner_id = config.discord.owner_discord_id
    mentions = config.discord.mentions
    ev_type = event.event_type.value
    raw_text_lower = event.raw_text.lower()

    # Sort rules so conditioned (non-empty text_contains) come first per event_type
    sorted_rules = sorted(
        config.alerts.rules,
        key=lambda r: (r.event_type, 0 if r.text_contains else 1),
    )

    # Check explicit rules first
    for rule in sorted_rules:
        if rule.event_type == ev_type:
            if rule.text_contains and rule.text_contains.lower() not in raw_text_lower:
                continue
            target = resolve_mention(rule.ping_target, mentions, owner_id)
            return rule.action, rule.severity_override or None, rule.ping, rule.discord, target, rule.ping_member

    # Fall back to built-in defaults
    default = _DEFAULT_ACTIONS.get(ev_type, "batch")
    is_critical = default == "critical"
    return default, None, is_critical, is_critical, "", False


def _format_duration(seconds: float) -> str:
    """Format a duration in seconds to a human-readable string."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    return f"{m}m {s}s"


class TribeWatchApp:
    """Orchestrates the full TribeWatch pipeline."""

    def __init__(
        self,
        config: TribeWatchConfig,
        event_store: EventStore | None = None,
        ws_manager: WebSocketManager | None = None,
        relay: ServerRelay | None = None,
        tribe_store: TribeStore | None = None,
    ) -> None:
        self.config = config
        self._relay = relay
        self.capture = ScreenCapture(
            bbox=config.tribe_log.bbox,
            monitor=config.general.monitor,
            window_title=config.general.window_title,
        )
        self._dedup_stores: dict[str, DedupStore] = {}
        self._state_file_base: Path = Path(config.general.state_file)
        self.dispatcher = WebhookDispatcher(
            alert_webhook=config.discord.alert_webhook,
            raid_webhook=config.discord.raid_webhook,
            debug_webhook=config.discord.debug_webhook,
            tasks_webhook=config.discord.tasks_webhook,
            ping_role_id=config.discord.ping_role_id,
            batch_interval=config.discord.batch_interval,
            owner_discord_id=config.discord.owner_discord_id,
            mentions=config.discord.mentions,
        )
        # Parasaur detection window (optional — only if bbox configured)
        self._parasaur_capture: ScreenCapture | None = None
        self._parasaur_lookup: dict[str, str] = {}
        # Session tracking: key = event_type.value, value = {start, last_seen, raw_text}
        self._parasaur_sessions: dict[str, dict] = {}
        if config.parasaur.bbox:
            self._parasaur_capture = ScreenCapture(
                bbox=config.parasaur.bbox,
                monitor=config.general.monitor,
                window_title=config.general.window_title,
            )
            self._parasaur_lookup = {
                p.name: p.mode for p in config.parasaur.parasaurs if p.name
            }

        # Tribe window capture (optional — only if bbox configured)
        self._tribe_capture: ScreenCapture | None = None
        self._tribe_info: TribeInfo | None = None
        if config.tribe.bbox:
            self._tribe_capture = ScreenCapture(
                bbox=config.tribe.bbox,
                monitor=config.general.monitor,
                window_title=config.general.window_title,
            )

        self._event_store = event_store
        self._ws_manager = ws_manager
        self._tribe_store = tribe_store
        self._running = False
        self._paused = False

        # Stats tracking
        self._last_capture_at: float | None = None
        self._last_ocr_duration_ms: float | None = None
        self._events_today_count: int = 0
        self._total_events_count: int = 0

        # Tribe log monitoring awareness
        self._last_log_seen_at: float | None = None
        self._log_header_visible: bool = False
        self._log_visible_since: float | None = None  # when LOG first appeared continuously
        self._tribe_window_visible: bool = False
        self._tribe_window_last_ok: float | None = None
        self._tribe_window_fail_since: float | None = None
        self._tribe_sessions_closed: bool = False  # prevents repeated close-session msgs
        self._started_at: float | None = None

        # Join/leave dedup: "platform_id:join" or "platform_id:leave" → monotonic time last seen
        self._join_leave_seen: dict[str, float] = {}

        # Non-tribemate server join dedup: player_name_lower → monotonic time last seen
        self._server_join_seen: dict[str, float] = {}

        # Escalation tracking
        self._escalation_events: dict[str, list[tuple[float, str]]] = {}  # event_type -> [(timestamp, raw_text)]
        self._escalation_suppressed_until: dict[str, float] = {}    # event_type -> monotonic deadline

        # Idle screen detection
        self._prev_thumb = None  # previous capture thumbnail for change detection
        self._screen_still_since: float | None = None  # time.time() when screen became static
        self._screen_change_pct: float = 100.0  # last measured change % (0-100)
        self._idle_recovery_attempted: bool = False  # prevents repeated recovery attempts
        self._auto_reconnect_cb = None  # callback set by __main__.py
        self._on_server_change_cb = None  # callback set by __main__.py
        self._server_id: str = ""
        self._server_name: str = ""

        # EOS server info
        self._eos_client: object | None = None  # lazy AsyncEOSClient
        self._eos_info: dict | None = None
        self._eos_last_query: float = 0  # monotonic time of last successful query
        self._EOS_REFRESH_INTERVAL: float = 300  # re-query every 5 minutes

        # Resolution preset tracking (presets are applied at startup by __main__)
        self._last_game_resolution: tuple[int, int] | None = None
        cal_res = config.general.calibration_resolution
        if cal_res and len(cal_res) == 2 and all(isinstance(v, int) and v > 0 for v in cal_res):
            self._last_game_resolution = (cal_res[0], cal_res[1])

    # -- Dynamic bbox preset switching -------------------------------------

    def _check_resolution_scaling(self) -> None:
        """Check game resolution and swap to preset bboxes if it changed."""
        from tribewatch.server_id import get_game_resolution

        game_res = get_game_resolution()
        if not game_res:
            return
        if game_res == getattr(self, "_last_game_resolution", None):
            return  # no change

        from tribewatch.calibrate import get_preset

        preset = get_preset(game_res)
        self._last_game_resolution = game_res

        if preset:
            self._apply_preset(preset, game_res)
        else:
            log.warning(
                "Resolution changed to %dx%d but no preset available — "
                "run --setup to calibrate",
                game_res[0], game_res[1],
            )

    def _apply_preset(self, preset: dict[str, list[int]], resolution: tuple[int, int]) -> None:
        """Apply a resolution preset to all capture regions."""
        self.capture.bbox = list(preset["tribe_log"])
        log.info(
            "Applied preset tribe_log bbox %s for %dx%d",
            preset["tribe_log"], resolution[0], resolution[1],
        )

        if getattr(self, "_parasaur_capture", None) and "parasaur" in preset:
            self._parasaur_capture.bbox = list(preset["parasaur"])
            log.info("Applied preset parasaur bbox %s", preset["parasaur"])

        if getattr(self, "_tribe_capture", None) and "tribe" in preset:
            self._tribe_capture.bbox = list(preset["tribe"])
            log.info("Applied preset tribe bbox %s", preset["tribe"])

    # -- Per-tribe dedup helpers ------------------------------------------

    @property
    def dedup(self) -> DedupStore:
        """Backward-compat property so ``self.dedup`` still works in tests."""
        return self._get_dedup()

    def _state_file_for(self, tribe_name: str) -> Path | None:
        """Compute state file path for a tribe, including server_id if known."""
        base = getattr(self, "_state_file_base", None)
        if base is None:
            return None
        if not tribe_name:
            return base
        parts = []
        server_id = getattr(self, "_server_id", "")
        if server_id:
            parts.append(re.sub(r'[<>:"/\\|?*\s]+', "_", server_id).strip("_").lower())
        parts.append(re.sub(r'[<>:"/\\|?*\s]+', "_", tribe_name).strip("_").lower())
        suffix = "_".join(parts)
        return base.with_stem(f"{base.stem}_{suffix}")

    def _migrate_dedup_for_server_id(self, server_id: str) -> None:
        """Rename dedup state files to include server_id when it first becomes known."""
        if not server_id:
            return
        for tribe_name, store in list(self._dedup_stores.items()):
            if not tribe_name:
                continue  # generic store, will be migrated when tribe name is known
            old_sf = store.state_file
            new_sf = self._state_file_for(tribe_name)
            if old_sf and new_sf and old_sf != new_sf:
                store.state_file = new_sf
                store.save()
                if old_sf.exists():
                    try:
                        old_sf.unlink()
                    except OSError:
                        pass
                log.info("Migrated dedup state %s -> %s", old_sf.name, new_sf.name)

    def _get_dedup(self) -> DedupStore:
        """Return the DedupStore for the currently monitored tribe.

        Creates a new store on first access for each tribe, using a
        per-tribe state file so that high-water marks and hash buffers
        don't leak across tribes.
        """
        tribe_name = ""
        info = getattr(self, "_tribe_info", None)
        if info is not None:
            tribe_name = info.tribe_name or ""

        if tribe_name not in self._dedup_stores:
            sf = self._state_file_for(tribe_name)
            # Migrate legacy state file (without server_id) if the new path doesn't exist yet
            if sf and not sf.exists() and tribe_name:
                base = getattr(self, "_state_file_base", None)
                if base:
                    legacy_safe = re.sub(r'[<>:"/\\|?*\s]+', "_", tribe_name).strip("_").lower()
                    legacy_sf = base.with_stem(f"{base.stem}_{legacy_safe}")
                    if legacy_sf != sf and legacy_sf.exists():
                        try:
                            legacy_sf.rename(sf)
                            log.info("Migrated state file %s -> %s", legacy_sf.name, sf.name)
                        except OSError:
                            pass
            self._dedup_stores[tribe_name] = DedupStore(state_file=sf)

        return self._dedup_stores[tribe_name]

    _SEVERITY_PREFIX = {
        Severity.CRITICAL: "\033[91m[CRITICAL]\033[0m",
        Severity.WARNING: "\033[93m[WARNING]\033[0m",
        Severity.INFO: "\033[92m[INFO]\033[0m",
    }

    def _print_event(self, event: TribeLogEvent) -> None:
        """Print an event to stdout with a colour-coded severity prefix."""
        prefix = self._SEVERITY_PREFIX.get(event.severity, "[???]")
        print(f"{prefix} Day {event.day}, {event.time}: {event.raw_text}")

    def _find_alert_rule(self, event_type: str, raw_text: str = "") -> AlertRule | None:
        """Find the AlertRule for a given event type, or None.

        Conditioned rules (non-empty text_contains) are checked first so
        the most specific match wins.
        """
        raw_lower = raw_text.lower()
        sorted_rules = sorted(
            self.config.alerts.rules,
            key=lambda r: (r.event_type, 0 if r.text_contains else 1),
        )
        for rule in sorted_rules:
            if rule.event_type == event_type:
                if rule.text_contains and rule.text_contains.lower() not in raw_lower:
                    continue
                return rule
        return None

    def _find_member_discord_ids(
        self, member_name: str, members: list[dict],
    ) -> list[str]:
        """Fuzzy-match a member name against cached tribe members.

        Returns a list of discord_id strings for matched members (usually 0 or 1).
        """
        if not member_name or not members:
            return []

        name_lower = member_name.lower()
        for m in members:
            # Try exact match on name or display_name first
            if name_lower == m["name"].lower():
                return [m["discord_id"]] if m.get("discord_id") else []
            display = m.get("display_name") or ""
            if display and name_lower == display.lower():
                return [m["discord_id"]] if m.get("discord_id") else []

        # Fuzzy match
        threshold = _fuzzy_threshold(member_name)
        best_member: dict | None = None
        best_dist = threshold + 1
        for m in members:
            d = _edit_distance(name_lower, m["name"].lower())
            if d < best_dist:
                best_dist = d
                best_member = m
            display = m.get("display_name") or ""
            if display:
                d2 = _edit_distance(name_lower, display.lower())
                if d2 < best_dist:
                    best_dist = d2
                    best_member = m

        if best_member is not None and best_dist <= threshold:
            return [best_member["discord_id"]] if best_member.get("discord_id") else []

        return []

    def _parasaur_alert_settings(self, session_key: str) -> ParasaurAlertSettings:
        """Return the ParasaurAlertSettings for a session key."""
        if session_key == "parasaur_babies":
            return self.config.parasaur.babies_alerts
        return self.config.parasaur.player_alerts

    def _check_escalation(
        self, event_type: str, rule: AlertRule | None, raw_text: str = "",
    ) -> tuple[str, list[str]]:
        """Check escalation state for an event type.

        Returns:
            (state, event_texts) where state is one of:
            "normal"   — send individual alert as usual
            "escalate" — threshold just hit, send escalation summary
            "suppress" — inside suppression window, skip Discord

            event_texts is non-empty only when state == "escalate".
        """
        if rule is None or rule.escalation_count <= 0:
            return "normal", []

        now = time.monotonic()
        window_secs = rule.escalation_window * 60

        # Currently suppressed?
        deadline = self._escalation_suppressed_until.get(event_type)
        if deadline is not None:
            if now < deadline:
                return "suppress", []
            # Window expired — reset
            del self._escalation_suppressed_until[event_type]
            self._escalation_events.pop(event_type, None)

        # Track timestamp + raw text
        entries = self._escalation_events.setdefault(event_type, [])
        entries.append((now, raw_text))

        # Prune entries outside the window
        cutoff = now - window_secs
        entries[:] = [e for e in entries if e[0] > cutoff]

        # Check threshold
        if len(entries) >= rule.escalation_count:
            self._escalation_suppressed_until[event_type] = now + window_secs
            texts = [e[1] for e in entries if e[1]]
            return "escalate", texts

        return ("suppress" if rule.suppress_individual else "normal"), []

    # --- Abstraction helpers: local vs relay ---

    async def _store_events(self, event_dicts: list[dict]) -> list[int]:
        """Insert events into local EventStore or send via relay."""
        if self._relay:
            return await self._relay.send_events(event_dicts)
        if self._event_store:
            return await self._event_store.insert_many(event_dicts)
        return [0] * len(event_dicts)

    async def _update_ping(self, event_id: int, ping_status: str, ping_detail: str) -> None:
        """Update ping status locally (no-op in relay/client mode — server handles dispatch)."""
        if event_id == 0 or self._relay:
            return
        if self._event_store:
            await self._event_store.update_ping(event_id, ping_status, ping_detail)

    async def _broadcast_events(self, event_dicts: list[dict]) -> None:
        """Broadcast events to browser WebSocket clients (no-op when relay is active)."""
        if self._relay:
            return  # server handles browser broadcast
        if self._ws_manager:
            await self._ws_manager.broadcast_events(event_dicts)

    def build_status(self) -> dict:
        """Build a status dict from the current app state."""
        now_mono = time.monotonic()
        status: dict = {
            "running": self._running,
            "paused": self._paused,
            "monitoring": False,
            "last_capture_at": self._last_capture_at,
            "last_log_seen_at": self._last_log_seen_at,
            "last_ocr_duration_ms": self._last_ocr_duration_ms,
            "events_today_count": self._events_today_count,
            "total_events": self._total_events_count,
            "idle_alert_minutes": self.config.alerts.idle_alert_minutes,
        }

        if self._running:
            # Only report monitoring=True after 30s of continuous visibility
            # to avoid flapping in both standalone and client→server modes.
            visible_since = getattr(self, "_log_visible_since", None)
            if self._log_header_visible and visible_since:
                status["monitoring"] = (time.time() - visible_since) >= 30
            else:
                status["monitoring"] = False

        # Parasaur sessions
        parasaur_activity = []
        for key, sess in self._parasaur_sessions.items():
            age = now_mono - sess.get("start", now_mono)
            silence = now_mono - sess.get("last_seen", now_mono)
            parasaur_activity.append({
                "key": key,
                "state": sess.get("state", "unknown"),
                "age_secs": round(age, 1),
                "silence_secs": round(silence, 1),
            })
        status["parasaur_sessions"] = parasaur_activity

        # Escalation progress
        rules = self.config.alerts.rules
        rule_map = {r.event_type: r for r in rules}
        escalation_activity = []
        for ev_type, entries in self._escalation_events.items():
            rule = rule_map.get(ev_type)
            if not rule or rule.escalation_count <= 0:
                continue
            window_secs = rule.escalation_window * 60
            cutoff = now_mono - window_secs
            recent = [e for e in entries if e[0] > cutoff]
            deadline = self._escalation_suppressed_until.get(ev_type)
            suppressed = deadline is not None and now_mono < deadline
            escalation_activity.append({
                "event_type": ev_type,
                "count": len(recent),
                "threshold": rule.escalation_count,
                "window_minutes": rule.escalation_window,
                "suppressed": suppressed,
                "suppressed_remaining_secs": round(deadline - now_mono, 1) if suppressed else 0,
            })
        status["escalation_progress"] = escalation_activity

        # Server info
        try:
            from tribewatch.server_id import get_server_info
            info = get_server_info()
            new_id = info["server_id"]
            new_name = info["server_name"]
            if new_id:
                old_id = getattr(self, "_server_id", "")
                if old_id and new_id != old_id:
                    cb = getattr(self, "_on_server_change_cb", None)
                    if cb:
                        cb(old_id, getattr(self, "_server_name", ""), new_id, new_name)
                    # Don't update _server_id/_server_name here — the
                    # change handler does it after the user confirms.
                else:
                    self._server_id = new_id
                    # Migrate dedup state files when server_id first becomes known
                    if not old_id:
                        self._migrate_dedup_for_server_id(new_id)
                    self._server_name = new_name
        except Exception:
            pass
        status["server_id"] = getattr(self, "_server_id", "")
        status["server_name"] = getattr(self, "_server_name", "")

        # Dynamic bbox scaling — check if game resolution changed
        try:
            self._check_resolution_scaling()
        except Exception:
            pass

        # Idle screen detection
        status["screen_still_since"] = getattr(self, "_screen_still_since", None)
        status["screen_change_pct"] = getattr(self, "_screen_change_pct", 100.0)

        # Component health
        status["components"] = {
            "ark_window": self.capture.window_found,
            "tribe_log": self._log_header_visible,
            "tribe_window": self._is_tribe_window_ok(),
            "parasaur": "enabled" if getattr(self, "_parasaur_capture", None) else "disabled",
        }

        # Tribe info
        if self._tribe_info is not None:
            from dataclasses import asdict as _asdict
            status["tribe_info"] = _asdict(self._tribe_info)

        # EOS server info (cached from last refresh)
        eos_info = getattr(self, "_eos_info", None)
        if eos_info is not None:
            status["eos_server_info"] = eos_info

        return status

    async def _refresh_eos_info(self) -> None:
        """Query EOS for server info if refresh interval has elapsed."""
        server_name = getattr(self, "_server_name", "")
        if not server_name:
            return

        now = time.monotonic()
        interval = getattr(self, "_EOS_REFRESH_INTERVAL", 300)
        last_query = getattr(self, "_eos_last_query", 0)
        if now - last_query < interval:
            return

        try:
            from tribewatch.eos import AsyncEOSClient, extract_server_info, parse_eos_daytime

            if getattr(self, "_eos_client", None) is None:
                self._eos_client = AsyncEOSClient()

            session = await self._eos_client.get_server_by_name(server_name)
            if session is None:
                log.debug("EOS: no session found for server %r", server_name)
                return

            info = extract_server_info(session)
            self._eos_info = info
            self._eos_last_query = now

            log.info(
                "EOS server info: %s — %d/%d players, map=%s, Day %s",
                info.get("server_name", "?"),
                info.get("total_players", 0),
                info.get("max_players", 0),
                info.get("map_name", "?"),
                info.get("day", "?"),
            )

            # Feed day to dedup stores as reference for garbled-OCR rejection
            eos_day = info.get("day")
            if eos_day is not None:
                for store in self._dedup_stores.values():
                    store.set_eos_reference(eos_day, "00:00:00")
                    store.seed_high_water_from_eos(eos_day, "00:00:00")

        except Exception:
            log.warning("EOS refresh failed", exc_info=True)

    async def _relay_heartbeat_loop(self) -> None:
        """Periodically send status to server via relay."""
        assert self._relay is not None
        interval = self.config.server.heartbeat_interval
        while self._running:
            await asyncio.sleep(interval)
            try:
                await self._refresh_eos_info()
                await self._relay.send_status(self.build_status())
            except Exception:
                log.debug("Relay heartbeat error", exc_info=True)

    async def _tribe_log_refresh_loop(self) -> None:
        """Periodically press Esc then L to close and reopen the tribe log.

        Also serves as a heartbeat: verifies the tribe log actually closed
        (after Esc) and reopened (after L). If either check fails, the game
        is likely frozen.

        Only runs while monitoring is active (not paused, tribe log visible)
        AND the screen is static (user is AFK). Skips if the user is actively
        playing to avoid disrupting gameplay.
        Waits a random 20–25 minutes between refreshes.
        """
        import random
        from tribewatch.capture import send_key

        # How long to wait for the capture cycle to update _log_header_visible
        check_wait = max(getattr(self.config.tribe_log, "interval", 3) * 2, 6)

        while self._running:
            delay = random.uniform(20 * 60, 25 * 60)
            await asyncio.sleep(delay)

            if not self._running:
                break
            if self._paused or not self._log_header_visible:
                continue

            # Skip if the screen is actively changing — user is playing
            if self._screen_still_since is None:
                log.debug("Tribe log refresh: skipped — screen is active (user playing)")
                continue

            window_title = self.config.general.window_title
            if not window_title:
                continue

            try:
                log.info("Tribe log refresh: pressing Esc to close tribe log")
                send_key(window_title, "escape")

                # Wait for capture cycle to detect the tribe log closed
                await asyncio.sleep(check_wait)
                if self._log_header_visible:
                    # Esc didn't close the log — game is likely frozen.
                    log.warning(
                        "Tribe log refresh: tribe log still visible after Esc — triggering auto-reconnect"
                    )
                    if self._auto_reconnect_cb:
                        self._auto_reconnect_cb()
                    continue

                log.info("Tribe log refresh: pressing L to reopen tribe log")
                send_key(window_title, "l")

                # Wait for capture cycle to detect the tribe log reopened
                await asyncio.sleep(check_wait)
                if not self._log_header_visible:
                    log.warning(
                        "Tribe log refresh: tribe log NOT visible after pressing L — triggering auto-reconnect"
                    )
                    if self._auto_reconnect_cb:
                        self._auto_reconnect_cb()
                else:
                    log.info("Tribe log refresh: tribe log reopened successfully")
            except Exception:
                log.debug("Tribe log refresh failed", exc_info=True)

    async def _idle_screen_monitor(self) -> None:
        """Detect idle screen and attempt to reopen tribe log / auto-reconnect.

        Triggers when BOTH conditions are true for 10 minutes:
        - Screen is static (pixel change < 2%) — user is AFK or disconnected
        - Tribe log is not visible — needs reopening

        This avoids being invasive: if the user is actively playing (screen
        changing), we don't press L even if the tribe log is closed.
        """
        IDLE_THRESHOLD = 10 * 60  # 10 minutes
        _last_log_min = 0  # track last logged minute to avoid spam

        while self._running:
            await asyncio.sleep(30)

            if self._paused or not self.capture.window_found:
                continue

            # If tribe log is visible or screen is active, reset everything
            if self._log_header_visible or self._screen_still_since is None:
                self._idle_recovery_attempted = False
                _last_log_min = 0
                continue

            idle_duration = time.time() - self._screen_still_since
            idle_whole_mins = int(idle_duration // 60)

            # Log progress at each even minute (2, 4, 6, 8)
            if (
                idle_whole_mins >= 2
                and idle_whole_mins % 2 == 0
                and idle_whole_mins != _last_log_min
                and not self._idle_recovery_attempted
                and idle_duration < IDLE_THRESHOLD
            ):
                _last_log_min = idle_whole_mins
                remaining = IDLE_THRESHOLD - idle_duration
                log.info(
                    "Screen idle for %dm, tribe log closed — recovery in %dm%ds",
                    idle_whole_mins,
                    int(remaining // 60), int(remaining % 60),
                )

            if idle_duration < IDLE_THRESHOLD or self._idle_recovery_attempted:
                continue

            self._idle_recovery_attempted = True
            log.warning(
                "Screen idle for %ds with tribe log closed — pressing L to reopen",
                int(idle_duration),
            )

            # Press L to try reopening the tribe log
            from tribewatch.capture import send_key
            send_key(self.config.general.window_title, "l")

            # Wait for capture cycle to detect tribe log
            check_wait = max(getattr(self.config.tribe_log, "interval", 3) * 3, 10)
            await asyncio.sleep(check_wait)

            if self._log_header_visible:
                log.info("Recovery successful — tribe log reopened")
                self._screen_still_since = None
                continue

            # Recovery failed — trigger auto-reconnect
            log.warning("Recovery failed — triggering auto-reconnect")
            if self._auto_reconnect_cb:
                self._auto_reconnect_cb()

    async def _capture_cycle(self) -> None:
        """Single capture → OCR → parse → dedup → dispatch cycle."""
        if self._paused:
            return

        img = self.capture.grab()
        if img is None:
            was_visible = self._log_header_visible
            self._log_header_visible = False
            if was_visible:
                log.info("Tribe log lost — window not found")
            else:
                log.debug("Capture returned None, skipping cycle")
            return

        self._last_capture_at = time.time()

        # Pixel change detection for idle screen monitoring
        from PIL import ImageChops, ImageStat
        thumb = img.copy()
        thumb.thumbnail((160, 90))
        if self._prev_thumb is not None and thumb.size == self._prev_thumb.size:
            diff = ImageChops.difference(thumb, self._prev_thumb)
            stat = ImageStat.Stat(diff)
            change_pct = sum(stat.mean) / 3 / 255 * 100  # 0-100%
            self._screen_change_pct = change_pct
            if change_pct < 2.0:  # screen is "still"
                if self._screen_still_since is None:
                    self._screen_still_since = time.time()
            else:
                self._screen_still_since = None  # screen is active
        self._prev_thumb = thumb

        # Save last capture for debugging
        _debug_dir = Path("debug")
        _debug_dir.mkdir(exist_ok=True)
        try:
            img.save(_debug_dir / "last_capture.png")
        except Exception:
            pass

        # Save preprocessed image for debugging
        from tribewatch.ocr_engine import _preprocess
        try:
            preprocessed = _preprocess(img, self.config.tribe_log.upscale)
            preprocessed.save(_debug_dir / "last_preprocessed.png")
        except Exception:
            pass

        # OCR the image first — we detect the LOG header from the text,
        # not from pixel heuristics (which false-positive on bright game scenes).
        ocr_start = time.monotonic()
        text = await recognize(
            img,
            engine=self.config.tribe_log.ocr_engine,
            upscale=self.config.tribe_log.upscale,
            tesseract_path=self.config.tribe_log.tesseract_path,
        )
        self._last_ocr_duration_ms = (time.monotonic() - ocr_start) * 1000

        # Save raw OCR text for debugging
        try:
            (_debug_dir / "last_ocr.txt").write_text(text, encoding="utf-8")
        except Exception:
            pass

        # Detect LOG header from OCR text — the tribe log always starts with "LOG"
        was_visible = self._log_header_visible
        log_header_visible = text.lstrip().upper().startswith("LOG")
        self._log_header_visible = log_header_visible

        if log_header_visible and not was_visible:
            log.info("Tribe log detected — monitoring active")
            self._log_visible_since = time.time()
            # Tribe log reappeared — trigger an immediate tribe window capture
            # to refresh member online/offline status without waiting for the
            # normal tribe polling interval.
            if self._tribe_capture:
                log.info("Triggering immediate tribe window capture")
                try:
                    await self._tribe_cycle()
                except Exception:
                    log.debug("Immediate tribe cycle failed", exc_info=True)
        elif not log_header_visible and was_visible:
            log.info("Tribe log lost — header no longer visible")
            self._log_visible_since = None

        if log_header_visible:
            self._last_log_seen_at = time.time()

        if not text.strip():
            log.debug("OCR returned empty text")
            return

        # Send raw text to debug webhook if configured (client-side only)
        if not self._relay and self.config.discord.debug_webhook:
            await self.dispatcher.send_debug(text)

        events = parse_events(text)
        if not events:
            return

        # Use resolve_event_action to filter ignored events
        pre_filter = len(events)
        events = [e for e in events if resolve_event_action(e, self.config)[0] != "ignore"]
        if not events:
            if pre_filter:
                log.info("Parsed %d events but all filtered as 'ignore'", pre_filter)
            return
        if len(events) < pre_filter:
            log.info("Parsed %d events, %d filtered as 'ignore'", pre_filter, pre_filter - len(events))

        # Dedup
        dedup = self._get_dedup()
        new_events = dedup.filter_new(events)
        if not new_events:
            log.info("Parsed %d events, all duplicates (high water: Day %d, %s)",
                     len(events), dedup._high_water[0], dedup._high_water[1])
            return

        # Don't dispatch events until tribe name is known (e.g. after server change)
        _tribe_info = getattr(self, "_tribe_info", None)
        _tribe_name = _tribe_info.tribe_name if _tribe_info else ""
        if not _tribe_name:
            log.info("Skipping %d events — tribe name not yet known", len(new_events))
            return

        log.info("Dispatching %d new events", len(new_events))
        event_dicts = [
            {
                "day": e.day,
                "time": e.time,
                "raw_text": e.raw_text,
                "event_type": e.event_type.value,
                "severity": e.severity.value,
                "timestamp": e.timestamp.timestamp(),
                "ping_status": None,
                "ping_detail": None,
                "tribe_name": _tribe_name,
            }
            for e in new_events
        ]

        # Store events (local SQLite or relay to server)
        ids = await self._store_events(event_dicts)
        for i, eid in enumerate(ids):
            event_dicts[i]["id"] = eid

        # Print events to console
        for event in new_events:
            self._print_event(event)

        # Discord dispatch is server-side only.  When connected via relay the
        # server's ClientHandler._dispatch_discord() handles webhooks — the
        # client must not duplicate that work.
        if not self._relay:
            await self._dispatch_discord(new_events, event_dicts)

        # Broadcast to browser WebSocket clients
        await self._broadcast_events(event_dicts)

        # Update stats
        self._events_today_count += len(new_events)
        self._total_events_count += len(new_events)

    async def _dispatch_discord(
        self, new_events: list, event_dicts: list[dict],
    ) -> None:
        """Client-side Discord dispatch (only used when NOT connected via relay)."""
        # Cache tribe members once per cycle for per-member Discord pings
        _cached_members: list[dict] | None = None
        if self._tribe_store:
            try:
                _cached_members = await self._tribe_store.get_all_members()
            except Exception:
                log.debug("Failed to fetch tribe members for ping lookup", exc_info=True)

        # Deferred escalation sends: collect all same-type events from the
        # batch before sending, so the summary includes events that arrive
        # after the threshold is first crossed.
        pending_escalations: dict[str, dict] = {}

        for i, event in enumerate(new_events):
            action, severity_override, ping, discord, ping_target, ping_member = resolve_event_action(event, self.config)
            if not discord:
                continue

            ev_type = event.event_type.value
            rule = self._find_alert_rule(ev_type, raw_text=event.raw_text)
            condition_label = rule.text_contains if rule else ""
            event_line = f"Day {event.day}, {event.time}: {event.raw_text}"
            esc, esc_texts = self._check_escalation(ev_type, rule, raw_text=event_line)

            if esc == "escalate":
                raw_esc = (rule.escalation_target if rule else "") or ping_target
                esc_target = resolve_mention(
                    raw_esc, self.config.discord.mentions,
                    self.config.discord.owner_discord_id,
                )
                pending_escalations[ev_type] = {
                    "texts": esc_texts,
                    "rule": rule,
                    "condition_label": condition_label,
                    "esc_target": esc_target,
                    "last_idx": i,
                }
                continue
            if esc == "suppress":
                if ev_type in pending_escalations:
                    pending_escalations[ev_type]["texts"].append(event_line)
                    pending_escalations[ev_type]["last_idx"] = i
                else:
                    log.debug("Escalation: suppressing Discord send for %s", ev_type)
                continue

            if action == "critical":
                extra_mentions: list[str] = []
                if ping_member and _cached_members:
                    member_name = extract_member_name(event.raw_text, event.event_type)
                    if member_name:
                        extra_mentions = self._find_member_discord_ids(member_name, _cached_members)

                result = await self.dispatcher.send_critical(
                    event, ping=ping, ping_target=ping_target,
                    extra_mentions=extra_mentions or None,
                    condition_label=condition_label,
                )
                event_dicts[i]["ping_status"] = result["ping_status"]
                event_dicts[i]["ping_detail"] = result["ping_detail"]
                log.info(
                    "Discord: %s → %s (%s)",
                    ev_type, result["ping_status"], result["ping_detail"],
                )

                if event_dicts[i].get("id"):
                    await self._update_ping(
                        event_dicts[i]["id"],
                        result["ping_status"],
                        result["ping_detail"],
                    )
            else:
                self.dispatcher.queue_batch(event, condition_label=condition_label)

        # --- Send deferred escalation summaries ---
        for ev_type, esc_info in pending_escalations.items():
            rule = esc_info["rule"]
            result = await self.dispatcher.send_escalation(
                event_type=ev_type,
                count=len(esc_info["texts"]),
                window_minutes=rule.escalation_window if rule else 10,
                ping_target=esc_info["esc_target"],
                event_texts=esc_info["texts"],
                condition_label=esc_info.get("condition_label", ""),
            )
            idx = esc_info["last_idx"]
            event_dicts[idx]["ping_status"] = result["ping_status"]
            event_dicts[idx]["ping_detail"] = result["ping_detail"]
            log.info(
                "Discord escalation: %s → %s (%s)",
                ev_type, result["ping_status"], result["ping_detail"],
            )
            if event_dicts[idx].get("id"):
                await self._update_ping(
                    event_dicts[idx]["id"],
                    result["ping_status"],
                    result["ping_detail"],
                )

    async def _parasaur_cycle(self) -> None:
        """Single parasaur notification capture → OCR → parse → session dispatch."""
        if self._paused or self._parasaur_capture is None:
            return
        tribe_info = getattr(self, "_tribe_info", None)
        if tribe_info is None or not tribe_info.tribe_name:
            return

        img = self._parasaur_capture.grab()
        if img is None:
            # Still need to check for grace/clear even with no image
            await self._check_parasaur_grace()
            await self._check_parasaur_clears()
            return

        # Save debug captures
        _debug_dir = Path("debug")
        _debug_dir.mkdir(exist_ok=True)
        try:
            img.save(_debug_dir / "parasaur_capture.png")
        except Exception:
            pass

        try:
            img.save(_debug_dir / "parasaur_preprocessed.png")
        except Exception:
            pass

        # Resolve per-window engine (fallback to global)
        parasaur_engine = self.config.parasaur.ocr_engine or self.config.tribe_log.ocr_engine

        text = await recognize(
            img,
            engine=parasaur_engine,
            upscale=self.config.tribe_log.upscale,
            tesseract_path=self.config.tribe_log.tesseract_path,
            retries=0,  # empty is normal — no retry spam
            preprocess=True,
        )

        # Save raw OCR text
        try:
            (_debug_dir / "parasaur_ocr.txt").write_text(text, encoding="utf-8")
        except Exception:
            pass

        # Process join/leave notifications from the same OCR text
        if text.strip():
            await self._process_join_leave_events(text)
            await self._process_server_join_events(text)

        # Parse events from OCR (may be empty — that's fine, we still check clears)
        events: list[TribeLogEvent] = []
        if text.strip():
            events = parse_parasaur_notification(text, self._parasaur_lookup)
            if not events and text.strip():
                log.debug("Parasaur OCR text but no detection parsed: %s", text.strip()[:100])

        # Filter ignored events
        events = [e for e in events if resolve_event_action(e, self.config)[0] != "ignore"]

        # --- Session-based dedup ---
        now_mono = time.monotonic()
        new_events: list[TribeLogEvent] = []
        grace_period = self.config.parasaur.grace_period

        for e in events:
            key = e.event_type.value  # "parasaur_detection" or "parasaur_babies"
            session = self._parasaur_sessions.get(key)
            # Track the most detailed text seen (one with name/level, not
            # the generic "Parasaur detected an enemy! [unknown]" fallback).
            is_detailed = "[unknown]" not in e.raw_text
            if session is None:
                # New session — create as pending (no alert yet)
                self._parasaur_sessions[key] = {
                    "start": now_mono,
                    "last_seen": now_mono,
                    "raw_text": e.raw_text,
                    "best_text": e.raw_text,
                    "state": "pending",
                }
            elif session["state"] == "pending":
                session["last_seen"] = now_mono
                session["raw_text"] = e.raw_text
                if is_detailed:
                    session["best_text"] = e.raw_text
                # Check if grace period has elapsed → promote to active
                age = now_mono - session["start"]
                if age >= grace_period:
                    session["state"] = "active"
                    # Use the best text seen during the session for the embed
                    best = session.get("best_text", e.raw_text)
                    sustained_text = best.replace(
                        "detected an enemy!", "is detecting an enemy!"
                    )
                    sustained_event = TribeLogEvent(
                        day=e.day,
                        time=e.time,
                        raw_text=sustained_text,
                        event_type=e.event_type,
                        severity=e.severity,
                        timestamp=e.timestamp,
                    )
                    new_events.append(sustained_event)
            else:
                # Active session — just update last_seen (suppress alert)
                session["last_seen"] = now_mono
                session["raw_text"] = e.raw_text
                if is_detailed:
                    session["best_text"] = e.raw_text

        # --- Store ALL detected events (even suppressed ones) ---
        _tribe_name = getattr(self, "_tribe_info", None)
        _tribe_name = _tribe_name.tribe_name if _tribe_name else ""
        if events:
            all_dicts = [
                {
                    "day": e.day,
                    "time": e.time,
                    "raw_text": e.raw_text,
                    "event_type": e.event_type.value,
                    "severity": e.severity.value,
                    "timestamp": e.timestamp.timestamp(),
                    "ping_status": None,
                    "ping_detail": None,
                    "tribe_name": _tribe_name,
                }
                for e in events
            ]
            await self._store_events(all_dicts)

        # --- Check for grace expiry (pending → brief) and session clears (active → cleared) ---
        await self._check_parasaur_grace()
        await self._check_parasaur_clears()

        # --- Dispatch new session starts (sustained) ---
        if not new_events:
            return

        log.info("Dispatching %d parasaur session start(s)", len(new_events))

        event_dicts = [
            {
                "day": e.day,
                "time": e.time,
                "raw_text": e.raw_text,
                "event_type": e.event_type.value,
                "severity": e.severity.value,
                "timestamp": e.timestamp.timestamp(),
                "ping_status": None,
                "ping_detail": None,
                "tribe_name": _tribe_name,
            }
            for e in new_events
        ]

        ids = await self._store_events(event_dicts)
        for i, eid in enumerate(ids):
            event_dicts[i]["id"] = eid

        for i, event in enumerate(new_events):
            self._print_event(event)

        # Discord dispatch is server-side only when connected via relay
        if not self._relay:
            for i, event in enumerate(new_events):
                ev_type = event.event_type.value
                alerts = self._parasaur_alert_settings(ev_type)

                if not alerts.discord or alerts.action == "ignore":
                    continue

                if alerts.action == "critical":
                    result = await self.dispatcher.send_critical(
                        event, ping=alerts.ping, ping_target=alerts.ping_target,
                    )
                    event_dicts[i]["ping_status"] = result["ping_status"]
                    event_dicts[i]["ping_detail"] = result["ping_detail"]
                    log.info(
                        "Discord: %s → %s (%s)",
                        ev_type, result["ping_status"], result["ping_detail"],
                    )
                    if event_dicts[i].get("id"):
                        await self._update_ping(
                            event_dicts[i]["id"],
                            result["ping_status"],
                            result["ping_detail"],
                        )
                else:
                    self.dispatcher.queue_batch(event)

        await self._broadcast_events(event_dicts)

        self._events_today_count += len(new_events)
        self._total_events_count += len(new_events)

    async def _check_parasaur_grace(self) -> None:
        """Check pending parasaur sessions whose notification has disappeared.

        Uses a short silence window (grace_period / 2) to confirm the
        on-screen notification is gone.  If the session never reached the
        grace_period promotion threshold, it fires a brief alert and deletes
        the session (no "all clear" for brief detections).

        Brief dispatch settings come from ``config.parasaur.player_alerts``
        or ``config.parasaur.babies_alerts``.
        """
        now_mono = time.monotonic()
        brief_silence = self.config.parasaur.grace_period / 2
        expired_keys: list[str] = []

        for key, session in self._parasaur_sessions.items():
            if session["state"] != "pending":
                continue
            silence = now_mono - session["last_seen"]
            if silence >= brief_silence:
                expired_keys.append(key)

                brief_type = _PARASAUR_BRIEF_MAP.get(key, key)
                raw_text = session.get("best_text", session["raw_text"])

                log.info("Parasaur brief detection expired: %s", key)

                # Build a synthetic event for the brief alert
                brief_event = TribeLogEvent(
                    day=0,
                    time="00:00:00",
                    raw_text=raw_text,
                    event_type=EventType(brief_type),
                    severity=Severity.WARNING,
                    timestamp=datetime.now(timezone.utc),
                )

                # Store event
                _tribe_name = getattr(self, "_tribe_info", None)
                _tribe_name = _tribe_name.tribe_name if _tribe_name else ""
                brief_dict = {
                    "day": 0,
                    "time": "00:00:00",
                    "raw_text": raw_text,
                    "event_type": brief_type,
                    "severity": "warning",
                    "timestamp": datetime.now(timezone.utc).timestamp(),
                    "ping_status": None,
                    "ping_detail": None,
                    "tribe_name": _tribe_name,
                }
                ids = await self._store_events([brief_dict])
                if ids:
                    brief_dict["id"] = ids[0]

                # Read brief dispatch settings from parasaur config.
                alerts = self._parasaur_alert_settings(key)
                action = alerts.brief_action or alerts.action
                ping = alerts.brief_ping
                discord = alerts.discord
                ping_target = alerts.ping_target

                send = action != "ignore" and discord
                if send and not self._relay:
                    if action == "critical":
                        result = await self.dispatcher.send_critical(
                            brief_event, ping=ping, ping_target=ping_target,
                        )
                    else:
                        self.dispatcher.queue_batch(brief_event)

                # Broadcast to browser WebSocket clients
                await self._broadcast_events([brief_dict])

        for key in expired_keys:
            del self._parasaur_sessions[key]

    async def _check_parasaur_clears(self) -> None:
        """Check active parasaur sessions and fire 'all clear' for expired ones."""
        now_mono = time.monotonic()
        clear_delay = self.config.parasaur.clear_delay
        cleared_keys: list[str] = []

        for key, session in self._parasaur_sessions.items():
            if session["state"] != "active":
                continue
            if now_mono - session["last_seen"] >= clear_delay:
                cleared_keys.append(key)
                duration = session["last_seen"] - session["start"]
                duration_str = _format_duration(duration)
                label = EVENT_TYPE_LABELS.get(key, key.replace("_", " ").title())
                raw_text = f"{label}: no longer detecting (duration: {duration_str})"

                log.info("Parasaur session clear: %s after %s", key, duration_str)

                # Store clear event
                _tribe_name = getattr(self, "_tribe_info", None)
                _tribe_name = _tribe_name.tribe_name if _tribe_name else ""
                clear_dict = {
                    "day": 0,
                    "time": "00:00:00",
                    "raw_text": raw_text,
                    "event_type": key,
                    "severity": "info",
                    "timestamp": datetime.now(timezone.utc).timestamp(),
                    "ping_status": None,
                    "ping_detail": None,
                    "tribe_name": _tribe_name,
                }
                ids = await self._store_events([clear_dict])
                if ids:
                    clear_dict["id"] = ids[0]

                # Post green embed to alert webhook (client-side only)
                if not self._relay and self.config.discord.alert_webhook:
                    await self.dispatcher._post_webhook(
                        self.config.discord.alert_webhook,
                        {"embeds": [{
                            "title": "\u2705 " + label,
                            "description": f"No longer detecting an enemy.\nSession duration: **{duration_str}**",
                            "color": 0x00CC00,
                        }]},
                    )

                # Broadcast to browser WebSocket clients
                await self._broadcast_events([clear_dict])

        for key in cleared_keys:
            del self._parasaur_sessions[key]

    async def _parasaur_loop(self) -> None:
        """Continuously poll parasaur notification region."""
        while self._running:
            try:
                await self._parasaur_cycle()
            except Exception:
                log.exception("Error in parasaur cycle")
            await asyncio.sleep(self.config.parasaur.interval)

    async def _handle_tribe_window_lost(self) -> None:
        """Handle the tribe window being unreadable or absent.

        Starts a grace timer on first failure. After ``offline_grace_seconds``
        expires, closes all open sessions and sets members offline.
        """
        self._tribe_window_visible = False
        now = time.time()

        if self._tribe_window_fail_since is None:
            self._tribe_window_fail_since = now
            return

        grace = self.config.tribe.offline_grace_seconds
        elapsed = now - self._tribe_window_fail_since
        if elapsed < grace:
            return

        if getattr(self, "_tribe_sessions_closed", False):
            return  # already sent close-sessions, don't spam

        # Grace expired — tell the server to close all sessions for the tribe
        tribe_name = ""
        if self._tribe_info is not None:
            tribe_name = self._tribe_info.tribe_name
        if not tribe_name:
            tribe_name = self.config.tribe.tribe_name

        if tribe_name and self._relay:
            log.info("Tribe window lost for %.0fs — requesting server close sessions", elapsed)
            await self._relay.send_tribe_window_lost(tribe_name)
            self._tribe_sessions_closed = True

    async def _process_join_leave_events(self, ocr_text: str) -> None:
        """Parse join/leave notifications from parasaur OCR and send to server.

        The client handles parsing and dedup. The server (client_handler)
        handles member resolution, status updates, event storage, and broadcast.
        """
        events = parse_join_leave_notifications(ocr_text)
        if not events:
            return

        now_mono = time.monotonic()

        # Dedup and collect events to send
        to_send: list[dict] = []
        for event in events:
            direction = "join" if event.is_join else "leave"
            dedup_key = f"{event.platform_id}:{direction}"
            last_seen = self._join_leave_seen.get(dedup_key)
            if last_seen is not None and (now_mono - last_seen) < 30:
                continue
            self._join_leave_seen[dedup_key] = now_mono
            to_send.append({
                "platform_id": event.platform_id,
                "is_join": event.is_join,
                "raw_text": event.raw_text,
            })
            log.info(
                "Join/leave detected: %s (%s)",
                "JOIN" if event.is_join else "LEAVE",
                event.platform_id,
            )

        if to_send and self._relay:
            await self._relay.send_join_leave(to_send)

        # Prune old dedup entries (older than 60s)
        cutoff = now_mono - 60
        self._join_leave_seen = {
            k: v for k, v in self._join_leave_seen.items() if v > cutoff
        }

    async def _process_server_join_events(self, ocr_text: str) -> None:
        """Parse non-tribemate join notifications and send to server via relay.

        Client-side dedup with a 30s window prevents duplicate sends from
        repeated OCR frames.
        """
        events = parse_server_join_notifications(ocr_text)
        if not events:
            return

        now_mono = time.monotonic()

        to_send: list[dict] = []
        for event in events:
            key = event.player_name.lower()
            last_seen = self._server_join_seen.get(key)
            if last_seen is not None and (now_mono - last_seen) < 30:
                continue
            self._server_join_seen[key] = now_mono
            to_send.append({
                "player_name": event.player_name,
                "raw_text": event.raw_text,
                "timestamp": event.timestamp.timestamp(),
            })
            log.info("Server join detected: %s", event.player_name)

        if to_send and self._relay:
            await self._relay.send_server_joins(to_send)

        # Prune old dedup entries (older than 60s)
        cutoff = now_mono - 60
        self._server_join_seen = {
            k: v for k, v in self._server_join_seen.items() if v > cutoff
        }

    async def _tribe_cycle(self) -> None:
        """Single tribe window capture → OCR → parse → transition detect → persist → relay."""
        if self._paused or self._tribe_capture is None:
            return

        img = self._tribe_capture.grab()
        if img is None:
            return

        # Resolve per-window engine (fallback to global)
        engine = self.config.tribe.ocr_engine or self.config.tribe_log.ocr_engine

        # Use tribe-window-specific preprocessing; apply binary threshold
        # when using Tesseract to suppress game-world bleed-through.
        from tribewatch.ocr_engine import preprocess_tribe_window
        threshold = 180 if engine == "tesseract" else 0
        preprocessed = preprocess_tribe_window(img, binary_threshold=threshold)

        text = await recognize(
            preprocessed,
            engine=engine,
            upscale=1,  # already upscaled by preprocess_tribe_window
            tesseract_path=self.config.tribe_log.tesseract_path,
            retries=0,
            preprocess=False,  # already preprocessed
        )

        # Save debug artifacts
        _debug_dir = Path("debug")
        _debug_dir.mkdir(exist_ok=True)
        try:
            img.save(_debug_dir / "tribe_capture.png")
        except Exception:
            pass
        try:
            preprocessed.save(_debug_dir / "tribe_preprocessed.png")
        except Exception:
            pass
        try:
            (_debug_dir / "tribe_ocr.txt").write_text(text, encoding="utf-8")
        except Exception:
            pass

        if not text.strip():
            await self._handle_tribe_window_lost()
            return

        info = parse_tribe_window(text)
        if info is None:
            await self._handle_tribe_window_lost()
            return

        self._tribe_window_visible = True
        self._tribe_window_last_ok = time.time()
        self._tribe_window_fail_since = None
        self._tribe_sessions_closed = False

        # Detect tribe name change — OCR'd name differs from configured name
        saved_tribe = self.config.tribe.tribe_name
        if saved_tribe and info.tribe_name:
            if not names_match(saved_tribe, info.tribe_name):
                cb = getattr(self, "_on_tribe_name_change_cb", None)
                if cb and not getattr(self, "_tribe_name_change_pending", False):
                    self._tribe_name_change_pending = True
                    cb(saved_tribe, info.tribe_name)

        # Override with confirmed tribe name to avoid OCR variations in DB/relay
        if self.config.tribe.tribe_name:
            from dataclasses import replace
            info = replace(info, tribe_name=self.config.tribe.tribe_name)

        prev_info = self._tribe_info
        self._tribe_info = info
        now = time.time()

        # --- Migrate dedup store when tribe name first becomes known ---
        prev_tribe = prev_info.tribe_name if prev_info else ""
        new_tribe = info.tribe_name or ""
        if prev_tribe == "" and new_tribe != "" and "" in self._dedup_stores:
            old_store = self._dedup_stores.pop("")
            old_sf = old_store.state_file
            new_sf = self._state_file_for(new_tribe)
            if new_sf and new_sf.exists():
                # Named state file already exists — load it instead of overwriting
                named_store = DedupStore(state_file=new_sf)
                self._dedup_stores[new_tribe] = named_store
            else:
                # No existing file — move the generic store to the named path
                old_store.state_file = new_sf
                old_store.save()
                self._dedup_stores[new_tribe] = old_store
            # Remove the old generic state file
            if old_sf and old_sf.exists() and old_sf != new_sf:
                try:
                    old_sf.unlink()
                except OSError:
                    pass
            log.info("Migrated dedup state from generic to tribe '%s' (%s)", new_tribe, new_sf)

        # --- Transition detection & persistence ---
        # When a relay is active (standalone or client mode), the server-side
        # ClientHandler handles upsert/snapshot/stale via the tribe_info message.
        # Only persist directly when running without a relay (pure client mode
        # without a server, which doesn't happen in practice but guards the path).
        if self._tribe_store and not self._relay:
            prev_members: dict[str, bool] = {}
            if prev_info and prev_info.members:
                for m in prev_info.members:
                    prev_members[m.name.lower()] = m.online

            # Track member_ids used this cycle so that two players with the
            # same display name (e.g. two "Human" entries) don't collapse
            # into a single DB record.
            seen_ids: set[int] = set()

            for i, member in enumerate(info.members):
                key = member.name.lower()
                was_online = prev_members.get(key)

                member_id = await self._tribe_store.upsert_member(
                    name=member.name,
                    tribe_name=info.tribe_name,
                    is_online=member.online,
                    timestamp=now,
                    platform_id=member.platform_id,
                    position=i,
                    tribe_group=member.group,
                    exclude_ids=seen_ids,
                )
                seen_ids.add(member_id)

                if was_online is not None:
                    if was_online and not member.online:
                        log.info("Tribe member offline: %s", member.name)
                    elif not was_online and member.online:
                        log.info("Tribe member online: %s", member.name)

            # Mark members no longer in the tribe window as stale
            await self._tribe_store.mark_unseen_stale(info.tribe_name, seen_ids)
            await self._tribe_store.purge_stale_members(
                info.tribe_name, max_age_seconds=3600,
            )

            await self._tribe_store.insert_snapshot(
                timestamp=now,
                tribe_name=info.tribe_name,
                members_online=info.members_online,
                members_total=info.members_total,
            )

        # --- Relay & broadcast ---
        changed = prev_info != info
        if changed:
            log.info(
                "Tribe: %s — %d/%d online",
                info.tribe_name,
                info.members_online,
                info.members_total,
            )
            if self._relay:
                from dataclasses import asdict
                await self._relay.send_tribe_info(asdict(info))
            if self._ws_manager:
                from dataclasses import asdict as _asdict
                await self._ws_manager.broadcast_tribe_update(_asdict(info))

    async def _tribe_loop(self) -> None:
        """Continuously poll tribe window region."""
        while self._running:
            try:
                await self._tribe_cycle()
            except Exception:
                log.exception("Error in tribe cycle")
            await asyncio.sleep(self.config.tribe.interval)

    def _is_tribe_window_ok(self) -> bool | None:
        """Whether the tribe window capture is working.

        Returns None if tribe capture is not configured, True if the
        tribe window is currently visible and was parsed recently,
        False otherwise.
        """
        if not getattr(self, "_tribe_capture", None):
            return None
        if not getattr(self, "_tribe_window_visible", False):
            return False
        last_ok = getattr(self, "_tribe_window_last_ok", None)
        if last_ok is None:
            return False
        return (time.time() - last_ok) < 300  # 5 minutes

    async def _batch_flush_loop(self) -> None:
        """Periodically flush batched non-critical events."""
        while self._running:
            await asyncio.sleep(self.config.discord.batch_interval)
            try:
                await self.dispatcher.flush_batch()
                await self.dispatcher.flush_retries()
            except Exception:
                log.exception("Batch flush error")

    async def run(self) -> None:
        """Run the main monitoring loop."""
        self._running = True
        self._started_at = time.time()
        if self.config.general.window_title:
            if self.capture._hwnd:
                log.info(
                    "Window capture: locked onto '%s' (hwnd=%s)",
                    self.config.general.window_title,
                    self.capture._hwnd,
                )
            else:
                log.warning(
                    "Window capture: '%s' not found — will retry each cycle",
                    self.config.general.window_title,
                )
        log.info(
            "TribeWatch started — capturing every %.1fs, bbox=%s%s",
            self.config.tribe_log.interval,
            self.config.tribe_log.bbox,
            f", window='{self.config.general.window_title}'" if self.config.general.window_title else "",
        )
        if self._parasaur_capture:
            log.info(
                "Parasaur detection enabled — polling every %.1fs, bbox=%s",
                self.config.parasaur.interval,
                self.config.parasaur.bbox,
            )
        if self._tribe_capture:
            log.info(
                "Tribe window capture enabled — polling every %.1fs, bbox=%s",
                self.config.tribe.interval,
                self.config.tribe.bbox,
            )

        flush_task = asyncio.create_task(self._batch_flush_loop())
        parasaur_task = None
        if self._parasaur_capture:
            parasaur_task = asyncio.create_task(self._parasaur_loop())
        tribe_task = None
        if self._tribe_capture:
            if self._tribe_store:
                await self._tribe_store.close_all_open_sessions(time.time())
            tribe_task = asyncio.create_task(self._tribe_loop())
        relay_heartbeat_task = None
        if self._relay:
            relay_heartbeat_task = asyncio.create_task(self._relay_heartbeat_loop())
        refresh_task = asyncio.create_task(self._tribe_log_refresh_loop())
        idle_monitor_task = asyncio.create_task(self._idle_screen_monitor())

        try:
            while self._running:
                try:
                    await self._capture_cycle()
                except Exception:
                    log.exception("Error in capture cycle")
                await asyncio.sleep(self.config.tribe_log.interval)
        finally:
            self._running = False
            flush_task.cancel()
            try:
                await flush_task
            except asyncio.CancelledError:
                pass
            if parasaur_task:
                parasaur_task.cancel()
                try:
                    await parasaur_task
                except asyncio.CancelledError:
                    pass
            if tribe_task:
                tribe_task.cancel()
                try:
                    await tribe_task
                except asyncio.CancelledError:
                    pass
            if relay_heartbeat_task:
                relay_heartbeat_task.cancel()
                try:
                    await relay_heartbeat_task
                except asyncio.CancelledError:
                    pass
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
            idle_monitor_task.cancel()
            try:
                await idle_monitor_task
            except asyncio.CancelledError:
                pass
            # Final flush and save
            await self.dispatcher.flush_batch()
            for store in self._dedup_stores.values():
                store.save()
            self.capture.close()
            if self._parasaur_capture:
                self._parasaur_capture.close()
            if self._tribe_capture:
                self._tribe_capture.close()
            await self.dispatcher.close()
            log.info("TribeWatch stopped")

    def stop(self) -> None:
        self._running = False
