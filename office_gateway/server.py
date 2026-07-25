"""Local reference HTTP API for Office end-user labs."""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .service import (
    GatewayError,
    OfficeGatewayService,
    OfficeSessionClient,
    resolve_office_endpoint,
)

MAX_REQUEST_BYTES = 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    """Expose safe document job operations without Azure session credentials."""

    service: OfficeGatewayService
    server_version = "OfficeUserGateway/1.0"

    def send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise GatewayError(400, "Content-Length가 올바르지 않다") from error
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise GatewayError(400, "1MB 이하의 JSON body가 필요하다")
        if not self.headers.get("Content-Type", "").lower().startswith(
            "application/json"
        ):
            raise GatewayError(415, "Content-Type은 application/json이어야 한다")
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise GatewayError(400, "JSON body가 올바르지 않다") from error
        if not isinstance(payload, dict):
            raise GatewayError(400, "JSON body는 object여야 한다")
        return payload

    def user(self) -> str:
        return self.service.validate_user(self.headers.get("X-Demo-User", ""))

    def handle_error(self, error: GatewayError) -> None:
        self.send_json(
            error.status,
            {"error": error.message, **error.details},
        )

    @staticmethod
    def parts(path: str) -> list[str]:
        return [unquote(part) for part in path.split("/") if part]

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/health":
                self.send_json(HTTPStatus.OK, {"status": "ok"})
                return

            parts = self.parts(path)
            if len(parts) == 3 and parts[:2] == ["api", "document-jobs"]:
                self.send_json(
                    HTTPStatus.OK,
                    self.service.get(self.user(), parts[2]),
                )
                return
            if (
                len(parts) == 5
                and parts[:2] == ["api", "document-jobs"]
                and parts[3] == "files"
            ):
                payload, content_type = self.service.download(
                    self.user(),
                    parts[2],
                    parts[4],
                )
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{parts[4]}"',
                )
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except GatewayError as error:
            self.handle_error(error)

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            parts = self.parts(path)
            payload = self.read_json()
            user = self.user()

            if parts == ["api", "document-jobs"]:
                self.send_json(
                    HTTPStatus.CREATED,
                    self.service.create(
                        user,
                        payload.get("title"),
                        payload.get("content"),
                    ),
                )
                return
            if len(parts) == 4 and parts[:2] == ["api", "document-jobs"]:
                public_id, action = parts[2], parts[3]
                if action == "convert":
                    self.send_json(
                        HTTPStatus.OK,
                        self.service.convert(
                            user,
                            public_id,
                            payload.get("source"),
                            payload.get("target"),
                        ),
                    )
                    return
                if action == "edit":
                    self.send_json(
                        HTTPStatus.OK,
                        self.service.edit(
                            user,
                            public_id,
                            payload.get("operations"),
                        ),
                    )
                    return
                if action == "approve":
                    self.send_json(
                        HTTPStatus.OK,
                        self.service.approve(user, public_id),
                    )
                    return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except GatewayError as error:
            self.handle_error(error)

    def do_DELETE(self) -> None:
        try:
            parts = self.parts(urlparse(self.path).path)
            if len(parts) == 3 and parts[:2] == ["api", "document-jobs"]:
                self.service.delete(self.user(), parts[2])
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except GatewayError as error:
            self.handle_error(error)


def build_service() -> OfficeGatewayService:
    resource_group = os.environ.get(
        "RESOURCE_GROUP",
        "rg-ai-workspace-sandbox-lab",
    )
    pool_name = os.environ.get(
        "OFFICE_POOL_NAME",
        "ai-workspace-office-sbx",
    )
    endpoint = os.environ.get("OFFICE_ENDPOINT") or resolve_office_endpoint(
        resource_group,
        pool_name,
    )
    work_root = Path(os.environ.get("OFFICE_USER_WORK_DIR", ".work/office-user"))
    return OfficeGatewayService(
        lambda identifier: OfficeSessionClient(endpoint, identifier),
        staging_root=work_root / "staging",
        approved_root=work_root / "approved",
    )


def main() -> None:
    host = os.environ.get("OFFICE_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("OFFICE_GATEWAY_PORT", "8090"))
    Handler.service = build_service()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Office user gateway listening on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
