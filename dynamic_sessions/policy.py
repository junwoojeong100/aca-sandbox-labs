"""Dynamic Sessions request policy."""

from __future__ import annotations

from agent import policy

MAX_EXECUTION_SECONDS = 220
MAX_UPLOAD_BYTES = 128 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 128 * 1024 * 1024
RULES_VERSION = "dynamic-sessions-2026-07-29"


def classify(request: policy.PolicyInput) -> policy.PolicyDecision:
    decision = policy.classify_base(
        request,
        rules_version=RULES_VERSION,
    )
    controls = {
        "network": "EgressDisabled",
        "maxCodeRetries": 2,
        "writeToBusinessSystem": False,
    }
    if decision.route is policy.Route.OFFICE:
        controls.update(
            runtime="custom-container",
            allowedOperations=["generate", "convert", "edit"],
        )
        decision.controls.update(controls)
        return decision
    if decision.route is not policy.Route.PYTHON:
        decision.controls.update(controls)
        return decision

    controls.update(
        runtime="code-interpreter",
        maxExecutionSeconds=MAX_EXECUTION_SECONDS,
        maxUploadBytesPerFile=MAX_UPLOAD_BYTES,
        maxTotalAttachmentBytes=MAX_TOTAL_ATTACHMENT_BYTES,
    )
    largest = max(request.attachment_sizes, default=0)
    total = sum(request.attachment_sizes)
    if largest > MAX_UPLOAD_BYTES:
        return policy.PolicyDecision(
            classification="C",
            route=policy.Route.ASYNC_COMPUTE,
            allowed=False,
            reason=f"첨부파일이 Code Interpreter {MAX_UPLOAD_BYTES} byte 한도를 초과",
            rules_version=RULES_VERSION,
            controls=controls,
        )
    if total > MAX_TOTAL_ATTACHMENT_BYTES:
        return policy.PolicyDecision(
            classification="C",
            route=policy.Route.ASYNC_COMPUTE,
            allowed=False,
            reason="첨부파일 전체 크기가 Reference Gateway 한도를 초과",
            rules_version=RULES_VERSION,
            controls=controls,
        )
    if request.estimated_seconds > MAX_EXECUTION_SECONDS:
        return policy.PolicyDecision(
            classification="C",
            route=policy.Route.ASYNC_COMPUTE,
            allowed=False,
            reason=f"예상 실행 시간이 Code Interpreter {MAX_EXECUTION_SECONDS}초 한도를 초과",
            rules_version=RULES_VERSION,
            controls=controls,
        )
    decision.controls.update(controls)
    return decision
