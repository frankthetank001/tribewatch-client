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
            return rule.action, rule.severity_override or None, rule.ping, rule.discord, rule.ping_target, rule.ping_member

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
        # Discord dispatching is server-side only — the client sends raw
        # events over the relay websocket and the server handles webhooks.
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

        # Image-hash short-circuit for cycles where the captured region
        # rarely changes between cycles (tribe member panel, parasaur
        # notification panel, and the tribe-log capture during idle /
        # menu / static screens). On match we skip OCR + parse and
        # reuse the existing parsed state — ONNX inference is the
        # dominant cost in the OCR thread, so this is the biggest CPU
        # win available without changing engines.
        #
        # Safe for tribe-log capture because a new event scrolls the
        # existing entries down by one row, which changes the bytes →
        # hash misses → OCR runs → event captured. The dedup layer
        # handles the rare "duplicate text" case.
        self._tribe_img_hash: bytes | None = None
        self._parasaur_img_hash: bytes | None = None
        self._capture_img_hash: bytes | None = None

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
        self._active_play: bool = False  # True iff ARK foreground + recent input (OS gate)
        self._heartbeat_kick: asyncio.Event | None = None  # set to short-circuit heartbeat sleep
        self._idle_recovery_attempted: bool = False  # prevents repeated recovery attempts
        self._overlay = None  # StatusOverlay (set by __main__ if enabled)
        self._auto_reconnect_cb = None  # callback set by __main__.py
        self._on_character_death_cb = None  # callback set by __main__.py
        self._character_dead = False  # prevents repeated death alerts
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

    def _maybe_auto_reconnect(self, trigger: str = "unknown") -> None:
        """Trigger the auto-reconnect callback iff the feature is enabled.

        *trigger* identifies why the reconnect was initiated, e.g.
        ``"tribe_log_refresh_stuck"``, ``"tribe_log_reopen_failed"``,
        ``"idle_recovery_failed"``.

        Gated by config.reconnect.enabled — set to False to opt out of
        the automatic ARK relaunch / server rejoin behaviour. Manual
        reconnects via the dashboard still work either way.

        Always saves a debug screenshot of the current screen state to
        ``debug/auto_reconnect_<timestamp>.png`` before firing the
        callback (or skipping it). Useful for diagnosing why the
        recovery logic decided the tribe log was missing — sometimes
        the OCR misreads, sometimes the game window is genuinely
        elsewhere, and the screenshot is the only way to tell.
        """
        self._save_auto_reconnect_debug_screenshot()
        if not getattr(self.config.reconnect, "enabled", True):
            log.info("Auto-reconnect skipped: disabled in config")
            return
        if self._auto_reconnect_cb:
            self._auto_reconnect_cb(trigger)

    def _handle_character_death(self) -> None:
        """Called when the death screen is detected instead of auto-reconnect.

        Fires once per death — resets when the tribe log becomes visible
        again (meaning the player respawned).
        """
        if self._character_dead:
            return  # already alerted
        self._character_dead = True
        log.warning("Character death detected — skipping auto-reconnect")
        if self._on_character_death_cb:
            try:
                self._on_character_death_cb()
            except Exception:
                log.exception("Character death callback error")

    async def _is_esc_menu_open(self) -> bool:
        """Return True if ARK's in-game pause menu is currently visible.

        Captures a centred region of the ARK window and runs OCR looking
        for the unmistakable button labels — RESUME, SETTINGS, EXIT TO
        MAIN MENU. Used by the tribe-log refresh and idle recovery loops
        to detect the case where Esc opened the pause menu instead of
        just closing the tribe log: pressing L while the menu is up
        does nothing useful, and the recovery would otherwise escalate
        to a false-positive auto-reconnect.

        Returns False on any failure (no window, no OCR engine, no
        match) — the caller can safely treat False as "menu not detected".
        """
        try:
            import ctypes
            from tribewatch.capture import _IS_WIN32, _grab_window
            from tribewatch.ocr_engine import recognize

            if not _IS_WIN32:
                return False
            hwnd = getattr(self.capture, "_hwnd", None)
            if not hwnd:
                return False

            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            rect = (ctypes.c_long * 4)()
            user32.GetClientRect(hwnd, ctypes.byref(rect))
            width = rect[2]
            height = rect[3]
            if width <= 0 or height <= 0:
                return False

            # Centred bbox covering ~40% width × ~60% height — wide enough
            # to catch the button stack at any common resolution, narrow
            # enough to skip the surrounding game world clutter.
            bw = int(width * 0.40)
            bh = int(height * 0.60)
            x0 = (width - bw) // 2
            y0 = (height - bh) // 2
            bbox = [x0, y0, x0 + bw, y0 + bh]

            img = _grab_window(hwnd, bbox)
            if img is None:
                return False

            engine = getattr(self.config.tribe_log, "ocr_engine", "winrt") or "winrt"
            text = await recognize(img, engine=engine, retries=0, preprocess=False)
            if not text:
                return False
            upper = text.upper()
            # Any one of these keywords is enough — RESUME is the most
            # reliable, but check a few in case OCR mangles one.
            for needle in ("RESUME", "EXIT TO MAIN", "SETTINGS"):
                if needle in upper:
                    log.debug("Esc menu detected via OCR keyword %r", needle)
                    return True
            return False
        except Exception:
            log.debug("Esc menu OCR check failed", exc_info=True)
            return False

    async def _check_log_header_now(self) -> bool:
        """Run an immediate grab+OCR on the tribe-log bbox and return
        True iff the LOG header is currently visible.

        The periodic ``_capture_cycle`` updates ``_log_header_visible``
        only every ``tribe_log.interval`` seconds, so a fixed sleep in
        ``refresh_tribe_log`` can read a stale flag and decide L failed
        when it actually opened the log a moment ago. This helper does
        an inline check so the refresh loop can poll the *current*
        state without waiting for the bg cycle.

        Mirrors the OCR header detection in ``_capture_cycle`` (text
        starts with ``LOG``). Updates ``_log_header_visible`` and the
        related freshness fields when True so the rest of the app sees
        the same state — but does NOT dispatch events (the bg cycle
        owns parsing/dedup/dispatch).

        Returns False on any failure.
        """
        try:
            from tribewatch.ocr_engine import recognize

            img = self.capture.grab()
            if img is None:
                return False
            text = await recognize(
                img,
                engine=self.config.tribe_log.ocr_engine,
                upscale=self.config.tribe_log.upscale,
                tesseract_path=self.config.tribe_log.tesseract_path,
            )
            visible = bool(text and text.lstrip().upper().startswith("LOG"))
            if visible:
                self._log_header_visible = True
                if self._log_visible_since is None:
                    self._log_visible_since = time.time()
                self._last_log_seen_at = time.time()
            return visible
        except Exception:
            log.debug("Inline tribe log header check failed", exc_info=True)
            return False

    async def _is_death_screen(self) -> bool:
        """Return True if the ARK death/respawn screen is visible.

        Uses RapidOCR (same engine as reconnect sequence) to scan the
        full ARK window for "CREATE NEW SURVIVOR" or "RESPAWN" text.
        """
        try:
            from tribewatch.capture import _IS_WIN32, _grab_window
            from tribewatch.ocr_engine import _get_rapidocr_engine
            import numpy as np

            if not _IS_WIN32:
                return False
            hwnd = getattr(self.capture, "_hwnd", None)
            if not hwnd:
                return False

            img = _grab_window(hwnd, bbox=None)
            if img is None:
                return False

            engine = _get_rapidocr_engine()
            result, _ = engine(np.array(img))
            if result is None:
                return False

            for detection in result:
                _, text, _ = detection
                upper = text.upper()
                for needle in ("CREATE NEW SURVIVOR", "RESPAWN"):
                    if needle in upper:
                        log.warning("Death screen detected via OCR keyword %r in %r", needle, text)
                        return True
            return False
        except Exception:
            log.debug("Death screen OCR check failed", exc_info=True)
            return False

    def _save_auto_reconnect_debug_screenshot(self) -> None:
        """Capture the current ARK window and save it under ``debug/``.

        Best-effort — failures are logged at debug level and don't
        block the auto-reconnect.
        """
        try:
            from datetime import datetime
            from pathlib import Path

            img = self.capture.grab()
            if img is None:
                log.debug("Auto-reconnect debug screenshot: capture.grab() returned None")
                return

            debug_dir = Path("debug")
            debug_dir.mkdir(exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = debug_dir / f"auto_reconnect_{ts}.png"
            img.save(out)
            log.warning("Auto-reconnect debug screenshot saved: %s", out)

            # Prune old screenshots — keep at most the 20 most recent so the
            # directory doesn't grow forever.
            try:
                old = sorted(
                    debug_dir.glob("auto_reconnect_*.png"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                for stale_path in old[20:]:
                    stale_path.unlink(missing_ok=True)
            except Exception:
                log.debug("Failed to prune old auto_reconnect screenshots", exc_info=True)
        except Exception:
            log.debug("Failed to save auto-reconnect debug screenshot", exc_info=True)

    # -- Dynamic bbox preset switching -------------------------------------

    def _check_resolution_scaling(self) -> None:
        """Check game resolution and swap to preset bboxes if it changed.

        Uses ``capture.last_window_size`` as the cached fast path (set
        on every successful window grab), and falls back to the shared
        ``get_active_resolution`` helper otherwise — same source the
        startup path (``__main__._apply_resolution_preset``) uses, so
        the two never disagree about which preset is current.
        """
        game_res = getattr(self.capture, "last_window_size", None)
        if not game_res:
            from tribewatch.capture import get_active_resolution
            window_title = getattr(self.config.general, "window_title", "ArkAscended")
            game_res = get_active_resolution(window_title=window_title)
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

    def _get_dedup(self) -> DedupStore:
        """Return the DedupStore for the currently monitored tribe.

        Creates a new store on first access for each tribe, using a
        per-tribe state file so that high-water marks and hash buffers
        don't leak across tribes. Waits for server_id to be known before
        creating a persistent state file.
        """
        tribe_name = ""
        info = getattr(self, "_tribe_info", None)
        if info is not None:
            tribe_name = info.tribe_name or ""

        if tribe_name not in self._dedup_stores:
            server_id = getattr(self, "_server_id", "")
            if tribe_name and not server_id:
                # Server ID not yet known — use in-memory only (no state file)
                self._dedup_stores[tribe_name] = DedupStore(state_file=None)
            else:
                sf = self._state_file_for(tribe_name)
                self._dedup_stores[tribe_name] = DedupStore(state_file=sf)
        elif tribe_name:
            # If we have a store without a state file and server_id is now known,
            # upgrade it to use a persistent file
            store = self._dedup_stores[tribe_name]
            server_id = getattr(self, "_server_id", "")
            if store.state_file is None and server_id:
                sf = self._state_file_for(tribe_name)
                store.state_file = sf
                store.save()
                log.info("Dedup store upgraded to persistent: %s", sf)

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
            "tribe_log_tunables": {
                "active_play_threshold": getattr(
                    self.config.tribe_log, "active_play_threshold", 2.0,
                ),
                "active_play_peek_interval": getattr(
                    self.config.tribe_log, "active_play_peek_interval", 8.0,
                ),
            },
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
                    log.info("Server ID changed: %s -> %s (%s)", old_id, new_id, new_name)
                    cb = getattr(self, "_on_server_change_cb", None)
                    if cb:
                        cb(old_id, getattr(self, "_server_name", ""), new_id, new_name)
                    # Don't update _server_id/_server_name here — the
                    # change handler does it after the user confirms.
                else:
                    if not old_id:
                        log.info("Server ID detected: %s (%s)", new_id, new_name)
                    self._server_id = new_id
                    self._server_name = new_name
                    # If we just returned to the monitored server after
                    # declining a change to a different one, clear the
                    # pause and the decline memo so monitoring resumes
                    # without requiring /resume.
                    if getattr(self, "_declined_server_id", "") and getattr(self, "_paused", False):
                        log.info(
                            "Returned to monitored server %s — clearing decline and resuming",
                            new_id,
                        )
                        self._declined_server_id = ""
                        self._paused = False
        except Exception:
            log.debug("Server info lookup failed", exc_info=True)
        status["server_id"] = getattr(self, "_server_id", "")
        status["server_name"] = getattr(self, "_server_name", "")

        # Client identity (for multi-client view on dashboard)
        from tribewatch import __version__ as _client_version
        status["client_version"] = _client_version
        game_res = getattr(self, "_last_game_resolution", None)
        status["resolution"] = f"{game_res[0]}x{game_res[1]}" if game_res else ""

        # Surface tunable settings so the dashboard tile can show their
        # current state without an extra API call.
        status["auto_reconnect"] = bool(getattr(self.config.reconnect, "enabled", True))
        rcfg = getattr(self.config, "reconnect", None)
        status["reconnect_tunables"] = {
            "speed": getattr(rcfg, "speed", 1.0) if rcfg else 1.0,
            "launch_timeout": getattr(rcfg, "launch_timeout", 180.0) if rcfg else 180.0,
            "title_timeout": getattr(rcfg, "title_timeout", 180.0) if rcfg else 180.0,
            "load_timeout": getattr(rcfg, "load_timeout", 180.0) if rcfg else 180.0,
            "browser_timeout": getattr(rcfg, "browser_timeout", 60.0) if rcfg else 60.0,
            "tribe_log_delay": getattr(rcfg, "tribe_log_delay", 30.0) if rcfg else 30.0,
            "load_stable_secs": getattr(rcfg, "load_stable_secs", 10.0) if rcfg else 10.0,
        }

        # Dynamic bbox scaling — check if game resolution changed
        try:
            self._check_resolution_scaling()
        except Exception:
            pass

        # Idle screen / active play detection
        status["screen_still_since"] = getattr(self, "_screen_still_since", None)
        status["screen_change_pct"] = getattr(self, "_screen_change_pct", 100.0)
        status["active_play"] = getattr(self, "_active_play", False)
        status["character_dead"] = getattr(self, "_character_dead", False)

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

    def _update_overlay(self) -> None:
        """Update the overlay with current client status."""
        overlay = getattr(self, "_overlay", None)
        if not overlay:
            return

        if self._character_dead:
            overlay.update("dead", "Dead \u2022 respawn required")
            return

        if self._paused:
            overlay.update("paused", "Paused")
            return

        if not self.capture.window_found:
            overlay.update("offline", "No game window")
            return

        if self._active_play:
            overlay.update("playing", "Playing")
            return

        if self._log_header_visible:
            overlay.update("monitoring", "Monitoring")
            return

        # Screen is still but tribe log not visible — idle/recovery countdown
        still_since = self._screen_still_since
        if still_since is not None:
            idle_secs = time.time() - still_since
            threshold = self.config.alerts.idle_alert_minutes * 60
            remaining = threshold - idle_secs
            if remaining > 0:
                mins = int(remaining // 60)
                secs = int(remaining % 60)
                overlay.update("idle", f"Idle \u2022 opening log in {mins}m{secs:02d}s")
            else:
                overlay.update("recovery", "Recovering...")
            return

        overlay.update("idle", "Idle")

    _EOS_BASE_INTERVAL = 300       # 5 min normal
    _EOS_MAX_BACKOFF = 3600        # 1 hour max between retries

    async def _refresh_eos_info(self) -> None:
        """Query EOS for server info with exponential backoff on failure."""
        server_name = getattr(self, "_server_name", "")
        if not server_name:
            return

        now = time.monotonic()
        fail_count = getattr(self, "_eos_fail_count", 0)
        # Exponential backoff: 5min, 10min, 20min, 40min, capped at 1hr
        interval = min(
            self._EOS_BASE_INTERVAL * (2 ** fail_count),
            self._EOS_MAX_BACKOFF,
        )
        last_query = getattr(self, "_eos_last_query", 0)
        if now - last_query < interval:
            return

        self._eos_last_query = now

        info = None

        # Try BattleMetrics first (more reliable, no auth)
        try:
            from tribewatch.eos import BattleMetricsClient, extract_battlemetrics_info

            if getattr(self, "_bm_client", None) is None:
                self._bm_client = BattleMetricsClient()

            bm_server = await self._bm_client.get_server_by_name(server_name)
            if bm_server is not None:
                info = extract_battlemetrics_info(bm_server)
        except Exception:
            log.debug("BattleMetrics query failed", exc_info=True)

        # Fallback to EOS
        if info is None:
            try:
                from tribewatch.eos import AsyncEOSClient, extract_server_info

                if getattr(self, "_eos_client", None) is None:
                    self._eos_client = AsyncEOSClient()

                session = await self._eos_client.get_server_by_name(server_name)
                if session is not None:
                    info = extract_server_info(session)
                    info["source"] = "eos"
            except Exception:
                log.debug("EOS query failed", exc_info=True)

        if info is None:
            self._eos_fail_count = getattr(self, "_eos_fail_count", 0) + 1
            next_interval = min(self._EOS_BASE_INTERVAL * (2 ** self._eos_fail_count), self._EOS_MAX_BACKOFF)
            if self._eos_fail_count <= 3:
                log.warning("Server info refresh failed (retry #%d in %ds)", self._eos_fail_count, next_interval)
            else:
                log.debug("Server info refresh still failing (retry #%d in %ds)", self._eos_fail_count, next_interval)
            return

        self._eos_info = info
        self._eos_fail_count = 0
        source = info.get("source", "?")

        log.info(
            "Server info [%s]: %s — %d/%d players, map=%s, Day %s",
            source,
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

    async def _relay_heartbeat_loop(self) -> None:
        """Periodically send status to server via relay.

        Also short-circuits when ``self._heartbeat_kick`` is set so callers
        can request an immediate heartbeat after a state transition (e.g.
        active_play start/stop, tribe log gained/lost) instead of waiting
        out the full interval.
        """
        assert self._relay is not None
        interval = self.config.server.heartbeat_interval
        if self._heartbeat_kick is None:
            self._heartbeat_kick = asyncio.Event()
        while self._running:
            try:
                await asyncio.wait_for(self._heartbeat_kick.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            self._heartbeat_kick.clear()
            try:
                await self._refresh_eos_info()
                await self._relay.send_status(self.build_status())
            except Exception:
                log.debug("Relay heartbeat error", exc_info=True)

    def _kick_heartbeat(self) -> None:
        """Request an immediate status heartbeat (no-op if loop not running)."""
        ev = self._heartbeat_kick
        if ev is not None:
            ev.set()

    async def refresh_tribe_log(self, *, manual: bool = False) -> bool:
        """Press Esc then L to close and reopen the tribe log.

        Verifies the tribe log actually closed (after Esc) and reopened
        (after L). Returns True on success, False if anything along the
        way looked wrong (in which case auto-reconnect may be triggered).

        ``manual=True`` relaxes the safety gates so a server-side
        ``refresh_log`` control command can force a refresh even when
        the tribe log isn't currently visible or the user is mid-play.
        The periodic loop and idle-monitor still pass ``manual=False``.
        """
        from tribewatch.capture import send_key, focus_window, is_window_foreground

        check_wait = max(getattr(self.config.tribe_log, "interval", 3) * 2, 6)
        log.info(
            "Tribe log refresh: ENTER (manual=%s, paused=%s, log_visible=%s, active_play=%s, check_wait=%s)",
            manual, self._paused, self._log_header_visible, self._active_play, check_wait,
        )

        if not manual:
            if self._paused or not self._log_header_visible:
                log.info(
                    "Tribe log refresh: gated out (paused=%s, log_visible=%s)",
                    self._paused, self._log_header_visible,
                )
                return False
            if self._active_play:
                log.info("Tribe log refresh: gated out (active_play)")
                return False

        window_title = self.config.general.window_title
        if not window_title:
            log.info("Tribe log refresh: no window_title configured, skipping")
            return False

        # PostMessage (used by send_key) only registers in-game when the
        # window has input focus. If ARK isn't foreground, the Esc/L
        # keys are silently ignored and the refresh appears to succeed
        # while doing nothing. Focus the window first — if it's already
        # foreground this is a no-op.
        if not is_window_foreground(window_title):
            log.info("Tribe log refresh: ARK not foreground, focusing window")
            focus_window(window_title)
            await asyncio.sleep(0.3)

        try:
            log.info("Tribe log refresh: pressing Esc (manual=%s)", manual)
            sent = send_key(window_title, "escape")
            log.info("Tribe log refresh: Esc send_key returned %s; waiting %ss", sent, check_wait)
            await asyncio.sleep(check_wait)
            log.info(
                "Tribe log refresh: post-Esc state log_visible=%s",
                self._log_header_visible,
            )

            if self._log_header_visible:
                log.warning(
                    "Tribe log refresh: tribe log still visible after Esc — triggering auto-reconnect"
                )
                if await self._is_death_screen():
                    self._handle_character_death()
                    return False
                self._maybe_auto_reconnect("tribe_log_refresh_stuck")
                return False

            # Esc may have opened the pause menu (either because the log
            # wasn't open and Esc went to the menu, or because the menu
            # was already up). Either way, dismiss it before pressing L.
            if await self._is_esc_menu_open():
                log.info("Tribe log refresh: pause menu detected, dismissing")
                send_key(window_title, "escape")
                await asyncio.sleep(check_wait)

            # Active polling instead of a single fixed-wait check.
            #
            # The previous logic pressed L, slept 6s, then read
            # ``_log_header_visible`` (which the bg OCR cycle updates
            # every ``interval`` seconds). That race produced two
            # failure modes:
            #   1. The flag was stale because the most recent bg cycle
            #      captured before ARK finished rendering the log, so
            #      we falsely escalated.
            #   2. L is a toggle in ARK — pressing it again on attempt
            #      2 closed the just-opened log, so the system fought
            #      itself for 3 attempts and then auto-reconnected.
            #
            # Now: press L *once* per attempt and poll
            # ``_check_log_header_now`` (inline grab+OCR) every ~1s
            # for up to ``poll_budget`` seconds. As soon as the header
            # is visible, return success — typically within 2-3s.
            # Only retry L (max 1 extra time) if the budget expires
            # AND a pause menu is present (genuine missed keystroke).
            _MAX_L_PRESSES = 2
            _POLL_INTERVAL = 1.0
            poll_budget = max(check_wait * 2, 12.0)
            for press in range(1, _MAX_L_PRESSES + 1):
                log.info(
                    "Tribe log refresh: pressing L to reopen tribe log "
                    "(press %d/%d, polling up to %.0fs)",
                    press, _MAX_L_PRESSES, poll_budget,
                )
                send_key(window_title, "l")
                # Tiny initial settle so the very first poll doesn't
                # OCR a frame mid-fade-in.
                await asyncio.sleep(0.5)
                deadline = time.monotonic() + poll_budget
                while time.monotonic() < deadline:
                    if await self._check_log_header_now():
                        elapsed = poll_budget - (deadline - time.monotonic())
                        log.info(
                            "Tribe log refresh: tribe log reopened "
                            "successfully (after %.1fs)", elapsed,
                        )
                        return True
                    await asyncio.sleep(_POLL_INTERVAL)
                # Budget exhausted — only retry L if the pause menu is
                # the reason (i.e. the keystroke truly didn't reach
                # the game). Otherwise another L would just toggle the
                # log closed.
                if press < _MAX_L_PRESSES and await self._is_esc_menu_open():
                    log.info(
                        "Tribe log refresh: pause menu detected after "
                        "L poll, dismissing and retrying"
                    )
                    send_key(window_title, "escape")
                    await asyncio.sleep(1.0)
                else:
                    break

            log.warning(
                "Tribe log refresh: tribe log NOT visible after %d L press(es) "
                "with %.0fs polling — triggering auto-reconnect",
                press, poll_budget,
            )
            if await self._is_death_screen():
                self._handle_character_death()
                return False
            self._maybe_auto_reconnect("tribe_log_reopen_failed")
            return False
        except Exception:
            log.debug("Tribe log refresh failed", exc_info=True)
            return False

    async def _tribe_log_refresh_loop(self) -> None:
        """Periodically force a tribe-log refresh while idle.

        Waits 20–25 minutes between attempts and only fires when the log
        is visible, the user isn't actively playing, and the client isn't
        paused. Delegates the actual key sequence + verification to
        :meth:`refresh_tribe_log`.
        """
        import random

        while self._running:
            delay = random.uniform(20 * 60, 25 * 60)
            await asyncio.sleep(delay)
            if not self._running:
                break
            await self.refresh_tribe_log(manual=False)

    async def _idle_screen_monitor(self) -> None:
        """Detect idle screen and attempt to reopen tribe log / auto-reconnect.

        Triggers when BOTH conditions are true for 10 minutes:
        - Screen is static (pixel change < 2%) — user is AFK or disconnected
        - Tribe log is not visible — needs reopening

        This avoids being invasive: if the user is actively playing (screen
        changing), we don't press L even if the tribe log is closed.
        """
        # Use idle_alert_minutes from config so the overlay countdown and
        # the actual recovery threshold stay in sync.
        IDLE_THRESHOLD = self.config.alerts.idle_alert_minutes * 60
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

            # Check for death screen before attempting any recovery
            if await self._is_death_screen():
                self._handle_character_death()
                self._idle_recovery_attempted = True
                continue

            self._idle_recovery_attempted = True

            window_title = self.config.general.window_title
            from tribewatch.capture import send_key, focus_window, is_window_foreground

            log.warning(
                "Screen idle for %ds with tribe log closed — attempting recovery",
                int(idle_duration),
            )

            # Focus ARK so the PostMessage keystrokes actually register.
            if window_title and not is_window_foreground(window_title):
                log.info("Idle recovery: ARK not foreground, focusing window")
                focus_window(window_title)
                await asyncio.sleep(0.3)

            check_wait = max(getattr(self.config.tribe_log, "interval", 3) * 2, 6)
            _L_ATTEMPTS = 3
            recovered = False

            for attempt in range(1, _L_ATTEMPTS + 1):
                # Dismiss esc menu if open before pressing L
                if await self._is_esc_menu_open():
                    log.info("Idle recovery: pause menu detected, dismissing before L (attempt %d/%d)", attempt, _L_ATTEMPTS)
                    send_key(window_title, "escape")
                    await asyncio.sleep(2)

                log.info("Idle recovery: pressing L to reopen tribe log (attempt %d/%d)", attempt, _L_ATTEMPTS)
                send_key(window_title, "l")
                await asyncio.sleep(check_wait)

                if self._log_header_visible:
                    log.info("Recovery successful — tribe log reopened (attempt %d/%d)", attempt, _L_ATTEMPTS)
                    self._screen_still_since = None
                    recovered = True
                    break

            if recovered:
                continue

            # All attempts failed — check for death screen before reconnecting
            if await self._is_death_screen():
                self._handle_character_death()
                continue
            log.warning("Recovery failed after %d L attempts — triggering auto-reconnect", _L_ATTEMPTS)
            self._maybe_auto_reconnect("idle_recovery_failed")

    async def _capture_cycle(self) -> None:
        """Single capture → OCR → parse → dedup → dispatch cycle."""
        if self._paused:
            return

        # OS-signal active-play gate. If ARK is the foreground window and
        # the user has provided input recently, they're actively playing —
        # skip ALL work (no PrintWindow, no thumbnail diff, no OCR) to
        # avoid contributing any GPU/CPU contention to the game's frame
        # pipeline. The cycle resumes immediately the moment the user
        # tabs away or stops providing input for the configured idle
        # threshold (default ~5s).
        from tribewatch.capture import is_actively_playing
        idle_threshold_ms = int(
            (getattr(self.config.tribe_log, "active_play_idle_seconds", 5.0) or 5.0)
            * 1000
        )
        playing = is_actively_playing(
            self.config.general.window_title, idle_threshold_ms=idle_threshold_ms,
        )
        was_active = self._active_play
        if playing:
            if not was_active:
                self._active_play = True
                log.info(
                    "Active play detected (ARK foreground + recent input) — "
                    "pausing tribe log & parasaur OCR",
                )
                self._log_header_visible = False
                self._log_visible_since = None
                if self._character_dead:
                    log.info("Character death state cleared — active play detected")
                    self._character_dead = False
                self._kick_heartbeat()
            return  # zero further work this cycle
        if was_active:
            # User just stopped playing (tabbed away or went idle). The
            # rest of the cycle below will run normally and resume
            # tribe-log monitoring.
            self._active_play = False
            log.info("Resumed monitoring — ARK no longer foreground or input idle")
            self._kick_heartbeat()

        img = self.capture.grab()
        if img is None:
            was_visible = self._log_header_visible
            was_active = self._active_play
            self._log_header_visible = False
            # Reset motion / active-play state so the server's presence
            # calculation correctly transitions to INACTIVE when the
            # game window disappears.  Without this, a stale
            # active_play=True from the previous session persists across
            # heartbeats and the dashboard continues to show "playing"
            # long after ARK has exited.
            self._active_play = False
            self._screen_still_since = None
            if was_visible:
                log.info("Tribe log lost — window not found")
            elif was_active:
                log.info("Window not found — resetting active_play")
            else:
                log.debug("Capture returned None, skipping cycle")
            if was_active:
                self._kick_heartbeat()
            return

        self._last_capture_at = time.time()

        # Image-hash short-circuit for OCR. If the bbox bytes are
        # identical to the previous successful cycle, the OCR result
        # would be the same — skip all three OCR call sites below
        # (cooldown peek, active-play peek, main OCR) and return.
        # Active-play hysteresis (motion-based, uses the thumbnail
        # diff) still runs above this — gameplay motion is detected
        # independently of OCR.
        try:
            import hashlib
            img_hash = hashlib.blake2b(img.tobytes(), digest_size=8).digest()
        except Exception:
            img_hash = None
        hash_changed = (img_hash is None) or (img_hash != self._capture_img_hash)
        # Cache the new hash now (regardless of which OCR path runs
        # below) so the next cycle compares against the current frame
        # instead of an older one. OCR is deterministic on identical
        # input, so caching before OCR is safe even if the OCR call
        # later fails or returns garbage.
        if img_hash is not None:
            self._capture_img_hash = img_hash

        # Pixel change detection — kept for the dashboard status and for
        # _idle_screen_monitor's "screen has been static for X minutes"
        # recovery path. No longer drives active_play (the OS-signal
        # gate at the top of this function does).
        from PIL import ImageChops, ImageStat
        thumb = img.copy()
        thumb.thumbnail((160, 90))
        active_threshold = float(
            getattr(self.config.tribe_log, "active_play_threshold", 2.0) or 2.0
        )
        if self._prev_thumb is not None and thumb.size == self._prev_thumb.size:
            diff = ImageChops.difference(thumb, self._prev_thumb)
            stat = ImageStat.Stat(diff)
            change_pct = sum(stat.mean) / 3 / 255 * 100  # 0-100%
            self._screen_change_pct = change_pct
            if change_pct < active_threshold:  # screen is "still"
                if self._screen_still_since is None:
                    self._screen_still_since = time.time()
            else:
                self._screen_still_since = None
        self._prev_thumb = thumb

        # Once-per-minute diagnostic log
        _now = time.monotonic()
        _last = getattr(self, "_active_play_diag_last", 0.0)
        if _now - _last >= 60.0:
            self._active_play_diag_last = _now
            log.info(
                "screen change %.2f%% (active_play=%s, OS-gated)",
                self._screen_change_pct, self._active_play,
            )

        # _active_play is now driven solely by the OS-signal gate at
        # the top of this function — if we got here, the user isn't
        # actively playing, so we run the full monitoring cycle below.
        # The thumbnail diff above still feeds _screen_change_pct and
        # _screen_still_since for the dashboard status and the
        # _idle_screen_monitor's "screen has been static for X minutes"
        # recovery logic.
        self._update_overlay()

        # Save last capture for debugging — only when DEBUG logging is
        # enabled. PNG encoding was ~28% of MainThread work in profiles
        # (one full encode per cycle, every cycle), unconditionally.
        debug_captures = log.isEnabledFor(logging.DEBUG)
        _debug_dir = Path("debug")
        if debug_captures:
            _debug_dir.mkdir(exist_ok=True)
            try:
                img.save(_debug_dir / "last_capture.png")
            except Exception:
                pass

        # Save preprocessed image for debugging
        from tribewatch.ocr_engine import _preprocess
        if debug_captures:
            try:
                preprocessed = _preprocess(img, self.config.tribe_log.upscale)
                preprocessed.save(_debug_dir / "last_preprocessed.png")
            except Exception:
                pass

        # OCR the image first — we detect the LOG header from the text,
        # not from pixel heuristics (which false-positive on bright game scenes).
        # Skip when the bbox is byte-identical to the previous successful
        # cycle: the OCR result would be identical, no new events, no
        # change in log_header_visible. ONNX inference is the dominant
        # CPU cost in the worker thread, so this is the biggest
        # monitoring-mode win.
        if not hash_changed:
            return
        ocr_start = time.monotonic()
        text = await recognize(
            img,
            engine=self.config.tribe_log.ocr_engine,
            upscale=self.config.tribe_log.upscale,
            tesseract_path=self.config.tribe_log.tesseract_path,
        )
        self._last_ocr_duration_ms = (time.monotonic() - ocr_start) * 1000

        # Save raw OCR text for debugging
        if debug_captures:
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
            if self._character_dead:
                log.info("Character death state cleared — tribe log visible again")
                self._character_dead = False
            self._kick_heartbeat()
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
            self._kick_heartbeat()

        if log_header_visible:
            self._last_log_seen_at = time.time()

        if not text.strip():
            log.debug("OCR returned empty text")
            return

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

        # Don't process events until tribe name and server ID are known
        _tribe_info = getattr(self, "_tribe_info", None)
        _tribe_name = _tribe_info.tribe_name if _tribe_info else ""
        if not _tribe_name:
            log.info("Skipping %d events — tribe name not yet known", len(events))
            return
        if not getattr(self, "_server_id", ""):
            log.info("Skipping %d events — server ID not yet known", len(events))
            return

        # Dedup
        dedup = self._get_dedup()
        new_events = dedup.filter_new(events)
        if not new_events:
            log.debug("Parsed %d events, all duplicates (high water: Day %d, %s)",
                      len(events), dedup._high_water[0], dedup._high_water[1])
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

        # Broadcast to browser WebSocket clients
        await self._broadcast_events(event_dicts)

        # Update stats
        self._events_today_count += len(new_events)
        self._total_events_count += len(new_events)

    async def _parasaur_cycle(self) -> None:
        """Single parasaur notification capture → OCR → parse → session dispatch."""
        if self._paused or self._parasaur_capture is None:
            return
        # Skip capture/OCR during active play — still check session timers below
        if self._active_play:
            await self._check_parasaur_grace()
            await self._check_parasaur_clears()
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

        # Image-hash short-circuit. The parasaur notification panel is
        # blank most of the time; OCR-ing identical blank frames is the
        # bulk of the work this loop does. On a hit, skip the OCR /
        # parse / event-dispatch block but still run the grace + clear
        # checks (those are time-based, not image-based).
        try:
            import hashlib
            img_hash = hashlib.blake2b(img.tobytes(), digest_size=8).digest()
        except Exception:
            img_hash = None
        if img_hash is not None and img_hash == self._parasaur_img_hash:
            await self._check_parasaur_grace()
            await self._check_parasaur_clears()
            return
        self._parasaur_img_hash = img_hash

        # Save debug captures (only when DEBUG logging is on)
        if log.isEnabledFor(logging.DEBUG):
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
        if log.isEnabledFor(logging.DEBUG):
            try:
                _debug_dir = Path("debug")
                _debug_dir.mkdir(exist_ok=True)
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

        if to_send and self._relay and getattr(self, "_server_id", ""):
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
        # Skip while the user is actively playing — member online/offline
        # status isn't time-critical mid-game, and PrintWindow + OCR
        # contend with the GPU. Mirrors the gate parasaur already has.
        if self._active_play:
            return

        img = self._tribe_capture.grab()
        if img is None:
            return

        # Image-hash short-circuit. The tribe member panel only changes
        # when someone goes online/offline — between such transitions
        # the bytes are identical and OCR would produce the same parse.
        # Skip the entire OCR/parse/dispatch path on a hit (the previous
        # cycle already updated _tribe_info accordingly).
        try:
            import hashlib
            img_hash = hashlib.blake2b(img.tobytes(), digest_size=8).digest()
        except Exception:
            img_hash = None
        if img_hash is not None and img_hash == self._tribe_img_hash:
            return
        self._tribe_img_hash = img_hash

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

        # Save debug artifacts (only when DEBUG logging is on — these
        # PNG encodes were a measurable share of MainThread work).
        if log.isEnabledFor(logging.DEBUG):
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

        # Detect tribe name change:
        # - Always prompt once per app startup if the OCR'd name differs
        # - After that, only re-prompt if the server_id changes (new server)
        saved_tribe = self.config.tribe.tribe_name
        log.debug(
            "Tribe name check: saved=%r detected=%r startup_checked=%s",
            saved_tribe, info.tribe_name,
            getattr(self, "_tribe_name_startup_checked", False),
        )
        if saved_tribe and info.tribe_name:
            matches = names_match(saved_tribe, info.tribe_name)
            log.debug("Tribe name names_match result: %s", matches)
            if not matches:
                startup_check_done = getattr(self, "_tribe_name_startup_checked", False)
                last_prompt_server = getattr(self, "_tribe_name_last_prompt_server", "")
                current_server = getattr(self, "_server_id", "")
                is_new_server_since_prompt = (
                    last_prompt_server and current_server
                    and last_prompt_server != current_server
                )
                should_prompt = (not startup_check_done) or is_new_server_since_prompt
                log.debug(
                    "Tribe name mismatch — should_prompt=%s (startup_done=%s, new_server=%s)",
                    should_prompt, startup_check_done, is_new_server_since_prompt,
                )
                if not should_prompt:
                    pass
                else:
                    cb = getattr(self, "_on_tribe_name_change_cb", None)
                    pending = getattr(self, "_tribe_name_change_pending", False)
                    log.debug("Tribe name prompt: cb=%s pending=%s", bool(cb), pending)
                    if cb and not pending:
                        self._tribe_name_change_pending = True
                        self._tribe_name_startup_checked = True
                        self._tribe_name_last_prompt_server = current_server
                        cb(saved_tribe, info.tribe_name)
            else:
                # Names match — mark startup check as done
                self._tribe_name_startup_checked = True

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
            if self._relay and getattr(self, "_server_id", ""):
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
        cal_res = getattr(self.config.general, "calibration_resolution", None)
        res_str = f"{cal_res[0]}x{cal_res[1]}" if cal_res and len(cal_res) == 2 else "unknown"
        log.info(
            "TribeWatch started — capturing every %.1fs at %s%s",
            self.config.tribe_log.interval,
            res_str,
            f", window='{self.config.general.window_title}'" if self.config.general.window_title else "",
        )
        if self._parasaur_capture:
            log.info(
                "Parasaur detection enabled — polling every %.1fs at %s",
                self.config.parasaur.interval,
                res_str,
            )
        if self._tribe_capture:
            log.info(
                "Tribe window capture enabled — polling every %.1fs at %s",
                self.config.tribe.interval,
                res_str,
            )

        # Resolve server_id early so events aren't skipped on first cycle
        try:
            from tribewatch.server_id import get_server_info
            info = get_server_info()
            if info["server_id"]:
                self._server_id = info["server_id"]
                self._server_name = info["server_name"]
                log.info("Server ID resolved on startup: %s (%s)", self._server_id, self._server_name)
        except Exception:
            log.debug("Early server_id resolution failed, will retry via heartbeat")

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
            for store in self._dedup_stores.values():
                store.save()
            self.capture.close()
            if self._parasaur_capture:
                self._parasaur_capture.close()
            if self._tribe_capture:
                self._tribe_capture.close()
            log.info("TribeWatch stopped")

    def stop(self) -> None:
        self._running = False
