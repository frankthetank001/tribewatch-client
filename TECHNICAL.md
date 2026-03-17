# Technical Documentation

Developer reference for the TribeWatch client. For features and screenshots, see [README.md](README.md).

---

## Architecture

TribeWatch is a two-component system:

| Component | Role | Stack |
|-----------|------|-------|
| **Client** (this repo) | Screen capture, OCR, event parsing, Discord alerts, WebSocket relay | Python 3.12+, asyncio, aiohttp, mss, PaddleOCR/WinRT |
| **Server** (separate) | Event storage, web dashboard, tribe management, multi-client coordination | Python 3.12+, FastAPI, SQLite (aiosqlite), WebSocket |

The client connects to the server over WebSocket (`wss://`), authenticating with a shared token. Events flow client → server. Control commands (pause, resume, screenshot, reconnect) flow server → client.

```
Client                              Server
TribeWatchApp                       FastAPI + WebSocketManager
  ├─ ScreenCapture (mss)              ├─ RelayManager
  ├─ OCR Engine                       │   └─ ClientHandler[] per tribe
  ├─ Parser (regex)                   ├─ EventStore (SQLite)
  ├─ DedupStore                       ├─ TribeStore (SQLite)
  ├─ WebhookDispatcher (Discord)      ├─ ActivityStore (SQLite)
  ├─ ServerRelay (WebSocket)          ├─ GeneratorStore (SQLite)
  └─ ReconnectManager                 ├─ TodoStore (SQLite)
                                      ├─ CalendarStore (SQLite)
                                      └─ Web UI (static JS SPA)
```

### Deployment Modes

| Mode | What runs | Config file |
|------|-----------|-------------|
| **Client** (default for exe) | Client only — connects to remote server | `tribewatch_client.toml` |
| **Server** | Server only — receives from clients | `tribewatch.toml` |
| **Standalone** | Both in one process (dev/testing) | Both files merged via `load_config_pair` |

The PyInstaller build uses `client_main.py` as entry point, which only exposes client-mode flags.

---

## Client Pipeline

The main loop in `TribeWatchApp` runs three concurrent async cycles:

### 1. Tribe Log Cycle (`_tribe_log_cycle`)

```
capture screenshot (mss) → crop to bbox → upscale (Pillow) → OCR → parse_events() → dedup → Discord + relay
```

- **Interval**: configurable, default 2 seconds
- **OCR engines**: `winrt` (Windows.Media.Ocr), `tesseract`, `paddleocr` (rapidocr-onnxruntime), `easyocr`
- **Parser**: `parse_events()` in `parser.py` — 30+ regex patterns with OCR-tolerant alternatives
- **Dedup**: `DedupStore` — hash + fuzzy + count-aware + high-water-mark validation

### 2. Parasaur Cycle (`_parasaur_cycle`)

Separate capture region for top-screen parasaur notifications. Faster polling (1s). Grace period (30s) before promoting to sustained alert. "All clear" after configurable silence.

### 3. Tribe Window Cycle (`_tribe_cycle`)

Reads the tribe member list from the in-game tribe window. Fuzzy-matches names against the roster. Sends member presence updates to the server.

### Heartbeat

`_relay_heartbeat_loop()` sends periodic status to the server including:
- Monitoring state (active/paused/idle)
- Online members
- Server info (from EOS)
- Tribe name/ID
- EOS server metadata (player count, map, day/time)

---

## Event Types

Defined in `parser.py` as `EventType` enum. Each has:
- Regex pattern(s) with OCR-tolerant alternatives
- Default severity (critical / warning / info)
- Extracted fields (player name, dino name, level, structure name, etc.)

```python
class EventType(Enum):
    STRUCTURE_DESTROYED = "structure_destroyed"
    DINO_KILLED = "dino_killed"
    TRIBE_MEMBER_KILLED = "tribe_member_killed"
    ANTI_MESHED = "anti_meshed"
    DINO_STARVED = "dino_starved"
    PLAYER_ADDED = "player_added"
    PLAYER_REMOVED = "player_removed"
    PLAYER_PROMOTED = "player_promoted"
    PLAYER_DEMOTED = "player_demoted"
    RANK_GROUP_CHANGED = "rank_group_changed"
    DINO_TAMED = "dino_tamed"
    DEMOLISHED = "demolished"
    TRIBE_DESTROYED = "tribe_destroyed"
    ENEMY_DINO_KILLED = "enemy_dino_killed"
    ENEMY_PLAYER_KILLED = "enemy_player_killed"
    ENEMY_STRUCTURE_DESTROYED = "enemy_structure_destroyed"
    AUTO_DECAY = "auto_decay"
    CLAIMED = "claimed"
    UNCLAIMED = "unclaimed"
    UPLOADED = "uploaded"
    DOWNLOADED = "downloaded"
    EGG_HATCHED = "egg_hatched"
    CRYOPODDED = "cryopodded"
    RELEASED = "released"
    PARASAUR_DETECTION = "parasaur_detection"
    PARASAUR_BABIES = "parasaur_babies"
    PARASAUR_BRIEF = "parasaur_brief"
    PARASAUR_BRIEF_BABIES = "parasaur_brief_babies"
    MEMBER_JOINED_ARK = "member_joined_ark"
    MEMBER_LEFT_ARK = "member_left_ark"
    BLACKLIST_JOIN = "blacklist_join"
    UNKNOWN = "unknown"
```

---

## Deduplication (`dedup.py`)

`DedupStore` prevents duplicate alerts from the same visible tribe log.

### Layers

1. **Normalization** — strip punctuation, collapse whitespace, lowercase
2. **Hash** — SHA-256 of `day:time:normalized_text`. Exact match = skip.
3. **Fuzzy** — for same day/time, SequenceMatcher >= 0.97 against recent hashes. Catches OCR variance between frames.
4. **Count-aware** — tracks how many times each hash appears. Allows genuine duplicates (two identical structures destroyed) while suppressing retransmissions.
5. **High-water mark** — tracks the latest `(day, time)` seen. Rejects events with day/time earlier than the high-water mark (prevents re-alerting on old log entries scrolling into view). Includes `_MAX_DAY_JUMP=200` guard against OCR garbling the day number.

### EOS Reference

`set_eos_reference(day, time)` — feeds authoritative day/time from the EOS API. If set, the high-water validation uses EOS day as a sanity check: OCR day must be within `eos_day + _MAX_DAY_JUMP`.

`seed_high_water_from_eos(day, time)` — on first startup with an empty state, seeds the high-water mark from EOS to prevent old-event flood.

### State Persistence

Per-server JSON state file (`tribewatch_state_{server_id}.json`) with hash counts and high-water mark. Migrated automatically on server change.

---

## Discord Integration (`webhook.py`)

`WebhookDispatcher` manages multiple Discord webhooks with batching, rate limiting, and retry.

### Webhooks

| Key | Purpose |
|-----|---------|
| `alert_webhook` | General tribe log events |
| `raid_webhook` | Critical events only (structures destroyed, dinos killed, etc.) |
| `debug_webhook` | All events including info-level (for debugging) |
| `tasks_webhook` | Todo/calendar notifications (falls back to alert_webhook) |

### Batching

Events are queued and flushed every `batch_interval` seconds (default 15). Critical events bypass the queue and send immediately. Flush produces a single embed with all queued events grouped by type.

### Escalation

Configured per-event-type: if N events of the same type occur within M minutes, fire an escalation alert with a summary of all events in the window. Optionally suppress individual event alerts when escalation is active.

### Mention Resolution

`resolve_mention(name, config)` resolves symbolic names:
- `!owner` → owner Discord ID from config
- `role:name` → role ID from Discord guild
- Raw Discord user/role IDs pass through
- Named mentions from `discord.mentions` dict in config

### Rate Limiting

Tracks `X-RateLimit-Remaining` and `X-RateLimit-Reset` headers. Queues retries on 429 responses.

---

## Reconnect System (`reconnect.py`)

Multi-stage automated reconnect sequence:

| Stage | Action |
|-------|--------|
| 1 | Kill ARK process (`taskkill /f /im ArkAscended.exe`) |
| 2 | Launch via Steam protocol (`steam://run/2399830`) |
| 3 | Wait for title screen — OCR for "JOIN LAST SESSION" |
| 4 | Click "Join Last Session" |
| 5 | Wait for game load — OCR for tribe log text |
| 6 | Open tribe log (press L key, retry up to 3x) |
| **Fallback** | If "Join Last Session" fails: dismiss overlay → click "JOIN GAME" → server browser → search by server ID → click server → click JOIN |

**Backoff**: initial 30s, exponential growth, caps at 30 minutes.

**Console commands**: after successful reconnect, auto-pastes commands from `scripts/ini.txt` via the game console.

**Progress reporting**: each stage sends status + JPEG screenshot to the server for monitoring via the web UI.

---

## EOS Integration (`eos.py`)

Async client for the Epic Online Services matchmaking API using the same public ARK credentials as the official server browser.

```python
class AsyncEOSClient:
    async def _ensure_auth(self) -> None          # OAuth token + refresh
    async def query_servers(self, criteria) -> list[dict]
    async def get_server_by_name(self, name) -> dict | None
```

`extract_server_info(session)` pulls structured data from a raw EOS session:
- `total_players`, `max_players`
- `map_name`
- `is_pve`
- `daytime_raw` → parsed to `(day, time)` via `parse_eos_daytime()`

Queried on startup and refreshed every 5 minutes. Results cached in `TribeWatchApp._eos_info` and included in the heartbeat status.

---

## Configuration (`config.py`)

TOML-based config with typed dataclass schemas.

### Config Files

| File | Used by | Contains |
|------|---------|----------|
| `tribewatch_client.toml` | Client | `[server]`, `[tribe_log]`, `[general]`, `[parasaur]`, `[tribe]`, `[discord]`, `[alerts]` |
| `tribewatch.toml` | Server | `[web]`, `[presence]`, `[generator]`, `[todo_summary]`, `[discord_notifications]` + above |

Standalone mode merges both via `load_config_pair()`.

### Environment Overrides

| Variable | Field |
|----------|-------|
| `TRIBEWATCH_AUTH_TOKEN` | `server.auth_token` |
| `TRIBEWATCH_PORT` | `web.port` |
| `TRIBEWATCH_HOST` | `web.host` |
| `TRIBEWATCH_LOG_LEVEL` | `general.log_level` |
| `TRIBEWATCH_SERVER_URL` | `server.server_url` |

### Resolution Presets

`_apply_resolution_preset(cfg)` reads the game's resolution from `GameUserSettings.ini` and applies pre-calibrated bboxes for tribe_log, parasaur, and tribe window regions. Presets exist for 1280x720, 1920x1080, 2560x1080. Unknown resolutions fall back to proportional scaling from the 1080p baseline (same aspect ratio only).

---

## Fuzzy Matching (`fuzzy.py`)

Shared utilities for OCR-tolerant name comparison, used by both client and server.

```python
def edit_distance(s1, s2) -> int        # Levenshtein
def fuzzy_threshold(name) -> int        # 1 for <=5 chars, 2 for <=15, 3 for >15
def names_match(saved, detected) -> bool # prefix match OR edit distance <= threshold
```

---

## Server Change Detection

`TribeWatchApp.build_status()` reads the server name from `GameUserSettings.ini` (the `LastServerName` field). When it changes:

1. `_paused = True` set synchronously (prevents event leakage)
2. User prompted via MessageBox (accept/reject server change)
3. If accepted: dedup state file migrated to new `server_id`, monitoring resumes
4. Status heartbeat includes `server_id` and `server_name`

---

## Tribe Name Change Detection

During `_tribe_cycle()`, if the OCR'd tribe name doesn't fuzzy-match the configured name:

1. User prompted with three options:
   - **Rename** — update the existing tribe's name across all stores
   - **New tribe** — register as a separate tribe
   - **Ignore** — keep the current name (OCR noise)
2. Rename propagates via `TribeStore.rename_tribe()` and `EventStore.rename_tribe()`

---

## Building

### PyInstaller

`tribewatch.spec` defines a single-folder build with `client_main.py` as entry point. Excludes server packages (fastapi, uvicorn, aiosqlite) and heavy unused deps (torch, easyocr, full paddlepaddle).

```bash
pip install -e . && pip install pyinstaller
python build.py              # dist/TribeWatch/
python build.py --installer  # dist/TribeWatch-Setup.exe (requires Inno Setup 6)
```

### Release Workflow

`.github/workflows/release.yml` triggers on `v*` tags:
1. Checkout → Python 3.12 → `pip install -e . && pip install pyinstaller`
2. Install Inno Setup via Chocolatey
3. `python build.py --installer`
4. ZIP the portable build
5. Create draft GitHub Release with both assets

### Hidden Imports

The spec includes `hiddenimports` for:
- `tribewatch.eos` — lazy-loaded, not caught by PyInstaller analysis
- `tribewatch.updater` — same
- `tribewatch.reconnect` — same
- `rapidocr_onnxruntime` — OCR engine loaded by string name

---

## CLI Reference

| Flag | Description |
|------|-------------|
| `--setup` | Guided setup wizard — walks through config generation |
| `--config PATH` | Override config file path (default: `tribewatch_client.toml`) |
| `--calibrate` | Visual overlay (tkinter) to drag-select tribe log region |
| `--calibrate-manual` | Enter bbox coordinates as numbers |
| `--calibrate-parasaur` | Drag-select parasaur notification region |
| `--calibrate-tribe` | Drag-select tribe window region |
| `--test-ocr` | Single capture → OCR → print results → exit |
| `--test-discord` | Send test embed to configured webhooks → exit |
| `--run` | Start client (used by installer registry startup entry) |
| `--version` | Print version and exit |

No arguments = start client (same as `--run`).

---

## Dependencies

### Required

| Package | Purpose |
|---------|---------|
| `winocr` | Windows.Media.Ocr (WinRT) — fastest OCR engine |
| `mss` | Cross-platform screen capture |
| `Pillow` | Image processing, upscaling |
| `aiohttp` | WebSocket client, HTTP for Discord/EOS |
| `tomli-w` | TOML config writing |
| `python-dotenv` | `.env` file loading |
| `rapidocr-onnxruntime` | PaddleOCR via ONNX Runtime — best accuracy |

### Optional

| Package | Purpose |
|---------|---------|
| `pytesseract` | Tesseract OCR engine |
| `pyautogui` | Auto-reconnect (keyboard/mouse automation) |
