"""Local reference HTTP API for Python analysis end-user labs."""

from __future__ import annotations

import json
import mimetypes
import os
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from agent import config, orchestrator
from .service import GatewayError, PythonGatewayService

MAX_MULTIPART_BYTES = 128 * 1024 * 1024 + 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    """Expose safe analysis jobs without Dynamic Sessions credentials."""

    service: PythonGatewayService
    server_version = "PythonUserGateway/1.0"

    def send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def user(self) -> str:
        return self.service.validate_user(self.headers.get("X-Demo-User", ""))

    @staticmethod
    def parts(path: str) -> list[str]:
        return [unquote(part) for part in path.split("/") if part]

    def read_multipart(
        self,
    ) -> tuple[str, dict[str, bytes], tuple[str, ...]]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            raise GatewayError(415, "Content-Type은 multipart/form-data여야 한다")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise GatewayError(400, "Content-Length가 올바르지 않다") from error
        if length <= 0 or length > MAX_MULTIPART_BYTES:
            raise GatewayError(413, "요청 body가 허용 크기를 초과했다")
        body = self.rfile.read(length)
        message = BytesParser(policy=default).parsebytes(
            (
                f"Content-Type: {content_type}\r\n"
                "MIME-Version: 1.0\r\n\r\n"
            ).encode("utf-8")
            + body
        )
        if not message.is_multipart():
            raise GatewayError(400, "multipart body가 올바르지 않다")

        request_text = ""
        attachments: dict[str, bytes] = {}
        expected_outputs: list[str] = []
        for part in message.iter_parts():
            if part.get_content_disposition() != "form-data":
                continue
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            try:
                if name == "request" and filename is None:
                    request_text = payload.decode("utf-8")
                elif name == "expected" and filename is None:
                    expected_outputs.append(payload.decode("utf-8"))
                elif name == "file" and filename:
                    if filename in attachments:
                        raise GatewayError(
                            400,
                            f"중복 첨부파일 이름: {filename}",
                        )
                    attachments[filename] = payload
            except UnicodeDecodeError as error:
                raise GatewayError(400, "multipart text field는 UTF-8이어야 한다") from error
        return request_text, attachments, tuple(expected_outputs)

    def read_json(self) -> dict[str, object]:
        if not self.headers.get("Content-Type", "").lower().startswith(
            "application/json"
        ):
            raise GatewayError(415, "Content-Type은 application/json이어야 한다")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise GatewayError(400, "Content-Length가 올바르지 않다") from error
        if length <= 0 or length > 1024 * 1024:
            raise GatewayError(400, "1MB 이하의 JSON body가 필요하다")
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise GatewayError(400, "JSON body가 올바르지 않다") from error
        if not isinstance(payload, dict):
            raise GatewayError(400, "JSON body는 object여야 한다")
        return payload

    def handle_error(self, error: GatewayError) -> None:
        self.send_json(error.status, {"error": error.message})

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/health":
                self.send_json(HTTPStatus.OK, {"status": "ok"})
                return
            parts = self.parts(path)
            if len(parts) == 3 and parts[:2] == ["api", "analysis-jobs"]:
                self.send_json(
                    HTTPStatus.OK,
                    self.service.get(self.user(), parts[2]),
                )
                return
            if (
                len(parts) == 5
                and parts[:2] == ["api", "analysis-jobs"]
                and parts[3] == "files"
            ):
                payload, detected_type = self.service.download(
                    self.user(),
                    parts[2],
                    parts[4],
                )
                content_type = (
                    mimetypes.guess_type(parts[4])[0]
                    or detected_type
                    or "application/octet-stream"
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
            parts = self.parts(urlparse(self.path).path)
            user = self.user()
            if parts == ["api", "analysis-jobs"]:
                request_text, attachments, expected_outputs = self.read_multipart()
                self.send_json(
                    HTTPStatus.CREATED,
                    self.service.create(
                        user,
                        request_text,
                        attachments,
                        expected_outputs,
                    ),
                )
                return
            if (
                len(parts) == 4
                and parts[:2] == ["api", "analysis-jobs"]
                and parts[3] == "approve"
            ):
                self.read_json()
                self.send_json(
                    HTTPStatus.OK,
                    self.service.approve(user, parts[2]),
                )
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except GatewayError as error:
            self.handle_error(error)

    def do_DELETE(self) -> None:
        try:
            parts = self.parts(urlparse(self.path).path)
            if len(parts) == 3 and parts[:2] == ["api", "analysis-jobs"]:
                self.service.delete(self.user(), parts[2])
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except GatewayError as error:
            self.handle_error(error)


def build_service() -> PythonGatewayService:
    work_root = Path(os.environ.get("PYTHON_USER_WORK_DIR", ".work/python-user-api"))
    settings = config.Settings(
        staging_dir=work_root / "staging",
        approved_dir=work_root / "unused-orchestrator-approved",
    )
    return PythonGatewayService(
        lambda: orchestrator.Orchestrator(settings),
        staging_root=settings.staging_dir,
        approved_root=work_root / "approved",
    )


def main() -> None:
    host = os.environ.get("PYTHON_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("PYTHON_GATEWAY_PORT", "8089"))
    Handler.service = build_service()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Python user gateway listening on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
