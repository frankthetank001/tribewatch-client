"""Deduplication — prevents the same tribe log entry from being sent twice."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections import Counter, OrderedDict
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from tribewatch.parser import TribeLogEvent

log = logging.getLogger(__name__)


def _normalize(text: str) -> str:
    """Lowercase, strip OCR noise characters, collapse whitespace."""
    text = text.strip().lower()
    # Strip pipe chars and trailing punctuation noise that OCR introduces
    # at visual line-break boundaries (|, /, \, ;, _, +, *)
    text = re.sub(r"[|/\\;_+*]+", " ", text)
    # Strip trailing punctuation junk (comma, period, colon after !)
    text = re.sub(r"[,.:;]+\s*$", "", text)
    return re.sub(r"\s+", " ", text.strip())


def _event_key(event: TribeLogEvent) -> str:
    """Build a dedup key from day + time + normalized text."""
    return f"{event.day}:{event.time}:{_normalize(event.raw_text)}"


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _parse_daytime(event: TribeLogEvent) -> tuple[int, str]:
    """Extract (day, time) tuple for ordering comparisons."""
    return (event.day, event.time)


class DedupStore:
    """Rolling hash buffer with fuzzy matching, counts, and high-water mark.

    Tracks how many times each event has been seen so that genuinely
    duplicate events at the same timestamp (e.g. two Metal Foundations
    both auto-decaying) pass through, while re-captures of the same
    screen are still suppressed.
    """

    def __init__(
        self,
        max_size: int = 500,
        fuzzy_threshold: float = 0.97,
        state_file: str | Path | None = None,
    ) -> None:
        self.max_size = max_size
        self.fuzzy_threshold = fuzzy_threshold
        self.state_file = Path(state_file) if state_file else None
        # OrderedDict preserves insertion order for eviction
        # hash → (key, count)
        self._hashes: OrderedDict[str, tuple[str, int]] = OrderedDict()
        self._keys_by_daytime: dict[str, list[str]] = {}  # "day:time" → [key, ...]
        # High-water mark: (day, time) of the newest event we've ever processed.
        # Events older than this are silently dropped (handles scroll-back).
        self._high_water: tuple[int, str] = (0, "00:00:00")
        # EOS authoritative reference for day validation
        self._eos_ref: tuple[int, str] | None = None

        if self.state_file:
            self._load()

    def _load(self) -> None:
        if self.state_file and self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                for entry in data.get("hashes", []):
                    h = entry["hash"]
                    key = entry["key"]
                    count = entry.get("count", 1)
                    self._hashes[h] = (key, count)
                    dt = ":".join(key.split(":")[:2])
                    self._keys_by_daytime.setdefault(dt, []).append(key)
                hw = data.get("high_water")
                if hw:
                    self._high_water = (hw["day"], hw["time"])
                log.info(
                    "Loaded %d dedup entries from %s (high water: Day %d, %s)",
                    len(self._hashes), self.state_file,
                    self._high_water[0], self._high_water[1],
                )
            except Exception:
                log.exception("Failed to load dedup state, starting fresh")

    def save(self) -> None:
        """Persist state to disk with atomic write."""
        if not self.state_file:
            return
        entries = [
            {"hash": h, "key": k, "count": c}
            for h, (k, c) in self._hashes.items()
        ]
        data = json.dumps({
            "hashes": entries,
            "high_water": {"day": self._high_water[0], "time": self._high_water[1]},
        }, indent=2)
        tmp = self.state_file.with_suffix(".tmp")
        for attempt in range(3):
            try:
                tmp.write_text(data, encoding="utf-8")
                os.replace(tmp, self.state_file)
                return
            except PermissionError:
                if attempt < 2:
                    import time as _time
                    _time.sleep(0.05)
                else:
                    log.warning("Could not save dedup state — file locked, will retry next cycle")

    def _evict(self) -> None:
        while len(self._hashes) > self.max_size:
            _, (old_key, _) = self._hashes.popitem(last=False)
            dt = ":".join(old_key.split(":")[:2])
            keys = self._keys_by_daytime.get(dt, [])
            if old_key in keys:
                keys.remove(old_key)
                if not keys:
                    del self._keys_by_daytime[dt]

    def _find_fuzzy_hash(self, key: str) -> str | None:
        """Find an existing fuzzy-matching hash for this key, or None."""
        dt = ":".join(key.split(":")[:2])
        existing = self._keys_by_daytime.get(dt, [])
        text = key.split(":", 2)[2] if key.count(":") >= 2 else key
        for other_key in existing:
            other_text = other_key.split(":", 2)[2] if other_key.count(":") >= 2 else other_key
            ratio = SequenceMatcher(None, text, other_text).ratio()
            if ratio >= self.fuzzy_threshold:
                return _hash_key(other_key)
        return None

    def _get_count(self, key: str) -> int:
        """Get the stored count for a key (exact or fuzzy match)."""
        h = _hash_key(key)
        if h in self._hashes:
            return self._hashes[h][1]
        fuzzy_h = self._find_fuzzy_hash(key)
        if fuzzy_h and fuzzy_h in self._hashes:
            return self._hashes[fuzzy_h][1]
        return 0

    def _set_count(self, key: str, count: int) -> None:
        """Set the stored count for a key, creating the entry if needed."""
        h = _hash_key(key)
        if h in self._hashes:
            self._hashes[h] = (key, count)
            self._hashes.move_to_end(h)
            return
        # Check fuzzy match — update existing entry instead of creating a new one
        fuzzy_h = self._find_fuzzy_hash(key)
        if fuzzy_h and fuzzy_h in self._hashes:
            old_key = self._hashes[fuzzy_h][0]
            self._hashes[fuzzy_h] = (old_key, count)
            self._hashes.move_to_end(fuzzy_h)
            return
        # New entry
        self._hashes[h] = (key, count)
        dt = ":".join(key.split(":")[:2])
        self._keys_by_daytime.setdefault(dt, []).append(key)
        self._evict()

    def _is_older_than_high_water(self, event: TribeLogEvent) -> bool:
        """Return True if the event is strictly older than the high-water mark."""
        dt = _parse_daytime(event)
        return dt < self._high_water

    # Maximum forward jump allowed for the high-water mark (in days).
    # Prevents garbled OCR day numbers (e.g. "1089228" from "1089" + junk)
    # from poisoning the high-water mark and suppressing all future events.
    _MAX_DAY_JUMP = 200

    def set_eos_reference(self, day: int, time_str: str) -> None:
        """Store the authoritative EOS day/time as a reference for validation.

        When set, ``_advance_high_water`` uses the EOS day instead of the
        previous high-water mark for jump validation.  This is strictly
        better than the heuristic-only approach because it anchors the
        check against a ground-truth value.
        """
        self._eos_ref = (day, time_str)
        log.debug("EOS reference set: Day %d, %s", day, time_str)

    def seed_high_water_from_eos(self, day: int, time_str: str) -> None:
        """Seed the high-water mark from EOS on first startup.

        Only seeds when the high water is at its initial ``(0, "00:00:00")``
        value.  Prevents the flood of old events that otherwise occurs on
        first run when the tribe log has hundreds of historical entries.
        """
        if self._high_water != (0, "00:00:00"):
            return
        self._high_water = (day, "00:00:00")
        log.info(
            "Seeded high-water mark from EOS: Day %d, 00:00:00 (EOS time: %s)",
            day, time_str,
        )
        self.save()

    def _advance_high_water(self, event: TribeLogEvent) -> None:
        """Advance the high-water mark if this event is newer.

        Rejects suspiciously large jumps that are almost certainly OCR
        artifacts (e.g. "Day 1089228" from garbled "Day 1089" + "228").

        When an EOS reference is available, validates against the EOS day
        instead of the previous high-water mark for more reliable rejection.
        """
        dt = _parse_daytime(event)
        if dt > self._high_water:
            # Use EOS reference day when available, else fall back to high-water
            if self._eos_ref is not None:
                ref_day = self._eos_ref[0]
            else:
                ref_day = self._high_water[0]
            day_jump = dt[0] - ref_day
            if ref_day > 0 and day_jump > self._MAX_DAY_JUMP:
                log.warning(
                    "Ignoring suspicious high-water advance: Day %d → %d "
                    "(jump of %d days from %s day %d, max allowed %d) — likely garbled OCR",
                    self._high_water[0], dt[0],
                    day_jump,
                    "EOS" if self._eos_ref is not None else "high-water",
                    ref_day, self._MAX_DAY_JUMP,
                )
                return
            self._high_water = dt

    def is_new(self, event: TribeLogEvent) -> bool:
        """Return True if this event has not been seen before.

        Events older than the high-water mark are always considered duplicates,
        preventing re-notification when the user scrolls the tribe log.

        Note: for count-aware dedup of batches with duplicate events,
        use filter_new() instead.
        """
        if self._is_older_than_high_water(event):
            return False
        key = _event_key(event)
        return self._get_count(key) == 0

    def filter_new(self, events: Sequence[TribeLogEvent]) -> list[TribeLogEvent]:
        """Return only events not previously seen, marking them as seen.

        Count-aware: if the same event appears N times in the batch and
        M times are already stored, allows max(0, N - M) new events through.
        This handles genuinely duplicate events at the same timestamp
        (e.g. two Metal Foundations auto-decaying simultaneously).
        """
        # Count occurrences of each key in this batch (preserving order)
        batch_keys: list[str | None] = []
        batch_counts: Counter[str] = Counter()
        for event in events:
            if self._is_older_than_high_water(event):
                batch_keys.append(None)  # rejected by high-water
            else:
                key = _event_key(event)
                batch_counts[key] += 1
                batch_keys.append(key)

        # For each unique key, determine how many new events to allow
        stored_counts: dict[str, int] = {}
        allowed: dict[str, int] = {}
        for key in batch_counts:
            stored = self._get_count(key)
            stored_counts[key] = stored
            new_count = max(0, batch_counts[key] - stored)
            allowed[key] = new_count

        # Walk the events in order, emitting up to `allowed` per key
        emitted: Counter[str] = Counter()
        new = []
        for event, key in zip(events, batch_keys):
            if key is None:
                continue
            if emitted[key] < allowed[key]:
                new.append(event)
            emitted[key] += 1

        # Update stored counts to the max of stored vs batch
        for key in batch_counts:
            new_total = max(stored_counts[key], batch_counts[key])
            self._set_count(key, new_total)

        # Advance high-water mark based on all events in this batch
        for event in events:
            self._advance_high_water(event)

        self.save()
        return new
