"""Backend-only Microsoft Entra token acquisition."""

from __future__ import annotations

import json
import subprocess
import time

_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def get_token(resource: str) -> str:
    """Get an Azure access token without exposing it to users or prompts."""
    cached = _TOKEN_CACHE.get(resource)
    if cached and cached[1] > time.time() + 60:
        return cached[0]

    result = subprocess.run(
        [
            "az",
            "account",
            "get-access-token",
            "--resource",
            resource,
            "--output",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    payload = json.loads(result.stdout)
    token = payload["accessToken"]
    _TOKEN_CACHE[resource] = (token, time.time() + 600)
    return token
