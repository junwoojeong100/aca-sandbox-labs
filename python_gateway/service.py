"""Python analysis user gateway service."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from agent import broker, config, llm, orchestrator, staging

SAFE_USER = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_REQUEST_CHARS = 20_000
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 128 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 128 * 1024 * 1024
INTERNAL_PATH = re.compile(
    r"(?<!\w)/(?:mnt/data|Users|home|tmp|var)(?:/[^\s'\";,)\]]+)*"
)


class GatewayError(RuntimeError):
    """A safe user-facing Python gateway error."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class AnalysisJob:
    public_id: str
    owner: str
    result: orchestrator.RunResult
    status: str
    deleted: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)


class PythonGatewayService:
    """Store analysis jobs while hiding backend session details."""

    def __init__(
        self,
        runner_factory: Callable[[], orchestrator.Orchestrator],
        *,
        staging_root: Path,
        approved_root: Path,
    ) -> None:
        self.runner_factory = runner_factory
        self.staging_root = Path(staging_root).resolve()
        self.approved_root = Path(approved_root)
        self.jobs: dict[str, AnalysisJob] = {}
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
    def _validate_inputs(
        request_text: str,
        attachments: dict[str, bytes],
        expected_outputs: tuple[str, ...],
    ) -> None:
        if not isinstance(request_text, str) or not request_text.strip():
            raise GatewayError(400, "request가 필요하다")
        if len(request_text) > MAX_REQUEST_CHARS:
            raise GatewayError(400, f"request는 {MAX_REQUEST_CHARS}자 이하여야 한다")
        if len(attachments) > MAX_ATTACHMENTS:
            raise GatewayError(400, f"첨부파일은 {MAX_ATTACHMENTS}개 이하여야 한다")
        total_bytes = sum(len(payload) for payload in attachments.values())
        if total_bytes > MAX_TOTAL_ATTACHMENT_BYTES:
            raise GatewayError(413, "첨부파일 전체 크기가 128MB를 초과했다")
        for name, payload in attachments.items():
            if not staging.SAFE_NAME.match(name):
                raise GatewayError(400, f"안전하지 않은 첨부파일 이름: {name}")
            if name in staging.RESERVED_ARTIFACT_NAMES:
                raise GatewayError(400, f"예약된 첨부파일 이름: {name}")
            if not payload:
                raise GatewayError(400, f"빈 첨부파일: {name}")
            if len(payload) > MAX_ATTACHMENT_BYTES:
                raise GatewayError(413, f"{name} 크기가 128MB를 초과했다")
        for name in expected_outputs:
            if not staging.SAFE_NAME.match(name):
                raise GatewayError(400, f"안전하지 않은 결과 파일 이름: {name}")
            if name in staging.RESERVED_ARTIFACT_NAMES:
                raise GatewayError(400, f"예약된 결과 파일 이름: {name}")

    def _job(self, user: str, public_id: str) -> AnalysisJob:
        user = self.validate_user(user)
        with self.lock:
            job = self.jobs.get(public_id)
        if job is None or job.owner != user:
            raise GatewayError(404, "분석 작업을 찾을 수 없다")
        return job

    def _ensure_active(self, job: AnalysisJob) -> None:
        with self.lock:
            if job.deleted or self.jobs.get(job.public_id) is not job:
                raise GatewayError(404, "분석 작업을 찾을 수 없다")

    @staticmethod
    def _safe_artifacts(
        result: orchestrator.RunResult,
    ) -> list[dict[str, object]]:
        return [
            {
                "name": artifact["name"],
                "size": artifact["size"],
                "sha256": artifact["sha256"],
            }
            for artifact in result.artifacts
        ]

    @staticmethod
    def _safe_text(text: str, session_identifier: str) -> str:
        sanitized = text.replace(session_identifier, "[session]") if session_identifier else text
        return INTERNAL_PATH.sub("[internal-path]", sanitized)

    def public_view(self, job: AnalysisJob) -> dict[str, object]:
        return {
            "id": job.public_id,
            "status": job.status,
            "classification": job.result.decision.get("classification"),
            "route": job.result.decision.get("route"),
            "allowed": job.result.decision.get("allowed"),
            "reason": job.result.decision.get("reason"),
            "succeeded": job.result.succeeded,
            "attempts": job.result.attempts,
            "plan": self._safe_text(
                job.result.plan,
                job.result.session_identifier,
            ),
            "stdout": self._safe_text(
                job.result.stdout[-4000:],
                job.result.session_identifier,
            ),
            "artifacts": self._safe_artifacts(job.result),
            "promotions": job.result.promotions,
        }

    def create(
        self,
        user: str,
        request_text: str,
        attachments: dict[str, bytes],
        expected_outputs: tuple[str, ...],
    ) -> dict[str, object]:
        user = self.validate_user(user)
        self._validate_inputs(request_text, attachments, expected_outputs)
        try:
            result = self.runner_factory().run(
                request_text,
                tenant_id=self.storage_key(user),
                user_id=user,
                attachments=attachments,
                expected_outputs=expected_outputs,
                approve=False,
            )
        except (
            broker.BrokerError,
            config.ConfigError,
            llm.LLMError,
            staging.StagingError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as error:
            raise GatewayError(502, "분석 backend 실행에 실패했다") from error

        public_id = uuid.uuid4().hex
        status = "completed" if result.succeeded else "failed"
        job = AnalysisJob(
            public_id=public_id,
            owner=user,
            result=result,
            status=status,
        )
        with self.lock:
            self.jobs[public_id] = job
        return self.public_view(job)

    def get(self, user: str, public_id: str) -> dict[str, object]:
        job = self._job(user, public_id)
        with job.lock:
            self._ensure_active(job)
            return self.public_view(job)

    def _artifact(self, job: AnalysisJob, filename: str) -> dict[str, object]:
        for artifact in job.result.artifacts:
            if artifact.get("name") == filename:
                return artifact
        raise GatewayError(404, "결과 파일을 찾을 수 없다")

    def download(
        self,
        user: str,
        public_id: str,
        filename: str,
    ) -> tuple[bytes, str]:
        job = self._job(user, public_id)
        with job.lock:
            self._ensure_active(job)
            artifact = self._artifact(job, filename)
            path_value = artifact.get("path")
            expected_hash = artifact.get("sha256")
            if not isinstance(path_value, str) or not isinstance(expected_hash, str):
                raise GatewayError(502, "결과 파일 metadata가 올바르지 않다")
            path = Path(path_value).resolve()
            if job.status == "approved":
                path = (self.approved_root / public_id / filename).resolve()
            if not path.is_relative_to(self.staging_root) or not path.is_file():
                approved_root = self.approved_root.resolve()
                if (
                    job.status != "approved"
                    or not path.is_relative_to(approved_root)
                    or not path.is_file()
                ):
                    raise GatewayError(404, "결과 파일을 찾을 수 없다")
            payload = path.read_bytes()
            if staging.sha256_bytes(payload) != expected_hash:
                raise GatewayError(409, f"{filename}의 hash가 staging 이후 변경됐다")
            return payload, str(artifact.get("detectedType") or "application/octet-stream")

    def approve(self, user: str, public_id: str) -> dict[str, object]:
        job = self._job(user, public_id)
        with job.lock:
            self._ensure_active(job)
            if not job.result.succeeded or not job.result.artifacts:
                raise GatewayError(409, "성공한 artifact가 없어 승인할 수 없다")
            artifacts = []
            for item in job.result.artifacts:
                path_value = item.get("path")
                if not isinstance(path_value, str):
                    raise GatewayError(502, "결과 파일 metadata가 올바르지 않다")
                artifact_path = Path(path_value).resolve()
                if (
                    not artifact_path.is_relative_to(self.staging_root)
                    or not artifact_path.is_file()
                ):
                    raise GatewayError(404, "결과 파일을 찾을 수 없다")
                artifacts.append(
                    staging.Artifact(
                        name=str(item["name"]),
                        path=artifact_path,
                        size=int(item["size"]),
                        sha256=str(item["sha256"]),
                        detected_type=(
                            str(item["detectedType"])
                            if item.get("detectedType") is not None
                            else None
                        ),
                        checks=dict(item.get("checks") or {}),
                    )
                )
            try:
                batch = staging.promote_batch(
                    artifacts,
                    self.approved_root / public_id,
                    approver=user,
                )
            except staging.StagingError as error:
                raise GatewayError(409, str(error)) from error
            promotions = [
                {
                    "name": result["name"],
                    "promoted": True,
                    "sha256": result["sha256"],
                }
                for result in batch
            ]
            job.result.promotions = promotions
            job.status = "approved"
            return self.public_view(job)

    def delete(self, user: str, public_id: str) -> None:
        job = self._job(user, public_id)
        with job.lock:
            self._ensure_active(job)
            artifact_directories = {
                Path(str(item["path"])).resolve().parent
                for item in job.result.artifacts
                if isinstance(item.get("path"), str)
            }
            for directory in artifact_directories:
                if directory.is_relative_to(self.staging_root) and directory.is_dir():
                    shutil.rmtree(directory)
            with self.lock:
                job.deleted = True
                self.jobs.pop(public_id, None)
