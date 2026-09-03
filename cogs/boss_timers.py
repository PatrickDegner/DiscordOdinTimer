import discord
from discord.ext import commands, tasks
from discord import app_commands
import io
from pathlib import Path
from PIL import Image, ImageGrab
import json
import os
import asyncio
import time
from datetime import datetime, timedelta
import re
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones
from dotenv import load_dotenv
import aiohttp

# Load environment variables
load_dotenv()

# Get BOSS_COMMAND_CHANNEL_ID from environment
BOSS_COMMAND_CHANNEL_ID = int(os.getenv('BOSS_COMMAND_CHANNEL_ID', 0))


def parse_role_ids(raw: str | None) -> frozenset[int]:
    """Parses a comma/space/semicolon separated list of role IDs, ignoring blanks and 0."""
    if not raw:
        return frozenset()
    ids = set()
    for token in re.split(r'[,;\s]+', str(raw)):
        if not token:
            continue
        try:
            role_id = int(token)
        except ValueError:
            print(f"Ignoring invalid role ID in ALLOWED_BOSS_MANAGER_ROLE_ID: {token!r}")
            continue
        if role_id > 0:
            ids.add(role_id)
    return frozenset(ids)


ALLOWED_BOSS_MANAGER_ROLE_IDS = parse_role_ids(
    os.getenv('ALLOWED_BOSS_MANAGER_ROLE_IDS') or os.getenv('ALLOWED_BOSS_MANAGER_ROLE_ID')
)

ALL_TIMEZONE_NAMES = tuple(sorted(available_timezones()))


def resolve_zone(name: str | None):
    """Returns a ZoneInfo for an IANA name, or None to use the host's local time."""
    if not name or not str(name).strip():
        return None
    try:
        return ZoneInfo(str(name).strip())
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise ValueError(
            f"Unknown timezone '{name}'. Use an IANA name like Europe/Berlin or America/New_York."
        )


try:
    DEFAULT_ZONE = resolve_zone(os.getenv('TIMEZONE'))
except ValueError as exc:
    print(f"Invalid TIMEZONE in .env, falling back to server local time: {exc}")
    DEFAULT_ZONE = None

# Import OCR functions from the ocr directory
from ocr import (
    IGNORED_DAY_TIMER_RESULT,
    MAX_TIMER_SECONDS,
    is_ignored_ocr_result,
    parse_boss_info,
    split_boss_cards,
)


class OcrTimeCorrectionModal(discord.ui.Modal, title="Correct remaining time"):
    """Lets the uploader fix a misread timer before it is registered."""

    remaining = discord.ui.TextInput(
        label="Remaining time",
        placeholder="e.g. 1h30m, 45m, or 90 (minutes)",
        max_length=32,
    )

    def __init__(self, view: 'OcrConfirmView'):
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            seconds = BossTimers._parse_duration_seconds(str(self.remaining.value))
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        await self.view_ref.finalize(interaction, int(time.time() + seconds))


class OcrConfirmView(discord.ui.View):
    """Confirmation step so a misread OCR result never silently creates a timer."""

    def __init__(self, cog: 'BossTimers', boss_name, future_timestamp, cropped_image, requester_id, formatted_time=None, timeout=180):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.boss_name = boss_name
        self.future_timestamp = future_timestamp
        self.cropped_image = cropped_image
        self.requester_id = requester_id
        self.formatted_time = formatted_time
        self.message = None
        self.completed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "❌ Only the person who uploaded this image can confirm it.",
                ephemeral=True,
            )
            return False
        return True

    def _disable_all(self):
        for child in self.children:
            child.disabled = True

    async def finalize(self, interaction: discord.Interaction, future_timestamp: int):
        self.completed = True
        self._disable_all()
        self.stop()

        try:
            image_path, using_library_image = await self.cog._register_ocr_timer(
                self.boss_name,
                future_timestamp,
                self.cropped_image,
            )
        except Exception as exc:
            await interaction.response.edit_message(
                content=f"❌ Failed to add the timer: {exc}",
                view=self,
            )
            return

        content = (
            f"✅ Added **{self.boss_name}** — spawns <t:{future_timestamp}:F> "
            f"which is <t:{future_timestamp}:R>."
        )
        if using_library_image:
            content += f"\nUsing image from {image_path}."

        await interaction.response.edit_message(content=content, view=self)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.finalize(interaction, self.future_timestamp)

    @discord.ui.button(label="Correct time", style=discord.ButtonStyle.primary, emoji="✏️")
    async def correct_time(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(OcrTimeCorrectionModal(self))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.completed = True
        self._disable_all()
        self.stop()
        await interaction.response.edit_message(
            content=f"❌ Discarded the OCR result for **{self.boss_name}**. No timer was added.",
            view=self,
        )

    async def on_timeout(self):
        if self.completed or self.message is None:
            return
        self._disable_all()
        try:
            await self.message.edit(
                content=f"⌛ Confirmation for **{self.boss_name}** timed out. No timer was added.",
                view=self,
            )
        except discord.HTTPException:
            pass


class ConfirmDeleteView(discord.ui.View):
    """Shows what /boss delete would remove before anything is destroyed."""

    def __init__(self, cog: 'BossTimers', boss_name: str, requester_id: int, timeout=60):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.boss_name = boss_name
        self.requester_id = requester_id
        self.message = None
        self.completed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "❌ Only the person who ran the command can confirm it.",
                ephemeral=True,
            )
            return False
        return True

    def _disable_all(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.completed = True
        self._disable_all()
        self.stop()

        async with self.cog.UPDATE_MESSAGE_LOCKED:
            deleted_timers, deleted_events = self.cog._delete_boss_entries(self.boss_name)

        await interaction.response.edit_message(
            content=(
                f"✅ Deleted {deleted_timers} timer(s) and {deleted_events} "
                f"static/one-time event(s) for '{self.boss_name}'."
            ),
            view=self,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.completed = True
        self._disable_all()
        self.stop()
        await interaction.response.edit_message(
            content=f"❌ Cancelled. Nothing was deleted for '{self.boss_name}'.",
            view=self,
        )

    async def on_timeout(self):
        if self.completed or self.message is None:
            return
        self._disable_all()
        try:
            await self.message.edit(
                content=f"⌛ Delete confirmation for '{self.boss_name}' timed out. Nothing was deleted.",
                view=self,
            )
        except discord.HTTPException:
            pass


class BossTimers(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.boss_timers = {}
        self.update_message_id = None
        self.upcoming_message_id = None
        self.last_update_event_key = None
        self.UPDATE_MESSAGE_LOCKED = asyncio.Lock()
        self.static_events_file = Path('data') / 'static_events.json'
        self.timers_file = Path('data') / 'timers.json'
        self.static_image_dir = Path('data') / 'static_images'
        self.boss_image_library_dir = Path('data') / 'boss_images'
        self.static_image_dir.mkdir(parents=True, exist_ok=True)
        self.boss_image_library_dir.mkdir(parents=True, exist_ok=True)
        self.static_events = {}
        self._load_timers()
        self._load_static_events()
        self._schedule_all_static_events()

    @staticmethod
    def _zone_label(zone) -> str:
        return str(zone) if zone is not None else 'server local time'

    @staticmethod
    def _naive_to_timestamp(naive_dt: datetime, zone) -> int:
        """Interprets a wall-clock datetime as belonging to the given zone."""
        if zone is None:
            return int(naive_dt.timestamp())
        return int(naive_dt.replace(tzinfo=zone).timestamp())

    @staticmethod
    def _wall_clock_now(zone) -> datetime:
        if zone is None:
            return datetime.now()
        return datetime.now(zone).replace(tzinfo=None)

    @staticmethod
    def _wall_clock_at(timestamp: float, zone) -> datetime:
        if zone is None:
            return datetime.fromtimestamp(timestamp)
        return datetime.fromtimestamp(timestamp, zone).replace(tzinfo=None)

    def _event_zone(self, event: dict):
        """Zone an event was created in; falls back to the configured default."""
        try:
            zone = resolve_zone(event.get('timezone'))
        except ValueError:
            return DEFAULT_ZONE
        return zone if zone is not None else DEFAULT_ZONE

    def _known_event_names(self) -> list[str]:
        """Every name that /boss edit or /boss delete could act on."""
        names = {data['name'] for data in self.boss_timers.values() if data.get('name')}
        names.update(event['name'] for event in self.static_events.values() if event.get('name'))
        return sorted(names, key=str.lower)

    def _known_recurring_static_names(self) -> list[str]:
        names = {
            event['name'] for event in self.static_events.values()
            if event.get('name') and not event.get('is_one_time')
        }
        return sorted(names, key=str.lower)

    async def _boss_name_autocomplete(self, interaction: discord.Interaction, current: str):
        typed = current.strip().lower()
        matches = [name for name in self._known_event_names() if typed in name.lower()][:25]
        return [app_commands.Choice(name=name, value=name) for name in matches]

    async def _recurring_static_name_autocomplete(self, interaction: discord.Interaction, current: str):
        typed = current.strip().lower()
        matches = [name for name in self._known_recurring_static_names() if typed in name.lower()][:25]
        return [app_commands.Choice(name=name, value=name) for name in matches]

    def _collect_delete_targets(self, boss_name: str) -> tuple[list[tuple[int, dict]], list[str]]:
        """Returns the timers and static event ids that /boss delete would remove."""
        target = boss_name.strip().lower()
        timers = sorted(
            (timestamp, data) for timestamp, data in self.boss_timers.items()
            if data.get('name', '').strip().lower() == target
        )
        static_ids = [
            event_id for event_id, event in self.static_events.items()
            if event.get('name', '').strip().lower() == target
        ]
        return timers, static_ids

    def _delete_boss_entries(self, boss_name: str) -> tuple[int, int]:
        """Removes matching timers and static events. Returns (timers, static events)."""
        timers, static_ids = self._collect_delete_targets(boss_name)

        for timestamp, _ in timers:
            removed_timer = self.boss_timers.pop(timestamp, None)
            self._cleanup_timer_image(removed_timer)

        for event_id in static_ids:
            event = self.static_events.pop(event_id, None)
            if event and not event.get('is_reused_image'):
                self._delete_file_if_exists(event.get('image'))

        if static_ids:
            self._save_static_events()
        if timers:
            self._save_timers()

        self.last_update_event_key = None
        return len(timers), len(static_ids)

    def _skip_static_occurrence(self, boss_name: str) -> tuple[int, int, dict]:
        """Skips the soonest recurring static occurrence matching boss_name."""
        target = boss_name.strip().lower()
        matches = []
        for timestamp, timer in self.boss_timers.items():
            static_event = self.static_events.get(timer.get('static_id'))
            if (
                timer.get('name', '').strip().lower() == target
                and static_event
                and not static_event.get('is_one_time')
            ):
                matches.append((timestamp, timer, static_event))

        if not matches:
            raise LookupError(f"No recurring static timer found for '{boss_name}'.")

        skipped_timestamp, skipped_timer, static_event = min(matches, key=lambda item: item[0])
        previous_skip_until = static_event.get('skip_until')
        static_event['skip_until'] = skipped_timestamp
        del self.boss_timers[skipped_timestamp]
        next_timestamp = self._schedule_static_event(static_event, after=skipped_timestamp)
        if next_timestamp is None:
            self.boss_timers[skipped_timestamp] = skipped_timer
            if previous_skip_until is None:
                static_event.pop('skip_until', None)
            else:
                static_event['skip_until'] = previous_skip_until
            raise RuntimeError(f"Could not schedule the next occurrence of '{boss_name}'.")

        self._save_static_events()
        self.last_update_event_key = None
        return skipped_timestamp, next_timestamp, static_event

    @staticmethod
    def _build_delete_preview(boss_name: str, timers: list, static_ids: list) -> str:
        lines = [f"⚠️ This will delete **{len(timers)}** timer(s) and **{len(static_ids)}** static/one-time event(s) named **{boss_name}**:"]
        for timestamp, data in timers[:10]:
            lines.append(f"- Timer: <t:{timestamp}:F> (<t:{timestamp}:R>)")
        if len(timers) > 10:
            lines.append(f"- ...and {len(timers) - 10} more timer(s).")
        if static_ids:
            lines.append(f"- The saved event definition will be removed, so it will stop recurring.")
        lines.append("\nThis cannot be undone. Confirm?")
        return "\n".join(lines)

    async def _timezone_autocomplete(self, interaction: discord.Interaction, current: str):
        typed = current.strip().lower()
        matches = [name for name in ALL_TIMEZONE_NAMES if typed in name.lower()][:25]
        return [app_commands.Choice(name=name, value=name) for name in matches]

    @staticmethod
    def _parse_alert_time(alert_time: str | None) -> int:
        if alert_time is None:
            return 300

        text = str(alert_time).strip().lower()
        if not text:
            return 300
        if text in {'default', 'normal'}:
            return 300
        if text.isdigit():
            value = int(text)
            if 60 <= value <= 3600:
                return value
            raise ValueError("Alert time must be between 60 and 3600 seconds (1 to 60 minutes).")

        match = re.fullmatch(r'(?:([0-9]+)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?))', text)
        if not match:
            raise ValueError("Invalid alert time. Use values like 5m, 15m, 1m, or 60m.")

        value = int(match.group(1))
        unit = match.group(2)
        if unit in {'s', 'sec', 'secs', 'second', 'seconds'}:
            seconds = value
        elif unit in {'m', 'min', 'mins', 'minute', 'minutes'}:
            seconds = value * 60
        elif unit in {'h', 'hr', 'hrs', 'hour', 'hours'}:
            seconds = value * 3600
        else:
            raise ValueError("Invalid alert time. Use values like 5m, 15m, 1m, or 60m.")

        if 60 <= seconds <= 3600:
            return seconds
        raise ValueError("Alert time must be between 60 and 3600 seconds (1 to 60 minutes).")

    @staticmethod
    def _parse_duration_seconds(text: str | None) -> int:
        """Parses '1h30m', '45m', '90' (minutes) or '2h 5m 10s' into seconds."""
        if text is None:
            raise ValueError("Enter a duration like 1h30m, 45m, or 90.")

        cleaned = str(text).strip().lower().replace(' ', '')
        if not cleaned:
            raise ValueError("Enter a duration like 1h30m, 45m, or 90.")

        if cleaned.isdigit():
            total = int(cleaned) * 60
        else:
            if not re.fullmatch(r'(?:\d+[hms])+', cleaned):
                raise ValueError("Invalid duration. Use values like 1h30m, 45m, or 90.")
            unit_seconds = {'h': 3600, 'm': 60, 's': 1}
            total = sum(
                int(value) * unit_seconds[unit]
                for value, unit in re.findall(r'(\d+)([hms])', cleaned)
            )

        if total <= 0:
            raise ValueError("Duration must be greater than zero.")
        if total > MAX_TIMER_SECONDS:
            raise ValueError(f"Duration must not exceed {MAX_TIMER_SECONDS // 3600} hours.")
        return total

    @staticmethod
    def _crop_image_for_timer(image: Image.Image) -> Image.Image:
        crop_bottom_percentage = 0.14
        cropped_height = int(image.height * (1 - crop_bottom_percentage))
        return image.crop((0, 0, image.width, cropped_height))

    @staticmethod
    def _image_to_discord_file(image: Image.Image, filename: str = 'preview.png') -> discord.File:
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
        buffer.seek(0)
        return discord.File(buffer, filename=filename)

    @staticmethod
    def _build_ocr_preview_content(boss_name: str, future_timestamp: int, formatted_time: str | None = None, library_image_path: str | None = None) -> str:
        content = (
            f"\U0001F50E OCR result: **{boss_name}**\n"
            f"Spawns <t:{future_timestamp}:F> which is <t:{future_timestamp}:R>"
        )
        if formatted_time:
            content += f" (image showed **{formatted_time}** remaining)"
        content += ".\n"
        if library_image_path:
            content += f"Using the saved image `{library_image_path}` instead of the screenshot.\n"
        content += (
            "Press **Confirm** to add the timer, **Correct time** to fix a misread, "
            "or **Cancel** to discard."
        )
        return content

    def _build_ocr_preview(self, boss_name: str, future_timestamp: int, cropped_image: Image.Image, formatted_time: str | None = None):
        """Preview shows the image that will actually be posted, not always the crop."""
        library_image_path = self._find_library_boss_image(boss_name)
        if library_image_path and os.path.exists(library_image_path):
            preview_file = discord.File(library_image_path, filename=os.path.basename(library_image_path))
            normalized = self._normalize_image_path(library_image_path)
        else:
            preview_file = self._image_to_discord_file(cropped_image)
            normalized = None

        return self._build_ocr_preview_content(boss_name, future_timestamp, formatted_time, normalized), preview_file

    @staticmethod
    def _normalize_boss_name(boss_name: str) -> str:
        return re.sub(r'\s+', ' ', boss_name).strip().casefold()

    def _find_active_timer(self, boss_name: str, now: float | None = None) -> tuple[int, dict] | None:
        """Returns the soonest future timer with the same normalized boss name."""
        now_timestamp = time.time() if now is None else now
        target = self._normalize_boss_name(boss_name)
        matches = [
            (timestamp, data)
            for timestamp, data in self.boss_timers.items()
            if timestamp > now_timestamp
            and self._normalize_boss_name(data.get('name', '')) == target
        ]
        return min(matches, key=lambda item: item[0]) if matches else None

    async def _prepare_ocr_confirmations(self, image: Image.Image, requester_id: int):
        """Parses every detected card and builds independent confirmation views."""
        cards = split_boss_cards(image)
        confirmations = []
        failures = []
        skipped_existing = []
        skipped_long_timers = []
        skipped_ignored = []
        accepted_names = set()

        for card_number, card in enumerate(cards, start=1):
            if len(cards) == 1:
                result = await asyncio.to_thread(parse_boss_info, card, allow_absolute=True)
            else:
                result = await asyncio.to_thread(parse_boss_info, card)
            result_message, future_timestamp, boss_name, formatted_time = result
            if result_message == IGNORED_DAY_TIMER_RESULT and boss_name:
                skipped_long_timers.append((card_number, boss_name))
                continue
            if is_ignored_ocr_result(result_message):
                skipped_ignored.append((card_number, result_message.removeprefix("IGNORED: ")))
                continue
            if future_timestamp is None or not boss_name:
                failures.append((card_number, result_message))
                continue

            normalized_name = self._normalize_boss_name(boss_name)
            active_timer = self._find_active_timer(boss_name)
            if active_timer is not None:
                skipped_existing.append((card_number, boss_name, active_timer[0]))
                continue
            if normalized_name in accepted_names:
                skipped_existing.append((card_number, boss_name, None))
                continue
            accepted_names.add(normalized_name)

            cropped_image = self._crop_image_for_timer(card)
            preview_content, preview_file = self._build_ocr_preview(
                boss_name, future_timestamp, cropped_image, formatted_time
            )
            if len(cards) > 1:
                preview_content = f"Card {card_number}/{len(cards)}\n{preview_content}"
            view = OcrConfirmView(
                self,
                boss_name,
                future_timestamp,
                cropped_image,
                requester_id,
                formatted_time,
            )
            confirmations.append((preview_content, preview_file, view))

        return confirmations, failures, skipped_existing, skipped_long_timers, skipped_ignored, len(cards)

    @staticmethod
    def _format_ocr_failures(failures: list[tuple[int, str]], card_count: int) -> str:
        if card_count == 1:
            return f"⚠️ {failures[0][1]}"

        lines = [f"⚠️ Could not read {len(failures)} of {card_count} detected cards:"]
        for card_number, message in failures:
            summary = message.splitlines()[0] if message else "Unknown OCR error."
            lines.append(f"- Card {card_number}: {summary}")
        return "\n".join(lines)

    @staticmethod
    def _format_existing_ocr_skips(skipped: list[tuple[int, str, int | None]]) -> str:
        lines = ["ℹ️ Skipped bosses that already have a timer:"]
        for card_number, boss_name, timestamp in skipped:
            if timestamp is None:
                lines.append(f"- Card {card_number}: **{boss_name}** (duplicate in this image)")
            else:
                lines.append(
                    f"- Card {card_number}: **{boss_name}** already spawns "
                    f"<t:{timestamp}:F> (<t:{timestamp}:R>)"
                )
        return "\n".join(lines)

    @staticmethod
    def _format_long_timer_skips(skipped: list[tuple[int, str]]) -> str:
        lines = ["ℹ️ Skipped bosses with more than 24 hours remaining:"]
        for card_number, boss_name in skipped:
            lines.append(f"- Card {card_number}: **{boss_name}** (more than 24 hours)")
        return "\n".join(lines)

    @staticmethod
    def _format_ignored_ocr_skips(skipped: list[tuple[int, str]]) -> str:
        lines = ["ℹ️ Skipped cards:"]
        for card_number, reason in skipped:
            lines.append(f"- Card {card_number}: {reason}")
        return "\n".join(lines)

    def _build_event_message_content(self, boss_name: str, timestamp: int, boss_data: dict | None) -> str:
        if boss_data is None:
            boss_data = {}

        content = f"🔥 The next Event is **{boss_name}**!\nStarts at <t:{timestamp}:F> which is <t:{timestamp}:R>."

        extra_informations = boss_data.get('extra_informations', boss_data.get('description', ''))
        if extra_informations:
            content += f"\n\n{extra_informations}"

        return content

    def _build_upcoming_events_section(
        self,
        now: float | None = None,
        horizon_seconds: int = 24 * 60 * 60,
        max_items: int = 20,
        timers: dict | None = None,
    ) -> str:
        now_ts = now if now is not None else time.time()
        cutoff_ts = now_ts + horizon_seconds
        source = timers if timers is not None else self.boss_timers

        upcoming = [
            (timestamp, boss_data)
            for timestamp, boss_data in sorted(source.items())
            if now_ts <= timestamp <= cutoff_ts
        ]

        if not upcoming:
            return "**Upcoming events (next 24h):**\nNo upcoming events in the next 24 hours."

        shown = upcoming[:max_items]
        lines = ["**Upcoming events (next 24h):**"]
        for timestamp, boss_data in shown:
            boss_name = boss_data.get('name', 'Unknown Event')
            lines.append(f"- **{boss_name}**: <t:{timestamp}:F> (<t:{timestamp}:R>)")

        remaining = len(upcoming) - len(shown)
        if remaining > 0:
            lines.append(f"- ...and {remaining} more event(s).")

        return "\n".join(lines)

    @staticmethod
    def _normalize_alert_mention(mention: str | None) -> str:
        if mention is None:
            return '@here'

        text = str(mention).strip()
        if not text:
            return '@here'

        # Keep Discord role/user mention tokens intact.
        if text.startswith('<@') and text.endswith('>'):
            return text

        # Collapse any accidental repeated leading '@' for plain mentions.
        text_no_at = text.lstrip('@')
        if not text_no_at:
            return '@here'

        lowered = text_no_at.lower()
        if lowered in {'here', '@here'}:
            return '@here'
        if lowered in {'everyone', '@everyone'}:
            return '@everyone'

        return f'@{text_no_at}'

    def _build_alert_message_content(self, boss_name: str, boss_data: dict | None) -> str:
        mention = '@here'
        if boss_data is not None:
            mention = boss_data.get('alert_mention', '@here')
        mention = self._normalize_alert_mention(mention)
        return f"{mention} The next event **{boss_name}** starts soon."

    def _should_alert_now(self, timestamp: int, boss_data: dict | None, now: float | None = None) -> bool:
        if boss_data is None:
            return False

        now_ts = now if now is not None else time.time()
        time_until_spawn = timestamp - now_ts
        alert_seconds = boss_data.get('alert_seconds', 300)
        return 0 < time_until_spawn <= alert_seconds

    def _get_alert_candidates(self, now: float | None = None, timers: dict | None = None) -> list[tuple[int, dict]]:
        timers = timers if timers is not None else self.boss_timers
        now_ts = now if now is not None else time.time()
        candidates = []
        for timestamp, boss_data in timers.items():
            if self._should_alert_now(timestamp, boss_data, now=now_ts):
                candidates.append((timestamp, boss_data))
        return sorted(candidates, key=lambda item: item[0])

    @staticmethod
    def _has_management_permission(interaction: discord.Interaction) -> bool:
        if not ALLOWED_BOSS_MANAGER_ROLE_IDS:
            return False

        roles = getattr(interaction.user, 'roles', None)
        if not roles:
            return False

        return any(getattr(role, 'id', None) in ALLOWED_BOSS_MANAGER_ROLE_IDS for role in roles)

    @commands.Cog.listener()
    async def on_ready(self):
        print("BossTimers cog loaded.")
        await self.cleanup_temp_images()
        
    async def start_tasks(self):
        self.manage_boss_timers_task.start()
        
    async def _cleanup_expired_timers(self):
        now = time.time()
        removed_any = False
        for ts in list(self.boss_timers.keys()):
            if ts < now:
                try:
                    expired_timer = self.boss_timers.pop(ts, None)
                    if not expired_timer:
                        continue

                    removed_any = True
                    if expired_timer.get('static_id'):
                        static_event = self.static_events.get(expired_timer['static_id'])
                        if static_event:
                            if static_event.get('is_one_time'):
                                # Remove one-time events permanently after they fire.
                                # Skip deletion if the image is shared with another event.
                                if not static_event.get('is_reused_image'):
                                    self._delete_file_if_exists(static_event.get('image'))
                                del self.static_events[expired_timer['static_id']]
                                self._save_static_events()
                            else:
                                self._schedule_static_event(static_event, after=now)

                    self._cleanup_timer_image(expired_timer)
                except Exception as e:
                    print(f"Error cleaning up expired timer: {e}")

        if removed_any:
            self._save_timers()

    def _get_next_timer(self):
        if not self.boss_timers:
            return None, None
        next_timestamp = min(self.boss_timers.keys())
        return next_timestamp, self.boss_timers.get(next_timestamp)

    def _sanitize_filename(self, name: str) -> str:
        cleaned = name.replace(' ', '_').replace('/', '_').replace('\\', '_')
        return ''.join(char for char in cleaned if char.isalnum() or char in ('_', '-'))

    def _normalize_boss_image_key(self, name: str) -> str:
        return self._sanitize_filename(name).lower()

    @staticmethod
    def _normalize_image_path(path_value: str | None) -> str | None:
        if path_value is None:
            return None

        text = str(path_value).strip()
        if not text:
            return text

        # Store portable paths so persisted JSON works on both Windows and Linux.
        return text.replace('\\', '/')

    def _find_library_boss_image(self, boss_name: str) -> str | None:
        image_dir = getattr(self, 'boss_image_library_dir', Path('data') / 'boss_images')
        if not image_dir.exists():
            return None

        target_key = self._normalize_boss_image_key(boss_name)
        allowed_extensions = {'.png', '.jpg', '.jpeg', '.webp'}

        for candidate in image_dir.iterdir():
            if not candidate.is_file() or candidate.suffix.lower() not in allowed_extensions:
                continue
            if self._normalize_boss_image_key(candidate.stem) == target_key:
                return str(candidate)

        return None

    def _find_static_boss_image(self, boss_name: str) -> str | None:
        """Search data/static_images/ for a file whose stem matches boss_name."""
        image_dir = getattr(self, 'static_image_dir', Path('data') / 'static_images')
        if not image_dir.exists():
            return None

        target_key = self._normalize_boss_image_key(boss_name)
        allowed_extensions = {'.png', '.jpg', '.jpeg', '.webp'}

        for candidate in image_dir.iterdir():
            if not candidate.is_file() or candidate.suffix.lower() not in allowed_extensions:
                continue
            if self._normalize_boss_image_key(candidate.stem) == target_key:
                return str(candidate)

        return None

    def _ensure_data_dir(self):
        data_dir = Path('data')
        data_dir.mkdir(exist_ok=True)
        return data_dir

    @staticmethod
    def _delete_file_if_exists(file_path: str | None):
        if not file_path:
            return
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as exc:
            print(f"Failed to delete file {file_path}: {exc}")

    def _cleanup_timer_image(self, timer_data: dict | None):
        if not timer_data:
            return

        # Static events reuse long-lived files in data/static_images.
        if timer_data.get('static_id'):
            return

        # Boss image library files are user-managed and should never be auto-deleted.
        if timer_data.get('is_custom_image', False):
            return

        self._delete_file_if_exists(timer_data.get('image'))

    def _load_static_events(self):
        try:
            if self.static_events_file.exists():
                with self.static_events_file.open('r', encoding='utf-8') as f:
                    events = json.load(f)
                    events_changed = False
                    for event in events:
                        normalized_image = self._normalize_image_path(event.get('image'))
                        if event.get('image') != normalized_image:
                            event['image'] = normalized_image
                            events_changed = True

                        normalized_mention = self._normalize_alert_mention(event.get('alert_mention', '@here'))
                        if event.get('alert_mention') != normalized_mention:
                            event['alert_mention'] = normalized_mention
                            events_changed = True
                    self.static_events = {event['id']: event for event in events}
                    if events_changed:
                        self._save_static_events()
            else:
                self.static_events = {}
        except Exception as e:
            print(f"Failed to load static events: {e}")
            self.static_events = {}

    def _save_static_events(self):
        try:
            with self.static_events_file.open('w', encoding='utf-8') as f:
                json.dump(list(self.static_events.values()), f, indent=2)
        except Exception as e:
            print(f"Failed to save static events: {e}")

    def _load_timers(self):
        """Restores non-static timers so a restart does not lose OCR/manual entries."""
        timers_file = getattr(self, 'timers_file', None)
        if timers_file is None:
            return

        try:
            if not timers_file.exists():
                return
            with timers_file.open('r', encoding='utf-8') as f:
                saved = json.load(f)
        except Exception as e:
            print(f"Failed to load timers: {e}")
            return

        now = time.time()
        entries = saved if isinstance(saved, list) else []
        restored = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                timestamp = int(entry['timestamp'])
            except (KeyError, TypeError, ValueError):
                continue
            # Static occurrences are rebuilt from static_events.json instead.
            if entry.get('static_id') or timestamp <= now:
                continue

            data = {key: value for key, value in entry.items() if key != 'timestamp'}
            data['image'] = self._normalize_image_path(data.get('image'))
            data.setdefault('sent_alert', False)
            data.setdefault('alert_seconds', 300)
            while timestamp in self.boss_timers:
                timestamp += 1
            self.boss_timers[timestamp] = data
            restored += 1

        if restored:
            print(f"Restored {restored} boss timer(s) from {timers_file}.")

        dropped = len(entries) - restored
        if dropped > 0:
            # Compact on startup so expired rows do not linger until the next write.
            print(f"Dropped {dropped} expired or invalid timer entry/entries from {timers_file}.")
            self._save_timers()

    def _save_timers(self):
        timers_file = getattr(self, 'timers_file', None)
        if timers_file is None:
            return

        try:
            timers_file.parent.mkdir(parents=True, exist_ok=True)
            payload = []
            for timestamp, data in sorted(self.boss_timers.items()):
                if data.get('static_id'):
                    continue
                entry = dict(data)
                entry['image'] = self._normalize_image_path(entry.get('image'))
                entry['timestamp'] = int(timestamp)
                payload.append(entry)
            with timers_file.open('w', encoding='utf-8') as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            print(f"Failed to save timers: {e}")

    async def _register_ocr_timer(
        self,
        boss_name: str,
        future_timestamp: int,
        cropped_image: Image.Image | None = None,
        alert_seconds: int = 300,
    ) -> tuple[str | None, bool]:
        """Persists the timer image if needed and registers the timer."""
        library_image_path = self._find_library_boss_image(boss_name)
        using_library_image = library_image_path is not None

        if using_library_image:
            selected_image_path = self._normalize_image_path(library_image_path)
        elif cropped_image is not None:
            sanitized_boss_name = self._sanitize_filename(boss_name)
            data_dir = self._ensure_data_dir()
            unique_filename = data_dir / f"cropped_screenshot_{sanitized_boss_name}_{future_timestamp}.png"
            cropped_image.save(unique_filename)
            selected_image_path = self._normalize_image_path(str(unique_filename))
        else:
            selected_image_path = None

        async with self.UPDATE_MESSAGE_LOCKED:
            existing_timer = self.boss_timers.get(future_timestamp)
            self._cleanup_timer_image(existing_timer)
            self.boss_timers[future_timestamp] = {
                'name': boss_name,
                'image': selected_image_path,
                'sent_alert': False,
                'alert_seconds': alert_seconds,
                'extra_informations': '',
                'is_custom_image': using_library_image,
            }
            self.last_update_event_key = None
            self._save_timers()

        return selected_image_path, using_library_image

    def _resolve_new_timestamp(self, text: str, now: float | None = None, zone=None) -> int:
        """Resolves 'HH:MM', 'YYYY-MM-DD HH:MM' or a duration like '1h30m' to a timestamp."""
        now_ts = now if now is not None else time.time()
        cleaned = str(text).strip()

        date_and_time = re.fullmatch(r'(\S+)\s+(\d{1,2}:\d{2})', cleaned)
        if date_and_time:
            year, month, day = self._parse_date(date_and_time.group(1), zone=zone)
            hour, minute = self._parse_time(date_and_time.group(2))
            timestamp = self._naive_to_timestamp(datetime(year, month, day, hour, minute), zone)
            if timestamp <= now_ts:
                raise ValueError(f"'{cleaned}' is already in the past.")
            return timestamp

        if re.fullmatch(r'\d{1,2}:\d{2}', cleaned):
            hour, minute = self._parse_time(cleaned)
            base = self._wall_clock_at(now_ts, zone)
            candidate = datetime(base.year, base.month, base.day, hour, minute)
            if self._naive_to_timestamp(candidate, zone) <= now_ts:
                candidate += timedelta(days=1)
            return self._naive_to_timestamp(candidate, zone)

        return int(now_ts + self._parse_duration_seconds(cleaned))

    def _apply_timer_edit(
        self,
        boss_name: str,
        new_timestamp: int | None = None,
        alert_seconds: int | None = None,
        alert_mention: str | None = None,
        extra_informations: str | None = None,
        now: float | None = None,
    ) -> tuple[int, dict, list[str]]:
        """Edits the soonest timer matching boss_name. Raises LookupError when absent."""
        target_name = boss_name.strip().lower()
        matching = sorted(
            timestamp for timestamp, data in self.boss_timers.items()
            if data.get('name', '').strip().lower() == target_name
        )
        if not matching:
            raise LookupError(f"No scheduled timer found for '{boss_name}'.")

        current_timestamp = matching[0]
        data = self.boss_timers[current_timestamp]
        changes = []

        if alert_seconds is not None:
            data['alert_seconds'] = alert_seconds
            changes.append(f"alert lead time to {alert_seconds}s")

        if alert_mention is not None:
            data['alert_mention'] = self._normalize_alert_mention(alert_mention)
            changes.append(f"mention to {data['alert_mention']}")

        if extra_informations is not None:
            data['extra_informations'] = extra_informations
            changes.append("extra informations")

        if new_timestamp is not None and new_timestamp != current_timestamp:
            del self.boss_timers[current_timestamp]
            while new_timestamp in self.boss_timers:
                new_timestamp += 1
            self.boss_timers[new_timestamp] = data
            current_timestamp = new_timestamp
            changes.append("spawn time")

        # Re-arm the alert whenever the timer moved back outside its alert window.
        if not self._should_alert_now(current_timestamp, data, now=now):
            data['sent_alert'] = False

        self.last_update_event_key = None
        self._save_timers()
        return current_timestamp, data, changes

    @staticmethod
    def _parse_schedule_days(schedule_text: str):
        normalized = schedule_text.strip().lower().replace('and', ',')
        normalized = normalized.replace('-', ' ')
        normalized = re.sub(r'\s+', ' ', normalized).strip()

        if normalized.startswith('every '):
            normalized = normalized[6:].strip()

        monthly_aliases = {
            'monthly',
            'first of month',
            'first day of month',
            '1st of month',
            '1st day of month',
            'first of the month',
            'first day of the month',
            '1st of the month',
            '1st day of the month',
        }
        if normalized in monthly_aliases:
            return ['monthly']

        monthly_day_match = re.fullmatch(r'(\d{1,2})(?:st|nd|rd|th)(?:\s+of(?:\s+the)?\s+month)?', normalized)
        if monthly_day_match:
            day = int(monthly_day_match.group(1))
            if 1 <= day <= 31:
                return ['monthly', day]
            raise ValueError("Monthly schedule day must be between 1 and 31.")

        if normalized in ('daily', 'everyday'):
            return list(range(7))
        if normalized in ('weekdays', 'monday friday', 'monday-friday', 'mon fri', 'mon-fri'):
            return [0, 1, 2, 3, 4]
        if normalized in ('weekends', 'saturday sunday', 'saturday-sunday', 'sat sun', 'sat-sun'):
            return [5, 6]

        mapping = {
            'monday': 0, 'mon': 0,
            'tuesday': 1, 'tue': 1, 'tues': 1,
            'wednesday': 2, 'wed': 2,
            'thursday': 3, 'thu': 3, 'thur': 3, 'thurs': 3,
            'friday': 4, 'fri': 4,
            'saturday': 5, 'sat': 5,
            'sunday': 6, 'sun': 6,
        }

        parts = re.split(r'[\s,]+', normalized)
        days = []
        for part in parts:
            if not part:
                continue
            if part in mapping:
                days.append(mapping[part])
            else:
                raise ValueError(f"Invalid schedule day: '{part}'. Use names like Tuesday, 'daily', 'monthly', or a day like '15th'.")

        if not days:
            raise ValueError("Schedule must include at least one weekday, 'daily', 'monthly', or a day like '15th'.")

        return sorted(set(days))

    def _parse_time(self, time_text: str):
        match = re.fullmatch(r'([01]?\d|2[0-3]):([0-5]\d)', time_text.strip())
        if not match:
            raise ValueError("Invalid time format. Use HH:MM in 24-hour time.")
        return int(match.group(1)), int(match.group(2))

    @staticmethod
    def _parse_date(date_text: str, zone=None):
        """Parse a date string into (year, month, day).

        Accepted formats: YYYY-MM-DD, DD.MM.YYYY, DD/MM/YYYY, or
        keywords: today, tomorrow. Relative keywords resolve in `zone`.
        Raises ValueError on unrecognised format or invalid calendar date.
        """
        text = date_text.strip()
        lowered = text.lower()

        if lowered == 'today':
            now = BossTimers._wall_clock_now(zone)
            return now.year, now.month, now.day

        if lowered == 'tomorrow':
            tomorrow = BossTimers._wall_clock_now(zone) + timedelta(days=1)
            return tomorrow.year, tomorrow.month, tomorrow.day

        # YYYY-MM-DD
        m = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})', text)
        if m:
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            # DD.MM.YYYY or DD/MM/YYYY
            m = re.fullmatch(r'(\d{2})[./](\d{2})[./](\d{4})', text)
            if m:
                day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            else:
                raise ValueError(
                    "Invalid date format. Use YYYY-MM-DD, DD.MM.YYYY, or DD/MM/YYYY."
                )
        try:
            datetime(year, month, day)  # validate calendar date
        except ValueError:
            raise ValueError(f"Invalid date: {date_text!r}. Check day/month values.")
        return year, month, day

    def _get_next_occurrence(self, event: dict, after: float | None = None):
        after_ts = after if after is not None else time.time()
        zone = self._event_zone(event)

        # One-time events have a fixed absolute date rather than a recurring schedule.
        if event.get('is_one_time'):
            year, month, day = self._parse_date(event['date'], zone=zone)
            hour, minute = self._parse_time(event['time'])
            event_ts = self._naive_to_timestamp(datetime(year, month, day, hour, minute), zone)
            if event_ts <= after_ts:
                raise ValueError(
                    f"One-time event '{event.get('name')}' is scheduled in the past ({event['date']} {event['time']})."
                )
            return event_ts

        after_dt = self._wall_clock_at(after_ts, zone)
        hour, minute = self._parse_time(event['time'])
        schedule_days = self._parse_schedule_days(event['schedule'])

        if schedule_days == ['monthly']:
            for month_offset in range(0, 24):
                candidate_month = after_dt.month + month_offset
                candidate_year = after_dt.year + (candidate_month - 1) // 12
                candidate_month = ((candidate_month - 1) % 12) + 1
                candidate_date = datetime(candidate_year, candidate_month, 1).date()
                candidate_dt = datetime(candidate_date.year, candidate_date.month, 1, hour, minute)
                candidate_ts = self._naive_to_timestamp(candidate_dt, zone)
                if candidate_ts > after_ts:
                    return int(candidate_ts)
            raise ValueError("Could not find a next monthly occurrence within the next two years.")

        if len(schedule_days) == 2 and schedule_days[0] == 'monthly':
            month_day = schedule_days[1]
            for month_offset in range(0, 24):
                candidate_month = after_dt.month + month_offset
                candidate_year = after_dt.year + (candidate_month - 1) // 12
                candidate_month = ((candidate_month - 1) % 12) + 1
                last_day = monthrange(candidate_year, candidate_month)[1]
                target_day = min(month_day, last_day)
                candidate_dt = datetime(candidate_year, candidate_month, target_day, hour, minute)
                candidate_ts = self._naive_to_timestamp(candidate_dt, zone)
                if candidate_ts > after_ts:
                    return int(candidate_ts)
            raise ValueError("Could not find a next monthly occurrence within the next two years.")

        weekdays = schedule_days
        for day_offset in range(0, 14):
            candidate_date = after_dt.date() + timedelta(days=day_offset)
            if candidate_date.weekday() not in weekdays:
                continue
            candidate_dt = datetime(
                candidate_date.year,
                candidate_date.month,
                candidate_date.day,
                hour,
                minute,
            )
            candidate_ts = self._naive_to_timestamp(candidate_dt, zone)
            if candidate_ts > after_ts:
                return int(candidate_ts)

        raise ValueError("Could not find a next occurrence within the next two weeks.")

    def _schedule_static_event(self, event: dict, after: float | None = None):
        try:
            effective_after = after
            skip_until = event.get('skip_until')
            if skip_until is not None:
                skip_until = int(skip_until)
                if effective_after is None or skip_until > effective_after:
                    effective_after = skip_until
            next_timestamp = self._get_next_occurrence(event, after=effective_after)
        except (TypeError, ValueError) as e:
            print(f"Static event scheduling failed for {event.get('name')}: {e}")
            return None

        while next_timestamp in self.boss_timers:
            next_timestamp += 1

        self.boss_timers[next_timestamp] = {
            'name': event['name'],
            'image': event['image'],
            'sent_alert': False,
            'static_id': event['id'],
            'extra_informations': event.get('extra_informations', event.get('description', '')),
            'alert_seconds': event.get('alert_seconds', 300),
            'alert_mention': self._normalize_alert_mention(event.get('alert_mention', '@here')),
        }
        return next_timestamp

    def _schedule_all_static_events(self):
        for event in self.static_events.values():
            self._schedule_static_event(event)

    async def _read_attachment_with_retries(
        self,
        attachment: discord.Attachment,
        max_attempts: int = 3,
        initial_delay: float = 1.0,
    ) -> bytes:
        last_error = None

        for attempt in range(1, max_attempts + 1):
            try:
                return await attachment.read()
            except Exception as exc:
                last_error = exc
                if attempt >= max_attempts:
                    break

                # Backoff retries for transient Discord CDN/network errors.
                delay = initial_delay * (2 ** (attempt - 1))
                await asyncio.sleep(delay)

        raise RuntimeError(f"Cannot read attachment after {max_attempts} attempts: {last_error}")

    async def _get_or_create_update_message(self, update_channel):
        if self.update_message_id:
            try:
                return await update_channel.fetch_message(self.update_message_id)
            except discord.NotFound:
                self.update_message_id = None

        await self.cleanup_old_messages(update_channel, self.bot.user)
        sent_message = await update_channel.send("Fetching next Event timer...")
        self.update_message_id = sent_message.id
        return sent_message

    async def _get_or_create_upcoming_message(self, update_channel):
        if self.upcoming_message_id:
            try:
                return await update_channel.fetch_message(self.upcoming_message_id)
            except discord.NotFound:
                self.upcoming_message_id = None

        sent_message = await update_channel.send("Fetching upcoming events...")
        self.upcoming_message_id = sent_message.id
        return sent_message

    async def _safe_edit_update_message(
        self,
        message,
        content: str,
        image_path: str | None = None,
        preserve_attachments: bool = False,
    ):
        if image_path and os.path.exists(image_path):
            try:
                discord_file = discord.File(image_path, filename=os.path.basename(image_path))
                await message.edit(content=content, attachments=[discord_file])
                return
            except Exception as exc:
                print(f"Attachment-based update failed, retrying without attachment: {exc}")

        if preserve_attachments:
            try:
                await message.edit(content=content)
                return
            except Exception as exc:
                print(f"Preserve-attachment update failed: {exc}")

        try:
            await message.edit(content=content, attachments=[])
        except Exception as exc:
            print(f"Fallback update failed: {exc}")
            try:
                await message.edit(content=content)
            except Exception as fallback_exc:
                print(f"Content-only update failed: {fallback_exc}")

    def cog_unload(self):
        self.manage_boss_timers_task.cancel()

    @staticmethod
    async def cleanup_old_messages(channel, bot_user):
        """Deletes all messages sent by the bot in a specified channel.
        DEPRECATED: No longer used since switching to DM-only updates."""
        if not channel:
            print("Cleanup channel not found.")
            return

        try:
            print(f"Cleaning up old messages in channel {channel.name}...")
            async for message in channel.history(limit=100):
                if message.author == bot_user:
                    await message.delete()
                    await asyncio.sleep(0.5) 
            print("Cleanup complete.")
        except discord.Forbidden:
            print("Bot does not have permissions to delete messages in this channel.")
        except Exception as e:
            print(f"An error occurred during message cleanup: {e}")

    @tasks.loop(seconds=15)
    async def manage_boss_timers_task(self):
        """Manages the single update message, changing content based on time until spawn."""
        await self.bot.wait_until_ready()
        update_channel = self.bot.get_channel(BOSS_COMMAND_CHANNEL_ID)

        if not update_channel:
            print("Update channel not found.")
            return

        async with self.UPDATE_MESSAGE_LOCKED:
            try:
                message_to_edit = await self._get_or_create_update_message(update_channel)
                upcoming_message_to_edit = await self._get_or_create_upcoming_message(update_channel)
                await self._cleanup_expired_timers()

                if not self.boss_timers:
                    await message_to_edit.edit(content="There are no upcoming bosses scheduled.", attachments=[])
                    self.last_update_event_key = None
                    await upcoming_message_to_edit.edit(
                        content=self._build_upcoming_events_section(now=time.time()),
                        attachments=[],
                    )
                    return

                next_timestamp, boss_data = self._get_next_timer()
                if not boss_data:
                    return

                next_boss_name = boss_data['name']
                image_path = boss_data['image']
                message_content = self._build_event_message_content(next_boss_name, next_timestamp, boss_data)
                upcoming_content = self._build_upcoming_events_section(now=time.time())
                current_event_key = (next_timestamp, next_boss_name, image_path)
                event_changed = current_event_key != self.last_update_event_key

                if event_changed and image_path and os.path.exists(image_path):
                    await self._safe_edit_update_message(message_to_edit, message_content, image_path=image_path)
                elif event_changed:
                    if image_path:
                        print("Image file not found for next Event, updating without image.")
                    await self._safe_edit_update_message(message_to_edit, message_content)
                else:
                    await self._safe_edit_update_message(
                        message_to_edit,
                        message_content,
                        preserve_attachments=True,
                    )

                self.last_update_event_key = current_event_key

                await self._safe_edit_update_message(upcoming_message_to_edit, upcoming_content)

                alert_candidates = self._get_alert_candidates(now=time.time())
                for alert_timestamp, alert_boss_data in alert_candidates:
                    if alert_boss_data.get('sent_alert', False):
                        continue
                    if alert_timestamp != next_timestamp:
                        continue
                    try:
                        # Keep static timer mention in sync with persisted static event config.
                        static_id = alert_boss_data.get('static_id')
                        if static_id:
                            static_event = self.static_events.get(static_id)
                            if static_event:
                                alert_boss_data['alert_mention'] = self._normalize_alert_mention(
                                    static_event.get('alert_mention', '@here')
                                )

                        alert_boss_data['sent_alert'] = True
                        # Persist immediately so a restart before the spawn does not re-alert.
                        self._save_timers()
                        alert_message_content = self._build_alert_message_content(alert_boss_data['name'], alert_boss_data)
                        await update_channel.send(alert_message_content, delete_after=180)
                    except Exception as exc:
                        print(f"Error sending temporary alert message: {exc}")

                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Updated next Event message for {next_boss_name}.")
            except discord.NotFound:
                print("Update message not found. Creating a new one.")
                message_to_edit = await self._get_or_create_update_message(update_channel)
            except Exception as e:
                print(f"Error updating message: {e}")

    # Create a command group for boss management
    boss_group = app_commands.Group(name="boss", description="Manage boss timers.")
    add_group = app_commands.Group(name="add", description="Add boss timers.")
    boss_group.add_command(add_group)

    @add_group.command(name="static", description="Add a new static boss event.")
    @app_commands.describe(
        name="Name of the static event.",
        schedule="Recurring schedule like 'Tuesday and Thursday' or 'daily'.",
        time="Time of day in 24-hour HH:MM format.",
        image="Image to use for this event. You can also paste or drop it here.",
        alert_time="Optional alert timing like 5m, 15m, 1s, or 90.",
        alert_mention="Optional text mention like @role, @everyone, or LW. Defaults to @here.",
        extra_informations="Optional text to show beneath the event message.",
        timezone="Timezone the time is given in, e.g. America/New_York. Defaults to the server timezone.",
    )
    @app_commands.autocomplete(timezone=_timezone_autocomplete)
    async def add_static_boss_command(
        self,
        interaction: discord.Interaction,
        name: str,
        schedule: str,
        time: str,
        image: discord.Attachment | None = None,
        alert_time: str | None = None,
        alert_mention: str | None = None,
        extra_informations: str | None = None,
        timezone: str | None = None,
    ):
        """Slash command to add a persistent static event."""
        if not self._has_management_permission(interaction):
            await interaction.response.send_message(
                "❌ You do not have the required boss management role to use this command.",
                ephemeral=True,
            )
            return

        if interaction.channel_id != BOSS_COMMAND_CHANNEL_ID:
            await interaction.response.send_message(
                f"❌ Please use this command in the <#{BOSS_COMMAND_CHANNEL_ID}> channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            hours, minutes = self._parse_time(time)
            self._parse_schedule_days(schedule)
            alert_seconds = self._parse_alert_time(alert_time)
            event_zone = resolve_zone(timezone) or DEFAULT_ZONE
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        image_bytes = None
        if image is not None:
            try:
                image_bytes = await self._read_attachment_with_retries(image)
            except Exception as exc:
                await interaction.followup.send(
                    f"Unable to read the uploaded image: {exc}",
                    ephemeral=True,
                )
                return
        else:
            try:
                clipboard_image = ImageGrab.grabclipboard()
                if not clipboard_image:
                    await interaction.followup.send("No image was found in the clipboard.", ephemeral=True)
                    return
                image_bytes = io.BytesIO()
                clipboard_image.save(image_bytes, format='PNG')
                image_bytes = image_bytes.getvalue()
            except Exception as exc:
                await interaction.followup.send(
                    f"Unable to read the clipboard image: {exc}",
                    ephemeral=True,
                )
                return

        try:
            image = Image.open(io.BytesIO(image_bytes))
            image.verify()
            image = Image.open(io.BytesIO(image_bytes))
        except Exception as exc:
            await interaction.followup.send(
                f"Unable to validate the image: {exc}",
                ephemeral=True,
            )
            return

        sanitized_name = self._sanitize_filename(name)
        event_id = str(uuid.uuid4())
        filename = self.static_image_dir / f"static_{sanitized_name}_{event_id}.png"
        image.save(filename)

        event = {
            'id': event_id,
            'name': name,
            'schedule': schedule,
            'time': f"{hours:02d}:{minutes:02d}",
            'image': filename.as_posix(),
            'alert_seconds': alert_seconds,
            'alert_mention': self._normalize_alert_mention(alert_mention),
            'extra_informations': extra_informations or '',
            'timezone': str(event_zone) if event_zone is not None else '',
        }

        self.static_events[event_id] = event
        self._save_static_events()
        next_timestamp = self._schedule_static_event(event)

        alert_target = event['alert_mention']
        response = (
            f"✅ Static event '{name}' added for {schedule} at {hours:02d}:{minutes:02d} "
            f"({self._zone_label(event_zone)}) with alert timing {alert_seconds}s and mention {alert_target}."
        )
        if next_timestamp:
            response += f"\nNext occurrence: <t:{next_timestamp}:F> which is <t:{next_timestamp}:R>."

        await interaction.followup.send(response, ephemeral=True)

    @add_group.command(name="onetime", description="Add a one-time event that fires once and then removes itself.")
    @app_commands.describe(
        name="Name of the event.",
        date="Date in YYYY-MM-DD, DD.MM.YYYY, DD/MM/YYYY, or 'today'/'tomorrow'.",
        time="Time of day in 24-hour HH:MM format.",
        image="Image to use for this event. You can also paste or drop it here.",
        alert_time="Optional alert timing like 5m, 15m, 1s, or 90.",
        alert_mention="Optional text mention like @role, @everyone, or LW. Defaults to @here.",
        extra_informations="Optional text to show beneath the event message.",
        timezone="Timezone the date/time is given in, e.g. America/New_York. Defaults to the server timezone.",
    )
    @app_commands.autocomplete(timezone=_timezone_autocomplete)
    async def add_onetime_boss_command(
        self,
        interaction: discord.Interaction,
        name: str,
        date: str,
        time: str,
        image: discord.Attachment | None = None,
        alert_time: str | None = None,
        alert_mention: str | None = None,
        extra_informations: str | None = None,
        timezone: str | None = None,
    ):
        """Slash command to add a one-time event that fires once and then auto-removes itself."""
        if not self._has_management_permission(interaction):
            await interaction.response.send_message(
                "❌ You do not have the required boss management role to use this command.",
                ephemeral=True,
            )
            return

        if interaction.channel_id != BOSS_COMMAND_CHANNEL_ID:
            await interaction.response.send_message(
                f"❌ Please use this command in the <#{BOSS_COMMAND_CHANNEL_ID}> channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            event_zone = resolve_zone(timezone) or DEFAULT_ZONE
            year, month, day = self._parse_date(date, zone=event_zone)
            hours, minutes = self._parse_time(time)
            alert_seconds = self._parse_alert_time(alert_time)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        event_timestamp = self._naive_to_timestamp(datetime(year, month, day, hours, minutes), event_zone)
        # `time` is shadowed by the command parameter here, so derive "now" from datetime.
        if event_timestamp <= datetime.now(event_zone).timestamp():
            await interaction.followup.send(
                f"❌ The date/time {year:04d}-{month:02d}-{day:02d} {hours:02d}:{minutes:02d} "
                f"({self._zone_label(event_zone)}) is already in the past.",
                ephemeral=True,
            )
            return

        event_id = str(uuid.uuid4())

        # Prefer an existing image from boss_images/ or static_images/ over the upload.
        preexisting_image = self._find_library_boss_image(name) or self._find_static_boss_image(name)

        if preexisting_image:
            selected_image_path = preexisting_image
            is_reused_image = True
        else:
            image_bytes = None
            if image is not None:
                try:
                    image_bytes = await self._read_attachment_with_retries(image)
                except Exception as exc:
                    await interaction.followup.send(
                        f"Unable to read the uploaded image: {exc}",
                        ephemeral=True,
                    )
                    return
            else:
                try:
                    clipboard_image = ImageGrab.grabclipboard()
                    if not clipboard_image:
                        await interaction.followup.send("No image was found in the clipboard.", ephemeral=True)
                        return
                    image_bytes = io.BytesIO()
                    clipboard_image.save(image_bytes, format='PNG')
                    image_bytes = image_bytes.getvalue()
                except Exception as exc:
                    await interaction.followup.send(
                        f"Unable to read the clipboard image: {exc}",
                        ephemeral=True,
                    )
                    return

            try:
                img = Image.open(io.BytesIO(image_bytes))
                img.verify()
                img = Image.open(io.BytesIO(image_bytes))
            except Exception as exc:
                await interaction.followup.send(
                    f"Unable to validate the image: {exc}",
                    ephemeral=True,
                )
                return

            sanitized_name = self._sanitize_filename(name)
            filename = self.static_image_dir / f"onetime_{sanitized_name}_{event_id}.png"
            img.save(filename)
            selected_image_path = filename.as_posix()
            is_reused_image = False

        event = {
            'id': event_id,
            'name': name,
            'is_one_time': True,
            'is_reused_image': is_reused_image,
            'date': f"{year:04d}-{month:02d}-{day:02d}",
            'time': f"{hours:02d}:{minutes:02d}",
            'image': selected_image_path,
            'alert_seconds': alert_seconds,
            'alert_mention': self._normalize_alert_mention(alert_mention),
            'extra_informations': extra_informations or '',
            'timezone': str(event_zone) if event_zone is not None else '',
        }

        self.static_events[event_id] = event
        self._save_static_events()
        self._schedule_static_event(event)

        alert_target = event['alert_mention']
        await interaction.followup.send(
            f"✅ One-time event '{name}' added for {year:04d}-{month:02d}-{day:02d} at {hours:02d}:{minutes:02d} "
            f"({self._zone_label(event_zone)}) with alert timing {alert_seconds}s and mention {alert_target}."
            f"\nStarts <t:{event_timestamp}:F> which is <t:{event_timestamp}:R>.",
            ephemeral=True,
        )

    @add_group.command(name="normal", description="Add a boss timer from an OCR image like the DM flow.")
    @app_commands.describe(
        image="Image to process. You can also paste or drop it here.",
    )
    async def add_normal_boss_command(
        self,
        interaction: discord.Interaction,
        image: discord.Attachment,
    ):
        """Slash command to add a boss timer using OCR like the DM image flow."""
        if not self._has_management_permission(interaction):
            await interaction.response.send_message(
                "❌ You do not have the required boss management role to use this command.",
                ephemeral=True,
            )
            return

        if interaction.channel_id != BOSS_COMMAND_CHANNEL_ID:
            await interaction.response.send_message(
                f"❌ Please use this command in the <#{BOSS_COMMAND_CHANNEL_ID}> channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            image_bytes = await self._read_attachment_with_retries(image)
            image_to_process = Image.open(io.BytesIO(image_bytes))
        except Exception as exc:
            await interaction.followup.send(f"Unable to read the uploaded image: {exc}", ephemeral=True)
            return

        try:
            confirmations, failures, skipped_existing, skipped_long_timers, skipped_ignored, card_count = await self._prepare_ocr_confirmations(
                image_to_process, interaction.user.id
            )
            for preview_content, preview_file, view in confirmations:
                view.message = await interaction.followup.send(
                    content=preview_content,
                    file=preview_file,
                    view=view,
                    ephemeral=True,
                    wait=True,
                )
            if skipped_existing:
                await interaction.followup.send(
                    self._format_existing_ocr_skips(skipped_existing),
                    ephemeral=True,
                )
            if skipped_long_timers:
                await interaction.followup.send(
                    self._format_long_timer_skips(skipped_long_timers),
                    ephemeral=True,
                )
            if skipped_ignored:
                await interaction.followup.send(
                    self._format_ignored_ocr_skips(skipped_ignored),
                    ephemeral=True,
                )
            if failures:
                await interaction.followup.send(
                    self._format_ocr_failures(failures, card_count),
                    ephemeral=True,
                )
        except Exception as exc:
            await interaction.followup.send(f"An unexpected error occurred: {exc}", ephemeral=True)

    @boss_group.command(name="list", description="Shows a list of all upcoming boss timers.")
    async def bosslist_command(self, interaction: discord.Interaction):
        """Slash command to show a list of all upcoming boss timers."""
        await interaction.response.defer(thinking=True, ephemeral=True)

        async with self.UPDATE_MESSAGE_LOCKED:
            if not self.boss_timers:
                message = "There are no upcoming bosses scheduled."
                await interaction.followup.send(message, ephemeral=True)
                return

            sorted_bosses = sorted(self.boss_timers.items())
            
            boss_list_message = "Here are the upcoming boss timers:\n\n"
            for timestamp, data in sorted_bosses:
                boss_name = data['name']
                discord_timestamp = f"<t:{timestamp}:F> which is <t:{timestamp}:R>"
                boss_list_message += f"**{boss_name}**: Starts at {discord_timestamp}\n"

            await interaction.followup.send(boss_list_message, ephemeral=True)

    @boss_group.command(name="delete", description="Deletes all timer entries for a specified boss.")
    @app_commands.describe(boss_name="Name of the boss/event to delete.")
    @app_commands.autocomplete(boss_name=_boss_name_autocomplete)
    async def delete_boss_command(self, interaction: discord.Interaction, boss_name: str):
        """Slash command to delete boss timers by name, behind a confirmation."""
        if not self._has_management_permission(interaction):
            await interaction.response.send_message(
                "❌ You do not have the required boss management role to use this command.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        async with self.UPDATE_MESSAGE_LOCKED:
            timers, static_ids = self._collect_delete_targets(boss_name)

        if not timers and not static_ids:
            await interaction.followup.send(
                f"❌ Could not find an event named '{boss_name}'.", ephemeral=True
            )
            return

        view = ConfirmDeleteView(self, boss_name, interaction.user.id)
        view.message = await interaction.followup.send(
            content=self._build_delete_preview(boss_name, timers, static_ids),
            view=view,
            ephemeral=True,
            wait=True,
        )

    @boss_group.command(name="skip", description="Skip the next occurrence of a recurring static timer.")
    @app_commands.describe(name="Name of the recurring static event to skip.")
    @app_commands.autocomplete(name=_recurring_static_name_autocomplete)
    async def skip_boss_command(self, interaction: discord.Interaction, name: str):
        """Slash command to skip one occurrence without deleting its recurring event."""
        if not self._has_management_permission(interaction):
            await interaction.response.send_message(
                "❌ You do not have the required boss management role to use this command.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        async with self.UPDATE_MESSAGE_LOCKED:
            try:
                skipped_timestamp, next_timestamp, event = self._skip_static_occurrence(name)
            except (LookupError, RuntimeError) as exc:
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)
                return

        await interaction.followup.send(
            f"✅ Skipped **{event['name']}** at <t:{skipped_timestamp}:F>.\n"
            f"The next occurrence is <t:{next_timestamp}:F> which is <t:{next_timestamp}:R>.",
            ephemeral=True,
        )

    @boss_group.command(name="edit", description="Edit the next scheduled timer for a boss.")
    @app_commands.describe(
        boss_name="Name of the boss/event to edit.",
        new_time="New spawn time: duration (1h30m, 45m, 90), HH:MM, or 'YYYY-MM-DD HH:MM'.",
        alert_time="New alert lead time like 5m, 15m, or 600.",
        alert_mention="New mention like @role, @everyone, or LW.",
        extra_informations="Replacement text shown beneath the event message.",
        timezone="Timezone new_time is given in, e.g. America/New_York. Defaults to the server timezone.",
    )
    @app_commands.autocomplete(boss_name=_boss_name_autocomplete, timezone=_timezone_autocomplete)
    async def edit_boss_command(
        self,
        interaction: discord.Interaction,
        boss_name: str,
        new_time: str | None = None,
        alert_time: str | None = None,
        alert_mention: str | None = None,
        extra_informations: str | None = None,
        timezone: str | None = None,
    ):
        """Slash command to adjust the soonest timer for a boss without re-adding it."""
        if not self._has_management_permission(interaction):
            await interaction.response.send_message(
                "❌ You do not have the required boss management role to use this command.",
                ephemeral=True,
            )
            return

        if new_time is None and alert_time is None and alert_mention is None and extra_informations is None:
            await interaction.response.send_message(
                "❌ Provide at least one field to change.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            edit_zone = resolve_zone(timezone) or DEFAULT_ZONE
            new_timestamp = self._resolve_new_timestamp(new_time, zone=edit_zone) if new_time is not None else None
            alert_seconds = self._parse_alert_time(alert_time) if alert_time is not None else None
        except ValueError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        async with self.UPDATE_MESSAGE_LOCKED:
            try:
                timestamp, data, changes = self._apply_timer_edit(
                    boss_name,
                    new_timestamp=new_timestamp,
                    alert_seconds=alert_seconds,
                    alert_mention=alert_mention,
                    extra_informations=extra_informations,
                )
            except LookupError as exc:
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)
                return

            response = (
                f"✅ Updated **{data['name']}** ({', '.join(changes)}).\n"
                f"Now spawns <t:{timestamp}:F> which is <t:{timestamp}:R>."
            )
            if data.get('static_id'):
                response += "\n⚠️ This is a static event occurrence; the next occurrence reverts to the saved schedule."

        await interaction.followup.send(response, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message):
        """Handles image processing for messages sent via DM."""
        if message.author == self.bot.user or not isinstance(message.channel, discord.DMChannel) or not message.attachments:
            return

        image_attachment = message.attachments[0]
        if image_attachment.content_type and image_attachment.content_type.startswith('image/'):
            await message.channel.send("Processing your image, please wait...")
            try:
                image_bytes = await self._read_attachment_with_retries(image_attachment)
                img = Image.open(io.BytesIO(image_bytes))
                confirmations, failures, skipped_existing, skipped_long_timers, skipped_ignored, card_count = await self._prepare_ocr_confirmations(
                    img, message.author.id
                )
                for preview_content, preview_file, view in confirmations:
                    view.message = await message.channel.send(
                        content=preview_content,
                        file=preview_file,
                        view=view,
                    )
                if skipped_existing:
                    await message.channel.send(self._format_existing_ocr_skips(skipped_existing))
                if skipped_long_timers:
                    await message.channel.send(self._format_long_timer_skips(skipped_long_timers))
                if skipped_ignored:
                    await message.channel.send(self._format_ignored_ocr_skips(skipped_ignored))
                if failures:
                    await message.channel.send(self._format_ocr_failures(failures, card_count))

            except (aiohttp.ClientConnectorError, OSError) as e:
                await message.channel.send("An unexpected network error occurred while fetching the image. Please try again.")
            except Exception as e:
                await message.channel.send(f"An unexpected error occurred: {e}")

    # Fixed-schedule helpers and timers fully removed; timers are created via OCR or manual commands only.

    def _referenced_image_paths(self) -> set:
        """Absolute paths of images still needed by restored or scheduled timers."""
        referenced = set()
        for data in self.boss_timers.values():
            image_path = data.get('image')
            if not image_path:
                continue
            try:
                referenced.add(Path(image_path).resolve())
            except OSError:
                continue
        return referenced

    async def cleanup_temp_images(self):
        """Removes PNG files at the data root that no active timer references."""
        try:
            data_dir = Path('data')
            if not data_dir.exists():
                data_dir.mkdir(parents=True, exist_ok=True)
                return

            referenced = self._referenced_image_paths()

            print("Cleaning up temporary image files...")
            for candidate in data_dir.iterdir():
                # Only remove PNG files at the data root; subfolders are long-lived.
                if candidate.is_dir() or candidate.suffix.lower() != '.png':
                    continue

                try:
                    if candidate.resolve() in referenced:
                        continue
                    candidate.unlink()
                    print(f"Removed temporary file: {candidate.name}")
                except Exception as e:
                    print(f"Error removing file {candidate.name}: {e}")

            print("Temporary image cleanup complete.")
        except Exception as e:
            print(f"Error during image cleanup: {e}")

async def setup(bot):
    await bot.add_cog(BossTimers(bot))