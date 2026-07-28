"""Session Broker.

AI Workspace backend만 token과 session identifier를 다룬다.
identifier는 사용자 입력과 무관한 128-bit 이상 난수이며 사용자에게 노출하지 않는다.
"""

from __future__ import annotations

import json
import mimetypes
import posixpath
import secrets
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import config

_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
MAX_SANDBOX_DOWNLOAD_BYTES = 64 * 1024 * 1024


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


class SandboxesPythonSession:
    """ACA Sandboxes implementation of the PythonSession contract."""

    def __init__(
        self,
        settings: config.Settings,
        *,
        group_client: Any | None = None,
        credential: Any | None = None,
    ) -> None:
        self.settings = settings
        self._closed = False
        self._credential = credential
        self._owns_credential = credential is None
        self._client = group_client
        self._owns_client = group_client is None
        self._sandbox: Any | None = None

        try:
            if self._client is None:
                try:
                    from azure.containerapps.sandbox import (
                        SandboxGroupClient,
                        endpoint_for_region,
                    )
                    from azure.identity import DefaultAzureCredential
                except ImportError as error:
                    raise BrokerError(
                        "ACA Sandboxes SDK가 없다. "
                        "pip install azure-containerapps-sandbox를 실행한다."
                    ) from error
                if self._credential is None:
                    self._credential = DefaultAzureCredential(
                        exclude_interactive_browser_credential=True
                    )
                self._client = SandboxGroupClient(
                    endpoint_for_region(settings.location),
                    self._credential,
                    subscription_id=settings.resolved_subscription_id(),
                    resource_group=settings.resource_group,
                    sandbox_group=settings.sandbox_group_name,
                )

            self._sandbox = self._create_sandbox()
            self.identifier = str(self._sandbox.sandbox_id)
            try:
                self._sandbox.mkdir("/mnt/data")
            except Exception as error:
                if "FileAlreadyExists" not in str(error):
                    raise
        except Exception as error:
            cleanup_error: Exception | None = None
            if self._sandbox is not None:
                try:
                    self._sandbox.delete()
                except Exception as cleanup:
                    cleanup_error = cleanup
                finally:
                    self._sandbox.close()
            self._close_clients()
            if isinstance(error, BrokerError):
                raise
            suffix = (
                "; server-side auto-delete will retry cleanup"
                if cleanup_error is not None
                else ""
            )
            raise BrokerError(
                f"ACA Sandbox 생성 실패: {error}{suffix}"
            ) from error

    def _create_sandbox(self) -> Any:
        try:
            from azure.containerapps.sandbox import (
                AutoDeletePolicy,
                AutoSuspendPolicy,
                EgressPolicy,
                LifecyclePolicy,
            )
        except ImportError as error:
            raise BrokerError("ACA Sandboxes SDK가 없다") from error

        arguments: dict[str, Any] = {
            "cpu": "1000m",
            "memory": "2048Mi",
            "auto_suspend_seconds": 1800,
            "auto_suspend_mode": "Memory",
            "egress_policy": EgressPolicy(
                default_action="Deny",
                traffic_inspection="Full",
            ),
            "labels": {
                "component": "python-gateway",
                "gateway-request": secrets.token_hex(8),
            },
        }
        disk_image_id = (
            self.settings.python_sandbox_disk_id
            or self._latest_python_disk_image()
        )
        if not disk_image_id:
            raise BrokerError(
                "Ready 상태의 python-code-interpreter disk image가 없다"
            )
        arguments.update(
            disk=None,
            disk_id=disk_image_id,
        )
        label = arguments["labels"]["gateway-request"]
        sandbox = None
        try:
            sandbox = self._client.begin_create_sandbox(**arguments).result()
            sandbox.set_lifecycle_policy(
                LifecyclePolicy(
                    auto_suspend=AutoSuspendPolicy(
                        enabled=True,
                        interval=1800,
                        mode="Memory",
                    ),
                    auto_delete=AutoDeletePolicy(
                        enabled=True,
                        delete_interval_seconds=3600,
                    ),
                ),
            )
            return sandbox
        except Exception:
            delete_sandboxes_by_label(
                self._client,
                "gateway-request",
                label,
                known_sandbox=sandbox,
            )
            raise

    def _latest_python_disk_image(self) -> str | None:
        images = [
            image
            for image in self._client.list_disk_images()
            if image.labels.get("name", "").startswith(
                "python-code-interpreter-"
            )
            and image.status
            and image.status.state in {"Ready", "Succeeded"}
        ]
        if not images:
            return None
        return max(images, key=lambda image: image.labels["name"]).id

    @staticmethod
    def _safe_name(name: str) -> str:
        if (
            not name
            or posixpath.basename(name) != name
            or name in {".", ".."}
        ):
            raise BrokerError(f"안전하지 않은 파일 이름: {name}")
        return name

    def execute(self, code: str, *, timeout: int = 300) -> ExecutionResult:
        if self._closed or self._sandbox is None:
            raise BrokerError("ACA Sandbox가 이미 종료됐다")
        script_path = f"/tmp/ai-workspace-{secrets.token_hex(8)}.py"
        try:
            self._sandbox.write_file(script_path, code)
        except Exception as error:
            raise BrokerError(
                f"ACA Sandbox 실행 코드 업로드 실패: {error}"
            ) from error
        command = (
            f"timeout --signal=KILL {max(1, int(timeout))}s "
            f"python3 {shlex.quote(script_path)}"
        )
        try:
            result = self._sandbox.exec(command)
        except Exception as error:
            raise BrokerError(f"ACA Sandbox Python 실행 실패: {error}") from error
        finally:
            try:
                self._sandbox.delete_file(script_path)
            except Exception:
                pass
        stderr = str(result.stderr or "")
        if result.exit_code == 124:
            stderr = stderr or f"Execution timed out after {timeout} seconds"
        return ExecutionResult(
            status="Succeeded" if result.exit_code == 0 else "Failed",
            stdout=str(result.stdout or ""),
            stderr=stderr,
            result={"exitCode": result.exit_code},
            raw={"exitCode": result.exit_code},
        )

    def upload(self, name: str, content: bytes) -> dict[str, Any]:
        name = self._safe_name(name)
        if len(content) > 128 * 1024 * 1024:
            raise BrokerError(f"업로드 한도 128MB 초과: {name}")
        try:
            self._sandbox.write_file(f"/mnt/data/{name}", content)
        except Exception as error:
            raise BrokerError(f"{name} 업로드 실패: {error}") from error
        return {"name": name, "size": len(content)}

    def list_files(self) -> dict[str, Any]:
        try:
            listing = self._sandbox.list_files("/mnt/data")
        except Exception as error:
            raise BrokerError(f"파일 목록 조회 실패: {error}") from error
        return {
            "value": [
                {
                    "name": entry.name,
                    "size": entry.size,
                    "isDirectory": entry.is_directory,
                }
                for entry in listing.entries
                if not entry.is_directory
            ]
        }

    def download(self, name: str) -> bytes:
        name = self._safe_name(name)
        source_path = f"/mnt/data/{name}"
        snapshot_path = f"/tmp/download-{secrets.token_hex(16)}"
        try:
            command = (
                f"test -f {shlex.quote(source_path)} && "
                f"test ! -L {shlex.quote(source_path)} && "
                f"timeout --signal=KILL 30s head -c "
                f"{MAX_SANDBOX_DOWNLOAD_BYTES + 1} -- "
                f"{shlex.quote(source_path)} > {shlex.quote(snapshot_path)}"
            )
            copied = self._sandbox.exec(command)
            if copied.exit_code != 0:
                raise BrokerError(
                    f"{name} bounded snapshot 생성 실패: {copied.stderr}"
                )
            metadata = self._sandbox.stat_file(snapshot_path)
            if (
                not isinstance(metadata.size, int)
                or metadata.size < 0
                or metadata.size > MAX_SANDBOX_DOWNLOAD_BYTES
            ):
                raise BrokerError(
                    f"{name} 다운로드 크기 metadata가 허용 범위를 벗어났다: "
                    f"{metadata.size}"
                )
            payload = self._sandbox.read_file(snapshot_path)
            if len(payload) != metadata.size:
                raise BrokerError(f"{name} bounded snapshot 크기가 변경됐다")
            return payload
        except Exception as error:
            if isinstance(error, BrokerError):
                raise
            raise BrokerError(f"{name} 다운로드 실패: {error}") from error
        finally:
            try:
                self._sandbox.delete_file(snapshot_path)
            except Exception:
                pass

    def info(self) -> dict[str, Any]:
        try:
            resource = self._sandbox.get()
        except Exception as error:
            raise BrokerError(f"ACA Sandbox 조회 실패: {error}") from error
        return {
            "id": self.identifier,
            "state": resource.state,
            "region": resource.region,
        }

    def delete(self) -> int:
        if self._closed:
            return 0
        try:
            if self._sandbox is not None:
                self._sandbox.delete()
        except Exception as error:
            if "NotFound" not in str(error) and "not found" not in str(
                error
            ).lower():
                raise BrokerError(f"ACA Sandbox 삭제 실패: {error}") from error
        self._closed = True
        if self._sandbox is not None:
            self._sandbox.close()
        self._close_clients()
        return 204

    def _close_clients(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
        if self._owns_credential and self._credential is not None:
            close = getattr(self._credential, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> SandboxesPythonSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            self.delete()
        except BrokerError:
            pass


def create_python_session(settings: config.Settings) -> PythonSession | SandboxesPythonSession:
    """Create the configured Python execution backend."""
    settings.validate_execution_backend()
    if settings.execution_backend == "sandboxes":
        return SandboxesPythonSession(settings)
    return PythonSession(settings.resolved_python_endpoint())


def _is_not_found(error: Exception) -> bool:
    text = str(error)
    return "NotFound" in text or "not found" in text.lower()


def delete_sandboxes_by_label(
    group_client: Any,
    key: str,
    value: str,
    *,
    known_sandbox: Any | None = None,
    attempts: int = 12,
    delay_seconds: int = 5,
) -> None:
    """Delete partially created Sandboxes, retrying conflicts and visibility lag."""
    last_error: Exception | None = None
    known_delete_error: Exception | None = None
    if known_sandbox is not None:
        try:
            for attempt in range(attempts):
                try:
                    known_sandbox.delete()
                    return
                except Exception as error:
                    if _is_not_found(error):
                        return
                    last_error = error
                    known_delete_error = error
                    if attempt + 1 < attempts:
                        time.sleep(delay_seconds)
        finally:
            known_sandbox.close()

    for attempt in range(attempts):
        try:
            resources = [
                resource
                for resource in group_client.list_sandboxes()
                if getattr(resource, "labels", {}).get(key) == value
            ]
            if known_delete_error is None:
                last_error = None
        except Exception as error:
            last_error = error
            resources = []
        if resources:
            all_deleted = True
            for resource in resources:
                sandbox = group_client.get_sandbox_client(resource.id)
                try:
                    sandbox.delete()
                except Exception as error:
                    if not _is_not_found(error):
                        last_error = error
                        all_deleted = False
                finally:
                    sandbox.close()
            if all_deleted:
                return
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    if last_error is not None:
        raise BrokerError(
            f"부분 생성된 ACA Sandbox 정리 실패: {last_error}"
        ) from last_error


def cleanup_gateway_sandboxes(
    settings: config.Settings,
    component: str,
    *,
    group_client: Any | None = None,
    credential: Any | None = None,
    exclude_ids: set[str] | None = None,
) -> int:
    """Delete orphaned Sandboxes for one gateway component.

    Gateway job maps are in memory. On process startup no prior job can be
    resumed safely, so labeled Sandboxes from a previous process are removed.
    """
    settings.validate_execution_backend()
    if settings.execution_backend != "sandboxes":
        return 0
    owns_credential = credential is None
    owns_client = group_client is None
    if group_client is None:
        try:
            from azure.containerapps.sandbox import (
                SandboxGroupClient,
                endpoint_for_region,
            )
            from azure.identity import DefaultAzureCredential
        except ImportError as error:
            raise BrokerError("ACA Sandboxes SDK가 없다") from error
        if credential is None:
            credential = DefaultAzureCredential(
                exclude_interactive_browser_credential=True
            )
        group_client = SandboxGroupClient(
            endpoint_for_region(settings.location),
            credential,
            subscription_id=settings.resolved_subscription_id(),
            resource_group=settings.resource_group,
            sandbox_group=settings.sandbox_group_name,
        )
    deleted = 0
    cleanup_errors: list[str] = []
    now = time.time()
    excluded = exclude_ids or set()
    try:
        for resource in list(group_client.list_sandboxes()):
            if getattr(resource, "labels", {}).get("component") != component:
                continue
            if resource.id in excluded:
                continue
            if getattr(resource, "state", None) not in {
                "Stopped",
                "Suspended",
                "Failed",
            }:
                continue
            created_at = getattr(resource, "created_at", None)
            created_timestamp = _created_timestamp(created_at)
            if created_timestamp is None:
                continue
            if now - created_timestamp < 3600:
                continue
            sandbox = group_client.get_sandbox_client(resource.id)
            try:
                sandbox.delete()
                deleted += 1
            except Exception as error:
                if not _is_not_found(error):
                    cleanup_errors.append(f"{resource.id}: {error}")
            finally:
                sandbox.close()
    finally:
        if owns_client:
            group_client.close()
        if owns_credential and credential is not None:
            close = getattr(credential, "close", None)
            if callable(close):
                close()
    if cleanup_errors:
        raise BrokerError(
            "orphaned ACA Sandbox 일부 삭제 실패: "
            + "; ".join(cleanup_errors)
        )
    return deleted


def start_gateway_cleanup_loop(
    settings: config.Settings,
    component: str,
    *,
    interval_seconds: int = 300,
    exclude_ids_provider: Any | None = None,
) -> threading.Thread:
    """Periodically remove stale gateway Sandboxes without blocking startup."""

    def cleanup_loop() -> None:
        while True:
            time.sleep(interval_seconds)
            try:
                excluded = (
                    set(exclude_ids_provider())
                    if callable(exclude_ids_provider)
                    else set()
                )
                cleanup_gateway_sandboxes(
                    settings,
                    component,
                    exclude_ids=excluded,
                )
            except Exception as error:
                print(
                    json.dumps(
                        {
                            "level": "warning",
                            "message": f"{component} orphan cleanup failed",
                            "error": str(error),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    thread = threading.Thread(
        target=cleanup_loop,
        name=f"{component}-sandbox-reaper",
        daemon=True,
    )
    thread.start()
    return thread


def _created_timestamp(value: Any) -> float | None:
    if isinstance(value, datetime):
        normalized = (
            value
            if value.tzinfo is not None
            else value.replace(tzinfo=timezone.utc)
        )
        return normalized.timestamp()
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


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
