import hashlib
import json
import mimetypes
import subprocess
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import openpyxl
import pptx
from openpyxl import Workbook
from pptx import Presentation


BASE_DIR = Path("/work")
MAX_REQUEST_BYTES = 1024 * 1024
PORT = 8080


def tool_version(command: list[str]) -> str:
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    output = result.stdout.strip() or result.stderr.strip()
    return output.splitlines()[0]


TOOL_VERSIONS = {
    "libreoffice": tool_version(["libreoffice", "--version"]),
    "openpyxl": openpyxl.__version__,
    "pandoc": tool_version(["pandoc", "--version"]),
    "pdftotext": tool_version(["pdftotext", "-v"]),
    "python-pptx": pptx.__version__,
}


def file_metadata(path: Path, job_id: str) -> dict[str, object]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "name": path.name,
        "size": path.stat().st_size,
        "sha256": digest,
        "downloadPath": f"/files/{job_id}/{path.name}",
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "OfficeSandbox/1.0"

    def send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_json(
                HTTPStatus.OK,
                {"status": "ok", "tools": TOOL_VERSIONS},
            )
            return

        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) == 3 and parts[0] == "files":
            job_id, filename = parts[1], parts[2]
            if not job_id.isalnum() or filename not in {
                "report.docx",
                "report.pdf",
                "report.pptx",
                "report.xlsx",
            }:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid file path"})
                return

            path = BASE_DIR / job_id / filename
            if not path.is_file():
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "File not found"})
                return

            content = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header(
                "Content-Type",
                mimetypes.guess_type(filename)[0] or "application/octet-stream",
            )
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(content)
            return

        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/generate":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length"})
            return

        if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
            self.send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "Request body must be between 1 byte and 1 MB"},
            )
            return

        try:
            payload = json.loads(self.rfile.read(content_length))
        except json.JSONDecodeError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON"})
            return

        title = payload.get("title")
        content = payload.get("content")
        if not isinstance(title, str) or not title.strip():
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "title is required"})
            return
        if not isinstance(content, str) or not content.strip():
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "content is required"})
            return

        job_id = uuid.uuid4().hex
        job_dir = BASE_DIR / job_id
        job_dir.mkdir(parents=True)
        markdown_path = job_dir / "report.md"
        docx_path = job_dir / "report.docx"
        pdf_path = job_dir / "report.pdf"
        pptx_path = job_dir / "report.pptx"
        xlsx_path = job_dir / "report.xlsx"
        markdown_path.write_text(
            f"% {title.strip()}\n\n# {title.strip()}\n\n{content.strip()}\n",
            encoding="utf-8",
        )

        try:
            workbook = Workbook()
            worksheet = workbook.active
            worksheet.title = "Report"
            worksheet["A1"] = title.strip()
            worksheet["A2"] = "Generated by AI Workspace Office Sandbox"
            for index, line in enumerate(content.strip().splitlines(), start=4):
                worksheet.cell(row=index, column=1, value=line)
            worksheet.column_dimensions["A"].width = 80
            workbook.save(xlsx_path)

            presentation = Presentation()
            title_slide = presentation.slides.add_slide(
                presentation.slide_layouts[0]
            )
            title_slide.shapes.title.text = title.strip()
            title_slide.placeholders[1].text = "Generated by AI Workspace Office Sandbox"
            content_slide = presentation.slides.add_slide(
                presentation.slide_layouts[1]
            )
            content_slide.shapes.title.text = "Summary"
            text_frame = content_slide.placeholders[1].text_frame
            text_frame.clear()
            for index, line in enumerate(content.strip().splitlines()):
                paragraph = text_frame.paragraphs[0] if index == 0 else text_frame.add_paragraph()
                paragraph.text = line
            presentation.save(pptx_path)

            subprocess.run(
                ["pandoc", str(markdown_path), "--output", str(docx_path)],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            subprocess.run(
                [
                    "libreoffice",
                    f"-env:UserInstallation=file:///tmp/lo-profile-{job_id}",
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(job_dir),
                    str(docx_path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired as error:
            self.send_json(
                HTTPStatus.GATEWAY_TIMEOUT,
                {"error": f"Document conversion timed out: {error.cmd[0]}"},
            )
            return
        except subprocess.CalledProcessError as error:
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": f"Document conversion failed: {error.cmd[0]}",
                    "stderr": error.stderr[-2000:],
                },
            )
            return
        except OSError as error:
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"Document tool could not start: {error}"},
            )
            return

        if not all(
            path.is_file()
            for path in (docx_path, pdf_path, pptx_path, xlsx_path)
        ):
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "Expected document artifacts were not generated"},
            )
            return

        self.send_json(
            HTTPStatus.OK,
            {
                "jobId": job_id,
                "files": [
                    file_metadata(docx_path, job_id),
                    file_metadata(pdf_path, job_id),
                    file_metadata(pptx_path, job_id),
                    file_metadata(xlsx_path, job_id),
                ],
            },
        )

    def log_message(self, format_string: str, *args: object) -> None:
        print(
            json.dumps(
                {
                    "client": self.client_address[0],
                    "message": format_string % args,
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
