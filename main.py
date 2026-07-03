import discord
from discord.ext import commands
import json
import os
import asyncio
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Load configuration from a JSON file
with open('config/config.json', 'r') as f:
    config = json.load(f)

# Get bot token from environment variable
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError('BOT_TOKEN not found in .env file')

# Get channel ID from environment variable
BOSS_COMMAND_CHANNEL_ID = int(os.getenv('BOSS_COMMAND_CHANNEL_ID', 0))
if not BOSS_COMMAND_CHANNEL_ID:
    raise ValueError('BOSS_COMMAND_CHANNEL_ID must be set in .env file')

# Add to config for cogs to access
config['BOSS_COMMAND_CHANNEL_ID'] = BOSS_COMMAND_CHANNEL_ID

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

async def load_cogs():
    """Dynamically loads all cogs from the cogs directory."""
    for filename in os.listdir('./cogs'):
        if filename.endswith('.py'):
            try:
                await bot.load_extension(f'cogs.{filename[:-3]}')
                print(f"Loaded cog: {filename[:-3]}")
            except Exception as e:
                print(f"Failed to load cog {filename[:-3]}: {e}")

@bot.event
async def on_ready():
    """Event handler for when the bot successfully connects."""
    print(f'{bot.user.name} connected!')
    await bot.tree.sync() # Sync slash commands globally
    
    # Start background tasks from the cogs
    for cog_name, cog_instance in bot.cogs.items():
        if hasattr(cog_instance, 'start_tasks'):
            await cog_instance.start_tasks()

async def main():
    await load_cogs()
    try:
        async with bot:
            await bot.start(BOT_TOKEN)
    except OSError as exc:
        print("Network error connecting to Discord. Check your internet connection and DNS settings.")
        print(f"Details: {exc}")
    except discord.DiscordException as exc:
        print(f"Discord client error: {exc}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot shut down by user.")
    except Exception as e:
        print(f"An error occurred: {e}")