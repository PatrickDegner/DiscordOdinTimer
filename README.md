# Discord Odin Timer Bot

A Discord bot for managing and tracking boss spawn timers in the Odin game. Uses OCR (Optical Character Recognition) to automatically extract boss information from screenshots and maintains real-time countdown timers.

## Features

- Image-based boss registration via OCR
- Real-time timer management and intelligent update frequency
- Special boss alerts (configurable)
- Direct message support for private submissions

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

3. Configure `.env` with `BOT_TOKEN`, `BOSS_COMMAND_CHANNEL_ID`, and (on Windows) optionally `TESSERACT_PATH`.

4. Run the bot:

```bash
python main.py
```

## Usage

Use the `/boss` slash commands to add, list, and delete timers. Create boss timers by sending a screenshot to the bot via DM or by adding static events with `/boss add static`.

### Commands
- `/boss add static <name> <schedule> <time> <image_url>` — create a persistent fixed event
- `/boss list` — show upcoming timers
- `/boss delete <boss_name>` — delete timers by name

### Static event format
- `name`: a human-friendly event name, e.g. `Dragon Spawn`
- `schedule`: recurring days, for example:
  - `daily`
  - `weekdays`
  - `weekends`
  - `Tuesday and Thursday`
  - `Sunday`
- `time`: 24-hour time in `HH:MM` format, e.g. `19:30`
- `image_url`: a public URL pointing to the fixed event image

### Examples
- `/boss add static Dragon Saturday 20:00 https://example.com/dragon.png`
- `/boss add static ArenaBoss Tuesday and Thursday 18:15 https://example.com/arena.png`
- `/boss add static WeekendRaid weekends 12:00 https://example.com/raid.png`

## Notes
- The project no longer includes the optional fixed-schedule boss feature; timers are created via OCR or manual commands only.
- Special bosses can be configured in `config/config.json` under `SPECIAL_BOSSES_FOR_ALERT`.

## Project Structure

```
DiscordOdinTimer/
├── main.py
├── requirements.txt
├── .env
├── config/
│   └── config.json
├── data/
│   └── [temp images]/
├── cogs/
│   └── boss_timers.py
└── ocr.py
```
