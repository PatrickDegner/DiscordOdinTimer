import asyncio
import json
import os
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent
ENV_FILE = ROOT_DIR / '.env'
CONFIG_FILE = ROOT_DIR / 'config' / 'config.json'

load_dotenv(dotenv_path=ENV_FILE)

with CONFIG_FILE.open('r', encoding='utf-8') as config_file:
    config = json.load(config_file)

BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError('BOT_TOKEN not found in .env file')

BOSS_COMMAND_CHANNEL_ID = int(os.getenv('BOSS_COMMAND_CHANNEL_ID', 0))
if not BOSS_COMMAND_CHANNEL_ID:
    raise ValueError('BOSS_COMMAND_CHANNEL_ID must be set in .env file')

config['BOSS_COMMAND_CHANNEL_ID'] = BOSS_COMMAND_CHANNEL_ID

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

async def load_cogs():
    """Dynamically loads all cogs from the cogs directory."""
    cog_dir = ROOT_DIR / 'cogs'
    for path in cog_dir.glob('*.py'):
        cog_name = path.stem
        try:
            await bot.load_extension(f'cogs.{cog_name}')
            print(f"Loaded cog: {cog_name}")
        except Exception as e:
            print(f"Failed to load cog {cog_name}: {e}")

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