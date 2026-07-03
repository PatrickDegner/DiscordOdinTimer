import discord
from discord.ext import commands, tasks
from discord import app_commands
import io
from PIL import Image
import json
import os
import asyncio
import time
from dotenv import load_dotenv
import aiohttp

# Load environment variables
load_dotenv()

# Load configuration from the parent directory
with open('config/config.json', 'r') as f:
    config = json.load(f)

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
        # Load the special bosses list from the config file
        self.SPECIAL_BOSSES_FOR_ALERT = config.get("SPECIAL_BOSSES_FOR_ALERT", [])
        
    @commands.Cog.listener()
    async def on_ready(self):
        print("BossTimers cog loaded.")
        await self.cleanup_temp_images()
        
    async def start_tasks(self):
        self.manage_boss_timers_task.start()
        self.special_boss_alert_task.start()
        
    def cog_unload(self):
        self.manage_boss_timers_task.cancel()
        self.special_boss_alert_task.cancel()

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

    @tasks.loop(seconds=60)
    async def manage_boss_timers_task(self):
        """Manages the single update message, changing content based on time until spawn."""
        await self.bot.wait_until_ready()
        update_channel = self.bot.get_channel(BOSS_COMMAND_CHANNEL_ID)

        if not update_channel:
            print("Update channel not found.")
            return

        async with self.UPDATE_MESSAGE_LOCKED:
            try:
                
                # Get the single message to edit
                if not self.update_message_id:
                    await self.cleanup_old_messages(update_channel, self.bot.user)
                    sent_message = await update_channel.send("Fetching next Event timer...")
                    self.update_message_id = sent_message.id
                
                message_to_edit = await update_channel.fetch_message(self.update_message_id)

                # Clean up old boss timers
                for ts in list(self.boss_timers.keys()):
                    if ts < time.time():
                        try:
                            old_image = self.boss_timers[ts].get('image')
                            if old_image and os.path.exists(old_image):
                                os.remove(old_image)
                            del self.boss_timers[ts]
                        except Exception as e:
                            print(f"Error cleaning up old image: {e}")

                if not self.boss_timers:
                    new_content = "There are no upcoming bosses scheduled."
                    await message_to_edit.edit(content=new_content, attachments=[])
                    return
                
                next_timestamp = min(self.boss_timers.keys())
                boss_data = self.boss_timers.get(next_timestamp)
                
                if not boss_data:
                    return

                time_until_spawn = next_timestamp - time.time()
                next_boss_name = boss_data['name']
                image_path = boss_data['image']

                message_content = f"🔥 The next Event is **{next_boss_name}**!\nStarts at <t:{next_timestamp}:F> which is <t:{next_timestamp}:R>."
                
                if os.path.exists(image_path):
                    discord_file = discord.File(image_path, filename=os.path.basename(image_path))
                    await message_to_edit.edit(content=message_content, attachments=[discord_file])
                    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Updated next Event message for {next_boss_name}.")
                else:
                    print("Image file not found for next Event, updating without image.")
                    await message_to_edit.edit(content=message_content, attachments=[])
            
            except discord.NotFound:
                print("Update message not found. Creating a new one.")
                sent_message = await update_channel.send("Fetching next Event timer...")
                self.update_message_id = sent_message.id
            except Exception as e:
                print(f"Error updating message: {e}")

        if self.boss_timers:
            next_timestamp = min(self.boss_timers.keys())
            time_until_spawn = next_timestamp - time.time()
            self.manage_boss_timers_task.change_interval(seconds=5 if time_until_spawn <= 300 else 60)
        else:
            self.manage_boss_timers_task.change_interval(seconds=60)

    @tasks.loop(seconds=5)
    async def special_boss_alert_task(self):
        """Sends a one-time, temporary alert for specific bosses with customized timing."""
        await self.bot.wait_until_ready()
        update_channel = self.bot.get_channel(BOSS_COMMAND_CHANNEL_ID)
        if not update_channel:
            return

        now = time.time()
        
        async with self.UPDATE_MESSAGE_LOCKED:
            sorted_bosses = sorted(self.boss_timers.items())
            
            for i, (timestamp, boss_data) in enumerate(sorted_bosses):
                boss_name = boss_data.get('name', '').strip()
                time_until_spawn = timestamp - now
                
                # Skip if alert already sent
                if boss_data.get('sent_alert', False):
                    continue
                
                alert_time = 0
                alert_mention = ""
                
                # Check if it's a fixed boss
                if boss_name in self.SPECIAL_BOSSES_FOR_ALERT:
                    alert_time = 600  # 10 minutes for special bosses
                    alert_mention = "@everyone"
                else:
                    alert_time = 300  # 5 minutes for regular bosses
                    alert_mention = "@here"
                
                if time_until_spawn <= alert_time and time_until_spawn > 0:
                    try:
                        alert_message_content = f"{alert_mention} **🔥 {boss_name}** starts in **{int(alert_time / 60)} minutes**! Get ready!"
                        
                        alert_message = await update_channel.send(alert_message_content)
                        print(f"Sent special alert for {boss_name}.")
                        
                        self.boss_timers[timestamp]['sent_alert'] = True
                        
                        await asyncio.sleep(120) # Wait 2 minute before deleting
                        await alert_message.delete()
                        print(f"Deleted special alert for {boss_name}.")
                        
                    except Exception as e:
                        print(f"Error sending/deleting special alert: {e}")

    

    # Create a command group for boss management
    boss_group = app_commands.Group(name="boss", description="Manage boss timers.")

    # `/boss add` removed — image scheduling is done via DM or external tools.

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
            if not self.boss_timers:
                await interaction.followup.send("There are no bosses to delete.", ephemeral=True)
                return

            keys_to_delete = []
            for timestamp, data in self.boss_timers.items():
                if data['name'].strip().lower() == boss_name.strip().lower():
                    keys_to_delete.append(timestamp)

            if not keys_to_delete:
                await interaction.followup.send(f"❌ Could not find an event named '{boss_name}'.", ephemeral=True)
                return

            for key in keys_to_delete:
                deleted_boss_data = self.boss_timers.pop(key, None)
                if deleted_boss_data:
                    image_path = deleted_boss_data.get('image')
                    if image_path and os.path.exists(image_path):
                        os.remove(image_path)

            await interaction.followup.send(f"✅ Successfully deleted {len(keys_to_delete)} timer(s) for '{boss_name}'.", ephemeral=True)

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
                    # Remove spaces from boss name for filename
                    sanitized_boss_name = boss_name.replace(' ', '_').replace('/', '_').replace('\\', '_')
                    unique_filename = f"data/cropped_screenshot_{sanitized_boss_name}_{future_timestamp}.png"
                    
                    crop_bottom_percentage = 0.14
                    cropped_height = int(img.height * (1 - crop_bottom_percentage))
                    cropped_image = img.crop((0, 0, img.width, cropped_height))
                    
                    async with self.UPDATE_MESSAGE_LOCKED:
                        cropped_image.save(unique_filename)
                        self.boss_timers[future_timestamp] = {'name': boss_name, 'image': unique_filename, 'sent_alert': False}

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