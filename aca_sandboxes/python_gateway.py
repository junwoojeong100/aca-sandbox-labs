"""ACA Sandboxes Python user gateway entrypoint."""

from __future__ import annotations

import json
import os
from pathlib import Path

from agent import config as agent_config
from agent import orchestrator
from python_gateway import http_server
from python_gateway.service import PythonGatewayService
from . import cleanup, config, policy
from .python_session import PythonSession


def build_service() -> PythonGatewayService:
    work_root = Path(
        os.environ.get(
            "PYTHON_USER_WORK_DIR",
            ".work/aca-sandboxes/python-user",
        )
    )
    backend_settings = config.Settings()
    agent_settings = agent_config.Settings(
        staging_dir=work_root / "staging",
        approved_dir=work_root / "unused-orchestrator-approved",
        execution_timeout_seconds=backend_settings.execution_timeout_seconds,
    )
    return PythonGatewayService(
        lambda: orchestrator.Orchestrator(
            lambda: PythonSession(backend_settings),
            "aca-sandboxes",
            policy.classify,
            agent_settings,
        ),
        staging_root=agent_settings.staging_dir,
        approved_root=work_root / "approved",
        backend="aca-sandboxes",
        max_active_jobs=int(
            os.environ.get("MAX_ACTIVE_PYTHON_JOBS", "5")
        ),
    )


def main() -> None:
    settings = config.Settings()
    try:
        cleanup.cleanup_gateway_sandboxes(settings, "python-gateway")
    except Exception as error:
        print(
            json.dumps(
                {
                    "level": "warning",
                    "message": "Python orphan cleanup failed",
                    "error": str(error),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    cleanup.start_cleanup_loop(settings, "python-gateway")
    http_server.serve(
        build_service(),
        host=os.environ.get("PYTHON_GATEWAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("PYTHON_GATEWAY_PORT", "8089")),
    )


if __name__ == "__main__":
    main()
