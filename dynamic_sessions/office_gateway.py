"""Dynamic Sessions Office user gateway entrypoint."""

from __future__ import annotations

import os
from pathlib import Path

from office_gateway import http_server
from office_gateway.service import OfficeGatewayService
from . import config
from .office_client import OfficeClient


def build_service() -> OfficeGatewayService:
    settings = config.Settings()
    work_root = Path(
        os.environ.get(
            "OFFICE_USER_WORK_DIR",
            ".work/dynamic-sessions/office-user",
        )
    )
    return OfficeGatewayService(
        lambda identifier: OfficeClient(settings, identifier),
        staging_root=work_root / "staging",
        approved_root=work_root / "approved",
        backend="dynamic-sessions",
    )


def main() -> None:
    http_server.serve(
        build_service(),
        host=os.environ.get("OFFICE_GATEWAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("OFFICE_GATEWAY_PORT", "8090")),
    )


if __name__ == "__main__":
    main()
