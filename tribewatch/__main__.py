"""TribeWatch CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from tribewatch import __version__

DEFAULT_CONFIG = "tribewatch.toml"


def _tribe_names_match(saved: str, detected: str) -> bool:
    """Check if two tribe names are close enough to be considered the same.

    Accounts for OCR truncation (prefix matching) and minor OCR noise
    (edit distance).
    """
    if not saved or not detected:
        return False
    s, d = saved.lower(), detected.lower()
    if s == d:
        return True
    # Prefix check: OCR often truncates the name
    if s.startswith(d) or d.startswith(s):
        return True
    from tribewatch.fuzzy import edit_distance, fuzzy_threshold
    return edit_distance(s, d) <= fuzzy_threshold(saved)




def _set_dpi_awareness() -> None:
    """Enable per-monitor DPI awareness on Windows."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


def _set_console_title_and_icon() -> None:
    """Set the console window title to include the version and apply the app icon."""
    try:
        ctypes.windll.kernel32.SetConsoleTitleW(f"TribeWatch v{__version__}")
    except Exception:
        pass

    # Set the console window icon from the bundled .ico file
    try:
        if getattr(sys, "frozen", False):
            base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
        else:
            base = Path(__file__).parent.parent
        ico_path = base / "tribewatch.ico"
        if not ico_path.exists():
            return

        # Declare proper ctypes signatures — default c_int truncates
        # 64-bit handles on x64 Windows.
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        _GetConsoleWindow = ctypes.windll.kernel32.GetConsoleWindow
        _GetConsoleWindow.restype = wintypes.HWND

        _LoadImageW = user32.LoadImageW
        _LoadImageW.argtypes = [
            wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        _LoadImageW.restype = wintypes.HANDLE

        _SendMessageW = user32.SendMessageW
        _SendMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        _SendMessageW.restype = wintypes.LPARAM

        hwnd = _GetConsoleWindow()
        if not hwnd:
            return

        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1

        icon_path_str = str(ico_path)
        h_small = _LoadImageW(None, icon_path_str, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        h_big = _LoadImageW(None, icon_path_str, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)

        if h_small:
            _SendMessageW(hwnd, WM_SETICON, ICON_SMALL, h_small)
        if h_big:
            _SendMessageW(hwnd, WM_SETICON, ICON_BIG, h_big)
    except Exception:
        pass


def _setup_logging(level: str) -> None:
    from logging.handlers import RotatingFileHandler

    log_level = getattr(logging, level.upper(), logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(log_level)

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(log_level)
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(console)

    # Rotating file handler — 5 MB per file, keep 3 backups
    file_handler = RotatingFileHandler(
        "tribewatch.log", maxBytes=5 * 1024 * 1024, backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)  # always capture DEBUG to file
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def _cmd_generate_config(config_path: Path, mode: str = "client") -> None:
    from tribewatch.config import generate_default_config, save_config

    cfg = generate_default_config(config_path)
    cfg.server.mode = mode
    save_config(cfg, config_path, mode=mode)
    print(f"Generated {mode} config at: {config_path}")


from tribewatch.setup import (
    cmd_calibrate as _cmd_calibrate,
    cmd_calibrate_manual as _cmd_calibrate_manual,
    cmd_calibrate_parasaur as _cmd_calibrate_parasaur,
    cmd_calibrate_tribe as _cmd_calibrate_tribe,
    cmd_setup as _cmd_setup,
)


def _cmd_test_ocr(config_path: Path) -> None:
    from tribewatch.capture import ScreenCapture
    from tribewatch.config import load_config
    from tribewatch.ocr_engine import recognize
    from tribewatch.parser import parse_events

    cfg = load_config(config_path)
    _setup_logging(cfg.general.log_level)

    print("Capturing screen region...")
    cap = ScreenCapture(cfg.tribe_log.bbox, cfg.general.monitor)
    img = cap.grab()
    cap.close()

    if img is None:
        print("ERROR: Screen capture returned None. Is the game running?")
        sys.exit(1)

    print(f"Captured {img.size[0]}x{img.size[1]} image")

    print("Running OCR...")
    text = asyncio.run(
        recognize(img, engine=cfg.tribe_log.ocr_engine, upscale=cfg.tribe_log.upscale)
    )

    print("\n--- Raw OCR Text ---")
    print(text)
    print("--- End OCR Text ---\n")

    events = parse_events(text)
    if not events:
        print("No tribe log events parsed. Check your bbox calibration.")
    else:
        print(f"Parsed {len(events)} events:")
        for e in events:
            print(f"  [{e.severity.value.upper()}] Day {e.day}, {e.time}: "
                  f"{e.event_type.value} — {e.raw_text}")


def _cmd_test_discord(config_path: Path) -> None:
    from tribewatch.config import load_config
    from tribewatch.parser import EventType, Severity, TribeLogEvent
    from tribewatch.webhook import WebhookDispatcher  # server-side module (requires full repo)

    cfg = load_config(config_path)

    if not cfg.discord.alert_webhook:
        print("ERROR: No alert_webhook configured in", config_path)
        sys.exit(1)

    disp = WebhookDispatcher(
        alert_webhook=cfg.discord.alert_webhook,
        raid_webhook=cfg.discord.raid_webhook,
        ping_role_id=cfg.discord.ping_role_id,
    )

    now = datetime.now(timezone.utc)
    test_event = TribeLogEvent(
        day=9999,
        time="00:00:00",
        raw_text="This is a test message from TribeWatch!",
        event_type=EventType.UNKNOWN,
        severity=Severity.INFO,
        timestamp=now,
    )

    async def send():
        await disp.send_critical(test_event)
        await disp.close()

    print("Sending test event to Discord...")
    asyncio.run(send())
    print("Done! Check your Discord channel.")




def _apply_env_overrides(cfg: object) -> None:
    """Apply environment variable overrides to the loaded config.

    Env vars follow the pattern TRIBEWATCH_{SECTION}_{FIELD} in upper-case.
    Short aliases are kept for backwards compatibility.
    """
    import os

    def _env(name: str) -> str | None:
        return os.environ.get(name) or None

    # --- server ---
    if v := _env("TRIBEWATCH_AUTH_TOKEN"):
        cfg.server.auth_token = v
    if v := _env("TRIBEWATCH_SERVER_URL"):
        cfg.server.server_url = v
    if v := _env("TRIBEWATCH_SERVER_MODE"):
        cfg.server.mode = v
    if v := _env("TRIBEWATCH_RECONNECT_DELAY"):
        cfg.server.reconnect_delay = float(v)

    # --- web ---
    if v := _env("TRIBEWATCH_PORT"):
        cfg.web.port = int(v)
    if v := _env("TRIBEWATCH_HOST"):
        cfg.web.host = v
    if v := _env("TRIBEWATCH_BASE_URL"):
        cfg.web.base_url = v
    if v := _env("TRIBEWATCH_OAUTH_CLIENT_ID"):
        cfg.web.oauth_client_id = v
    if v := _env("TRIBEWATCH_OAUTH_CLIENT_SECRET"):
        cfg.web.oauth_client_secret = v
    if v := _env("TRIBEWATCH_SESSION_SECRET"):
        cfg.web.session_secret = v
    if v := _env("TRIBEWATCH_ADMIN_DISCORD_ID"):
        cfg.web.admin_discord_id = v

    # --- general ---
    if v := _env("TRIBEWATCH_LOG_LEVEL"):
        cfg.general.log_level = v

    # Note: discord, alerts, generator, and presence are per-tribe settings
    # stored in the tribe_config DB — not configurable via env vars.
    # owner_discord_id lives in discord config (per-tribe).


def _custom_button_dialog(
    title: str, message: str, buttons: list[tuple[str, str]],
    default: str = "",
) -> str:
    """Show a modal dialog with arbitrarily-labelled buttons.

    *buttons* is a list of ``(button_text, return_value)`` tuples.
    The dialog returns the value of the clicked button, or empty
    string if the window was closed.
    """
    import tkinter as tk

    result = {"value": ""}
    root = tk.Tk()
    root.title(title)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    root.configure(padx=20, pady=20)

    tk.Label(
        root, text=message, justify="left", anchor="w", font=("Segoe UI", 10),
    ).pack(fill="x")

    btn_frame = tk.Frame(root)
    btn_frame.pack(fill="x", pady=(16, 0))

    def _make_cb(value: str):
        def _cb() -> None:
            result["value"] = value
            root.destroy()
        return _cb

    default_btn = None
    for text, value in buttons:
        b = tk.Button(
            btn_frame, text=text, command=_make_cb(value),
            padx=12, pady=4,
        )
        b.pack(side="left", padx=4)
        if value == default:
            default_btn = b
    if default_btn is not None:
        default_btn.focus_set()
        root.bind("<Return>", lambda _e: default_btn.invoke())

    # Center on screen
    root.update_idletasks()
    w = root.winfo_width()
    h = root.winfo_height()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    root.geometry(f"+{(sw - w) // 2}+{(sh - h) // 3}")

    root.mainloop()
    return result["value"]


def _discover_and_confirm_tribe_name(
    cfg: object, config_path: Path, mode: str = "client",
) -> None:
    """Discover tribe name via OCR and prompt the user to confirm.

    Uses Win32 MessageBox dialogs on Windows, falls back to console prompts.
    Modifies cfg.tribe.tribe_name in-place and saves to config file
    when the user confirms a new or updated name.
    """
    import sys

    from tribewatch.app import discover_tribe_name
    from tribewatch.config import save_config

    saved = cfg.tribe.tribe_name
    detected = discover_tribe_name(cfg)

    if sys.platform == "win32":
        _discover_tribe_name_win32(cfg, config_path, mode, saved, detected)
    else:
        _discover_tribe_name_console(cfg, config_path, mode, saved, detected)


def _discover_tribe_name_win32(
    cfg: object, config_path: Path, mode: str,
    saved: str, detected: str,
) -> None:
    """Tribe name confirmation via Win32 MessageBox dialogs."""
    import ctypes

    from tribewatch.config import save_config

    MB_YESNO = 0x04
    MB_ICONQUESTION = 0x20
    MB_TOPMOST = 0x40000
    IDYES = 6

    title = "TribeWatch \u2014 Tribe Name"

    if not saved:
        # No saved tribe name — keep trying OCR until we get one
        if not detected:
            log = logging.getLogger(__name__)
            log.info("Tribe name not detected — waiting for tribe window to open")
            import time as _time

            from tribewatch.app import discover_tribe_name

            _RETRY_INTERVAL = 5  # seconds
            while not detected:
                _time.sleep(_RETRY_INTERVAL)
                detected = discover_tribe_name(cfg)

            log.info("Tribe name detected: %s", detected)

        result = ctypes.windll.user32.MessageBoxW(
            0,
            f'Detected tribe name:\n\n"{detected}"\n\nIs this correct?',
            title,
            MB_YESNO | MB_ICONQUESTION | MB_TOPMOST,
        )
        if result == IDYES:
            cfg.tribe.tribe_name = detected
            save_config(cfg, config_path, mode=mode)
        else:
            name = _win32_input_box("Enter tribe name:", title)
            if name:
                cfg.tribe.tribe_name = name
                save_config(cfg, config_path, mode=mode)
    else:
        if detected and not _tribe_names_match(saved, detected):
            choice = _custom_button_dialog(
                title,
                (
                    f"Tribe name mismatch:\n\n"
                    f'Saved:      "{saved}"\n'
                    f'Detected:  "{detected}"'
                ),
                buttons=[
                    ("Rename saved tribe", "rename"),
                    ("Treat as new tribe", "new"),
                    ("Keep original", "keep"),
                ],
                default="keep",
            )
            if choice == "rename":
                # Adopt detected name locally. Server-side rename
                # cascade is available via POST /api/tribe/{id}/rename
                # and should be triggered from the dashboard once
                # connected.
                cfg.tribe.tribe_name = detected
                save_config(cfg, config_path, mode=mode)
            elif choice == "new":
                # Switch local config to the detected name. The server
                # will report tribe_unknown on first connect and the
                # operator can finish creating it from the dashboard.
                cfg.tribe.tribe_name = detected
                save_config(cfg, config_path, mode=mode)
            else:
                # keep / closed: keep saved name unchanged.
                pass


def _win32_input_box(prompt: str, title: str) -> str:
    """Show a simple VBScript InputBox dialog and return the entered text.

    Returns empty string if the user cancels or enters nothing.
    """
    import subprocess
    import tempfile

    # Escape quotes for VBScript
    vbs_prompt = prompt.replace('"', '""').replace("\n", '" & vbCrLf & "')
    vbs_title = title.replace('"', '""')

    script = f'WScript.Echo InputBox("{vbs_prompt}", "{vbs_title}", "")'

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".vbs", delete=False, encoding="utf-8",
    ) as f:
        f.write(script)
        vbs_path = f.name

    try:
        result = subprocess.run(
            ["cscript", "//Nologo", vbs_path],
            capture_output=True, text=True, timeout=120,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    finally:
        import os
        try:
            os.unlink(vbs_path)
        except OSError:
            pass


def _discover_tribe_name_console(
    cfg: object, config_path: Path, mode: str,
    saved: str, detected: str,
) -> None:
    """Tribe name confirmation via console prompts (non-Windows fallback)."""
    from tribewatch.config import save_config

    if not saved:
        if not detected:
            print("\nWaiting for tribe window to be visible...")
            import time as _time

            from tribewatch.app import discover_tribe_name

            while not detected:
                _time.sleep(5)
                detected = discover_tribe_name(cfg)

        print(f'\nDetected tribe: "{detected}"')
        answer = input("Is this correct? [Y/n/manual]: ").strip().lower()
        if answer in ("", "y", "yes"):
            cfg.tribe.tribe_name = detected
            save_config(cfg, config_path, mode=mode)
            print(f'Tribe name saved: "{detected}"')
        elif answer in ("n", "no", "manual"):
            name = input("Enter tribe name: ").strip()
            if name:
                cfg.tribe.tribe_name = name
                save_config(cfg, config_path, mode=mode)
                print(f'Tribe name saved: "{name}"')
            else:
                print("No tribe name entered, skipping.")
    else:
        if detected and not _tribe_names_match(saved, detected):
            print(f'\nSaved tribe:  "{saved}"')
            print(f'Detected:     "{detected}"')
            print("  [r]ename — adopt detected name (sync with server via dashboard)")
            print("  [n]ew    — treat detected as a new tribe")
            print("  [k]eep   — keep saved name (default)")
            answer = input("Choice [r/n/K]: ").strip().lower()
            if answer in ("r", "rename", "n", "new"):
                cfg.tribe.tribe_name = detected
                save_config(cfg, config_path, mode=mode)
                print(f'Tribe name updated: "{detected}"')
            else:
                print(f'Keeping saved name: "{saved}"')

    print()


def _apply_resolution_preset(cfg: object) -> bool:
    """Detect game resolution and apply derived bbox presets.

    Returns True if the resolution is verified or matches an existing
    calibration; False if the resolution is unverified and the user
    has no calibration for it (caller should force the setup wizard).
    """
    log = logging.getLogger(__name__)
    try:
        from tribewatch.calibrate import derive_preset, is_verified_resolution
        from tribewatch.server_id import get_game_resolution

        resolution = get_game_resolution()
        cal_res = getattr(cfg.general, "calibration_resolution", None)

        if resolution is None:
            # No detected game resolution. If the user already has a
            # saved calibration, trust it. Otherwise this is a fresh /
            # post-reset state and the dataclass default bbox is
            # garbage — force the setup wizard.
            if cal_res:
                log.debug("Could not detect game resolution — keeping saved calibration")
                return True
            log.warning(
                "Could not detect game resolution and no saved calibration — "
                "forcing setup wizard",
            )
            return False

        cal_matches = bool(cal_res) and tuple(cal_res) == resolution

        # User has already calibrated for this exact resolution — keep their bboxes.
        # But if the saved bbox is empty (e.g. truncated state), fall through
        # so the preset gets applied.
        if cal_matches and cfg.tribe_log.bbox:
            log.debug(
                "Resolution %dx%d matches saved calibration — keeping user bboxes",
                resolution[0], resolution[1],
            )
            return True

        verified = is_verified_resolution(resolution)
        preset = derive_preset(resolution)

        if cal_res and tuple(cal_res) != resolution:
            log.info(
                "Resolution changed from %s to %dx%d — applying derived preset",
                cal_res, resolution[0], resolution[1],
            )

        cfg.tribe_log.bbox = list(preset["tribe_log"])
        cfg.parasaur.bbox = list(preset["parasaur"])
        cfg.tribe.bbox = list(preset["tribe"])
        cfg.general.calibration_resolution = list(resolution)

        log.info(
            "Applied %s bbox preset for %dx%d — tribe_log=%s parasaur=%s tribe=%s",
            "verified" if verified else "derived",
            resolution[0], resolution[1],
            preset["tribe_log"], preset["parasaur"], preset["tribe"],
        )
        return verified
    except Exception:
        log.debug("Resolution preset auto-apply failed", exc_info=True)
        return True


def _check_for_updates() -> None:
    """Check GitHub for a newer release and prompt to update (frozen builds only)."""
    import asyncio as _asyncio

    from tribewatch.updater import check_for_update, download_and_run_installer

    log = logging.getLogger(__name__)
    log.info("Checking for updates...")

    try:
        update = _asyncio.run(check_for_update())
    except Exception:
        log.debug("Update check failed", exc_info=True)
        return

    if update is None:
        log.info("TribeWatch is up to date.")
        return

    print(f"\n  A new version of TribeWatch is available: {update['version']} (current: {update['current']})")
    if update.get("body"):
        # Show first few lines of release notes
        notes = update["body"].strip().split("\n")[:5]
        for line in notes:
            print(f"    {line}")
        if len(update["body"].strip().split("\n")) > 5:
            print("    ...")

    if update["is_installer"]:
        # On a windowed/no-console build (e.g. launched from the start-menu
        # shortcut), input() will raise or hang because there's no stdin.
        # In that case auto-accept and proceed to download silently.
        has_console = sys.stdin is not None and sys.stdin.isatty()
        if has_console:
            answer = input("\n  Download and install update now? [Y/n] ").strip().lower()
        else:
            log.info("No console attached — auto-accepting update prompt")
            answer = "y"
        if answer in ("", "y", "yes"):
            print("  Downloading update...")
            try:
                ok = _asyncio.run(download_and_run_installer(update["download_url"]))
            except Exception:
                ok = False
            if ok:
                print("  Installer launched. TribeWatch will restart shortly.")
                sys.exit(0)
            else:
                print("  Failed to download update. Continuing with current version.")
                print(f"  You can update manually: {update['release_url']}")
        else:
            print("  Skipping update.")
    else:
        print(f"\n  Download the update at: {update['release_url']}")
        if sys.stdin is not None and sys.stdin.isatty():
            input("  Press Enter to continue...")

    print()


def _cmd_run(config_path: Path, *, skip_unverified_setup: bool = False) -> None:
    from tribewatch.config import client_config_path, load_config
    from tribewatch.singleton import ensure_single_instance

    from dotenv import load_dotenv
    load_dotenv()

    # Client mode: load ONLY the client config file
    cp = client_config_path(config_path)
    cfg = load_config(cp)

    _apply_env_overrides(cfg)
    # Configure logging BEFORE singleton enforcement so its diagnostic
    # warnings (process scan results, kill failures, etc) actually land
    # in tribewatch.log instead of being silently swallowed by the
    # uninitialized root logger.
    _setup_logging(cfg.general.log_level)

    # Kill any existing instance before we start
    ensure_single_instance()

    # --- Auto-update check (frozen/installed builds only) ---
    from tribewatch.updater import is_frozen
    if is_frozen():
        _check_for_updates()

    verified = _apply_resolution_preset(cfg)
    if not verified and not skip_unverified_setup:
        try:
            from tribewatch.server_id import get_game_resolution
            res = get_game_resolution()
        except Exception:
            res = None
        res_str = f"{res[0]}x{res[1]}" if res else "your current"
        print()
        print("=" * 70)
        print(f"  Unverified resolution: {res_str}")
        print("=" * 70)
        print(
            "  TribeWatch derived capture regions for this resolution from the\n"
            "  1920x1080 baseline, but it has not been hand-verified. The setup\n"
            "  wizard will now open so you can confirm or adjust the regions."
        )
        print("=" * 70)
        print()
        try:
            _cmd_setup(cp)
            # Reload config so the user's confirmed bboxes are picked up
            cfg = load_config(cp)
            _apply_env_overrides(cfg)
        except Exception:
            log = logging.getLogger(__name__)
            log.exception("Forced setup wizard failed")

    # --- Tribe name discovery ---
    if cfg.tribe.bbox:
        _discover_and_confirm_tribe_name(cfg, cp, mode="client")

    _cmd_run_client(cfg, cp)


async def _handle_screenshot(app: Any, msg_id: str) -> None:
    """Capture a full-window screenshot, JPEG-encode, and send via relay."""
    import base64
    import io

    from tribewatch.capture import _find_window_by_title, _grab_window

    log = logging.getLogger(__name__)
    window_title = getattr(app.config.general, "window_title", "")
    if not window_title:
        log.warning("Screenshot requested but no window_title configured")
        return

    hwnd = _find_window_by_title(window_title)
    if hwnd is None:
        log.warning("Screenshot requested but window '%s' not found", window_title)
        return

    img = _grab_window(hwnd, bbox=None)
    if img is None:
        log.warning("Screenshot capture returned None")
        return

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    relay = getattr(app, "_relay", None)
    if relay:
        await relay.send_screenshot_response(msg_id, b64)
        log.info("Screenshot sent (%d bytes JPEG)", buf.tell())


async def _handle_reconnect_cancel(app: Any) -> None:
    """Cancel a running reconnect sequence."""
    log = logging.getLogger(__name__)
    seq = getattr(app, "_reconnect_seq", None)
    if seq is None or not seq.running:
        log.info("No reconnect in progress to cancel")
        return
    log.info("Cancelling reconnect sequence")
    await seq.cancel()


async def _handle_server_change(
    app: Any, cfg: Any, config_path: Path, old_name: str, new_id: str, new_name: str,
) -> None:
    """Pause monitoring and prompt the user about a server change via MessageBox."""
    log = logging.getLogger(__name__)
    app._paused = True
    log.info("Server changed: %s -> %s — pausing monitoring", old_name, new_name)

    accepted = False
    try:
        import ctypes

        MB_YESNO = 0x04
        MB_ICONWARNING = 0x30
        MB_TOPMOST = 0x40000
        IDYES = 6

        result = await asyncio.get_event_loop().run_in_executor(
            None,
            ctypes.windll.user32.MessageBoxW,
            0,
            (
                f"Server changed!\n\n"
                f"Previous: {old_name}\n"
                f"New: {new_name}\n\n"
                f"Accept this server change and re-detect tribe name?\n"
                f"(Monitoring is paused until you respond)"
            ),
            "TribeWatch \u2014 Server Change Detected",
            MB_YESNO | MB_ICONWARNING | MB_TOPMOST,
        )

        if result == IDYES:
            accepted = True
            from tribewatch.config import client_config_path

            save_path = client_config_path(config_path)
            await asyncio.get_event_loop().run_in_executor(
                None, _discover_and_confirm_tribe_name, cfg, save_path, "client",
            )
            app.config.tribe.tribe_name = cfg.tribe.tribe_name
    except Exception:
        log.exception("Server change handler failed")
    finally:
        if accepted:
            app._server_id = new_id
            app._server_name = new_name
            # Clear stale tribe info so no events dispatch with old tribe name.
            # _tribe_cycle will re-populate once the new tribe is detected.
            app._tribe_info = None
            app._paused = False
            log.info("Server change accepted — monitoring resumed (server_id=%s)", new_id)
        else:
            log.info(
                "Server change declined — monitoring stays paused "
                "(use /resume or transfer back to unpause)"
            )


async def _handle_tribe_name_change(
    app: Any, cfg: Any, config_path: Path, old_name: str, detected_name: str,
) -> None:
    """Prompt user when OCR detects a different tribe name than configured.

    Offers three choices:
    1. Rename — update the existing tribe records to the new name
    2. New tribe — keep old data, start tracking a new tribe
    3. Ignore — do nothing, keep using the old name
    """
    log = logging.getLogger(__name__)
    app._paused = True
    log.info("Tribe name change detected: %r -> %r — pausing monitoring", old_name, detected_name)

    try:
        choice = await asyncio.get_event_loop().run_in_executor(
            None,
            _custom_button_dialog,
            "TribeWatch \u2014 Tribe Name Changed",
            (
                f"Tribe name changed!\n\n"
                f'Previous:  "{old_name}"\n'
                f'Detected:  "{detected_name}"'
            ),
            [
                ("Rename existing tribe", "rename"),
                ("Track as new tribe", "new"),
                ("Ignore (keep old name)", "ignore"),
            ],
            "ignore",
        )

        from tribewatch.config import client_config_path, save_config

        save_path = client_config_path(config_path)
        save_mode = "client"

        if choice == "rename":
            # Rename existing tribe in all stores
            log.info("User chose: rename tribe %r -> %r", old_name, detected_name)
            tribe_store = getattr(app, "_tribe_store", None)
            event_store = getattr(app, "_event_store", None)
            if tribe_store:
                await tribe_store.rename_tribe(old_name, detected_name)
            if event_store:
                await event_store.rename_tribe(old_name, detected_name)
            cfg.tribe.tribe_name = detected_name
            app.config.tribe.tribe_name = detected_name
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: save_config(cfg, save_path, mode=save_mode),
            )
        elif choice == "new":
            # New tribe — just update the config, old data stays under old name
            log.info("User chose: new tribe %r (old data kept as %r)", detected_name, old_name)
            cfg.tribe.tribe_name = detected_name
            app.config.tribe.tribe_name = detected_name
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: save_config(cfg, save_path, mode=save_mode),
            )
        else:
            log.info("User chose: ignore tribe name change, keeping %r", old_name)
    except Exception:
        log.exception("Tribe name change handler failed")
    finally:
        app._tribe_name_change_pending = False
        app._paused = False
        log.info("Tribe name change handled — monitoring resumed")


async def _handle_tribe_unknown(cfg: Any, config_path: Path, msg: dict) -> None:
    """Server reported the (tribe_name, server_id) is unknown for this user.

    Show a Win32 dialog so the operator can pick whether to keep an
    existing tribe, rename one to the newly-detected name, or create a
    brand-new tribe — and then call the matching server REST API.
    """
    log = logging.getLogger(__name__)
    detected = (msg.get("detected_name") or "").strip()
    server_id = (msg.get("server_id") or "").strip()
    candidates = msg.get("candidates") or []
    if not detected or not server_id:
        log.warning("tribe_unknown without detected_name/server_id, ignoring: %r", msg)
        return

    from tribewatch.config import client_config_path, save_config
    from tribewatch import server_api

    save_path = client_config_path(config_path)
    save_mode = "client"
    server_url = cfg.server.server_url
    client_token = cfg.server.client_token
    if not client_token:
        log.warning("tribe_unknown but no client_token configured — cannot call server API")
        return

    title = "TribeWatch \u2014 Tribe Setup"

    loop = asyncio.get_event_loop()

    if len(candidates) == 1:
        cand = candidates[0]
        cand_name = cand.get("tribe_name", "")
        cand_id = int(cand.get("tribe_id") or 0)
        body = (
            f'Server doesn\'t recognise tribe "{detected}" on this ARK server.\n\n'
            f'Your existing tribe here:\n'
            f'    "{cand_name}"'
        )
        choice = await loop.run_in_executor(
            None,
            _custom_button_dialog,
            title,
            body,
            [
                (f'Rename "{cand_name}" → "{detected}"', "rename"),
                (f'Create "{detected}" as new tribe', "new"),
                (f'Keep "{cand_name}" (ignore detected)', "keep"),
            ],
            "keep",
        )
        try:
            if choice == "rename" and cand_id:
                log.info("tribe_unknown: renaming tribe_id=%d %r -> %r",
                         cand_id, cand_name, detected)
                await server_api.rename_tribe(
                    server_url, client_token,
                    tribe_id=cand_id, new_name=detected,
                )
                cfg.tribe.tribe_name = detected
                await loop.run_in_executor(
                    None, lambda: save_config(cfg, save_path, mode=save_mode),
                )
            elif choice == "new":
                log.info("tribe_unknown: creating new tribe %r on %r", detected, server_id)
                await server_api.claim_tribe(
                    server_url, client_token, name=detected, server_id=server_id,
                )
                cfg.tribe.tribe_name = detected
                await loop.run_in_executor(
                    None, lambda: save_config(cfg, save_path, mode=save_mode),
                )
            else:
                log.info("tribe_unknown: keeping existing tribe %r", cand_name)
                cfg.tribe.tribe_name = cand_name
                await loop.run_in_executor(
                    None, lambda: save_config(cfg, save_path, mode=save_mode),
                )
        except Exception:
            log.exception("tribe_unknown action failed")
        return

    if not candidates:
        body = (
            f'Server doesn\'t recognise tribe "{detected}" on this ARK server,\n'
            f'and you don\'t have any other tribes here yet.'
        )
        choice = await loop.run_in_executor(
            None,
            _custom_button_dialog,
            title,
            body,
            [
                (f'Create "{detected}" as new tribe', "create"),
                ("Cancel", "cancel"),
            ],
            "cancel",
        )
        if choice == "create":
            try:
                log.info("tribe_unknown: claiming new tribe %r on %r", detected, server_id)
                await server_api.claim_tribe(
                    server_url, client_token, name=detected, server_id=server_id,
                )
                cfg.tribe.tribe_name = detected
                await loop.run_in_executor(
                    None, lambda: save_config(cfg, save_path, mode=save_mode),
                )
            except Exception:
                log.exception("tribe_unknown claim failed")
        else:
            log.info("tribe_unknown: user cancelled new tribe creation")
        return

    # Multiple candidates — too ambiguous for a MessageBox; tell the
    # operator to use the dashboard. Logged once per occurrence.
    cand_str = ", ".join(c.get("tribe_name", "?") for c in candidates)
    log.warning(
        "tribe_unknown with %d candidates (%s) — please rename or create from the dashboard.",
        len(candidates), cand_str,
    )


def _handle_reconnect(app: Any, auto: bool = False, use_browser: bool | None = None) -> None:
    """Start a reconnect sequence (if not already running)."""
    from tribewatch.reconnect import ReconnectSequence

    log = logging.getLogger(__name__)
    existing = getattr(app, "_reconnect_seq", None)
    if existing is not None and existing.running:
        if not auto:
            # Manual reconnect from website/API — cancel the stale sequence
            # (which may be sitting in a long backoff sleep) and start fresh.
            log.info("Cancelling existing reconnect sequence for manual retry")
            asyncio.create_task(existing.cancel())
        else:
            log.info("Reconnect already in progress, ignoring")
            return

    relay = getattr(app, "_relay", None)
    if not relay:
        log.warning("Reconnect requested but no relay available")
        return

    fail_count = getattr(app, "_reconnect_fail_count", 0)
    if use_browser is None:
        use_browser = auto and fail_count >= 2

    window_title = getattr(app.config.general, "window_title", "ArkAscended")
    ocr_engine = getattr(app.config.tribe_log, "ocr_engine", "paddleocr")
    seq = ReconnectSequence(
        window_title=window_title,
        relay=relay,
        ocr_engine=ocr_engine,
        auto=auto,
        use_browser=use_browser,
    )
    app._reconnect_seq = seq
    task = seq.start()
    log.info("Reconnect sequence started (auto=%s, browser=%s)", auto, use_browser)

    def _on_done(_task: asyncio.Task) -> None:
        if seq.succeeded:
            app._reconnect_fail_count = 0
            log.info("Reconnect succeeded — fail counter reset")
        elif auto:
            app._reconnect_fail_count = getattr(app, "_reconnect_fail_count", 0) + 1
            log.info(
                "Reconnect failed — fail counter now %d",
                app._reconnect_fail_count,
            )

    task.add_done_callback(_on_done)




def _cmd_run_client(cfg: object, config_path: Path) -> None:
    """Client mode: capture pipeline + relay to remote server.

    Discord webhooks are NOT sent in client mode — the server handles dispatch.
    The client toml only contains client-relevant sections; server-owned
    sections (discord, alerts, web, generator) are never written.
    """
    log = logging.getLogger(__name__)
    log.info("TribeWatch client v%s starting", __version__)

    from dataclasses import asdict

    from tribewatch.app import TribeWatchApp
    from tribewatch.config import (
        _build_section,
        _CLIENT_SECTIONS,
        load_config,
        save_config,
        validate_config,
    )
    from tribewatch.relay import ServerRelay

    # Soft-restart flag — set by _handle_restart, drained by the run loop.
    # Allows the dashboard's restart button to tear down + rebuild the app
    # in-process without exiting the OS process. Critical for the frozen
    # PyInstaller exe where spawning a fresh child is unreliable
    # (the parent's MEIPASS temp dir gets cleaned up on exit).
    _restart_requested = {"value": False}

    # Blank out Discord webhooks — server handles dispatch, not the client
    cfg.discord.alert_webhook = ""
    cfg.discord.raid_webhook = ""
    cfg.discord.debug_webhook = ""

    # Rewrite toml with only client sections (strips any server leftovers)
    save_config(cfg, config_path, mode="client")

    def _on_control(command: str, msg_id: str) -> None:
        if command == "pause":
            app._paused = True
        elif command == "resume":
            app._paused = False
        elif command == "flush":
            asyncio.create_task(app.dispatcher.flush_batch())
        elif command == "screenshot":
            asyncio.create_task(_handle_screenshot(app, msg_id))
        elif command == "reconnect":
            app._reconnect_fail_count = 0
            _handle_reconnect(app)
        elif command == "reconnect_browser":
            app._reconnect_fail_count = 0
            _handle_reconnect(app, use_browser=True)
        elif command == "reconnect_cancel":
            asyncio.create_task(_handle_reconnect_cancel(app))
        elif command == "restart":
            # In-process soft restart — flag the run loop and stop the
            # current app. The wrapping `while _restart_requested[...]`
            # loop in _run_client tears down the relay/overlay and
            # rebuilds a fresh TribeWatchApp without spawning a new
            # process. Required for frozen exe builds where the
            # PyInstaller MEIPASS temp dir doesn't survive a parent exit.
            log = logging.getLogger(__name__)
            log.warning("Restart requested via remote control — soft-restarting in place")
            _restart_requested["value"] = True
            try:
                app.stop()
            except Exception:
                log.debug("app.stop() during restart failed", exc_info=True)

    def _on_config_update(section: str, data: dict, msg_id: str) -> None:
        # Only save client-owned sections
        if section not in _CLIENT_SECTIONS:
            logging.getLogger(__name__).debug(
                "Ignoring server-owned config update for [%s]", section,
            )
            return
        try:
            current_cfg = load_config(config_path)
            from tribewatch.config import (
                GeneralConfig,
                ParasaurConfig,
                ReconnectConfig,
                ServerConfig,
                TribeConfig,
                TribeLogConfig,
            )
            _section_cls = {
                "tribe_log": (TribeLogConfig, "tribe_log"),
                "general": (GeneralConfig, "general"),
                "parasaur": (ParasaurConfig, "parasaur"),
                "tribe": (TribeConfig, "tribe"),
                "server": (ServerConfig, "server"),
                "reconnect": (ReconnectConfig, "reconnect"),
            }
            entry = _section_cls.get(section)
            if entry:
                cls, attr = entry
                current = asdict(getattr(current_cfg, attr))
                current.update(data)
                new_section = _build_section(cls, current)
                setattr(current_cfg, attr, new_section)
                validate_config(current_cfg)
                save_config(current_cfg, config_path, mode="client")
                app.config = current_cfg
        except Exception:
            logging.getLogger(__name__).exception("Config update from server failed")
            raise

    async def _do_client_oauth() -> None:
        """Run Discord OAuth flow to obtain a client token."""
        from tribewatch.client_auth import obtain_client_token_interactive
        _log = logging.getLogger(__name__)
        _log.info("Client token missing or expired — starting Discord OAuth...")

        token = await obtain_client_token_interactive(
            cfg.server.server_url,
            tribe_hint=cfg.tribe.tribe_name or "",
        )
        if token:
            cfg.server.client_token = token
            save_config(cfg, config_path, mode="client")
            _log.info("Client token saved to %s", config_path)
        else:
            _log.error("No client token received — cannot authenticate")

    async def _run_client() -> None:
        nonlocal app, cfg

        # Auto-trigger OAuth if no client_token (one-time, before loop)
        if not cfg.server.client_token:
            await _do_client_oauth()
            if not cfg.server.client_token:
                logging.getLogger(__name__).error(
                    "Cannot start client without authentication. "
                    "Run again to retry Discord OAuth."
                )
                return

        def _on_auth_expired() -> None:
            """Called by relay when server rejects an expired token."""
            logging.getLogger(__name__).warning(
                "Client token expired — will re-authenticate on next connect"
            )
            asyncio.ensure_future(_reauth_and_reconnect())

        async def _reauth_and_reconnect() -> None:
            await _do_client_oauth()
            # Always unblock reconnection (set_client_token clears the auth gate)
            relay.set_client_token(cfg.server.client_token)

        # Soft-restart loop. The body builds a fresh relay + TribeWatchApp,
        # runs until app.stop() (either user shutdown or remote restart),
        # cleans up, then either breaks or loops to rebuild.
        while True:
            async def _on_tribe_unknown(msg: dict) -> None:
                await _handle_tribe_unknown(cfg, config_path, msg)

            relay = ServerRelay(
                server_url=cfg.server.server_url,
                auth_token=cfg.server.auth_token,
                client_token=cfg.server.client_token,
                reconnect_delay=cfg.server.reconnect_delay,
                on_control=_on_control,
                on_config_update=_on_config_update,
                on_auth_expired=_on_auth_expired,
                on_tribe_unknown=_on_tribe_unknown,
            )
            app = TribeWatchApp(cfg, relay=relay)
            app._auto_reconnect_cb = lambda: _handle_reconnect(app, auto=True)

            # Start overlay if enabled
            try:
                from tribewatch.overlay import StatusOverlay
                overlay = StatusOverlay(window_title=cfg.general.window_title)
                overlay.start()
                app._overlay = overlay
                log.info("Status overlay started")
            except Exception:
                log.debug("Overlay not available", exc_info=True)

            def _on_server_change(old_id, old_name, new_id, new_name):
                app._paused = True  # pause immediately (sync) before async handler
                app._eos_last_query = 0  # force EOS refresh on next heartbeat
                asyncio.create_task(
                    _handle_server_change(app, cfg, config_path, old_name, new_id, new_name)
                )

            app._on_server_change_cb = _on_server_change

            def _on_tribe_name_change(old_name, detected_name):
                app._paused = True
                asyncio.create_task(
                    _handle_tribe_name_change(app, cfg, config_path, old_name, detected_name)
                )

            app._on_tribe_name_change_cb = _on_tribe_name_change

            await relay.start()

            # Send initial config snapshot
            from dataclasses import asdict as _asdict
            await relay.send_config(_asdict(cfg))

            try:
                await app.run()
            finally:
                await relay.stop()
                # Stop the overlay window/thread so the rebuilt app can
                # claim a fresh one without two overlapping floating
                # windows.
                try:
                    if getattr(app, "_overlay", None) is not None:
                        app._overlay.stop()
                        app._overlay = None
                except Exception:
                    log.debug("Overlay stop after run loop failed", exc_info=True)

            if not _restart_requested["value"]:
                break

            # Soft restart — drain the flag, reload config from disk so
            # any changes made via remote settings updates are picked up,
            # and rebuild on the next loop iteration.
            _restart_requested["value"] = False
            log.warning("Soft restart: reloading config and rebuilding app")
            try:
                cfg = load_config(config_path)
                # Re-blank server-owned discord webhooks (same as initial setup)
                cfg.discord.alert_webhook = ""
                cfg.discord.raid_webhook = ""
            except Exception:
                log.exception("Config reload during soft restart failed — keeping old config")
            # Brief pause so the overlay thread/window has time to fully die
            await asyncio.sleep(0.3)

    app = None  # type: ignore[assignment]
    try:
        asyncio.run(_run_client())
    except KeyboardInterrupt:
        print("\nShutting down...")




def _cmd_reset_calibration(config_path: Path) -> None:
    """Clear manual calibration so resolution presets re-apply on next run."""
    from tribewatch.config import client_config_path
    cp = client_config_path(config_path)
    if not cp.exists():
        print(f"No config file found at {cp}")
        return
    import tomllib
    import tomli_w
    with open(cp, "rb") as f:
        data = tomllib.load(f)
    data.get("general", {}).pop("calibration_resolution", None)
    for section in ("tribe_log", "parasaur", "tribe"):
        data.get(section, {}).pop("bbox", None)
    with open(cp, "wb") as f:
        tomli_w.dump(data, f)
    print(f"Calibration reset. Bboxes cleared from {cp}")
    print("Run TribeWatch again — resolution presets will be auto-applied.")


def _cmd_reset_all(config_path: Path) -> None:
    """Delete client config, dedup state, calibration previews, and debug folder."""
    from tribewatch.config import client_config_path
    cp = client_config_path(config_path)
    removed: list[Path] = []

    if cp.exists():
        cp.unlink()
        removed.append(cp)

    work_dir = cp.parent
    for pattern in (
        "tribewatch_state*.json",
        "tribewatch_state*.json.tmp",
        "tribewatch_calibration_preview.png",
        "parasaur_calibration_preview.png",
        "tribe_calibration_preview.png",
    ):
        for f in work_dir.glob(pattern):
            try:
                f.unlink()
                removed.append(f)
            except Exception:
                pass

    debug_dir = work_dir / "debug"
    if debug_dir.exists():
        for f in debug_dir.iterdir():
            try:
                f.unlink()
            except Exception:
                pass
        try:
            debug_dir.rmdir()
            removed.append(debug_dir)
        except Exception:
            pass

    print("Full reset complete. Removed:")
    for p in removed:
        print(f"  - {p}")
    print()
    print("Run TribeWatch again to start fresh — you'll go through OAuth and calibration again.")


def main() -> None:
    _set_dpi_awareness()
    _set_console_title_and_icon()

    parser = argparse.ArgumentParser(
        prog="tribewatch",
        description="TribeWatch — ARK: Survival Ascended tribe log monitor",
    )
    parser.add_argument(
        "--version", action="version", version=f"TribeWatch {__version__}"
    )
    parser.add_argument(
        "--config", "-c",
        type=Path,
        default=Path(DEFAULT_CONFIG),
        help=f"Config file path (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Guided setup wizard — calibrate all screen regions step by step",
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="Calibrate screen capture region (visual drag-to-select overlay)",
    )
    parser.add_argument(
        "--calibrate-manual",
        action="store_true",
        help="Calibrate screen capture region (manual coordinate entry)",
    )
    parser.add_argument(
        "--calibrate-parasaur",
        action="store_true",
        help="Calibrate parasaur detection notification region",
    )
    parser.add_argument(
        "--calibrate-tribe",
        action="store_true",
        help="Calibrate tribe window capture region",
    )
    parser.add_argument(
        "--reset-calibration",
        action="store_true",
        help="Reset screen regions to resolution defaults (discards manual calibration)",
    )
    parser.add_argument(
        "--reset-all",
        action="store_true",
        help="Full reset: deletes client config, calibration, dedup state, and local caches",
    )
    parser.add_argument(
        "--test-ocr",
        action="store_true",
        help="Capture once, run OCR, print results, exit",
    )
    parser.add_argument(
        "--test-discord",
        action="store_true",
        help="Send test event to configured webhooks",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Run in default mode (used by startup entry / installer)",
    )

    args = parser.parse_args()
    config_path: Path = args.config

    # Setup / calibrate commands don't need an existing config
    if args.setup:
        # cmd_setup returns None; treat any non-False return as success and
        # fall through so the client launches automatically after calibration.
        result = _cmd_setup(config_path)
        if result is False:
            return

    if args.reset_calibration:
        _cmd_reset_calibration(config_path)
        return

    if args.reset_all:
        _cmd_reset_all(config_path)
        return

    if args.calibrate:
        _cmd_calibrate(config_path)
        return

    if args.calibrate_manual:
        _cmd_calibrate_manual(config_path)
        return

    if args.calibrate_parasaur:
        _cmd_calibrate_parasaur(config_path)
        return

    if args.calibrate_tribe:
        _cmd_calibrate_tribe(config_path)
        return

    # First run: generate default client config
    from tribewatch.config import client_config_path
    effective_path = client_config_path(config_path)
    if not effective_path.exists():
        _cmd_generate_config(effective_path, mode="client")
        return

    if args.test_ocr:
        _cmd_test_ocr(config_path)
    elif args.test_discord:
        _cmd_test_discord(config_path)
    else:
        # If we just ran the setup wizard explicitly, don't let _cmd_run
        # re-trigger it again via the unverified-resolution gate.
        _cmd_run(config_path, skip_unverified_setup=bool(args.setup))


if __name__ == "__main__":
    main()
