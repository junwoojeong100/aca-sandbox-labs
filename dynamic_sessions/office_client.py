"""Dynamic Sessions Office Custom Container client."""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from agent import auth
from office_gateway.service import GatewayError
from . import config


class OfficeClient:
    def __init__(
        self,
        settings: config.Settings,
        identifier: str,
    ) -> None:
        self.settings = settings
        try:
            self.endpoint = settings.resolved_office_endpoint().rstrip("/")
        except (
            OSError,
            RuntimeError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as error:
            raise GatewayError(
                502,
                "Office session endpoint resolution failed",
            ) from error
        self.identifier = identifier

    def _url(self, path: str, **query: str) -> str:
        query["identifier"] = self.identifier
        return (
            f"{self.endpoint}/{path.lstrip('/')}?"
            f"{urllib.parse.urlencode(query)}"
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        query: dict[str, str] | None = None,
    ) -> tuple[int, bytes, str]:
        body = None
        try:
            token = auth.get_token(config.DYNAMIC_SESSIONS_SCOPE)
        except (
            json.JSONDecodeError,
            KeyError,
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as error:
            raise GatewayError(
                502,
                "Office session access token acquisition failed",
            ) from error
        headers = {"Authorization": f"Bearer {token}"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._url(path, **(query or {})),
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return (
                    response.status,
                    response.read(),
                    response.headers.get("Content-Type", ""),
                )
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers.get("Content-Type", "")
        except urllib.error.URLError as error:
            raise GatewayError(
                502,
                f"Office session connection failed: {error.reason}",
            ) from error
        except (TimeoutError, ConnectionError, OSError) as error:
            raise GatewayError(
                502,
                "Office session transport failed",
            ) from error

    def _json(
        self,
        method: str,
        path: str,
        payload: dict[str, object],
    ) -> dict[str, Any]:
        status, body, _ = self._request(method, path, payload=payload)
        text = body.decode("utf-8", errors="replace")
        if not 200 <= status < 300:
            try:
                error_payload = json.loads(text)
                detail = error_payload.get("error")
                details = {
                    key: error_payload[key]
                    for key in ("allowed",)
                    if key in error_payload
                }
            except (json.JSONDecodeError, AttributeError, TypeError):
                detail = None
                details = {}
            raise GatewayError(
                status,
                str(detail or "Office operation failed"),
                details,
            )
        try:
            result = json.loads(text)
        except json.JSONDecodeError as error:
            raise GatewayError(502, "Office session returned non-JSON data") from error
        if not isinstance(result, dict):
            raise GatewayError(502, "Office session response must be an object")
        return result

    def generate(self, title: str, content: str) -> dict[str, Any]:
        return self._json(
            "POST",
            "/generate",
            {"title": title, "content": content},
        )

    def convert(
        self,
        job_id: str,
        source: str,
        target: str,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/convert",
            {"jobId": job_id, "source": source, "target": target},
        )

    def edit(
        self,
        job_id: str,
        operations: list[dict[str, object]],
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/edit",
            {"jobId": job_id, "operations": operations},
        )

    def download(self, path: str) -> tuple[bytes, str]:
        status, body, content_type = self._request("GET", path)
        if not 200 <= status < 300:
            raise GatewayError(status, "Office result download failed")
        return body, content_type or "application/octet-stream"

    def stop(self) -> None:
        status, _, _ = self._request(
            "POST",
            "/.management/stopSession",
            query={"api-version": self.settings.session_api_version},
        )
        if status not in (200, 202, 204, 404):
            raise GatewayError(status, "Office session stop failed")
