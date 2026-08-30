"""Ad-hoc OCR checker for boss screenshots.

Usage:
    python tools/ocr_check.py                          # every image in tests/images/
    python tools/ocr_check.py shot.png other.jpg       # specific files
    python tools/ocr_check.py multi_boss.png            # splits and checks every detected card
    python tools/ocr_check.py --dir path/to/folder     # a whole folder
    python tools/ocr_check.py shot.png --raw           # also dump raw OCR text
    python tools/ocr_check.py --out other/folder       # write crops elsewhere

Cropped previews are written to tests/images/out by default so card splitting
and the 14% bottom crop can be eyeballed for different screenshot layouts.
"""
import argparse
import importlib.util
import inspect
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import ocr  # noqa: E402

IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp'}
DEFAULT_IMAGE_DIR = ROOT / 'tests' / 'images'
DEFAULT_CROP_DIR = DEFAULT_IMAGE_DIR / 'out'


def _load_cog_module():
    spec = importlib.util.spec_from_file_location('boss_timers_module', ROOT / 'cogs' / 'boss_timers.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_preview_cog(cog_module):
    """Bare cog instance used only to resolve the image the bot would pick."""
    instance = cog_module.BossTimers.__new__(cog_module.BossTimers)
    instance.boss_image_library_dir = ROOT / 'data' / 'boss_images'
    return instance


def _default_alert_seconds(cog_module) -> int:
    signature = inspect.signature(cog_module.BossTimers._register_ocr_timer)
    return signature.parameters['alert_seconds'].default


def _print_bot_preview(preview_cog, alert_seconds, boss_name, timestamp):
    """Prints the timer exactly as _register_ocr_timer would store it."""
    library_image = preview_cog._find_library_boss_image(boss_name)
    if library_image:
        stored_path = Path(library_image)
        try:
            stored_path = stored_path.relative_to(ROOT)
        except ValueError:
            pass
        image_note = f"{stored_path.as_posix()}  (reused from data/boss_images/)"
    else:
        sanitized = preview_cog._sanitize_filename(boss_name)
        image_note = f"data/cropped_screenshot_{sanitized}_{timestamp}.png  (cropped screenshot)"

    spawn_at = datetime.fromtimestamp(timestamp)
    alert_at = datetime.fromtimestamp(timestamp - alert_seconds)

    print("  bot would register:")
    print(f"    name:    {boss_name!r}")
    print(f"    spawns:  {spawn_at:%Y-%m-%d %H:%M:%S}  (<t:{timestamp}:F>)")
    print(f"    alert:   {alert_at:%Y-%m-%d %H:%M:%S}  ({alert_seconds // 60}m before)")
    print(f"    image:   {image_note}")


def _collect_images(args) -> list[Path]:
    if args.images:
        return [Path(item) for item in args.images]

    folder = Path(args.dir) if args.dir else DEFAULT_IMAGE_DIR
    if not folder.exists():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS)


def _format_duration(seconds: int) -> str:
    hours, remainder = divmod(max(seconds, 0), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('images', nargs='*', help='Image files to check.')
    parser.add_argument('--dir', help='Folder of images to check.')
    parser.add_argument('--raw', action='store_true', help='Print the raw OCR text for every image.')
    parser.add_argument('--out', default=str(DEFAULT_CROP_DIR), help='Folder for the cropped previews.')
    args = parser.parse_args()

    images = _collect_images(args)
    if not images:
        print("No images found. Put screenshots in tests/images/ or pass paths explicitly.")
        return 0

    cog_module = _load_cog_module()
    crop = cog_module.BossTimers._crop_image_for_timer
    preview_cog = _make_preview_cog(cog_module)
    alert_seconds = _default_alert_seconds(cog_module)

    crop_dir = Path(args.out)
    crop_dir.mkdir(parents=True, exist_ok=True)

    parsed_cards = 0
    ignored_cards = 0
    total_cards = 0
    for image_path in images:
        print(f"\n=== {image_path.name} ===")
        if not image_path.exists():
            print("  MISSING FILE")
            continue

        with Image.open(image_path) as image:
            print(f"  size: {image.width}x{image.height} (aspect {image.width / image.height:.2f})")
            cards = ocr.split_boss_cards(image.copy())

        total_cards += len(cards)
        if len(cards) > 1:
            print(f"  detected cards: {len(cards)}")

        for card_number, card in enumerate(cards, start=1):
            label = f"card {card_number}/{len(cards)}" if len(cards) > 1 else "card"
            print(f"\n  --- {label} ({card.width}x{card.height}) ---")

            cropped = crop(card)
            suffix = f"_card_{card_number}" if len(cards) > 1 else ""
            out_path = crop_dir / f"crop_{image_path.stem}{suffix}.png"
            cropped.convert('RGB').save(out_path)
            print(f"  crop: {cropped.width}x{cropped.height} (removed {card.height - cropped.height}px)")
            print(f"  crop written to {out_path}")

            started = time.perf_counter()
            message, timestamp, boss_name = ocr.parse_boss_info(card)
            elapsed = time.perf_counter() - started
            print(f"  ocr time: {elapsed:.2f}s")

            if timestamp is None:
                if ocr.is_ignored_ocr_result(message):
                    ignored_cards += 1
                    reason = message.removeprefix("IGNORED: ")
                    print(f"  RESULT: ignored ({reason})")
                    continue
                print("  RESULT: no timer parsed")
                print(f"  {message.splitlines()[0]}")
                if args.raw:
                    print(message)
                continue

            remaining = timestamp - int(time.time())
            parsed_cards += 1
            print(f"  RESULT: {boss_name} in {_format_duration(remaining)} ({remaining}s)")
            _print_bot_preview(preview_cog, alert_seconds, boss_name, timestamp)
            if args.raw:
                print(message)

    print(
        f"\nChecked {len(images)} image(s), detected {total_cards} card(s), "
        f"{parsed_cards} parsed, {ignored_cards} ignored, crops in {crop_dir}."
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
