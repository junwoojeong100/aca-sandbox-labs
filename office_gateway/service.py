"""Office user gateway service.

The gateway owns Azure tokens and session identifiers. User-facing responses
contain only public job IDs, safe file metadata, and approval state.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agent import broker, config, staging

SAFE_USER = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SAFE_FILE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
MAX_TITLE_CHARS = 200
MAX_CONTENT_CHARS = 100_000


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
    client: OfficeSessionClient
    files: dict[str, dict[str, object]] = field(default_factory=dict)
    status: str = "draft"
    deleted: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)


class OfficeGatewayService:
    """In-memory reference service for user-facing Office jobs."""

    def __init__(
        self,
        client_factory: Callable[[str], OfficeSessionClient],
        *,
        staging_root: Path,
        approved_root: Path,
    ) -> None:
        self.client_factory = client_factory
        self.staging_root = Path(staging_root)
        self.approved_root = Path(approved_root)
        self.jobs: dict[str, OfficeJob] = {}
        self.lock = threading.Lock()

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
                or not isinstance(sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", sha256)
            ):
                raise GatewayError(502, "Office 파일 metadata가 안전하지 않다")
            job.files[name] = dict(item)
            merged.append(self._safe_file_metadata(item))
        job.status = "draft"
        return merged

    def _job(self, user: str, public_id: str) -> OfficeJob:
        user = self.validate_user(user)
        with self.lock:
            job = self.jobs.get(public_id)
        if job is None:
            raise GatewayError(404, "문서 작업을 찾을 수 없다")
        if job.owner != user:
            raise GatewayError(404, "문서 작업을 찾을 수 없다")
        return job

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

    def create(self, user: str, title: str, content: str) -> dict[str, object]:
        user = self.validate_user(user)
        if not isinstance(title, str) or not title.strip():
            raise GatewayError(400, "title이 필요하다")
        if not isinstance(content, str) or not content.strip():
            raise GatewayError(400, "content가 필요하다")
        if len(title) > MAX_TITLE_CHARS:
            raise GatewayError(400, f"title은 {MAX_TITLE_CHARS}자 이하여야 한다")
        if len(content) > MAX_CONTENT_CHARS:
            raise GatewayError(400, f"content는 {MAX_CONTENT_CHARS}자 이하여야 한다")
        public_id = uuid.uuid4().hex
        session_identifier = broker.new_session_identifier("office")
        client = self.client_factory(session_identifier)
        try:
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
        except (
            GatewayError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
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
        with self.lock:
            self.jobs[public_id] = job
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
            return job.client.download(path)

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
            return {
                "id": public_id,
                "status": job.status,
                "files": promotions,
            }

    def delete(self, user: str, public_id: str) -> None:
        job = self._job(user, public_id)
        with job.lock:
            self._ensure_active(job)
            job.client.stop()
            with self.lock:
                job.deleted = True
                self.jobs.pop(public_id, None)
