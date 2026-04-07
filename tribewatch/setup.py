"""Setup wizard and calibration commands (shared by all entry points)."""

from __future__ import annotations

import sys
from pathlib import Path


def _show_prompt(
    title: str,
    message: str,
    action_label: str | None = None,
    action_callback: callable | None = None,
) -> None:
    """Show a blocking GUI prompt.

    If *action_label* / *action_callback* are provided, an extra button is
    shown that runs the callback (e.g. "Open Tribe Log" → sends L key).
    """
    import tkinter as tk

    if action_label is None:
        # Simple messagebox
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showinfo(title, message, parent=root)
        root.destroy()
        return

    # Custom dialog with action button + OK
    root = tk.Tk()
    root.title(title)
    root.attributes("-topmost", True)
    root.resizable(False, False)

    frame = tk.Frame(root, padx=20, pady=15)
    frame.pack()

    tk.Label(
        frame, text=message, justify=tk.LEFT, wraplength=450,
        font=("Segoe UI", 11),
    ).pack(pady=(0, 15))

    btn_frame = tk.Frame(frame)
    btn_frame.pack()

    def on_action() -> None:
        if action_callback:
            action_callback()

    tk.Button(
        btn_frame, text=action_label, width=18,
        font=("Segoe UI", 11, "bold"),
        command=on_action,
    ).pack(side=tk.LEFT, padx=8)
    tk.Button(
        btn_frame, text="OK", width=10,
        font=("Segoe UI", 11),
        command=root.destroy,
    ).pack(side=tk.LEFT, padx=8)

    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_reqwidth()) // 2
    y = (root.winfo_screenheight() - root.winfo_reqheight()) // 2
    root.geometry(f"+{x}+{y}")

    # Force the dialog to the front and grab input focus on Windows
    root.lift()
    root.focus_force()
    root.grab_set()

    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()


def _get_window_title(config_path: Path) -> str:
    """Read the game window title from config, or return empty string.

    Checks both the given path and the client config path.
    """
    import tomllib

    from tribewatch.config import client_config_path

    for p in [config_path, client_config_path(config_path)]:
        if not p.exists():
            continue
        try:
            with open(p, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            continue
        title = data.get("general", {}).get("window_title", "")
        if title:
            return title
    return ""


def _focus_game_window(config_path: Path) -> None:
    """Try to bring the game window to the foreground before calibration."""
    import time

    window_title = _get_window_title(config_path)
    if not window_title:
        return

    from tribewatch.capture import focus_window, _find_window_by_title

    hwnd = _find_window_by_title(window_title)
    if not hwnd:
        print(f"Game window not found: '{window_title}' — make sure ARK is running")
        return

    if focus_window(window_title):
        print(f"Focused window: '{window_title}'")
    else:
        print(f"Game window found but couldn't bring to foreground — this is normal for fullscreen")
    time.sleep(0.5)


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


def cmd_setup(config_path: Path) -> None:
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

    # If the game resolution differs from the saved calibration resolution,
    # swap in the preset for the current resolution so the overlay shows
    # the right "existing" bboxes to keep/redraw.
    try:
        from tribewatch.server_id import get_game_resolution
        from tribewatch.calibrate import get_preset

        current_res = get_game_resolution()
        cal_res = data.get("general", {}).get("calibration_resolution")
        if current_res and cal_res and tuple(cal_res) != current_res:
            print(
                f"Resolution changed: calibrated at {cal_res[0]}x{cal_res[1]}, "
                f"game is now at {current_res[0]}x{current_res[1]}"
            )
            preset = get_preset(current_res)
            if preset:
                print("Using preset for current resolution as starting bboxes.")
                for section, key in (("tribe_log", "tribe_log"), ("parasaur", "parasaur"), ("tribe", "tribe")):
                    data.setdefault(section, {})["bbox"] = list(preset[key])
                data.setdefault("general", {})["calibration_resolution"] = list(current_res)
            else:
                print("No preset available for current resolution — existing bboxes cleared.")
                for section in ("tribe_log", "parasaur", "tribe"):
                    data.get(section, {}).pop("bbox", None)
            print()
    except Exception:
        pass

    # Focus the game window before calibration so it's visible behind the overlay
    _focus_game_window(config_path)

    # Helper to send a key to the game window
    window_title = _get_window_title(config_path)

    def _send_game_key(key: str) -> callable:
        def _send() -> None:
            if not window_title:
                return
            import time
            from tribewatch.capture import focus_window
            focus_window(window_title)
            time.sleep(0.5)
            try:
                import pyautogui
                pyautogui.press(key)
            except ImportError:
                from tribewatch.capture import send_key
                send_key(window_title, key)
        return _send

    # Step definitions
    steps = [
        {
            "section": "tribe_log",
            "label": "Tribe Log",
            "description": "The main tribe log text area in ARK.",
            "prompt_msg": 'Make sure to INCLUDE the "LOG" header text in the capture region.',
            "preview_name": "tribewatch_calibration_preview.png",
            "action_label": "Open Tribe Log",
            "action_callback": _send_game_key("l"),
            "example_url": "https://raw.githubusercontent.com/frankthetank001/tribewatch-client/main/docs/calibration_tribe_log.png",
        },
        {
            "section": "parasaur",
            "label": "Parasaur",
            "description": "The top-of-screen area where parasaur detection alerts appear.",
            "prompt_msg": (
                "You will now select the parasaur detection notification area.\n\n"
                "This is the top-of-screen area where alerts like\n"
                '"Players - Lvl 20 (Parasaur) detected an enemy!" appear.'
            ),
            "preview_name": "parasaur_calibration_preview.png",
            "action_label": None,
            "action_callback": None,
            "example_url": "https://raw.githubusercontent.com/frankthetank001/tribewatch-client/main/docs/calibration_parasaur.png",
        },
        {
            "section": "tribe",
            "label": "Tribe Members List",
            "description": "The tribe member list showing online/offline status.",
            "prompt_msg": "Make sure to INCLUDE your tribe name at the top of the capture region.",
            "preview_name": "tribe_calibration_preview.png",
            "action_label": None,
            "action_callback": None,
            "example_url": "https://raw.githubusercontent.com/frankthetank001/tribewatch-client/main/docs/calibration_tribe_members.png",
        },
    ]

    # Track current bboxes (label -> bbox) across steps
    current: dict[str, list[int]] = {}
    for step in steps:
        bbox = data.get(step["section"], {}).get("bbox")
        if bbox and len(bbox) == 4:
            current[step["label"]] = bbox

    # Track what changed for the summary
    results: list[tuple[str, list[int] | None, str]] = []  # (label, bbox, status)

    print()
    print("=== TribeWatch Setup Wizard ===")
    print()

    total = len(steps)
    for i, step in enumerate(steps, 1):
        section = step["section"]
        label = step["label"]
        description = step["description"]
        prompt_msg = step["prompt_msg"]
        preview_name = step["preview_name"]
        action_label = step["action_label"]
        action_callback = step["action_callback"]
        example_url = step.get("example_url")

        print(f"--- Step {i}/{total}: {label} ---")
        print(f"  {description}")

        existing_bbox = current.get(label)
        if existing_bbox:
            print(f"  Current: {existing_bbox}")
        else:
            print("  Current: not configured")
        print()

        draw_instruction = f"Draw a rectangle over the {label} region."

        _focus_game_window(config_path)

        # Show only the current step's existing bbox on the overlay
        overlay_bboxes: dict[str, list[int]] = {}
        if existing_bbox:
            overlay_bboxes[f"{label} (current)"] = existing_bbox

        # Always show the prompt dialog on the overlay so action buttons
        # (e.g. "Open Tribe Log") are available.  When an existing bbox is
        # present the dialog also offers Re-draw / Keep Current.
        overlay_prompt = f"{prompt_msg}\n\n{draw_instruction}"

        try:
            from tribewatch.calibrate import run_overlay

            bbox, kept = run_overlay(
                instruction=draw_instruction,
                existing_bboxes=overlay_bboxes if overlay_bboxes else None,
                prompt=overlay_prompt,
                action_label=action_label,
                action_callback=action_callback,
                example_url=example_url,
                window_title=window_title,
            )
        except Exception as exc:
            print(f"  Overlay failed ({exc}). Skipping this step.")
            if existing_bbox:
                results.append((label, existing_bbox, "kept"))
            else:
                results.append((label, None, "skipped"))
            print()
            continue

        if kept:
            print(f"  Keeping current: {existing_bbox}")
            results.append((label, existing_bbox, "kept"))
        elif bbox is None:
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

    return True


def cmd_calibrate(config_path: Path) -> None:
    from tribewatch.calibrate import get_default_bbox

    print("=== TribeWatch Calibration (Visual Overlay) ===")
    print()

    _focus_game_window(config_path)

    default = get_default_bbox()
    print(f"Suggested default for your resolution: {default}")
    print("A fullscreen overlay will appear. Drag a rectangle over the tribe log.")
    print()

    try:
        from tribewatch.calibrate import run_overlay
        from tribewatch.config import load_config

        cfg = load_config(config_path)
        existing_bbox = cfg.tribe_log.bbox if cfg.tribe_log.bbox else None
        overlay_bboxes = {}
        prompt_text = "Select the tribe log area."
        if existing_bbox:
            overlay_bboxes["tribe log (current)"] = existing_bbox
            prompt_text += "\n\nYou already have a region configured."

        bbox, _kept = run_overlay(
            instruction="Draw a rectangle over the tribe log.",
            existing_bboxes=overlay_bboxes if overlay_bboxes else None,
            prompt=prompt_text,
            example_url="https://raw.githubusercontent.com/frankthetank001/tribewatch-client/main/docs/calibration_tribe_log.png",
            window_title=cfg.general.window_title,
        )
    except Exception as exc:
        print(f"Overlay failed ({exc}). Falling back to manual input.")
        cmd_calibrate_manual(config_path)
        return

    if bbox is None:
        if _kept:
            print("Keeping current calibration.")
        else:
            print("Calibration cancelled.")
        return

    _save_bbox(config_path, bbox)


def cmd_calibrate_manual(config_path: Path) -> None:
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


def cmd_calibrate_parasaur(config_path: Path) -> None:
    print("=== TribeWatch Parasaur Detection Calibration ===")
    print()
    _focus_game_window(config_path)
    print("Select the screen region where parasaur detection notifications appear.")
    print("These are the top-of-screen messages like:")
    print('  "Players" - Lvl 20 (Parasaur) detected an enemy!')
    print()
    print("A fullscreen overlay will appear. Drag a rectangle over the notification area.")
    print()

    try:
        from tribewatch.calibrate import run_overlay
        from tribewatch.config import load_config

        cfg = load_config(config_path)
        existing_bbox = cfg.parasaur.bbox if cfg.parasaur.bbox else None
        overlay_bboxes = {}
        prompt_text = "Select the parasaur detection notification area."
        if existing_bbox:
            overlay_bboxes["parasaur (current)"] = existing_bbox
            prompt_text += "\n\nYou already have a region configured."

        bbox, _kept = run_overlay(
            instruction="Draw a rectangle over the parasaur notification area.",
            existing_bboxes=overlay_bboxes if overlay_bboxes else None,
            prompt=prompt_text,
            example_url="https://raw.githubusercontent.com/frankthetank001/tribewatch-client/main/docs/calibration_parasaur.png",
            window_title=cfg.general.window_title,
        )
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
        if _kept:
            print("Keeping current calibration.")
        else:
            print("Calibration cancelled.")
        return

    _save_bbox(config_path, bbox, section="parasaur", preview_name="parasaur_calibration_preview.png")


def cmd_calibrate_tribe(config_path: Path) -> None:
    print("=== TribeWatch Tribe Members List Calibration ===")
    print()
    _focus_game_window(config_path)
    print("Select the screen region where the tribe members list is displayed.")
    print("This window shows: tribe name, members online count, and member list.")
    print()
    print("A fullscreen overlay will appear. Drag a rectangle over the tribe members list.")
    print()

    try:
        from tribewatch.calibrate import run_overlay
        from tribewatch.config import load_config

        cfg = load_config(config_path)
        existing_bbox = cfg.tribe.bbox if cfg.tribe.bbox else None
        overlay_bboxes = {}
        prompt_text = "Select the tribe members list area."
        if existing_bbox:
            overlay_bboxes["tribe members (current)"] = existing_bbox
            prompt_text += "\n\nYou already have a region configured."

        bbox, _kept = run_overlay(
            instruction="Draw a rectangle over the tribe members list.",
            existing_bboxes=overlay_bboxes if overlay_bboxes else None,
            prompt=prompt_text,
            example_url="https://raw.githubusercontent.com/frankthetank001/tribewatch-client/main/docs/calibration_tribe_members.png",
            window_title=cfg.general.window_title,
        )
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
        if _kept:
            print("Keeping current calibration.")
        else:
            print("Calibration cancelled.")
        return

    _save_bbox(config_path, bbox, section="tribe", preview_name="tribe_calibration_preview.png")
