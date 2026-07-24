import hashlib
import json
import mimetypes
import shutil
import subprocess
import threading
import time
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
MAX_TITLE_CHARS = 200
MAX_CONTENT_CHARS = 100_000
MAX_JOBS = 20
MAX_STORAGE_BYTES = 256 * 1024 * 1024
JOB_TTL_SECONDS = 3600
PORT = 8080
JOB_LOCK = threading.Lock()


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
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            digest.update(chunk)
    return {
        "name": path.name,
        "size": path.stat().st_size,
        "sha256": digest.hexdigest(),
        "downloadPath": f"/files/{job_id}/{path.name}",
    }


def directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def cleanup_expired_jobs(now: float | None = None) -> None:
    cutoff = (now or time.time()) - JOB_TTL_SECONDS
    for job_dir in BASE_DIR.iterdir():
        if job_dir.is_dir() and job_dir.stat().st_mtime < cutoff:
            shutil.rmtree(job_dir)


def remove_job(job_dir: Path) -> None:
    try:
        shutil.rmtree(job_dir)
    except OSError as error:
        print(
            json.dumps(
                {
                    "level": "error",
                    "message": "Failed to remove job directory",
                    "jobDirectory": str(job_dir),
                    "error": str(error),
                }
            ),
            flush=True,
        )


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
                {
                    "status": "ok",
                    "tools": TOOL_VERSIONS,
                    "limits": {
                        "maxRequestBytes": MAX_REQUEST_BYTES,
                        "maxJobs": MAX_JOBS,
                        "maxStorageBytes": MAX_STORAGE_BYTES,
                        "jobTtlSeconds": JOB_TTL_SECONDS,
                    },
                },
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

            self.send_response(HTTPStatus.OK)
            self.send_header(
                "Content-Type",
                mimetypes.guess_type(filename)[0] or "application/octet-stream",
            )
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            with path.open("rb") as source:
                shutil.copyfileobj(source, self.wfile, length=64 * 1024)
            return

        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/generate":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("application/json"):
            self.send_json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "Content-Type must be application/json"},
            )
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
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON"})
            return
        if not isinstance(payload, dict):
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "JSON body must be an object"},
            )
            return

        title = payload.get("title")
        content = payload.get("content")
        if not isinstance(title, str) or not title.strip():
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "title is required"})
            return
        if not isinstance(content, str) or not content.strip():
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "content is required"})
            return
        if len(title) > MAX_TITLE_CHARS:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": f"title must be at most {MAX_TITLE_CHARS} characters"},
            )
            return
        if len(content) > MAX_CONTENT_CHARS:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": f"content must be at most {MAX_CONTENT_CHARS} characters"},
            )
            return

        job_id = uuid.uuid4().hex
        job_dir = BASE_DIR / job_id
        try:
            with JOB_LOCK:
                cleanup_expired_jobs()
                job_directories = [
                    path for path in BASE_DIR.iterdir() if path.is_dir()
                ]
                if len(job_directories) >= MAX_JOBS:
                    self.send_json(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        {"error": "Session job limit reached"},
                    )
                    return
                if directory_size(BASE_DIR) >= MAX_STORAGE_BYTES:
                    self.send_json(
                        HTTPStatus.INSUFFICIENT_STORAGE,
                        {"error": "Session storage limit reached"},
                    )
                    return
                job_dir.mkdir(parents=True)
        except OSError as error:
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"Unable to allocate job workspace: {error}"},
            )
            return

        markdown_path = job_dir / "report.md"
        docx_path = job_dir / "report.docx"
        pdf_path = job_dir / "report.pdf"
        pptx_path = job_dir / "report.pptx"
        xlsx_path = job_dir / "report.xlsx"

        try:
            markdown_path.write_text(
                f"% {title.strip()}\n\n# {title.strip()}\n\n{content.strip()}\n",
                encoding="utf-8",
            )
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
                paragraph = (
                    text_frame.paragraphs[0]
                    if index == 0
                    else text_frame.add_paragraph()
                )
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
            remove_job(job_dir)
            self.send_json(
                HTTPStatus.GATEWAY_TIMEOUT,
                {"error": f"Document conversion timed out: {error.cmd[0]}"},
            )
            return
        except subprocess.CalledProcessError as error:
            remove_job(job_dir)
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": f"Document conversion failed: {error.cmd[0]}",
                    "stderr": (error.stderr or "")[-2000:],
                },
            )
            return
        except (KeyError, ValueError) as error:
            remove_job(job_dir)
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"Document generation failed: {error}"},
            )
            return
        except OSError as error:
            remove_job(job_dir)
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"Document tool could not start: {error}"},
            )
            return

        if not all(
            path.is_file()
            for path in (docx_path, pdf_path, pptx_path, xlsx_path)
        ):
            remove_job(job_dir)
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "Expected document artifacts were not generated"},
            )
            return

        try:
            with JOB_LOCK:
                if directory_size(BASE_DIR) > MAX_STORAGE_BYTES:
                    remove_job(job_dir)
                    self.send_json(
                        HTTPStatus.INSUFFICIENT_STORAGE,
                        {"error": "Generated artifacts exceed the session storage limit"},
                    )
                    return
        except OSError as error:
            remove_job(job_dir)
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"Unable to validate generated artifacts: {error}"},
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
        status = args[1] if len(args) > 1 else None
        print(
            json.dumps(
                {
                    "client": self.client_address[0],
                    "method": self.command,
                    "path": urlparse(self.path).path,
                    "status": status,
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
