"""Orchestrator.

자연어 요청 -> 정책 결정 -> LLM 계획·코드 -> 격리 실행 -> 오류 수정 재실행
-> artifact staging과 검사 -> 승인 -> 승격까지의 전체 흐름을 연결한다.

모든 단계는 correlation ID로 이어진다.
session identifier는 감사 로그에만 남기고 사용자 응답에는 넣지 않는다.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import broker, config, llm, policy, staging

BOOTSTRAP_INSPECTION_CODE = """\
import json
import os
import platform
import sys

entries = []
for name in sorted(os.listdir("/mnt/data")):
    path = os.path.join("/mnt/data", name)
    if os.path.isfile(path):
        entries.append({"name": name, "size": os.path.getsize(path)})

print(json.dumps({
    "python": platform.python_version(),
    "platform": sys.platform,
    "files": entries,
}, ensure_ascii=False))
"""

USER_INTERNAL_PATH = re.compile(
    r"(?<!\w)/(?:mnt/data|Users|home|tmp|var)(?:/[^\s'\";,)\]]+)*"
)


@dataclass
class StepLog:
    step: str
    detail: dict[str, Any] = field(default_factory=dict)
    at: str = field(
        default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )

    def as_dict(self) -> dict[str, Any]:
        return {"step": self.step, "at": self.at, "detail": self.detail}


@dataclass
class RunResult:
    request_id: str
    decision: dict[str, Any]
    succeeded: bool
    attempts: int
    plan: str = ""
    stdout: str = ""
    stderr: str = ""
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    promotions: list[dict[str, Any]] = field(default_factory=list)
    audit: list[dict[str, Any]] = field(default_factory=list)
    session_identifier: str = ""

    def user_view(self) -> dict[str, Any]:
        """사용자에게 반환하는 응답. identifier와 내부 경로를 넣지 않는다."""
        return {
            "classification": self.decision.get("classification"),
            "route": self.decision.get("route"),
            "allowed": self.decision.get("allowed"),
            "reason": self.decision.get("reason"),
            "succeeded": self.succeeded,
            "attempts": self.attempts,
            "plan": sanitize_user_text(self.plan, self.session_identifier),
            "stdout": sanitize_user_text(
                self.stdout[-4000:],
                self.session_identifier,
            ),
            "artifacts": [
                {
                    "name": artifact["name"],
                    "size": artifact["size"],
                    "sha256": artifact["sha256"],
                }
                for artifact in self.artifacts
            ],
            "promotions": [
                {
                    key: promotion[key]
                    for key in (
                        "name",
                        "promoted",
                        "reason",
                        "approver",
                        "sha256",
                        "promotedAt",
                    )
                    if key in promotion
                }
                for promotion in self.promotions
            ],
        }

    def audit_view(self) -> dict[str, Any]:
        """감사 로그. backend 저장소에만 남긴다."""
        return {
            "requestId": self.request_id,
            "sessionIdentifier": self.session_identifier,
            "decision": self.decision,
            "succeeded": self.succeeded,
            "attempts": self.attempts,
            "artifacts": self.artifacts,
            "promotions": self.promotions,
            "steps": self.audit,
        }


def sanitize_error(stderr: str, limit: int = 2000) -> str:
    """LLM에 되돌려줄 오류에서 내부 경로와 identifier 흔적을 줄인다."""
    lines = [line for line in stderr.splitlines() if line.strip()]
    trimmed = "\n".join(lines[-40:])
    return trimmed[-limit:]


def sanitize_user_text(text: str, session_identifier: str = "") -> str:
    """Remove backend-only session identifiers and common internal paths."""
    sanitized = text.replace(session_identifier, "[session]") if session_identifier else text
    return USER_INTERNAL_PATH.sub("[internal-path]", sanitized)


class Orchestrator:
    def __init__(
        self,
        settings: config.Settings | None = None,
        llm_client: Any | None = None,
    ) -> None:
        self.settings = settings or config.Settings()
        self.llm = llm_client or llm.create_client(self.settings)

    def run(
        self,
        request_text: str,
        *,
        tenant_id: str = "tenant-demo",
        user_id: str = "user-demo",
        attachments: dict[str, bytes] | None = None,
        expected_outputs: tuple[str, ...] = (),
        approve: bool = False,
        approver: str = "unapproved",
        estimated_seconds: int = 30,
    ) -> RunResult:
        attachments = attachments or {}
        request_id = f"req-{secrets.token_hex(8)}"
        audit: list[StepLog] = []

        decision = policy.classify(
            policy.PolicyInput(
                tenant_id=tenant_id,
                user_id=user_id,
                request_text=request_text,
                attachment_names=tuple(attachments),
                attachment_sizes=tuple(
                    len(value) for value in attachments.values()
                ),
                estimated_seconds=estimated_seconds,
            )
        )
        audit.append(StepLog("policy", decision.as_dict()))

        if not decision.allowed:
            return RunResult(
                request_id=request_id,
                decision=decision.as_dict(),
                succeeded=False,
                attempts=0,
                stderr=decision.reason,
                audit=[entry.as_dict() for entry in audit],
            )

        if decision.route is not policy.Route.PYTHON_POOL:
            return RunResult(
                request_id=request_id,
                decision=decision.as_dict(),
                succeeded=False,
                attempts=0,
                stderr=(
                    f"이 orchestrator는 Python pool 경로만 실행한다. "
                    f"요청 경로: {decision.route.value}"
                ),
                audit=[entry.as_dict() for entry in audit],
            )

        session = broker.create_python_session(self.settings)
        audit.append(
            StepLog(
                "session-allocated",
                {
                    "backend": self.settings.execution_backend,
                    "pool": (
                        self.settings.python_pool_name
                        if self.settings.execution_backend == "dynamic-sessions"
                        else None
                    ),
                    "sandboxGroup": (
                        self.settings.sandbox_group_name
                        if self.settings.execution_backend == "sandboxes"
                        else None
                    ),
                    "reuse": False,
                },
            )
        )

        result = RunResult(
            request_id=request_id,
            decision=decision.as_dict(),
            succeeded=False,
            attempts=0,
            session_identifier=session.identifier,
        )

        try:
            for name, payload in attachments.items():
                session.upload(name, payload)
                audit.append(
                    StepLog(
                        "attachment-uploaded",
                        {"name": name, "size": len(payload)},
                    )
                )

            bootstrap = session.execute(BOOTSTRAP_INSPECTION_CODE)
            audit.append(
                StepLog("session-bootstrap", {"stdout": bootstrap.stdout.strip()[:1000]})
            )

            failure: str | None = None
            plan: llm.Plan | None = None
            for attempt in range(1, self.settings.max_code_retries + 2):
                result.attempts = attempt
                plan = self.llm.plan(
                    request_text,
                    attachments=tuple(attachments),
                    expected_outputs=tuple(expected_outputs),
                    failure=failure,
                )
                result.plan = plan.plan
                violations = policy.inspect_code(plan.code)
                audit.append(
                    StepLog(
                        "code-generated",
                        {
                            "attempt": attempt,
                            "provider": plan.provider,
                            "plan": plan.plan,
                            "codeSha256": staging.sha256_bytes(plan.code.encode()),
                            "violations": violations,
                        },
                    )
                )
                if violations:
                    failure = (
                        "정책 위반으로 실행하지 않았다: "
                        + ", ".join(violations)
                        + ". 표준 라이브러리와 사전 설치 분석 라이브러리만 사용한다."
                    )
                    result.stderr = failure
                    continue

                execution = session.execute(
                    plan.code, timeout=self.settings.execution_timeout_seconds + 60
                )
                result.stdout = execution.stdout
                result.stderr = execution.stderr
                audit.append(
                    StepLog(
                        "execution",
                        {
                            "attempt": attempt,
                            "status": execution.status,
                            "stdoutBytes": len(execution.stdout),
                            "stderrBytes": len(execution.stderr),
                            "warningsOnly": bool(execution.warnings),
                        },
                    )
                )
                if execution.succeeded:
                    if expected_outputs:
                        listing = session.list_files()
                        available = {
                            str(item.get("name"))
                            for item in (listing.get("value") or [])
                            if isinstance(item, dict) and item.get("name")
                        }
                        missing = sorted(set(expected_outputs) - available)
                        audit.append(
                            StepLog(
                                "expected-output-check",
                                {
                                    "expected": list(expected_outputs),
                                    "available": sorted(available),
                                    "missing": missing,
                                },
                            )
                        )
                        if missing:
                            failure = (
                                "코드는 성공했지만 필수 결과 파일이 없다: "
                                + ", ".join(missing)
                                + ". 요청된 파일명을 정확히 사용해 전체 코드를 다시 만든다."
                            )
                            result.stderr = failure
                            continue
                    result.succeeded = True
                    break
                failure = sanitize_error(execution.stderr or execution.stdout)

            if not result.succeeded:
                audit.append(
                    StepLog(
                        "retry-exhausted",
                        {"attempts": result.attempts, "lastError": result.stderr[-500:]},
                    )
                )
                return result

            store = staging.ArtifactStaging(
                self.settings.staging_dir, tenant_id, request_id
            )
            listing = session.list_files()
            available_metadata = {
                str(item.get("name")): item
                for item in (listing.get("value") or [])
                if isinstance(item, dict) and item.get("name")
            }
            available = set(available_metadata)
            targets = tuple(expected_outputs) or tuple(
                sorted(available - set(attachments))
            )
            for name in targets:
                if name not in available:
                    audit.append(StepLog("artifact-missing", {"name": name}))
                    result.succeeded = False
                    result.stderr = f"필수 결과 파일이 없다: {name}"
                    continue
                size = available_metadata[name].get("size")
                if (
                    not isinstance(size, int)
                    or size < 0
                    or size > staging.MAX_ARTIFACT_BYTES
                ):
                    audit.append(
                        StepLog(
                            "artifact-oversized",
                            {"name": name, "size": size},
                        )
                    )
                    result.succeeded = False
                    result.stderr = (
                        f"{name} 크기 metadata가 artifact 허용 범위를 벗어났다"
                    )
                    continue
                payload = session.download(name)
                artifact = store.stage(name, payload)
                audit.append(StepLog("artifact-staged", artifact.as_dict()))
                result.artifacts.append(artifact.as_dict())

            manifest_path = store.write_manifest()
            audit.append(StepLog("manifest", {"path": str(manifest_path)}))

            complete_artifact_set = (
                result.succeeded
                and len(store.artifacts) == len(targets)
            )
            if approve and store.artifacts and complete_artifact_set:
                result.promotions = staging.promote_batch(
                    store.artifacts,
                    self.settings.approved_dir / request_id,
                    approver=approver,
                )
            else:
                result.promotions = [
                    {
                        "name": artifact.name,
                        "promoted": False,
                        "reason": "승인되지 않음",
                    }
                    for artifact in store.artifacts
                ]
            audit.append(
                StepLog(
                    "approval",
                    {
                        "approved": approve,
                        "approver": approver if approve else None,
                        "promoted": sum(
                            1 for item in result.promotions if item.get("promoted")
                        ),
                    },
                )
            )
            return result
        finally:
            try:
                status = session.delete()
                audit.append(
                    StepLog("session-deleted", {"httpStatus": status})
                )
            except Exception as error:
                audit.append(
                    StepLog(
                        "session-delete-failed",
                        {"error": str(error)[:500]},
                    )
                )
            result.audit = [entry.as_dict() for entry in audit]


def write_audit(result: RunResult, directory: Path) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{result.request_id}-audit.json"
    path.write_text(
        json.dumps(result.audit_view(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path
