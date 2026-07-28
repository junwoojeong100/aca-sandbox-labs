"""Backend-only opaque identifier helpers."""

from __future__ import annotations

import secrets


def new_identifier(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(16)}"
