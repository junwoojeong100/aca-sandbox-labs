from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from office_gateway import server, service


class FakeOfficeClient:
    def __init__(self, identifier: str) -> None:
        self.identifier = identifier
        self.internal_job_id = "internal123"
        self.stopped = False
        self.payloads = {
            "report.docx": b"PK\x03\x04docx",
            "report.pdf": b"%PDF-1.4\npdf",
            "report.pptx": b"PK\x03\x04pptx",
            "report.xlsx": b"PK\x03\x04xlsx",
        }

    def metadata(self, name: str) -> dict[str, object]:
        payload = self.payloads[name]
        return {
            "name": name,
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "downloadPath": f"/files/{self.internal_job_id}/{name}",
        }

    def generate(self, title: str, content: str):
        return {
            "jobId": self.internal_job_id,
            "files": [self.metadata(name) for name in sorted(self.payloads)],
        }

    def convert(self, job_id: str, source: str, target: str):
        name = f"{source}.{target}"
        self.payloads[name] = b"%PDF-1.4\nconverted"
        return {"jobId": job_id, "files": [self.metadata(name)]}

    def edit(self, job_id: str, operations):
        self.payloads["report.edited.docx"] = b"PK\x03\x04edited-docx"
        self.payloads["report.edited.pptx"] = b"PK\x03\x04edited-pptx"
        self.payloads["report.edited.xlsx"] = b"PK\x03\x04edited-xlsx"
        return {
            "jobId": job_id,
            "applied": len(operations),
            "files": [
                self.metadata("report.edited.docx"),
                self.metadata("report.edited.pptx"),
                self.metadata("report.edited.xlsx"),
            ],
        }

    def download(self, path: str):
        name = path.rsplit("/", 1)[-1]
        return self.payloads[name], "application/octet-stream"

    def stop(self) -> None:
        self.stopped = True


class FailingOfficeClient(FakeOfficeClient):
    def generate(self, title: str, content: str):
        raise service.GatewayError(400, "generation failed")


class MalformedOfficeClient(FakeOfficeClient):
    def generate(self, title: str, content: str):
        raise ValueError("malformed response")


class OversizedOfficeClient(FakeOfficeClient):
    def generate(self, title: str, content: str):
        response = super().generate(title, content)
        response["files"][0]["size"] = service.staging.MAX_ARTIFACT_BYTES + 1
        return response


class StopFailingOfficeClient(FakeOfficeClient):
    def __init__(self, identifier: str) -> None:
        super().__init__(identifier)
        self.abandoned = False

    def stop(self) -> None:
        raise service.GatewayError(502, "transient cleanup failure")

    def abandon(self) -> None:
        self.abandoned = True


class OfficeGatewayServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.clients: list[FakeOfficeClient] = []

        def factory(identifier: str) -> FakeOfficeClient:
            client = FakeOfficeClient(identifier)
            self.clients.append(client)
            return client

        self.gateway = service.OfficeGatewayService(
            factory,
            staging_root=root / "staging",
            approved_root=root / "approved",
        )
        self.root = root

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_job(self) -> dict[str, object]:
        return self.gateway.create("user-demo", "Quarterly report", "Draft")

    def test_create_hides_internal_session_details(self) -> None:
        result = self.create_job()
        self.assertEqual(result["status"], "draft")
        self.assertNotIn("sessionIdentifier", result)
        self.assertNotIn("internalJobId", result)
        self.assertEqual(len(result["files"]), 4)

    def test_create_failure_stops_allocated_session(self) -> None:
        clients: list[FailingOfficeClient] = []

        def factory(identifier: str) -> FailingOfficeClient:
            client = FailingOfficeClient(identifier)
            clients.append(client)
            return client

        gateway = service.OfficeGatewayService(
            factory,
            staging_root=self.root / "failed-staging",
            approved_root=self.root / "failed-approved",
        )
        with self.assertRaises(service.GatewayError):
            gateway.create("user-demo", "title", "content")
        self.assertTrue(clients[0].stopped)

    def test_malformed_creation_response_stops_allocated_session(self) -> None:
        clients: list[MalformedOfficeClient] = []

        def factory(identifier: str) -> MalformedOfficeClient:
            client = MalformedOfficeClient(identifier)
            clients.append(client)
            return client

        gateway = service.OfficeGatewayService(
            factory,
            staging_root=self.root / "malformed-staging",
            approved_root=self.root / "malformed-approved",
        )
        with self.assertRaises(service.GatewayError):
            gateway.create("user-demo", "title", "content")
        self.assertTrue(clients[0].stopped)

    def test_oversized_metadata_stops_allocated_session(self) -> None:
        clients: list[OversizedOfficeClient] = []

        def factory(identifier: str) -> OversizedOfficeClient:
            client = OversizedOfficeClient(identifier)
            clients.append(client)
            return client

        gateway = service.OfficeGatewayService(
            factory,
            staging_root=self.root / "oversized-staging",
            approved_root=self.root / "oversized-approved",
        )
        with self.assertRaises(service.GatewayError) as context:
            gateway.create("user-demo", "title", "content")
        self.assertEqual(context.exception.status, 502)
        self.assertTrue(clients[0].stopped)
        self.assertEqual(gateway.jobs, {})

    def test_oversized_title_is_rejected_before_session_allocation(self) -> None:
        with self.assertRaises(service.GatewayError) as context:
            self.gateway.create("user-demo", "x" * 201, "content")
        self.assertEqual(context.exception.status, 400)
        self.assertEqual(self.clients, [])

    def test_active_job_limit_rejects_before_allocation(self) -> None:
        clients: list[FakeOfficeClient] = []

        def factory(identifier: str) -> FakeOfficeClient:
            client = FakeOfficeClient(identifier)
            clients.append(client)
            return client

        gateway = service.OfficeGatewayService(
            factory,
            staging_root=self.root / "limited-staging",
            approved_root=self.root / "limited-approved",
            max_active_jobs=1,
        )
        gateway.create("user-demo", "first", "draft")
        with self.assertRaises(service.GatewayError) as context:
            gateway.create("user-demo", "second", "draft")
        self.assertEqual(context.exception.status, 429)
        self.assertEqual(len(clients), 1)

    def test_job_is_scoped_to_owner(self) -> None:
        result = self.create_job()
        with self.assertRaises(service.GatewayError) as context:
            self.gateway.get("another-user", str(result["id"]))
        self.assertEqual(context.exception.status, 404)

    def test_path_like_user_identity_is_rejected(self) -> None:
        with self.assertRaises(service.GatewayError) as context:
            self.gateway.create("..", "title", "content")
        self.assertEqual(context.exception.status, 401)

    def test_token_failure_is_translated_to_gateway_error(self) -> None:
        original = service.broker.get_token
        service.broker.get_token = lambda: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(["az"], 120)
        )
        try:
            client = service.OfficeSessionClient(
                "https://example.invalid",
                "office-test",
            )
            with self.assertRaises(service.GatewayError) as context:
                client.generate("title", "content")
            self.assertEqual(context.exception.status, 502)
        finally:
            service.broker.get_token = original

    def test_convert_and_edit_forward_allowed_operations(self) -> None:
        result = self.create_job()
        public_id = str(result["id"])
        converted = self.gateway.convert(
            "user-demo",
            public_id,
            "report.pptx",
            "pdf",
        )
        self.assertEqual(converted["files"][0]["name"], "report.pptx.pdf")
        edited = self.gateway.edit(
            "user-demo",
            public_id,
            [{"op": "replaceText", "find": "Draft", "replace": "Approved"}],
        )
        self.assertEqual(len(edited["files"]), 3)

    def test_approval_stages_and_promotes_all_files(self) -> None:
        result = self.create_job()
        public_id = str(result["id"])
        approved = self.gateway.approve("user-demo", public_id)
        self.assertEqual(approved["status"], "approved")
        self.assertTrue(self.clients[0].stopped)
        self.assertEqual(len(approved["files"]), 4)
        self.assertNotIn("target", approved["files"][0])
        self.assertTrue(
            (self.root / "approved" / public_id / "report.pdf").is_file()
        )

    def test_approval_succeeds_when_backend_cleanup_fails(self) -> None:
        clients: list[StopFailingOfficeClient] = []

        def factory(identifier: str) -> StopFailingOfficeClient:
            client = StopFailingOfficeClient(identifier)
            clients.append(client)
            return client

        gateway = service.OfficeGatewayService(
            factory,
            staging_root=self.root / "cleanup-failure-staging",
            approved_root=self.root / "cleanup-failure-approved",
        )
        result = gateway.create("user-demo", "Quarterly report", "Draft")
        approved = gateway.approve("user-demo", str(result["id"]))
        self.assertEqual(approved["status"], "approved")
        self.assertTrue(clients[0].abandoned)
        payload, _ = gateway.download(
            "user-demo",
            str(result["id"]),
            "report.pdf",
        )
        self.assertTrue(payload.startswith(b"%PDF"))

    def test_approval_rejects_changed_payload(self) -> None:
        result = self.create_job()
        public_id = str(result["id"])
        self.clients[0].payloads["report.pdf"] = b"%PDF-1.4\ntampered"
        with self.assertRaises(service.GatewayError) as context:
            self.gateway.approve("user-demo", public_id)
        self.assertEqual(context.exception.status, 409)

    def test_draft_download_rejects_changed_payload(self) -> None:
        result = self.create_job()
        public_id = str(result["id"])
        self.clients[0].payloads["report.pdf"] = b"%PDF-1.4\ntampered"
        with self.assertRaises(service.GatewayError) as context:
            self.gateway.download("user-demo", public_id, "report.pdf")
        self.assertEqual(context.exception.status, 409)

    def test_approval_can_retry_after_validation_failure(self) -> None:
        result = self.create_job()
        public_id = str(result["id"])
        original = self.clients[0].payloads["report.pdf"]
        self.clients[0].payloads["report.pdf"] = b"%PDF-1.4\ntampered"
        with self.assertRaises(service.GatewayError):
            self.gateway.approve("user-demo", public_id)
        self.clients[0].payloads["report.pdf"] = original
        approved = self.gateway.approve("user-demo", public_id)
        self.assertEqual(approved["status"], "approved")

    def test_approved_job_is_immutable(self) -> None:
        result = self.create_job()
        public_id = str(result["id"])
        self.gateway.approve("user-demo", public_id)
        with self.assertRaises(service.GatewayError) as context:
            self.gateway.convert(
                "user-demo",
                public_id,
                "report.pptx",
                "pdf",
            )
        self.assertEqual(context.exception.status, 409)

    def test_approved_download_uses_promoted_copy(self) -> None:
        result = self.create_job()
        public_id = str(result["id"])
        self.gateway.approve("user-demo", public_id)
        expected = self.clients[0].payloads["report.pdf"]
        self.clients[0].payloads["report.pdf"] = b"%PDF-1.4\nchanged-backend"
        payload, _ = self.gateway.download(
            "user-demo",
            public_id,
            "report.pdf",
        )
        self.assertEqual(payload, expected)

    def test_invalid_artifact_returns_safe_approval_error(self) -> None:
        result = self.create_job()
        public_id = str(result["id"])
        invalid = b"not-a-docx"
        self.clients[0].payloads["report.docx"] = invalid
        metadata = self.gateway.jobs[public_id].files["report.docx"]
        metadata["size"] = len(invalid)
        metadata["sha256"] = hashlib.sha256(invalid).hexdigest()
        with self.assertRaises(service.GatewayError) as context:
            self.gateway.approve("user-demo", public_id)
        self.assertEqual(context.exception.status, 409)

    def test_delete_stops_session_and_removes_job(self) -> None:
        result = self.create_job()
        public_id = str(result["id"])
        self.gateway.delete("user-demo", public_id)
        self.assertTrue(self.clients[0].stopped)
        with self.assertRaises(service.GatewayError):
            self.gateway.get("user-demo", public_id)

    def test_expired_draft_job_stops_client_and_is_removed(self) -> None:
        result = self.create_job()
        public_id = str(result["id"])
        job = self.gateway.jobs[public_id]
        job.created_at = 1
        job.last_activity_at = 1
        self.gateway._cleanup_expired_jobs(
            now=service.OFFICE_JOB_TTL_SECONDS + 2
        )
        self.assertTrue(self.clients[0].stopped)
        self.assertNotIn(public_id, self.gateway.jobs)

    def test_build_service_reports_sandboxes_backend(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "EXECUTION_BACKEND": "sandboxes",
                "SUBSCRIPTION_ID": "sub",
                "OFFICE_USER_WORK_DIR": str(self.root / "sandbox-service"),
            },
            clear=False,
        ):
            gateway = server.build_service()
        self.assertEqual(gateway.backend, "sandboxes")

    def test_sandbox_client_keeps_payload_out_of_command(self) -> None:
        class FakeSandbox:
            def __init__(self) -> None:
                self.writes = []
                self.commands = []
                self.deleted_files = []

            def write_file(self, path, content) -> None:
                self.writes.append((path, content))

            def ensure_running(self) -> None:
                return

            def exec(self, command):
                self.commands.append(command)
                return SimpleNamespace(
                    exit_code=0,
                    stdout='{"jobId":"job","files":[]}',
                    stderr="",
                )

            def delete_file(self, path) -> None:
                self.deleted_files.append(path)

        client = service.SandboxOfficeClient.__new__(
            service.SandboxOfficeClient
        )
        client._closed = False
        client._sandbox = FakeSandbox()
        payload = {"title": "title; rm -rf /", "content": "draft"}
        response = client._invoke("generate", payload)
        self.assertEqual(response["jobId"], "job")
        self.assertNotIn("rm -rf", client._sandbox.commands[0])
        self.assertIn("rm -rf", client._sandbox.writes[0][1])

    def test_sandbox_client_rejects_unsafe_download_path(self) -> None:
        client = service.SandboxOfficeClient.__new__(
            service.SandboxOfficeClient
        )
        client._sandbox = object()
        with self.assertRaises(service.GatewayError) as context:
            client.download("/files/job/../../secret")
        self.assertEqual(context.exception.status, 400)

    def test_sandbox_client_rejects_oversized_file_before_read(self) -> None:
        sandbox = mock.Mock()
        sandbox.exec.return_value = SimpleNamespace(exit_code=0, stderr="")
        sandbox.stat_file.return_value = SimpleNamespace(
            size=service.staging.MAX_ARTIFACT_BYTES + 1
        )
        client = service.SandboxOfficeClient.__new__(
            service.SandboxOfficeClient
        )
        client._sandbox = sandbox
        with self.assertRaises(service.GatewayError) as context:
            client.download("/files/job/report.pdf")
        self.assertEqual(context.exception.status, 413)
        sandbox.read_file.assert_not_called()

    def test_sandbox_client_rejects_unknown_file_size(self) -> None:
        sandbox = mock.Mock()
        sandbox.exec.return_value = SimpleNamespace(exit_code=0, stderr="")
        sandbox.stat_file.return_value = SimpleNamespace(size=None)
        client = service.SandboxOfficeClient.__new__(
            service.SandboxOfficeClient
        )
        client._sandbox = sandbox
        with self.assertRaises(service.GatewayError) as context:
            client.download("/files/job/report.pdf")
        self.assertEqual(context.exception.status, 413)
        sandbox.read_file.assert_not_called()

    def test_sandbox_stop_can_retry_after_transient_failure(self) -> None:
        class FakeSandbox:
            def __init__(self) -> None:
                self.delete_calls = 0
                self.close_calls = 0

            def delete(self) -> None:
                self.delete_calls += 1
                if self.delete_calls == 1:
                    raise RuntimeError("transient")

            def close(self) -> None:
                self.close_calls += 1

        client = service.SandboxOfficeClient.__new__(
            service.SandboxOfficeClient
        )
        client._closed = False
        client._sandbox = FakeSandbox()
        client._client = None
        client._credential = None
        client._owns_client = False
        client._owns_credential = False
        with self.assertRaises(service.GatewayError):
            client.stop()
        self.assertFalse(client._closed)
        client.stop()
        self.assertTrue(client._closed)
        self.assertEqual(client._sandbox.delete_calls, 2)
        self.assertEqual(client._sandbox.close_calls, 1)

    def test_sandbox_init_failure_deletes_allocated_sandbox(self) -> None:
        class FakeSandbox:
            def __init__(self) -> None:
                self.deleted = False
                self.closed = False

            def write_file(self, path, content) -> None:
                raise RuntimeError("upload failed")

            def delete(self) -> None:
                self.deleted = True

            def close(self) -> None:
                self.closed = True

        sandbox = FakeSandbox()
        with (
            mock.patch.object(
                service.SandboxOfficeClient,
                "_latest_office_disk_image",
                return_value="disk",
            ),
            mock.patch.object(
                service.SandboxOfficeClient,
                "_create_sandbox",
                return_value=sandbox,
            ),
            mock.patch.object(
                Path,
                "read_text",
                return_value="runner",
            ),
        ):
            with self.assertRaises(service.GatewayError):
                service.SandboxOfficeClient(
                    "office-test",
                    subscription_id="sub",
                    resource_group="rg",
                    location="region",
                    sandbox_group="group",
                    group_client=object(),
                    credential=object(),
                )
        self.assertTrue(sandbox.deleted)
        self.assertTrue(sandbox.closed)


if __name__ == "__main__":
    unittest.main()
