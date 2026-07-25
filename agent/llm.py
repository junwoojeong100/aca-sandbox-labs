"""LLM client.

`azure-openai` provider는 Entra token으로 Azure OpenAI chat completions를 호출한다.
`stub` provider는 LLM 배포 없이도 실습과 CI가 동작하도록 결정론적 계획을 만든다.

LLM에는 절대 다음을 전달하지 않는다.
- session identifier, access token, pool endpoint
- production credential과 connector 정보
- 다른 tenant의 데이터
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import broker, config

SYSTEM_PROMPT = """\
너는 격리된 sandbox에서 실행할 Python 코드를 만드는 데이터 분석 assistant다.

규칙:
- 반드시 JSON 하나만 출력한다. markdown code fence를 쓰지 않는다.
- JSON 형식: {"plan": "한국어 한 문장", "code": "python source"}
- 입력 파일은 /mnt/data 아래에 있다. 결과 파일도 /mnt/data 아래에 쓴다.
- 외부 network 접근은 차단돼 있다. requests, urllib, socket, pip install을 쓰지 않는다.
- subprocess, os.system 같은 shell 실행을 쓰지 않는다.
- 표준 라이브러리와 사전 설치된 분석 라이브러리만 쓴다.
- 실행 시간은 220초 미만이어야 한다.
- 마지막에 무엇을 만들었는지 print로 요약한다.
"""

FIX_PROMPT = """\
직전 코드가 sandbox에서 실패했다. 오류를 고친 전체 코드를 다시 만든다.
같은 JSON 형식으로만 답한다. 같은 오류를 반복하지 않는다.
"""


class LLMError(RuntimeError):
    """LLM 호출 또는 응답 파싱 실패."""


class _UnsupportedParameter(RuntimeError):
    """모델이 요청 payload의 파라미터를 지원하지 않는 경우의 내부 신호."""


@dataclass
class Plan:
    plan: str
    code: str
    provider: str


def _extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"LLM 응답에서 JSON을 찾지 못했다: {text[:400]}") from None
        try:
            return json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as error:
            raise LLMError(f"LLM 응답 JSON 파싱 실패: {text[:400]}") from error


class StubClient:
    """LLM 배포 없이 흐름을 검증하기 위한 결정론적 planner.

    실제 모델이 아니므로 요청 문구가 아니라 첨부 파일 형태로 계획을 고른다.
    """

    provider = "stub"

    def __init__(self) -> None:
        self._attempt = 0

    def plan(
        self,
        request_text: str,
        *,
        attachments: tuple[str, ...] = (),
        failure: str | None = None,
    ) -> Plan:
        self._attempt += 1
        csv_files = [name for name in attachments if name.lower().endswith(".csv")]
        target = csv_files[0] if csv_files else "input.csv"

        if failure is None and self._attempt == 1 and "오류" in request_text:
            # 오류 복구 루프를 시연하기 위해 존재하지 않는 열을 참조한다.
            code = _CSV_TEMPLATE.format(path=target, column="sales_amount")
            return Plan(
                plan=f"{target}의 sales_amount 열을 월별로 합계한다.",
                code=code,
                provider=self.provider,
            )

        code = _CSV_TEMPLATE.format(path=target, column="amount")
        return Plan(
            plan=f"{target}를 월별로 집계하고 차트와 요약 JSON을 만든다.",
            code=code,
            provider=self.provider,
        )


_CSV_TEMPLATE = '''\
import csv
import json
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

monthly = defaultdict(float)
products = defaultdict(float)
with open("/mnt/data/{path}", newline="", encoding="utf-8") as source:
    for row in csv.DictReader(source):
        amount = float(row["{column}"])
        monthly[row["month"]] += amount
        products[row["product"]] += amount

months = sorted(monthly)
plt.figure()
plt.plot(months, [monthly[month] for month in months], marker="o")
plt.title("Monthly total")
plt.xlabel("Month")
plt.ylabel("Amount")
plt.tight_layout()
plt.savefig("/mnt/data/monthly_sales.png")

with open("/mnt/data/summary.json", "w", encoding="utf-8") as output:
    json.dump(
        {{
            "monthly_sales": dict(sorted(monthly.items())),
            "top_products": sorted(
                products.items(), key=lambda item: item[1], reverse=True
            )[:5],
        }},
        output,
        ensure_ascii=False,
        indent=2,
    )

print("generated monthly_sales.png and summary.json")
'''


class AzureOpenAIClient:
    """Entra 인증 Azure OpenAI chat completions client. API key를 쓰지 않는다."""

    provider = "azure-openai"

    def __init__(self, settings: config.Settings) -> None:
        settings.validate_llm()
        self.settings = settings
        self._history: list[dict[str, str]] = []
        self._legacy_payload = False

    def _url(self) -> str:
        endpoint = (self.settings.azure_openai_endpoint or "").rstrip("/")
        deployment = self.settings.azure_openai_deployment
        version = self.settings.azure_openai_api_version
        return (
            f"{endpoint}/openai/deployments/{deployment}"
            f"/chat/completions?api-version={version}"
        )

    def _call(self, messages: list[dict[str, str]]) -> str:
        """Chat completions를 호출한다.

        추론 모델(gpt-5.x 계열)과 기존 모델(gpt-4o 계열)의 payload 규약이 다르다.
        추론 모델은 `max_completion_tokens`를 쓰고 `temperature` 변경을 허용하지 않으며
        `reasoning_effort`를 받는다. 먼저 추론 모델 규약으로 호출하고, 서버가
        해당 파라미터를 모른다고 응답하면 기존 모델 규약으로 한 번 재시도한다.
        """
        payload = {
            "messages": messages,
            "max_completion_tokens": self.settings.max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        if self.settings.reasoning_effort:
            payload["reasoning_effort"] = self.settings.reasoning_effort

        try:
            return self._post(payload)
        except _UnsupportedParameter as error:
            if self._legacy_payload:
                raise LLMError(
                    f"Azure OpenAI가 요청 파라미터를 거부했다: {error}"
                ) from error
            self._legacy_payload = True

        legacy = {
            "messages": messages,
            "max_tokens": self.settings.max_output_tokens,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        try:
            return self._post(legacy)
        except _UnsupportedParameter as error:
            raise LLMError(f"Azure OpenAI가 요청 파라미터를 거부했다: {error}") from error

    def _post(self, payload: dict[str, object]) -> str:
        token = broker.get_token(config.COGNITIVE_SERVICES_SCOPE)
        request = urllib.request.Request(
            self._url(),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            if error.code == 400 and (
                "unsupported_parameter" in detail or "unsupported_value" in detail
            ):
                raise _UnsupportedParameter(detail[:400]) from error
            raise LLMError(
                f"Azure OpenAI 호출 실패 (HTTP {error.code}): {detail[:1000]}"
            ) from error
        except urllib.error.URLError as error:
            raise LLMError(f"Azure OpenAI 연결 실패: {error.reason}") from error

        choices = body.get("choices") or []
        if not choices:
            raise LLMError("Azure OpenAI 응답에 choices가 없다")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not content:
            reason = choices[0].get("finish_reason")
            raise LLMError(
                "Azure OpenAI 응답 content가 비었다. "
                f"finish_reason={reason}. 추론 모델이면 max_output_tokens를 늘린다."
            )
        return content

    def plan(
        self,
        request_text: str,
        *,
        attachments: tuple[str, ...] = (),
        failure: str | None = None,
    ) -> Plan:
        if failure is None:
            attachment_note = (
                f"\n\n첨부 파일: {', '.join(attachments)}" if attachments else ""
            )
            self._history = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": request_text + attachment_note},
            ]
        else:
            self._history.append(
                {"role": "user", "content": f"{FIX_PROMPT}\n\n오류:\n{failure}"}
            )

        content = self._call(self._history)
        self._history.append({"role": "assistant", "content": content})
        data = _extract_json(content)
        code = data.get("code")
        if not isinstance(code, str) or not code.strip():
            raise LLMError("LLM 응답에 code가 없다")
        return Plan(
            plan=str(data.get("plan", "")).strip() or "(계획 없음)",
            code=code,
            provider=self.provider,
        )


def create_client(settings: config.Settings):
    """설정에 맞는 LLM client를 만든다."""
    settings.validate_llm()
    if settings.llm_provider == "azure-openai":
        return AzureOpenAIClient(settings)
    return StubClient()
