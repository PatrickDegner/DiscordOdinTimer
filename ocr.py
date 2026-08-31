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
IGNORED_SPAWNING_RESULT = "IGNORED: Boss is currently spawning."
IGNORED_DAY_TIMER_RESULT = "IGNORED: Boss timer includes days."
IGNORED_ABSOLUTE_RESULT = "IGNORED: The Absolute cards are not Domain Rulers."

MIN_CARD_ASPECT = 0.30
MAX_CARD_ASPECT = 0.55
MIN_CARD_COVERAGE = 0.60
MAX_CARDS_PER_IMAGE = 10
MIN_MOBILE_CARD_ASPECT = 0.90
MAX_MOBILE_CARD_ASPECT = 1.50
TARGET_MOBILE_CARD_ASPECT = 1.20

_DEBUG_TEXT_LIMIT = 400
OCR_TIMEOUT_SECONDS = 5
LAYOUT_REGION_TOP_RATIO = 0.50

# Pass 1 reads the layout with a normal alphabet so the boss name stays readable.
LAYOUT_CONFIG = r'--oem 1 --psm 6 -l eng'
# Pass 2 re-reads only the timer words. The whitelist makes letter look-alikes
# (ih/Ih/th for 1h, S for 5, O for 0) impossible instead of patching them afterwards.
TIMER_CONFIG = r'--oem 1 --psm 7 -l eng -c tessedit_char_whitelist=0123456789hms'

# Card text is bright over arbitrary game art, so a single global threshold only
# works when the art behind the text is dark. Sauvola compares every pixel with
# its own neighbourhood, which also handles bright backgrounds.
SAUVOLA_WINDOW = 31
SAUVOLA_K = 0.25
SAUVOLA_RANGE = 128.0

_UNIT_SECONDS = {'h': 3600, 'm': 60, 's': 1}

# Applied only to the numeric part of a timer token, never to the boss name.
_DIGIT_FIX = str.maketrans({
    'i': '1', 'I': '1', 'l': '1', 'L': '1', 't': '1', 'T': '1', '|': '1', '!': '1',
    'o': '0', 'O': '0', 'q': '0', 'Q': '0', 'D': '0',
    's': '5', 'S': '5', 'z': '2', 'Z': '2', 'b': '6', 'B': '8', 'g': '9', 'G': '6',
})

# Two characters max: hours <= 24, minutes and seconds <= 59.
_TIME_TOKEN_PATTERN = re.compile(r'([0-9iIlL|!tToOqQsSzZbBgG]{1,2})\s*([hms])')
_DAY_TOKEN_PATTERN = re.compile(r'[0-9iIlL|!tToOqQsSzZbBgG]{1,2}\s*d\b', re.IGNORECASE)


def ocr_debug_snippet(text: str | None, limit: int = _DEBUG_TEXT_LIMIT) -> str:
    """Formats raw OCR output for display in a Discord message."""
    cleaned = re.sub(r'\n{2,}', '\n', (text or '').strip()).replace('`', "'")
    if not cleaned:
        return "\nRaw OCR text: (empty)"
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + '...'
    return f"\nRaw OCR text:\n```\n{cleaned}\n```"


def is_ignored_ocr_result(message: str | None) -> bool:
    return message in {
        IGNORED_SPAWNING_RESULT,
        IGNORED_DAY_TIMER_RESULT,
        IGNORED_ABSOLUTE_RESULT,
    }


def _cluster_positions(positions: list[int], tolerance: int) -> list[int]:
    """Combines nearby detections of the same card border."""
    clusters = []
    for position in sorted(positions):
        if not clusters or position - clusters[-1][-1] > tolerance:
            clusters.append([position])
        else:
            clusters[-1].append(position)
    return [round(sum(cluster) / len(cluster)) for cluster in clusters]


def _hough_segments(lines) -> np.ndarray:
    """Normalizes OpenCV HoughLinesP output to one row per x1/y1/x2/y2 segment."""
    if lines is None:
        return np.empty((0, 4), dtype=np.int32)
    values = np.asarray(lines)
    if values.size == 0 or values.size % 4 != 0:
        return np.empty((0, 4), dtype=np.int32)
    return values.reshape(-1, 4)


def _has_visible_card_content(image: Image.Image) -> bool:
    """Rejects empty black grid cells while retaining dark cards with visible artwork."""
    gray = np.array(image.convert('L'))
    inset_y = max(1, image.height // 20)
    inset_x = max(1, image.width // 20)
    interior = gray[inset_y:-inset_y, inset_x:-inset_x]
    if interior.size == 0:
        return False
    return float(np.percentile(interior, 95) - np.percentile(interior, 5)) >= 20


def _snap_grid_boundaries(scores: np.ndarray, count: int, tolerance: int) -> list[int] | None:
    """Snaps evenly spaced grid boundaries to nearby edge-projection peaks."""
    length = len(scores)
    minimum_strength = max(12.0, float(np.percentile(scores, 90)))
    boundaries = [0]
    for index in range(1, count):
        expected = round(length * index / count)
        start = max(0, expected - tolerance)
        stop = min(length, expected + tolerance + 1)
        position = start + int(np.argmax(scores[start:stop]))
        if scores[position] < minimum_strength:
            return None
        boundaries.append(position)
    boundaries.append(length)
    return boundaries


def split_boss_cards(image: Image.Image) -> list[Image.Image]:
    """Splits bordered portrait rows or mobile card grids when confidently detected."""
    if not isinstance(image, Image.Image) or image.width < 2 or image.height < 2:
        return [image]

    rgb = np.array(image.convert('RGB'))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 35, 110)
    lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 180,
        threshold=max(25, image.height // 3),
        minLineLength=max(20, int(image.height * 0.65)),
        maxLineGap=max(4, int(image.height * 0.08)),
    )
    segments = _hough_segments(lines)
    if not len(segments):
        return [image]

    max_horizontal_drift = max(2, image.width // 250)
    border_positions = []
    for line in segments:
        x1, _, x2, _ = (int(value) for value in line)
        if abs(x1 - x2) <= max_horizontal_drift:
            border_positions.append(round((x1 + x2) / 2))

    tolerance = max(3, image.width // 200)
    borders = _cluster_positions(border_positions, tolerance)
    if not borders:
        return [image]

    if borders[0] <= tolerance * 2:
        borders[0] = 0
    else:
        borders.insert(0, 0)
    if image.width - 1 - borders[-1] <= tolerance * 2:
        borders[-1] = image.width
    else:
        borders.append(image.width)

    portrait_cards = []
    portrait_covered_width = 0
    for left, right in zip(borders, borders[1:]):
        width = right - left
        aspect = width / image.height
        if MIN_CARD_ASPECT <= aspect <= MAX_CARD_ASPECT:
            portrait_cards.append(image.crop((left, 0, right, image.height)))
            portrait_covered_width += width

    if (
        2 <= len(portrait_cards) <= MAX_CARDS_PER_IMAGE
        and portrait_covered_width / image.width >= MIN_CARD_COVERAGE
    ):
        return portrait_cards

    vertical_scores = edges.mean(axis=0)
    horizontal_scores = edges.mean(axis=1)

    best_mobile_row = None
    best_mobile_row_score = None
    for column_count in range(3, MAX_CARDS_PER_IMAGE + 1):
        cell_aspect = (image.width / column_count) / image.height
        if not MIN_MOBILE_CARD_ASPECT <= cell_aspect <= MAX_MOBILE_CARD_ASPECT:
            continue
        x_boundaries = _snap_grid_boundaries(
            vertical_scores,
            column_count,
            max(5, image.width // 60),
        )
        if x_boundaries is None:
            continue
        score = abs(cell_aspect - TARGET_MOBILE_CARD_ASPECT)
        if best_mobile_row_score is None or score < best_mobile_row_score:
            best_mobile_row_score = score
            best_mobile_row = x_boundaries

    if best_mobile_row is not None:
        cards = []
        for left, right in zip(best_mobile_row, best_mobile_row[1:]):
            card = image.crop((left, 0, right, image.height))
            if _has_visible_card_content(card):
                cards.append(card)
        if 2 <= len(cards) <= MAX_CARDS_PER_IMAGE:
            return cards

    best_grid = None
    best_score = None
    for row_count in range(2, 5):
        for column_count in range(3, 7):
            if row_count * column_count > MAX_CARDS_PER_IMAGE:
                continue
            cell_aspect = (image.width / column_count) / (image.height / row_count)
            if not MIN_MOBILE_CARD_ASPECT <= cell_aspect <= MAX_MOBILE_CARD_ASPECT:
                continue

            x_boundaries = _snap_grid_boundaries(
                vertical_scores,
                column_count,
                max(5, image.width // 60),
            )
            y_boundaries = _snap_grid_boundaries(
                horizontal_scores,
                row_count,
                max(5, image.height // 40),
            )
            if x_boundaries is None or y_boundaries is None:
                continue

            score = abs(cell_aspect - TARGET_MOBILE_CARD_ASPECT)
            if best_score is None or score < best_score:
                best_score = score
                best_grid = (x_boundaries, y_boundaries)

    if best_grid is None:
        return [image]

    x_boundaries, y_boundaries = best_grid
    cards = []
    for top, bottom in zip(y_boundaries, y_boundaries[1:]):
        for left, right in zip(x_boundaries, x_boundaries[1:]):
            card = image.crop((left, top, right, bottom))
            if _has_visible_card_content(card):
                cards.append(card)

    if not 2 <= len(cards) <= MAX_CARDS_PER_IMAGE:
        return [image]
    return cards


def _upscale(image: Image.Image, factor: int = 4) -> np.ndarray:
    """Returns the image as an RGB array, enlarged so small card text stays legible."""
    upscaled = image.resize((image.width * factor, image.height * factor), Image.LANCZOS)
    return np.array(upscaled.convert('RGB'))


def _value_channel(rgb: np.ndarray) -> np.ndarray:
    """Brightness per channel maximum, so the red 'Domain Ruler' label stays as bright as white text."""
    return rgb.max(axis=2)


def _gray_channel(rgb: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)


def _otsu_binarize(channel: np.ndarray) -> np.ndarray:
    _, binary = cv2.threshold(channel, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return binary


def _sauvola_binarize(channel: np.ndarray, window: int = SAUVOLA_WINDOW, k: float = SAUVOLA_K) -> np.ndarray:
    """Thresholds every pixel against its own neighbourhood; returns dark text on white."""
    # Inverted so the bright card text becomes the dark foreground Sauvola expects.
    inverted = (255 - channel).astype(np.float32)
    mean = cv2.boxFilter(inverted, cv2.CV_32F, (window, window), normalize=True)
    mean_of_squares = cv2.boxFilter(inverted * inverted, cv2.CV_32F, (window, window), normalize=True)
    std = np.sqrt(np.maximum(mean_of_squares - mean * mean, 0.0))
    threshold = mean * (1.0 + k * (std / SAUVOLA_RANGE - 1.0))
    return np.where(inverted < threshold, 0, 255).astype(np.uint8)


def preprocess_image(image: Image.Image) -> Image.Image:
    """Upscales and binarizes a card so Tesseract sees black text on white."""
    return Image.fromarray(_sauvola_binarize(_value_channel(_upscale(image))))


# Tried in order when the primary binarization does not produce a readable card.
_FALLBACK_BINARIZERS = (
    lambda rgb: _sauvola_binarize(_value_channel(rgb), window=61),
    lambda rgb: _otsu_binarize(_gray_channel(rgb)),
)


def _candidate_images(image: Image.Image):
    """Yields binarized versions of the card, cheapest and most reliable first."""
    yield preprocess_image(image)
    rgb = _upscale(image)
    for binarize in _FALLBACK_BINARIZERS:
        yield Image.fromarray(binarize(rgb))


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


def format_remaining_time(total_seconds: int) -> str:
    """Converts seconds to human-readable format like '18h', '2h 30m', '45m', etc."""
    hours = total_seconds // 3600
    remaining = total_seconds % 3600
    minutes = remaining // 60
    seconds = remaining % 60
    
    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if seconds > 0 or not parts:
        parts.append(f"{seconds}s")
    
    # If we have hours, don't show seconds
    if hours > 0 and seconds > 0:
        parts.pop()  # Remove seconds
    
    return " ".join(parts)


def _parse_time_components(text: str | None) -> dict[str, int]:
    if not text:
        return {}

    components = {}
    for value, unit in _TIME_TOKEN_PATTERN.findall(text):
        if unit in components:
            continue
        digits = value.translate(_DIGIT_FIX)
        if not digits.isdigit():
            continue
        components[unit] = int(digits)
    return components


def parse_remaining_seconds(text: str | None) -> int | None:
    """Sums the h/m/s tokens of a timer string like '1h 4m' or '6m 52s'."""
    components = _parse_time_components(text)
    if not components:
        return None
    return sum(value * _UNIT_SECONDS[unit] for unit, value in components.items())


def _layout_restores_repeated_hour(timer_text: str, layout_text: str) -> bool:
    """True when layout OCR preserves a repeated hour digit collapsed by the timer pass."""
    timer = _parse_time_components(timer_text)
    layout = _parse_time_components(layout_text)
    layout_hour = layout.get('h')
    timer_hour = timer.get('h')

    return (
        layout_hour in (11, 22)
        and timer_hour == layout_hour // 11
        and timer.get('m') == layout.get('m')
        and timer.get('s') == layout.get('s')
    )


def _is_time_word(text: str) -> bool:
    """True for words like '17h' or '44m', false for the clock icon and the 'left' label."""
    cleaned = text.strip('()[]{}.,:;')
    return bool(
        _TIME_TOKEN_PATTERN.fullmatch(cleaned)
        or _DAY_TOKEN_PATTERN.fullmatch(cleaned)
    )


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


def _letters_only(word: str) -> str:
    """Lowercased letters of a word, so OCR punctuation noise like "Ruler'" still matches."""
    return re.sub(r'[^a-z]', '', word.lower())


def _find_ruler_line_index(lines: list[dict]) -> int | None:
    for index, line in enumerate(lines):
        lowered = [_letters_only(word) for word in line['words']]
        if 'domain' in lowered and 'ruler' in lowered:
            return index
    return None


def _has_absolute_label(lines: list[dict]) -> bool:
    for line in lines:
        words = [_letters_only(word) for word in line['words']]
        if 'the' in words and 'absolute' in words:
            return True
    return False


def _find_timer_line_index(lines: list[dict], after: int, require_full_token: bool = False) -> int | None:
    """Returns the last line below the label that contains a duration token."""
    for index in range(len(lines) - 1, after, -1):
        if require_full_token:
            words = (word.strip('()[]{}.,:;') for word in lines[index]['words'])
            if any(
                _TIME_TOKEN_PATTERN.fullmatch(word) or _DAY_TOKEN_PATTERN.fullmatch(word)
                for word in words
            ):
                return index
            continue
        if _TIME_TOKEN_PATTERN.search(lines[index]['text']) or _DAY_TOKEN_PATTERN.search(lines[index]['text']):
            return index
    return None


def _has_spawning_status(lines: list[dict], after: int) -> bool:
    """True when a status row below the ruler label says Spawning."""
    return any(
        'spawning' in (_letters_only(word) for word in line['words'])
        for line in lines[after + 1:]
    )


def _clean_boss_name(candidate: str) -> str | None:
    cleaned = re.sub(r'[^A-Za-z\s-]', '', candidate)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' -')
    return cleaned or None


def _extract_boss_name(lines: list[dict], ruler_index: int, timer_index: int) -> str | None:
    ruler_line = lines[ruler_index]
    lowered = [_letters_only(word) for word in ruler_line['words']]
    candidates = [' '.join(ruler_line['words'][lowered.index('ruler') + 1:])]

    if ruler_index + 1 < len(lines) and ruler_index + 1 != timer_index:
        candidates.append(lines[ruler_index + 1]['text'])

    for candidate in candidates:
        name = _clean_boss_name(candidate)
        if name:
            return name
    return None


def _extract_name_before_timer(lines: list[dict], timer_index: int) -> str | None:
    if timer_index <= 0:
        return None
    return _clean_boss_name(lines[timer_index - 1]['text'])


def _read_timer_region(processed_img: Image.Image, line: dict, padding: int = 8) -> str:
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
    return pytesseract.image_to_string(
        region,
        config=TIMER_CONFIG,
        timeout=OCR_TIMEOUT_SECONDS,
    )


def parse_boss_info(image_source):
    """
    Reads a boss card in two passes: a layout pass for the name and a
    digits-only pass for the timer row. Retries with other binarizations when
    the card art defeats the primary one.
    Returns: (discord_message, future_timestamp, boss_name, formatted_time)
    """
    try:
        if not isinstance(image_source, Image.Image):
            return "ERROR: Invalid image source provided. Expected a PIL Image object.", None, None, None

        first_result = None
        for processed_img in _candidate_images(image_source):
            result = _parse_processed_card(processed_img)
            if result[1] is not None or is_ignored_ocr_result(result[0]):
                return result
            if first_result is None:
                first_result = result
        return first_result

    except pytesseract.TesseractNotFoundError:
        return (
            "ERROR: Tesseract OCR engine not found. Please install Tesseract and configure its path if necessary."
        ), None, None, None
    except RuntimeError as exc:
        if 'timeout' in str(exc).lower():
            return "ERROR: OCR timed out while processing this image.", None, None, None
        return f"An unexpected OCR error occurred: {exc}", None, None, None
    except Exception as e:
        return f"An unexpected error occurred: {e}", None, None, None


def _parse_processed_card(processed_img):
    """Runs both Tesseract passes over one binarized image."""
    layout_top = int(processed_img.height * LAYOUT_REGION_TOP_RATIO)
    layout_img = processed_img.crop((0, layout_top, processed_img.width, processed_img.height))
    data = pytesseract.image_to_data(
        layout_img,
        config=LAYOUT_CONFIG,
        output_type=pytesseract.Output.DICT,
        timeout=OCR_TIMEOUT_SECONDS,
    )
    lines = _group_text_lines(data)
    full_text = '\n'.join(line['text'] for line in lines)

    if not full_text.strip():
        return "ERROR: No text was extracted from the image by Tesseract OCR.", None, None, None

    ruler_index = _find_ruler_line_index(lines)
    if _has_absolute_label(lines):
        return IGNORED_ABSOLUTE_RESULT, None, None, None
    if _has_spawning_status(lines, after=ruler_index if ruler_index is not None else -1):
        return IGNORED_SPAWNING_RESULT, None, None, None

    timer_index = _find_timer_line_index(
        lines,
        after=ruler_index if ruler_index is not None else -1,
        require_full_token=ruler_index is None,
    )
    if timer_index is None:
        if ruler_index is None:
            return (
                "Could not find a timer row in the extracted card text."
                f"{ocr_debug_snippet(full_text)}"
            ), None, None, None
        return (
            "Could not find a remaining time (hours/minutes/seconds) below the "
            "'Domain Ruler' label."
            f"{ocr_debug_snippet(full_text)}"
        ), None, None, None

    if ruler_index is None:
        boss_name = _extract_name_before_timer(lines, timer_index)
    else:
        boss_name = _extract_boss_name(lines, ruler_index, timer_index)
    if not boss_name:
        return (
            "ERROR: A timer was found but no boss name could be read."
            f"{ocr_debug_snippet(full_text)}"
        ), None, None, None

    timer_line = lines[timer_index]
    if _DAY_TOKEN_PATTERN.search(timer_line['text']):
        return IGNORED_DAY_TIMER_RESULT, None, boss_name, None

    timer_text = _read_timer_region(layout_img, timer_line)
    total_seconds_remaining = parse_remaining_seconds(timer_text)

    if total_seconds_remaining is None:
        # Fall back to the layout pass when the digits-only pass reads nothing.
        timer_text = timer_line['text']
        total_seconds_remaining = parse_remaining_seconds(timer_text)
    elif _layout_restores_repeated_hour(timer_text, timer_line['text']):
        timer_text = timer_line['text']
        total_seconds_remaining = parse_remaining_seconds(timer_text)

    if total_seconds_remaining is None:
        return (
            f"Could not read the remaining time from the timer row '{timer_line['text']}'."
            f"{ocr_debug_snippet(full_text)}"
        ), None, None, None

    validation_error = validate_remaining_seconds(total_seconds_remaining)
    if validation_error:
        return (
            f"ERROR: {validation_error}\nDetected boss: '{boss_name}', "
            f"timer text: '{timer_text.strip()}'."
            f"{ocr_debug_snippet(full_text)}"
        ), None, None, None

    future_timestamp = int(time.time() + total_seconds_remaining)
    formatted_time = format_remaining_time(total_seconds_remaining)
    discord_message = (
        f"**{boss_name}** \n<t:{future_timestamp}:F> thats <t:{future_timestamp}:R>"
    )
    return discord_message, future_timestamp, boss_name, formatted_time


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
