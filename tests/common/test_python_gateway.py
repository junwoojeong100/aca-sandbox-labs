from __future__ import annotations

import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest import mock

from agent import orchestrator, staging
from python_gateway import service


class FakeRunner:
    def __init__(self, staging_root: Path, *, succeeded: bool = True) -> None:
        self.staging_root = staging_root
        self.succeeded = succeeded

    def run(
        self,
        request_text: str,
        *,
        tenant_id: str,
        user_id: str,
        attachments,
        expected_outputs,
        approve: bool,
    ) -> orchestrator.RunResult:
        result = orchestrator.RunResult(
            request_id="internal-request",
            decision={
                "classification": "A",
                "route": "python-pool",
                "allowed": True,
                "reason": "test",
            },
            succeeded=self.succeeded,
            attempts=1,
            plan="CSV를 집계한다.",
            stdout="done\n" if self.succeeded else "",
            stderr="" if self.succeeded else "failed",
            execution_identifier="py-secret-execution",
        )
        if not self.succeeded:
            return result
        store = staging.ArtifactStaging(
            self.staging_root,
            tenant_id,
            result.request_id,
        )
        artifact = store.stage(
            "summary.json",
            b'{"monthly_sales":{"2026-01":200}}',
        )
        result.artifacts = [artifact.as_dict()]
        result.promotions = [
            {
                "name": artifact.name,
                "promoted": False,
                "reason": "승인되지 않음",
            }
        ]
        return result


class PythonGatewayServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.staging_root = self.root / "staging"
        self.approved_root = self.root / "approved"
        self.gateway = service.PythonGatewayService(
            lambda: FakeRunner(self.staging_root),
            staging_root=self.staging_root,
            approved_root=self.approved_root,
            backend="fake",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_job(self) -> dict[str, object]:
        return self.gateway.create(
            "user-demo",
            "매출 CSV를 집계해줘",
            {"sales.csv": b"month,amount\n2026-01,200\n"},
            ("summary.json",),
        )

    def test_create_hides_backend_identifiers(self) -> None:
        result = self.create_job()
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("requestId", result)
        self.assertNotIn("executionIdentifier", result)
        self.assertEqual(result["artifacts"][0]["name"], "summary.json")

    def test_public_text_sanitizes_internal_paths_and_execution_ids(self) -> None:
        result = self.create_job()
        public_id = str(result["id"])
        job = self.gateway.jobs[public_id]
        job.result.plan = "Read /mnt/data/sales.csv"
        job.result.stdout = (
            f"saved /tmp/result.json in {job.result.execution_identifier}"
        )
        view = self.gateway.get("user-demo", public_id)
        self.assertNotIn("/mnt/data", view["plan"])
        self.assertNotIn("/tmp", view["stdout"])
        self.assertNotIn(job.result.execution_identifier, view["stdout"])

    def test_job_is_scoped_to_owner(self) -> None:
        result = self.create_job()
        with self.assertRaises(service.GatewayError) as context:
            self.gateway.get("other-user", str(result["id"]))
        self.assertEqual(context.exception.status, 404)

    def test_path_like_user_identity_is_rejected(self) -> None:
        with self.assertRaises(service.GatewayError) as context:
            self.gateway.create("..", "분석해줘", {}, ())
        self.assertEqual(context.exception.status, 401)

    def test_reserved_manifest_output_is_rejected(self) -> None:
        with self.assertRaises(service.GatewayError) as context:
            self.gateway.create(
                "user-demo",
                "분석해줘",
                {},
                ("manifest.json",),
            )
        self.assertEqual(context.exception.status, 400)

    def test_attachment_limit_is_checked_per_file(self) -> None:
        original = service.MAX_ATTACHMENT_BYTES
        service.MAX_ATTACHMENT_BYTES = 3
        try:
            with self.assertRaises(service.GatewayError) as context:
                service.PythonGatewayService._validate_inputs(
                    "analyze",
                    {"large.csv": b"1234"},
                    (),
                )
        finally:
            service.MAX_ATTACHMENT_BYTES = original
        self.assertEqual(context.exception.status, 413)

    def test_active_job_limit_rejects_before_runner_allocation(self) -> None:
        factory = mock.Mock()
        gateway = service.PythonGatewayService(
            factory,
            staging_root=self.root / "limited-staging",
            approved_root=self.root / "limited-approved",
            backend="fake",
            max_active_jobs=1,
        )
        gateway.allocating = 1
        with self.assertRaises(service.GatewayError) as context:
            gateway.create("user-demo", "분석해줘", {}, ())
        self.assertEqual(context.exception.status, 429)
        factory.assert_not_called()

    def test_total_attachment_limit_is_an_explicit_gateway_limit(self) -> None:
        original = service.MAX_TOTAL_ATTACHMENT_BYTES
        service.MAX_TOTAL_ATTACHMENT_BYTES = 5
        try:
            with self.assertRaises(service.GatewayError) as context:
                service.PythonGatewayService._validate_inputs(
                    "compare",
                    {"a.csv": b"123", "b.csv": b"456"},
                    (),
                )
        finally:
            service.MAX_TOTAL_ATTACHMENT_BYTES = original
        self.assertEqual(context.exception.status, 413)

    def test_download_and_approve_same_staged_artifact(self) -> None:
        result = self.create_job()
        public_id = str(result["id"])
        payload, _ = self.gateway.download(
            "user-demo",
            public_id,
            "summary.json",
        )
        self.assertIn(b"monthly_sales", payload)
        approved = self.gateway.approve("user-demo", public_id)
        self.assertEqual(approved["status"], "approved")
        self.assertTrue(approved["promotions"][0]["promoted"])
        self.assertTrue(
            (self.approved_root / public_id / "summary.json").is_file()
        )

    def test_approved_download_uses_promoted_copy(self) -> None:
        result = self.create_job()
        public_id = str(result["id"])
        self.gateway.approve("user-demo", public_id)
        job = self.gateway.jobs[public_id]
        staged_path = Path(str(job.result.artifacts[0]["path"]))
        staged_path.chmod(0o640)
        staged_path.write_text('{"changed":true}', encoding="utf-8")
        payload, _ = self.gateway.download(
            "user-demo",
            public_id,
            "summary.json",
        )
        self.assertIn(b"monthly_sales", payload)

    def test_tampered_artifact_is_not_downloaded_or_approved(self) -> None:
        result = self.create_job()
        public_id = str(result["id"])
        job = self.gateway.jobs[public_id]
        artifact_path = Path(str(job.result.artifacts[0]["path"]))
        artifact_path.chmod(0o640)
        artifact_path.write_text(
            '{"tampered":true}',
            encoding="utf-8",
        )
        with self.assertRaises(service.GatewayError) as context:
            self.gateway.download("user-demo", public_id, "summary.json")
        self.assertEqual(context.exception.status, 409)
        with self.assertRaises(service.GatewayError):
            self.gateway.approve("user-demo", public_id)

    def test_failed_job_cannot_be_approved(self) -> None:
        gateway = service.PythonGatewayService(
            lambda: FakeRunner(self.staging_root, succeeded=False),
            staging_root=self.staging_root,
            approved_root=self.approved_root,
            backend="fake",
        )
        result = gateway.create(
            "user-demo",
            "실패 요청",
            {},
            ("summary.json",),
        )
        with self.assertRaises(service.GatewayError) as context:
            gateway.approve("user-demo", str(result["id"]))
        self.assertEqual(context.exception.status, 409)

    def test_backend_exception_is_not_exposed(self) -> None:
        class FailingRunner:
            def run(self, *args, **kwargs):
                raise subprocess.CalledProcessError(
                    1,
                    ["/Users/internal/secret/tool"],
                )

        gateway = service.PythonGatewayService(
            lambda: FailingRunner(),
            staging_root=self.staging_root,
            approved_root=self.approved_root,
            backend="fake",
        )
        with self.assertRaises(service.GatewayError) as context:
            gateway.create("user-demo", "분석해줘", {}, ())
        self.assertNotIn("/Users/internal", context.exception.message)

    def test_delete_removes_job_and_staging_files(self) -> None:
        result = self.create_job()
        public_id = str(result["id"])
        artifact_path = Path(
            str(self.gateway.jobs[public_id].result.artifacts[0]["path"])
        )
        self.gateway.delete("user-demo", public_id)
        self.assertFalse(artifact_path.exists())
        with self.assertRaises(service.GatewayError):
            self.gateway.get("user-demo", public_id)

if __name__ == "__main__":
    unittest.main()
