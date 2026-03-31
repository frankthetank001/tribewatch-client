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


def _focus_game_window(config_path: Path) -> None:
    """Try to bring the game window to the foreground before calibration."""
    import time
    import tomllib

    if not config_path.exists():
        return

    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return

    window_title = data.get("general", {}).get("window_title", "")
    if not window_title:
        return

    from tribewatch.capture import focus_window

    if focus_window(window_title):
        print(f"Focused window: '{window_title}'")
        time.sleep(0.5)
    else:
        print(f"Window not found: '{window_title}' — make sure the game is running")


def _save_bbox(
    config_path: Path,
    bbox: list[int],
    *,
    section: str = "tribe_log",
    preview_name: str = "tribewatch_calibration_preview.png",
) -> None:
    """Save a bbox to the client config file and capture a preview screenshot.

    Writes to the derived client config file (e.g. tribewatch_client.toml)
    so that server-side saves never overwrite calibration data.
    """
    import tomllib

    import tomli_w

    from tribewatch.config import client_config_path

    cp = client_config_path(config_path)
    if cp.exists():
        with open(cp, "rb") as f:
            data = tomllib.load(f)
    else:
        data = {}

    data.setdefault(section, {})["bbox"] = bbox

    # Record the game resolution at calibration time for dynamic scaling
    try:
        from tribewatch.server_id import get_game_resolution

        res = get_game_resolution()
        if res:
            data.setdefault("general", {})["calibration_resolution"] = list(res)
    except Exception:
        pass

    with open(cp, "wb") as f:
        tomli_w.dump(data, f)

    print(f"\nCalibrated: {section}.bbox = {bbox}")
    print(f"Saved to {cp}")

    # Quick preview capture
    try:
        from tribewatch.capture import ScreenCapture

        cap = ScreenCapture(bbox)
        img = cap.grab()
        cap.close()
        if img:
            preview_path = Path(preview_name)
            img.save(preview_path)
            print(f"Preview saved to {preview_path}")
    except Exception:
        pass


def _cmd_calibrate(config_path: Path) -> None:
    from tribewatch.calibrate import get_default_bbox

    print("=== TribeWatch Calibration (Visual Overlay) ===")
    print()

    _focus_game_window(config_path)

    default = get_default_bbox()
    print(f"Suggested default for your resolution: {default}")
    print("A fullscreen overlay will appear. Drag a rectangle over the tribe log.")
    print("Press Escape to cancel.")
    print()

    try:
        from tribewatch.calibrate import run_overlay

        bbox = run_overlay()
    except Exception as exc:
        print(f"Overlay failed ({exc}). Falling back to manual input.")
        _cmd_calibrate_manual(config_path)
        return

    if bbox is None:
        print("Calibration cancelled.")
        return

    _save_bbox(config_path, bbox)


def _cmd_calibrate_manual(config_path: Path) -> None:
    print("=== TribeWatch Calibration (Manual) ===")
    print()
    print("Position your ARK window with the tribe log visible.")
    print("You need to enter the pixel coordinates of the tribe log region.")
    print()
    print("Tip: Use Windows Snipping Tool or Paint to find coordinates.")
    print("The region should cover the tribe log text area (not the title bar).")
    print()

    try:
        left = int(input("Left edge (x):   "))
        top = int(input("Top edge (y):    "))
        right = int(input("Right edge (x):  "))
        bottom = int(input("Bottom edge (y): "))
    except (ValueError, EOFError):
        print("Invalid input. Calibration cancelled.")
        sys.exit(1)

    if left >= right or top >= bottom:
        print("Invalid region: left must be < right, top must be < bottom.")
        sys.exit(1)

    bbox = [left, top, right, bottom]
    _save_bbox(config_path, bbox)


def _cmd_calibrate_parasaur(config_path: Path) -> None:
    from tribewatch.calibrate import get_default_bbox

    print("=== TribeWatch Parasaur Detection Calibration ===")
    print()
    _focus_game_window(config_path)
    print("Select the screen region where parasaur detection notifications appear.")
    print("These are the top-of-screen messages like:")
    print('  "Rex" - Lvl 20 (Parasaur) detected an enemy!')
    print()
    print("A fullscreen overlay will appear. Drag a rectangle over the notification area.")
    print("Press Escape to cancel.")
    print()

    try:
        from tribewatch.calibrate import run_overlay

        bbox = run_overlay()
    except Exception as exc:
        print(f"Overlay failed ({exc}). Falling back to manual input.")
        print()
        try:
            left = int(input("Left edge (x):   "))
            top = int(input("Top edge (y):    "))
            right = int(input("Right edge (x):  "))
            bottom = int(input("Bottom edge (y): "))
        except (ValueError, EOFError):
            print("Invalid input. Calibration cancelled.")
            sys.exit(1)
        if left >= right or top >= bottom:
            print("Invalid region: left must be < right, top must be < bottom.")
            sys.exit(1)
        bbox = [left, top, right, bottom]
        _save_bbox(config_path, bbox, section="parasaur", preview_name="parasaur_calibration_preview.png")
        return

    if bbox is None:
        print("Calibration cancelled.")
        return

    _save_bbox(config_path, bbox, section="parasaur", preview_name="parasaur_calibration_preview.png")


def _cmd_calibrate_tribe(config_path: Path) -> None:
    print("=== TribeWatch Tribe Window Calibration ===")
    print()
    _focus_game_window(config_path)
    print("Select the screen region where the tribe window is displayed.")
    print("This window shows: tribe name, members online count, and member list.")
    print()
    print("A fullscreen overlay will appear. Drag a rectangle over the tribe window.")
    print("Press Escape to cancel.")
    print()

    try:
        from tribewatch.calibrate import run_overlay

        bbox = run_overlay()
    except Exception as exc:
        print(f"Overlay failed ({exc}). Falling back to manual input.")
        print()
        try:
            left = int(input("Left edge (x):   "))
            top = int(input("Top edge (y):    "))
            right = int(input("Right edge (x):  "))
            bottom = int(input("Bottom edge (y): "))
        except (ValueError, EOFError):
            print("Invalid input. Calibration cancelled.")
            sys.exit(1)
        if left >= right or top >= bottom:
            print("Invalid region: left must be < right, top must be < bottom.")
            sys.exit(1)
        bbox = [left, top, right, bottom]
        _save_bbox(config_path, bbox, section="tribe", preview_name="tribe_calibration_preview.png")
        return

    if bbox is None:
        print("Calibration cancelled.")
        return

    _save_bbox(config_path, bbox, section="tribe", preview_name="tribe_calibration_preview.png")


def _cmd_setup(config_path: Path) -> None:
    """Guided setup wizard that calibrates all three regions in sequence."""
    import tomllib

    from tribewatch.config import client_config_path

    # Load existing config data from the CLIENT config (where bboxes are saved)
    cp = client_config_path(config_path)
    if cp.exists():
        with open(cp, "rb") as f:
            data = tomllib.load(f)
    else:
        data = {}

    # Focus the game window before calibration so it's visible behind the overlay
    _focus_game_window(config_path)

    # Step definitions: (config_section, label, description, preview_name)
    steps = [
        (
            "tribe_log",
            "Tribe Log",
            "The main tribe log text area in ARK.",
            "tribewatch_calibration_preview.png",
        ),
        (
            "parasaur",
            "Parasaur",
            "The top-of-screen area where parasaur detection alerts appear.",
            "parasaur_calibration_preview.png",
        ),
        (
            "tribe",
            "Tribe Window",
            "The tribe member list showing online/offline status.",
            "tribe_calibration_preview.png",
        ),
    ]

    # Track current bboxes (label -> bbox) across steps
    current: dict[str, list[int]] = {}
    for section, label, _desc, _pname in steps:
        bbox = data.get(section, {}).get("bbox")
        if bbox and len(bbox) == 4:
            current[label] = bbox

    # Track what changed for the summary
    results: list[tuple[str, list[int] | None, str]] = []  # (label, bbox, status)

    print()
    print("=== TribeWatch Setup Wizard ===")
    print()

    total = len(steps)
    for i, (section, label, description, preview_name) in enumerate(steps, 1):
        print(f"Step {i}/{total}: {label}")
        print(f"  {description}")

        existing_bbox = current.get(label)
        if existing_bbox:
            print(f"  Current: {existing_bbox}")
        else:
            print("  Current: not configured")
        print()

        # Build existing_bboxes for overlay: all configured regions except current step
        overlay_bboxes: dict[str, list[int]] = {}
        for other_label, other_bbox in current.items():
            if other_label == label:
                overlay_bboxes[f"{other_label} (current)"] = other_bbox
            else:
                overlay_bboxes[other_label] = other_bbox

        instruction = (
            f"Draw a rectangle over the {label} region. "
            f"Press Escape to {'keep current' if existing_bbox else 'skip'}."
        )

        try:
            from tribewatch.calibrate import run_overlay

            bbox = run_overlay(
                instruction=instruction,
                existing_bboxes=overlay_bboxes if overlay_bboxes else None,
            )
        except Exception as exc:
            print(f"  Overlay failed ({exc}). Skipping this step.")
            if existing_bbox:
                results.append((label, existing_bbox, "kept"))
            else:
                results.append((label, None, "skipped"))
            print()
            continue

        if bbox is None:
            # Escaped — keep current value
            if existing_bbox:
                print(f"  Skipped — keeping {existing_bbox}")
                results.append((label, existing_bbox, "kept"))
            else:
                print("  Skipped")
                results.append((label, None, "skipped"))
        else:
            _save_bbox(config_path, bbox, section=section, preview_name=preview_name)
            current[label] = bbox
            results.append((label, bbox, "new"))

        print()

    # Final summary
    print("Setup complete!")
    for label, bbox, status in results:
        if bbox:
            print(f"  {label + ':':<16} {bbox} ({status})")
        else:
            print(f"  {label + ':':<16} not configured ({status})")
    print()


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

    MB_OK = 0x00
    MB_YESNO = 0x04
    MB_YESNOCANCEL = 0x03
    MB_ICONQUESTION = 0x20
    MB_ICONINFORMATION = 0x40
    MB_TOPMOST = 0x40000
    IDYES = 6
    IDNO = 7

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
            result = ctypes.windll.user32.MessageBoxW(
                0,
                (
                    f"Tribe name mismatch:\n\n"
                    f'Saved:      "{saved}"\n'
                    f'Detected:  "{detected}"\n\n'
                    f"Update to the detected name?"
                ),
                title,
                MB_YESNO | MB_ICONQUESTION | MB_TOPMOST,
            )
            if result == IDYES:
                cfg.tribe.tribe_name = detected
                save_config(cfg, config_path, mode=mode)


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
            answer = input("Update tribe name? [y/N]: ").strip().lower()
            if answer in ("y", "yes"):
                cfg.tribe.tribe_name = detected
                save_config(cfg, config_path, mode=mode)
                print(f'Tribe name updated: "{detected}"')
            else:
                print(f'Keeping saved name: "{saved}"')

    print()


def _apply_resolution_preset(cfg: object) -> None:
    """Detect game resolution and auto-apply bbox presets if available.

    If the resolution is recognized, all three capture regions (tribe_log,
    parasaur, tribe) are overridden with the preset values.  If unknown,
    a warning is printed telling the user to calibrate.
    """
    log = logging.getLogger(__name__)
    try:
        from tribewatch.calibrate import get_preset
        from tribewatch.server_id import get_game_resolution

        resolution = get_game_resolution()
        if resolution is None:
            log.debug("Could not detect game resolution — skipping preset auto-apply")
            return

        preset = get_preset(resolution)
        if preset is None:
            log.warning(
                "No bbox preset for resolution %dx%d — run --setup to calibrate",
                resolution[0], resolution[1],
            )
            print(
                f"\n*** Unknown resolution {resolution[0]}x{resolution[1]} — "
                "run  python -m tribewatch --setup  to calibrate screen regions ***\n"
            )
            return

        # Apply preset bboxes to the live config
        cfg.tribe_log.bbox = list(preset["tribe_log"])
        cfg.parasaur.bbox = list(preset["parasaur"])
        cfg.tribe.bbox = list(preset["tribe"])

        # Store calibration resolution so heartbeat knows the baseline
        cfg.general.calibration_resolution = list(resolution)

        log.info(
            "Auto-applied bbox preset for %dx%d — tribe_log=%s parasaur=%s tribe=%s",
            resolution[0], resolution[1],
            preset["tribe_log"], preset["parasaur"], preset["tribe"],
        )
    except Exception:
        log.debug("Resolution preset auto-apply failed", exc_info=True)


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
        answer = input("\n  Download and install update now? [Y/n] ").strip().lower()
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
        input("  Press Enter to continue...")

    print()


def _cmd_run(config_path: Path) -> None:
    from tribewatch.config import client_config_path, load_config

    from dotenv import load_dotenv
    load_dotenv()

    # Client mode: load ONLY the client config file
    cp = client_config_path(config_path)
    cfg = load_config(cp)

    _apply_env_overrides(cfg)
    _setup_logging(cfg.general.log_level)

    # --- Auto-update check (frozen/installed builds only) ---
    from tribewatch.updater import is_frozen
    if is_frozen():
        _check_for_updates()

    _apply_resolution_preset(cfg)

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
        import ctypes

        MB_YESNOCANCEL = 0x03
        MB_ICONQUESTION = 0x20
        MB_TOPMOST = 0x40000
        IDYES = 6
        IDNO = 7
        # IDCANCEL = 2

        result = await asyncio.get_event_loop().run_in_executor(
            None,
            ctypes.windll.user32.MessageBoxW,
            0,
            (
                f"Tribe name changed!\n\n"
                f'Previous:  "{old_name}"\n'
                f'Detected:  "{detected_name}"\n\n'
                f"Yes = Rename existing tribe to new name\n"
                f"No = New tribe (keep old data separate)\n"
                f"Cancel = Ignore (keep using old name)"
            ),
            "TribeWatch \u2014 Tribe Name Changed",
            MB_YESNOCANCEL | MB_ICONQUESTION | MB_TOPMOST,
        )

        from tribewatch.config import client_config_path, save_config

        save_path = client_config_path(config_path)
        save_mode = "client"

        if result == IDYES:
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
                None, save_config, cfg, save_path, save_mode,
            )
        elif result == IDNO:
            # New tribe — just update the config, old data stays under old name
            log.info("User chose: new tribe %r (old data kept as %r)", detected_name, old_name)
            cfg.tribe.tribe_name = detected_name
            app.config.tribe.tribe_name = detected_name
            await asyncio.get_event_loop().run_in_executor(
                None, save_config, cfg, save_path, save_mode,
            )
        else:
            log.info("User chose: ignore tribe name change, keeping %r", old_name)
    except Exception:
        log.exception("Tribe name change handler failed")
    finally:
        app._tribe_name_change_pending = False
        app._paused = False
        log.info("Tribe name change handled — monitoring resumed")


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

        token = await obtain_client_token_interactive(cfg.server.server_url)
        if token:
            cfg.server.client_token = token
            save_config(cfg, config_path, mode="client")
            _log.info("Client token saved to %s", config_path)
        else:
            _log.error("No client token received — cannot authenticate")

    async def _run_client() -> None:
        nonlocal app

        # Auto-trigger OAuth if no client_token
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

        relay = ServerRelay(
            server_url=cfg.server.server_url,
            auth_token=cfg.server.auth_token,
            client_token=cfg.server.client_token,
            reconnect_delay=cfg.server.reconnect_delay,
            on_control=_on_control,
            on_config_update=_on_config_update,
            on_auth_expired=_on_auth_expired,
        )
        app = TribeWatchApp(cfg, relay=relay)
        app._auto_reconnect_cb = lambda: _handle_reconnect(app, auto=True)

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

    app = None  # type: ignore[assignment]
    try:
        asyncio.run(_run_client())
    except KeyboardInterrupt:
        print("\nShutting down...")




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
        _cmd_setup(config_path)
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
        _cmd_run(config_path)


if __name__ == "__main__":
    main()
