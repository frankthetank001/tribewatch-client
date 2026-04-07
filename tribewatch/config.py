"""Configuration loading, validation, and generation for TribeWatch."""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import tomli_w


@dataclass
class TribeLogConfig:
    bbox: list[int] = field(default_factory=lambda: [0, 0, 800, 600])
    interval: float = 2.0
    ocr_engine: str = "paddleocr"  # winrt / tesseract / easyocr / paddleocr
    upscale: int = 2
    tesseract_path: str = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


@dataclass
class DiscordConfig:
    alert_webhook: str = ""
    raid_webhook: str = ""
    debug_webhook: str = ""
    tasks_webhook: str = ""
    ping_role_id: str = ""
    batch_interval: int = 15
    mentions: dict[str, str] = field(default_factory=dict)
    owner_discord_id: str = ""  # tribe owner's Discord user ID; use "!owner" in ping fields
    guild_id: str = ""  # Discord guild (server) ID; enables role-based auto-provisioning


@dataclass
class AlertRule:
    event_type: str = ""
    action: str = "batch"  # "critical", "batch", or "ignore"
    severity_override: str = ""  # "", "critical", "warning", "info"
    discord: bool = True  # send to Discord at all
    ping: bool = False  # include @mention
    ping_target: str = ""  # role/user ID to ping (empty = use global ping_role_id)
    escalation_count: int = 0       # 0 = disabled
    escalation_window: int = 10     # minutes
    escalation_target: str = ""     # role ID (empty = fall back to ping_target / global)
    suppress_individual: bool = False  # suppress per-event alerts when escalation is active
    ping_member: bool = False  # @mention the specific tribe member involved (requires discord_id)
    text_contains: str = ""  # substring match against raw_text (case-insensitive)


@dataclass
class AlertsConfig:
    rules: list[AlertRule] = field(default_factory=list)
    idle_alert_minutes: int = 10
    idle_recovery_alert: bool = True
    idle_ping: bool = False
    idle_ping_target: str = ""  # role/user ID (empty = use global ping_role_id)
    never_active_alert: bool = True  # alert if client connected but monitoring never starts
    reconnect_alert: bool = True  # Discord alert on auto-reconnect success/failure
    # Presence alerts (consolidated from former [presence] section)
    offline_webhook: bool = False  # Discord alert on client offline
    online_webhook: bool = False  # Discord alert on client back online
    presence_webhook_url: str = ""  # empty = use discord.alert_webhook


@dataclass
class GeneralConfig:
    log_level: str = "INFO"
    state_file: str = "tribewatch_state.json"
    monitor: int = 0  # monitor index for screen capture
    window_title: str = "ArkAscended"  # window capture by title; empty = full screen capture
    calibration_resolution: list[int] = field(default_factory=list)  # [width, height] at calibration time


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8777
    base_url: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    session_secret: str = ""  # auto-generated; persisted so sessions survive restarts
    admin_discord_id: str = ""  # server admin's Discord user ID — always gets admin access
    bot_token: str = ""  # Discord bot token (platform-level, shared across all tribes)
    application_id: str = ""  # Discord application ID (for slash commands)
    public_key: str = ""  # Discord Ed25519 public key (for interaction verification)


@dataclass
class ParasaurEntry:
    name: str = ""
    mode: str = "player"  # "player" or "babies"


@dataclass
class ParasaurAlertSettings:
    """Dispatch settings for one parasaur detection mode (player or babies)."""
    discord: bool = True        # send to Discord
    action: str = "critical"    # "critical", "batch", or "ignore"
    ping: bool = False          # include @mention
    ping_target: str = ""       # role/user ID (empty = use global)
    brief_action: str = ""      # "" = inherit action, "critical"/"batch"/"ignore"
    brief_ping: bool = False


@dataclass
class ParasaurConfig:
    bbox: list[int] = field(default_factory=list)  # empty = disabled
    interval: float = 1.0  # faster — notifications are fleeting
    clear_delay: float = 30.0  # seconds of silence before declaring "all clear"
    grace_period: float = 30.0  # seconds of detection before promoting to sustained
    ocr_engine: str = ""  # "" = use tribe_log.ocr_engine
    parasaurs: list[ParasaurEntry] = field(default_factory=list)
    player_alerts: ParasaurAlertSettings = field(
        default_factory=lambda: ParasaurAlertSettings(action="critical"),
    )
    babies_alerts: ParasaurAlertSettings = field(
        default_factory=lambda: ParasaurAlertSettings(discord=False, action="batch"),
    )


@dataclass
class TribeConfig:
    bbox: list[int] = field(default_factory=list)  # empty = disabled
    interval: float = 30.0  # tribe window changes infrequently
    tribe_name: str = ""  # confirmed tribe name (empty = not yet discovered)
    ocr_engine: str = "paddleocr"  # RapidOCR (PaddleOCR models via ONNX, no paddlepaddle needed)
    offline_grace_seconds: float = 60.0  # seconds after tribe window disappears before setting members offline


@dataclass
class TodoSummaryConfig:
    enabled: bool = False           # off by default
    frequency: str = "daily"        # "daily" or "weekly"
    time: str = "09:00"             # HH:MM in local time
    day_of_week: int = 0            # 0=Monday, only used when frequency="weekly"
    include_completed: bool = False  # show recently completed items


@dataclass
class DiscordNotificationsConfig:
    list_created: bool = True       # send Discord embed when a task list is created/updated
    task_completed: bool = True     # send Discord embed when a task is completed
    task_assigned: bool = True      # send Discord embed when a task is assigned
    event_created: bool = True      # send Discord embed when a calendar event is created
    event_updated: bool = True      # send Discord embed when a calendar event is updated

@dataclass
class GeneratorConfig:
    discord: bool = True          # global toggle for generator Discord pings
    ping_target: str = ""         # default ping target (empty = use discord.ping_role_id)


@dataclass
class ServerConfig:
    mode: str = "standalone"  # "standalone", "client", or "server"
    server_url: str = "https://tribewatch.fly.dev"  # Server URL
    auth_token: str = ""  # shared secret for relay authentication
    client_token: str = ""  # signed Discord OAuth token for client→server auth
    reconnect_delay: float = 5.0  # seconds between reconnect attempts
    heartbeat_interval: float = 10.0  # seconds between status heartbeats


@dataclass
class PresenceConfig:
    """Legacy — presence fields now live in AlertsConfig. Kept for backward compat."""
    offline_webhook: bool = False
    online_webhook: bool = False
    webhook_url: str = ""


@dataclass
class TribeWatchConfig:
    tribe_log: TribeLogConfig = field(default_factory=TribeLogConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    general: GeneralConfig = field(default_factory=GeneralConfig)
    web: WebConfig = field(default_factory=WebConfig)
    parasaur: ParasaurConfig = field(default_factory=ParasaurConfig)
    tribe: TribeConfig = field(default_factory=TribeConfig)
    generator: GeneratorConfig = field(default_factory=GeneratorConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    presence: PresenceConfig = field(default_factory=PresenceConfig)
    todo_summary: TodoSummaryConfig = field(default_factory=TodoSummaryConfig)
    discord_notifications: DiscordNotificationsConfig = field(default_factory=DiscordNotificationsConfig)



# Registries for nested dataclass fields.
# Needed because `from __future__ import annotations` makes type hints strings.
_NESTED_LIST_FIELDS: dict[tuple[type, str], type] = {}   # list[DataClass]
_NESTED_OBJECT_FIELDS: dict[tuple[type, str], type] = {}  # single DataClass


def _register_nested(cls: type, field_name: str, inner_cls: type) -> None:
    _NESTED_LIST_FIELDS[(cls, field_name)] = inner_cls


def _register_nested_object(cls: type, field_name: str, inner_cls: type) -> None:
    _NESTED_OBJECT_FIELDS[(cls, field_name)] = inner_cls


def _build_section(cls: type, data: dict[str, Any]) -> Any:
    """Build a dataclass instance from a dict, ignoring unknown keys."""
    valid = {f.name for f in cls.__dataclass_fields__.values()}
    filtered = {}
    for k, v in data.items():
        if k not in valid:
            continue
        # Handle nested list-of-dicts (e.g. alerts.rules → list[AlertRule])
        inner_cls = _NESTED_LIST_FIELDS.get((cls, k))
        if inner_cls and isinstance(v, list) and v and isinstance(v[0], dict):
            v = [_build_section(inner_cls, item) for item in v]
        # Handle nested single-object dicts (e.g. parasaur.player_alerts → ParasaurAlertSettings)
        obj_cls = _NESTED_OBJECT_FIELDS.get((cls, k))
        if obj_cls and isinstance(v, dict):
            v = _build_section(obj_cls, v)
        filtered[k] = v
    return cls(**filtered)


# Register nested list fields
_register_nested(AlertsConfig, "rules", AlertRule)
_register_nested(ParasaurConfig, "parasaurs", ParasaurEntry)

# Register nested object fields
_register_nested_object(ParasaurConfig, "player_alerts", ParasaurAlertSettings)
_register_nested_object(ParasaurConfig, "babies_alerts", ParasaurAlertSettings)


def validate_config(cfg: TribeWatchConfig) -> None:
    """Validate a loaded config, raising ValueError on problems."""
    bbox = cfg.tribe_log.bbox
    if len(bbox) != 4 or not all(isinstance(v, int) for v in bbox):
        raise ValueError(f"tribe_log.bbox must be 4 integers, got {bbox!r}")
    left, top, right, bottom = bbox
    if left >= right:
        raise ValueError(f"tribe_log.bbox left ({left}) must be < right ({right})")
    if top >= bottom:
        raise ValueError(f"tribe_log.bbox top ({top}) must be < bottom ({bottom})")

    _valid_engines = ("winrt", "tesseract", "easyocr", "paddleocr")
    if cfg.tribe_log.ocr_engine not in _valid_engines:
        raise ValueError(f"tribe_log.ocr_engine must be one of {_valid_engines}, got {cfg.tribe_log.ocr_engine!r}")

    _valid_engines_optional = ("", *_valid_engines)
    if cfg.tribe.ocr_engine not in _valid_engines_optional:
        raise ValueError(f"tribe.ocr_engine must be one of {_valid_engines_optional}, got {cfg.tribe.ocr_engine!r}")
    if cfg.parasaur.ocr_engine not in _valid_engines_optional:
        raise ValueError(f"parasaur.ocr_engine must be one of {_valid_engines_optional}, got {cfg.parasaur.ocr_engine!r}")

    if cfg.discord.batch_interval < 5:
        raise ValueError(f"discord.batch_interval must be >= 5, got {cfg.discord.batch_interval}")

    if cfg.tribe_log.interval <= 0:
        raise ValueError(f"tribe_log.interval must be > 0, got {cfg.tribe_log.interval}")

    # Server config validation
    if cfg.server.mode not in ("standalone", "client", "server"):
        raise ValueError(f"server.mode must be 'standalone', 'client', or 'server', got {cfg.server.mode!r}")
    if cfg.server.mode == "client" and not cfg.server.server_url:
        raise ValueError("server.server_url is required when server.mode is 'client'")

    # Parasaur bbox validation (only if configured — empty list means disabled)
    pbbox = cfg.parasaur.bbox
    if pbbox:
        if len(pbbox) != 4 or not all(isinstance(v, int) for v in pbbox):
            raise ValueError(f"parasaur.bbox must be 4 integers, got {pbbox!r}")
        left, top, right, bottom = pbbox
        if left >= right:
            raise ValueError(f"parasaur.bbox left ({left}) must be < right ({right})")
        if top >= bottom:
            raise ValueError(f"parasaur.bbox top ({top}) must be < bottom ({bottom})")

    # OAuth config: both must be set, or both empty
    has_id = bool(cfg.web.oauth_client_id)
    has_secret = bool(cfg.web.oauth_client_secret)
    if has_id != has_secret:
        raise ValueError(
            "web.oauth_client_id and web.oauth_client_secret must both be set or both be empty"
        )

    # Tribe bbox validation (only if configured — empty list means disabled)
    tbbox = cfg.tribe.bbox
    if tbbox:
        if len(tbbox) != 4 or not all(isinstance(v, int) for v in tbbox):
            raise ValueError(f"tribe.bbox must be 4 integers, got {tbbox!r}")
        left, top, right, bottom = tbbox
        if left >= right:
            raise ValueError(f"tribe.bbox left ({left}) must be < right ({right})")
        if top >= bottom:
            raise ValueError(f"tribe.bbox top ({top}) must be < bottom ({bottom})")


def _migrate_presence_fields(data: dict[str, Any]) -> None:
    """Migrate old presence fields from [server] to [presence]."""
    server_raw = data.get("server", {})
    presence_raw = data.setdefault("presence", {})
    _PRESENCE_MIGRATION = {
        "presence_offline_webhook": "offline_webhook",
        "presence_online_webhook": "online_webhook",
        "presence_webhook_url": "webhook_url",
    }
    for old_key, new_key in _PRESENCE_MIGRATION.items():
        if old_key in server_raw and new_key not in presence_raw:
            presence_raw[new_key] = server_raw.pop(old_key)


def _build_config_from_data(data: dict[str, Any]) -> TribeWatchConfig:
    """Build and validate TribeWatchConfig from a raw TOML dict."""
    _migrate_presence_fields(data)
    cfg = TribeWatchConfig(
        tribe_log=_build_section(TribeLogConfig, data.get("tribe_log", {})),
        discord=_build_section(DiscordConfig, data.get("discord", {})),
        alerts=_build_section(AlertsConfig, data.get("alerts", {})),
        general=_build_section(GeneralConfig, data.get("general", {})),
        web=_build_section(WebConfig, data.get("web", {})),
        parasaur=_build_section(ParasaurConfig, data.get("parasaur", {})),
        tribe=_build_section(TribeConfig, data.get("tribe", {})),
        generator=_build_section(GeneratorConfig, data.get("generator", {})),
        server=_build_section(ServerConfig, data.get("server", {})),
        presence=_build_section(PresenceConfig, data.get("presence", {})),
        todo_summary=_build_section(TodoSummaryConfig, data.get("todo_summary", {})),
        discord_notifications=_build_section(DiscordNotificationsConfig, data.get("discord_notifications", {})),
    )
    validate_config(cfg)
    return cfg


def load_config(path: str | Path) -> TribeWatchConfig:
    """Load and validate a TOML config file, returning a TribeWatchConfig."""
    path = Path(path)
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return _build_config_from_data(raw)


def client_config_path(server_path: str | Path) -> Path:
    """Derive client config path from server config path.

    tribewatch.toml -> tribewatch_client.toml
    custom.toml -> custom_client.toml

    Idempotent: passing an already-derived client path returns it
    unchanged so callers don't end up with foo_client_client.toml.
    """
    p = Path(server_path)
    if p.stem.endswith("_client"):
        return p
    return p.with_name(p.stem + "_client" + p.suffix)


_CLIENT_OWNED = ("tribe_log", "parasaur", "tribe")


def load_config_pair(server_path: str | Path) -> TribeWatchConfig:
    """Load and merge config from server and client TOML files.

    Used by standalone mode. On first run, migrates client sections
    from the single-file config into a separate client config file.
    """
    import logging

    server_path = Path(server_path)
    cp = client_config_path(server_path)

    with open(server_path, "rb") as f:
        data = tomllib.load(f)

    if not cp.exists():
        # One-time migration: extract client sections to separate file
        log = logging.getLogger(__name__)
        log.info(
            "Migrating client config sections to %s", cp,
        )
        cfg = _build_config_from_data(data)
        save_config(cfg, cp, mode="standalone_client")
        save_config(cfg, server_path, mode="server")
        return cfg

    # Merge: overlay client-owned sections from client file
    with open(cp, "rb") as f:
        client_data = tomllib.load(f)
    for section in _CLIENT_OWNED:
        if section in client_data:
            data.setdefault(section, {}).update(client_data[section])

    # Merge client-owned general fields (e.g. calibration_resolution)
    client_general = client_data.get("general", {})
    if client_general:
        for field in _CLIENT_GENERAL_FIELDS:
            if field in client_general:
                data.setdefault("general", {})[field] = client_general[field]

    # Merge client_token from client file (persisted by standalone OAuth flow)
    client_token = client_data.get("server", {}).get("client_token", "")
    if client_token:
        data.setdefault("server", {})["client_token"] = client_token

    return _build_config_from_data(data)


def _config_to_dict(cfg: TribeWatchConfig) -> dict[str, Any]:
    """Convert config to a nested dict suitable for TOML serialization."""
    return asdict(cfg)


# Which top-level sections and general fields belong to each mode.
_CLIENT_SECTIONS = {"server", "tribe_log", "general", "parasaur", "tribe"}
_CLIENT_GENERAL_FIELDS = {"log_level", "state_file", "monitor", "window_title", "calibration_resolution"}
_CLIENT_PARASAUR_FIELDS = {"bbox", "interval", "clear_delay", "grace_period", "ocr_engine", "parasaurs"}
_CLIENT_SERVER_FIELDS = {"server_url", "client_token"}  # mode is a CLI flag, auth_token is server-side
_SERVER_SECTIONS = {"server", "discord", "alerts", "general", "web", "generator", "presence", "todo_summary", "discord_notifications"}
_SERVER_GENERAL_FIELDS = {"log_level"}


def _filter_for_mode(data: dict[str, Any], mode: str) -> dict[str, Any]:
    """Filter a config dict to only include sections relevant to the mode.

    Modes:
      - ``"standalone"``        — all sections (no filtering)
      - ``"client"``            — remote client deployment (includes server connection info)
      - ``"server"``            — server deployment
      - ``"standalone_client"`` — client-owned data only (standalone split)
    """
    if mode == "client":
        sections = _CLIENT_SECTIONS
        general_fields = _CLIENT_GENERAL_FIELDS
    elif mode == "server":
        sections = _SERVER_SECTIONS
        general_fields = _SERVER_GENERAL_FIELDS
    elif mode == "standalone_client":
        # Standalone split: calibration data + client_token only
        sections = set(_CLIENT_OWNED)
        filtered = {k: v for k, v in data.items() if k in sections}
        if "parasaur" in filtered and isinstance(filtered["parasaur"], dict):
            filtered["parasaur"] = {
                k: v for k, v in filtered["parasaur"].items() if k in _CLIENT_PARASAUR_FIELDS
            }
        # Persist calibration_resolution in general if set
        cal_res = data.get("general", {}).get("calibration_resolution")
        if cal_res:
            filtered.setdefault("general", {})["calibration_resolution"] = cal_res
        # Only persist client_token (no mode/auth_token/server_url — standalone is localhost)
        server_data = data.get("server", {})
        if server_data.get("client_token"):
            filtered["server"] = {"client_token": server_data["client_token"]}
        return filtered
    else:
        return data

    filtered = {k: v for k, v in data.items() if k in sections}
    if "general" in filtered and isinstance(filtered["general"], dict):
        filtered["general"] = {
            k: v for k, v in filtered["general"].items() if k in general_fields
        }
    if mode in ("client", "standalone_client") and "parasaur" in filtered and isinstance(filtered["parasaur"], dict):
        filtered["parasaur"] = {
            k: v for k, v in filtered["parasaur"].items() if k in _CLIENT_PARASAUR_FIELDS
        }
    # Client config: only server_url + client_token (mode is a CLI flag, auth_token is server-side)
    if mode == "client" and "server" in filtered and isinstance(filtered["server"], dict):
        filtered["server"] = {
            k: v for k, v in filtered["server"].items() if k in _CLIENT_SERVER_FIELDS
        }
    return filtered


def save_config(
    cfg: TribeWatchConfig, path: str | Path, *, mode: str = "standalone",
) -> None:
    """Write a TribeWatchConfig back to a TOML file.

    *mode* controls which sections are written:
      - ``"standalone"`` — all sections (default)
      - ``"client"``     — only client-relevant sections
      - ``"server"``     — only server-relevant sections
    """
    path = Path(path)
    data = _filter_for_mode(_config_to_dict(cfg), mode)
    with open(path, "wb") as f:
        tomli_w.dump(data, f)


def generate_default_config(path: str | Path) -> TribeWatchConfig:
    """Write a default config TOML file and return the config."""
    path = Path(path)
    cfg = TribeWatchConfig()
    save_config(cfg, path)
    return cfg
