"""Reconnect audit log — appends one JSON-lines entry per reconnect event."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_HISTORY_FILE = Path("reconnect_history.jsonl")
_MAX_ENTRIES = 200  # prune oldest when exceeded


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _save_screenshot(img: Any, label: str) -> tuple[str, str]:
    """Save a PIL image to debug/ and return ``(file_path, base64_jpeg)``.

    Either value may be ``""`` on failure.
    """
    if img is None:
        return "", ""
    try:
        import base64
        import io

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=50)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        debug_dir = Path("debug")
        debug_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = debug_dir / f"reconnect_{ts}_{label}.jpg"
        img.save(out, format="JPEG", quality=60)
        # Prune old reconnect screenshots — keep most recent 40
        try:
            old = sorted(
                debug_dir.glob("reconnect_*_*.jpg"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for stale in old[40:]:
                stale.unlink(missing_ok=True)
        except Exception:
            pass
        return str(out), b64
    except Exception:
        log.debug("Failed to save reconnect screenshot (%s)", label, exc_info=True)
        return "", ""


class ReconnectRecord:
    """Collects data for a single reconnect event, then writes it to disk."""

    def __init__(
        self,
        trigger: str,
        auto: bool,
        method: str,
        fail_count: int,
        client_phase: dict,
        screenshot_start: str = "",
        screenshot_start_b64: str = "",
        resolution: tuple[int, int] | None = None,
    ) -> None:
        self.started_at = _now_iso()
        self._start_mono = time.monotonic()
        self.trigger = trigger
        self.auto = auto
        self.method = method
        self.fail_count = fail_count
        self.client_phase = client_phase
        self.screenshot_start = screenshot_start
        self.screenshot_start_b64 = screenshot_start_b64
        self.resolution = resolution

        # Filled in on completion
        self.ended_at: str = ""
        self.outcome: str = ""
        self.failure_reason: str = ""
        self.attempts: int = 0
        self.switched_to_browser: bool = False
        self.screenshot_end: str = ""
        self.screenshot_end_b64: str = ""
        self.duration_secs: float = 0

    def finalise(
        self,
        outcome: str,
        failure_reason: str = "",
        attempts: int = 0,
        switched_to_browser: bool = False,
        screenshot_end: str = "",
        screenshot_end_b64: str = "",
    ) -> None:
        self.ended_at = _now_iso()
        self.duration_secs = round(time.monotonic() - self._start_mono, 1)
        self.outcome = outcome
        self.failure_reason = failure_reason
        self.attempts = attempts
        self.switched_to_browser = switched_to_browser
        self.screenshot_end = screenshot_end
        self.screenshot_end_b64 = screenshot_end_b64

    def to_dict(self, include_images: bool = False) -> dict:
        d: dict = {
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "trigger": self.trigger,
            "auto": self.auto,
            "method": self.method,
            "fail_count_at_start": self.fail_count,
            "client_phase": self.client_phase,
            "screenshot_start": self.screenshot_start,
            "screenshot_end": self.screenshot_end,
            "outcome": self.outcome,
            "failure_reason": self.failure_reason,
            "attempts": self.attempts,
            "switched_to_browser": self.switched_to_browser,
            "duration_secs": self.duration_secs,
            "resolution": (
                f"{self.resolution[0]}x{self.resolution[1]}"
                if self.resolution else ""
            ),
        }
        if include_images:
            d["screenshot_start_b64"] = self.screenshot_start_b64
            d["screenshot_end_b64"] = self.screenshot_end_b64
        return d

    def save(self) -> None:
        """Append this record to the JSONL history file."""
        try:
            with open(_HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(self.to_dict()) + "\n")
            log.info("Reconnect record saved: %s → %s (%.0fs)", self.trigger, self.outcome, self.duration_secs)
            _prune_history()
        except Exception:
            log.debug("Failed to save reconnect record", exc_info=True)


def load_recent_records(limit: int = 50, include_images: bool = False) -> list[dict]:
    """Load the most recent records from the JSONL file.

    If *include_images* is True, re-reads screenshot files from disk
    and embeds them as base64 for relay to the server.
    """
    if not _HISTORY_FILE.exists():
        return []
    try:
        import base64 as _b64

        lines = _HISTORY_FILE.read_text(encoding="utf-8").splitlines()
        records = []
        for line in lines[-limit:]:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if include_images:
                for key, b64_key in [
                    ("screenshot_start", "screenshot_start_b64"),
                    ("screenshot_end", "screenshot_end_b64"),
                ]:
                    path = r.get(key, "")
                    if path and Path(path).exists():
                        try:
                            r[b64_key] = _b64.b64encode(
                                Path(path).read_bytes()
                            ).decode("ascii")
                        except Exception:
                            r[b64_key] = ""
                    else:
                        r.setdefault(b64_key, "")
            records.append(r)
        return records
    except Exception:
        log.debug("Failed to load reconnect history", exc_info=True)
        return []


def _prune_history() -> None:
    """Keep only the most recent _MAX_ENTRIES lines."""
    try:
        lines = _HISTORY_FILE.read_text(encoding="utf-8").splitlines()
        if len(lines) > _MAX_ENTRIES:
            _HISTORY_FILE.write_text(
                "\n".join(lines[-_MAX_ENTRIES:]) + "\n",
                encoding="utf-8",
            )
    except Exception:
        pass
