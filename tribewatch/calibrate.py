"""Drag-to-select calibration overlay using tkinter.

Provides a transparent fullscreen overlay where the user can draw a rectangle
over the tribe log region, replacing the old manual coordinate entry.

Also provides resolution-based bbox presets for known resolutions.
"""

from __future__ import annotations

import ctypes
import logging
from typing import Any, Optional

try:
    import tkinter as tk
    from tkinter import messagebox
except ImportError:
    tk = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resolution presets — verified bboxes for all three capture regions.
# Key: (width, height), Value: {section: [left, top, right, bottom]}
# ---------------------------------------------------------------------------

_RESOLUTION_PRESETS: dict[tuple[int, int], dict[str, list[int]]] = {
    (1280, 720): {
        "tribe_log": [1144, 241, 1419, 706],
        "parasaur": [936, 156, 1603, 187],
        "tribe": [759, 274, 1115, 748],
    },
    (1920, 1080): {
        "tribe_log": [1078, 127, 1486, 829],
        "parasaur": [662, 3, 1842, 43],
        "tribe": [498, 174, 1040, 899],
    },
    (2560, 1080): {
        "tribe_log": [1079, 129, 1481, 825],
        "parasaur": [457, 7, 1776, 52],
        "tribe": [501, 185, 1029, 890],
    },
}

# Legacy: tribe-log-only known bboxes (kept for get_default_bbox compat)
_KNOWN_BBOXES: dict[tuple[int, int], list[int]] = {
    res: preset["tribe_log"] for res, preset in _RESOLUTION_PRESETS.items()
}

_BASELINE_RES = (1920, 1080)


def _get_screen_resolution() -> tuple[int, int] | None:
    """Return (width, height) of the primary monitor, or None on failure."""
    try:
        user32 = ctypes.windll.user32
        return (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
    except Exception:
        return None


def get_preset(resolution: tuple[int, int] | None = None) -> dict[str, list[int]] | None:
    """Return the full preset (all three regions) for *resolution*, or None.

    Pass *resolution* explicitly or omit to auto-detect from the game INI.
    """
    if resolution is None:
        try:
            from tribewatch.server_id import get_game_resolution
            resolution = get_game_resolution()
        except Exception:
            pass
    if resolution is None:
        return None
    return _RESOLUTION_PRESETS.get(resolution)


def get_default_bbox(resolution: tuple[int, int] | None = None) -> list[int]:
    """Return the best tribe log bbox for the given (or detected) screen resolution.

    Lookup order:
    1. Exact match in presets
    2. Proportional scale from the 1920x1080 baseline
    """
    if resolution is None:
        resolution = _get_screen_resolution()

    baseline_bbox = _RESOLUTION_PRESETS[_BASELINE_RES]["tribe_log"]

    if resolution is None:
        return list(baseline_bbox)

    preset = _RESOLUTION_PRESETS.get(resolution)
    if preset:
        return list(preset["tribe_log"])

    # Scale from baseline
    scale_x = resolution[0] / _BASELINE_RES[0]
    scale_y = resolution[1] / _BASELINE_RES[1]
    left, top, right, bottom = baseline_bbox
    return [
        int(left * scale_x),
        int(top * scale_y),
        int(right * scale_x),
        int(bottom * scale_y),
    ]


class _OverlayApp:
    """Transparent fullscreen overlay for drag-to-select region picking."""

    # Color scheme for existing bbox labels
    _LABEL_COLORS: dict[str, str] = {
        "Tribe Log": "#00CC00",
        "Parasaur": "#00CCCC",
        "Tribe Window": "#CC00CC",
    }
    _DEFAULT_COLOR = "#CCCC00"

    def __init__(
        self,
        instruction: str = "Click and drag to select the region. Press Escape to cancel.",
        existing_bboxes: dict[str, list[int]] | None = None,
        prompt: str | None = None,
        action_label: str | None = None,
        action_callback: callable | None = None,
        example_url: str | None = None,
    ) -> None:
        self.result: Optional[list[int]] = None
        self._start_x = 0
        self._start_y = 0
        self._rect_id: Optional[int] = None
        self._kept = False  # True if user clicked "Keep Current"
        self._instruction = instruction
        # Check if any existing bbox is tagged "(current)" — meaning the
        # user already has a calibration for this step and can keep it.
        self._has_current = any(
            "(current)" in lbl for lbl in (existing_bboxes or {})
        )
        self._example_url = example_url

        self.root = tk.Tk()
        self.root.title("TribeWatch Calibration")
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.3)
        self.root.configure(background="black")
        self.root.overrideredirect(True)

        # Canvas fills the whole screen — start with default cursor;
        # switch to crosshair only when drawing is enabled.
        self.canvas = tk.Canvas(
            self.root, bg="black", highlightthickness=0,
            cursor="arrow" if prompt else "crosshair",
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # Instruction text (hidden in prompt mode — shown in the prompt dialog instead)
        self._instruction_id = self.canvas.create_text(
            self.root.winfo_screenwidth() // 2,
            40,
            text="" if prompt else instruction,
            fill="white",
            font=("Segoe UI", 18, "bold"),
        )

        # Draw existing bboxes as colored rectangles with labels
        if existing_bboxes:
            for label, bbox in existing_bboxes.items():
                if len(bbox) != 4:
                    continue
                color = self._LABEL_COLORS.get(
                    label.replace(" (current)", ""), self._DEFAULT_COLOR
                )
                left, top, right, bottom = bbox
                self.canvas.create_rectangle(
                    left, top, right, bottom,
                    outline=color, width=2,
                )
                self.canvas.create_text(
                    left, top - 8,
                    text=label,
                    fill=color,
                    font=("Segoe UI", 12, "bold"),
                    anchor="sw",
                )

        # If prompt mode, show a dialog frame on the overlay instead of
        # enabling drawing immediately.  User picks Re-draw or Keep Current.
        self._prompt_widgets: list[int | tk.Widget] = []
        if prompt:
            self._show_overlay_prompt(prompt, action_label, action_callback)
        else:
            self._enable_drawing()

    def _show_overlay_prompt(
        self,
        prompt: str,
        action_label: str | None = None,
        action_callback: callable | None = None,
    ) -> None:
        """Show a prompt with Re-draw / Keep Current buttons on the overlay.

        Uses a separate Toplevel window so buttons aren't affected by the
        overlay's alpha transparency.
        """
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()

        # Create an opaque popup window on top of the transparent overlay
        self._prompt_win = tk.Toplevel(self.root)
        self._prompt_win.overrideredirect(True)
        self._prompt_win.attributes("-topmost", True)
        self._prompt_win.configure(bg="#1a1a1a")

        win_w = 650
        # Set initial position centered — will be refined after widgets are packed
        x0 = (sw - win_w) // 2
        y0 = (sh - 400) // 2
        self._prompt_win.geometry(f"{win_w}x400+{x0}+{y0}")

        # Gold border effect via inner frame
        border = tk.Frame(self._prompt_win, bg="#FFD700", padx=3, pady=3)
        border.pack(fill=tk.BOTH, expand=True)
        inner = tk.Frame(border, bg="#1a1a1a", padx=25, pady=20)
        inner.pack(fill=tk.BOTH, expand=True)

        tk.Label(
            inner, text=prompt, fg="white", bg="#1a1a1a",
            font=("Segoe UI", 12, "bold"), wraplength=580, justify="center",
        ).pack(pady=(8, 14))

        # Action button row (e.g. "Open Tribe Log")
        if action_label and action_callback:
            def _on_action(cb=action_callback) -> None:
                # Hide overlay + prompt so the game can receive the key
                self._prompt_win.grab_release()
                self._prompt_win.withdraw()
                self.root.withdraw()
                self.root.update()
                cb()
                import time
                time.sleep(0.5)
                # Bring overlay + prompt back
                self.root.deiconify()
                self._prompt_win.deiconify()
                self._prompt_win.lift()
                self._prompt_win.focus_force()
                self._prompt_win.grab_set()

            tk.Button(
                inner, text=action_label, width=20,
                font=("Segoe UI", 12, "bold"),
                bg="#225522", fg="white", activebackground="#337733",
                relief=tk.RAISED, bd=2, padx=10, pady=4,
                command=_on_action,
            ).pack(pady=(0, 15))

        btn_frame = tk.Frame(inner, bg="#1a1a1a")
        btn_frame.pack()

        if self._has_current:
            # Existing calibration — offer Re-draw or Keep Current
            tk.Button(
                btn_frame, text="Re-draw", width=16,
                font=("Segoe UI", 12, "bold"),
                bg="#444444", fg="white", activebackground="#666666",
                relief=tk.RAISED, bd=2, padx=8, pady=4,
                command=self._on_prompt_redraw,
            ).pack(side=tk.LEFT, padx=15)
            tk.Button(
                btn_frame, text="Keep Current", width=16,
                font=("Segoe UI", 12, "bold"),
                bg="#444444", fg="white", activebackground="#666666",
                relief=tk.RAISED, bd=2, padx=8, pady=4,
                command=self._on_prompt_keep,
            ).pack(side=tk.LEFT, padx=15)

            # Escape hint
            tk.Label(
                inner, text="Press Escape to keep current",
                fg="#888888", bg="#1a1a1a",
                font=("Segoe UI", 10, "italic"),
            ).pack(pady=(10, 0))
        else:
            # New region — just a Draw button
            tk.Button(
                btn_frame, text="Draw", width=16,
                font=("Segoe UI", 12, "bold"),
                bg="#444444", fg="white", activebackground="#666666",
                relief=tk.RAISED, bd=2, padx=8, pady=4,
                command=self._on_prompt_redraw,
            ).pack(padx=15)

            # Escape hint
            tk.Label(
                inner, text="Press Escape to skip",
                fg="#888888", bg="#1a1a1a",
                font=("Segoe UI", 10, "italic"),
            ).pack(pady=(10, 0))

        # "See Example" link
        if self._example_url:
            def _open_example(url=self._example_url) -> None:
                import webbrowser
                webbrowser.open(url)

            tk.Button(
                inner, text="See Example", width=16,
                font=("Segoe UI", 10),
                bg="#333333", fg="#58a6ff", activebackground="#444444",
                relief=tk.FLAT, bd=0, padx=6, pady=2,
                command=_open_example,
                cursor="hand2",
            ).pack(pady=(10, 0))

        # Let tkinter calculate the required size, then center on screen
        self._prompt_win.update_idletasks()
        actual_w = self._prompt_win.winfo_reqwidth()
        actual_h = self._prompt_win.winfo_reqheight()
        if actual_w < win_w:
            actual_w = win_w
        scr_w = self._prompt_win.winfo_screenwidth()
        scr_h = self._prompt_win.winfo_screenheight()
        x = (scr_w - actual_w) // 2
        y = (scr_h - actual_h) // 2
        self._prompt_win.geometry(f"{actual_w}x{actual_h}+{x}+{y}")

        # Drop topmost from overlay so the prompt can sit above it at the OS level
        self.root.attributes("-topmost", False)
        self._prompt_win.lift()
        self._prompt_win.focus_force()
        self._prompt_win.grab_set()

        # Bind Escape on the prompt window to keep current
        self._prompt_win.bind("<Escape>", lambda e: self._on_prompt_keep())

    def _on_prompt_redraw(self) -> None:
        """User chose Re-draw — remove prompt and enable drawing."""
        if hasattr(self, "_prompt_win"):
            self._prompt_win.destroy()
        self.root.attributes("-topmost", True)
        # Show the instruction text now that we're in draw mode
        self.canvas.itemconfigure(self._instruction_id, text=self._instruction)
        self._enable_drawing()

    def _on_prompt_keep(self) -> None:
        """User chose Keep Current — close everything, return None."""
        self._kept = True
        self.result = None
        if hasattr(self, "_prompt_win"):
            self._prompt_win.destroy()
        self.root.destroy()

    def _enable_drawing(self) -> None:
        """Bind mouse events for drag-to-select."""
        self.canvas.configure(cursor="crosshair")
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<Escape>", self._on_escape)

    def _on_press(self, event: tk.Event) -> None:
        self._start_x = event.x
        self._start_y = event.y
        if self._rect_id is not None:
            self.canvas.delete(self._rect_id)
        self._rect_id = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline="red", width=3,
        )

    def _on_drag(self, event: tk.Event) -> None:
        if self._rect_id is not None:
            self.canvas.coords(
                self._rect_id,
                self._start_x, self._start_y,
                event.x, event.y,
            )

    def _on_release(self, event: tk.Event) -> None:
        x0, y0 = self._start_x, self._start_y
        x1, y1 = event.x, event.y

        # Normalize so left < right, top < bottom
        left = min(x0, x1)
        top = min(y0, y1)
        right = max(x0, x1)
        bottom = max(y0, y1)

        # Ignore tiny accidental clicks
        if (right - left) < 10 or (bottom - top) < 10:
            return

        bbox = [left, top, right, bottom]
        self.result = bbox
        self.root.destroy()

    def _on_escape(self, event: tk.Event) -> None:
        self.result = None
        self.root.destroy()

    def run(self) -> Optional[list[int]]:
        self.root.mainloop()
        return self.result


def run_overlay(
    instruction: str | None = None,
    existing_bboxes: dict[str, list[int]] | None = None,
    prompt: str | None = None,
    action_label: str | None = None,
    action_callback: callable | None = None,
    example_url: str | None = None,
) -> tuple[Optional[list[int]], bool]:
    """Launch the calibration overlay and return (bbox, kept).

    Parameters
    ----------
    instruction:
        Custom instruction text shown at the top of the overlay.
        ``None`` uses the default message.
    existing_bboxes:
        Mapping of label → [left, top, right, bottom] for regions to display
        as colored reference rectangles on the overlay.
    prompt:
        If set, show Re-draw / Keep Current buttons on the overlay instead
        of enabling drawing immediately.
    action_label:
        Label for an extra action button on the prompt (e.g. "Open Tribe Log").
    action_callback:
        Callback invoked when the action button is clicked.

    Returns
    -------
    (bbox, kept) — bbox is the selected region or None; kept is True if the
    user clicked "Keep Current".
    """
    if tk is None:
        raise RuntimeError(
            "tkinter is not available — cannot open the calibration overlay. "
            "Reinstall TribeWatch or install Python with tkinter support."
        )
    kwargs: dict = {}
    if instruction is not None:
        kwargs["instruction"] = instruction
    if existing_bboxes is not None:
        kwargs["existing_bboxes"] = existing_bboxes
    if prompt is not None:
        kwargs["prompt"] = prompt
    if action_label is not None:
        kwargs["action_label"] = action_label
    if action_callback is not None:
        kwargs["action_callback"] = action_callback
    if example_url is not None:
        kwargs["example_url"] = example_url
    app = _OverlayApp(**kwargs)
    return app.run(), app._kept
