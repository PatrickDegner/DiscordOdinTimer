import importlib.util
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("boss_timers_module", ROOT / "cogs" / "boss_timers.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_alert_state_tracking_and_default_timer():
    cog = module.BossTimers.__new__(module.BossTimers)
    cog.boss_timers = {
        1000: {"name": "Test Boss", "sent_alert": False},
        2000: {"name": "Another Boss", "sent_alert": True},
    }

    next_ts, next_data = cog._get_next_timer()
    assert next_ts == 1000
    assert next_data["name"] == "Test Boss"

    assert module.BossTimers._parse_alert_time(None) == 300
    assert module.BossTimers._parse_alert_time("5m") == 300
    assert module.BossTimers._parse_alert_time("1m") == 60


def test_alert_candidates_include_custom_static_alert_windows():
    cog = module.BossTimers.__new__(module.BossTimers)
    timers = {
        1300: {"name": "Normal Boss", "alert_seconds": 300, "sent_alert": False},
        1400: {"name": "Static Boss", "alert_seconds": 600, "sent_alert": False},
        2000: {"name": "Far Boss", "alert_seconds": 600, "sent_alert": False},
    }

    candidates = cog._get_alert_candidates(1000, timers)
    assert [data["name"] for _, data in candidates] == ["Normal Boss", "Static Boss"]


def test_build_upcoming_events_section_lists_only_next_24h_sorted():
    cog = module.BossTimers.__new__(module.BossTimers)
    timers = {
        900: {"name": "Already Expired"},
        1100: {"name": "Soon"},
        1200: {"name": "Later"},
        1100 + (24 * 60 * 60) + 5: {"name": "Beyond 24h"},
    }

    section = cog._build_upcoming_events_section(now=1000, timers=timers)

    assert "Soon" in section
    assert "Later" in section
    assert "Already Expired" not in section
    assert "Beyond 24h" not in section
    assert section.index("Soon") < section.index("Later")


def test_build_upcoming_events_section_handles_empty_window():
    cog = module.BossTimers.__new__(module.BossTimers)
    section = cog._build_upcoming_events_section(now=1000, timers={900: {"name": "Past"}})
    assert "No upcoming events in the next 24 hours." in section


def test_timer_loop_interval_is_stable_at_15_seconds():
    cog = module.BossTimers.__new__(module.BossTimers)
    assert cog.manage_boss_timers_task.seconds == 15


def test_expired_non_static_timer_deletes_cropped_image(tmp_path):
    cog = module.BossTimers.__new__(module.BossTimers)
    image_path = tmp_path / "expired_timer.png"
    image_path.write_bytes(b"test")

    expired_timestamp = int(time.time()) - 5
    cog.boss_timers = {
        expired_timestamp: {
            "name": "Temporary Boss",
            "image": str(image_path),
            "sent_alert": False,
        }
    }
    cog.static_events = {}
    cog._schedule_static_event = lambda event, after=None: None

    asyncio.run(cog._cleanup_expired_timers())

    assert expired_timestamp not in cog.boss_timers
    assert not image_path.exists()


def test_expired_static_timer_keeps_image_and_reschedules(tmp_path):
    cog = module.BossTimers.__new__(module.BossTimers)
    image_path = tmp_path / "static_event.png"
    image_path.write_bytes(b"test")

    event_id = "event-1"
    event = {
        "id": event_id,
        "name": "Static Event",
        "schedule": "daily",
        "time": "20:00",
        "image": str(image_path),
    }

    expired_timestamp = int(time.time()) - 5
    cog.boss_timers = {
        expired_timestamp: {
            "name": "Static Event",
            "image": str(image_path),
            "sent_alert": False,
            "static_id": event_id,
        }
    }
    cog.static_events = {event_id: event}

    reschedule_calls = []

    def _record_reschedule(event_data, after=None):
        reschedule_calls.append((event_data.get("id"), after))

    cog._schedule_static_event = _record_reschedule

    asyncio.run(cog._cleanup_expired_timers())

    assert expired_timestamp not in cog.boss_timers
    assert image_path.exists()
    assert len(reschedule_calls) == 1
    assert reschedule_calls[0][0] == event_id


def test_find_library_boss_image_matches_sanitized_name(tmp_path):
    cog = module.BossTimers.__new__(module.BossTimers)
    boss_image_dir = tmp_path / "boss_images"
    boss_image_dir.mkdir()
    expected_image = boss_image_dir / "chaos_priest.jpg"
    expected_image.write_bytes(b"test")
    cog.boss_image_library_dir = boss_image_dir

    found = cog._find_library_boss_image("Chaos Priest")
    assert found == str(expected_image)


def test_cleanup_timer_image_keeps_custom_library_image(tmp_path):
    cog = module.BossTimers.__new__(module.BossTimers)
    image_path = tmp_path / "bjorn.jpg"
    image_path.write_bytes(b"test")

    timer_data = {
        "name": "Bjorn",
        "image": str(image_path),
        "is_custom_image": True,
    }

    cog._cleanup_timer_image(timer_data)
    assert image_path.exists()


def test_cleanup_existing_timer_does_not_delete_library_image_when_replaced(tmp_path):
    cog = module.BossTimers.__new__(module.BossTimers)
    library_image = tmp_path / "chaos_priest.jpg"
    library_image.write_bytes(b"test")

    existing_timer = {
        "name": "Chaos Priest",
        "image": str(library_image),
        "is_custom_image": True,
    }

    cog._cleanup_timer_image(existing_timer)
    assert library_image.exists()


def test_build_alert_message_uses_custom_alert_mention():
    cog = module.BossTimers.__new__(module.BossTimers)
    content = cog._build_alert_message_content(
        "Static Boss",
        {"alert_mention": "<@&1234567890>"},
    )
    assert content.startswith("<@&1234567890> ")


def test_build_alert_message_defaults_to_here():
    cog = module.BossTimers.__new__(module.BossTimers)
    content = cog._build_alert_message_content("Normal Boss", {"alert_seconds": 300})
    assert content.startswith("@here ")


def test_build_alert_message_normalizes_double_at_everyone():
    cog = module.BossTimers.__new__(module.BossTimers)
    content = cog._build_alert_message_content("Tester", {"alert_mention": "@@everyone"})
    assert content.startswith("@everyone ")


def test_normalize_alert_mention_supports_plain_role_names():
    normalized = module.BossTimers._normalize_alert_mention("LW2")
    assert normalized == "@LW2"


def test_normalize_alert_mention_collapses_repeated_ats_for_roles():
    normalized = module.BossTimers._normalize_alert_mention("@@@LW")
    assert normalized == "@LW"


def test_schedule_static_event_normalizes_alert_mention():
    cog = module.BossTimers.__new__(module.BossTimers)
    cog.boss_timers = {}
    cog._get_next_occurrence = lambda event, after=None: 2000

    event = {
        "id": "event-1",
        "name": "Static Boss",
        "image": "data/static_images/test.png",
        "schedule": "daily",
        "time": "20:00",
        "alert_mention": "@@everyone",
    }

    cog._schedule_static_event(event)
    timer = cog.boss_timers[2000]
    assert timer["alert_mention"] == "@everyone"


def test_normalize_alert_mention_collapses_repeated_ats_for_everyone():
    normalized = module.BossTimers._normalize_alert_mention("@@@everyone")
    assert normalized == "@everyone"


def test_read_attachment_with_retries_succeeds_after_transient_failure():
    class _AttachmentStub:
        def __init__(self):
            self.calls = 0

        async def read(self):
            self.calls += 1
            if self.calls < 2:
                raise OSError("temporary network issue")
            return b"ok"

    cog = module.BossTimers.__new__(module.BossTimers)
    payload = asyncio.run(cog._read_attachment_with_retries(_AttachmentStub(), max_attempts=3, initial_delay=0))
    assert payload == b"ok"


def test_read_attachment_with_retries_raises_after_max_attempts():
    class _AttachmentStub:
        async def read(self):
            raise OSError("cdn unavailable")

    cog = module.BossTimers.__new__(module.BossTimers)

    try:
        asyncio.run(cog._read_attachment_with_retries(_AttachmentStub(), max_attempts=2, initial_delay=0))
    except RuntimeError as exc:
        assert "after 2 attempts" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError after retry exhaustion")


def test_has_management_permission_returns_true_for_allowed_role(monkeypatch):
    monkeypatch.setattr(module, "ALLOWED_BOSS_MANAGER_ROLE_ID", 1522906832492822688)
    interaction = SimpleNamespace(
        user=SimpleNamespace(roles=[SimpleNamespace(id=111), SimpleNamespace(id=1522906832492822688)])
    )
    assert module.BossTimers._has_management_permission(interaction) is True


def test_has_management_permission_returns_false_without_allowed_role(monkeypatch):
    monkeypatch.setattr(module, "ALLOWED_BOSS_MANAGER_ROLE_ID", 1522906832492822688)
    interaction = SimpleNamespace(
        user=SimpleNamespace(roles=[SimpleNamespace(id=111), SimpleNamespace(id=222)])
    )
    assert module.BossTimers._has_management_permission(interaction) is False


def test_has_management_permission_returns_false_when_role_id_not_configured(monkeypatch):
    monkeypatch.setattr(module, "ALLOWED_BOSS_MANAGER_ROLE_ID", 0)
    interaction = SimpleNamespace(
        user=SimpleNamespace(roles=[SimpleNamespace(id=1522906832492822688)])
    )
    assert module.BossTimers._has_management_permission(interaction) is False
