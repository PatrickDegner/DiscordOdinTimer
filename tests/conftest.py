import asyncio

import pytest

from helpers import ROOT, boss_timers_module, ocr_module


@pytest.fixture
def boss_module():
    return boss_timers_module


@pytest.fixture
def ocr():
    return ocr_module


@pytest.fixture
def repo_root():
    return ROOT


@pytest.fixture
def cog(tmp_path):
    """Bare cog instance with all filesystem state redirected into tmp_path."""
    instance = boss_timers_module.BossTimers.__new__(boss_timers_module.BossTimers)
    instance.bot = None
    instance.boss_timers = {}
    instance.static_events = {}
    instance.update_message_id = None
    instance.upcoming_message_id = None
    instance.last_update_event_key = None
    instance.UPDATE_MESSAGE_LOCKED = asyncio.Lock()
    instance.timers_file = tmp_path / "timers.json"
    instance.static_events_file = tmp_path / "static_events.json"
    instance.static_image_dir = tmp_path / "static_images"
    instance.boss_image_library_dir = tmp_path / "boss_images"
    instance.static_image_dir.mkdir(parents=True, exist_ok=True)
    instance.boss_image_library_dir.mkdir(parents=True, exist_ok=True)
    return instance
