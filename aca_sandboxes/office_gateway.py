"""ACA Sandboxes Office user gateway entrypoint."""

from __future__ import annotations

import json
import os
from pathlib import Path

from office_gateway import http_server
from office_gateway.service import OfficeGatewayService
from . import cleanup, config
from .office_client import OfficeClient


def build_service() -> OfficeGatewayService:
    settings = config.Settings()
    work_root = Path(
        os.environ.get(
            "OFFICE_USER_WORK_DIR",
            ".work/aca-sandboxes/office-user",
        )
    )
    return OfficeGatewayService(
        lambda identifier: OfficeClient(settings, identifier),
        staging_root=work_root / "staging",
        approved_root=work_root / "approved",
        backend="aca-sandboxes",
    )


def main() -> None:
    settings = config.Settings()
    service = build_service()
    try:
        cleanup.cleanup_gateway_sandboxes(
            settings,
            "office-gateway",
            exclude_ids=service.active_backend_ids(),
        )
    except Exception as error:
        print(
            json.dumps(
                {
                    "level": "warning",
                    "message": "Office orphan cleanup failed",
                    "error": str(error),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    cleanup.start_cleanup_loop(
        settings,
        "office-gateway",
        exclude_ids_provider=service.active_backend_ids,
    )
    http_server.serve(
        service,
        host=os.environ.get("OFFICE_GATEWAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("OFFICE_GATEWAY_PORT", "8090")),
    )


if __name__ == "__main__":
    main()
