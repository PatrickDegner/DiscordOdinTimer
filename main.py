import asyncio
import os
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent
ENV_FILE = ROOT_DIR / '.env'

load_dotenv(dotenv_path=ENV_FILE)

BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError('BOT_TOKEN not found in .env file')

BOSS_COMMAND_CHANNEL_ID = int(os.getenv('BOSS_COMMAND_CHANNEL_ID', 0))
if not BOSS_COMMAND_CHANNEL_ID:
    raise ValueError('BOSS_COMMAND_CHANNEL_ID must be set in .env file')

STARTUP_RETRY_DELAY_SECONDS = int(os.getenv('STARTUP_RETRY_DELAY_SECONDS', '2'))
MAX_STARTUP_RETRIES = int(os.getenv('MAX_STARTUP_RETRIES', '10'))

def create_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True

    bot = commands.Bot(command_prefix='!', intents=intents)

    @bot.event
    async def on_ready():
        """Event handler for when the bot successfully connects."""
        try:
            user_name = bot.user.name if bot.user else 'UnknownUser'
            print(f'{user_name} connected!')
            await bot.tree.sync()  # Sync slash commands globally

            # Start background tasks from the cogs
            for cog_instance in bot.cogs.values():
                if hasattr(cog_instance, 'start_tasks'):
                    await cog_instance.start_tasks()
        except Exception as exc:
            print(f"on_ready failed: {exc}")

    return bot

async def load_cogs(bot: commands.Bot):
    """Dynamically loads all cogs from the cogs directory."""
    cog_dir = ROOT_DIR / 'cogs'
    for path in cog_dir.glob('*.py'):
        cog_name = path.stem
        try:
            await bot.load_extension(f'cogs.{cog_name}')
            print(f"Loaded cog: {cog_name}")
        except Exception as e:
            print(f"Failed to load cog {cog_name}: {e}")

async def run_bot_once():
    bot = create_bot()
    await load_cogs(bot)
    async with bot:
        await bot.start(BOT_TOKEN)

async def main():
    attempt = 1
    while True:
        try:
            await run_bot_once()
            return
        except discord.LoginFailure as exc:
            print(f"Login failed. Check BOT_TOKEN in .env. Details: {exc}")
            return
        except OSError as exc:
            print("Network error connecting to Discord. Check your internet connection and DNS settings.")
            print(f"Details: {exc}")
        except discord.DiscordException as exc:
            print(f"Discord client error: {exc}")
        except Exception as exc:
            print(f"Unexpected startup/runtime error: {exc}")

        if MAX_STARTUP_RETRIES and attempt >= MAX_STARTUP_RETRIES:
            print(f"Reached MAX_STARTUP_RETRIES ({MAX_STARTUP_RETRIES}). Giving up.")
            return

        delay = min(STARTUP_RETRY_DELAY_SECONDS * (2 ** (attempt - 1)), 60)
        next_attempt = attempt + 1
        print(f"Retrying startup in {delay} seconds (attempt {next_attempt})...")
        await asyncio.sleep(delay)
        attempt = next_attempt

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot shut down by user.")
    except Exception as e:
        print(f"An error occurred: {e}")