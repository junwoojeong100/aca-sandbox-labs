"""Session Broker.

AI Workspace backend만 token과 session identifier를 다룬다.
identifier는 사용자 입력과 무관한 128-bit 이상 난수이며 사용자에게 노출하지 않는다.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import config

_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


class BrokerError(RuntimeError):
    """Session API 호출 실패."""

    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def get_token(scope: str = config.DYNAMIC_SESSIONS_SCOPE) -> str:
    """Azure CLI 로그인 또는 Managed Identity로 access token을 얻는다.

    실제 배포에서는 azure-identity의 DefaultAzureCredential을 사용한다.
    이 예제는 추가 dependency 없이 동작하도록 Azure CLI를 사용한다.
    """
    cached = _TOKEN_CACHE.get(scope)
    if cached and cached[1] > time.time() + 60:
        return cached[0]

    result = subprocess.run(
        [
            "az",
            "account",
            "get-access-token",
            "--resource",
            scope,
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
    # expiresOn 형식이 환경마다 달라 보수적으로 10분만 캐시한다.
    _TOKEN_CACHE[scope] = (token, time.time() + 600)
    return token


def new_session_identifier(prefix: str) -> str:
    """예측 불가능한 session identifier를 만든다.

    사용자 ID, 순번, 대화 제목을 identifier에 넣지 않는다.
    """
    return f"{prefix}-{secrets.token_hex(16)}"


def _request(
    method: str,
    url: str,
    *,
    token: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 300,
) -> tuple[int, bytes, str]:
    request_headers = {"Authorization": f"Bearer {token}"}
    request_headers.update(headers or {})
    request = urllib.request.Request(
        url, data=body, headers=request_headers, method=method
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
    except urllib.error.URLError as error:  # pragma: no cover - 네트워크 장애
        raise BrokerError(f"Session endpoint에 연결하지 못했다: {error.reason}") from error


def _json_or_raise(status: int, payload: bytes, operation: str) -> dict[str, Any]:
    text = payload.decode("utf-8", errors="replace")
    if not 200 <= status < 300:
        raise BrokerError(f"{operation} 실패 (HTTP {status})", status, text[:2000])
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise BrokerError(f"{operation} 응답이 JSON이 아니다", status, text[:2000]) from error


@dataclass
class ExecutionResult:
    status: str
    stdout: str
    stderr: str
    result: Any = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        """실행 성공 여부.

        `stderr`가 비어 있는지로 판단하지 않는다. 정상 동작하는 Python 코드도
        경고(matplotlib font, pandas UserWarning 등)를 stderr로 출력하기 때문이다.
        플랫폼이 돌려주는 `status`만 신뢰한다.
        """
        return self.status == "Succeeded"

    @property
    def warnings(self) -> str:
        """성공했지만 stderr에 남은 경고."""
        return self.stderr.strip() if self.succeeded else ""


class PythonSession:
    """Python Code Interpreter session 하나에 대한 backend-only 핸들."""

    def __init__(self, endpoint: str, identifier: str | None = None) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.identifier = identifier or new_session_identifier("py")
        self._closed = False

    def _url(self, path: str, api_version: str = config.PYTHON_API_VERSION) -> str:
        query = urllib.parse.urlencode(
            {"api-version": api_version, "identifier": self.identifier}
        )
        return f"{self.endpoint}/{path.lstrip('/')}?{query}"

    def execute(self, code: str, *, timeout: int = 300) -> ExecutionResult:
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
                token=get_token(),
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
        data = _json_or_raise(status, payload, "Python 실행")
        result = data.get("result") or {}
        if not isinstance(result, dict):
            result = {"executionResult": result}
        return ExecutionResult(
            status=str(data.get("status", "Unknown")),
            stdout=str(result.get("stdout", "")),
            stderr=str(result.get("stderr", "")),
            result=result.get("executionResult"),
            raw=data,
        )

    def upload(self, name: str, content: bytes) -> dict[str, Any]:
        if len(content) > 128 * 1024 * 1024:
            raise BrokerError(f"업로드 한도 128MB 초과: {name}")
        boundary = f"----aiworkspace{uuid.uuid4().hex}"
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        parts = [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode(),
            content,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        status, payload, _ = _request(
            "POST",
            self._url("files"),
            token=get_token(),
            body=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        return _json_or_raise(status, payload, f"{name} 업로드")

    def list_files(self) -> dict[str, Any]:
        status, payload, _ = _request("GET", self._url("files"), token=get_token())
        return _json_or_raise(status, payload, "파일 목록 조회")

    def download(self, name: str) -> bytes:
        encoded = urllib.parse.quote(name)
        status, payload, _ = _request(
            "GET", self._url(f"files/{encoded}/content"), token=get_token()
        )
        if not 200 <= status < 300:
            raise BrokerError(
                f"{name} 다운로드 실패 (HTTP {status})",
                status,
                payload.decode("utf-8", errors="replace")[:2000],
            )
        return payload

    def info(self) -> dict[str, Any]:
        status, payload, _ = _request(
            "GET",
            self._url("session", config.SESSION_API_VERSION),
            token=get_token(),
        )
        return _json_or_raise(status, payload, "session 조회")

    def delete(self) -> int:
        """session과 임시 파일을 즉시 회수한다."""
        if self._closed:
            return 0
        status, _, _ = _request(
            "DELETE",
            self._url("session", config.SESSION_API_VERSION),
            token=get_token(),
        )
        self._closed = True
        return status

    def __enter__(self) -> PythonSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            self.delete()
        except BrokerError:  # pragma: no cover - 정리 실패는 치명적이지 않다
            pass


class OfficeSession:
    """Office Custom Container session 핸들."""

    def __init__(self, endpoint: str, identifier: str | None = None) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.identifier = identifier or new_session_identifier("office")
        self._closed = False

    def _url(self, path: str, api_version: str | None = None) -> str:
        parameters = {"identifier": self.identifier}
        if api_version:
            parameters["api-version"] = api_version
        return f"{self.endpoint}/{path.lstrip('/')}?{urllib.parse.urlencode(parameters)}"

    def health(self) -> dict[str, Any]:
        status, payload, _ = _request("GET", self._url("health"), token=get_token())
        return _json_or_raise(status, payload, "Office health")

    def generate(self, title: str, content: str) -> dict[str, Any]:
        body = json.dumps({"title": title, "content": content}).encode("utf-8")
        status, payload, _ = _request(
            "POST",
            self._url("generate"),
            token=get_token(),
            body=body,
            headers={"Content-Type": "application/json"},
        )
        return _json_or_raise(status, payload, "Office 생성")

    def convert(self, job_id: str, source: str, target: str) -> dict[str, Any]:
        body = json.dumps(
            {"jobId": job_id, "source": source, "target": target}
        ).encode("utf-8")
        status, payload, _ = _request(
            "POST",
            self._url("convert"),
            token=get_token(),
            body=body,
            headers={"Content-Type": "application/json"},
        )
        return _json_or_raise(status, payload, "Office 변환")

    def edit(self, job_id: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
        body = json.dumps({"jobId": job_id, "operations": operations}).encode("utf-8")
        status, payload, _ = _request(
            "POST",
            self._url("edit"),
            token=get_token(),
            body=body,
            headers={"Content-Type": "application/json"},
        )
        return _json_or_raise(status, payload, "Office 편집")

    def download(self, download_path: str) -> bytes:
        url = f"{self.endpoint}{download_path}?" + urllib.parse.urlencode(
            {"identifier": self.identifier}
        )
        status, payload, _ = _request("GET", url, token=get_token())
        if not 200 <= status < 300:
            raise BrokerError(
                f"{download_path} 다운로드 실패 (HTTP {status})",
                status,
                payload.decode("utf-8", errors="replace")[:2000],
            )
        return payload

    def stop(self) -> int:
        if self._closed:
            return 0
        status, _, _ = _request(
            "POST",
            self._url(".management/stopSession", config.SESSION_API_VERSION),
            token=get_token(),
        )
        self._closed = True
        return status

    def __enter__(self) -> OfficeSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            self.stop()
        except BrokerError:  # pragma: no cover
            pass
