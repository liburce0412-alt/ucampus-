"""Prefer the bundled runtime; preserve the installed-skill fallback for local upgrades."""
from pathlib import Path

_bundled = Path(__file__).resolve().parent / 'skill'
SKILL = _bundled if (_bundled / 'scripts/runtime.py').is_file() else Path.home() / '.codex/skills/ucampus-adb'
