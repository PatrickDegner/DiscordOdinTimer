"""OCR parsing tests that run without a Tesseract installation.

Both Tesseract passes are stubbed so the layout grouping, the digits-only
timer pass and the plausibility checks can be tested deterministically.
"""
import time

import cv2
import numpy as np
import pytest
from PIL import Image

# Layout as Tesseract reports it for a boss card: label line, name line, timer line.
CARD_LINES = [["Domain", "Ruler"], ["Megir"], ["(", "17h", "44m", "left"]]


def build_ocr_data(lines, conf=95):
    """Builds an image_to_data DICT from a list of word lists."""
    keys = ('text', 'conf', 'block_num', 'par_num', 'line_num', 'word_num',
            'left', 'top', 'width', 'height')
    data = {key: [] for key in keys}
    for line_index, words in enumerate(lines, start=1):
        for word_index, word in enumerate(words, start=1):
            data['text'].append(word)
            data['conf'].append(conf)
            data['block_num'].append(1)
            data['par_num'].append(1)
            data['line_num'].append(line_index)
            data['word_num'].append(word_index)
            data['left'].append(word_index * 120)
            data['top'].append(line_index * 60)
            data['width'].append(100)
            data['height'].append(40)
    return data


@pytest.fixture
def fake_ocr(ocr, monkeypatch):
    """Stubs the layout pass (image_to_data) and the timer pass (image_to_string)."""

    def _install(lines, timer_text=""):
        monkeypatch.setattr(ocr, "preprocess_image", lambda image: image)
        monkeypatch.setattr(ocr.pytesseract, "image_to_data", lambda *a, **k: build_ocr_data(lines))
        monkeypatch.setattr(ocr.pytesseract, "image_to_string", lambda *a, **k: timer_text)
        return Image.new("RGB", (2000, 1000))

    return _install


# --- multi-card segmentation ---

@pytest.mark.parametrize("shape", [(2, 1, 4), (2, 4), (8,)])
def test_hough_segments_normalizes_opencv_return_shapes(ocr, shape):
    lines = np.array([
        [10, 0, 10, 100],
        [20, 0, 20, 100],
    ], dtype=np.int32).reshape(shape)

    segments = ocr._hough_segments(lines)

    assert segments.shape == (2, 4)
    assert segments.tolist() == [[10, 0, 10, 100], [20, 0, 20, 100]]


def test_hough_segments_handles_none_and_malformed_values(ocr):
    assert ocr._hough_segments(None).shape == (0, 4)
    assert ocr._hough_segments(np.array([1, 2, 3])).shape == (0, 4)

def test_split_boss_cards_finds_bordered_portrait_cards(ocr):
    canvas = np.full((380, 640, 3), 12, dtype=np.uint8)
    for left in (2, 162, 322, 482):
        cv2.rectangle(canvas, (left, 5), (left + 150, 374), (90, 90, 90), 2)

    cards = ocr.split_boss_cards(Image.fromarray(canvas))

    assert len(cards) == 4
    assert all(145 <= card.width <= 155 for card in cards)
    assert all(card.height == 380 for card in cards)


def test_split_boss_cards_keeps_a_wide_single_card_intact(ocr):
    canvas = np.full((280, 336, 3), 12, dtype=np.uint8)
    cv2.rectangle(canvas, (2, 2), (333, 277), (90, 90, 90), 2)
    image = Image.fromarray(canvas)

    cards = ocr.split_boss_cards(image)

    assert len(cards) == 1
    assert cards[0] is image


def _mobile_grid(rows, columns, empty_cells=()):
    card_width = 336
    card_height = 280
    canvas = np.zeros((rows * card_height, columns * card_width, 3), dtype=np.uint8)

    for row in range(rows):
        for column in range(columns):
            left = column * card_width
            top = row * card_height
            if (row, column) not in empty_cells:
                canvas[top + 3:top + 230, left + 3:left + card_width - 3] = (35, 55, 75)
                canvas[top + 70:top + 150, left + 80:left + 250] = (130, 90, 60)
                canvas[top + 240:top + 270, left + 80:left + 260] = (20, 150, 210)

    for column in range(columns + 1):
        x = min(column * card_width, canvas.shape[1] - 1)
        cv2.line(canvas, (x, 0), (x, canvas.shape[0] - 1), (90, 90, 90), 2)
    for row in range(rows + 1):
        y = min(row * card_height, canvas.shape[0] - 1)
        cv2.line(canvas, (0, y), (canvas.shape[1] - 1, y), (90, 90, 90), 2)
    return Image.fromarray(canvas)


def test_split_boss_cards_finds_mobile_card_grid(ocr):
    cards = ocr.split_boss_cards(_mobile_grid(rows=2, columns=3))

    assert len(cards) == 6
    assert all(325 <= card.width <= 345 for card in cards)
    assert all(270 <= card.height <= 290 for card in cards)


@pytest.mark.parametrize("columns", [3, 4])
def test_split_boss_cards_finds_single_mobile_row(ocr, columns):
    cards = ocr.split_boss_cards(_mobile_grid(rows=1, columns=columns))

    assert len(cards) == columns
    assert all(325 <= card.width <= 345 for card in cards)
    assert all(270 <= card.height <= 290 for card in cards)


def test_split_boss_cards_omits_empty_mobile_grid_cell(ocr):
    cards = ocr.split_boss_cards(
        _mobile_grid(rows=2, columns=4, empty_cells={(1, 3)})
    )

    assert len(cards) == 7


# --- timer token parsing (the whitelist pass output) ---

@pytest.mark.parametrize("text,expected", [
    ("17h44m", 63840),
    ("17h 44m", 63840),
    ("1h4m", 3840),
    ("14h23m", 51780),
    ("6m52s", 412),
    ("4m54s", 294),
    ("52s", 52),
    ("2h", 7200),
])
def test_parse_remaining_seconds_handles_ingame_formats(ocr, text, expected):
    assert ocr.parse_remaining_seconds(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("ih 44m", 6240),
    ("Ih 4m", 3840),
    ("th 30m", 5400),
    ("1h 3O m", 5400),
    ("S2s", 52),
])
def test_parse_remaining_seconds_repairs_digit_lookalikes(ocr, text, expected):
    """Fallback path only: digits are repaired in the value, never in the unit."""
    assert ocr.parse_remaining_seconds(text) == expected


@pytest.mark.parametrize("text", ["", None, "left", "Megir", "Helgarm"])
def test_parse_remaining_seconds_returns_none_without_units(ocr, text):
    assert ocr.parse_remaining_seconds(text) is None


def test_parse_remaining_seconds_ignores_repeated_units(ocr):
    assert ocr.parse_remaining_seconds("5m 9m") == 300


def test_parse_remaining_seconds_ignores_values_longer_than_two_digits(ocr):
    # Hours are capped at two characters so icon noise cannot inflate the value.
    assert ocr.parse_remaining_seconds("123h") == 23 * 3600


# --- word classification for the timer crop ---

@pytest.mark.parametrize("word,expected", [
    ("17h", True),
    ("44m", True),
    ("ih", True),
    ("left", False),
    ("(", False),
    ("©", False),
    ("", False),
])
def test_is_time_word_excludes_icons_and_the_left_label(ocr, word, expected):
    assert ocr._is_time_word(word) is expected


# --- layout grouping ---

def test_group_text_lines_groups_words_and_boxes(ocr):
    lines = ocr._group_text_lines(build_ocr_data(CARD_LINES))

    assert [line['text'] for line in lines] == ["Domain Ruler", "Megir", "( 17h 44m left"]
    assert lines[2]['boxes'][1]['text'] == "17h"


def test_group_text_lines_drops_words_without_confidence(ocr):
    data = build_ocr_data([["Domain", "Ruler"]], conf=-1)
    assert ocr._group_text_lines(data) == []


def test_find_timer_line_index_picks_the_last_duration_line(ocr):
    lines = ocr._group_text_lines(build_ocr_data(CARD_LINES))
    assert ocr._find_timer_line_index(lines, after=0) == 2


def test_extract_boss_name_reads_the_line_below_the_label(ocr):
    lines = ocr._group_text_lines(build_ocr_data(CARD_LINES))
    assert ocr._extract_boss_name(lines, 0, 2) == "Megir"


def test_extract_boss_name_reads_the_same_line_when_inline(ocr):
    lines = ocr._group_text_lines(build_ocr_data([
        ["Domain", "Ruler", "The", "Matriarch"], ["(", "4m", "54s", "left"],
    ]))
    assert ocr._extract_boss_name(lines, 0, 1) == "The Matriarch"


def test_extract_boss_name_never_falls_back_to_the_timer_line(ocr):
    lines = ocr._group_text_lines(build_ocr_data([["Domain", "Ruler"], ["(", "17h", "44m", "left"]]))
    assert ocr._extract_boss_name(lines, 0, 1) is None


def test_find_ruler_line_index_ignores_punctuation_noise(ocr):
    lines = ocr._group_text_lines(build_ocr_data([["Domain", "Ruler\u2019", "'"], ["Svart"]]))
    assert ocr._find_ruler_line_index(lines) == 0


def test_extract_boss_name_skips_punctuation_only_trailing_words(ocr):
    """'Domain Ruler' ' is one misread line; the name still comes from the line below."""
    lines = ocr._group_text_lines(build_ocr_data([
        ["Domain", "Ruler\u2019", "'"], ["Svart", "~~"], ["\u00a9", "15h", "9m", "left"],
    ]))
    assert ocr._extract_boss_name(lines, 0, 2) == "Svart"


# --- end to end ---

def test_parse_boss_info_uses_the_whitelist_pass(ocr, fake_ocr):
    image = fake_ocr(CARD_LINES, timer_text="17h44m")
    message, timestamp, boss_name = ocr.parse_boss_info(image)

    assert boss_name == "Megir"
    assert 63835 <= timestamp - int(time.time()) <= 63845
    assert "Megir" in message


def test_whitelist_pass_overrides_a_misread_layout_pass(ocr, fake_ocr):
    """Layout reads '(S ih 44m'; the digits-only pass is authoritative."""
    image = fake_ocr(
        [["Domain", "Ruler"], ["Megir"], ["(S", "ih", "44m", "left"]],
        timer_text="1h44m",
    )
    _, timestamp, _ = ocr.parse_boss_info(image)

    assert 6235 <= timestamp - int(time.time()) <= 6245


def test_parse_boss_info_falls_back_when_whitelist_pass_is_empty(ocr, fake_ocr):
    image = fake_ocr(CARD_LINES, timer_text="")
    _, timestamp, boss_name = ocr.parse_boss_info(image)

    assert boss_name == "Megir"
    assert 63835 <= timestamp - int(time.time()) <= 63845


@pytest.mark.parametrize("timer_text,expected_error", [
    ("0m0s", "read as 0"),
    ("99h", "above the 24h limit"),
])
def test_parse_boss_info_rejects_implausible_timers(ocr, fake_ocr, timer_text, expected_error):
    image = fake_ocr(CARD_LINES, timer_text=timer_text)
    message, timestamp, boss_name = ocr.parse_boss_info(image)

    assert timestamp is None
    assert boss_name is None
    assert expected_error in message
    assert "Raw OCR text" in message


def test_parse_boss_info_reports_missing_domain_ruler_label(ocr, fake_ocr):
    image = fake_ocr([["some", "unrelated", "interface"]], timer_text="")
    message, timestamp, _ = ocr.parse_boss_info(image)

    assert timestamp is None
    assert "Domain Ruler" in message
    assert "some unrelated interface" in message


def test_parse_boss_info_recovers_name_when_ruler_label_is_garbled(ocr, fake_ocr):
    image = fake_ocr(
        [["noise"], ["somal", "Rte"], ["Svart", "-"], ["©", "15h", "9m", "left"]],
        timer_text="15h9m",
    )

    _, timestamp, boss_name = ocr.parse_boss_info(image)

    assert boss_name == "Svart"
    assert 54535 <= timestamp - int(time.time()) <= 54545


def test_parse_boss_info_ignores_spawning_when_ruler_label_is_garbled(ocr, fake_ocr):
    image = fake_ocr(
        [["noise"], ["somal", "Rte"], ["Helgarm"], ["(", "Spawning"]],
        timer_text="",
    )

    message, timestamp, boss_name = ocr.parse_boss_info(image)

    assert ocr.is_ignored_ocr_result(message)
    assert timestamp is None
    assert boss_name is None


def test_parse_boss_info_reports_missing_timer_row(ocr, fake_ocr):
    image = fake_ocr([["Domain", "Ruler"], ["Megir"]], timer_text="")
    message, timestamp, _ = ocr.parse_boss_info(image)

    assert timestamp is None
    assert "Could not find a remaining time" in message


def test_parse_boss_info_ignores_spawning_status(ocr, fake_ocr):
    image = fake_ocr(
        [["Domain", "Ruler"], ["Helgarm"], ["(", "Spawning"]],
        timer_text="",
    )

    message, timestamp, boss_name = ocr.parse_boss_info(image)

    assert ocr.is_ignored_ocr_result(message)
    assert timestamp is None
    assert boss_name is None


def test_parse_boss_info_reports_empty_ocr_output(ocr, fake_ocr):
    image = fake_ocr([], timer_text="")
    message, timestamp, _ = ocr.parse_boss_info(image)

    assert timestamp is None
    assert "No text was extracted" in message


def test_parse_boss_info_rejects_non_image_input(ocr):
    message, timestamp, _ = ocr.parse_boss_info("not an image")
    assert timestamp is None
    assert "Invalid image source" in message


def test_parse_boss_info_retries_with_another_binarization(ocr, monkeypatch):
    """Bright card art can defeat the first threshold; a later one still reads the card."""
    image = Image.new("RGB", (400, 200))
    monkeypatch.setattr(ocr, "_candidate_images", lambda source: iter([image, image]))
    monkeypatch.setattr(ocr.pytesseract, "image_to_string", lambda *a, **k: "17h44m")

    attempts = [build_ocr_data([["unreadable", "noise"]]), build_ocr_data(CARD_LINES)]
    monkeypatch.setattr(ocr.pytesseract, "image_to_data", lambda *a, **k: attempts.pop(0))

    _, timestamp, boss_name = ocr.parse_boss_info(image)

    assert boss_name == "Megir"
    assert 63835 <= timestamp - int(time.time()) <= 63845


def test_parse_boss_info_reports_the_first_failure_when_every_pass_fails(ocr, monkeypatch):
    image = Image.new("RGB", (400, 200))
    monkeypatch.setattr(ocr, "_candidate_images", lambda source: iter([image, image]))
    monkeypatch.setattr(ocr.pytesseract, "image_to_string", lambda *a, **k: "")

    attempts = [build_ocr_data([["first", "attempt"]]), build_ocr_data([["second", "attempt"]])]
    monkeypatch.setattr(ocr.pytesseract, "image_to_data", lambda *a, **k: attempts.pop(0))

    message, timestamp, _ = ocr.parse_boss_info(image)

    assert timestamp is None
    assert "first attempt" in message


# --- binarization ---

def test_preprocess_image_binarizes_bright_text_on_a_bright_background(ocr):
    """Local thresholding keeps the card text readable when the art behind it is light."""
    import numpy as np

    canvas = np.full((60, 120, 3), 190, dtype=np.uint8)
    canvas[20:40, 58:61] = 250  # a thin bright stroke on an almost equally bright background
    binarized = np.array(ocr.preprocess_image(Image.fromarray(canvas)))

    assert binarized[120, 236] == 0  # stroke reads as dark text
    assert binarized[10, 10] == 255  # background reads as white paper


# --- raw text diagnostics ---

def test_ocr_debug_snippet_truncates_long_text(ocr):
    snippet = ocr.ocr_debug_snippet("x" * 1000, limit=50)
    assert "x" * 50 in snippet
    assert "..." in snippet
    assert len(snippet) < 200


def test_ocr_debug_snippet_strips_backticks_to_keep_code_block_intact(ocr):
    snippet = ocr.ocr_debug_snippet("bad ``` text")
    assert snippet.count("```") == 2


def test_ocr_debug_snippet_handles_empty_text(ocr):
    assert "(empty)" in ocr.ocr_debug_snippet("")


# --- plausibility bounds ---

def test_validate_remaining_seconds_accepts_normal_duration(ocr):
    assert ocr.validate_remaining_seconds(3600) is None


def test_validate_remaining_seconds_rejects_zero(ocr):
    assert "0" in ocr.validate_remaining_seconds(0)


def test_validate_remaining_seconds_rejects_above_24_hours(ocr):
    assert "24h limit" in ocr.validate_remaining_seconds(ocr.MAX_TIMER_SECONDS + 1)
