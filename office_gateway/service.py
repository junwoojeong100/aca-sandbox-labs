"""Backend-neutral Office user gateway service."""

from __future__ import annotations

import hashlib
import mimetypes
import re
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from agent import ids, staging

SAFE_USER = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SAFE_FILE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
MAX_TITLE_CHARS = 200
MAX_CONTENT_CHARS = 100_000
OFFICE_JOB_TTL_SECONDS = 3600
MAX_ACTIVE_OFFICE_JOBS = 5


class GatewayError(RuntimeError):
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


@dataclass
class OfficeJob:
    public_id: str
    owner: str
    execution_identifier: str
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
    def __init__(
        self,
        client_factory: Callable[[str], OfficeBackendClient],
        *,
        staging_root: Path,
        approved_root: Path,
        backend: str,
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
    def _safe_file_metadata(
        metadata: dict[str, object],
    ) -> dict[str, object]:
        return {
            "name": metadata["name"],
            "size": metadata["size"],
            "sha256": metadata["sha256"],
        }

    def _merge_files(
        self,
        job: OfficeJob,
        response: dict[str, Any],
    ) -> list[dict[str, object]]:
        files = response.get("files")
        if not isinstance(files, list):
            raise GatewayError(502, "Office backend 응답에 files가 없다")
        merged = []
        for item in files:
            if not isinstance(item, dict):
                raise GatewayError(502, "Office 파일 metadata 형식이 올바르지 않다")
            name = item.get("name")
            path = item.get("downloadPath")
            size = item.get("size")
            digest = item.get("sha256")
            if (
                not isinstance(name, str)
                or not SAFE_FILE.match(name)
                or not isinstance(path, str)
                or not path.startswith("/")
                or not isinstance(size, int)
                or size < 1
                or size > staging.MAX_ARTIFACT_BYTES
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
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
        if job is None or job.owner != user:
            raise GatewayError(404, "문서 작업을 찾을 수 없다")
        job.last_activity_at = time.time()
        return job

    def _cleanup_expired_jobs(self, now: float | None = None) -> None:
        cutoff = (now or time.time()) - OFFICE_JOB_TTL_SECONDS
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
                    if self.jobs.get(job.public_id) is job:
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

    def active_backend_ids(self) -> set[str]:
        with self.lock:
            jobs = list(self.jobs.values())
        identifiers = set()
        for job in jobs:
            if job.deleted or job.backend_stopped:
                continue
            backend_id = getattr(job.client, "sandbox_id", None)
            if isinstance(backend_id, str) and backend_id:
                identifiers.add(backend_id)
        return identifiers

    def create(
        self,
        user: str,
        title: str,
        content: str,
    ) -> dict[str, object]:
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
                raise GatewayError(429, "동시 Office 작업 한도에 도달했다")
            self.allocating += 1
        public_id = uuid.uuid4().hex
        execution_identifier = ids.new_identifier("office")
        client: OfficeBackendClient | None = None
        job: OfficeJob | None = None
        job_ready = False
        try:
            client = self.client_factory(execution_identifier)
            response = client.generate(title, content)
            if not isinstance(response, dict):
                raise GatewayError(502, "Office backend 응답 형식이 올바르지 않다")
            internal_job_id = response.get("jobId")
            if not isinstance(internal_job_id, str):
                raise GatewayError(502, "Office backend 응답에 jobId가 없다")
            job = OfficeJob(
                public_id=public_id,
                owner=user,
                execution_identifier=execution_identifier,
                internal_job_id=internal_job_id,
                client=client,
            )
            self._merge_files(job, response)
            job_ready = True
        except (GatewayError, KeyError, TypeError, ValueError) as error:
            if client is not None:
                try:
                    client.stop()
                except GatewayError as cleanup_error:
                    original = (
                        error.message
                        if isinstance(error, GatewayError)
                        else "Office backend 응답 처리 실패"
                    )
                    raise GatewayError(
                        502,
                        f"{original}; 실패한 backend 정리도 완료하지 못했다",
                    ) from cleanup_error
            if isinstance(error, GatewayError):
                raise
            raise GatewayError(502, "Office backend 응답 처리에 실패했다") from error
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
                raise GatewayError(409, f"{filename}의 hash가 생성 이후 변경됐다")
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
