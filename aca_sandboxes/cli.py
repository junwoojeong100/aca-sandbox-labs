"""ACA Sandboxes orchestrator CLI."""

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
        prog="aca_sandboxes.cli",
        backend_name="aca-sandboxes",
        session_factory=lambda: PythonSession(settings),
        classifier=policy.classify,
        default_audit_dir=".work/aca-sandboxes/agent/audit",
        settings=agent_config.Settings(
            staging_dir=Path(
                os.environ.get(
                    "STAGING_DIR",
                    ".work/aca-sandboxes/agent/staging",
                )
            ),
            approved_dir=Path(
                os.environ.get(
                    "APPROVED_DIR",
                    ".work/aca-sandboxes/agent/approved",
                )
            ),
            execution_timeout_seconds=settings.execution_timeout_seconds,
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
