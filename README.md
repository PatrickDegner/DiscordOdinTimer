# Discord Odin Timer Bot

A Discord bot for managing and tracking boss spawn timers in the Odin game. Uses OCR (Optical Character Recognition) to automatically extract boss information from screenshots and maintains real-time countdown timers.

## Features

- Image-based boss registration via OCR with a confirmation step before the timer is created
- Supports both DM image submissions and slash commands
- Static recurring events with custom schedule and alert windows
- One-time manual events with custom date, time, image, and alert settings
- Editing of scheduled timers without deleting and re-adding them
- Active timers survive a bot restart
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

3. Configure `.env` with `BOT_TOKEN`, `BOSS_COMMAND_CHANNEL_ID`, `ALLOWED_BOSS_MANAGER_ROLE_ID`, `TIMEZONE`, and (on Windows) optionally `TESSERACT_PATH`.

Example:

```env
BOT_TOKEN=your_bot_token_here
BOSS_COMMAND_CHANNEL_ID=1521521777963044934
ALLOWED_BOSS_MANAGER_ROLE_ID=1522906832492822688,1522906832492822689
TIMEZONE=Europe/Berlin
TESSERACT_PATH=C:\Program Files\Tesseract-OCR\tesseract.exe
```

`TIMEZONE` is the IANA name of your guild's timezone and defines what `20:00` means in `/boss add static`, `/boss add onetime`, and `/boss edit`. If it is omitted the bot falls back to the host machine's local time, which means moving the bot to a different server would silently shift every event. Setting it explicitly is recommended.

Startup retry settings are optional. If omitted, the defaults are:

```env
STARTUP_RETRY_DELAY_SECONDS=2
MAX_STARTUP_RETRIES=10
```

4. Run the bot:

```bash
python main.py
```

## Usage

Use the `/boss` slash commands to add, list, and delete timers.

You can create timers in three ways:
- Send a screenshot to the bot in DM (OCR extracts boss and remaining time, then asks you to confirm)
- Use `/boss add normal` with an image attachment
- Use `/boss add static` for recurring events
- Use `/boss add onetime` for a fixed date and time event that runs once

### Commands
- `/boss add normal <image>` - create a one-time timer from OCR
- `/boss add static <name> <schedule> <time> [image] [alert_time] [alert_mention] [extra_informations]` - create a recurring event
- `/boss add onetime <name> <date> <time> [image] [alert_time] [alert_mention] [extra_informations]` - create a fixed one-time event
- `/boss list` — show upcoming timers
- `/boss edit <boss_name> [new_time] [alert_time] [alert_mention] [extra_informations] [timezone]` — change the next scheduled timer for a boss
- `/boss delete <boss_name>` — delete timers by name, after a confirmation prompt

`boss_name` has autocomplete on both `/boss edit` and `/boss delete`, suggesting the names of currently scheduled timers and saved static events, so there is no need to type long names by hand.

### Command permissions
- `/boss list` can be used by everyone.
- `/boss add normal`, `/boss add static`, `/boss add onetime`, `/boss edit`, and `/boss delete` require one of the roles whose IDs are set in `.env` as `ALLOWED_BOSS_MANAGER_ROLE_ID`.
- Multiple roles are supported: separate the IDs with commas (spaces and semicolons also work). Having any one of them is enough.
- Example single role: `ALLOWED_BOSS_MANAGER_ROLE_ID=1522906832492822688`
- Example multiple roles: `ALLOWED_BOSS_MANAGER_ROLE_ID=1522906832492822688,1522906832492822689`
- `ALLOWED_BOSS_MANAGER_ROLE_IDS` can be used instead as an alias; if both are set, `ALLOWED_BOSS_MANAGER_ROLE_IDS` wins.
- If no valid role ID is configured, add/edit/delete commands are blocked for everyone.

### OCR flow

When you DM a screenshot to the bot or use `/boss add normal`, the bot does not create the timer immediately. It replies with a preview showing the detected boss name, the resulting spawn time, and the cropped screenshot, plus three buttons:

- **Confirm** — creates the timer.
- **Correct time** — opens a dialog where you type the real remaining time (`1h30m`, `45m`, or `90` for minutes) if the OCR misread it.
- **Cancel** — discards the result.

Only the person who uploaded the image can use the buttons. The preview expires after 3 minutes, and no file is written to disk unless you confirm.

Screenshots containing a horizontal row of bordered portrait boss cards or a single-row/multi-row mobile card layout are split automatically. Empty grid cells are skipped, and each readable card gets its own numbered preview and confirmation controls. Cards whose status is `Spawning` are ignored, while other unreadable cards are reported without blocking the successful cards.

Before showing confirmations, the bot compares each OCR boss name with future timers already registered. Bosses that already have an active timer are listed as skipped, and only newly timed bosses receive confirmation controls. This means a boss previously ignored as `Spawning` is offered normally once a later screenshot shows a timer. Matching ignores capitalization and repeated surrounding whitespace; expired timers do not block a new entry.

If a matching file exists in `data/boss_images/`, the preview shows **that** image rather than the screenshot, so what you see is what gets posted.

The screenshot is read in two passes: a layout pass that finds the `Domain Ruler` label, the boss name, and the timer row, then a digits-only pass over just the timer words. The second pass restricts Tesseract to the characters `0123456789hms`, so letter look-alikes such as `ih`/`Ih`/`th` for `1h` cannot be produced. Timer formats like `6m 52s left`, `1h 4m left`, and `14h 23m left` are all supported.

A parsed time of `0` or anything above 24 hours is rejected as a misread. When parsing fails, the reply includes the raw OCR text so you can see what the engine actually read.

### Editing a timer

`/boss edit` changes the **soonest** scheduled timer whose name matches. Provide at least one field to change:

- `new_time`: a duration from now (`1h30m`, `45m`, `90` = 90 minutes), a clock time (`20:00`, rolls to tomorrow if it already passed today), or an absolute `YYYY-MM-DD HH:MM`
- `alert_time`: new alert lead time between 60 and 3600 seconds (`5m`, `15m`, `600`)
- `alert_mention`: new mention such as `@role`, `@everyone`, or `LW`
- `extra_informations`: replacement text shown under the event message
- `timezone`: optional IANA timezone `new_time` is given in. Defaults to `TIMEZONE` from `.env`

If the timer is moved back outside its alert window, the alert is re-armed and will fire again. Editing an occurrence of a static event only affects that occurrence; the next one follows the saved schedule again.

### Deleting a timer

`/boss delete` does not delete anything immediately. It first shows what would be removed — how many timers and how many saved static/one-time events, with the spawn time of each timer — and waits for you to press **Delete** or **Cancel**. The prompt expires after 60 seconds without deleting anything.

Deleting a static or one-time event removes its saved definition, so it stops recurring. Images reused from `data/boss_images/` are never deleted.

### Boss image library for /boss add normal
- Put reusable boss images in `data/boss_images/`.
- Name files after the boss, for example: `bjorn.jpg`, `chaos_priest.jpg`.
- When `/boss add normal` parses a boss name via OCR, the bot first checks `data/boss_images/` for a matching file name (case-insensitive, supports `.png`, `.jpg`, `.jpeg`, `.webp`).
- If a match exists, that image is used for the event.
- If no match exists, the bot falls back to the cropped OCR screenshot image.

### Timezones

All absolute times (`/boss add static`, `/boss add onetime`, and `/boss edit` with a clock time or date) are interpreted in the timezone set by `TIMEZONE` in `.env`. Durations like `1h30m` and OCR timers are relative, so they are unaffected.

Someone in a different timezone can enter their **own** local time by passing the optional `timezone` option, which has autocomplete over all IANA zone names:

```
/boss add onetime name: Raid date: tomorrow time: 20:00 timezone: America/New_York
```

With `TIMEZONE=Europe/Berlin`, that schedules 20:00 New York time, which is 02:00 the following day in Berlin. Without the option it would have scheduled 20:00 Berlin time, i.e. 14:00 in New York.

The `timezone` option also controls what `today` and `tomorrow` mean, so a US user entering `today` at 19:00 their time gets their own date rather than the German one.

Every confirmation reply names the timezone used and includes a Discord timestamp, which each member sees rendered in their own local time:

```
✅ One-time event 'Raid' added for 2026-08-29 at 20:00 (America/New_York) with alert timing 300s and mention @here.
Starts <t:1788048000:F> which is <t:1788048000:R>.
```

For recurring static events the chosen timezone is stored with the event, so every future occurrence follows that zone's clock, including its daylight saving changes.

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
- `timezone`: optional IANA timezone the `time` is given in, e.g. `America/New_York`. Defaults to `TIMEZONE` from `.env`

### One-time event format
- `name`: a human-friendly event name, e.g. `Castle Push`
- `date`: one of these formats:
  - `today`
  - `tomorrow`
  - `YYYY-MM-DD` for example `2026-07-12`
  - `DD.MM.YYYY` for example `12.07.2026`
  - `DD/MM/YYYY` for example `12/07/2026`
- `time`: 24-hour time in `HH:MM` format, e.g. `19:30`
- `image`: optional uploaded image; if omitted, clipboard image is used
- `alert_time`: optional alert timing between 60 and 3600 seconds (examples: `5m`, `15m`, `60`)
- `alert_mention`: optional text mention for alerts like `@role`, `@everyone`, or `LW`
- `extra_informations`: optional text shown under the event message
- `timezone`: optional IANA timezone the `date` and `time` are given in, e.g. `America/New_York`. Defaults to `TIMEZONE` from `.env`
- The command rejects dates/times that are already in the past.

### Examples
- `/boss add normal` (attach a screenshot)
- `/boss add static Dragon Saturday 20:00`
- `/boss add static Dragon Saturday 20:00` + `alert_mention: @everyone`
- `/boss add static Dragon Saturday 20:00` + `alert_mention: @LW`
- `/boss add static Dragon Saturday 20:00` + `alert_mention: LW`
- `/boss add static ArenaBoss "Tuesday and Thursday" 18:15`
- `/boss add static WeekendRaid weekends 12:00` + image + `alert_time: 15m`
- `/boss add onetime CastlePush 2026-07-12 20:00`
- `/boss add onetime CastlePush today 20:00`
- `/boss add onetime CastlePush tomorrow 20:00`
- `/boss add onetime CastlePush 12.07.2026 20:00` + `alert_mention: @everyone`
- `/boss add onetime CastlePush 12/07/2026 20:00` + image + `alert_time: 10m`
- `/boss add onetime CastlePush tomorrow 20:00` + `timezone: America/New_York`
- `/boss edit Megir` + `new_time: 1h30m`
- `/boss edit Megir` + `new_time: 20:00` + `alert_time: 15m`
- `/boss edit Megir` + `new_time: 2026-07-12 20:00`
- `/boss edit Megir` + `alert_mention: @everyone`

## Testing

Run the unit tests (no Tesseract installation required, both OCR passes are stubbed):

```bash
python -m pytest
```

On Windows, use the virtual environment interpreter directly:

```powershell
.\venv\Scripts\python.exe -m pytest
```

### Checking real screenshots

`tools/ocr_check.py` runs the real OCR pipeline against screenshots so you can verify a new image before feeding it to the bot:

```bash
python tools/ocr_check.py                       # every image in tests/images/
python tools/ocr_check.py shot.png other.jpg    # specific files
python tools/ocr_check.py --dir path/to/folder  # a whole folder
python tools/ocr_check.py shot.png --raw        # also dump the raw OCR text
python tools/ocr_check.py --out other/folder    # write crops elsewhere
```

For every image it prints the source size and aspect ratio, how many pixels the crop removed, the OCR runtime, the parsed result, and exactly what the bot would register:

```
=== Megir.png ===
  size: 160x376 (aspect 0.43)
  crop: 160x323 (removed 53px)
  crop written to tests/images/out/crop_Megir.png
  ocr time: 0.22s
  RESULT: Megir in 17h44m00s (63840s)
  bot would register:
    name:    'Megir'
    spawns:  2026-08-28 19:20:08  (<t:1787937608:F>)
    alert:   2026-08-28 19:10:08  (10m before)
    image:   data/cropped_screenshot_Megir_1787937608.png  (cropped screenshot)
```

Cropped previews are always written to `tests/images/out/` so the crop can be inspected for different screenshot aspect ratios. The `tests/images/` folder is git-ignored, so your own screenshots stay local.

## Notes
- Active timers are saved to `data/timers.json` and restored on startup, so a restart does not lose OCR or manual timers. Static event occurrences are rebuilt from `data/static_events.json` instead.
- One-time OCR timer screenshots are automatically deleted when the event expires or is manually deleted.
- Static event images are stored in `data/static_images/` and are deleted when the static event is deleted.
- One-time event images are stored in `data/static_images/` and are deleted automatically after the event fires or if the event is deleted.
- Boss library images in `data/boss_images/` are never auto-deleted by the bot.
- On startup, the bot removes leftover temporary PNG files from `data/`, except those still referenced by a restored timer.

## Project Structure

```
DiscordOdinTimer/
├── main.py
├── ocr.py
├── requirements.txt
├── pytest.ini
├── .env
├── cogs/
│   └── boss_timers.py
├── data/
│   ├── static_events.json
│   ├── timers.json
│   ├── static_images/
│   ├── boss_images/
│   └── [temporary cropped screenshots]
├── tests/
│   ├── conftest.py
│   ├── helpers.py
│   ├── test_boss_timers.py
│   ├── test_ocr.py
│   ├── test_timer_management.py
│   └── images/            (git-ignored local screenshots)
└── tools/
    └── ocr_check.py
```
