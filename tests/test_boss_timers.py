import importlib.util
from pathlib import Path

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


def test_timer_loop_interval_is_stable_at_15_seconds():
    cog = module.BossTimers.__new__(module.BossTimers)
    assert cog.manage_boss_timers_task.seconds == 15
