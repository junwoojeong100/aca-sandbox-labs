"""Artifact Staging과 Approval Service.

Sandbox는 실제 업무 저장소에 직접 쓰지 않는다.
결과 파일은 staging에 기록하고, 검사와 승인을 거친 뒤에만 승격한다.
승격 직전에 hash를 다시 확인해 staging 이후 변조를 잡는다.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024

# magic bytes로 실제 형식을 확인한다. 확장자만 믿지 않는다.
MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"PK\x03\x04", "zip"),
    (b"\xff\xd8\xff", "jpeg"),
)

ALLOWED_EXTENSIONS = {
    ".csv",
    ".docx",
    ".json",
    ".md",
    ".pdf",
    ".png",
    ".pptx",
    ".txt",
    ".xlsx",
}

ZIP_BASED_EXTENSIONS = {".docx", ".pptx", ".xlsx"}
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
RESERVED_ARTIFACT_NAMES = {"manifest.json"}


class StagingError(RuntimeError):
    """검사 실패 또는 승인 위반."""


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def detect_magic(payload: bytes) -> str | None:
    for signature, name in MAGIC_SIGNATURES:
        if payload.startswith(signature):
            return name
    return None


@dataclass
class Artifact:
    name: str
    path: Path
    size: int
    sha256: str
    detected_type: str | None
    checks: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": str(self.path),
            "size": self.size,
            "sha256": self.sha256,
            "detectedType": self.detected_type,
            "checks": self.checks,
        }


class ArtifactStaging:
    """격리된 staging 위치. tenant와 request 단위로 분리한다."""

    def __init__(self, root: Path, tenant_id: str, request_id: str) -> None:
        self.root = Path(root) / tenant_id / request_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.artifacts: list[Artifact] = []

    def _inspect(self, name: str, payload: bytes) -> tuple[str | None, dict[str, str]]:
        checks: dict[str, str] = {}
        extension = Path(name).suffix.lower()

        if not SAFE_NAME.match(name):
            raise StagingError(f"안전하지 않은 파일 이름: {name}")
        if name in RESERVED_ARTIFACT_NAMES:
            raise StagingError(f"예약된 artifact 파일 이름: {name}")
        if extension not in ALLOWED_EXTENSIONS:
            raise StagingError(f"허용되지 않은 확장자: {extension or '(없음)'}")
        if len(payload) == 0:
            raise StagingError(f"빈 파일: {name}")
        if len(payload) > MAX_ARTIFACT_BYTES:
            raise StagingError(
                f"artifact 크기 한도 초과: {name} ({len(payload)} bytes)"
            )
        checks["name"] = "ok"
        checks["size"] = "ok"

        detected = detect_magic(payload)
        if extension == ".pdf" and detected != "pdf":
            raise StagingError(f"{name}의 내용이 PDF가 아니다")
        if extension == ".png" and detected != "png":
            raise StagingError(f"{name}의 내용이 PNG가 아니다")
        if extension in ZIP_BASED_EXTENSIONS and detected != "zip":
            raise StagingError(f"{name}의 내용이 OOXML(zip)이 아니다")
        checks["magicBytes"] = detected or "text"

        if extension == ".json":
            try:
                json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise StagingError(f"{name}이 올바른 JSON이 아니다: {error}") from error
            checks["json"] = "ok"

        if extension in ZIP_BASED_EXTENSIONS and b"vbaProject.bin" in payload:
            raise StagingError(f"{name}에 macro가 포함돼 있다")
        if extension in ZIP_BASED_EXTENSIONS:
            checks["macro"] = "none"

        # 이 예제에는 malware scanner가 없다. production에서는 이 지점에서
        # Defender 또는 사내 AV/DLP 검사를 호출하고 결과를 checks에 기록한다.
        checks["malwareScan"] = "not-implemented-in-reference"
        return detected, checks

    def stage(self, name: str, payload: bytes) -> Artifact:
        detected, checks = self._inspect(name, payload)
        destination = self.root / name
        destination.write_bytes(payload)
        destination.chmod(0o440)
        artifact = Artifact(
            name=name,
            path=destination,
            size=len(payload),
            sha256=sha256_bytes(payload),
            detected_type=detected,
            checks=checks,
        )
        self.artifacts.append(artifact)
        return artifact

    def manifest(self) -> dict[str, object]:
        return {
            "stagingRoot": str(self.root),
            "stagedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "artifacts": [artifact.as_dict() for artifact in self.artifacts],
        }

    def write_manifest(self) -> Path:
        path = self.root / "manifest.json"
        path.write_text(
            json.dumps(self.manifest(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path


class ApprovalService:
    """Sandbox와 분리된 승격 경로.

    Sandbox와 Agent는 이 class의 destination에 접근 권한이 없다고 가정한다.
    """

    def __init__(self, destination: Path) -> None:
        self.destination = Path(destination)
        self.destination.mkdir(parents=True, exist_ok=True)

    def promote(
        self,
        artifact: Artifact,
        *,
        approved: bool,
        approver: str,
    ) -> dict[str, object]:
        if not approved:
            return {
                "name": artifact.name,
                "promoted": False,
                "reason": "승인되지 않음",
            }

        payload = artifact.path.read_bytes()
        current_hash = sha256_bytes(payload)
        if current_hash != artifact.sha256:
            raise StagingError(
                f"{artifact.name}의 hash가 staging 이후 변경됐다. 승격을 중단한다."
            )

        target = self.destination / artifact.name
        shutil.copyfile(artifact.path, target)
        return {
            "name": artifact.name,
            "promoted": True,
            "approver": approver,
            "sha256": current_hash,
            "target": str(target),
            "promotedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }


def promote_batch(
    artifacts: list[Artifact],
    destination: Path,
    *,
    approver: str,
) -> list[dict[str, object]]:
    """Verify all artifacts, then publish the complete set with one directory rename."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    snapshots: list[tuple[Artifact, bytes]] = []
    for artifact in artifacts:
        payload = artifact.path.read_bytes()
        current_hash = sha256_bytes(payload)
        if current_hash != artifact.sha256:
            raise StagingError(
                f"{artifact.name}의 hash가 staging 이후 변경됐다. 승격을 중단한다."
            )
        snapshots.append((artifact, payload))

    promoted_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if destination.exists():
        if not destination.is_dir():
            raise StagingError("승인 대상 경로가 디렉터리가 아니다")
        expected_names = {artifact.name for artifact, _ in snapshots}
        actual_names = {path.name for path in destination.iterdir() if path.is_file()}
        if expected_names != actual_names:
            raise StagingError("기존 승인 디렉터리의 파일 집합이 요청과 다르다")
        for artifact, _ in snapshots:
            target = destination / artifact.name
            if sha256_bytes(target.read_bytes()) != artifact.sha256:
                raise StagingError(f"기존 승인 파일의 hash가 다르다: {artifact.name}")
        return [
            {
                "name": artifact.name,
                "promoted": True,
                "approver": approver,
                "sha256": artifact.sha256,
                "target": str(destination / artifact.name),
                "promotedAt": promoted_at,
            }
            for artifact, _ in snapshots
        ]

    temporary = destination.parent / f".{destination.name}.tmp-{uuid.uuid4().hex}"
    temporary.mkdir()
    try:
        for artifact, payload in snapshots:
            target = temporary / artifact.name
            target.write_bytes(payload)
            target.chmod(0o440)
        temporary.replace(destination)
    except OSError as error:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise StagingError(f"artifact batch 승격 실패: {error}") from error

    return [
        {
            "name": artifact.name,
            "promoted": True,
            "approver": approver,
            "sha256": artifact.sha256,
            "target": str(destination / artifact.name),
            "promotedAt": promoted_at,
        }
        for artifact, _ in snapshots
    ]
