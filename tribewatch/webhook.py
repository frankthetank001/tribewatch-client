"""Discord webhook dispatcher with batching, rate limiting, and retry."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import time as _time

import aiohttp

if TYPE_CHECKING:
    from tribewatch.parser import Severity, TribeLogEvent

from tribewatch.parser import EVENT_TYPE_LABELS

log = logging.getLogger(__name__)


_EVERYONE_TOKENS = frozenset({"@everyone", "@here"})


def _is_discord_id(s: str) -> bool:
    """Return True if *s* looks like a Discord snowflake ID (all digits)."""
    return bool(s) and s.isdigit()


def resolve_owner(id_str: str, owner_discord_id: str = "") -> str:
    """Expand ``!owner`` to the actual owner Discord ID.

    Returns the string unchanged if it isn't ``!owner``.
    Returns empty string for ``!owner`` when no owner_discord_id is configured.
    """
    if id_str == "!owner":
        if owner_discord_id:
            return f"!{owner_discord_id}"
        return ""
    return id_str


def resolve_mention(
    name: str,
    mentions: dict[str, str],
    owner_discord_id: str = "",
) -> str:
    """Resolve a mention name to a Discord ID string.

    If *name* is a key in the *mentions* map, return the mapped value
    (with ``!owner`` expansion applied).  Otherwise fall back to
    :func:`resolve_owner` so raw IDs and ``!owner`` still work.

    Returns empty string if the name cannot be resolved to a valid
    Discord ID (prevents garbage like ``<@&role:lost colony>`` in messages).
    """
    # @everyone / @here are literal Discord tokens — pass through directly
    if name in _EVERYONE_TOKENS:
        return name
    if name in mentions:
        mapped = mentions[name]
        if mapped in _EVERYONE_TOKENS:
            return mapped
        return resolve_owner(mapped, owner_discord_id)
    resolved = resolve_owner(name, owner_discord_id)
    # If it's a raw Discord ID or a user-prefixed ID, pass through
    if _is_discord_id(resolved) or (resolved.startswith("!") and _is_discord_id(resolved[1:])):
        return resolved
    # Unresolvable name — return empty so the mention is skipped
    if name:
        import logging
        logging.getLogger(__name__).warning(
            "Could not resolve mention %r — not in mentions map and not a numeric ID; "
            "ping will be skipped",
            name,
        )
    return ""


def _format_mention(id_str: str) -> str:
    """Format a Discord mention for a role or user ID.

    Prefix the ID with ``!`` to ping a user instead of a role.
    E.g. ``!197967989964800000`` → ``<@197967989964800000>`` (user),
         ``197967989964800000``  → ``<@&197967989964800000>`` (role).

    Returns empty string if *id_str* is empty or doesn't look like a valid ID.
    """
    if not id_str:
        return ""
    # @everyone / @here are plain-text tokens, not angle-bracket mentions
    if id_str in _EVERYONE_TOKENS:
        return id_str
    if id_str.startswith("!"):
        digits = id_str[1:]
        if not _is_discord_id(digits):
            return ""
        return f"<@{digits}>"
    if not _is_discord_id(id_str):
        return ""
    return f"<@&{id_str}>"


def _friendly_duration(seconds: float) -> str:
    """Format seconds as a friendly human-readable duration.

    Shows the two most significant units: "2d 5h", "3h 12m", "4m 30s", "15s".
    """
    secs = int(seconds)
    if secs < 0:
        secs = 0
    d = secs // 86400
    h = (secs % 86400) // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    if d > 0:
        return f"{d}d {h}h"
    if h > 0:
        return f"{h}h {m}m"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


# Discord embed colour codes
_COLORS = {
    "critical": 0xFF0000,  # red
    "warning": 0xFF8C00,   # orange
    "info": 0x00CC00,      # green
}

_EMOJI = {
    "critical": "\U0001f534",  # red circle
    "warning": "\U0001f7e0",   # orange circle
    "info": "\U0001f7e2",      # green circle
}

MAX_EMBEDS_PER_MESSAGE = 10


def build_embed(event: TribeLogEvent, condition_label: str = "") -> dict[str, Any]:
    """Build a Discord embed dict for a single event."""
    sev = event.severity.value
    ev_val = event.event_type.value
    label = EVENT_TYPE_LABELS.get(ev_val, ev_val.replace("_", " ").title())
    if condition_label:
        label += f" [{condition_label}]"
    title = f"{_EMOJI.get(sev, '')} {label}"
    return {
        "title": title,
        "description": event.raw_text,
        "color": _COLORS.get(sev, 0x808080),
        "footer": {"text": f"Day {event.day}, {event.time}"},
        "timestamp": event.timestamp.isoformat(),
    }


def build_embed_from_dict(event: dict[str, Any], condition_label: str = "") -> dict[str, Any]:
    """Build a Discord embed dict from a raw event dict (server-side)."""
    sev = event.get("severity", "info")
    ev_val = event.get("event_type", "unknown")
    label = EVENT_TYPE_LABELS.get(ev_val, ev_val.replace("_", " ").title())
    if condition_label:
        label += f" [{condition_label}]"
    title = f"{_EMOJI.get(sev, '')} {label}"
    embed: dict[str, Any] = {
        "title": title,
        "description": event.get("raw_text", ""),
        "color": _COLORS.get(sev, 0x808080),
        "footer": {"text": f"Day {event.get('day', '?')}, {event.get('time', '?')}"},
    }
    ts = event.get("timestamp")
    if ts is not None:
        from datetime import datetime, timezone
        embed["timestamp"] = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return embed


class WebhookDispatcher:
    """Sends events to Discord webhooks with batching and rate-limit handling."""

    def __init__(
        self,
        alert_webhook: str = "",
        raid_webhook: str = "",
        debug_webhook: str = "",
        tasks_webhook: str = "",
        ping_role_id: str = "",
        batch_interval: int = 15,
        owner_discord_id: str = "",
        mentions: dict[str, str] | None = None,
    ) -> None:
        self.alert_webhook = alert_webhook
        self.raid_webhook = raid_webhook
        self.debug_webhook = debug_webhook
        self.tasks_webhook = tasks_webhook
        self.ping_role_id = resolve_mention(ping_role_id, mentions or {}, owner_discord_id)
        self.batch_interval = batch_interval
        self._owner_discord_id = owner_discord_id
        self.mentions: dict[str, str] = mentions or {}

        self._batch: list[dict[str, Any]] = []
        self._retry_queue: list[tuple[str, dict[str, Any]]] = []
        self._session: aiohttp.ClientSession | None = None
        self._rate_limit_reset: float = 0  # monotonic time when rate limit resets
        self._rate_limit_remaining: int = 30

    @property
    def _tasks_hook(self) -> str:
        """Resolve tasks webhook — falls back to alert_webhook if not set."""
        return self.tasks_webhook or self.alert_webhook

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _post_webhook(self, url: str, payload: dict[str, Any]) -> tuple[bool, str]:
        """POST to a webhook URL. Returns (success, detail)."""
        if not url:
            return True, ""
        session = await self._get_session()
        try:
            async with session.post(url, json=payload) as resp:
                # Track rate limits
                remaining = resp.headers.get("X-RateLimit-Remaining")
                reset_after = resp.headers.get("X-RateLimit-Reset-After")
                if remaining is not None:
                    self._rate_limit_remaining = int(remaining)
                if reset_after is not None:
                    loop = asyncio.get_running_loop()
                    self._rate_limit_reset = loop.time() + float(reset_after)

                if resp.status == 429:
                    retry_after = (await resp.json()).get("retry_after", 5)
                    log.warning("Rate limited, retry after %.1fs", retry_after)
                    await asyncio.sleep(retry_after)
                    self._retry_queue.append((url, payload))
                    return False, f"Rate limited (retry after {retry_after}s)"

                if resp.status >= 400:
                    body = await resp.text()
                    log.error("Webhook POST failed (%d): %s", resp.status, body[:200])
                    return False, f"HTTP {resp.status}: {body[:100]}"

                return True, ""
        except Exception as exc:
            log.exception("Webhook POST error")
            self._retry_queue.append((url, payload))
            return False, f"Connection error: {exc}"

    async def send_critical(
        self,
        event: TribeLogEvent,
        ping: bool = True,
        ping_target: str = "",
        extra_mentions: list[str] | None = None,
        condition_label: str = "",
    ) -> dict[str, str]:
        """Send a critical event immediately to alert + raid webhooks.

        extra_mentions is an optional list of Discord user IDs to @mention
        in addition to the role ping (e.g. the specific tribe member involved).

        Returns a dict with ping_status and ping_detail describing what happened.
        ping_status values:
          "pinged"     — sent to Discord with @role mention
          "sent"       — sent to Discord (no role mention)
          "failed"     — webhook POST failed
          "no_webhook" — no webhook URLs configured
        """
        embed = build_embed(event, condition_label=condition_label)

        if not self.alert_webhook and not self.raid_webhook:
            return {"ping_status": "no_webhook", "ping_detail": "No webhook URLs configured"}

        # Per-rule ping_target overrides global ping_role_id
        role_id = ping_target or self.ping_role_id
        include_mention = ping and bool(role_id)

        # Build content string: role mention + any per-member user mentions
        mentions: list[str] = []
        if include_mention:
            mentions.append(_format_mention(role_id))
        if extra_mentions:
            for uid in extra_mentions:
                mentions.append(f"<@{uid}>")

        payload: dict[str, Any] = {"embeds": [embed]}
        if mentions:
            payload["content"] = " ".join(mentions)

        # Track success across webhooks
        all_ok = True
        fail_detail = ""

        if self.alert_webhook:
            ok, detail = await self._post_webhook(self.alert_webhook, payload)
            if not ok:
                all_ok = False
                fail_detail = f"Alert webhook: {detail}"

        if self.raid_webhook:
            ok, detail = await self._post_webhook(self.raid_webhook, payload)
            if not ok:
                all_ok = False
                fail_detail = f"Raid webhook: {detail}" if not fail_detail else f"{fail_detail}; Raid webhook: {detail}"

        if not all_ok:
            return {"ping_status": "failed", "ping_detail": fail_detail}

        if mentions:
            mention_desc = ", ".join(mentions)
            return {"ping_status": "pinged", "ping_detail": f"Sent with mentions: {mention_desc}"}

        # Sent without mention — explain why if ping was requested
        detail = "Sent to Discord"
        if ping and not role_id:
            detail += " (no role ID configured)"
        return {"ping_status": "sent", "ping_detail": detail}

    async def send_escalation(
        self,
        event_type: str,
        count: int,
        window_minutes: int,
        ping_target: str = "",
        event_texts: list[str] | None = None,
        condition_label: str = "",
    ) -> dict[str, str]:
        """Send an escalation alert summarising repeated events.

        Returns the same {ping_status, ping_detail} dict as send_critical.
        """
        if not self.alert_webhook and not self.raid_webhook:
            return {"ping_status": "no_webhook", "ping_detail": "No webhook URLs configured"}

        label = event_type.replace("_", " ").title()
        if condition_label:
            label += f" [{condition_label}]"
        description = f"**{count} {label}** events in {window_minutes} min!"
        if event_texts:
            # Truncate to fit Discord's 4096-char embed description limit
            block = "\n".join(event_texts)
            max_block = 4096 - len(description) - 20  # room for markdown
            if len(block) > max_block:
                block = block[:max_block] + "\n..."
            description += f"\n```\n{block}\n```"

        embed: dict[str, Any] = {
            "title": "\U0001f6a8 Escalation Alert",
            "description": description,
            "color": 0xFF0000,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        role_id = ping_target or self.ping_role_id
        payload: dict[str, Any] = {"embeds": [embed]}
        if role_id:
            payload["content"] = _format_mention(role_id)

        all_ok = True
        fail_detail = ""

        if self.alert_webhook:
            ok, detail = await self._post_webhook(self.alert_webhook, payload)
            if not ok:
                all_ok = False
                fail_detail = f"Alert webhook: {detail}"

        if self.raid_webhook:
            ok, detail = await self._post_webhook(self.raid_webhook, payload)
            if not ok:
                all_ok = False
                fail_detail = f"Raid webhook: {detail}" if not fail_detail else f"{fail_detail}; Raid webhook: {detail}"

        if not all_ok:
            return {"ping_status": "failed", "ping_detail": fail_detail}

        if role_id:
            return {"ping_status": "pinged", "ping_detail": f"Escalation sent with @{role_id} mention"}

        return {"ping_status": "sent", "ping_detail": "Escalation sent (no role ID configured)"}

    async def send_critical_dict(
        self,
        event: dict[str, Any],
        ping: bool = True,
        ping_target: str = "",
        extra_mentions: list[str] | None = None,
        condition_label: str = "",
    ) -> dict[str, str]:
        """Send a critical event dict immediately (server-side dispatch)."""
        embed = build_embed_from_dict(event, condition_label=condition_label)

        if not self.alert_webhook and not self.raid_webhook:
            return {"ping_status": "no_webhook", "ping_detail": "No webhook URLs configured"}

        role_id = ping_target or self.ping_role_id
        include_mention = ping and bool(role_id)

        mentions: list[str] = []
        if include_mention:
            mentions.append(_format_mention(role_id))
        if extra_mentions:
            for uid in extra_mentions:
                mentions.append(f"<@{uid}>")

        payload: dict[str, Any] = {"embeds": [embed]}
        if mentions:
            payload["content"] = " ".join(mentions)

        all_ok = True
        fail_detail = ""

        if self.alert_webhook:
            ok, detail = await self._post_webhook(self.alert_webhook, payload)
            if not ok:
                all_ok = False
                fail_detail = f"Alert webhook: {detail}"

        if self.raid_webhook:
            ok, detail = await self._post_webhook(self.raid_webhook, payload)
            if not ok:
                all_ok = False
                fail_detail = f"Raid webhook: {detail}" if not fail_detail else f"{fail_detail}; Raid webhook: {detail}"

        if not all_ok:
            return {"ping_status": "failed", "ping_detail": fail_detail}
        if mentions:
            return {"ping_status": "pinged", "ping_detail": f"Sent with mentions: {', '.join(mentions)}"}
        detail = "Sent to Discord"
        if ping and not role_id:
            detail += " (no role ID configured)"
        return {"ping_status": "sent", "ping_detail": detail}

    def queue_batch_dict(self, event: dict[str, Any], condition_label: str = "") -> None:
        """Add a non-critical event dict to the batch queue (server-side)."""
        self._batch.append(build_embed_from_dict(event, condition_label=condition_label))

    def queue_batch(self, event: TribeLogEvent, condition_label: str = "") -> None:
        """Add a non-critical event to the batch queue."""
        self._batch.append(build_embed(event, condition_label=condition_label))

    async def flush_batch(self) -> None:
        """Send all queued batch embeds."""
        if not self._batch:
            return
        if not self.alert_webhook:
            self._batch.clear()
            return

        # Discord allows max 10 embeds per message
        while self._batch:
            chunk = self._batch[:MAX_EMBEDS_PER_MESSAGE]
            self._batch = self._batch[MAX_EMBEDS_PER_MESSAGE:]
            await self._post_webhook(self.alert_webhook, {"embeds": chunk})

    async def flush_retries(self) -> None:
        """Retry any previously failed sends."""
        retries = self._retry_queue[:]
        self._retry_queue.clear()
        for url, payload in retries:
            await self._post_webhook(url, payload)

    async def send_debug(self, text: str) -> None:
        """Send raw OCR text to the debug webhook."""
        if not self.debug_webhook:
            return
        # Truncate to Discord's 2000 char limit
        if len(text) > 1990:
            text = text[:1990] + "..."
        await self._post_webhook(self.debug_webhook, {"content": f"```\n{text}\n```"})

    async def send_generator_alert(
        self,
        name: str,
        pct_remaining: int,
        seconds_remaining: float,
        ping_target: str = "",
        base_url: str = "",
        base_name: str = "",
    ) -> None:
        """Send a generator fuel alert to Discord."""
        if not self.alert_webhook:
            return

        if pct_remaining <= 2:
            color = 0xFF0000  # red
            emoji = "\U0001f534"
        else:
            color = 0xFF8C00  # orange
            emoji = "\U0001f7e0"

        if pct_remaining == 0:
            title = f"{emoji} Generator EXPIRED"
            description = "Fuel has run out! All powered structures are offline."
            time_value = "Empty"
        else:
            title = f"{emoji} Generator Low Fuel"
            if pct_remaining <= 2:
                description = "Fuel is critically low — refuel immediately!"
            else:
                description = "Fuel is running low — refuel soon to avoid power loss."
            time_value = _friendly_duration(seconds_remaining)

        fields: list[dict[str, Any]] = [
            {"name": "Generator", "value": name, "inline": True},
            {"name": "Fuel", "value": f"{pct_remaining}%", "inline": True},
            {"name": "Time Remaining", "value": time_value, "inline": True},
        ]
        if base_name:
            fields.append({"name": "Base", "value": base_name, "inline": True})

        embed: dict[str, Any] = {
            "title": title,
            "description": description,
            "color": color,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if base_url:
            embed["url"] = f"{base_url.rstrip('/')}/#/generators"

        role_id = ping_target or self.ping_role_id
        payload: dict[str, Any] = {"embeds": [embed]}
        if role_id:
            payload["content"] = _format_mention(role_id)

        await self._post_webhook(self.alert_webhook, payload)

    async def send_generator_refueled(
        self,
        name: str,
        fuel_seconds: float,
        base_url: str = "",
    ) -> None:
        """Send a generator refueled notification to Discord."""
        if not self.alert_webhook:
            return

        time_value = _friendly_duration(fuel_seconds)

        fields: list[dict[str, Any]] = [
            {"name": "Generator", "value": name, "inline": True},
            {"name": "Fuel", "value": "100%", "inline": True},
            {"name": "Time Remaining", "value": time_value, "inline": True},
        ]

        embed: dict[str, Any] = {
            "title": "\u2705 Generator Refueled",
            "description": "Generator has been topped up and is running.",
            "color": 0x00CC00,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if base_url:
            embed["url"] = f"{base_url.rstrip('/')}/#/generators"

        await self._post_webhook(self.alert_webhook, {"embeds": [embed]})

    async def send_todo_reminder(
        self,
        title: str,
        reminder_type: str,
        due_at: float,
        assignees: list[str] | None = None,
        tribe_name: str = "",
        base_url: str = "",
        item_id: int | None = None,
    ) -> None:
        """Send a todo reminder to Discord.

        *assignees* is a list of Discord user IDs to @mention.
        """
        if not self._tasks_hook:
            return

        if reminder_type == "overdue":
            color = 0xFF0000  # red
            emoji = "\U0001f534"
            header = "OVERDUE"
            description = "This task is past its deadline!"
        else:
            color = 0xFF8C00  # orange
            emoji = "\U0001f7e0"
            header = "Due Soon"
            description = "A task is due soon — take action before the deadline."

        due_unix = int(due_at)
        fields: list[dict[str, Any]] = [
            {"name": "Due", "value": f"<t:{due_unix}:F>", "inline": True},
        ]
        if assignees:
            mention_strs = [f"<@{uid}>" for uid in assignees]
            fields.append({"name": "Assigned", "value": ", ".join(mention_strs), "inline": True})
        if tribe_name:
            fields.append({"name": "Tribe", "value": tribe_name, "inline": False})

        if base_url and item_id:
            action_base = base_url.rstrip("/")
            fields.append({"name": "Actions", "value": f"[Done]({action_base}/api/v1/todos/{item_id}/done)", "inline": False})

        embed: dict[str, Any] = {
            "title": f"{emoji} Todo {header}: {title}",
            "description": description,
            "color": color,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if base_url:
            embed["url"] = f"{base_url.rstrip('/')}/#/todo"

        # @mention assignees + role in content for push notifications
        mentions_parts: list[str] = []
        if self.ping_role_id:
            mentions_parts.append(_format_mention(self.ping_role_id))
        if assignees:
            for uid in assignees:
                mentions_parts.append(f"<@{uid}>")

        payload: dict[str, Any] = {"embeds": [embed]}
        if mentions_parts:
            payload["content"] = " ".join(mentions_parts)

        await self._post_webhook(self._tasks_hook, payload)

    async def send_todo_completed(
        self,
        title: str,
        assignees: list[str] | None = None,
        tribe_name: str = "",
        base_url: str = "",
        quantity: int = 1,
        quantity_desc: str = "",
    ) -> None:
        """Send a todo completion notification to Discord."""
        if not self._tasks_hook:
            return

        fields: list[dict[str, Any]] = []
        if assignees:
            fields.append({"name": "Completed by", "value": ", ".join(assignees), "inline": True})
        if quantity > 1:
            qty_str = f"{quantity}/{quantity}"
            if quantity_desc:
                qty_str += f" {quantity_desc}"
            fields.append({"name": "Quantity", "value": qty_str, "inline": True})
        if tribe_name:
            fields.append({"name": "Tribe", "value": tribe_name, "inline": False})

        embed: dict[str, Any] = {
            "title": f"\u2705 Task Completed: {title}",
            "color": 0x00CC00,  # green
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if base_url:
            embed["url"] = f"{base_url.rstrip('/')}/#/todo"

        await self._post_webhook(self._tasks_hook, {"embeds": [embed]})

    async def send_todo_assignment(
        self,
        title: str,
        assignee: str,
        assigned_by: str = "",
        tribe_name: str = "",
        list_name: str = "",
        discord_ids: list[str] | None = None,
        base_url: str = "",
        quantity: int = 1,
        quantity_desc: str = "",
        description: str = "",
        quantity_have: int = 0,
        item_id: int | None = None,
    ) -> None:
        """Send a todo assignment notification to Discord."""
        if not self._tasks_hook:
            return

        color = 0x58A6FF  # blue — matches --accent

        # Build description: task description + quantity info
        desc_parts: list[str] = []
        if description:
            desc_parts.append(description)
        if quantity > 1:
            qty_str = f"**Progress:** {quantity_have}/{quantity}"
            if quantity_desc:
                qty_str += f" {quantity_desc}"
            desc_parts.append(qty_str)
        embed_description = "\n".join(desc_parts) if desc_parts else "You have been assigned a new task."

        fields: list[dict[str, Any]] = []
        if list_name:
            fields.append({"name": "List", "value": list_name, "inline": True})
        fields.append({"name": "Assigned to", "value": assignee, "inline": True})
        if assigned_by:
            fields.append({"name": "Assigned by", "value": assigned_by, "inline": True})
        if tribe_name:
            fields.append({"name": "Tribe", "value": tribe_name, "inline": False})

        # Add action links (Done)
        if base_url and item_id:
            action_base = base_url.rstrip("/")
            links = [f"[Done]({action_base}/api/v1/todos/{item_id}/done)"]
            fields.append({"name": "Actions", "value": " \u2014 ".join(links), "inline": False})

        todo_url = f"{base_url.rstrip('/')}/#/todo" if base_url else ""

        embed: dict[str, Any] = {
            "title": title,
            "description": embed_description,
            "color": color,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if todo_url:
            embed["url"] = todo_url

        # @mention the assignee's Discord ID(s) in content
        payload: dict[str, Any] = {"embeds": [embed]}
        if discord_ids:
            payload["content"] = " ".join(f"<@{did}>" for did in discord_ids)

        await self._post_webhook(self._tasks_hook, payload)

    async def send_todo_list_assignment(
        self,
        list_name: str,
        assignments: dict[str, list[dict[str, Any]]],
        tribe_name: str = "",
        discord_id_map: dict[str, str] | None = None,
        total_tasks: int = 0,
        base_url: str = "",
    ) -> None:
        """Send a consolidated list-assignment notification to Discord.

        Args:
            list_name: Name of the list/goal.
            assignments: Mapping of assignee name → list of task detail dicts
                         (keys: title, quantity, quantity_desc, category, priority).
            tribe_name: Tribe for context.
            discord_id_map: Mapping of assignee name → Discord user ID for inline @mentions.
            total_tasks: Total number of tasks in the list (including unassigned).
            base_url: Base URL for linking to the todo page.
        """
        if not self._tasks_hook:
            return

        color = 0x58A6FF  # blue — matches --accent
        unassigned_tasks = assignments.pop("", [])
        assigned_count = sum(len(ts) for ts in assignments.values())
        member_count = len(assignments)

        def _format_task_lines(task_list: list[dict[str, Any]]) -> str:
            """Format assigned tasks — plain markdown with Done links when possible."""
            has_links = base_url and any(t.get("item_id") for t in task_list)
            if not has_links:
                # Code block fallback (no clickable links)
                lines: list[str] = []
                for t in task_list:
                    done = t.get("done", False)
                    check = "\u2611" if done else "\u2610"
                    line = f"{check} {t['title']}"
                    qty = t.get("quantity", 1)
                    qty_desc = t.get("quantity_desc", "")
                    if qty > 1:
                        qty_have = t.get("quantity_have", 0)
                        line += f"  {qty_have}/{qty}"
                        if qty_desc:
                            line += f" {qty_desc}"
                    cat = t.get("category", "")
                    if cat:
                        line += f"  [{cat}]"
                    lines.append(line)
                return "```\n" + "\n".join(lines) + "\n```"

            action_base = base_url.rstrip("/")
            lines = []
            for t in task_list:
                done = t.get("done", False)
                check = "\u2611" if done else "\u2610"
                line = f"{check} **{t['title']}**"
                qty = t.get("quantity", 1)
                qty_desc = t.get("quantity_desc", "")
                if qty > 1:
                    qty_have = t.get("quantity_have", 0)
                    line += f" {qty_have}/{qty}"
                    if qty_desc:
                        line += f" {qty_desc}"
                cat = t.get("category", "")
                if cat:
                    line += f" [{cat}]"
                iid = t.get("item_id")
                if iid and not done:
                    line += f" \u2014 [Done]({action_base}/api/v1/todos/{iid}/done)"
                lines.append(line)
            return "\n".join(lines)

        def _format_unassigned_lines(task_list: list[dict[str, Any]]) -> str:
            """Format unassigned tasks as plain markdown with claim + done links.

            Uses plain markdown (not code blocks) so Discord renders the
            links as clickable.  Falls back to code-block style when no
            base_url or item_id is available.
            """
            if not base_url or not any(t.get("item_id") for t in task_list):
                return _format_task_lines(task_list)

            lines: list[str] = []
            action_base = base_url.rstrip("/")
            for t in task_list:
                done = t.get("done", False)
                check = "\u2611" if done else "\u2610"
                line = f"{check} **{t['title']}**"
                qty = t.get("quantity", 1)
                qty_desc = t.get("quantity_desc", "")
                if qty > 1:
                    qty_have = t.get("quantity_have", 0)
                    line += f" {qty_have}/{qty}"
                    if qty_desc:
                        line += f" {qty_desc}"
                cat = t.get("category", "")
                if cat:
                    line += f" [{cat}]"
                iid = t.get("item_id")
                if iid and not done:
                    line += f" \u2014 [Claim]({action_base}/api/v1/todos/{iid}/claim) | [Done]({action_base}/api/v1/todos/{iid}/done)"
                lines.append(line)
            return "\n".join(lines)

        # Build per-assignee fields
        id_map = discord_id_map or {}
        fields: list[dict[str, Any]] = []
        for assignee, task_list in assignments.items():
            count = len(task_list)
            did = id_map.get(assignee, "")
            # Only @mention if they have incomplete tasks
            all_done = all(t.get("done", False) for t in task_list)
            mention_line = f"<@{did}>\n" if did and not all_done else ""
            fields.append({
                "name": f"👤 {assignee} — {count} task{'s' if count != 1 else ''}",
                "value": mention_line + _format_task_lines(task_list),
                "inline": False,
            })

        # Unassigned section
        if unassigned_tasks:
            fields.append({
                "name": f"📭 Unassigned — {len(unassigned_tasks)} task{'s' if len(unassigned_tasks) != 1 else ''}",
                "value": _format_unassigned_lines(unassigned_tasks),
                "inline": False,
            })

        desc_parts: list[str] = []
        if tribe_name:
            desc_parts.append(f"🏕️ **{tribe_name}**")

        todo_url = f"{base_url.rstrip('/')}/#/todo" if base_url else ""

        embed: dict[str, Any] = {
            "title": f"📋 New List: {list_name}",
            "description": "\n".join(desc_parts),
            "color": color,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if todo_url:
            embed["url"] = todo_url

        payload: dict[str, Any] = {"embeds": [embed]}

        await self._post_webhook(self._tasks_hook, payload)

    async def send_todo_list_reset(
        self,
        list_name: str,
        item_count: int,
        tribe_name: str = "",
        base_url: str = "",
        is_auto: bool = False,
        prev_summary: dict[str, Any] | None = None,
        next_reset_at: float | None = None,
    ) -> None:
        """Send a todo list reset notification to Discord."""
        if not self._tasks_hook:
            return

        color = 0x5865F2  # blurple
        emoji = "\U0001f504"  # 🔄

        if is_auto:
            description = "This recurring list has been automatically reset."
        else:
            description = "This list has been manually reset."

        fields: list[dict[str, Any]] = []

        # Previous cycle summary
        if prev_summary and prev_summary.get("total", 0) > 0:
            total = prev_summary["total"]
            done = prev_summary["completed"]
            incomplete = prev_summary.get("incomplete", [])
            if done == total:
                summary_val = f"\u2705 {done}/{total} tasks completed"
            else:
                summary_val = f"\u2705 {done}/{total} tasks completed"
                if incomplete:
                    summary_val += f"\n\u274c {len(incomplete)} incomplete:"
            fields.append({"name": "Previous Cycle", "value": summary_val, "inline": False})

            if incomplete:
                lines = []
                for item in incomplete[:8]:
                    line = f"\u2022 {item['title']}"
                    qty = item.get("quantity", 0) or 0
                    if qty > 0:
                        have = item.get("quantity_have", 0) or 0
                        desc = item.get("quantity_desc") or ""
                        line += f" ({have}/{qty}{(' ' + desc) if desc else ''})"
                    lines.append(line)
                if len(incomplete) > 8:
                    lines.append(f"*...and {len(incomplete) - 8} more*")
                fields.append({
                    "name": "Incomplete Tasks",
                    "value": "\n".join(lines),
                    "inline": False,
                })

        fields.append({"name": "Tasks", "value": str(item_count), "inline": True})
        if next_reset_at is not None:
            fields.append({
                "name": "Next Reset",
                "value": f"<t:{int(next_reset_at)}:R>",
                "inline": True,
            })
        if tribe_name:
            fields.append({"name": "Tribe", "value": tribe_name, "inline": True})

        embed: dict[str, Any] = {
            "title": f"{emoji} List Reset: {list_name}",
            "description": description,
            "color": color,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if base_url:
            embed["url"] = f"{base_url.rstrip('/')}/#/todo"

        await self._post_webhook(self._tasks_hook, {"embeds": [embed]})

    async def send_calendar_reminder(
        self,
        title: str,
        minutes_before: int,
        start_time: float,
        event_type: str = "general",
        rsvp_going: list[str] | None = None,
        tribe_name: str = "",
        extra_mentions: list[str] | None = None,
        base_url: str = "",
        linked_tasks: list[dict[str, Any]] | None = None,
        linked_goals: list[dict[str, Any]] | None = None,
        target_server: str = "",
    ) -> None:
        """Send a calendar event reminder to Discord.

        *extra_mentions* is an optional list of Discord user IDs to @mention
        (e.g. RSVP "going" members with a discord_id set).
        *linked_tasks* is an optional list of incomplete task dicts from linked
        goals (keys: id, title, status, priority).
        *linked_goals* is an optional list of goal info dicts
        (keys: name, incomplete_count) for showing list names in the embed.
        """
        if not self._tasks_hook:
            return

        color = 0x5865F2  # blurple
        emoji = "\U0001f4c5"

        if minutes_before >= 60:
            time_str = f"{minutes_before // 60}h"
        else:
            time_str = f"{minutes_before}min"

        start_unix = int(start_time)
        type_label = event_type.replace("_", " ").title()

        fields: list[dict[str, Any]] = [
            {"name": "Starts", "value": f"<t:{start_unix}:F>", "inline": True},
            {"name": "Type", "value": type_label, "inline": True},
        ]
        if target_server:
            fields.append({"name": "Target Server", "value": target_server, "inline": True})
        if rsvp_going:
            fields.append({"name": f"Attending ({len(rsvp_going)})", "value": ", ".join(rsvp_going), "inline": False})
        if tribe_name:
            fields.append({"name": "Tribe", "value": tribe_name, "inline": False})

        # Linked task lists and prep tasks
        incomplete_count = len(linked_tasks) if linked_tasks else 0

        # Show linked goal names with their incomplete counts
        if linked_goals:
            incomplete_goals = [g for g in linked_goals if g.get("incomplete_count", 0) > 0]
            done_goals = [g for g in linked_goals if g.get("incomplete_count", 0) == 0]
            if incomplete_goals:
                lines = [f"\u274c **{g['name']}** — {g['incomplete_count']} remaining" for g in incomplete_goals]
                if done_goals:
                    lines.extend(f"\u2705 ~~{g['name']}~~ — done" for g in done_goals)
                fields.append({
                    "name": f"Linked Lists ({len(linked_goals)})",
                    "value": "\n".join(lines),
                    "inline": False,
                })
            elif done_goals:
                lines = [f"\u2705 ~~{g['name']}~~ — done" for g in done_goals]
                fields.append({
                    "name": f"Linked Lists ({len(linked_goals)})",
                    "value": "\n".join(lines),
                    "inline": False,
                })

        if linked_tasks:
            max_shown = 8
            lines = []
            for t in linked_tasks[:max_shown]:
                pri = t.get("priority", "medium")
                lines.append(f"- {t['title']} [{pri}]")
            if len(linked_tasks) > max_shown:
                lines.append(f"+{len(linked_tasks) - max_shown} more")
            fields.append({
                "name": f"Prep Tasks ({incomplete_count} incomplete)",
                "value": "\n".join(lines),
                "inline": False,
            })

        # Adjust description when there are incomplete prep tasks
        if incomplete_count > 0:
            description = f"Upcoming tribe event \u2014 **{incomplete_count} prep task{'s' if incomplete_count != 1 else ''} still incomplete!**"
        else:
            description = "Upcoming tribe event \u2014 get ready!"

        embed: dict[str, Any] = {
            "title": f"{emoji} Starting in {time_str}: {title}",
            "description": description,
            "color": color,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if base_url:
            embed["url"] = f"{base_url.rstrip('/')}/#/calendar"

        mentions_parts: list[str] = []
        if self.ping_role_id:
            mentions_parts.append(_format_mention(self.ping_role_id))
        if extra_mentions:
            for uid in extra_mentions:
                mentions_parts.append(f"<@{uid}>")

        payload: dict[str, Any] = {"embeds": [embed]}
        if mentions_parts:
            payload["content"] = " ".join(mentions_parts)

        await self._post_webhook(self._tasks_hook, payload)

    async def send_calendar_event_notification(
        self,
        title: str,
        start_time: float,
        event_type: str = "general",
        tribe_name: str = "",
        description: str = "",
        created_by: str = "",
        base_url: str = "",
        *,
        is_update: bool = False,
        max_participants: int | None = None,
        target_server: str = "",
    ) -> None:
        """Send a Discord embed when a calendar event is created or updated.

        *is_update* switches the title/color from "New Event" to "Event Updated".
        """
        if not self._tasks_hook:
            return

        start_unix = int(start_time)
        type_label = event_type.replace("_", " ").title()

        if is_update:
            emoji = "\u270f\ufe0f"
            embed_title = f"{emoji} Event Updated: {title}"
            color = 0xFFA500  # orange
            desc = "A tribe event has been updated."
        else:
            emoji = "\U0001f4c5"
            embed_title = f"{emoji} New Event: {title}"
            color = 0x57F287  # green
            desc = "A new tribe event has been scheduled."

        if description:
            desc += f"\n\n{description}"

        fields: list[dict[str, Any]] = [
            {"name": "Starts", "value": f"<t:{start_unix}:F> (<t:{start_unix}:R>)", "inline": True},
            {"name": "Type", "value": type_label, "inline": True},
        ]
        if target_server:
            fields.append({"name": "Target Server", "value": target_server, "inline": True})
        if max_participants is not None:
            fields.append({"name": "Spots", "value": f"Max {max_participants}", "inline": True})
        if created_by:
            fields.append({"name": "Created by", "value": created_by, "inline": True})
        if tribe_name:
            fields.append({"name": "Tribe", "value": tribe_name, "inline": False})

        embed: dict[str, Any] = {
            "title": embed_title,
            "description": desc,
            "color": color,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if base_url:
            embed["url"] = f"{base_url.rstrip('/')}/#/calendar"

        payload: dict[str, Any] = {"embeds": [embed]}
        await self._post_webhook(self._tasks_hook, payload)

    async def send_todo_summary(
        self,
        summary_data: dict[str, Any],
        tribe_name: str = "",
        base_url: str = "",
    ) -> None:
        """Send a scheduled todo summary to Discord."""
        if not self._tasks_hook:
            return

        stats = summary_data["stats"]
        total_active = stats.get("by_status", {}).get("pending", 0) + stats.get("by_status", {}).get("in_progress", 0)
        overdue = stats["overdue"]

        # Build fields — one per goal/list
        fields: list[dict[str, Any]] = []

        for goal in summary_data["goals"]:
            items = goal["active_items"]
            if not items:
                continue
            lines: list[str] = []
            for t in items[:8]:
                line = f"\u2610 {t['title']}"
                if t.get("quantity", 1) > 1:
                    have = t.get("quantity_have", 0)
                    line += f"  {have}/{t['quantity']}"
                if t.get("due_at") and t["due_at"] < _time.time():
                    line += " \u26a0\ufe0f"
                lines.append(line)
            if len(items) > 8:
                lines.append(f"... +{len(items) - 8} more")

            fields.append({
                "name": f"\U0001f4cb {goal['name']} ({len(items)})",
                "value": "```\n" + "\n".join(lines) + "\n```",
                "inline": False,
            })

        # Ungrouped
        ungrouped = summary_data["ungrouped"]
        if ungrouped:
            lines = []
            for t in ungrouped[:5]:
                line = f"\u2610 {t['title']}"
                if t.get("due_at") and t["due_at"] < _time.time():
                    line += " \u26a0\ufe0f"
                lines.append(line)
            if len(ungrouped) > 5:
                lines.append(f"... +{len(ungrouped) - 5} more")
            fields.append({
                "name": f"\U0001f4ed Ungrouped ({len(ungrouped)})",
                "value": "```\n" + "\n".join(lines) + "\n```",
                "inline": False,
            })

        if not fields:
            return  # nothing to report

        # Summary stats line
        desc_parts = [f"**{total_active}** active tasks"]
        if overdue:
            desc_parts.append(f"**{overdue}** overdue \u26a0\ufe0f")
        description = " \u00b7 ".join(desc_parts)
        if tribe_name:
            description = f"\U0001f3d5\ufe0f **{tribe_name}**\n{description}"

        embed: dict[str, Any] = {
            "title": "\U0001f4cb Todo Summary",
            "description": description,
            "color": 0x58A6FF,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if base_url:
            embed["url"] = f"{base_url.rstrip('/')}/#/todo"

        await self._post_webhook(self._tasks_hook, {"embeds": [embed]})

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
