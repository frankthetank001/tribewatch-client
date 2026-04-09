"""Screen capture module — grabs the tribe log region via mss (or PIL fallback).

Supports optional Win32 window capture via PrintWindow for background capture.
"""

from __future__ import annotations

import ctypes
import logging
import struct
import sys
from typing import TYPE_CHECKING

from PIL import Image

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Win32 window capture helpers (only functional on Windows)
# ---------------------------------------------------------------------------

_IS_WIN32 = sys.platform == "win32"

PW_RENDERFULLCONTENT = 2


def _find_window_by_title(title: str) -> int | None:
    """Find a window by title. Tries exact match first, then partial (case-insensitive).

    Returns HWND as int, or None if not found.
    """
    if not _IS_WIN32:
        return None

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]

    # Exact match
    hwnd = user32.FindWindowW(None, title)
    if hwnd:
        return hwnd

    # Partial match via EnumWindows
    result: list[int] = []
    title_lower = title.lower()

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _enum_cb(hwnd_ptr: int, _lparam: int) -> bool:
        length = user32.GetWindowTextLengthW(hwnd_ptr)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd_ptr, buf, length + 1)
            if title_lower in buf.value.lower():
                result.append(hwnd_ptr)
                return False  # stop enumeration
        return True  # continue

    user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
    return result[0] if result else None


def focus_window(title: str) -> bool:
    """Bring the window with the given title to the foreground.

    Returns True if the window is now the foreground window.
    Uses the Alt-key trick to bypass Windows' foreground-lock restrictions.

    Note: PostMessage-based callers (send_key / send_click) do NOT need
    this — they deliver to the hwnd directly. Only callers that use
    pyautogui (SendInput) need real focus. The refresh loop and idle
    monitor should NOT call this.
    """
    if not _IS_WIN32:
        return False

    hwnd = _find_window_by_title(title)
    if hwnd is None:
        log.debug("focus_window: window %r not found", title)
        return False

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]

    if user32.GetForegroundWindow() == hwnd:
        return True

    user32.ShowWindow(hwnd, 9)

    VK_MENU = 0x12
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)

    user32.SetForegroundWindow(hwnd)
    return user32.GetForegroundWindow() == hwnd


# ---------------------------------------------------------------------------
# Key name → virtual key code mapping for send_key
# ---------------------------------------------------------------------------

_VK_MAP: dict[str, int] = {
    "escape": 0x1B,
    "esc": 0x1B,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "space": 0x20,
    "`": 0xC0,
    "~": 0xC0,
    "l": 0x4C,
}

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101


def is_window_foreground(title: str) -> bool:
    """Return True if the window with *title* is currently the foreground.

    Used by the tribe-log refresh / idle recovery loops to check whether
    ARK currently owns input focus before sending Esc/L. If not, the
    refresh loop will try to acquire focus via focus_window() first.
    """
    if not _IS_WIN32:
        return False
    hwnd = _find_window_by_title(title)
    if not hwnd:
        return False
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    return user32.GetForegroundWindow() == hwnd


def send_key(title: str, key: str) -> bool:
    """Send a key press to a window via PostMessage (no focus steal).

    Args:
        title: Window title to find.
        key: Key name (e.g. "escape", "l", "enter", "`").

    Returns:
        True if the key was sent, False if window not found or unsupported key.
    """
    if not _IS_WIN32:
        return False

    hwnd = _find_window_by_title(title)
    if not hwnd:
        log.warning("send_key: window '%s' not found", title)
        return False

    vk = _VK_MAP.get(key.lower())
    if vk is None:
        # Try single character → VkKeyScanW
        if len(key) == 1:
            vk = ctypes.windll.user32.VkKeyScanW(ord(key)) & 0xFF  # type: ignore[attr-defined]
        else:
            log.warning("send_key: unknown key '%s'", key)
            return False

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 0)
    user32.PostMessageW(hwnd, WM_KEYUP, vk, 0)
    return True


WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001


def send_click(title: str, client_x: int, client_y: int) -> bool:
    """Send a mouse click to a window via PostMessage at client coordinates.

    Uses WM_LBUTTONDOWN/WM_LBUTTONUP directly to the window handle,
    avoiding screen coordinate conversion issues (DPI, multi-monitor).

    Args:
        title: Window title to find.
        client_x: X coordinate in the window's client area.
        client_y: Y coordinate in the window's client area.

    Returns:
        True if the click was sent, False if window not found.
    """
    if not _IS_WIN32:
        return False

    hwnd = _find_window_by_title(title)
    if not hwnd:
        log.warning("send_click: window '%s' not found", title)
        return False

    # Pack coordinates as MAKELPARAM(x, y) = (y << 16) | (x & 0xFFFF)
    lparam = (client_y << 16) | (client_x & 0xFFFF)
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
    user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam)
    return True


def _grab_window(hwnd: int, bbox: list[int] | None = None) -> Image.Image | None:
    """Capture a window's client area via PrintWindow.

    Args:
        hwnd: Window handle.
        bbox: Optional [left, top, right, bottom] crop region relative to client area.

    Returns:
        PIL Image or None on failure.
    """
    if not _IS_WIN32:
        return None

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    gdi32 = ctypes.windll.gdi32  # type: ignore[attr-defined]

    # 1. Get client area dimensions
    rect = (ctypes.c_long * 4)()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    width = rect[2]
    height = rect[3]
    if width <= 0 or height <= 0:
        log.warning("Window client area is empty (%dx%d)", width, height)
        return None

    # 2. Get device context
    hdc = user32.GetDC(hwnd)
    if not hdc:
        log.warning("GetDC failed for hwnd %s", hwnd)
        return None

    mem_dc = None
    bitmap = None
    old_obj = None
    try:
        # 3. Create compatible DC and bitmap
        mem_dc = gdi32.CreateCompatibleDC(hdc)
        bitmap = gdi32.CreateCompatibleBitmap(hdc, width, height)
        old_obj = gdi32.SelectObject(mem_dc, bitmap)

        # 4. PrintWindow with PW_RENDERFULLCONTENT (required for DirectX games)
        ret = user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)
        if not ret:
            log.warning("PrintWindow failed for hwnd %s", hwnd)
            return None

        # 5. Read pixel data via GetDIBits
        # BITMAPINFOHEADER: 40 bytes
        bmi = struct.pack(
            "IiiHHIIiiII",
            40,         # biSize
            width,      # biWidth
            -height,    # biHeight (negative = top-down)
            1,          # biPlanes
            32,         # biBitCount (BGRA)
            0,          # biCompression (BI_RGB)
            0,          # biSizeImage
            0,          # biXPelsPerMeter
            0,          # biYPelsPerMeter
            0,          # biClrUsed
            0,          # biClrImportant
        )

        buf_size = width * height * 4
        pixel_buf = ctypes.create_string_buffer(buf_size)

        gdi32.GetDIBits(
            mem_dc, bitmap, 0, height, pixel_buf, bmi, 0  # DIB_RGB_COLORS
        )

        # 6. Convert BGRA → PIL Image
        img = Image.frombuffer("RGBA", (width, height), pixel_buf, "raw", "BGRA", 0, 1)
        img = img.convert("RGB")

        # 7. Crop if bbox provided
        if bbox:
            left, top, right, bottom = bbox
            img = img.crop((left, top, right, bottom))

        return img

    finally:
        # 8. Cleanup GDI objects
        if old_obj and mem_dc:
            gdi32.SelectObject(mem_dc, old_obj)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hdc)


# ---------------------------------------------------------------------------
# Screen capture (mss / PIL fallback)
# ---------------------------------------------------------------------------


def _bbox_to_mss_region(bbox: list[int]) -> dict[str, int]:
    """Convert [left, top, right, bottom] to mss monitor dict."""
    left, top, right, bottom = bbox
    return {"left": left, "top": top, "width": right - left, "height": bottom - top}


class ScreenCapture:
    """Captures a screen region as a PIL Image.

    If ``window_title`` is set, captures the window's client area via Win32
    PrintWindow (works even when the window is in the background). The bbox
    is then relative to the window's client area rather than the screen.
    """

    def __init__(
        self, bbox: list[int], monitor: int = 0, window_title: str = ""
    ) -> None:
        self.bbox = bbox
        self.monitor = monitor
        self._window_title = window_title
        self._hwnd: int | None = None

        # Window capture mode
        if window_title:
            if not _IS_WIN32:
                log.error(
                    "window_title is set but platform is %s — falling back to screen capture",
                    sys.platform,
                )
                self._window_title = ""
            else:
                self._hwnd = _find_window_by_title(window_title)
                if self._hwnd:
                    log.info(
                        "Window capture: found '%s' (hwnd=%s)", window_title, self._hwnd
                    )
                else:
                    log.warning(
                        "Window capture: '%s' not found, will retry on grab()",
                        window_title,
                    )

        # mss setup (used when not in window mode)
        self._mss = None
        self._use_mss = True
        try:
            import mss as _mss_mod

            self._mss_mod = _mss_mod
        except ImportError:
            log.warning("mss not available, falling back to PIL ImageGrab")
            self._use_mss = False

    def grab(self) -> Image.Image | None:
        """Capture the configured screen region. Returns PIL Image or None on failure."""
        try:
            if self._window_title:
                return self._grab_window()
            if self._use_mss:
                return self._grab_mss()
            return self._grab_pil()
        except Exception:
            log.exception("Screen capture failed")
            return None

    # -- Window capture path --------------------------------------------------

    def _grab_window(self) -> Image.Image | None:
        """Capture via Win32 PrintWindow."""
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]

        # Check for stale handle
        if self._hwnd and not user32.IsWindow(self._hwnd):
            log.warning("Window handle %s is stale, re-finding...", self._hwnd)
            self._hwnd = None

        # (Re-)find window if needed
        if not self._hwnd:
            self._hwnd = _find_window_by_title(self._window_title)
            if not self._hwnd:
                log.warning("Window '%s' not found", self._window_title)
                return None
            log.info("Window re-found: hwnd=%s", self._hwnd)

        return _grab_window(self._hwnd, self.bbox)

    # -- Screen capture paths -------------------------------------------------

    def _grab_mss(self) -> Image.Image:
        if self._mss is None:
            self._mss = self._mss_mod.mss()
        region = _bbox_to_mss_region(self.bbox)
        shot = self._mss.grab(region)
        # mss returns BGRA; convert to RGB PIL Image
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        return img

    def _grab_pil(self) -> Image.Image:
        from PIL import ImageGrab

        return ImageGrab.grab(bbox=tuple(self.bbox))

    @property
    def window_found(self) -> bool:
        """Whether the target window is currently found (always True if not using window mode)."""
        if not self._window_title:
            return True  # screen capture mode, always "found"
        if not self._hwnd:
            return False
        if _IS_WIN32:
            return bool(ctypes.windll.user32.IsWindow(self._hwnd))
        return False

    def close(self) -> None:
        if self._mss is not None:
            self._mss.close()
            self._mss = None
