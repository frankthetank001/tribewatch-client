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
# Bbox derivation — bboxes for any resolution are computed from a single
# 1920x1080 baseline by scaling to the target height and applying a
# horizontal centering offset for non-16:9 aspect ratios.
#
# The HUD inside ARK is anchored to a centered 16:9 viewport at the
# current screen height — verified empirically across 1280x720,
# 1920x1080 and 2560x1080. Resolutions in _VERIFIED_RESOLUTIONS have
# been hand-checked; others use the derived formula and require user
# confirmation via the setup wizard before they're trusted.
# ---------------------------------------------------------------------------

_BASELINE_RES = (1920, 1080)

_BASELINE_BBOXES: dict[str, list[int]] = {
    "tribe_log": [758, 128, 1164, 826],
    "parasaur":  [149, 4, 1376, 50],
    "tribe":     [176, 184, 708, 888],
}

# Resolutions where the derived formula has been hand-verified to work.
_VERIFIED_RESOLUTIONS: set[tuple[int, int]] = {
    (1280, 720),
    (1920, 1080),
    (2560, 1080),
}


def is_verified_resolution(resolution: tuple[int, int]) -> bool:
    """Return True if the derived preset for *resolution* has been hand-verified."""
    return tuple(resolution) in _VERIFIED_RESOLUTIONS


def derive_preset(resolution: tuple[int, int]) -> dict[str, list[int]]:
    """Compute bboxes for *resolution* by scaling the 1920x1080 baseline.

    ARK's HUD is a 16:9 inner viewport centered inside the window:
      * Wider-than-16:9 (e.g. ultrawide 21:9) → pillarboxed; the inner
        viewport sits at full window height and gets black bars on the
        left and right.
      * Narrower-than-16:9 (e.g. 1360x768 ≈ 1.77, or 1280x800 = 1.6)
        → letterboxed; the inner viewport sits at full window width
        and gets bars on top and bottom.
      * Exactly 16:9 → no bars, the whole window is the inner viewport.

    The previous formula always assumed pillarbox geometry, which
    produced negative ``offset_x`` for narrower-than-16:9 ratios — close
    enough to harmless for 1360x768 (≈2 px error) but visibly wrong for
    something like 1280x800 (≈70 px error). Branch on aspect to compute
    the right one.
    """
    W, H = int(resolution[0]), int(resolution[1])
    bw, bh = _BASELINE_RES

    target_aspect = W / H if H else 0
    baseline_aspect = bw / bh

    if target_aspect >= baseline_aspect:
        # Pillarbox: inner 16:9 viewport at the full target height.
        scale = H / bh
        inner_w = round(H * bw / bh)
        offset_x = (W - inner_w) / 2
        offset_y = 0.0
    else:
        # Letterbox: inner 16:9 viewport at the full target width.
        scale = W / bw
        inner_h = round(W * bh / bw)
        offset_x = 0.0
        offset_y = (H - inner_h) / 2

    out: dict[str, list[int]] = {}
    for region, (x1, y1, x2, y2) in _BASELINE_BBOXES.items():
        out[region] = [
            int(round(x1 * scale + offset_x)),
            int(round(y1 * scale + offset_y)),
            int(round(x2 * scale + offset_x)),
            int(round(y2 * scale + offset_y)),
        ]
    return out


def _get_screen_resolution() -> tuple[int, int] | None:
    """Return (width, height) of the primary monitor, or None on failure."""
    try:
        user32 = ctypes.windll.user32
        return (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
    except Exception:
        return None


def get_preset(resolution: tuple[int, int] | None = None) -> dict[str, list[int]] | None:
    """Return derived bboxes for *resolution*.

    Pass *resolution* explicitly or omit to auto-detect from the game INI.
    Always returns a derived preset — never None — unless the resolution
    could not be detected at all.
    """
    if resolution is None:
        try:
            from tribewatch.server_id import get_game_resolution
            resolution = get_game_resolution()
        except Exception:
            pass
    if resolution is None:
        return None
    return derive_preset(resolution)


def get_default_bbox(resolution: tuple[int, int] | None = None) -> list[int]:
    """Return the derived tribe log bbox for the given (or detected) resolution."""
    if resolution is None:
        resolution = _get_screen_resolution()
    if resolution is None:
        return list(_BASELINE_BBOXES["tribe_log"])
    return derive_preset(resolution)["tribe_log"]


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
        window_title: str = "ArkAscended",
        region_title: str | None = None,
    ) -> None:
        self.result: Optional[list[int]] = None
        self._start_x = 0
        self._start_y = 0
        self._rect_id: Optional[int] = None
        self._kept = False  # True if user clicked "Keep Current"
        self._instruction = instruction
        self._window_title = window_title
        self._region_title = region_title
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

        # Big region title banner — always visible so the user can see at
        # a glance which region they're calibrating, even when the prompt
        # dialog is dismissed during drawing.
        sw = self.root.winfo_screenwidth()
        if region_title:
            self.canvas.create_text(
                sw // 2, 40,
                text=f"REGION: {region_title.upper()}",
                fill="white",
                font=("Segoe UI", 22, "bold"),
            )

        # Instruction text (hidden in prompt mode — shown in the prompt dialog instead)
        self._instruction_id = self.canvas.create_text(
            sw // 2,
            120 if region_title else 40,
            text="" if prompt else instruction,
            fill="white",
            font=("Segoe UI", 18, "bold"),
        )

        # Get client→screen offset so existing bboxes (stored as client
        # coords) render in the correct position on the fullscreen overlay.
        offset_x, offset_y = 0, 0
        try:
            import ctypes
            from tribewatch.capture import _find_window_by_title
            hwnd = _find_window_by_title(getattr(self, "_window_title", "ArkAscended"))
            if hwnd:
                user32 = ctypes.windll.user32
                pt = (ctypes.c_long * 2)(0, 0)
                user32.ClientToScreen(hwnd, ctypes.byref(pt))
                offset_x, offset_y = pt[0], pt[1]
        except Exception:
            pass

        # Draw existing bboxes as colored rectangles with labels
        if existing_bboxes:
            for label, bbox in existing_bboxes.items():
                if len(bbox) != 4:
                    continue
                color = self._LABEL_COLORS.get(
                    label.replace(" (current)", ""), self._DEFAULT_COLOR
                )
                left, top, right, bottom = bbox
                # Convert stored client coords to screen coords for display
                left += offset_x
                top += offset_y
                right += offset_x
                bottom += offset_y
                # Main rectangle — thick outline so it pops on the dim overlay
                self.canvas.create_rectangle(
                    left, top, right, bottom,
                    outline=color, width=5,
                )
                # Corner brackets for extra visibility
                bracket = max(20, min(60, (right - left) // 6))
                bw = 7
                for cx, cy, dx, dy in (
                    (left, top, 1, 1),
                    (right, top, -1, 1),
                    (left, bottom, 1, -1),
                    (right, bottom, -1, -1),
                ):
                    self.canvas.create_line(
                        cx, cy, cx + dx * bracket, cy, fill=color, width=bw,
                    )
                    self.canvas.create_line(
                        cx, cy, cx, cy + dy * bracket, fill=color, width=bw,
                    )
                # Label with a dark background pill so it's readable
                label_y = top - 14
                self.canvas.create_rectangle(
                    left - 2, label_y - 14, left + 8 * len(label) + 12, label_y + 8,
                    fill="#000000", outline=color, width=2,
                )
                self.canvas.create_text(
                    left + 5, label_y - 3,
                    text=label,
                    fill=color,
                    font=("Segoe UI", 14, "bold"),
                    anchor="w",
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

        # Convert screen coords → client coords by subtracting the game
        # window's client area position on screen. This makes bboxes work
        # regardless of whether the game is fullscreen (offset 0,0) or
        # windowed on an ultrawide (offset e.g. 320,0).
        try:
            import ctypes
            from tribewatch.capture import _find_window_by_title
            # The tkinter overlay doesn't know the game window title, so
            # we look it up via the common default. Calibration should
            # always have the game running.
            hwnd = _find_window_by_title(getattr(self, "_window_title", "ArkAscended"))
            if hwnd:
                user32 = ctypes.windll.user32
                # Get client area top-left in screen coordinates
                pt = (ctypes.c_long * 2)(0, 0)
                user32.ClientToScreen(hwnd, ctypes.byref(pt))
                offset_x, offset_y = pt[0], pt[1]
                left -= offset_x
                top -= offset_y
                right -= offset_x
                bottom -= offset_y
        except Exception:
            pass  # Fall back to raw screen coords if conversion fails

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
    window_title: str = "ArkAscended",
    region_title: str | None = None,
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
    kwargs["window_title"] = window_title
    if region_title is not None:
        kwargs["region_title"] = region_title
    app = _OverlayApp(**kwargs)
    return app.run(), app._kept
