"""Shared loading helpers so tests can import the cog without a package install."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ocr as ocr_module  # noqa: E402  (requires ROOT on sys.path)


def load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


boss_timers_module = load_module("boss_timers_module", "cogs/boss_timers.py")
