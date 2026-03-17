"""Drag-to-select calibration overlay using tkinter.

Provides a transparent fullscreen overlay where the user can draw a rectangle
over the tribe log region, replacing the old manual coordinate entry.

Also provides resolution-based bbox presets for known resolutions.
"""

from __future__ import annotations

import ctypes
import logging
import tkinter as tk
from tkinter import messagebox
from typing import Any, Optional

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
    ) -> None:
        self.result: Optional[list[int]] = None
        self._start_x = 0
        self._start_y = 0
        self._rect_id: Optional[int] = None

        self.root = tk.Tk()
        self.root.title("TribeWatch Calibration")
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.3)
        self.root.configure(background="black")
        self.root.overrideredirect(True)

        # Canvas fills the whole screen
        self.canvas = tk.Canvas(
            self.root, bg="black", highlightthickness=0, cursor="crosshair"
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # Instruction text
        self.canvas.create_text(
            self.root.winfo_screenwidth() // 2,
            40,
            text=instruction,
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

        # Bindings
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
        self.root.withdraw()  # hide overlay for the dialog

        # Confirmation dialog
        answer = messagebox.askyesnocancel(
            "Confirm Region",
            f"Selected region: {bbox}\n\n"
            f"Width: {right - left}px, Height: {bottom - top}px\n\n"
            "Yes = accept, No = retry, Cancel = abort",
        )

        if answer is True:
            self.result = bbox
            self.root.destroy()
        elif answer is False:
            # Retry — show overlay again
            if self._rect_id is not None:
                self.canvas.delete(self._rect_id)
                self._rect_id = None
            self.root.deiconify()
        else:
            # Cancel
            self.result = None
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
) -> Optional[list[int]]:
    """Launch the calibration overlay and return [left, top, right, bottom] or None.

    Parameters
    ----------
    instruction:
        Custom instruction text shown at the top of the overlay.
        ``None`` uses the default message.
    existing_bboxes:
        Mapping of label → [left, top, right, bottom] for regions to display
        as colored reference rectangles on the overlay.
    """
    kwargs: dict = {}
    if instruction is not None:
        kwargs["instruction"] = instruction
    if existing_bboxes is not None:
        kwargs["existing_bboxes"] = existing_bboxes
    app = _OverlayApp(**kwargs)
    return app.run()
