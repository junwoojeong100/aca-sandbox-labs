"""Dynamic Sessions Code Interpreter client."""

from __future__ import annotations

import json
import mimetypes
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from agent import auth, execution, ids
from . import config


def _request(
    method: str,
    url: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 300,
) -> tuple[int, bytes, str]:
    try:
        token = auth.get_token(config.DYNAMIC_SESSIONS_SCOPE)
    except (
        json.JSONDecodeError,
        KeyError,
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as error:
        raise execution.ExecutionError(
            "Dynamic Sessions access token acquisition failed"
        ) from error
    request_headers = {"Authorization": f"Bearer {token}"}
    request_headers.update(headers or {})
    request = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (
                response.status,
                response.read(),
                response.headers.get("Content-Type", ""),
            )
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers.get("Content-Type", "")
    except urllib.error.URLError as error:
        raise execution.ExecutionError(
            f"Dynamic Sessions endpoint connection failed: {error.reason}"
        ) from error
    except (TimeoutError, ConnectionError, OSError) as error:
        raise execution.ExecutionError(
            "Dynamic Sessions transport failed"
        ) from error


def _json_or_raise(
    status: int,
    payload: bytes,
    operation: str,
) -> dict[str, Any]:
    text = payload.decode("utf-8", errors="replace")
    if not 200 <= status < 300:
        raise execution.ExecutionError(
            f"{operation} failed (HTTP {status})",
            status,
            text[:2000],
        )
    if not text.strip():
        return {}
    try:
        result = json.loads(text)
    except json.JSONDecodeError as error:
        raise execution.ExecutionError(
            f"{operation} returned non-JSON data",
            status,
            text[:2000],
        ) from error
    if not isinstance(result, dict):
        raise execution.ExecutionError(f"{operation} response must be an object")
    return result


class PythonSession:
    def __init__(
        self,
        settings: config.Settings,
        identifier: str | None = None,
    ) -> None:
        self.settings = settings
        try:
            self.endpoint = settings.resolved_python_endpoint().rstrip("/")
        except (
            OSError,
            RuntimeError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as error:
            raise execution.ExecutionError(
                "Dynamic Sessions endpoint resolution failed"
            ) from error
        self.identifier = identifier or ids.new_identifier("py")
        self._closed = False

    def _url(self, path: str, api_version: str | None = None) -> str:
        query = urllib.parse.urlencode(
            {
                "api-version": api_version or self.settings.python_api_version,
                "identifier": self.identifier,
            }
        )
        return f"{self.endpoint}/{path.lstrip('/')}?{query}"

    def execute(
        self,
        code: str,
        *,
        timeout: int = 300,
    ) -> execution.ExecutionResult:
        properties = {
            "codeInputType": "inline",
            "executionType": "synchronous",
            "code": code,
        }
        status = 0
        payload = b""
        for wrapped in (False, True):
            request_body = {"properties": properties} if wrapped else properties
            status, payload, _ = _request(
                "POST",
                self._url("executions"),
                body=json.dumps(request_body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
            if status != 400 or wrapped:
                break
            try:
                error_code = json.loads(payload).get("error", {}).get("code")
            except (json.JSONDecodeError, AttributeError):
                error_code = None
            if error_code != "SessionPropertiesMissing":
                break
        data = _json_or_raise(status, payload, "Python execution")
        result = data.get("result") or {}
        if not isinstance(result, dict):
            result = {"executionResult": result}
        return execution.ExecutionResult(
            status=str(data.get("status", "Unknown")),
            stdout=str(result.get("stdout", "")),
            stderr=str(result.get("stderr", "")),
            result=result.get("executionResult"),
            raw=data,
        )

    def upload(self, name: str, content: bytes) -> dict[str, Any]:
        if len(content) > 128 * 1024 * 1024:
            raise execution.ExecutionError(f"Upload exceeds 128MB: {name}")
        boundary = f"----aiworkspace{uuid.uuid4().hex}"
        content_type = (
            mimetypes.guess_type(name)[0] or "application/octet-stream"
        )
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="file"; '
                    f'filename="{name}"\r\n'
                    f"Content-Type: {content_type}\r\n\r\n"
                ).encode(),
                content,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        status, payload, _ = _request(
            "POST",
            self._url("files"),
            body=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}"
            },
        )
        return _json_or_raise(status, payload, f"Upload {name}")

    def list_files(self) -> dict[str, Any]:
        status, payload, _ = _request("GET", self._url("files"))
        return _json_or_raise(status, payload, "List files")

    def download(self, name: str) -> bytes:
        encoded = urllib.parse.quote(name)
        status, payload, _ = _request(
            "GET",
            self._url(f"files/{encoded}/content"),
        )
        if not 200 <= status < 300:
            raise execution.ExecutionError(
                f"Download failed for {name} (HTTP {status})",
                status,
                payload.decode("utf-8", errors="replace")[:2000],
            )
        return payload

    def info(self) -> dict[str, Any]:
        status, payload, _ = _request(
            "GET",
            self._url("session", self.settings.session_api_version),
        )
        return _json_or_raise(status, payload, "Get session")

    def delete(self) -> int:
        if self._closed:
            return 0
        status, _, _ = _request(
            "DELETE",
            self._url("session", self.settings.session_api_version),
        )
        if status in (200, 202, 204, 404):
            self._closed = True
            return status
        raise execution.ExecutionError(
            f"Dynamic Sessions delete failed (HTTP {status})",
            status,
        )
