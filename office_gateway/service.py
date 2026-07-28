"""Office user gateway service.

The gateway owns Azure tokens and session identifiers. User-facing responses
contain only public job IDs, safe file metadata, and approval state.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shlex
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from agent import broker, config, staging

SAFE_USER = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SAFE_FILE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
MAX_TITLE_CHARS = 200
MAX_CONTENT_CHARS = 100_000
OFFICE_JOB_TTL_SECONDS = 3600
MAX_ACTIVE_OFFICE_JOBS = 5


class GatewayError(RuntimeError):
    """A safe error that can be returned by the user-facing API."""

    def __init__(
        self,
        status: int,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details or {}


class OfficeBackendClient(Protocol):
    identifier: str

    def generate(self, title: str, content: str) -> dict[str, Any]: ...

    def convert(
        self,
        job_id: str,
        source: str,
        target: str,
    ) -> dict[str, Any]: ...

    def edit(
        self,
        job_id: str,
        operations: list[dict[str, object]],
    ) -> dict[str, Any]: ...

    def download(self, path: str) -> tuple[bytes, str]: ...

    def stop(self) -> None: ...


class OfficeSessionClient:
    """Backend-only client for one Office Custom Container session."""

    def __init__(self, endpoint: str, identifier: str) -> None:
        self.endpoint = endpoint.rstrip("/")
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
            token = broker.get_token()
        except (
            json.JSONDecodeError,
            KeyError,
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as error:
            raise GatewayError(502, "Office session 인증 token을 얻지 못했다") from error
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
            raise GatewayError(502, f"Office session 연결 실패: {error.reason}") from error
        except (TimeoutError, ConnectionError, OSError) as error:
            raise GatewayError(502, f"Office session 통신 실패: {error}") from error

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
                str(detail or "Office 작업이 실패했다"),
                details,
            )
        try:
            result = json.loads(text)
        except json.JSONDecodeError as error:
            raise GatewayError(502, "Office session이 JSON이 아닌 응답을 반환했다") from error
        if not isinstance(result, dict):
            raise GatewayError(502, "Office session 응답 형식이 올바르지 않다")
        return result

    def generate(self, title: str, content: str) -> dict[str, Any]:
        return self._json("POST", "/generate", {"title": title, "content": content})

    def convert(self, job_id: str, source: str, target: str) -> dict[str, Any]:
        return self._json(
            "POST",
            "/convert",
            {"jobId": job_id, "source": source, "target": target},
        )

    def edit(
        self, job_id: str, operations: list[dict[str, object]]
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/edit",
            {"jobId": job_id, "operations": operations},
        )

    def download(self, path: str) -> tuple[bytes, str]:
        status, body, content_type = self._request("GET", path)
        if not 200 <= status < 300:
            raise GatewayError(status, "Office 결과 파일을 다운로드하지 못했다")
        return body, content_type or "application/octet-stream"

    def stop(self) -> None:
        status, _, _ = self._request(
            "POST",
            "/.management/stopSession",
            query={"api-version": config.SESSION_API_VERSION},
        )
        if status not in (200, 202, 204, 404):
            raise GatewayError(status, "Office session을 종료하지 못했다")


class SandboxOfficeClient:
    """ACA Sandboxes backend for one Office document job."""

    DOWNLOAD_PATH = re.compile(
        r"^/files/([A-Za-z0-9]{1,64})/([A-Za-z0-9._-]{1,100})$"
    )

    @property
    def sandbox_id(self) -> str:
        return str(self._sandbox.sandbox_id)

    def __init__(
        self,
        identifier: str,
        *,
        subscription_id: str,
        resource_group: str,
        location: str,
        sandbox_group: str,
        disk_image_id: str | None = None,
        group_client: Any | None = None,
        credential: Any | None = None,
    ) -> None:
        self.identifier = identifier
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
                    raise GatewayError(
                        500,
                        "ACA Sandboxes SDK가 설치되지 않았다",
                    ) from error
                if self._credential is None:
                    self._credential = DefaultAzureCredential(
                        exclude_interactive_browser_credential=True
                    )
                self._client = SandboxGroupClient(
                    endpoint_for_region(location),
                    self._credential,
                    subscription_id=subscription_id,
                    resource_group=resource_group,
                    sandbox_group=sandbox_group,
                )
            disk_image_id = disk_image_id or self._latest_office_disk_image()
            self._sandbox = self._create_sandbox(disk_image_id)
            runner_source = (
                Path(__file__)
                .with_name("sandbox_runner.py")
                .read_text(encoding="utf-8")
            )
            self._sandbox.write_file(
                "/tmp/office_gateway_runner.py",
                runner_source,
            )
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
            if isinstance(error, GatewayError):
                raise
            raise GatewayError(
                502,
                (
                    "Office Sandbox 생성에 실패했다"
                    + (
                        "; server-side auto-delete가 정리를 재시도한다"
                        if cleanup_error is not None
                        else ""
                    )
                ),
            ) from error

    def _latest_office_disk_image(self) -> str:
        images = [
            image
            for image in self._client.list_disk_images()
            if image.labels.get("name", "").startswith("office-")
            and image.status
            and image.status.state in {"Ready", "Succeeded"}
        ]
        if not images:
            raise GatewayError(500, "Ready 상태의 Office disk image가 없다")
        return max(images, key=lambda image: image.labels["name"]).id

    def _create_sandbox(self, disk_image_id: str) -> Any:
        try:
            from azure.containerapps.sandbox import (
                AutoDeletePolicy,
                AutoSuspendPolicy,
                EgressPolicy,
                LifecyclePolicy,
            )
        except ImportError as error:
            raise GatewayError(500, "ACA Sandboxes SDK가 설치되지 않았다") from error
        request_label = uuid.uuid4().hex
        sandbox = None
        try:
            sandbox = self._client.begin_create_sandbox(
                disk=None,
                disk_id=disk_image_id,
                cpu="2000m",
                memory="4096Mi",
                auto_suspend_seconds=300,
                auto_suspend_mode="Memory",
                egress_policy=EgressPolicy(
                    default_action="Deny",
                    traffic_inspection="Full",
                ),
                labels={
                    "component": "office-gateway",
                    "gateway-request": request_label,
                },
            ).result()
            sandbox.set_lifecycle_policy(
                LifecyclePolicy(
                    auto_suspend=AutoSuspendPolicy(
                        enabled=True,
                        interval=300,
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
            broker.delete_sandboxes_by_label(
                self._client,
                "gateway-request",
                request_label,
                known_sandbox=sandbox,
            )
            raise

    def _invoke(
        self,
        action: str,
        payload: dict[str, object],
    ) -> dict[str, Any]:
        if self._closed or self._sandbox is None:
            raise GatewayError(410, "Office Sandbox가 이미 종료됐다")
        try:
            self._sandbox.ensure_running()
        except Exception as error:
            raise GatewayError(502, "Office Sandbox 재개에 실패했다") from error
        request_path = f"/tmp/office-request-{uuid.uuid4().hex}.json"
        try:
            self._sandbox.write_file(
                request_path,
                json.dumps(payload, ensure_ascii=False),
            )
        except Exception as error:
            raise GatewayError(
                502,
                "Office Sandbox 요청 업로드에 실패했다",
            ) from error
        command = " ".join(
            shlex.quote(value)
            for value in (
                "python3",
                "/tmp/office_gateway_runner.py",
                action,
                request_path,
            )
        )
        try:
            result = self._sandbox.exec(command)
        except Exception as error:
            raise GatewayError(502, "Office Sandbox 실행에 실패했다") from error
        finally:
            try:
                self._sandbox.delete_file(request_path)
            except Exception:
                pass
        try:
            response = json.loads((result.stdout or "").strip())
        except json.JSONDecodeError as error:
            raise GatewayError(
                502,
                "Office Sandbox 응답 형식이 올바르지 않다",
            ) from error
        if result.exit_code != 0:
            status = response.get("status")
            message = response.get("error")
            details = {
                key: response[key]
                for key in ("allowed",)
                if key in response
            }
            raise GatewayError(
                int(status) if isinstance(status, int) else 502,
                str(message or "Office Sandbox 작업이 실패했다"),
                details,
            )
        if not isinstance(response, dict):
            raise GatewayError(502, "Office Sandbox 응답은 object여야 한다")
        return response

    def generate(self, title: str, content: str) -> dict[str, Any]:
        return self._invoke(
            "generate",
            {"title": title, "content": content},
        )

    def convert(self, job_id: str, source: str, target: str) -> dict[str, Any]:
        return self._invoke(
            "convert",
            {"jobId": job_id, "source": source, "target": target},
        )

    def edit(
        self,
        job_id: str,
        operations: list[dict[str, object]],
    ) -> dict[str, Any]:
        return self._invoke(
            "edit",
            {"jobId": job_id, "operations": operations},
        )

    def download(self, path: str) -> tuple[bytes, str]:
        match = self.DOWNLOAD_PATH.fullmatch(path)
        if match is None:
            raise GatewayError(400, "Office 결과 파일 경로가 올바르지 않다")
        job_id, filename = match.groups()
        source_path = f"/work/{job_id}/{filename}"
        snapshot_path = f"/tmp/download-{uuid.uuid4().hex}"
        try:
            self._sandbox.ensure_running()
            copied = self._sandbox.exec(
                f"test -f {shlex.quote(source_path)} && "
                f"test ! -L {shlex.quote(source_path)} && "
                f"timeout --signal=KILL 30s head -c "
                f"{staging.MAX_ARTIFACT_BYTES + 1} -- "
                f"{shlex.quote(source_path)} > {shlex.quote(snapshot_path)}"
            )
            if copied.exit_code != 0:
                raise GatewayError(
                    502,
                    "Office 결과 파일 snapshot 생성에 실패했다",
                )
            metadata = self._sandbox.stat_file(snapshot_path)
            if (
                not isinstance(metadata.size, int)
                or metadata.size < 0
                or metadata.size > staging.MAX_ARTIFACT_BYTES
            ):
                raise GatewayError(
                    413,
                    f"{filename} 크기 metadata가 허용 범위를 벗어났다",
                )
            payload = self._sandbox.read_file(snapshot_path)
            if len(payload) != metadata.size:
                raise GatewayError(
                    409,
                    f"{filename} snapshot 크기가 변경됐다",
                )
        except Exception as error:
            if isinstance(error, GatewayError):
                raise
            raise GatewayError(404, "Office 결과 파일을 찾을 수 없다") from error
        finally:
            try:
                self._sandbox.delete_file(snapshot_path)
            except Exception:
                pass
        return (
            payload,
            mimetypes.guess_type(filename)[0]
            or "application/octet-stream",
        )

    def stop(self) -> None:
        if self._closed:
            return
        try:
            if self._sandbox is not None:
                self._sandbox.delete()
        except Exception as error:
            if "NotFound" not in str(error) and "not found" not in str(
                error
            ).lower():
                raise GatewayError(
                    502,
                    "Office Sandbox를 종료하지 못했다",
                ) from error
        self._closed = True
        if self._sandbox is not None:
            self._sandbox.close()
        self._close_clients()

    def abandon(self) -> None:
        """Release local SDK handles when cloud cleanup is delegated to reaper."""
        if self._closed:
            return
        self._closed = True
        if self._sandbox is not None:
            self._sandbox.close()
        self._close_clients()

    def _close_clients(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
        if self._owns_credential and self._credential is not None:
            close = getattr(self._credential, "close", None)
            if callable(close):
                close()


def resolve_office_endpoint(
    resource_group: str,
    pool_name: str,
) -> str:
    """Resolve the pool endpoint without exposing it to users."""
    result = subprocess.run(
        [
            "az",
            "containerapp",
            "sessionpool",
            "show",
            "--name",
            pool_name,
            "--resource-group",
            resource_group,
            "--query",
            "properties.poolManagementEndpoint",
            "--output",
            "tsv",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    endpoint = result.stdout.strip()
    if not endpoint:
        raise RuntimeError("Office session pool endpoint가 비어 있다")
    return endpoint


@dataclass
class OfficeJob:
    public_id: str
    owner: str
    session_identifier: str
    internal_job_id: str
    client: OfficeBackendClient
    files: dict[str, dict[str, object]] = field(default_factory=dict)
    status: str = "draft"
    backend_stopped: bool = False
    cleanup_pending: bool = False
    deleted: bool = False
    created_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)
    lock: threading.RLock = field(default_factory=threading.RLock)


class OfficeGatewayService:
    """In-memory reference service for user-facing Office jobs."""

    def __init__(
        self,
        client_factory: Callable[[str], OfficeBackendClient],
        *,
        staging_root: Path,
        approved_root: Path,
        backend: str = "dynamic-sessions",
        max_active_jobs: int = MAX_ACTIVE_OFFICE_JOBS,
    ) -> None:
        self.client_factory = client_factory
        self.staging_root = Path(staging_root)
        self.approved_root = Path(approved_root)
        self.backend = backend
        self.max_active_jobs = max_active_jobs
        self.jobs: dict[str, OfficeJob] = {}
        self.lock = threading.Lock()
        self.allocating = 0

    @staticmethod
    def validate_user(user: str) -> str:
        if not SAFE_USER.match(user) or user in {".", ".."}:
            raise GatewayError(401, "유효한 사용자 identity가 필요하다")
        return user

    @staticmethod
    def storage_key(user: str) -> str:
        return hashlib.sha256(user.encode("utf-8")).hexdigest()

    @staticmethod
    def _safe_file_metadata(metadata: dict[str, object]) -> dict[str, object]:
        return {
            "name": metadata["name"],
            "size": metadata["size"],
            "sha256": metadata["sha256"],
        }

    def _merge_files(
        self, job: OfficeJob, response: dict[str, Any]
    ) -> list[dict[str, object]]:
        files = response.get("files")
        if not isinstance(files, list):
            raise GatewayError(502, "Office session 응답에 files가 없다")
        merged = []
        for item in files:
            if not isinstance(item, dict):
                raise GatewayError(502, "Office 파일 metadata 형식이 올바르지 않다")
            name = item.get("name")
            path = item.get("downloadPath")
            size = item.get("size")
            sha256 = item.get("sha256")
            if (
                not isinstance(name, str)
                or not SAFE_FILE.match(name)
                or not isinstance(path, str)
                or not path.startswith("/")
                or not isinstance(size, int)
                or size < 1
                or size > staging.MAX_ARTIFACT_BYTES
                or not isinstance(sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", sha256)
            ):
                raise GatewayError(502, "Office 파일 metadata가 안전하지 않다")
            job.files[name] = dict(item)
            merged.append(self._safe_file_metadata(item))
        job.status = "draft"
        return merged

    def _job(self, user: str, public_id: str) -> OfficeJob:
        self._cleanup_expired_jobs()
        user = self.validate_user(user)
        with self.lock:
            job = self.jobs.get(public_id)
        if job is None:
            raise GatewayError(404, "문서 작업을 찾을 수 없다")
        if job.owner != user:
            raise GatewayError(404, "문서 작업을 찾을 수 없다")
        job.last_activity_at = time.time()
        return job

    def _cleanup_expired_jobs(self, now: float | None = None) -> None:
        current = now or time.time()
        cutoff = current - OFFICE_JOB_TTL_SECONDS
        with self.lock:
            candidates = [
                job
                for job in self.jobs.values()
                if (
                    not job.deleted
                    and (
                        (
                            job.status != "approved"
                            and job.last_activity_at < cutoff
                        )
                        or (
                            job.cleanup_pending
                            and job.last_activity_at < cutoff
                        )
                    )
                )
            ]
        for job in candidates:
            with job.lock:
                if job.backend_stopped and job.cleanup_pending:
                    job.cleanup_pending = False
                if not job.backend_stopped:
                    try:
                        job.client.stop()
                    except GatewayError:
                        continue
                    job.backend_stopped = True
                    job.cleanup_pending = False
                if job.status == "approved":
                    continue
                with self.lock:
                    if self.jobs.get(job.public_id) is not job:
                        continue
                    job.deleted = True
                    self.jobs.pop(job.public_id, None)

    def _ensure_active(self, job: OfficeJob) -> None:
        with self.lock:
            if job.deleted or self.jobs.get(job.public_id) is not job:
                raise GatewayError(404, "문서 작업을 찾을 수 없다")

    def public_view(self, job: OfficeJob) -> dict[str, object]:
        return {
            "id": job.public_id,
            "status": job.status,
            "files": [
                self._safe_file_metadata(job.files[name])
                for name in sorted(job.files)
            ],
        }

    def active_sandbox_ids(self) -> set[str]:
        with self.lock:
            jobs = list(self.jobs.values())
        identifiers = set()
        for job in jobs:
            if job.deleted or job.backend_stopped:
                continue
            sandbox_id = getattr(job.client, "sandbox_id", None)
            if isinstance(sandbox_id, str) and sandbox_id:
                identifiers.add(sandbox_id)
        return identifiers

    def create(self, user: str, title: str, content: str) -> dict[str, object]:
        self._cleanup_expired_jobs()
        user = self.validate_user(user)
        if not isinstance(title, str) or not title.strip():
            raise GatewayError(400, "title이 필요하다")
        if not isinstance(content, str) or not content.strip():
            raise GatewayError(400, "content가 필요하다")
        if len(title) > MAX_TITLE_CHARS:
            raise GatewayError(400, f"title은 {MAX_TITLE_CHARS}자 이하여야 한다")
        if len(content) > MAX_CONTENT_CHARS:
            raise GatewayError(400, f"content는 {MAX_CONTENT_CHARS}자 이하여야 한다")
        with self.lock:
            active_jobs = sum(
                1
                for job in self.jobs.values()
                if (
                    not job.deleted
                    and (not job.backend_stopped or job.cleanup_pending)
                )
            )
            if active_jobs + self.allocating >= self.max_active_jobs:
                raise GatewayError(
                    429,
                    "동시 Office Sandbox 작업 한도에 도달했다",
                )
            self.allocating += 1
        public_id = uuid.uuid4().hex
        session_identifier = broker.new_session_identifier("office")
        client: OfficeBackendClient | None = None
        job: OfficeJob | None = None
        job_ready = False
        try:
            client = self.client_factory(session_identifier)
            response = client.generate(title, content)
            if not isinstance(response, dict):
                raise GatewayError(502, "Office session 응답 형식이 올바르지 않다")
            internal_job_id = response.get("jobId")
            if not isinstance(internal_job_id, str):
                raise GatewayError(502, "Office session 응답에 jobId가 없다")
            job = OfficeJob(
                public_id=public_id,
                owner=user,
                session_identifier=session_identifier,
                internal_job_id=internal_job_id,
                client=client,
            )
            self._merge_files(job, response)
            job_ready = True
        except (
            GatewayError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            if client is not None:
                try:
                    client.stop()
                except GatewayError as cleanup_error:
                    original_message = (
                        error.message
                        if isinstance(error, GatewayError)
                        else "Office session 응답 처리 실패"
                    )
                    raise GatewayError(
                        502,
                        f"{original_message}; 실패한 session 정리도 완료하지 못했다",
                    ) from cleanup_error
            if isinstance(error, GatewayError):
                raise
            raise GatewayError(502, "Office session 응답 처리에 실패했다") from error
        finally:
            with self.lock:
                self.allocating -= 1
                if job is not None and job_ready:
                    self.jobs[public_id] = job
        if job is None:
            raise GatewayError(502, "Office job 생성에 실패했다")
        return self.public_view(job)

    def get(self, user: str, public_id: str) -> dict[str, object]:
        job = self._job(user, public_id)
        with job.lock:
            self._ensure_active(job)
            return self.public_view(job)

    def convert(
        self,
        user: str,
        public_id: str,
        source: str,
        target: str,
    ) -> dict[str, object]:
        if not isinstance(source, str) or not isinstance(target, str):
            raise GatewayError(400, "source와 target이 필요하다")
        job = self._job(user, public_id)
        with job.lock:
            self._ensure_active(job)
            if job.status == "approved":
                raise GatewayError(409, "승인된 문서 작업은 변경할 수 없다")
            response = job.client.convert(job.internal_job_id, source, target)
            files = self._merge_files(job, response)
            return {"id": public_id, "status": job.status, "files": files}

    def edit(
        self,
        user: str,
        public_id: str,
        operations: list[dict[str, object]],
    ) -> dict[str, object]:
        if not isinstance(operations, list) or not operations:
            raise GatewayError(400, "operations가 필요하다")
        if not all(isinstance(operation, dict) for operation in operations):
            raise GatewayError(400, "각 operation은 object여야 한다")
        job = self._job(user, public_id)
        with job.lock:
            self._ensure_active(job)
            if job.status == "approved":
                raise GatewayError(409, "승인된 문서 작업은 변경할 수 없다")
            response = job.client.edit(job.internal_job_id, operations)
            files = self._merge_files(job, response)
            return {"id": public_id, "status": job.status, "files": files}

    def download(
        self,
        user: str,
        public_id: str,
        filename: str,
    ) -> tuple[bytes, str]:
        job = self._job(user, public_id)
        with job.lock:
            self._ensure_active(job)
            metadata = job.files.get(filename)
            if metadata is None:
                raise GatewayError(404, "결과 파일을 찾을 수 없다")
            path = metadata.get("downloadPath")
            if not isinstance(path, str):
                raise GatewayError(502, "결과 파일 경로가 올바르지 않다")
            if job.status == "approved":
                approved_path = self.approved_root / public_id / filename
                if not approved_path.is_file():
                    raise GatewayError(404, "승인된 결과 파일을 찾을 수 없다")
                payload = approved_path.read_bytes()
                expected_hash = metadata.get("sha256")
                if (
                    not isinstance(expected_hash, str)
                    or staging.sha256_bytes(payload) != expected_hash
                ):
                    raise GatewayError(409, f"{filename}의 승인 파일 hash가 다르다")
                return (
                    payload,
                    mimetypes.guess_type(filename)[0]
                    or "application/octet-stream",
                )
            payload, content_type = job.client.download(path)
            expected_hash = metadata.get("sha256")
            if (
                not isinstance(expected_hash, str)
                or staging.sha256_bytes(payload) != expected_hash
            ):
                raise GatewayError(
                    409,
                    f"{filename}의 hash가 생성 이후 변경됐다",
                )
            return payload, content_type

    def approve(
        self,
        user: str,
        public_id: str,
    ) -> dict[str, object]:
        job = self._job(user, public_id)
        with job.lock:
            self._ensure_active(job)
            user_staging = self.staging_root / self.storage_key(user)
            user_staging.mkdir(parents=True, exist_ok=True)
            try:
                with tempfile.TemporaryDirectory(dir=user_staging) as temporary:
                    store = staging.ArtifactStaging(
                        Path(temporary),
                        "approval",
                        public_id,
                    )
                    for name in sorted(job.files):
                        payload, _ = self.download(user, public_id, name)
                        expected_hash = job.files[name].get("sha256")
                        if (
                            not isinstance(expected_hash, str)
                            or staging.sha256_bytes(payload) != expected_hash
                        ):
                            raise GatewayError(
                                409,
                                f"{name}의 hash가 생성 이후 변경됐다",
                            )
                        store.stage(name, payload)
                    store.write_manifest()
                    batch = staging.promote_batch(
                        store.artifacts,
                        self.approved_root / public_id,
                        approver=user,
                    )
            except staging.StagingError as error:
                raise GatewayError(409, str(error)) from error
            except OSError as error:
                raise GatewayError(500, "승인 파일 처리에 실패했다") from error
            promotions = [
                {
                    "name": result["name"],
                    "approved": True,
                    "sha256": result["sha256"],
                }
                for result in batch
            ]
            job.status = "approved"
            if not job.backend_stopped:
                try:
                    job.client.stop()
                    job.backend_stopped = True
                except GatewayError:
                    # Approval and local promotion are already committed.
                    # Server-side auto-delete and the next DELETE request retry
                    # cleanup without invalidating the approved result.
                    abandon = getattr(job.client, "abandon", None)
                    if callable(abandon):
                        abandon()
                        job.backend_stopped = True
                    job.cleanup_pending = True
                    job.last_activity_at = (
                        time.time() - OFFICE_JOB_TTL_SECONDS - 1
                    )
            return {
                "id": public_id,
                "status": job.status,
                "files": promotions,
            }

    def delete(self, user: str, public_id: str) -> None:
        job = self._job(user, public_id)
        with job.lock:
            self._ensure_active(job)
            if not job.backend_stopped:
                job.client.stop()
                job.backend_stopped = True
            with self.lock:
                job.deleted = True
                self.jobs.pop(public_id, None)
