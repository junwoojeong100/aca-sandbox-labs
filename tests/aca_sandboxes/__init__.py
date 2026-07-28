"""ACA Sandboxes backend tests."""

from pathlib import Path

_SOURCE_PACKAGE = Path(__file__).resolve().parents[2] / "aca_sandboxes"
__path__.append(str(_SOURCE_PACKAGE))
