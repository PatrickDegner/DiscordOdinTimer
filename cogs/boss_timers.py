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
from typing import Literal
from dotenv import load_dotenv
import aiohttp

# Load environment variables
load_dotenv()

# Load configuration from the parent directory when present
config_path = Path('config') / 'config.json'
try:
    with config_path.open('r', encoding='utf-8') as f:
        config = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    config = {}

# Get BOSS_COMMAND_CHANNEL_ID from environment
BOSS_COMMAND_CHANNEL_ID = int(os.getenv('BOSS_COMMAND_CHANNEL_ID', 0))

# Import OCR functions from the ocr directory
from ocr import parse_boss_info

class BossTimers(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.boss_timers = {}
        self.update_message_id = None
        self.UPDATE_MESSAGE_LOCKED = asyncio.Lock()
        self.static_events_file = Path('data') / 'static_events.json'
        self.static_image_dir = Path('data') / 'static_images'
        self.static_image_dir.mkdir(parents=True, exist_ok=True)
        self.static_events = {}
        self._load_static_events()
        self._schedule_all_static_events()

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
    def _crop_image_for_timer(image: Image.Image) -> Image.Image:
        crop_bottom_percentage = 0.14
        cropped_height = int(image.height * (1 - crop_bottom_percentage))
        return image.crop((0, 0, image.width, cropped_height))

    def _build_event_message_content(self, boss_name: str, timestamp: int, boss_data: dict | None) -> str:
        if boss_data is None:
            boss_data = {}

        content = f"🔥 The next Event is **{boss_name}**!\nStarts at <t:{timestamp}:F> which is <t:{timestamp}:R>."

        extra_informations = boss_data.get('extra_informations', boss_data.get('description', ''))
        if extra_informations:
            content += f"\n\n{extra_informations}"

        return content

    def _build_alert_message_content(self, boss_name: str, boss_data: dict | None) -> str:
        return f"@here The next event **{boss_name}** starts soon."

    def _should_alert_now(self, timestamp: int, boss_data: dict | None, now: float | None = None) -> bool:
        if boss_data is None:
            return False

        now_ts = now if now is not None else time.time()
        time_until_spawn = timestamp - now_ts
        alert_seconds = boss_data.get('alert_seconds', 300)
        return 0 < time_until_spawn <= alert_seconds

    def _should_send_alert(self, timestamp: int, boss_data: dict | None, now: float | None = None) -> bool:
        if boss_data is None:
            return False
        if boss_data.get('sent_alert', False):
            return False
        return self._should_alert_now(timestamp, boss_data, now=now)

    def _get_alert_candidates(self, now: float | None = None, timers: dict | None = None) -> list[tuple[int, dict]]:
        timers = timers if timers is not None else self.boss_timers
        now_ts = now if now is not None else time.time()
        candidates = []
        for timestamp, boss_data in timers.items():
            if self._should_alert_now(timestamp, boss_data, now=now_ts):
                candidates.append((timestamp, boss_data))
        return sorted(candidates, key=lambda item: item[0])

    @commands.Cog.listener()
    async def on_ready(self):
        print("BossTimers cog loaded.")
        await self.cleanup_temp_images()
        
    async def start_tasks(self):
        self.manage_boss_timers_task.start()
        
    async def _cleanup_expired_timers(self):
        now = time.time()
        for ts in list(self.boss_timers.keys()):
            if ts < now:
                try:
                    expired_timer = self.boss_timers.pop(ts, None)
                    if not expired_timer:
                        continue

                    if expired_timer.get('static_id'):
                        static_event = self.static_events.get(expired_timer['static_id'])
                        if static_event:
                            self._schedule_static_event(static_event, after=now)

                    if expired_timer.get('static_id') is None:
                        old_image = expired_timer.get('image')
                        if old_image and os.path.exists(old_image):
                            os.remove(old_image)
                except Exception as e:
                    print(f"Error cleaning up expired timer: {e}")

    def _get_next_timer(self):
        if not self.boss_timers:
            return None, None
        next_timestamp = min(self.boss_timers.keys())
        return next_timestamp, self.boss_timers.get(next_timestamp)

    def _sanitize_filename(self, name: str) -> str:
        cleaned = name.replace(' ', '_').replace('/', '_').replace('\\', '_')
        return ''.join(char for char in cleaned if char.isalnum() or char in ('_', '-'))

    def _ensure_data_dir(self):
        data_dir = Path('data')
        data_dir.mkdir(exist_ok=True)
        return data_dir

    def _load_static_events(self):
        try:
            if self.static_events_file.exists():
                with self.static_events_file.open('r', encoding='utf-8') as f:
                    events = json.load(f)
                    self.static_events = {event['id']: event for event in events}
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

    def _parse_schedule_days(self, schedule_text: str):
        normalized = schedule_text.strip().lower().replace('and', ',')
        if normalized in ('daily', 'everyday'):
            return list(range(7))
        if normalized in ('weekdays', 'monday-friday', 'mon-fri'):
            return [0, 1, 2, 3, 4]
        if normalized in ('weekends', 'saturday-sunday', 'sat-sun'):
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
                raise ValueError(f"Invalid schedule day: '{part}'. Use names like Tuesday or 'daily'.")

        if not days:
            raise ValueError("Schedule must include at least one weekday or 'daily'.")

        return sorted(set(days))

    def _parse_time(self, time_text: str):
        match = re.fullmatch(r'([01]?\d|2[0-3]):([0-5]\d)', time_text.strip())
        if not match:
            raise ValueError("Invalid time format. Use HH:MM in 24-hour time.")
        return int(match.group(1)), int(match.group(2))

    def _get_next_occurrence(self, event: dict, after: float | None = None):
        after_ts = after if after is not None else time.time()
        after_dt = datetime.fromtimestamp(after_ts)
        hour, minute = self._parse_time(event['time'])
        weekdays = self._parse_schedule_days(event['schedule'])

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
            candidate_ts = candidate_dt.timestamp()
            if candidate_ts > after_ts:
                return int(candidate_ts)

        raise ValueError("Could not find a next occurrence within the next two weeks.")

    def _schedule_static_event(self, event: dict, after: float | None = None):
        try:
            next_timestamp = self._get_next_occurrence(event, after=after)
        except ValueError as e:
            print(f"Static event scheduling failed for {event.get('name')}: {e}")
            return

        while next_timestamp in self.boss_timers:
            next_timestamp += 1

        self.boss_timers[next_timestamp] = {
            'name': event['name'],
            'image': event['image'],
            'sent_alert': False,
            'static_id': event['id'],
            'extra_informations': event.get('extra_informations', event.get('description', '')),
            'alert_seconds': event.get('alert_seconds', 300),
        }

    def _schedule_all_static_events(self):
        for event in self.static_events.values():
            self._schedule_static_event(event)

    async def _download_image_bytes(self, image_url: str) -> bytes:
        async with aiohttp.ClientSession() as session:
            async with session.get(image_url) as response:
                if response.status != 200:
                    raise ValueError(f"Image download failed with status {response.status}")
                return await response.read()

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

    async def _safe_edit_update_message(self, message, content: str, image_path: str | None = None):
        if image_path and os.path.exists(image_path):
            try:
                discord_file = discord.File(image_path, filename=os.path.basename(image_path))
                await message.edit(content=content, attachments=[discord_file])
                return
            except Exception as exc:
                print(f"Attachment-based update failed, retrying without attachment: {exc}")

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
                await self._cleanup_expired_timers()

                if not self.boss_timers:
                    await message_to_edit.edit(content="There are no upcoming bosses scheduled.", attachments=[])
                    return

                next_timestamp, boss_data = self._get_next_timer()
                if not boss_data:
                    return

                next_boss_name = boss_data['name']
                image_path = boss_data['image']
                message_content = self._build_event_message_content(next_boss_name, next_timestamp, boss_data)

                if image_path and os.path.exists(image_path):
                    await self._safe_edit_update_message(message_to_edit, message_content, image_path=image_path)
                else:
                    print("Image file not found for next Event, updating without image.")
                    await self._safe_edit_update_message(message_to_edit, message_content)

                alert_candidates = self._get_alert_candidates(now=time.time())
                for alert_timestamp, alert_boss_data in alert_candidates:
                    if alert_boss_data.get('sent_alert', False):
                        continue
                    if alert_timestamp != next_timestamp:
                        continue
                    try:
                        alert_boss_data['sent_alert'] = True
                        alert_message_content = self._build_alert_message_content(alert_boss_data['name'], alert_boss_data)
                        alert_message = await update_channel.send(alert_message_content)
                        await asyncio.sleep(30)
                        await alert_message.delete()
                    except Exception as exc:
                        print(f"Error sending temporary alert message: {exc}")

                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Updated next Event message for {next_boss_name}.")
            except discord.NotFound:
                print("Update message not found. Creating a new one.")
                message_to_edit = await self._get_or_create_update_message(update_channel)
            except Exception as e:
                print(f"Error updating message: {e}")

        self.manage_boss_timers_task.change_interval(seconds=15)

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
        extra_informations="Optional text to show beneath the event message.",
    )
    async def add_static_boss_command(
        self,
        interaction: discord.Interaction,
        name: str,
        schedule: str,
        time: str,
        image: discord.Attachment | None = None,
        alert_time: str | None = None,
        extra_informations: str | None = None,
    ):
        """Slash command to add a persistent static event."""
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
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        image_bytes = None
        if image is not None:
            try:
                image_bytes = await image.read()
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
            'image': str(filename),
            'alert_seconds': alert_seconds,
            'extra_informations': extra_informations or '',
        }

        self.static_events[event_id] = event
        self._save_static_events()
        self._schedule_static_event(event)

        await interaction.followup.send(
            f"✅ Static event '{name}' added for {schedule} at {hours:02d}:{minutes:02d} with alert timing {alert_seconds}s.",
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
        if interaction.channel_id != BOSS_COMMAND_CHANNEL_ID:
            await interaction.response.send_message(
                f"❌ Please use this command in the <#{BOSS_COMMAND_CHANNEL_ID}> channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        image_to_process = None
        if image is not None:
            try:
                image_bytes = await image.read()
                image_to_process = Image.open(io.BytesIO(image_bytes))
            except Exception as exc:
                await interaction.followup.send(f"Unable to read the uploaded image: {exc}", ephemeral=True)
                return
        else:
            try:
                clipboard_image = ImageGrab.grabclipboard()
                if not clipboard_image:
                    await interaction.followup.send("No image was found in the clipboard.", ephemeral=True)
                    return
                image_to_process = clipboard_image
            except Exception as exc:
                await interaction.followup.send(f"Unable to read the clipboard image: {exc}", ephemeral=True)
                return

        try:
            result_message, future_timestamp, parsed_boss_name = parse_boss_info(image_to_process)
            if future_timestamp is None:
                await interaction.followup.send(f"⚠️ {result_message}", ephemeral=True)
                return

            boss_name_to_use = parsed_boss_name
            if not boss_name_to_use:
                await interaction.followup.send("Could not determine a boss name from the OCR image.", ephemeral=True)
                return

            sanitized_boss_name = self._sanitize_filename(boss_name_to_use)
            data_dir = self._ensure_data_dir()
            unique_filename = data_dir / f"cropped_screenshot_{sanitized_boss_name}_{future_timestamp}.png"
            cropped_image = self._crop_image_for_timer(image_to_process)

            async with self.UPDATE_MESSAGE_LOCKED:
                cropped_image.save(unique_filename)
                self.boss_timers[future_timestamp] = {
                    'name': boss_name_to_use,
                    'image': str(unique_filename),
                    'sent_alert': False,
                    'alert_seconds': 300,
                    'extra_informations': '',
                }

            await interaction.followup.send(content=result_message, file=discord.File(unique_filename))
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
    async def delete_boss_command(self, interaction: discord.Interaction, boss_name: str):
        """Slash command to delete boss timers by name."""
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        async with self.UPDATE_MESSAGE_LOCKED:
            if not self.boss_timers and not self.static_events:
                await interaction.followup.send("There are no bosses to delete.", ephemeral=True)
                return

            static_ids_to_remove = []
            for event_id, event in self.static_events.items():
                if event['name'].strip().lower() == boss_name.strip().lower():
                    static_ids_to_remove.append(event_id)

            deleted_timers = 0
            for key in list(self.boss_timers.keys()):
                data = self.boss_timers[key]
                if data['name'].strip().lower() == boss_name.strip().lower():
                    self.boss_timers.pop(key, None)
                    deleted_timers += 1

            for event_id in static_ids_to_remove:
                event = self.static_events.pop(event_id, None)
                if event:
                    image_path = event.get('image')
                    if image_path and os.path.exists(image_path):
                        os.remove(image_path)
                    deleted_timers += 1

            if static_ids_to_remove:
                self._save_static_events()

            if deleted_timers == 0:
                await interaction.followup.send(f"❌ Could not find an event named '{boss_name}'.", ephemeral=True)
                return

            await interaction.followup.send(f"✅ Successfully deleted {deleted_timers} timer(s) for '{boss_name}'.", ephemeral=True)

    # Skipping fixed-schedule bosses has been removed; command removed.

    @commands.Cog.listener()
    async def on_message(self, message):
        """Handles image processing for messages sent via DM."""
        if message.author == self.bot.user or not isinstance(message.channel, discord.DMChannel) or not message.attachments:
            return

        image_attachment = message.attachments[0]
        if image_attachment.content_type and image_attachment.content_type.startswith('image/'):
            await message.channel.send("Processing your image, please wait...")
            try:
                image_bytes = await image_attachment.read()
                img = Image.open(io.BytesIO(image_bytes))
                result_message, future_timestamp, boss_name = parse_boss_info(img)

                if future_timestamp is not None:
                    sanitized_boss_name = self._sanitize_filename(boss_name)
                    data_dir = self._ensure_data_dir()
                    unique_filename = data_dir / f"cropped_screenshot_{sanitized_boss_name}_{future_timestamp}.png"
                    cropped_image = self._crop_image_for_timer(img)

                    async with self.UPDATE_MESSAGE_LOCKED:
                        cropped_image.save(unique_filename)
                        self.boss_timers[future_timestamp] = {
                            'name': boss_name,
                            'image': str(unique_filename),
                            'sent_alert': False,
                            'alert_seconds': 300,
                            'description': '',
                        }

                    await message.channel.send(content=result_message, file=discord.File(unique_filename))
                else:
                    await message.channel.send(f"⚠️ {result_message}")

            except (aiohttp.ClientConnectorError, OSError) as e:
                await message.channel.send("An unexpected network error occurred while fetching the image. Please try again.")
            except Exception as e:
                await message.channel.send(f"An unexpected error occurred: {e}")

    # Fixed-schedule helpers and timers fully removed; timers are created via OCR or manual commands only.

    @staticmethod
    async def cleanup_temp_images():
        """Cleans up temporary PNG files from /data directory."""
        try:
            # Ensure data directory exists
            if not os.path.exists('data'):
                os.makedirs('data')
                return

            print("Cleaning up temporary image files...")
            for filename in os.listdir('data'):
                filepath = os.path.join('data', filename)
                # Skip directories; only remove PNG files at data root
                if os.path.isdir(filepath) or not filename.endswith('.png'):
                    continue
                    
                try:
                    os.remove(filepath)
                    print(f"Removed temporary file: {filename}")
                except Exception as e:
                    print(f"Error removing file {filename}: {e}")
                    
            print("Temporary image cleanup complete.")
        except Exception as e:
            print(f"Error during image cleanup: {e}")

async def setup(bot):
    await bot.add_cog(BossTimers(bot))