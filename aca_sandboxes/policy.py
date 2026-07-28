"""ACA Sandboxes request policy."""

from __future__ import annotations

from agent import policy

MAX_TOTAL_ATTACHMENT_BYTES = 128 * 1024 * 1024
MAX_EXECUTION_SECONDS = 900
RULES_VERSION = "aca-sandboxes-2026-07-29"


def classify(request: policy.PolicyInput) -> policy.PolicyDecision:
    decision = policy.classify_base(
        request,
        rules_version=RULES_VERSION,
    )
    controls = {
        "network": "Deny",
        "trafficInspection": "Full",
        "maxCodeRetries": 2,
        "writeToBusinessSystem": False,
    }
    if decision.route is policy.Route.OFFICE:
        controls.update(
            runtime="aca-office-sandbox",
            allowedOperations=["generate", "convert", "edit"],
        )
    elif decision.route is policy.Route.PYTHON:
        controls.update(
            runtime="aca-python-sandbox",
            maxTotalAttachmentBytes=MAX_TOTAL_ATTACHMENT_BYTES,
            maxExecutionSeconds=MAX_EXECUTION_SECONDS,
        )
        if sum(request.attachment_sizes) > MAX_TOTAL_ATTACHMENT_BYTES:
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
                reason=(
                    f"예상 실행 시간이 ACA reference limit "
                    f"{MAX_EXECUTION_SECONDS}초를 초과"
                ),
                rules_version=RULES_VERSION,
                controls=controls,
            )
    decision.controls.update(controls)
    return decision
