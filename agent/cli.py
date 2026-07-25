"""Reference orchestrator CLI.

사용 예:

    python -m agent.cli \\
      --request "첨부한 매출 CSV를 월별로 집계하고 차트를 만들어줘" \\
      --attach .work/agent/sales.csv \\
      --expect monthly_sales.png --expect summary.json \\
      --approve --approver junwoo

`--approve`를 주지 않으면 artifact는 staging에만 남고 승격되지 않는다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config, orchestrator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent.cli",
        description="AI Workspace 격리형 Sandbox reference orchestrator",
    )
    parser.add_argument("--request", required=True, help="사용자의 자연어 요청")
    parser.add_argument(
        "--attach",
        action="append",
        default=[],
        metavar="PATH",
        help="세션에 업로드할 첨부파일. 여러 번 지정할 수 있다.",
    )
    parser.add_argument(
        "--expect",
        action="append",
        default=[],
        metavar="NAME",
        help="staging으로 회수할 결과 파일 이름. 생략하면 새로 생긴 파일을 모두 회수한다.",
    )
    parser.add_argument("--tenant", default="tenant-demo")
    parser.add_argument("--user", default="user-demo")
    parser.add_argument(
        "--estimated-seconds",
        type=int,
        default=30,
        help="정책 엔진이 사용하는 예상 실행 시간",
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="검사 통과 artifact를 승인하고 승격한다.",
    )
    parser.add_argument("--approver", default="unapproved")
    parser.add_argument(
        "--audit-dir",
        default=".work/agent/audit",
        help="감사 로그 저장 위치",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)

    attachments: dict[str, bytes] = {}
    for item in arguments.attach:
        path = Path(item)
        if not path.is_file():
            print(f"첨부파일을 찾을 수 없다: {path}", file=sys.stderr)
            return 2
        attachments[path.name] = path.read_bytes()

    settings = config.Settings()
    try:
        settings.validate_llm()
    except config.ConfigError as error:
        print(f"설정 오류: {error}", file=sys.stderr)
        return 2

    runner = orchestrator.Orchestrator(settings)
    result = runner.run(
        arguments.request,
        tenant_id=arguments.tenant,
        user_id=arguments.user,
        attachments=attachments,
        expected_outputs=tuple(arguments.expect),
        approve=arguments.approve,
        approver=arguments.approver,
        estimated_seconds=arguments.estimated_seconds,
    )

    audit_path = orchestrator.write_audit(result, Path(arguments.audit_dir))
    print(json.dumps(result.user_view(), ensure_ascii=False, indent=2))
    print(f"\n감사 로그: {audit_path}", file=sys.stderr)
    return 0 if result.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
