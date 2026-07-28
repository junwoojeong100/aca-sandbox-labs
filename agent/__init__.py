"""AI Workspace 격리형 Sandbox reference orchestration.

이 package는 자연어 요청을 받아 정책을 적용하고, LLM으로 계획과 코드를
생성한 뒤 주입된 격리 실행 backend에서 실행하고, 산출물을 staging에 두어
승인 후에만 반영하는 backend-neutral reference implementation이다.

Production 배포용이 아니라 구조와 trust boundary를 설명하기 위한 예제다.
"""

__all__ = [
    "auth",
    "config",
    "execution",
    "ids",
    "llm",
    "orchestrator",
    "policy",
    "staging",
]
