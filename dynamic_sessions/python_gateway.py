"""Dynamic Sessions Python user gateway entrypoint."""

from __future__ import annotations

import os
from pathlib import Path

from agent import config as agent_config
from agent import orchestrator
from python_gateway import http_server
from python_gateway.service import PythonGatewayService
from . import config, policy
from .python_session import PythonSession


def build_service() -> PythonGatewayService:
    work_root = Path(
        os.environ.get(
            "PYTHON_USER_WORK_DIR",
            ".work/dynamic-sessions/python-user",
        )
    )
    agent_settings = agent_config.Settings(
        staging_dir=work_root / "staging",
        approved_dir=work_root / "unused-orchestrator-approved",
        execution_timeout_seconds=int(
            os.environ.get(
                "DYNAMIC_SESSIONS_EXECUTION_TIMEOUT_SECONDS",
                "220",
            )
        ),
    )
    backend_settings = config.Settings()
    return PythonGatewayService(
        lambda: orchestrator.Orchestrator(
            lambda: PythonSession(backend_settings),
            "dynamic-sessions",
            policy.classify,
            agent_settings,
        ),
        staging_root=agent_settings.staging_dir,
        approved_root=work_root / "approved",
        backend="dynamic-sessions",
        max_active_jobs=int(
            os.environ.get("MAX_ACTIVE_PYTHON_JOBS", "5")
        ),
    )


def main() -> None:
    http_server.serve(
        build_service(),
        host=os.environ.get("PYTHON_GATEWAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("PYTHON_GATEWAY_PORT", "8089")),
    )


if __name__ == "__main__":
    main()
