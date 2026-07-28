"""Dynamic Sessions orchestrator CLI."""

from __future__ import annotations

import os
from pathlib import Path

from agent import cli_common, config as agent_config
from . import config
from . import policy
from .python_session import PythonSession


def main(argv: list[str] | None = None) -> int:
    settings = config.Settings()
    return cli_common.run(
        argv=argv,
        prog="dynamic_sessions.cli",
        backend_name="dynamic-sessions",
        session_factory=lambda: PythonSession(settings),
        classifier=policy.classify,
        default_audit_dir=".work/dynamic-sessions/agent/audit",
        settings=agent_config.Settings(
            staging_dir=Path(
                os.environ.get(
                    "STAGING_DIR",
                    ".work/dynamic-sessions/agent/staging",
                )
            ),
            approved_dir=Path(
                os.environ.get(
                    "APPROVED_DIR",
                    ".work/dynamic-sessions/agent/approved",
                )
            ),
            execution_timeout_seconds=int(
                os.environ.get(
                    "DYNAMIC_SESSIONS_EXECUTION_TIMEOUT_SECONDS",
                    "220",
                )
            ),
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
