# Discord Odin Timer Bot

A Discord bot for managing and tracking boss spawn timers in the Odin game. Uses OCR (Optical Character Recognition) to automatically extract boss information from screenshots and maintains real-time countdown timers.

## Features

- Image-based boss registration via OCR
- Supports both DM image submissions and slash commands
- Static recurring events with custom schedule and alert windows
- Real-time timer management and intelligent update frequency
- Automatic cleanup

## Installation

1. Install Python dependencies:

```bash
pip install -r requirements.txt
```

2. Install Tesseract OCR.

   - On Ubuntu / Debian:

   ```bash
   sudo apt update
   sudo apt install -y tesseract-ocr libtesseract-dev
   ```

   - On Fedora / CentOS (dnf):

   ```bash
   sudo dnf install -y tesseract
   ```

   - On Windows:

     1. Download the Tesseract installer from: https://github.com/tesseract-ocr/tesseract/releases
     2. Run the installer (default path is usually `C:\Program Files\Tesseract-OCR\tesseract.exe`).
     3. Add `TESSERACT_PATH` to your `.env` pointing to the `tesseract.exe` full path (see next step).

   Verify installation:

   ```bash
   tesseract --version
   ```

3. Configure `.env` with `BOT_TOKEN`, `BOSS_COMMAND_CHANNEL_ID`, `ALLOWED_BOSS_MANAGER_ROLE_ID`, and (on Windows) optionally `TESSERACT_PATH`.

Example:

```env
BOT_TOKEN=your_bot_token_here
BOSS_COMMAND_CHANNEL_ID=1521521777963044934
ALLOWED_BOSS_MANAGER_ROLE_ID=1522906832492822688
TESSERACT_PATH=C:\Program Files\Tesseract-OCR\tesseract.exe
```

Startup retry settings are optional. If omitted, the defaults are:

```env
STARTUP_RETRY_DELAY_SECONDS=2
MAX_STARTUP_RETRIES=3
```

4. Run the bot:

```bash
python main.py
```

## Usage

Use the `/boss` slash commands to add, list, and delete timers.

You can create timers in three ways:
- Send a screenshot to the bot in DM (OCR extracts boss and remaining time)
- Use `/boss add normal` with an image attachment
- Use `/boss add static` for recurring events

### Commands
- `/boss add normal <image>` - create a one-time timer from OCR
- `/boss add static <name> <schedule> <time> [image] [alert_time] [alert_mention] [extra_informations]` - create a recurring event
- `/boss list` — show upcoming timers
- `/boss delete <boss_name>` — delete timers by name

### Command permissions
- `/boss list` can be used by everyone.
- `/boss add normal`, `/boss add static`, and `/boss delete` require the role whose ID is set in `.env` as `ALLOWED_BOSS_MANAGER_ROLE_ID`.
- Example: `ALLOWED_BOSS_MANAGER_ROLE_ID=1522906832492822688`
- If `ALLOWED_BOSS_MANAGER_ROLE_ID` is missing or set to `0`, add/delete commands are blocked for everyone.

### Boss image library for /boss add normal
- Put reusable boss images in `data/boss_images/`.
- Name files after the boss, for example: `bjorn.jpg`, `chaos_priest.jpg`.
- When `/boss add normal` parses a boss name via OCR, the bot first checks `data/boss_images/` for a matching file name (case-insensitive, supports `.png`, `.jpg`, `.jpeg`, `.webp`).
- If a match exists, that image is used for the event.
- If no match exists, the bot falls back to the cropped OCR screenshot image.

### Static event format
- `name`: a human-friendly event name, e.g. `Dragon Spawn`
- `schedule`: recurring days, for example:
  - `daily`
  - `weekdays`
  - `weekends`
  - `Tuesday and Thursday`
  - `Sunday`
- `time`: 24-hour time in `HH:MM` format, e.g. `19:30`
- `image`: optional uploaded image; if omitted, clipboard image is used
- `alert_time`: optional alert timing between 60 and 3600 seconds (examples: `5m`, `15m`, `60`)
- `alert_mention`: optional text mention for alerts like `@role`, `@everyone`, or `LW`
- `extra_informations`: optional text shown under the event message

### Examples
- `/boss add normal` (attach a screenshot)
- `/boss add static Dragon Saturday 20:00`
- `/boss add static Dragon Saturday 20:00` + `alert_mention: @everyone`
- `/boss add static Dragon Saturday 20:00` + `alert_mention: @LW`
- `/boss add static Dragon Saturday 20:00` + `alert_mention: LW`
- `/boss add static ArenaBoss "Tuesday and Thursday" 18:15`
- `/boss add static WeekendRaid weekends 12:00` + image + `alert_time: 15m`

## Notes
- One-time OCR timer screenshots are automatically deleted when the event expires or is manually deleted.
- Static event images are stored in `data/static_images/` and are deleted when the static event is deleted.
- Boss library images in `data/boss_images/` are never auto-deleted by the bot.
- On startup, the bot also removes leftover temporary PNG files from `data/`.

## Project Structure

```
DiscordOdinTimer/
├── main.py
├── requirements.txt
├── .env
├── data/
│   ├── static_events.json
│   ├── static_images/
│   ├── boss_images/
│   └── [temporary cropped screenshots]
├── cogs/
│   └── boss_timers.py
└── ocr.py
```
