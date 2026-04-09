"""In-memory ring buffer for log lines, with optional relay streaming.

Attaches to the root logger to capture all application log output.
Provides two modes of operation:

1. **Buffer** (always on): keeps the most recent N formatted log lines
   in a ``collections.deque`` so they can be retrieved on-demand via
   ``get_lines()``.

2. **Stream** (opt-in): when a relay callback is set via
   ``start_stream()``, each new log line is also forwarded in real-time
   through the relay WebSocket.

Both modes are independent — streaming can be toggled without affecting
the buffer.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Callable

_DEFAULT_CAPACITY = 500


class LogBufferHandler(logging.Handler):
    """Logging handler that buffers lines and optionally streams them."""

    def __init__(self, capacity: int = _DEFAULT_CAPACITY) -> None:
        super().__init__()
        self._buffer: deque[str] = deque(maxlen=capacity)
        self._stream_cb: Callable[[str], Any] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- Buffer API --

    def get_lines(self, limit: int = 0) -> list[str]:
        """Return buffered lines (oldest-first). 0 = all."""
        if limit and limit < len(self._buffer):
            return list(self._buffer)[-limit:]
        return list(self._buffer)

    def clear(self) -> None:
        self._buffer.clear()

    # -- Stream API --

    def start_stream(
        self,
        callback: Callable[[str], Any],
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Enable real-time streaming. *callback* receives each formatted
        log line; if it returns a coroutine the handler schedules it on
        *loop* (or the running loop).
        """
        self._stream_cb = callback
        self._loop = loop

    def stop_stream(self) -> None:
        self._stream_cb = None
        self._loop = None

    @property
    def streaming(self) -> bool:
        return self._stream_cb is not None

    # -- Handler plumbing --

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            self.handleError(record)
            return
        self._buffer.append(line)
        cb = self._stream_cb
        if cb is not None:
            try:
                result = cb(line)
                if asyncio.iscoroutine(result):
                    loop = self._loop
                    if loop is None:
                        try:
                            loop = asyncio.get_running_loop()
                        except RuntimeError:
                            return
                    loop.call_soon_threadsafe(
                        lambda r=result, l=loop: l.create_task(r),
                    )
            except Exception:
                pass  # never let streaming break logging
