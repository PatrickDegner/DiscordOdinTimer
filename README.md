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

2. Install Tesseract OCR and set `TESSERACT_PATH` in your `.env` if on Windows.

3. Configure `.env` with `BOT_TOKEN` and `BOSS_COMMAND_CHANNEL_ID`.

4. Run the bot:

```bash
python main.py
```

## Usage

Use the `/boss` slash commands to add, list, and delete timers. Upload a screenshot via `/boss add` or send one to the bot via DM.

### Commands
- `/boss add <image>` — create a timer from a screenshot
- `/boss list` — show upcoming timers
- `/boss delete <boss_name>` — delete timers by name

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
