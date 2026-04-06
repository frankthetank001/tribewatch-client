"""Event parser — converts raw OCR text into structured TribeLogEvent objects."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from enum import Enum
from typing import Sequence

log = logging.getLogger(__name__)


class EventType(Enum):
    DINO_KILLED = "dino_killed"
    STRUCTURE_DESTROYED = "structure_destroyed"
    TRIBE_MEMBER_KILLED = "tribe_member_killed"
    DINO_TAMED = "dino_tamed"
    DEMOLISHED = "demolished"
    DINO_STARVED = "dino_starved"
    PLAYER_ADDED = "player_added"
    PLAYER_REMOVED = "player_removed"
    PLAYER_PROMOTED = "player_promoted"
    TRIBE_DESTROYED = "tribe_destroyed"
    ENEMY_DINO_KILLED = "enemy_dino_killed"
    ENEMY_PLAYER_KILLED = "enemy_player_killed"
    ENEMY_STRUCTURE_DESTROYED = "enemy_structure_destroyed"
    ANTI_MESHED = "anti_meshed"
    PARASAUR_DETECTION = "parasaur_detection"
    PARASAUR_BABIES = "parasaur_babies"
    PARASAUR_BRIEF = "parasaur_brief"
    PARASAUR_BRIEF_BABIES = "parasaur_brief_babies"
    MEMBER_JOINED_ARK = "member_joined_ark"
    MEMBER_LEFT_ARK = "member_left_ark"
    CLAIMED = "claimed"
    UNCLAIMED = "unclaimed"
    UPLOADED = "uploaded"
    DOWNLOADED = "downloaded"
    EGG_HATCHED = "egg_hatched"
    CRYOPODDED = "cryopodded"
    RELEASED = "released"
    AUTO_DECAY = "auto_decay"
    RANK_GROUP_CHANGED = "rank_group_changed"
    PLAYER_DEMOTED = "player_demoted"
    BLACKLIST_JOIN = "blacklist_join"
    UNKNOWN = "unknown"


# Human-readable labels for event types where the auto-formatted value
# (underscores → spaces, title-case) is misleading.  Used by the web API
# and Discord embed builder.
EVENT_TYPE_LABELS: dict[str, str] = {
    "parasaur_detection": "Parasaur Player Detection (Session)",
    "parasaur_babies": "Parasaur Babies Detection (Session)",
    "parasaur_brief": "Parasaur Player Detection (Brief)",
    "parasaur_brief_babies": "Parasaur Babies Detection (Brief)",
    "rank_group_changed": "Rank Group Changed",
    "player_demoted": "Player Demoted",
}


class Severity(Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class TribeLogEvent:
    day: int
    time: str  # HH:MM:SS
    raw_text: str
    event_type: EventType
    severity: Severity
    timestamp: datetime


# --- OCR text pre-processing ---
# WinRT OCR often returns all entries as one blob without newlines.
# Split on "Day" boundaries so each entry becomes its own line.
# OCR reads commas as periods, so accept both: [,.]
_SEP = r"[,.]"

_DAY_BOUNDARY_RE = re.compile(
    rf"(?<!\A)\s*(?=[DO0d][ae][yv]\s*\d+\s*{_SEP})"
)

# --- Line regex ---
# Captures: (1) day number, (2) time blob (digits/separators/spaces), (3) event text.
# The time blob is everything between the comma/period and the last colon/semicolon
# before the event text. Event text can start with a letter or special characters
# like - = " ' (player names such as -=Kapa=-). A separate helper parses the blob
# into HH:MM:SS. This tolerates arbitrarily garbled OCR time output.
_EVT_START = r"[A-Za-z=\"'\-=]"  # first char of event text
# Time blob characters: digits, common separators, spaces, plus OCR artifacts
# like bullet (U+2022), middle dot (U+00B7), commas, pipes.
_TB = r"[\d:.;\s\u2022\u00b7,*|/]"
_LINE_RE = re.compile(
    rf"[DO0d][ae][yv]\s*(\d{{1,5}})\s*{_SEP}\s*({_TB}*?)[:;]\s*({_EVT_START}.+)"
)
# Fallback: matches "Day NNN, <text>" when there's no colon before the event text
# (time completely missing or garbled beyond recognition).
# Consumes any non-event-start characters before the event text.
_LINE_RE_NO_TIME = re.compile(
    rf"[DO0d][ae][yv]\s*(\d{{1,5}})\s*{_SEP}\s*[^A-Za-z\"'=]*({_EVT_START}.+)"
)

# --- Quote / dash character classes for OCR tolerance ---
_Q = r"['\u2018\u2019'`\u0060\"\u201C\u201D]"  # quote variants (single + double, OCR often confuses them)
_NQ = r"[^'\u2018\u2019'`\u0060\"\u201C\u201D]"  # negated _Q (match anything except a quote)
_D = r"[-\u2013\u2014]"           # dash/en-dash/em-dash
_E = r"[eéèë@]"                   # OCR-tolerant "e" (accents, @ common on game text)
# OCR junk that can appear between structural elements (pipes from line breaks, punctuation, tildes)
_J = r"[\s|/\\;:,._+*~^]*"
_PARASAUR_ENEMY_RE = rf"{_E}n{_E}my"  # "enemy" with OCR-tolerant e's

# --- Event classification patterns (first match wins) ---
# Order matters: more specific patterns before generic ones.
_PATTERNS: list[tuple[re.Pattern[str], EventType, Severity]] = [
    # Anti-meshed: Anti-meshing destroyed 'Human - Lvl 131' at X=...
    (
        re.compile(r"anti.?meshing\s+destroyed", re.I),
        EventType.ANTI_MESHED,
        Severity.CRITICAL,
    ),
    # Enemy structure destroyed: C4 Charge destroyed their 'Large Storage Box (Tribe of X)'!
    (
        re.compile(rf"destroyed\s+their\s+{_Q}", re.I),
        EventType.ENEMY_STRUCTURE_DESTROYED,
        Severity.INFO,
    ),
    # Auto-decay: Your 'Metal Foundation' was auto-decay destroyed!
    # Opening quote is optional — OCR sometimes drops it entirely
    # \s* after "your" and before "was" tolerates OCR dropping spaces
    (
        re.compile(rf"your\s*{_Q}?{_NQ}+{_Q}\s*was\s+auto[- ]?decay\s+destroyed", re.I),
        EventType.AUTO_DECAY,
        Severity.INFO,
    ),
    # Structure destroyed: Your 'Metal Foundation' was destroyed!
    # Opening quote is optional — OCR sometimes drops it entirely
    # \s* after "your" and before "was" tolerates OCR dropping spaces
    (
        re.compile(rf"your\s*{_Q}?{_NQ}+{_Q}\s*was\s+destroyed", re.I),
        EventType.STRUCTURE_DESTROYED,
        Severity.CRITICAL,
    ),
    # Dino starved: Your/Adolescent/Juvenile/Baby <Dino> starved to death!
    (
        re.compile(r"starved\s*to\s*death", re.I),
        EventType.DINO_STARVED,
        Severity.WARNING,
    ),
    # Dino killed: Your <Dino> - Lvl <n> (<name>) was killed by
    # Dash before Lvl is optional — OCR may miss it or game log may omit it
    (
        re.compile(rf"your\s+.+?(?:{_D}{_J})?(?:Lv[l1ti!]|Level){_J}\d+.+was\s+killed", re.I),
        EventType.DINO_KILLED,
        Severity.CRITICAL,
    ),
    # Tribe member killed: Tribemember/Tribe member <Player> - Lvl <n> was killed!
    # Dash before Lvl is optional — OCR may drop it. Colon/junk may appear instead.
    (
        re.compile(rf"tribe\s?member\s+.+?(?:{_D}{_J})?(?::?\s*)(?:Lv[l1ti!]|Level){_J}\d+{_J}was\s+killed", re.I),
        EventType.TRIBE_MEMBER_KILLED,
        Severity.CRITICAL,
    ),
    # Tribe destroyed enemy structure: Your Tribe destroyed ...
    (
        re.compile(r"your\s+tribe\s+destroyed", re.I),
        EventType.TRIBE_DESTROYED,
        Severity.INFO,
    ),
    # Enemy dino/tame killed (with tribe name): Your Tribe killed Ghost Lvl 105 (Tribe of Ghost)!
    (
        re.compile(r"your\s+tribe\s+killed\s+.+\(tribe\s+of\s+", re.I),
        EventType.ENEMY_DINO_KILLED,
        Severity.INFO,
    ),
    # Enemy dino/tame killed (with dino name in parens):
    # Your Tribe killed FEEDER -Lvl 323(Maeguana) (WINNING)!
    # Key: Lvl + digits + (DinoName) before the tribe parens
    (
        re.compile(r"your\s+tribe\s+killed\s+.+(?:Lv[l1ti!]|Level)\s*\d+\s*\([^)]+\)\s*\(", re.I),
        EventType.ENEMY_DINO_KILLED,
        Severity.INFO,
    ),
    # Enemy dino killed: Your <Dino> killed a <Enemy>
    (
        re.compile(r"your\s+.+killed\s+a\s+", re.I),
        EventType.ENEMY_DINO_KILLED,
        Severity.INFO,
    ),
    # Enemy player killed: Your Tribe killed Ramdon - Lvl 12!
    (
        re.compile(r"your\s+tribe\s+killed\s+", re.I),
        EventType.ENEMY_PLAYER_KILLED,
        Severity.INFO,
    ),
    # Dino tamed: <Player> Tamed a <Dino> / Tamed a <Dino>
    (
        re.compile(r"tamed\s+a\s+", re.I),
        EventType.DINO_TAMED,
        Severity.INFO,
    ),
    # Demolished: <Player> demolished a '<Structure>'!
    # Opening quote is optional — OCR sometimes drops it entirely
    (
        re.compile(rf"demolished\s+a\s+{_Q}?", re.I),
        EventType.DEMOLISHED,
        Severity.INFO,
    ),
    # Player added to tribe
    (
        re.compile(r"was\s+added\s+to\s+the\s+tribe", re.I),
        EventType.PLAYER_ADDED,
        Severity.WARNING,
    ),
    # Player removed from tribe
    (
        re.compile(r"was\s+removed\s+from\s+the\s+tribe", re.I),
        EventType.PLAYER_REMOVED,
        Severity.WARNING,
    ),
    # Rank group changed: "Human with ID 556263720 set to Rank Group trust by uncle fuckwit with ID 402662003!"
    # OCR may glue "withID" or "with ID" and may have missing spaces
    (
        re.compile(r"set\s+to\s+rank\s+group\s+", re.I),
        EventType.RANK_GROUP_CHANGED,
        Severity.INFO,
    ),
    # Player demoted: "Shiki with ID 24175312 was demoted from Tribe Admin by ..."
    (
        re.compile(r"was\s+demoted\s+from\s+", re.I),
        EventType.PLAYER_DEMOTED,
        Severity.WARNING,
    ),
    # Player promoted
    (
        re.compile(r"was\s+promoted\s+to\s+", re.I),
        EventType.PLAYER_PROMOTED,
        Severity.WARNING,
    ),
    # Parasaur detection
    (
        re.compile(rf"d{_E}t{_E}ct{_E}d[,./\s]*an[-\s]*{_PARASAUR_ENEMY_RE}", re.I),
        EventType.PARASAUR_DETECTION,
        Severity.CRITICAL,
    ),
    # Unclaimed (must be before Claimed — "unclaimed" contains "claimed")
    (
        re.compile(rf"unclaimed\s+{_Q}?", re.I),
        EventType.UNCLAIMED,
        Severity.INFO,
    ),
    # Claimed
    (
        re.compile(rf"claimed\s+{_Q}?", re.I),
        EventType.CLAIMED,
        Severity.INFO,
    ),
    # Egg hatched
    (
        re.compile(r"egg\s+hatched", re.I),
        EventType.EGG_HATCHED,
        Severity.INFO,
    ),
    # Cryopodded / froze (ARK:SA uses "froze" for cryopod)
    (
        re.compile(r"(?:cryopodded|froze)\s+", re.I),
        EventType.CRYOPODDED,
        Severity.INFO,
    ),
    # Released (from cryopod)
    (
        re.compile(r"released\s+", re.I),
        EventType.RELEASED,
        Severity.INFO,
    ),
    # Uploaded
    (
        re.compile(r"uploaded", re.I),
        EventType.UPLOADED,
        Severity.INFO,
    ),
    # Downloaded
    (
        re.compile(r"downloaded", re.I),
        EventType.DOWNLOADED,
        Severity.INFO,
    ),
]


def _parse_time_blob(blob: str) -> str:
    """Extract HH:MM:SS from a garbled time string.

    Finds all digit groups in the blob and maps them to hour, minute, second.
    OCR often loses colons between digits, producing groups like "0905" instead
    of "09" and "05".  Groups with 4+ digits are split into 2-digit pairs
    before mapping.  Missing components default to 0.
    """
    raw_digits = re.findall(r"\d+", blob)

    # Expand groups with 4+ digits into 2-digit pairs (OCR lost the colon)
    digits: list[str] = []
    for d in raw_digits:
        if len(d) >= 4 and len(d) % 2 == 0:
            for i in range(0, len(d), 2):
                digits.append(d[i : i + 2])
        else:
            digits.append(d)

    if len(digits) >= 3:
        return f"{int(digits[0]):02d}:{int(digits[1]):02d}:{int(digits[2]):02d}"
    if len(digits) == 2:
        return f"{int(digits[0]):02d}:{int(digits[1]):02d}:00"
    if len(digits) == 1:
        return f"{int(digits[0]):02d}:00:00"
    return "00:00:00"


_UNKNOWN_LOG_PATH = "unknown_events.log"


def _log_unknown_event(text: str) -> None:
    """Append unrecognized event text to a debug file for investigation."""
    try:
        with open(_UNKNOWN_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} | {text}\n")
    except OSError:
        pass  # Best-effort; don't crash if file can't be written


def _classify(text: str) -> tuple[EventType, Severity]:
    """Classify event text using ordered pattern matching."""
    for pattern, etype, severity in _PATTERNS:
        if pattern.search(text):
            return etype, severity
    log.debug("Unknown event: %s", text)
    _log_unknown_event(text)
    return EventType.UNKNOWN, Severity.INFO


# --- Template-based post-OCR cleanup ---
# Once we know the event type, we know the expected structure. This lets us
# fix OCR artifacts that pure text matching can't.

# Max dino/player level in ARK
_MAX_LEVEL = 500

# Merge split level digits: "Lvl 1 94" → "Lvl 194"
# Matches Lvl + short group (1-2 digits) + space + longer group (1-3 digits)
# when followed by context that confirms it's a single level number:
#   (   → "(Name)" dino name
#   was → "was killed"
#   !   → end of event
#   $   → end of string
_SPLIT_LVL_RE = re.compile(
    r"(Lvl)\s+(\d{1,2})\s+(\d{1,3})(?=\s*[(]|\s+was\s|\s*!|\s*$)",
    re.I,
)

# Dangling "- Lvl" at end with no number (line truncated by OCR)
_DANGLING_LVL_RE = re.compile(r"\s*-\s*Lvl\s*$", re.I)

# Normalize level casing: "LVL" → "Lvl"
_LVL_CASE_RE = re.compile(r"\bLVL\b")


def _merge_split_level(m: re.Match) -> str:
    """Merge split level digits if the combined value is a valid ARK level."""
    prefix = m.group(1)  # "Lvl" or "LVL" etc
    part1 = m.group(2)
    part2 = m.group(3)
    merged = int(part1 + part2)
    if 1 <= merged <= _MAX_LEVEL:
        return f"Lvl {merged}"
    # Not a valid level — leave as-is
    return m.group(0)


def _clean_event_text(text: str) -> str:
    """Apply template-aware corrections to classified event text.

    Fixes OCR artifacts that require structural knowledge to resolve:
    - Split level numbers: "Lvl 1 94" → "Lvl 194"
    - Dangling truncated levels: "- Lvl" at end → removed
    - Level casing: "LVL" → "Lvl"
    """
    text = _SPLIT_LVL_RE.sub(_merge_split_level, text)
    text = _DANGLING_LVL_RE.sub("", text)
    text = _LVL_CASE_RE.sub("Lvl", text)
    return text


def _normalize_ocr_text(text: str) -> str:
    """Fix common OCR artifacts in tribe log text.

    - Lvi / LVI / LvI → Lvl  (lowercase L misread as i/I)
    - Lvl123 → Lvl 123  (missing space between Lvl and digits)
    """
    # "Lvi" / "LvI" / "LVI" → "Lvl" — OCR misreads lowercase L as i/I
    # Use negative lookahead for letters so "LvI131" matches (no word boundary between I and digit)
    text = re.sub(r"\bLv[iI](?=[^a-zA-Z]|$)", "Lvl", text)
    text = re.sub(r"\bLVI\b", "LVL", text)
    # "Lvl123" → "Lvl 123" — Tesseract sometimes glues level to digits
    text = re.sub(r"\bLvl(\d)", r"Lvl \1", text, flags=re.I)
    # Strip stray tildes/carets between dash and Lvl: "- ~ Lvl" → "- Lvl"
    text = re.sub(r"(-\s*)[~^]+\s*(?=Lvl\b)", r"\1", text, flags=re.I)
    return text


# --- Member name extraction from event text ---
# Maps event types to regex patterns that capture the member/player name.
_MEMBER_NAME_PATTERNS: dict[EventType, re.Pattern[str]] = {
    # "Tribemember Alice - Lvl 142 was killed!"
    # Dash before Lvl is optional — OCR may drop it or insert colon/junk
    EventType.TRIBE_MEMBER_KILLED: re.compile(
        r"tribe\s?member\s+(.+?)\s*(?:[-\u2013\u2014]\s*)?(?::?\s*)(?:Lv[l1ti!]|Level)",
        re.I,
    ),
    # "Alice was added to the tribe!"
    EventType.PLAYER_ADDED: re.compile(
        r"^(.+?)\s+was\s+added\s+to\s+the\s+tribe",
        re.I,
    ),
    # "Alice was removed from the tribe!"
    EventType.PLAYER_REMOVED: re.compile(
        r"^(.+?)\s+was\s+removed\s+from\s+the\s+tribe",
        re.I,
    ),
    # "Alice was promoted to Tribe Admin!"
    EventType.PLAYER_PROMOTED: re.compile(
        r"^(.+?)\s+was\s+promoted\s+to\s+",
        re.I,
    ),
    # "Human with ID 556263720 set to Rank Group trust by uncle fuckwit with ID 402662003!"
    EventType.RANK_GROUP_CHANGED: re.compile(
        r"^(.+?)\s+set\s+to\s+rank\s+group\s+",
        re.I,
    ),
    # "Shiki with ID 24175312 was demoted from Tribe Admin by ..."
    EventType.PLAYER_DEMOTED: re.compile(
        r"^(.+?)\s+was\s+demoted\s+from\s+",
        re.I,
    ),
    # "Alice demolished a 'Metal Wall'!"
    EventType.DEMOLISHED: re.compile(
        r"^(.+?)\s+demolished\s+a\s+",
        re.I,
    ),
}


def extract_member_name(raw_text: str, event_type: EventType) -> str | None:
    """Extract the tribe member name from event raw_text based on event type.

    Returns the member name if the event type references a specific player,
    or None for event types that don't (dino killed, structure destroyed, etc).
    """
    pattern = _MEMBER_NAME_PATTERNS.get(event_type)
    if pattern is None:
        return None
    m = pattern.search(raw_text)
    if m:
        return m.group(1).strip()
    return None


def parse_events(ocr_text: str) -> list[TribeLogEvent]:
    """Parse raw OCR text into a list of TribeLogEvent objects."""
    if not ocr_text or not ocr_text.strip():
        return []

    # Normalize common OCR artifacts before parsing
    ocr_text = _normalize_ocr_text(ocr_text)

    # Pre-split: OCR often returns all entries as one blob.
    # Insert newlines before each "Day NNN," boundary.
    ocr_text = _DAY_BOUNDARY_RE.sub("\n", ocr_text)

    # Tesseract preserves visual line breaks from the image, so a single entry
    # like "Day 779, 17:49:29: Adolescent Desmodus -\nLvl 214 (Desmodus) starved
    # to death!" spans two lines. Collapse continuation lines (those not starting
    # with a Day header) into the preceding entry.
    raw_lines = ocr_text.splitlines()
    merged: list[str] = []
    for raw in raw_lines:
        stripped = raw.strip()
        if not stripped:
            continue
        if re.match(r"[DO0d][ae][yv]\s*\d+", stripped):
            merged.append(stripped)
        elif merged:
            merged[-1] += " " + stripped

    now = datetime.now(timezone.utc)
    events: list[TribeLogEvent] = []

    for line in merged:

        m = _LINE_RE.search(line)
        if m:
            day = int(m.group(1))
            time_str = _parse_time_blob(m.group(2))
            event_text = m.group(3).strip()
        else:
            # Fallback: no colon before event text (time completely missing)
            m2 = _LINE_RE_NO_TIME.search(line)
            if not m2:
                log.debug("Unparseable line: %s", line)
                continue
            day = int(m2.group(1))
            time_str = "00:00:00"
            event_text = m2.group(2).strip()
            log.debug("Parsed with no-time fallback: Day %d → %s", day, event_text)

        event_text = _clean_event_text(event_text)
        etype, severity = _classify(event_text)

        events.append(
            TribeLogEvent(
                day=day,
                time=time_str,
                raw_text=event_text,
                event_type=etype,
                severity=severity,
                timestamp=now,
            )
        )

    return events


# --- Tribe window parsing ---

@dataclass(frozen=True)
class TribeMember:
    name: str
    online: bool
    platform_id: str = ""  # steam/platform name from parens; "..." when offline
    group: str = ""        # tribe rank group (e.g. "trust", "onboard"); empty if no groups


@dataclass(frozen=True)
class TribeInfo:
    tribe_name: str
    members_online: int
    members_total: int
    members: list[TribeMember]


# "MEMBERS ONLINE" header with "X / Y" count
_MEMBERS_ONLINE_RE = re.compile(r"(\d+)\s*/\s*(\d+)")

# Member status: name followed by ONLINE or OFFLINE (for multi-line format)
# Tolerates trailing OCR noise (e.g. "ONLINE bee", "OFFLINE |~", "ONLINE ;")
_MEMBER_STATUS_RE = re.compile(r"^(.+?)\s+(ONLINE|OFFLINE)\b.{0,10}$", re.I)

# Leading OCR noise on member names: pipes, dots, colons, angle brackets, question marks, etc.
_MEMBER_LEADING_NOISE_RE = re.compile(r"^[|<>?;:.,!~^*\s]+")


# Known role markers in the tribe window
_ROLE_MARKERS = re.compile(r"\(Owner\)|\(Admin\)|\(Member\)", re.I)

# Trailing platform identifier: "(...)" for offline or "(SteamName)" / "(ID)" for online.
# Matches the last parenthesized group at the end of the name after role markers are stripped.
_PLATFORM_ID_RE = re.compile(r"\s*\(([^)]*)\)\s*$")

# "TRIBE GROUP" UI label
_TRIBE_GROUP_RE = re.compile(r"\bTRIBE\s+GROUP\b", re.I)

# Trailing ONLINE/OFFLINE tokens (greedy from the right)
_TRAILING_STATUSES_RE = re.compile(r"((?:\s+(?:ONLINE|OFFLINE))+)\s*$", re.I)


def _clean_member_name(name: str) -> tuple[str, str]:
    """Strip the trailing platform identifier from a member name.

    ARK shows members as ``PlayerName(SteamName)`` when online and
    ``PlayerName(...)`` when offline.  Without stripping these, the same
    player produces two different store entries across OCR cycles.

    Called *after* role markers (Owner/Admin/Member) have already been removed.

    Returns ``(clean_name, platform_id)`` where *platform_id* is the
    content inside the parens (e.g. ``"BigFrank123"`` or ``"..."``).
    """
    m = _PLATFORM_ID_RE.search(name)
    if m:
        clean = name[:m.start()].strip()
        pid = m.group(1)  # content inside parens
        return clean, pid
    return name.strip(), ""


# Matches a parenthesized token followed by optional trailing text (the group name).
# Group 1 = content inside parens, Group 2 = trailing text after parens (the rank group).
_PLATFORM_AND_GROUP_RE = re.compile(r"\(([^)]*)\)\s*(.*)")


def _extract_member_parts(raw_name: str) -> tuple[str, str, str]:
    """Extract (clean_name, platform_id, group) from a member name string.

    Expected input (after role markers are stripped):
    - ``"uncle fuckwit(...) trust"``  → ("uncle fuckwit", "...", "trust")
    - ``"Shiki(...) onboard"``        → ("Shiki", "...", "onboard")
    - ``"SomePlayer(steam)"``         → ("SomePlayer", "steam", "")
    - ``"PlainName"``                 → ("PlainName", "", "")
    """
    # Find the last opening paren to locate the platform_id group
    paren_open = raw_name.rfind("(")
    if paren_open == -1:
        return raw_name.strip(), "", ""

    m = _PLATFORM_AND_GROUP_RE.search(raw_name, paren_open)
    if not m:
        return raw_name.strip(), "", ""

    clean_name = raw_name[:paren_open].strip()
    platform_id = m.group(1)    # e.g. "...", "BigFrank123"
    group = m.group(2).strip()  # e.g. "trust", "" if nothing after parens
    return clean_name, platform_id, group


def _is_header_line(line: str) -> bool:
    """Check if a line is the 'MEMBERS ONLINE' header (or a garbled version).

    Tesseract often garbles "MEMBERS ONLINE" into partial reads like
    "BERS ONLINE" or "TRIBEGROUP St W SE BERS ONLINE".  We detect these
    by looking for a line that ends with "...BERS ONLINE" (or similar) and
    does NOT look like a real member status line (member lines have a
    player-name-like prefix with parens or mixed case).
    """
    upper = line.upper().strip()
    # Exact header
    if "MEMBERS" in upper and "ONLINE" in upper:
        return True
    # Garbled: line ends with partial "BERS ONLINE" and is mostly uppercase
    # (real member lines have mixed case player names)
    if re.search(r"\bM?E?M?BERS\s+ONLINE\b", upper):
        # Check it's not a real member line — member lines have parens or
        # start with a mixed-case name
        if "(" not in line and line.strip() == line.upper().strip():
            return True
        # Also header if it contains TRIBE or GROUP
        if "TRIBE" in upper or "GROUP" in upper:
            return True
    return False


def _parse_tribe_window_multiline(lines: list[str]) -> TribeInfo | None:
    """Parse tribe window when OCR preserves line breaks."""
    # Find the "MEMBERS ONLINE" header line
    members_idx = None
    for i, line in enumerate(lines):
        if _is_header_line(line):
            members_idx = i
            break

    if members_idx is None:
        return None

    # Tribe name: everything before the MEMBERS ONLINE line
    tribe_name = " ".join(lines[:members_idx]).strip()
    # Strip "TRIBE GROUP" UI label from tribe name
    tribe_name = _TRIBE_GROUP_RE.sub("", tribe_name).strip()
    if not tribe_name:
        tribe_name = "Unknown"

    # Parse online/total count — may be on the header line or a nearby line
    members_online = 0
    members_total = 0
    for search_line in lines[members_idx:members_idx + 3]:
        count_match = _MEMBERS_ONLINE_RE.search(search_line)
        if count_match:
            members_online = int(count_match.group(1))
            members_total = int(count_match.group(2))
            break

    # Parse individual members from lines after the header
    members: list[TribeMember] = []
    for line in lines[members_idx + 1:]:
        m = _MEMBER_STATUS_RE.match(line)
        if m:
            raw_name = _ROLE_MARKERS.sub("", m.group(1)).strip()
            name, platform_id, group = _extract_member_parts(raw_name)
            online = m.group(2).upper() == "ONLINE"
            if name:
                members.append(TribeMember(name=name, online=online, platform_id=platform_id, group=group))

    if members_total == 0 and members:
        members_total = len(members)
        members_online = sum(1 for m in members if m.online)

    return TribeInfo(
        tribe_name=tribe_name,
        members_online=members_online,
        members_total=members_total,
        members=members,
    )


def _parse_tribe_window_flat(text: str) -> TribeInfo | None:
    """Parse tribe window when OCR produces a single flat line.

    Handles WinRT OCR output like:
      "Buckwild Buttnaked TRIBE GROUP uncle fuchwit(bigfranh) Human(...)
       DTM(DanBrem) (Owner) (Admin) MEMBERS ONLINE 1/3 ONLINE OFFLINE OFFLINE"

    Strategy:
      1. Find "MEMBERS ONLINE" + X/Y count to split the text
      2. Collect trailing ONLINE/OFFLINE tokens after the count — these are
         the per-member statuses in order
      3. Extract tribe name (before "TRIBE GROUP" if present)
      4. The text between the tribe header and "MEMBERS ONLINE" contains
         the member names + role markers
      5. Strip roles, then split into N names (where N = number of statuses)
    """
    upper = text.upper()

    # --- Find "MEMBERS ONLINE" and extract count ---
    # Also match garbled OCR like "BERS ONLINE" or "MBERS ONLINE"
    mo_match = re.search(r"M?E?M?BERS\s+ONLINE", upper)
    if mo_match is None:
        return None

    before_header = text[:mo_match.start()]
    after_header = text[mo_match.end():]

    count_match = _MEMBERS_ONLINE_RE.search(after_header)
    if count_match:
        members_online = int(count_match.group(1))
        members_total = int(count_match.group(2))
        after_count = after_header[count_match.end():]
    else:
        members_online = 0
        members_total = 0
        after_count = after_header

    # --- Collect trailing ONLINE/OFFLINE status tokens ---
    statuses: list[bool] = []
    status_match = _TRAILING_STATUSES_RE.search(after_count)
    if status_match:
        tokens = status_match.group(1).strip().split()
        statuses = [t.upper() == "ONLINE" for t in tokens]

    if not statuses:
        return None

    # Use status count as authority for member count if header parsing failed
    if members_total == 0:
        members_total = len(statuses)
        members_online = sum(1 for s in statuses if s)

    # --- Extract tribe name ---
    tg_match = _TRIBE_GROUP_RE.search(before_header)
    if tg_match:
        tribe_name = before_header[:tg_match.start()].strip()
        member_text = before_header[tg_match.end():]
    else:
        # No "TRIBE GROUP" — first few words are the tribe name, rest is members.
        # Heuristic: tribe name is everything before the first parenthesized word
        # that looks like a player name.
        tribe_name = before_header.strip()
        member_text = ""

    if not tribe_name:
        tribe_name = "Unknown"

    # --- Parse member names from the text between tribe header and MEMBERS ONLINE ---
    # Strip role markers
    member_text = _ROLE_MARKERS.sub(" ", member_text)
    # Strip OCR artifacts: short lowercase tokens trailing a closing paren are
    # partial reads of role markers, e.g. "(Admin)" → "aa", "(Owner)" → "ow".
    member_text = re.sub(r"\)\s+[a-z]{1,3}(?=\s|$)", ")", member_text)
    # Collapse whitespace
    member_text = re.sub(r"\s+", " ", member_text).strip()

    members: list[TribeMember] = []
    if member_text and len(statuses) > 0:
        # Split member_text into N chunks where N = number of status tokens.
        # Member names may contain spaces and parens, so we can't naively split on space.
        # Strategy: split on boundaries where a lowercase/paren char is followed by
        # an uppercase letter starting a new word that isn't inside parens.
        # Simpler: use the known count to greedily split.
        parts = _split_member_names(member_text, len(statuses))
        for i, raw_name in enumerate(parts):
            if i < len(statuses) and raw_name:
                name, platform_id, group = _extract_member_parts(raw_name.strip())
                if name:
                    members.append(TribeMember(name=name, online=statuses[i], platform_id=platform_id, group=group))
    elif len(statuses) > 0 and not member_text:
        # No member names parsed — create placeholder entries
        for i, online in enumerate(statuses):
            members.append(TribeMember(name=f"Member {i + 1}", online=online))

    return TribeInfo(
        tribe_name=tribe_name,
        members_online=members_online,
        members_total=members_total,
        members=members,
    )


def _split_member_names(text: str, count: int) -> list[str]:
    """Split a flat string of member names into *count* parts.

    Player names in ARK often follow the pattern "DisplayName(SteamName)" or just
    a plain name. We look for boundaries where a closing paren or plain word is
    followed by whitespace and then a new capitalized name or a new name with parens.

    Falls back to even splitting if heuristics fail.
    """
    if count <= 1:
        return [text]

    # Try splitting on boundaries: ") Name" or "word Name(" patterns
    # A boundary is: end of a paren group or a word, then space, then start of a
    # new token that begins with an uppercase letter or has its own parens.
    # Pattern: look for ") <UpperCase>" or "word <UpperCase>(" transitions
    candidates: list[int] = []
    i = 0
    in_parens = 0
    while i < len(text):
        ch = text[i]
        if ch == "(":
            in_parens += 1
        elif ch == ")":
            in_parens = max(0, in_parens - 1)
            # After closing paren + space + uppercase = likely boundary
            if in_parens == 0 and i + 1 < len(text) and text[i + 1] == " ":
                j = i + 2
                while j < len(text) and text[j] == " ":
                    j += 1
                if j < len(text) and (text[j].isupper() or text[j].isdigit()):
                    candidates.append(j)
        i += 1

    # Pick the best split points.
    # Always prefer paren-boundary candidates over word splitting — player names
    # can contain spaces (e.g. "uncle fuckwit(bigfrank)") so word splitting
    # would break them apart.
    if candidates:
        splits = candidates[:count - 1]
        parts = []
        prev = 0
        for s in splits:
            parts.append(text[prev:s].strip())
            prev = s
        parts.append(text[prev:].strip())
        return parts

    # No paren boundaries at all — fall back to returning the whole string
    # as a single part rather than guessing word boundaries.
    return [text]


def parse_tribe_window(ocr_text: str) -> TribeInfo | None:
    """Parse the tribe window OCR text into a TribeInfo.

    Handles both multi-line OCR output (line breaks between rows) and
    flat single-line output from WinRT OCR.

    Returns None if the text can't be parsed.
    """
    if not ocr_text or not ocr_text.strip():
        return None

    lines = [l.strip() for l in ocr_text.splitlines() if l.strip()]
    if not lines:
        return None

    # If we have multiple lines with ONLINE/OFFLINE on individual lines,
    # use the multi-line parser
    status_lines = sum(
        1 for l in lines if _MEMBER_STATUS_RE.match(l)
    )
    if status_lines > 0 and len(lines) > 2:
        result = _parse_tribe_window_multiline(lines)
        if result and result.members:
            return result

    # Otherwise fall back to flat single-line parsing
    # Join all lines into one string (in case there are some line breaks
    # but not between every row)
    flat = " ".join(lines)
    return _parse_tribe_window_flat(flat)


# --- Parasaur screen notification parsing ---

# Primary match: just "detected an enemy" — only parasaurs produce this in ARK.
# OCR-tolerant for observed variations:
#   "detected anenemy"   — missing space
#   "detected,an enemy"  — comma after detected
#   "detected an @nemy"  — @ for e
#   "detected an énemy"  — accented e
#   "detécted an enemy"  — accented e in detected
_PARASAUR_DETECTED_RE = re.compile(
    rf"d{_E}t{_E}ct{_E}d[,./\s]*an[-\s]*{_PARASAUR_ENEMY_RE}",
    re.I,
)

# Detail extraction: Name - Lvl 69 (Parasaur) detected an enemy!
# ARK:SA format uses no quotes (e.g. "EnemiesOnly - Lvl 69 (Parasaur)").
# OCR can produce commas/periods between Lvl and the number, and leading
# garbage from background text.  Parasaur names never contain spaces, so
# \S+ captures only the last contiguous token before " - Lvl", naturally
# discarding any OCR noise that precedes the real name.
# Separator between name and Lvl: OCR reads - as =, :, ;, comma, etc.
_PARASAUR_SEP = r"[-=:;,.'\"\u2013\u2014]"  # OCR-broadened dash/separator
_PARASAUR_DETAIL_RE = re.compile(
    rf"(\S+)\s*{_PARASAUR_SEP}+\s*Lv[l1i][l,.\s]*(\d+)\s*\(Parasaur\)\s*d{_E}t{_E}ct{_E}d[,./\s]*an[-\s]*{_PARASAUR_ENEMY_RE}",
    re.I,
)

# Strip leading OCR garbage (icon glyphs like <b>, brackets, etc.) from parasaur name
_PARASAUR_NAME_CLEAN_RE = re.compile(r"^(?:<[^>]*>|\s|[^A-Za-z0-9'\"])+")

# --- Fuzzy matching fallback for garbled parasaur OCR ---
_FUZZY_TARGET = "detected an enemy"
_FUZZY_THRESHOLD = 0.6


def _fuzzy_parasaur_detected(text: str) -> bool:
    """Fuzzy fallback: does *text* contain something close to 'detected an enemy'?

    Uses ``difflib.SequenceMatcher`` on a small region after the word
    "Parasaur" (which OCR almost always reads correctly).  Returns True
    when the best sliding-window similarity ≥ ``_FUZZY_THRESHOLD``.
    """
    text_lower = text.lower()
    # "Parasaur" is almost always readable — use it as anchor.
    m = re.search(r"par.?s.?aur", text_lower)
    if not m:
        return False

    # The key phrase follows "(Parasaur) " — search a 45-char window after it.
    search_start = max(0, m.end() - 2)
    search_end = min(len(text_lower), m.end() + 45)
    region = text_lower[search_start:search_end]

    target = _FUZZY_TARGET
    tlen = len(target)

    # Slide windows of varying size across the region.
    for wlen in range(max(tlen - 5, 8), tlen + 7):
        for i in range(max(len(region) - wlen + 1, 0)):
            candidate = region[i : i + wlen]
            if SequenceMatcher(None, target, candidate).ratio() >= _FUZZY_THRESHOLD:
                return True
    return False


def parse_parasaur_notification(
    ocr_text: str,
    parasaur_lookup: dict[str, str] | None = None,
) -> list[TribeLogEvent]:
    """Parse parasaur detection notifications from screen OCR text.

    Matches on "detected an enemy" — only parasaurs produce this in ARK.
    Optionally extracts name/level if the full pattern is readable.

    parasaur_lookup maps parasaur name → mode ("enemy" or "babies").
    Unknown names default to "enemy" (CRITICAL).
    """
    if not ocr_text or not ocr_text.strip():
        return []

    if parasaur_lookup is None:
        parasaur_lookup = {}

    now = datetime.now(timezone.utc)
    events: list[TribeLogEvent] = []

    for m in _PARASAUR_DETECTED_RE.finditer(ocr_text):
        # Try to extract name/level from surrounding context
        # Search backwards from the match for the full detail pattern
        line_start = ocr_text.rfind("\n", 0, m.start()) + 1
        line_end = ocr_text.find("\n", m.end())
        if line_end == -1:
            line_end = len(ocr_text)
        line = ocr_text[line_start:line_end]

        detail = _PARASAUR_DETAIL_RE.search(line)
        if detail:
            name = _PARASAUR_NAME_CLEAN_RE.sub("", detail.group(1)).strip().strip('"\'-=:;,.')
            level = detail.group(2)
            # Determine mode: explicit config → name-based auto-detect → default
            mode = parasaur_lookup.get(name)
            if mode == "enemy":  # legacy config value → treat as "player"
                mode = "player"
            if mode is None:
                name_lower = name.lower()
                if "bab" in name_lower or "babies" in name_lower:
                    mode = "babies"
                elif "player" in name_lower:
                    mode = "player"
                else:
                    mode = "unknown"
            raw_text = f"{name} - Lvl {level} (Parasaur) detected an enemy! [{mode}]"
        else:
            name = ""
            mode = "unknown"
            raw_text = f"Parasaur detected an enemy! [{mode}]"

        if mode == "babies":
            event_type = EventType.PARASAUR_BABIES
            severity = Severity.INFO
        else:
            event_type = EventType.PARASAUR_DETECTION
            severity = Severity.CRITICAL

        events.append(
            TribeLogEvent(
                day=0,
                time="00:00:00",
                raw_text=raw_text,
                event_type=event_type,
                severity=severity,
                timestamp=now,
            )
        )

    # Fuzzy fallback: regex missed but text is close enough to a parasaur alert.
    if not events and _fuzzy_parasaur_detected(ocr_text):
        log.debug("Fuzzy parasaur match on: %s", ocr_text[:120])
        events.append(
            TribeLogEvent(
                day=0,
                time="00:00:00",
                raw_text="Parasaur detected an enemy! [unknown]",
                event_type=EventType.PARASAUR_DETECTION,
                severity=Severity.CRITICAL,
                timestamp=now,
            )
        )

    return events


# --- Join/Leave notification parsing ---

@dataclass(frozen=True)
class JoinLeaveEvent:
    platform_id: str
    is_join: bool      # True = joined, False = left
    raw_text: str
    timestamp: datetime


# OCR-tolerant patterns for tribe member join/leave notifications.
# Only matches "Tribemember" prefix (our tribe's members).
# Case-insensitive — the game renders "ARK" in all caps.
# Single OCR-tolerant regex: captures name and the verb portion between "has" and "ark".
# Handles garbled OCR like "hasiet" (has left), "joinedthis", missing spaces, etc.
# Direction is determined by checking if "join" appears in the verb portion.
_JOIN_LEAVE_RE = re.compile(
    r"tribe\s?member\s*(.+?)(?:\s+|(?=has))has\s*(.+?)\s*(?:ark|server)",
    re.IGNORECASE,
)


def parse_join_leave_notifications(ocr_text: str) -> list[JoinLeaveEvent]:
    """Parse join/leave notifications from parasaur capture area OCR text.

    Matches "Tribemember <platform_id> has joined/left the ark" messages.
    Uses a single flexible regex to handle OCR garbling (missing spaces,
    mangled words like "hasiet" for "has left", "joinedthis", etc.).
    Direction is inferred: if "join" appears in the verb portion it's a join,
    otherwise it's a leave.
    Returns a list of JoinLeaveEvent objects.
    """
    if not ocr_text or not ocr_text.strip():
        return []

    now = datetime.now(timezone.utc)
    events: list[JoinLeaveEvent] = []
    seen: set[tuple[str, bool]] = set()  # dedup within same OCR frame

    for m in _JOIN_LEAVE_RE.finditer(ocr_text):
        pid = m.group(1).strip()
        verb = m.group(2).strip().lower()
        is_join = "join" in verb
        if pid and (pid, is_join) not in seen:
            seen.add((pid, is_join))
            events.append(JoinLeaveEvent(
                platform_id=pid,
                is_join=is_join,
                raw_text=m.group(0),
                timestamp=now,
            ))

    return events


# --- Non-tribemate server join parsing ---

@dataclass(frozen=True)
class ServerJoinEvent:
    player_name: str
    raw_text: str
    timestamp: datetime


# General "has joined ... ark/server" regex — matches both tribemate and non-tribemate joins.
# We exclude tribemate matches by span overlap in parse_server_join_notifications().
_SERVER_JOIN_RE = re.compile(
    r"(.+?)(?:\s+|(?=has))has\s*(?:join\w*)\s*(?:this|the)?\s*(?:ark|server)",
    re.IGNORECASE,
)

# Filter: names that are clearly OCR noise (only punctuation/symbols/whitespace)
_NOISE_RE = re.compile(r"^[\s\W]+$")

# Strip leading OCR bracket noise: [!], [k, [i], i.[, i.[ etc.
# ARK shows a [!] icon before join notifications; OCR garbles it into various
# short sequences mixing brackets, dots, and single chars.  We match any
# prefix that is <=4 chars of non-alpha + at most 1 letter, followed by a space.
_LEADING_BRACKET_RE = re.compile(
    r"^"
    r"(?:"
    r"  [<\[].?[>\]]?"    # [!]  <!>  [k  <i>  etc.
    r"| .{0,3}[<\[]"      # i.[  i.<  .[  etc.  (up to 3 chars then bracket/angle)
    r"| [.!|<>]{1,3}"     # ...  !!  <!  etc.
    r")"
    r"\s+",               # must be followed by whitespace
    re.VERBOSE,
)
# Strip trailing OCR noise (stray brackets, angles, punctuation)
_TRAILING_NOISE_RE = re.compile(r"[\[\]<>.!|]+$")


def parse_server_join_notifications(ocr_text: str) -> list[ServerJoinEvent]:
    """Parse non-tribemate join notifications from parasaur capture area OCR text.

    Uses a two-pass approach:
    1. Find all tribemate join/leave match spans (to exclude)
    2. Find all general "X has joined ... ark" matches
    3. Return only matches that don't overlap with tribemate spans

    Returns a list of ServerJoinEvent objects (deduped by player_name within frame).
    """
    if not ocr_text or not ocr_text.strip():
        return []

    # Pass 1: collect tribemate match spans
    tribemate_spans: list[tuple[int, int]] = []
    for m in _JOIN_LEAVE_RE.finditer(ocr_text):
        tribemate_spans.append((m.start(), m.end()))

    # Pass 2: collect all general join matches
    now = datetime.now(timezone.utc)
    events: list[ServerJoinEvent] = []
    seen: set[str] = set()

    for m in _SERVER_JOIN_RE.finditer(ocr_text):
        # Skip if this match overlaps with any tribemate match
        m_start, m_end = m.start(), m.end()
        overlaps = any(
            not (m_end <= ts or m_start >= te)
            for ts, te in tribemate_spans
        )
        if overlaps:
            continue

        name = m.group(1).strip()

        # Strip leading OCR bracket noise: [!], [k, etc.
        name = _LEADING_BRACKET_RE.sub("", name).strip()
        # Strip trailing bracket noise
        name = _TRAILING_NOISE_RE.sub("", name).strip()

        # Filter noise: too short, only punctuation, etc.
        if len(name) < 2:
            continue
        if _NOISE_RE.match(name):
            continue

        # Dedup within same OCR frame
        name_lower = name.lower()
        if name_lower in seen:
            continue
        seen.add(name_lower)

        events.append(ServerJoinEvent(
            player_name=name,
            raw_text=m.group(0),
            timestamp=now,
        ))

    return events
