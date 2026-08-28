import os
import pytesseract
from PIL import Image, ImageGrab
import cv2
import numpy as np
import re
import time
from pathlib import Path

from dotenv import load_dotenv

# Load .env here too so TESSERACT_PATH resolves regardless of import order.
load_dotenv(dotenv_path=Path(__file__).resolve().parent / '.env')

# Configure Tesseract executable path from environment if provided
tess_path = os.getenv('TESSERACT_PATH')
if tess_path:
    pytesseract.pytesseract.tesseract_cmd = tess_path

# Plausibility bounds for a parsed respawn timer.
MIN_TIMER_SECONDS = 1
MAX_TIMER_SECONDS = 24 * 60 * 60

_DEBUG_TEXT_LIMIT = 400

# Pass 1 reads the layout with a normal alphabet so the boss name stays readable.
LAYOUT_CONFIG = r'--oem 1 --psm 6 -l eng'
# Pass 2 re-reads only the timer words. The whitelist makes letter look-alikes
# (ih/Ih/th for 1h, S for 5, O for 0) impossible instead of patching them afterwards.
TIMER_CONFIG = r'--oem 1 --psm 7 -l eng -c tessedit_char_whitelist=0123456789hms'

_UNIT_SECONDS = {'h': 3600, 'm': 60, 's': 1}

# Applied only to the numeric part of a timer token, never to the boss name.
_DIGIT_FIX = str.maketrans({
    'i': '1', 'I': '1', 'l': '1', 'L': '1', 't': '1', 'T': '1', '|': '1', '!': '1',
    'o': '0', 'O': '0', 'q': '0', 'Q': '0', 'D': '0',
    's': '5', 'S': '5', 'z': '2', 'Z': '2', 'b': '6', 'B': '8', 'g': '9', 'G': '6',
})

# Two characters max: hours <= 24, minutes and seconds <= 59.
_TIME_TOKEN_PATTERN = re.compile(r'([0-9iIlL|!tToOqQsSzZbBgG]{1,2})\s*([hms])')


def ocr_debug_snippet(text: str | None, limit: int = _DEBUG_TEXT_LIMIT) -> str:
    """Formats raw OCR output for display in a Discord message."""
    cleaned = re.sub(r'\n{2,}', '\n', (text or '').strip()).replace('`', "'")
    if not cleaned:
        return "\nRaw OCR text: (empty)"
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + '...'
    return f"\nRaw OCR text:\n```\n{cleaned}\n```"


def preprocess_image(image: Image.Image) -> Image.Image:
    """
    Applies image processing steps to improve OCR accuracy.
    Upscales, converts to grayscale, and applies Otsu's binarization.
    """
    img_upscaled = image.resize((image.width * 4, image.height * 4), Image.LANCZOS)

    img_np = np.array(img_upscaled)

    if len(img_np.shape) == 3 and img_np.shape[2] == 4:  # RGBA
        img_gray = cv2.cvtColor(img_np, cv2.COLOR_RGBA2GRAY)
    elif len(img_np.shape) == 3 and img_np.shape[2] == 3:  # RGB
        img_gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
    else:
        img_gray = img_np

    _, img_bw = cv2.threshold(img_gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    img_pil_bw = Image.fromarray(img_bw)
    return img_pil_bw


def validate_remaining_seconds(total_seconds: int) -> str | None:
    """Returns an error message when a parsed duration is not plausible, else None."""
    if total_seconds < MIN_TIMER_SECONDS:
        return (
            "The remaining time was read as 0. The timer text was probably misread "
            "or the boss is already spawning."
        )
    if total_seconds > MAX_TIMER_SECONDS:
        hours = total_seconds / 3600
        return (
            f"The remaining time was read as {hours:.1f} hours, which is above the "
            f"{MAX_TIMER_SECONDS // 3600}h limit. This is almost certainly a misread."
        )
    return None


def parse_remaining_seconds(text: str | None) -> int | None:
    """Sums the h/m/s tokens of a timer string like '1h 4m' or '6m 52s'."""
    if not text:
        return None

    total = 0
    seen_units = set()
    for value, unit in _TIME_TOKEN_PATTERN.findall(text):
        if unit in seen_units:
            continue
        digits = value.translate(_DIGIT_FIX)
        if not digits.isdigit():
            continue
        seen_units.add(unit)
        total += int(digits) * _UNIT_SECONDS[unit]

    return total if seen_units else None


def _is_time_word(text: str) -> bool:
    """True for words like '17h' or '44m', false for the clock icon and the 'left' label."""
    cleaned = text.strip()
    if not cleaned:
        return False
    if not any(char.isalnum() for char in cleaned):
        return False
    # 'left' and other labels are long and purely alphabetic; misread digits are not.
    return not (cleaned.isalpha() and len(cleaned) >= 3)


def _group_text_lines(data: dict) -> list[dict]:
    """Groups image_to_data output into reading-order lines with word boxes."""
    grouped = {}
    for index, raw_text in enumerate(data.get('text', [])):
        text = (raw_text or '').strip()
        if not text:
            continue
        try:
            confidence = float(data['conf'][index])
        except (KeyError, IndexError, TypeError, ValueError):
            confidence = -1.0
        if confidence < 0:
            continue

        key = (data['block_num'][index], data['par_num'][index], data['line_num'][index])
        left, top = int(data['left'][index]), int(data['top'][index])
        box = {
            'text': text,
            'left': left,
            'top': top,
            'right': left + int(data['width'][index]),
            'bottom': top + int(data['height'][index]),
        }
        grouped.setdefault(key, []).append(box)

    lines = []
    for key in sorted(grouped):
        boxes = grouped[key]
        lines.append({
            'text': ' '.join(box['text'] for box in boxes),
            'words': [box['text'] for box in boxes],
            'boxes': boxes,
        })
    return lines


def _find_ruler_line_index(lines: list[dict]) -> int | None:
    for index, line in enumerate(lines):
        lowered = [word.lower().strip('.,:;') for word in line['words']]
        if 'domain' in lowered and 'ruler' in lowered:
            return index
    return None


def _find_timer_line_index(lines: list[dict], after: int) -> int | None:
    """Returns the last line below the label that contains a duration token."""
    for index in range(len(lines) - 1, after, -1):
        if _TIME_TOKEN_PATTERN.search(lines[index]['text']):
            return index
    return None


def _extract_boss_name(lines: list[dict], ruler_index: int, timer_index: int) -> str | None:
    ruler_line = lines[ruler_index]
    lowered = [word.lower().strip('.,:;') for word in ruler_line['words']]
    trailing = ruler_line['words'][lowered.index('ruler') + 1:]

    if trailing:
        candidate = ' '.join(trailing)
    elif ruler_index + 1 < len(lines) and ruler_index + 1 != timer_index:
        candidate = lines[ruler_index + 1]['text']
    else:
        return None

    cleaned = re.sub(r'[^A-Za-z\s-]', '', candidate)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' -')
    return cleaned or None


def _read_timer_region(processed_img: Image.Image, line: dict, padding: int = 6) -> str:
    """Re-reads only the timer words with the digits-only alphabet."""
    boxes = [box for box in line['boxes'] if _is_time_word(box['text'])]
    if not boxes:
        return ''

    left = max(min(box['left'] for box in boxes) - padding, 0)
    top = max(min(box['top'] for box in boxes) - padding, 0)
    right = min(max(box['right'] for box in boxes) + padding, processed_img.width)
    bottom = min(max(box['bottom'] for box in boxes) + padding, processed_img.height)
    if right <= left or bottom <= top:
        return ''

    region = processed_img.crop((left, top, right, bottom))
    return pytesseract.image_to_string(region, config=TIMER_CONFIG)


def parse_boss_info(image_source):
    """
    Reads a boss card in two passes: a layout pass for the name and a
    digits-only pass for the timer row.
    Returns: (discord_message, future_timestamp, boss_name)
    """
    try:
        if not isinstance(image_source, Image.Image):
            return "ERROR: Invalid image source provided. Expected a PIL Image object.", None, None

        processed_img = preprocess_image(image_source)

        data = pytesseract.image_to_data(
            processed_img,
            config=LAYOUT_CONFIG,
            output_type=pytesseract.Output.DICT,
        )
        lines = _group_text_lines(data)
        full_text = '\n'.join(line['text'] for line in lines)

        if not full_text.strip():
            return "ERROR: No text was extracted from the image by Tesseract OCR.", None, None

        ruler_index = _find_ruler_line_index(lines)
        if ruler_index is None:
            return (
                "Could not find a 'Domain Ruler' label in the extracted text."
                f"{ocr_debug_snippet(full_text)}"
            ), None, None

        timer_index = _find_timer_line_index(lines, after=ruler_index)
        if timer_index is None:
            return (
                "Could not find a remaining time (hours/minutes/seconds) below the "
                "'Domain Ruler' label."
                f"{ocr_debug_snippet(full_text)}"
            ), None, None

        boss_name = _extract_boss_name(lines, ruler_index, timer_index)
        if not boss_name:
            return (
                "ERROR: A timer was found but no boss name could be read."
                f"{ocr_debug_snippet(full_text)}"
            ), None, None

        timer_line = lines[timer_index]
        timer_text = _read_timer_region(processed_img, timer_line)
        total_seconds_remaining = parse_remaining_seconds(timer_text)

        if total_seconds_remaining is None:
            # Fall back to the layout pass when the digits-only pass reads nothing.
            timer_text = timer_line['text']
            total_seconds_remaining = parse_remaining_seconds(timer_text)

        if total_seconds_remaining is None:
            return (
                f"Could not read the remaining time from the timer row '{timer_line['text']}'."
                f"{ocr_debug_snippet(full_text)}"
            ), None, None

        validation_error = validate_remaining_seconds(total_seconds_remaining)
        if validation_error:
            return (
                f"ERROR: {validation_error}\nDetected boss: '{boss_name}', "
                f"timer text: '{timer_text.strip()}'."
                f"{ocr_debug_snippet(full_text)}"
            ), None, None

        future_timestamp = int(time.time() + total_seconds_remaining)
        discord_message = (
            f"**{boss_name}** \n<t:{future_timestamp}:F> thats <t:{future_timestamp}:R>"
        )
        return discord_message, future_timestamp, boss_name

    except pytesseract.TesseractNotFoundError:
        return (
            "ERROR: Tesseract OCR engine not found. Please install Tesseract and configure its path if necessary."
        ), None, None
    except Exception as e:
        return f"An unexpected error occurred: {e}", None, None


if __name__ == "__main__":
    clipboard_image = ImageGrab.grabclipboard()
    if clipboard_image:
        print("Processing image from clipboard...")
        result_message, _, _ = parse_boss_info(clipboard_image)
    else:
        result_message = "ERROR: No image found in clipboard."
    print("\n--- OCR Result ---")
    print(result_message)
    print("----------------------------")
