"""Dynamic Sessions backend tests."""

from pathlib import Path

_SOURCE_PACKAGE = Path(__file__).resolve().parents[2] / "dynamic_sessions"
__path__.append(str(_SOURCE_PACKAGE))
