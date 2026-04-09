"""Auto-reconnect sequence: relaunch ARK and rejoin the server."""

from __future__ import annotations

import asyncio
import base64
import ctypes
import io
import logging
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from tribewatch.capture import _find_window_by_title, _grab_window, focus_window, send_click, send_key

if TYPE_CHECKING:
    from tribewatch.relay import ServerRelay

log = logging.getLogger(__name__)

# Steam app ID for ARK: Survival Ascended
_ARK_APP_ID = "2399830"

# Epic Games launch URI — catalog namespace:item:artifact for ARK SA
# Fallback: find and launch the exe directly via _get_epic_install_paths()
_EPIC_LAUNCH_URI = "com.epicgames.launcher://apps/ark%3A343af302390741e0b69c4b16e580de9b%3ADroppedIcicle?action=launch&silent=true"

# Timeouts (seconds)
_LAUNCH_TIMEOUT = 180  # 3 min to detect game window
_TITLE_TIMEOUT = 180   # 3 min to find title screen text
_LOAD_TIMEOUT = 180    # 3 min for game to load after clicking join
_BROWSER_TIMEOUT = 60  # 1 min for server browser UI elements
_POLL_INTERVAL = 5     # seconds between polls
_TRIBE_LOG_DELAY = 30  # seconds to wait before opening tribe log
_KILL_TIMEOUT = 30     # seconds to wait for process to exit
_KILL_STEAM_DELAY = 3  # seconds to let Steam register game exit
_INITIAL_BACKOFF = 30  # first retry delay (seconds)
_MAX_BACKOFF = 1800    # cap at 30 minutes


class _ReconnectAbort(Exception):
    """Raised to cleanly exit a nested reconnect flow after reporting failure."""


class ReconnectSequence:
    """Manages an automated reconnect sequence for ARK.

    Stages reported via relay:
        closing_game → launching → waiting_title → clicking_join
        → waiting_load → opening_tribe_log → success | failed

    Browser fallback stages:
        browser_start → closing_game → launching → waiting_title
        → dismissing_title → opening_browser → searching_server
        → clicking_join_browser → waiting_load → opening_tribe_log
        → success | failed
    """

    def __init__(
        self,
        window_title: str,
        relay: ServerRelay,
        ocr_engine: str = "paddleocr",
        auto: bool = False,
        use_browser: bool = False,
    ) -> None:
        self._window_title = window_title
        self._relay = relay
        self._ocr_engine = ocr_engine
        self._auto = auto
        self._use_browser = use_browser
        self._task: asyncio.Task | None = None
        self._succeeded: bool = False

        # Detect launcher (steam / epic)
        from tribewatch.server_id import detect_launcher
        self._launcher = detect_launcher() or "steam"
        log.info("Detected launcher: %s", self._launcher)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def succeeded(self) -> bool:
        return self._succeeded

    def start(self) -> asyncio.Task:
        """Start the reconnect sequence as a background task."""
        if self.running:
            raise RuntimeError("Reconnect sequence already running")
        self._task = asyncio.create_task(self._run())
        return self._task

    async def cancel(self) -> None:
        """Cancel a running reconnect sequence."""
        if not self.running or self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        await self._report("failed", "Cancelled by user")

    def _capture_screenshot_b64(self) -> str:
        """Capture the game window as a base64-encoded JPEG, or empty string.

        Only captures the ARK window — never falls back to full-screen
        capture to avoid capturing sensitive content.
        """
        try:
            hwnd = _find_window_by_title(self._window_title)
            if not hwnd:
                return ""
            img = _grab_window(hwnd, bbox=None)
            if img is None:
                return ""
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=50)
            return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            log.debug("Failed to capture reconnect screenshot", exc_info=True)
            return ""

    async def _report(self, stage: str, message: str) -> None:
        """Report reconnect progress to the server, with a screenshot."""
        log.info("Reconnect [%s]: %s", stage, message)
        image = self._capture_screenshot_b64()
        try:
            await self._relay.send_reconnect_status(stage, message, image=image, auto=self._auto)
        except Exception:
            log.debug("Failed to send reconnect status", exc_info=True)

    async def _kill_game(self) -> None:
        """Kill the ARK game process if running, then wait for it to fully exit.

        Waits for both the window to disappear AND the process to exit,
        so Steam no longer considers the game running.
        """
        _ARK_EXES = ("ArkAscended.exe", "ShooterGame.exe")

        # Try taskkill on known ARK process names
        for exe in _ARK_EXES:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", exe],
                    capture_output=True, timeout=10,
                )
            except Exception:
                pass

        # Wait for window to disappear AND process to fully exit
        elapsed = 0.0
        while elapsed < _KILL_TIMEOUT:
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL
            window_gone = _find_window_by_title(self._window_title) is None
            process_gone = not self._is_ark_running(_ARK_EXES)
            if window_gone and process_gone:
                log.info("Game process fully exited")
                # Brief pause so Steam registers the game as closed
                await asyncio.sleep(_KILL_STEAM_DELAY)
                return

        log.warning("ARK process still present after %ds, attempting another kill", int(_KILL_TIMEOUT))
        for exe in _ARK_EXES:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", exe],
                    capture_output=True, timeout=10,
                )
            except Exception:
                pass
        await asyncio.sleep(_KILL_STEAM_DELAY + 2)

    def _launch_game(self) -> None:
        """Launch ARK via the detected launcher (Steam or Epic)."""
        if self._launcher == "epic":
            log.info("Launching game via Epic Games...")
            # Try Epic launcher URI first
            try:
                subprocess.Popen(
                    ["cmd", "/c", "start", "", _EPIC_LAUNCH_URI],
                    shell=False,
                )
            except Exception:
                # Fallback: try launching the exe directly
                log.warning("Epic launcher URI failed, trying direct exe launch")
                from tribewatch.server_id import _get_epic_install_paths
                for epic_path in _get_epic_install_paths():
                    exe = epic_path / "ShooterGame" / "Binaries" / "Win64" / "ArkAscended.exe"
                    if exe.exists():
                        subprocess.Popen([str(exe)], cwd=str(exe.parent))
                        return
                log.error("Could not find ARK exe in Epic install paths")
        else:
            log.info("Launching game via Steam...")
            subprocess.Popen(
                ["cmd", "/c", "start", f"steam://run/{_ARK_APP_ID}"],
                shell=False,
            )

    @staticmethod
    def _is_ark_running(exe_names: tuple[str, ...]) -> bool:
        """Check if any ARK process is still running via tasklist."""
        try:
            result = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
            output = result.stdout
            if not isinstance(output, str):
                return False
            output_lower = output.lower()
            return any(exe.lower() in output_lower for exe in exe_names)
        except Exception:
            return False

    # --- Generic OCR helpers ---

    def _find_text_coords(self, img, target: str) -> tuple[int, int] | None:
        """OCR the image and find *target* text (case-insensitive substring).

        Returns ``(cx, cy)`` centre of the bounding box, or ``None``.
        """
        try:
            from tribewatch.ocr_engine import _get_rapidocr_engine
            import numpy as np

            engine = _get_rapidocr_engine()
            img_array = np.array(img)
            result, _ = engine(img_array)

            if result is None:
                return None

            target_upper = target.upper()
            for detection in result:
                bbox_points, text, _conf = detection
                if target_upper in text.upper():
                    xs = [p[0] for p in bbox_points]
                    ys = [p[1] for p in bbox_points]
                    cx = int(sum(xs) / len(xs))
                    cy = int(sum(ys) / len(ys))
                    return (cx, cy)
        except Exception:
            log.debug("OCR failed looking for %r", target, exc_info=True)

        return None

    def _click_at(self, client_x: int, client_y: int, pyautogui) -> None:
        """Convert client coords to screen coords, focus the window, and click."""
        hwnd = _find_window_by_title(self._window_title)
        if not hwnd:
            return
        point = (ctypes.c_long * 2)(client_x, client_y)
        ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(point))  # type: ignore[attr-defined]
        focus_window(self._window_title)
        import time
        time.sleep(0.3)
        pyautogui.click(point[0], point[1])

    async def _wait_for_text(
        self,
        target: str,
        timeout: float,
        stage: str,
    ) -> tuple[int, int]:
        """Poll OCR until *target* text is found. Returns centre coords.

        Reports progress via *stage* and raises ``_ReconnectAbort`` on timeout.
        """
        elapsed = 0.0
        last_update = 0.0
        while elapsed < timeout:
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL

            if elapsed - last_update >= 15:
                last_update = elapsed
                await self._report(stage, f"Waiting for '{target}' ({int(elapsed)}s)...")

            hwnd = _find_window_by_title(self._window_title)
            if not hwnd:
                continue

            img = _grab_window(hwnd, bbox=None)
            if img is None:
                continue

            coords = self._find_text_coords(img, target)
            if coords is not None:
                return coords

        await self._report("failed", f"'{target}' not found after {int(timeout)}s")
        raise _ReconnectAbort(f"'{target}' not found after {int(timeout)}s")

    def _find_exact_text_coords(self, img, target: str) -> tuple[int, int] | None:
        """OCR the image and find *target* as an exact match (case-insensitive).

        Returns ``(cx, cy)`` centre of the bounding box, or ``None``.
        """
        try:
            from tribewatch.ocr_engine import _get_rapidocr_engine
            import numpy as np

            engine = _get_rapidocr_engine()
            img_array = np.array(img)
            result, _ = engine(img_array)

            if result is None:
                return None

            target_upper = target.upper()
            for detection in result:
                bbox_points, text, _conf = detection
                if text.strip().upper() == target_upper:
                    xs = [p[0] for p in bbox_points]
                    ys = [p[1] for p in bbox_points]
                    cx = int(sum(xs) / len(xs))
                    cy = int(sum(ys) / len(ys))
                    return (cx, cy)
        except Exception:
            log.debug("OCR failed looking for exact %r", target, exc_info=True)

        return None

    async def _wait_for_exact_text(
        self,
        target: str,
        timeout: float,
        stage: str,
    ) -> tuple[int, int]:
        """Poll OCR until *target* text is found as an exact match. Returns centre coords.

        Reports progress via *stage* and raises ``_ReconnectAbort`` on timeout.
        """
        elapsed = 0.0
        last_update = 0.0
        while elapsed < timeout:
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL

            if elapsed - last_update >= 15:
                last_update = elapsed
                await self._report(stage, f"Waiting for '{target}' ({int(elapsed)}s)...")

            hwnd = _find_window_by_title(self._window_title)
            if not hwnd:
                continue

            img = _grab_window(hwnd, bbox=None)
            if img is None:
                continue

            coords = self._find_exact_text_coords(img, target)
            if coords is not None:
                return coords

        await self._report("failed", f"'{target}' not found after {int(timeout)}s")
        raise _ReconnectAbort(f"'{target}' not found after {int(timeout)}s")

    # --- Main flow routing ---

    async def _run(self) -> None:
        """Execute the full reconnect sequence, retrying with exponential backoff."""
        attempt = 0
        backoff = _INITIAL_BACKOFF

        while True:
            attempt += 1
            try:
                if self._use_browser:
                    result = await self._do_browser_reconnect()
                else:
                    result = await self._do_reconnect(attempt)

                if result == "success":
                    self._succeeded = True
                    return
            except _ReconnectAbort:
                pass
            except Exception as exc:
                await self._report("failed", f"Unexpected error: {exc}")
                log.exception("Reconnect sequence failed (attempt %d)", attempt)

            # Wait with exponential backoff before retrying
            await self._report(
                "retrying",
                f"Attempt {attempt} failed — retrying in {int(backoff)}s...",
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)

    async def _do_reconnect(self, attempt: int = 1) -> str:
        """Run one reconnect attempt.

        Returns ``"success"`` or ``"retry"``.
        """
        # Lazy-import pyautogui
        try:
            import pyautogui
        except ImportError:
            await self._report("failed", "pyautogui not installed (pip install pyautogui)")
            return "failed"

        # --- Stage 0: Close existing game ---
        await self._report("closing_game", "Closing game...")
        await self._kill_game()

        # --- Stage 1: Launch game ---
        launcher_name = "Epic Games" if self._launcher == "epic" else "Steam"
        await self._report("launching", f"Launching game via {launcher_name}...")
        self._launch_game()

        # Poll for game window
        hwnd = None
        elapsed = 0.0
        while elapsed < _LAUNCH_TIMEOUT:
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL
            hwnd = _find_window_by_title(self._window_title)
            if hwnd:
                break

        if not hwnd:
            await self._report("failed", f"Game window not found after {_LAUNCH_TIMEOUT}s")
            return "retry"

        await self._report("launching", f"Game window found (hwnd={hwnd})")

        # --- Stage 2: Wait for title screen ---
        await self._report("waiting_title", "Waiting for title screen...")
        join_coords = None
        elapsed = 0.0
        last_update = 0.0

        while elapsed < _TITLE_TIMEOUT:
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL

            # Send periodic screenshot updates every 15s
            if elapsed - last_update >= 15:
                last_update = elapsed
                await self._report("waiting_title", f"Waiting for title screen ({int(elapsed)}s)...")

            # Re-find hwnd each iteration — ARK recreates its window during startup
            hwnd = _find_window_by_title(self._window_title)
            if not hwnd:
                continue

            img = _grab_window(hwnd, bbox=None)
            if img is None:
                continue

            join_coords = self._find_join_button(img)
            if join_coords is not None:
                break

        if join_coords is None:
            await self._report("failed", f"'JOIN LAST SESSION' not found after {_TITLE_TIMEOUT}s")
            return "retry"

        # --- Stage 3: Click JOIN LAST SESSION ---
        # The button is on the title screen — click it directly at client
        # coordinates via PostMessage (avoids DPI / screen-coord issues).
        _MAX_JOIN_CLICKS = 5

        for click_attempt in range(1, _MAX_JOIN_CLICKS + 1):
            # Re-grab the image to get fresh button coordinates each attempt
            hwnd = _find_window_by_title(self._window_title) or hwnd
            img = _grab_window(hwnd, bbox=None)
            if img:
                fresh_coords = self._find_join_button(img)
                if fresh_coords:
                    join_coords = fresh_coords

            # Send click directly to the window at client coordinates
            focus_window(self._window_title)
            await asyncio.sleep(0.3)
            send_click(self._window_title, join_coords[0], join_coords[1])
            await asyncio.sleep(1)
            await self._report(
                "clicking_join",
                f"Clicked 'JOIN LAST SESSION' at ({join_coords[0]}, {join_coords[1]}) "
                f"(attempt {click_attempt}/{_MAX_JOIN_CLICKS})",
            )

            # Verify the click worked — wait and confirm the button is gone.
            await asyncio.sleep(5)
            still_on_title = False
            capture_ok = False
            for _verify in range(3):
                hwnd = _find_window_by_title(self._window_title)
                if hwnd:
                    img = _grab_window(hwnd, bbox=None)
                    if img:
                        capture_ok = True
                        if self._find_join_button(img) is not None:
                            still_on_title = True
                        break
                await asyncio.sleep(2)

            # If we couldn't capture the window at all (PrintWindow failures
            # common during game transitions), assume the click landed and
            # proceed — waiting_load will catch errors.
            if not capture_ok:
                log.info("Click verification: capture failed, assuming click landed")
                break

            if not still_on_title:
                break  # Click worked — proceed to waiting_load

            if click_attempt < _MAX_JOIN_CLICKS:
                await self._report(
                    "clicking_join",
                    f"Click did not land — retrying ({click_attempt}/{_MAX_JOIN_CLICKS})...",
                )
            else:
                await self._report(
                    "click_failed",
                    f"JOIN LAST SESSION visible but click failed after {_MAX_JOIN_CLICKS} attempts",
                )
                return "retry"

        # --- Stage 3b + 4: Wait for game to load ---
        # Poll the screen and react to whatever state we see.  This handles
        # extra JOIN buttons (event/Easter dialogs, server browser), error
        # dialogs, and the transition from title screen → game world.
        await self._report("waiting_load", "Waiting for game to load...")
        elapsed = 0.0
        last_update = 0.0
        consecutive_clear = 0  # consecutive checks with no title/join UI

        while elapsed < _LOAD_TIMEOUT:
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL

            # Send periodic screenshot updates every 15s
            if elapsed - last_update >= 15:
                last_update = elapsed
                await self._report("waiting_load", f"Waiting for game to load ({int(elapsed)}s)...")

            # Re-find hwnd each iteration
            hwnd = _find_window_by_title(self._window_title)
            if not hwnd:
                consecutive_clear = 0
                continue

            img = _grab_window(hwnd, bbox=None)
            if img is None:
                consecutive_clear = 0
                continue

            # --- Check for error dialogs first ---

            # "Connection Failed" dialog
            if self._find_connection_failed(img):
                await self._report(
                    "waiting_load",
                    "Connection failed dialog detected",
                )
                return "retry"

            # "Failed to join" dialog — click ACCEPT, switch to browser
            if self._find_text_coords(img, "ACCEPT") is not None:
                failed_join = self._find_text_coords(img, "FAILED")
                if failed_join is not None:
                    accept_coords = self._find_text_coords(img, "ACCEPT")
                    if accept_coords:
                        send_click(self._window_title, accept_coords[0], accept_coords[1])
                    await asyncio.sleep(2)
                    self._use_browser = True
                    await self._report(
                        "failed",
                        "Join failed — switching to server browser",
                    )
                    return "retry"

            # --- Check for JOIN LAST SESSION (still on title screen) ---
            join_last = self._find_join_button(img)
            if join_last is not None:
                consecutive_clear = 0
                await self._report(
                    "clicking_join",
                    f"JOIN LAST SESSION still visible at ({join_last[0]}, {join_last[1]}) — clicking",
                )
                focus_window(self._window_title)
                await asyncio.sleep(0.3)
                send_click(self._window_title, join_last[0], join_last[1])
                await asyncio.sleep(3)
                continue

            # --- Check for any standalone JOIN button ---
            # Catches event/Easter confirmation dialogs, server browser JOIN,
            # or any other screen with a JOIN button that needs clicking.
            extra_join = self._find_exact_text_coords(img, "JOIN")
            if extra_join is not None:
                consecutive_clear = 0
                await self._report(
                    "clicking_join",
                    f"JOIN button at ({extra_join[0]}, {extra_join[1]}) — clicking",
                )
                focus_window(self._window_title)
                await asyncio.sleep(0.3)
                send_click(self._window_title, extra_join[0], extra_join[1])
                await asyncio.sleep(3)
                continue

            # --- Check for main menu (JOIN GAME without a JOIN button) ---
            if self._find_text_coords(img, "JOIN GAME") is not None:
                await self._report("failed", "Landed on main menu — retrying")
                return "retry"

            # --- No title screen, no JOIN, no errors — game might be loaded ---
            consecutive_clear += 1
            if consecutive_clear >= 2:
                opened = await self._open_tribe_log(pyautogui)
                return "success" if opened else "retry"

        await self._report("failed", f"Game did not load within {_LOAD_TIMEOUT}s")
        return "retry"

    # --- Server browser fallback ---

    async def _do_browser_reconnect(self) -> str:
        """Reconnect via the in-game server browser instead of JOIN LAST SESSION.

        Returns ``"success"`` or ``"retry"``.
        """
        import pyautogui

        # Step 1: Read server ID
        from tribewatch.server_id import get_server_info
        server_id = get_server_info()["server_id"]
        if not server_id:
            await self._report("failed", "Could not find server ID in GameUserSettings.ini")
            return "retry"

        await self._report(
            "browser_start",
            f"Using server browser fallback (server ID: {server_id})",
        )

        # Step 2: Kill game + relaunch
        await self._report("closing_game", "Closing game...")
        await self._kill_game()

        launcher_name = "Epic Games" if self._launcher == "epic" else "Steam"
        await self._report("launching", f"Launching game via {launcher_name}...")
        self._launch_game()

        # Poll for game window
        hwnd = None
        elapsed = 0.0
        while elapsed < _LAUNCH_TIMEOUT:
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL
            hwnd = _find_window_by_title(self._window_title)
            if hwnd:
                break

        if not hwnd:
            await self._report("failed", f"Game window not found after {_LAUNCH_TIMEOUT}s")
            return "retry"

        await self._report("launching", f"Game window found (hwnd={hwnd})")

        # Step 3: Wait for title screen (use JOIN LAST SESSION as readiness indicator)
        await self._report("waiting_title", "Waiting for title screen...")
        await self._wait_for_text("JOIN LAST SESSION", _TITLE_TIMEOUT, "waiting_title")

        # Step 4: Press Space to dismiss "PRESS TO START" overlay — retry until dismissed
        await self._report("dismissing_title", "Pressing Space to dismiss title overlay...")
        for press_attempt in range(6):  # up to ~30s
            send_key(self._window_title, "space")
            await asyncio.sleep(3)
            hwnd = _find_window_by_title(self._window_title)
            if hwnd:
                img = _grab_window(hwnd, bbox=None)
                if img and self._find_text_coords(img, "JOIN GAME"):
                    break  # overlay dismissed, main menu visible
            if press_attempt < 5:
                await self._report("dismissing_title", f"Retrying Space press (attempt {press_attempt + 2})...")
        await asyncio.sleep(1)

        # Step 5: Find and click "JOIN GAME"
        await self._report("opening_browser", "Looking for JOIN GAME button...")
        join_game_coords = await self._wait_for_text("JOIN GAME", _BROWSER_TIMEOUT, "opening_browser")
        self._click_at(join_game_coords[0], join_game_coords[1], pyautogui)
        await asyncio.sleep(1)
        await self._report("opening_browser", "Clicked JOIN GAME")

        # Step 6: Wait for server browser (look for SESSION NAME or SESSION FILTER)
        await self._report("searching_server", "Waiting for server browser...")
        await self._wait_for_text("SESSION", _BROWSER_TIMEOUT, "searching_server")
        await asyncio.sleep(1)

        # Step 7: Click search area and type server ID
        # Look for the search/filter field area
        hwnd = _find_window_by_title(self._window_title)
        if not hwnd:
            await self._report("failed", "Game window lost while in server browser")
            return "retry"

        img = _grab_window(hwnd, bbox=None)
        if img is None:
            await self._report("failed", "Could not capture game window")
            return "retry"

        # Try to find the filter text field
        filter_coords = self._find_text_coords(img, "NAME FILTER")
        if filter_coords is None:
            filter_coords = self._find_text_coords(img, "SESSION FILTER")
        if filter_coords is None:
            # Fallback: try "SEARCH" as some UIs label it that way
            filter_coords = self._find_text_coords(img, "SEARCH")

        if filter_coords is not None:
            self._click_at(filter_coords[0], filter_coords[1], pyautogui)
            await asyncio.sleep(0.5)

        # Type the server ID
        pyautogui.write(server_id, interval=0.05)
        await self._report("searching_server", f"Typed server ID: {server_id}")
        await asyncio.sleep(5)  # Wait for server list to filter

        # Step 8a: Click the first server row to select it
        await self._report("clicking_join_browser", "Selecting server in results...")
        row_coords = self._find_server_row(server_id)
        if row_coords:
            self._click_at(row_coords[0], row_coords[1], pyautogui)
            await asyncio.sleep(1)
            await self._report("clicking_join_browser", "Selected server row")
        else:
            await self._report("clicking_join_browser", "Could not locate server row, attempting JOIN anyway")

        # Step 8b: Find and click the JOIN button (exact match, bottom of screen)
        await self._report("clicking_join_browser", "Looking for JOIN button...")
        join_coords = await self._wait_for_exact_text("JOIN", _BROWSER_TIMEOUT, "clicking_join_browser")
        self._click_at(join_coords[0], join_coords[1], pyautogui)
        await asyncio.sleep(1)
        await self._report("clicking_join_browser", "Clicked JOIN in server browser")

        # Step 8c + 9: Wait for game to load, clicking any JOIN buttons that appear
        await self._report("waiting_load", "Waiting for game to load...")
        elapsed = 0.0
        last_update = 0.0
        consecutive_clear = 0

        while elapsed < _LOAD_TIMEOUT:
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL

            if elapsed - last_update >= 15:
                last_update = elapsed
                await self._report("waiting_load", f"Waiting for game to load ({int(elapsed)}s)...")

            hwnd = _find_window_by_title(self._window_title)
            if not hwnd:
                consecutive_clear = 0
                continue

            img = _grab_window(hwnd, bbox=None)
            if img is None:
                consecutive_clear = 0
                continue

            # Check for connection failures
            if self._find_connection_failed(img):
                await self._report("failed", "Connection failed dialog detected")
                return "retry"

            # Still on title screen?
            join_last = self._find_join_button(img)
            if join_last is not None:
                consecutive_clear = 0
                await self._report(
                    "clicking_join_browser",
                    f"JOIN LAST SESSION still visible at ({join_last[0]}, {join_last[1]}) — clicking",
                )
                focus_window(self._window_title)
                await asyncio.sleep(0.3)
                send_click(self._window_title, join_last[0], join_last[1])
                await asyncio.sleep(3)
                continue

            # Any standalone JOIN button (event dialogs, confirmations)
            extra_join = self._find_exact_text_coords(img, "JOIN")
            if extra_join is not None:
                consecutive_clear = 0
                await self._report(
                    "clicking_join_browser",
                    f"JOIN button at ({extra_join[0]}, {extra_join[1]}) — clicking",
                )
                focus_window(self._window_title)
                await asyncio.sleep(0.3)
                send_click(self._window_title, extra_join[0], extra_join[1])
                await asyncio.sleep(3)
                continue

            # Main menu?
            if self._find_text_coords(img, "JOIN GAME") is not None:
                await self._report("failed", "Landed on main menu — retrying")
                return "retry"

            # No UI elements — game might be loaded
            consecutive_clear += 1
            if consecutive_clear >= 2:
                opened = await self._open_tribe_log(pyautogui)
                return "success" if opened else "retry"

        await self._report("failed", f"Game did not load within {_LOAD_TIMEOUT}s")
        return "retry"

    async def _open_tribe_log(self, pyautogui) -> bool:
        """Wait for the game to settle, then press L to open the tribe log.

        Retries up to 3 times since the first press can be swallowed by
        loading transitions or input lag.

        Returns True if the tribe log was confirmed open, False otherwise.
        """
        await self._report(
            "waiting_load",
            f"Game loaded — waiting {_TRIBE_LOG_DELAY}s before opening tribe log...",
        )
        await asyncio.sleep(_TRIBE_LOG_DELAY)

        for attempt in range(1, 4):
            send_key(self._window_title, "l")
            await self._report(
                "opening_tribe_log",
                f"Pressed L to open tribe log (attempt {attempt}/3)",
            )
            await asyncio.sleep(10)

            # Check if tribe log actually opened via OCR
            hwnd = _find_window_by_title(self._window_title)
            if hwnd:
                img = _grab_window(hwnd, bbox=None)
                if img and self._has_tribe_log(img):
                    await self._apply_console_settings(pyautogui)

                    # Re-verify tribe log is still open after console settings
                    await asyncio.sleep(2)
                    hwnd = _find_window_by_title(self._window_title)
                    if hwnd:
                        img = _grab_window(hwnd, bbox=None)
                        if img and self._has_tribe_log(img):
                            await self._report("success", "Tribe log opened — monitoring resumed")
                            return True
                        # Console settings closed the tribe log — try reopening
                        log.info("Tribe log closed after console settings, pressing L to reopen")
                        send_key(self._window_title, "l")
                        await asyncio.sleep(5)
                        hwnd = _find_window_by_title(self._window_title)
                        if hwnd:
                            img = _grab_window(hwnd, bbox=None)
                            if img and self._has_tribe_log(img):
                                await self._report("success", "Tribe log reopened — monitoring resumed")
                                return True
                    # Fall through to next attempt

            log.info("Tribe log not detected after pressing L (attempt %d/3)", attempt)

        # All attempts to open tribe log failed
        await self._report(
            "failed",
            "Tribe log did not open after 3 attempts — reconnect failed",
        )
        return False

    async def _apply_console_settings(self, pyautogui) -> None:
        """Open console (~), paste ini.txt commands, press Enter, then close console.

        Uses focus_window + pyautogui for the whole sequence since the UE
        console requires the window to be focused and tilde doesn't reliably
        open via PostMessage.
        """
        # In frozen builds, look next to the exe; otherwise relative to package root
        if getattr(__import__('sys'), 'frozen', False):
            base = Path(__import__('sys')._MEIPASS)
        else:
            base = Path(__file__).parent.parent
        ini_path = base / "scripts" / "ini.txt"
        if not ini_path.exists():
            log.debug("scripts/ini.txt not found at %s, skipping console settings", ini_path)
            return

        try:
            text = ini_path.read_text(encoding="utf-8").strip()
            if not text:
                return

            import pyperclip

            # Focus window first — console requires active focus
            focus_window(self._window_title)
            await asyncio.sleep(0.5)

            # Open console with tilde via pyautogui (PostMessage doesn't
            # reliably trigger UE console bindings)
            pyautogui.press("`")
            await asyncio.sleep(0.5)

            # Copy commands to clipboard and paste
            pyperclip.copy(text)
            await asyncio.sleep(0.1)
            pyautogui.hotkey("ctrl", "v")
            await asyncio.sleep(1.5)

            # Submit command — Enter also closes the console in UE
            pyautogui.press("enter")

            log.info("Applied console settings from scripts/ini.txt")
        except Exception:
            log.debug("Failed to apply console settings", exc_info=True)

    def _has_tribe_log(self, img) -> bool:
        """OCR the image and check if 'LOG' header is visible (tribe log open)."""
        try:
            from tribewatch.ocr_engine import _get_rapidocr_engine
            import numpy as np

            engine = _get_rapidocr_engine()
            img_array = np.array(img)
            result, _ = engine(img_array)

            if result is None:
                return False

            for detection in result:
                _bbox, text, _conf = detection
                if text.strip().upper() == "LOG":
                    return True
        except Exception:
            log.debug("OCR failed during tribe log check", exc_info=True)

        return False

    def _find_server_row(self, server_id: str) -> tuple[int, int] | None:
        """Find the first server result row in the server browser.

        Looks for text below the SESSION NAME header that likely represents
        a server entry. Uses the server_id or common server name patterns.
        """
        try:
            from tribewatch.ocr_engine import _get_rapidocr_engine
            import numpy as np

            hwnd = _find_window_by_title(self._window_title)
            if not hwnd:
                return None
            img = _grab_window(hwnd, bbox=None)
            if img is None:
                return None

            engine = _get_rapidocr_engine()
            img_array = np.array(img)
            result, _ = engine(img_array)
            if result is None:
                return None

            # Find the SESSION NAME header Y position
            header_y = None
            for detection in result:
                bbox_points, text, _conf = detection
                if "SESSION" in text.upper() and "NAME" in text.upper():
                    header_y = max(p[1] for p in bbox_points)
                    break

            if header_y is None:
                # Fallback: try just "SESSION"
                for detection in result:
                    bbox_points, text, _conf = detection
                    if text.strip().upper() == "SESSION":
                        header_y = max(p[1] for p in bbox_points)
                        break

            # Find the first text detection below the header that isn't
            # a header column itself — this should be the first result row
            candidates = []
            header_texts = {"SESSION", "NAME", "ALL", "PLAYERS", "DAY", "PING", "BUILD", "W/MODS", "MAP"}
            for detection in result:
                bbox_points, text, _conf = detection
                cy = int(sum(p[1] for p in bbox_points) / len(bbox_points))
                cx = int(sum(p[0] for p in bbox_points) / len(bbox_points))
                text_upper = text.strip().upper()
                # Skip header row and UI elements
                if header_y is not None and cy <= header_y + 10:
                    continue
                if text_upper in header_texts:
                    continue
                # Skip bottom UI buttons
                if text_upper in {"BACK", "REFRESH", "JOIN", "SORT ORDER", "AUTO BALOON",
                                  "FREE PLAY", "SHOW PASSWORD", "OFFICIAL", "UNOFFICIAL",
                                  "NON-DEDICATED", "SORT"}:
                    continue
                candidates.append((cy, cx, text))

            if candidates:
                candidates.sort(key=lambda c: c[0])

                # Prefer a row whose text contains the server ID — this
                # validates we're about to join the right server and not
                # a stale/wrong result from the browser filter.
                for cy, cx, text in candidates:
                    if server_id in text or server_id in text.replace(" ", ""):
                        log.info(
                            "Validated server row: %r contains server_id %r (y=%d)",
                            text, server_id, cy,
                        )
                        return (cx, cy)

                # No row matched the server ID — if there's exactly one
                # result it's probably just an OCR mismatch on the ID
                # digits. Accept it with a warning.
                if len(candidates) == 1:
                    _, cx, text = candidates[0]
                    log.warning(
                        "Server row %r does not contain server_id %r "
                        "— only one result, accepting anyway (y=%d)",
                        text, server_id, candidates[0][0],
                    )
                    return (cx, candidates[0][0])

                # Multiple results and none match — refuse to guess.
                log.warning(
                    "Multiple server rows (%d) and none contain server_id %r — "
                    "refusing to click a potentially wrong server",
                    len(candidates), server_id,
                )
                return None

        except Exception:
            log.debug("Failed to find server row", exc_info=True)

        return None

    def _find_connection_failed(self, img) -> bool:
        """OCR the image and check for 'CONNECTION FAILED' text."""
        return self._find_text_coords(img, "CONNECTION FAILED") is not None

    def _find_join_button(self, img) -> tuple[int, int] | None:
        """OCR the image and look for 'JOIN LAST SESSION', return center coords or None."""
        return self._find_text_coords(img, "JOIN LAST SESSION")
