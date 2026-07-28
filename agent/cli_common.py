"""Backend-neutral orchestrator CLI runner."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

from . import config, execution, orchestrator, policy


def build_parser(prog: str, default_audit_dir: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="AI Workspace isolated execution reference orchestrator",
    )
    parser.add_argument("--request", required=True)
    parser.add_argument("--attach", action="append", default=[], metavar="PATH")
    parser.add_argument("--expect", action="append", default=[], metavar="NAME")
    parser.add_argument("--tenant", default="tenant-demo")
    parser.add_argument("--user", default="user-demo")
    parser.add_argument("--estimated-seconds", type=int, default=30)
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--approver", default="unapproved")
    parser.add_argument("--audit-dir", default=default_audit_dir)
    return parser


def run(
    *,
    argv: list[str] | None,
    prog: str,
    backend_name: str,
    session_factory: Callable[[], execution.PythonExecution],
    classifier: Callable[[policy.PolicyInput], policy.PolicyDecision],
    default_audit_dir: str,
    settings: config.Settings,
) -> int:
    arguments = build_parser(prog, default_audit_dir).parse_args(argv)
    attachments: dict[str, bytes] = {}
    for item in arguments.attach:
        path = Path(item)
        if not path.is_file():
            print(f"첨부파일을 찾을 수 없다: {path}", file=sys.stderr)
            return 2
        attachments[path.name] = path.read_bytes()

    try:
        settings.validate_llm()
    except config.ConfigError as error:
        print(f"설정 오류: {error}", file=sys.stderr)
        return 2

    runner = orchestrator.Orchestrator(
        session_factory,
        backend_name,
        classifier,
        settings,
    )
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
    audit_path = orchestrator.write_audit(
        result,
        Path(arguments.audit_dir),
    )
    print(json.dumps(result.user_view(), ensure_ascii=False, indent=2))
    print(f"\n감사 로그: {audit_path}", file=sys.stderr)
    return 0 if result.succeeded else 1
