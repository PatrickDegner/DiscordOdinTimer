"""Tests for timer persistence, editing and the OCR confirmation flow."""
import asyncio
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

BERLIN = ZoneInfo("Europe/Berlin")
NEW_YORK = ZoneInfo("America/New_York")


# --- timezone handling ---

def test_resolve_zone_returns_none_for_blank_names(boss_module):
    assert boss_module.resolve_zone(None) is None
    assert boss_module.resolve_zone("   ") is None


def test_resolve_zone_rejects_unknown_names(boss_module):
    with pytest.raises(ValueError, match="Unknown timezone"):
        boss_module.resolve_zone("Mars/Olympus_Mons")


def test_naive_to_timestamp_interprets_wall_clock_in_the_given_zone(boss_module):
    wall_clock = datetime(2026, 6, 15, 20, 0)
    berlin = boss_module.BossTimers._naive_to_timestamp(wall_clock, BERLIN)
    new_york = boss_module.BossTimers._naive_to_timestamp(wall_clock, NEW_YORK)

    # In June, Berlin is UTC+2 and New York is UTC-4.
    assert new_york - berlin == 6 * 3600


def test_wall_clock_at_converts_back_into_the_zone(boss_module):
    timestamp = datetime(2026, 6, 15, 20, 0, tzinfo=BERLIN).timestamp()
    assert boss_module.BossTimers._wall_clock_at(timestamp, BERLIN) == datetime(2026, 6, 15, 20, 0)
    assert boss_module.BossTimers._wall_clock_at(timestamp, NEW_YORK) == datetime(2026, 6, 15, 14, 0)


def test_parse_date_today_depends_on_the_zone(boss_module):
    # Kiritimati (UTC+14) and Midway (UTC-11) are 25 hours apart, so their
    # local calendar dates can never be the same.
    east = boss_module.BossTimers._parse_date("today", zone=ZoneInfo("Pacific/Kiritimati"))
    west = boss_module.BossTimers._parse_date("today", zone=ZoneInfo("Pacific/Midway"))
    assert east != west


def test_onetime_occurrence_honours_the_stored_event_timezone(cog):
    berlin_event = {"id": "a", "name": "E", "is_one_time": True, "date": "2099-06-15",
                    "time": "20:00", "timezone": "Europe/Berlin"}
    ny_event = dict(berlin_event, id="b", timezone="America/New_York")

    assert cog._get_next_occurrence(ny_event) - cog._get_next_occurrence(berlin_event) == 6 * 3600


def test_recurring_occurrence_honours_the_stored_event_timezone(cog):
    # Midday UTC, so "today at 20:00 local" is still ahead in both zones.
    after = datetime(2026, 6, 15, 12, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    berlin_event = {"id": "a", "name": "E", "schedule": "daily", "time": "20:00",
                    "timezone": "Europe/Berlin"}
    ny_event = dict(berlin_event, id="b", timezone="America/New_York")

    berlin_ts = cog._get_next_occurrence(berlin_event, after=after)
    ny_ts = cog._get_next_occurrence(ny_event, after=after)

    assert cog._wall_clock_at(berlin_ts, BERLIN) == datetime(2026, 6, 15, 20, 0)
    assert cog._wall_clock_at(ny_ts, NEW_YORK) == datetime(2026, 6, 15, 20, 0)
    assert ny_ts - berlin_ts == 6 * 3600


def test_event_zone_falls_back_to_default_for_invalid_values(cog, boss_module):
    assert cog._event_zone({"timezone": "Nope/Nope"}) is boss_module.DEFAULT_ZONE
    assert cog._event_zone({}) is boss_module.DEFAULT_ZONE


def test_resolve_new_timestamp_uses_the_supplied_zone(cog):
    now = datetime(2026, 6, 15, 6, 0, tzinfo=BERLIN).timestamp()

    berlin_ts = cog._resolve_new_timestamp("20:00", now=now, zone=BERLIN)
    ny_ts = cog._resolve_new_timestamp("20:00", now=now, zone=NEW_YORK)

    assert cog._wall_clock_at(berlin_ts, BERLIN) == datetime(2026, 6, 15, 20, 0)
    assert cog._wall_clock_at(ny_ts, NEW_YORK) == datetime(2026, 6, 15, 20, 0)


def test_resolve_new_timestamp_duration_is_timezone_independent(cog):
    now = datetime(2026, 6, 15, 6, 0, tzinfo=BERLIN).timestamp()
    assert (
        cog._resolve_new_timestamp("1h30m", now=now, zone=BERLIN)
        == cog._resolve_new_timestamp("1h30m", now=now, zone=NEW_YORK)
    )


# --- duration parsing (used by /boss edit and the correction modal) ---

@pytest.mark.parametrize("text,expected", [
    ("90", 5400),
    ("45m", 2700),
    ("1h30m", 5400),
    ("1h 30m", 5400),
    ("2h5m10s", 7510),
    ("30s", 30),
    ("  1H30M  ", 5400),
])
def test_parse_duration_seconds_accepts_common_formats(boss_module, text, expected):
    assert boss_module.BossTimers._parse_duration_seconds(text) == expected


@pytest.mark.parametrize("text", ["", None, "abc", "1x", "1h30", "-5m", "0m", "25h"])
def test_parse_duration_seconds_rejects_invalid_input(boss_module, text):
    with pytest.raises(ValueError):
        boss_module.BossTimers._parse_duration_seconds(text)


# --- new time resolution (improvement 13) ---

def test_resolve_new_timestamp_handles_duration(cog):
    now = 1_700_000_000
    assert cog._resolve_new_timestamp("1h30m", now=now) == now + 5400


def test_resolve_new_timestamp_handles_clock_time_later_today(cog):
    from datetime import datetime
    now_dt = datetime(2026, 7, 12, 10, 0)
    resolved = cog._resolve_new_timestamp("20:00", now=now_dt.timestamp())
    assert resolved == int(datetime(2026, 7, 12, 20, 0).timestamp())


def test_resolve_new_timestamp_rolls_clock_time_to_tomorrow(cog):
    from datetime import datetime
    now_dt = datetime(2026, 7, 12, 22, 0)
    resolved = cog._resolve_new_timestamp("06:00", now=now_dt.timestamp())
    assert resolved == int(datetime(2026, 7, 13, 6, 0).timestamp())


def test_resolve_new_timestamp_handles_absolute_date_and_time(cog):
    from datetime import datetime
    resolved = cog._resolve_new_timestamp("2099-06-15 20:00", now=1_700_000_000)
    assert resolved == int(datetime(2099, 6, 15, 20, 0).timestamp())


def test_resolve_new_timestamp_rejects_past_absolute_date(cog):
    with pytest.raises(ValueError, match="past"):
        cog._resolve_new_timestamp("2000-01-01 20:00", now=time.time())


# --- timer editing (improvement 13) ---

def test_apply_timer_edit_moves_timer_to_new_timestamp(cog):
    cog.boss_timers = {1000: {"name": "Bellar", "sent_alert": True, "alert_seconds": 300}}

    timestamp, data, changes = cog._apply_timer_edit("bellar", new_timestamp=5000, now=1000)

    assert timestamp == 5000
    assert 1000 not in cog.boss_timers
    assert cog.boss_timers[5000] is data
    assert "spawn time" in changes


def test_apply_timer_edit_rearms_alert_when_moved_out_of_window(cog):
    cog.boss_timers = {1100: {"name": "Bellar", "sent_alert": True, "alert_seconds": 300}}

    _, data, _ = cog._apply_timer_edit("Bellar", new_timestamp=9000, now=1000)

    assert data["sent_alert"] is False


def test_apply_timer_edit_keeps_alert_flag_inside_window(cog):
    cog.boss_timers = {1100: {"name": "Bellar", "sent_alert": True, "alert_seconds": 300}}

    _, data, _ = cog._apply_timer_edit("Bellar", extra_informations="bring potions", now=1000)

    assert data["sent_alert"] is True
    assert data["extra_informations"] == "bring potions"


def test_apply_timer_edit_updates_alert_settings(cog):
    cog.boss_timers = {5000: {"name": "Bellar", "sent_alert": False, "alert_seconds": 300}}

    _, data, changes = cog._apply_timer_edit(
        "Bellar", alert_seconds=900, alert_mention="@@LW", now=1000
    )

    assert data["alert_seconds"] == 900
    assert data["alert_mention"] == "@LW"
    assert len(changes) == 2


def test_apply_timer_edit_targets_the_soonest_matching_timer(cog):
    cog.boss_timers = {
        9000: {"name": "Bellar", "sent_alert": False, "alert_seconds": 300},
        3000: {"name": "Bellar", "sent_alert": False, "alert_seconds": 300},
    }

    timestamp, _, _ = cog._apply_timer_edit("Bellar", alert_seconds=600, now=1000)

    assert timestamp == 3000
    assert cog.boss_timers[9000]["alert_seconds"] == 300


def test_apply_timer_edit_avoids_timestamp_collision(cog):
    cog.boss_timers = {
        1000: {"name": "Bellar", "sent_alert": False, "alert_seconds": 300},
        5000: {"name": "Other", "sent_alert": False, "alert_seconds": 300},
    }

    timestamp, _, _ = cog._apply_timer_edit("Bellar", new_timestamp=5000, now=1000)

    assert timestamp == 5001
    assert cog.boss_timers[5000]["name"] == "Other"


def test_apply_timer_edit_raises_for_unknown_boss(cog):
    cog.boss_timers = {1000: {"name": "Bellar", "sent_alert": False}}

    with pytest.raises(LookupError):
        cog._apply_timer_edit("Nobody", alert_seconds=600)


def test_apply_timer_edit_invalidates_update_message_cache(cog):
    cog.boss_timers = {1000: {"name": "Bellar", "sent_alert": False, "alert_seconds": 300}}
    cog.last_update_event_key = (1000, "Bellar", None)

    cog._apply_timer_edit("Bellar", alert_seconds=600, now=1)

    assert cog.last_update_event_key is None


# --- persistence (improvement 7) ---

def test_save_and_load_timers_roundtrip(cog):
    future = int(time.time()) + 3600
    cog.boss_timers = {
        future: {
            "name": "Bellar",
            "image": "data/cropped_screenshot_Bellar_1.png",
            "sent_alert": False,
            "alert_seconds": 600,
        }
    }
    cog._save_timers()

    cog.boss_timers = {}
    cog._load_timers()

    assert future in cog.boss_timers
    assert cog.boss_timers[future]["name"] == "Bellar"
    assert cog.boss_timers[future]["alert_seconds"] == 600


def test_sent_alert_survives_a_restart(cog):
    """A timer alerted before a restart must not alert again afterwards."""
    future = int(time.time()) + 300
    cog.boss_timers = {future: {"name": "Bellar", "sent_alert": True, "alert_seconds": 600}}
    cog._save_timers()

    cog.boss_timers = {}
    cog._load_timers()

    restored = cog.boss_timers[future]
    # Still inside the alert window, so only the persisted flag stops a second ping.
    assert cog._should_alert_now(future, restored) is True
    assert restored["sent_alert"] is True


def test_save_timers_excludes_static_occurrences(cog):
    future = int(time.time()) + 3600
    cog.boss_timers = {
        future: {"name": "Static", "static_id": "abc", "sent_alert": False},
        future + 10: {"name": "Ocr", "sent_alert": False},
    }
    cog._save_timers()

    with cog.timers_file.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    assert [entry["name"] for entry in payload] == ["Ocr"]


def test_load_timers_skips_expired_entries(cog):
    past = int(time.time()) - 60
    future = int(time.time()) + 60
    cog.boss_timers = {past: {"name": "Old", "sent_alert": False}, future: {"name": "New", "sent_alert": False}}
    cog._save_timers()

    cog.boss_timers = {}
    cog._load_timers()

    assert [data["name"] for data in cog.boss_timers.values()] == ["New"]


def test_load_timers_compacts_expired_entries_on_disk(cog):
    past = int(time.time()) - 60
    future = int(time.time()) + 60
    cog.timers_file.write_text(
        json.dumps([
            {"timestamp": past, "name": "Old"},
            {"timestamp": future, "name": "New"},
            {"timestamp": future + 10, "name": "Static", "static_id": "abc"},
            {"name": "Malformed"},
        ]),
        encoding="utf-8",
    )

    cog._load_timers()

    with cog.timers_file.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    assert [entry["name"] for entry in payload] == ["New"]


def test_load_timers_leaves_a_clean_file_untouched(cog):
    future = int(time.time()) + 60
    raw = json.dumps([{"timestamp": future, "name": "New", "image": "data/a.png"}])
    cog.timers_file.write_text(raw, encoding="utf-8")

    cog._load_timers()

    # Unchanged formatting proves no rewrite happened.
    assert cog.timers_file.read_text(encoding="utf-8") == raw


def test_load_timers_normalizes_windows_paths(cog):
    future = int(time.time()) + 3600
    cog.timers_file.write_text(
        json.dumps([{"timestamp": future, "name": "Bellar", "image": r"data\shot.png"}]),
        encoding="utf-8",
    )

    cog._load_timers()

    assert cog.boss_timers[future]["image"] == "data/shot.png"


def test_load_timers_tolerates_corrupt_file(cog):
    cog.timers_file.write_text("{not json", encoding="utf-8")

    cog._load_timers()

    assert cog.boss_timers == {}


def test_delete_and_expiry_persist_remaining_timers(cog):
    future = int(time.time()) + 3600
    expired = int(time.time()) - 5
    cog.boss_timers = {
        future: {"name": "Keep", "sent_alert": False},
        expired: {"name": "Gone", "sent_alert": False},
    }

    asyncio.run(cog._cleanup_expired_timers())

    with cog.timers_file.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    assert [entry["name"] for entry in payload] == ["Keep"]


def test_save_timers_is_a_noop_without_a_configured_file(boss_module):
    instance = boss_module.BossTimers.__new__(boss_module.BossTimers)
    instance.boss_timers = {1000: {"name": "Bellar"}}
    instance._save_timers()  # must not raise


# --- startup cleanup must not delete restored timer images ---

def test_cleanup_temp_images_keeps_images_referenced_by_timers(cog, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    referenced = data_dir / "cropped_screenshot_Bellar_1.png"
    orphan = data_dir / "cropped_screenshot_Orphan_2.png"
    referenced.write_bytes(b"x")
    orphan.write_bytes(b"x")

    cog.boss_timers = {int(time.time()) + 60: {"name": "Bellar", "image": "data/cropped_screenshot_Bellar_1.png"}}

    asyncio.run(cog.cleanup_temp_images())

    assert referenced.exists()
    assert not orphan.exists()


def test_cleanup_temp_images_leaves_subfolders_untouched(cog, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    library = tmp_path / "data" / "boss_images"
    library.mkdir(parents=True)
    library_image = library / "bellar.png"
    library_image.write_bytes(b"x")

    asyncio.run(cog.cleanup_temp_images())

    assert library_image.exists()


# --- OCR confirmation flow (improvement 8) ---

def test_build_ocr_preview_content_mentions_all_actions(boss_module):
    content = boss_module.BossTimers._build_ocr_preview_content("Bellar", 1700000000)

    assert "Bellar" in content
    assert "<t:1700000000:F>" in content
    assert "Confirm" in content
    assert "Correct time" in content
    assert "Cancel" in content


def test_build_ocr_preview_content_notes_a_library_image(boss_module):
    content = boss_module.BossTimers._build_ocr_preview_content(
        "Bellar", 1700000000, "data/boss_images/bellar.png"
    )
    assert "data/boss_images/bellar.png" in content


def test_build_ocr_preview_uses_the_library_image_when_one_exists(cog):
    library_image = cog.boss_image_library_dir / "bellar.png"
    Image.new("RGB", (10, 10)).save(library_image)

    content, preview_file = cog._build_ocr_preview("Bellar", 1700000000, Image.new("RGB", (40, 20)))

    assert preview_file.filename == "bellar.png"
    assert "bellar.png" in content


def test_build_ocr_preview_falls_back_to_the_cropped_screenshot(cog):
    content, preview_file = cog._build_ocr_preview("Megir", 1700000000, Image.new("RGB", (40, 20)))

    assert preview_file.filename == "preview.png"
    assert "boss_images" not in content


# --- name autocomplete and guarded delete ---

def test_known_event_names_merges_timers_and_static_events(cog):
    cog.boss_timers = {1000: {"name": "Megir"}, 2000: {"name": "Bellar"}, 3000: {"name": "Megir"}}
    cog.static_events = {"a": {"name": "Weekly Raid"}}

    assert cog._known_event_names() == ["Bellar", "Megir", "Weekly Raid"]


def test_collect_delete_targets_matches_case_insensitively(cog):
    cog.boss_timers = {1000: {"name": "Megir"}, 2000: {"name": "Other"}}
    cog.static_events = {"a": {"name": "megir"}, "b": {"name": "Other"}}

    timers, static_ids = cog._collect_delete_targets("  MEGIR ")

    assert [ts for ts, _ in timers] == [1000]
    assert static_ids == ["a"]


def test_collect_delete_targets_is_read_only(cog):
    cog.boss_timers = {1000: {"name": "Megir"}}
    cog.static_events = {"a": {"name": "Megir"}}

    cog._collect_delete_targets("Megir")

    assert cog.boss_timers and cog.static_events


def test_delete_boss_entries_removes_timers_static_events_and_images(cog, tmp_path):
    timer_image = tmp_path / "timer.png"
    event_image = tmp_path / "event.png"
    timer_image.write_bytes(b"x")
    event_image.write_bytes(b"x")

    cog.boss_timers = {1000: {"name": "Megir", "image": str(timer_image)}}
    cog.static_events = {"a": {"name": "Megir", "image": str(event_image)}}

    deleted_timers, deleted_events = cog._delete_boss_entries("Megir")

    assert (deleted_timers, deleted_events) == (1, 1)
    assert cog.boss_timers == {}
    assert cog.static_events == {}
    assert not timer_image.exists()
    assert not event_image.exists()


def test_delete_boss_entries_keeps_reused_static_images(cog, tmp_path):
    shared_image = tmp_path / "shared.png"
    shared_image.write_bytes(b"x")
    cog.boss_timers = {}
    cog.static_events = {"a": {"name": "Megir", "image": str(shared_image), "is_reused_image": True}}

    cog._delete_boss_entries("Megir")

    assert shared_image.exists()


def test_delete_boss_entries_reports_zero_for_unknown_names(cog):
    cog.boss_timers = {1000: {"name": "Megir"}}
    assert cog._delete_boss_entries("Nobody") == (0, 0)
    assert 1000 in cog.boss_timers


def test_build_delete_preview_lists_counts_and_warns(boss_module):
    timers = [(1700000000, {"name": "Megir"}), (1700000600, {"name": "Megir"})]
    preview = boss_module.BossTimers._build_delete_preview("Megir", timers, ["a"])

    assert "**2** timer(s)" in preview
    assert "**1** static/one-time event(s)" in preview
    assert "<t:1700000000:F>" in preview
    assert "stop recurring" in preview
    assert "cannot be undone" in preview


def test_build_delete_preview_truncates_long_timer_lists(boss_module):
    timers = [(1700000000 + index, {"name": "Megir"}) for index in range(15)]
    preview = boss_module.BossTimers._build_delete_preview("Megir", timers, [])

    assert "and 5 more timer(s)" in preview


def test_register_ocr_timer_saves_cropped_image_and_persists(cog, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    future = int(time.time()) + 3600
    cropped = Image.new("RGB", (40, 20), "white")

    image_path, using_library = asyncio.run(cog._register_ocr_timer("Bellar", future, cropped))

    assert using_library is False
    assert os.path.exists(image_path)
    assert image_path.startswith("data/")
    assert cog.boss_timers[future]["name"] == "Bellar"
    assert cog.boss_timers[future]["alert_seconds"] == 600
    assert cog.timers_file.exists()


def test_register_ocr_timer_prefers_library_image(cog, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    library_image = cog.boss_image_library_dir / "bellar.png"
    Image.new("RGB", (10, 10)).save(library_image)
    future = int(time.time()) + 3600

    image_path, using_library = asyncio.run(
        cog._register_ocr_timer("Bellar", future, Image.new("RGB", (40, 20)))
    )

    assert using_library is True
    assert image_path.endswith("bellar.png")
    assert cog.boss_timers[future]["is_custom_image"] is True


def test_register_ocr_timer_replaces_colliding_timer(cog, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    future = int(time.time()) + 3600
    old_image = tmp_path / "old.png"
    old_image.write_bytes(b"x")
    cog.boss_timers = {future: {"name": "Old", "image": str(old_image), "sent_alert": False}}

    asyncio.run(cog._register_ocr_timer("Bellar", future, Image.new("RGB", (40, 20))))

    assert cog.boss_timers[future]["name"] == "Bellar"
    assert not old_image.exists()


# --- crop behaviour across aspect ratios ---

@pytest.mark.parametrize("size", [(1920, 1080), (3440, 1440), (1080, 1920), (800, 100)])
def test_crop_is_proportional_and_keeps_full_width(boss_module, size):
    image = Image.new("RGB", size)
    cropped = boss_module.BossTimers._crop_image_for_timer(image)

    assert cropped.width == image.width
    assert cropped.height == int(image.height * 0.86)
