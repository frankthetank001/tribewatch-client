"""Lightweight status overlay for the ARK game window.

Shows a small, click-through, always-on-top label in the corner of the
ARK window with the current monitoring status.  Uses a transparent
tkinter window positioned relative to the game window.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
import tkinter as tk
from typing import Any

log = logging.getLogger(__name__)

# Win32 constants for click-through
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x80000
WS_EX_TRANSPARENT = 0x20
WS_EX_TOPMOST = 0x8
WS_EX_TOOLWINDOW = 0x80  # hide from taskbar


STATUS_COLORS = {
    "monitoring": "#3fb950",   # green
    "playing":    "#a855f7",   # purple
    "idle":       "#d29922",   # orange
    "recovery":   "#d29922",   # orange
    "dead":       "#e74c3c",   # red
    "offline":    "#8b949e",   # grey
    "paused":     "#8b949e",   # grey
}

STATUS_ICONS = {
    "monitoring": "\u25cf",   # ●
    "playing":    "\u25cf",
    "idle":       "\u25cf",
    "recovery":   "\u25cf",
    "dead":       "\U0001f480",  # 💀
    "offline":    "\u25cf",
    "paused":     "\u25cf",
}


class StatusOverlay:
    """Manages a transparent overlay window showing client status.

    Runs tkinter on a background thread.  Call ``update(status, detail)``
    from any thread to change the displayed text.
    """

    def __init__(self, window_title: str = "ArkAscended") -> None:
        self._window_title = window_title
        self._root: tk.Tk | None = None
        self._label: tk.Label | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._pending_text: str | None = None
        self._pending_color: str | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start the overlay on a background thread."""
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the overlay."""
        self._running = False
        if self._root:
            try:
                self._root.after(0, self._root.destroy)
            except Exception:
                pass

    def update(self, status: str, detail: str = "") -> None:
        """Update the overlay text.  Thread-safe.

        *status*: one of monitoring, playing, idle, recovery, offline, paused
        *detail*: optional extra text (e.g. "opening tribe log in 3m")
        """
        icon = STATUS_ICONS.get(status, "\u25cf")
        color = STATUS_COLORS.get(status, "#8b949e")
        text = f" {icon} {detail}" if detail else f" {icon} {status.title()}"
        with self._lock:
            self._pending_text = text
            self._pending_color = color

    def _run(self) -> None:
        """Tkinter main loop on background thread."""
        try:
            root = tk.Tk()
            self._root = root
            root.title("TribeWatch Overlay")
            root.overrideredirect(True)  # no title bar
            root.attributes("-topmost", True)
            root.attributes("-alpha", 0.85)
            root.configure(bg="#1a1a2e")
            root.update_idletasks()

            # Make click-through on Windows using WS_EX_TRANSPARENT
            try:
                root.update()
                # Get the real Win32 HWND — tkinter's winfo_id returns the
                # inner frame, we need the top-level window
                hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
                style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                style |= WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
                ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            except Exception:
                log.debug("Failed to set window style", exc_info=True)

            label = tk.Label(
                root,
                text=" \u25cf Starting...",
                font=("Consolas", 11, "bold"),
                fg="#8b949e",
                bg="#1a1a2e",
                padx=8,
                pady=4,
            )
            label.pack()
            self._label = label

            self._poll_updates()
            root.mainloop()
        except Exception:
            log.debug("Overlay thread error", exc_info=True)
        finally:
            self._root = None
            self._label = None

    def _poll_updates(self) -> None:
        """Check for pending text updates and reposition over game window."""
        if not self._running or not self._root:
            return

        # Apply pending text/color
        with self._lock:
            text = self._pending_text
            color = self._pending_color
            self._pending_text = None
            self._pending_color = None

        if text and self._label:
            self._label.configure(text=text, fg=color or "#8b949e")

        # Reposition over game window
        self._reposition()

        # Schedule next poll
        if self._root and self._running:
            self._root.after(500, self._poll_updates)

    def _reposition(self) -> None:
        """Move the overlay to the top-left corner of the game window."""
        if not self._root:
            return
        try:
            hwnd = ctypes.windll.user32.FindWindowW(None, self._window_title)
            if not hwnd:
                # No game window — show at top-left of primary monitor
                self._root.geometry("+10+35")
                self._root.deiconify()
                return

            rect = (ctypes.c_long * 4)()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            x, y = rect[0], rect[1]

            # Offset slightly from top-left corner
            self._root.geometry(f"+{x + 10}+{y + 35}")
            self._root.deiconify()
        except Exception:
            pass
