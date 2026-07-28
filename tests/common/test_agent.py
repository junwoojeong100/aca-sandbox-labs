"""Backend-neutral agent orchestration offline tests.

Azure 없이 정책, 산출물 검사, 승인 게이트, 오류 재시도 루프를 검증한다.
실행: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import (
    config,
    execution,
    ids,
    llm,
    orchestrator,
    policy,
    staging,
)


class PolicyTests(unittest.TestCase):
    def _input(self, text: str, **kwargs) -> policy.PolicyInput:
        return policy.PolicyInput(
            tenant_id="t1", user_id="u1", request_text=text, **kwargs
        )

    def _classify(self, text: str, **kwargs) -> policy.PolicyDecision:
        return policy.classify_base(
            self._input(text, **kwargs),
            rules_version="test",
        )

    def test_general_python_request_is_class_a(self) -> None:
        decision = self._classify("매출 데이터를 월별로 집계해줘")
        self.assertEqual(decision.classification, "A")
        self.assertEqual(decision.route, policy.Route.PYTHON)
        self.assertTrue(decision.allowed)

    def test_office_request_is_class_b(self) -> None:
        decision = self._classify("결과를 pptx 보고서로 만들어줘")
        self.assertEqual(decision.classification, "B")
        self.assertEqual(decision.route, policy.Route.OFFICE)

    def test_office_attachment_routes_to_office_pool(self) -> None:
        decision = self._classify(
            "이 파일 정리해줘",
            attachment_names=("plan.docx",),
        )
        self.assertEqual(decision.route, policy.Route.OFFICE)
        self.assertFalse(decision.allowed)
        self.assertFalse(decision.controls["inputUploadImplemented"])

    def test_office_request_with_csv_attachment_is_rejected(self) -> None:
        decision = self._classify(
            "이 CSV로 pptx를 만들어줘",
            attachment_names=("data.csv",),
            attachment_sizes=(1024,),
        )
        self.assertEqual(decision.route, policy.Route.OFFICE)
        self.assertFalse(decision.allowed)

    def test_office_attachment_is_rejected_before_code_interpreter_limits(self) -> None:
        decision = self._classify(
            "첨부한 PPTX를 편집해줘",
            attachment_names=("deck.pptx",),
            attachment_sizes=(200 * 1024 * 1024,),
            estimated_seconds=600,
        )
        self.assertEqual(decision.classification, "B")
        self.assertEqual(decision.route, policy.Route.OFFICE)
        self.assertFalse(decision.allowed)
        self.assertNotIn("maxExecutionSeconds", decision.controls)

    def test_internet_request_is_class_d(self) -> None:
        decision = self._classify("https://example.com 에서 받아와줘")
        self.assertEqual(decision.classification, "D")
        self.assertEqual(decision.route, policy.Route.CONTROLLED_EGRESS)
        self.assertFalse(decision.allowed)

    def test_admin_request_is_class_e(self) -> None:
        decision = self._classify("production database 를 수정해줘")
        self.assertEqual(decision.classification, "E")
        self.assertEqual(decision.route, policy.Route.DENY)

    def test_admin_rule_wins_over_office_rule(self) -> None:
        decision = self._classify(
            "production database 백업을 xlsx로 만들어줘"
        )
        self.assertEqual(decision.classification, "E")

    def test_code_inspection_flags_shell_and_network(self) -> None:
        violations = policy.inspect_code(
            "import subprocess\nimport requests\nsubprocess.run(['ls'])"
        )
        self.assertIn("subprocess 호출", violations)
        self.assertIn("외부 HTTP client", violations)

    def test_code_inspection_allows_analysis_code(self) -> None:
        self.assertEqual(policy.inspect_code("import csv\nprint(1)"), [])


class StagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = staging.ArtifactStaging(self.root / "staging", "t1", "req-1")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_stage_valid_json(self) -> None:
        artifact = self.store.stage("summary.json", b'{"a": 1}')
        self.assertEqual(artifact.size, 8)
        self.assertEqual(artifact.checks["json"], "ok")
        self.assertTrue(artifact.path.is_file())

    def test_reject_extension_content_mismatch(self) -> None:
        with self.assertRaises(staging.StagingError):
            self.store.stage("report.pdf", b"not a pdf at all")

    def test_reject_disallowed_extension(self) -> None:
        with self.assertRaises(staging.StagingError):
            self.store.stage("payload.exe", b"MZ\x00\x00")

    def test_reject_path_traversal_name(self) -> None:
        with self.assertRaises(staging.StagingError):
            self.store.stage("../escape.json", b"{}")

    def test_reject_macro_enabled_ooxml(self) -> None:
        payload = b"PK\x03\x04" + b"x" * 20 + b"vbaProject.bin" + b"y" * 20
        with self.assertRaises(staging.StagingError):
            self.store.stage("report.docx", payload)

    def test_reject_empty_file(self) -> None:
        with self.assertRaises(staging.StagingError):
            self.store.stage("summary.json", b"")

    def test_reject_reserved_manifest_name(self) -> None:
        with self.assertRaises(staging.StagingError):
            self.store.stage("manifest.json", b"{}")

    def test_promotion_requires_approval(self) -> None:
        artifact = self.store.stage("summary.json", b'{"a": 1}')
        approval = staging.ApprovalService(self.root / "approved")
        outcome = approval.promote(artifact, approved=False, approver="nobody")
        self.assertFalse(outcome["promoted"])
        self.assertFalse((self.root / "approved" / "summary.json").exists())

    def test_promotion_copies_after_approval(self) -> None:
        artifact = self.store.stage("summary.json", b'{"a": 1}')
        approval = staging.ApprovalService(self.root / "approved")
        outcome = approval.promote(artifact, approved=True, approver="junwoo")
        self.assertTrue(outcome["promoted"])
        self.assertTrue((self.root / "approved" / "summary.json").is_file())

    def test_promotion_detects_tampering(self) -> None:
        artifact = self.store.stage("summary.json", b'{"a": 1}')
        artifact.path.chmod(0o640)
        artifact.path.write_bytes(b'{"a": 2}')
        approval = staging.ApprovalService(self.root / "approved")
        with self.assertRaises(staging.StagingError):
            approval.promote(artifact, approved=True, approver="junwoo")

    def test_batch_promotion_is_all_or_nothing(self) -> None:
        first = self.store.stage("first.json", b'{"a": 1}')
        second = self.store.stage("second.json", b'{"b": 2}')
        second.path.chmod(0o640)
        second.path.write_bytes(b'{"tampered": true}')
        destination = self.root / "batch-approved"
        with self.assertRaises(staging.StagingError):
            staging.promote_batch(
                [first, second],
                destination,
                approver="junwoo",
            )
        self.assertFalse(destination.exists())

    def test_batch_promotion_is_idempotent_for_same_hashes(self) -> None:
        first = self.store.stage("first.json", b'{"a": 1}')
        destination = self.root / "batch-approved"
        initial = staging.promote_batch(
            [first],
            destination,
            approver="junwoo",
        )
        repeated = staging.promote_batch(
            [first],
            destination,
            approver="junwoo",
        )
        self.assertEqual(initial[0]["sha256"], repeated[0]["sha256"])

    def test_manifest_lists_artifacts(self) -> None:
        self.store.stage("summary.json", b'{"a": 1}')
        manifest = self.store.write_manifest()
        self.assertIn("summary.json", manifest.read_text(encoding="utf-8"))


class LLMParsingTests(unittest.TestCase):
    def test_extract_plain_json(self) -> None:
        self.assertEqual(llm._extract_json('{"plan": "p", "code": "x"}')["code"], "x")

    def test_extract_fenced_json(self) -> None:
        text = '```json\n{"plan": "p", "code": "x"}\n```'
        self.assertEqual(llm._extract_json(text)["plan"], "p")

    def test_extract_json_with_prose(self) -> None:
        text = 'Sure!\n{"plan": "p", "code": "x"}\nHope this helps.'
        self.assertEqual(llm._extract_json(text)["code"], "x")

    def test_invalid_json_raises(self) -> None:
        with self.assertRaises(llm.LLMError):
            llm._extract_json("no json here")

    def test_stub_client_produces_runnable_shape(self) -> None:
        plan = llm.StubClient().plan("집계해줘", attachments=("sales.csv",))
        self.assertIn("sales.csv", plan.code)
        self.assertNotIn("subprocess", plan.code)
        self.assertEqual(policy.inspect_code(plan.code), [])

    def test_system_prompt_uses_backend_execution_limit(self) -> None:
        settings = config.Settings(
            llm_provider="azure-openai",
            azure_openai_endpoint="https://example.invalid",
            azure_openai_deployment="model",
            execution_timeout_seconds=900,
        )
        client = llm.AzureOpenAIClient(settings)
        with mock.patch.object(
            client,
            "_call",
            return_value='{"plan":"p","code":"print(1)"}',
        ):
            client.plan("분석해줘")
        self.assertIn("900초 미만", client._history[0]["content"])


class ExecutionResultTests(unittest.TestCase):
    """실행 성공 판정 회귀 테스트.

    실제 gpt-5.6-terra 실행에서 matplotlib/pandas 경고가 stderr로 나오면
    성공한 코드가 실패로 처리되던 버그가 있었다.
    """

    def test_warnings_on_stderr_do_not_fail_the_run(self) -> None:
        result = execution.ExecutionResult(
            "Succeeded",
            "done\n",
            "UserWarning: Could not infer format, falling back to dateutil.",
        )
        self.assertTrue(result.succeeded)
        self.assertIn("UserWarning", result.warnings)

    def test_failed_status_is_a_failure_even_without_stderr(self) -> None:
        result = execution.ExecutionResult("Failed", "", "")
        self.assertFalse(result.succeeded)
        self.assertEqual(result.warnings, "")

    def test_clean_success_has_no_warnings(self) -> None:
        result = execution.ExecutionResult("Succeeded", "ok\n", "")
        self.assertTrue(result.succeeded)
        self.assertEqual(result.warnings, "")

class FakeSession:
    """Backend-neutral Python execution test double."""

    def __init__(
        self,
        failures: int = 0,
        output_files: tuple[str, ...] = ("summary.json",),
    ) -> None:
        self.identifier = "py-fake"
        self.failures = failures
        self.output_files = output_files
        self.executions = 0
        self.uploaded: dict[str, bytes] = {}
        self.deleted = False

    def upload(self, name: str, content: bytes) -> dict[str, object]:
        self.uploaded[name] = content
        return {"name": name}

    def execute(
        self,
        code: str,
        *,
        timeout: int = 300,
    ) -> execution.ExecutionResult:
        if code.startswith("import json\nimport os"):
            return execution.ExecutionResult(
                "Succeeded",
                '{"files": []}',
                "",
            )
        self.executions += 1
        if self.executions <= self.failures:
            return execution.ExecutionResult(
                "Failed",
                "",
                "KeyError: 'sales_amount'",
            )
        return execution.ExecutionResult("Succeeded", "done\n", "")

    def list_files(self) -> dict[str, object]:
        return {
            "value": [
                *[
                    {"name": name, "size": 1024}
                    for name in self.output_files
                ],
                {"name": "sales.csv", "size": 1024},
            ]
        }

    def download(self, name: str) -> bytes:
        return b'{"monthly_sales": {"2026-01": 200.0}}'

    def delete(self) -> int:
        self.deleted = True
        return 204


class ShellHappyLLM:
    """항상 정책 위반 코드를 만드는 client."""

    def plan(
        self,
        request_text,
        *,
        attachments=(),
        expected_outputs=(),
        failure=None,
    ) -> llm.Plan:
        return llm.Plan("shell 실행", "import subprocess\nsubprocess.run(['ls'])", "test")


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = config.Settings(
            staging_dir=root / "staging",
            approved_dir=root / "approved",
        )
        self.settings.llm_provider = "stub"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _classify(request: policy.PolicyInput) -> policy.PolicyDecision:
        return policy.classify_base(
            request,
            rules_version="orchestrator-test",
        )

    def _runner(self, session_factory, llm_client):
        return orchestrator.Orchestrator(
            session_factory,
            "fake-backend",
            self._classify,
            settings=self.settings,
            llm_client=llm_client,
        )

    def _run(self, session: FakeSession, llm_client=None, **kwargs):
        runner = self._runner(
            lambda: session,
            llm_client or llm.StubClient(),
        )
        return runner.run("매출 CSV를 집계해줘", **kwargs)

    def test_successful_run_stages_but_does_not_promote(self) -> None:
        session = FakeSession()
        result = self._run(
            session,
            attachments={"sales.csv": b"month,product,amount\n"},
            expected_outputs=("summary.json",),
        )
        self.assertTrue(result.succeeded)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(result.artifacts), 1)
        self.assertFalse(result.promotions[0]["promoted"])
        self.assertTrue(session.deleted)

    def test_approved_run_promotes_batch_to_request_directory(self) -> None:
        session = FakeSession()
        result = self._run(
            session,
            expected_outputs=("summary.json",),
            approve=True,
            approver="user-demo",
        )
        self.assertTrue(result.promotions[0]["promoted"])
        self.assertTrue(
            (
                self.settings.approved_dir
                / result.request_id
                / "summary.json"
            ).is_file()
        )
        self.assertNotIn("target", result.user_view()["promotions"][0])

    def test_retry_loop_recovers_from_execution_error(self) -> None:
        session = FakeSession(failures=1)
        result = self._run(session, expected_outputs=("summary.json",))
        self.assertTrue(result.succeeded)
        self.assertEqual(result.attempts, 2)

    def test_retry_limit_is_enforced(self) -> None:
        session = FakeSession(failures=99)
        result = self._run(session, expected_outputs=("summary.json",))
        self.assertFalse(result.succeeded)
        self.assertEqual(result.attempts, self.settings.max_code_retries + 1)
        self.assertTrue(session.deleted)

    def test_missing_expected_output_is_retried_then_fails(self) -> None:
        session = FakeSession(output_files=("monthly_sales_summary.json",))
        result = self._run(session, expected_outputs=("summary.json",))
        self.assertFalse(result.succeeded)
        self.assertEqual(result.attempts, self.settings.max_code_retries + 1)
        self.assertIn("필수 결과 파일", result.stderr)
        self.assertTrue(session.deleted)

    def test_policy_violating_code_is_never_executed(self) -> None:
        session = FakeSession()
        result = self._run(session, llm_client=ShellHappyLLM())
        self.assertFalse(result.succeeded)
        self.assertEqual(session.executions, 0)
        self.assertIn("정책 위반", result.stderr)

    def test_denied_request_never_allocates_a_session(self) -> None:
        factory = mock.Mock()
        runner = self._runner(
            factory,
            llm.StubClient(),
        )
        result = runner.run("production database 를 지워줘")
        self.assertFalse(result.succeeded)
        self.assertEqual(result.decision["classification"], "E")
        self.assertEqual(result.execution_identifier, "")
        factory.assert_not_called()

    def test_user_view_hides_execution_identifier(self) -> None:
        session = FakeSession()
        result = self._run(session, expected_outputs=("summary.json",))
        self.assertNotIn("executionIdentifier", result.user_view())
        self.assertEqual(
            result.audit_view()["executionIdentifier"],
            session.identifier,
        )

    def test_user_view_hides_request_id_and_internal_paths(self) -> None:
        session = FakeSession()
        result = self._run(session, expected_outputs=("summary.json",))
        result.plan = "Read /mnt/data/sales.csv"
        result.stdout = (
            f"saved /tmp/result.json in {result.execution_identifier}"
        )
        view = result.user_view()
        self.assertNotIn("requestId", view)
        self.assertNotIn("/mnt/data", view["plan"])
        self.assertNotIn("/tmp", view["stdout"])
        self.assertNotIn(result.execution_identifier, view["stdout"])

    def test_input_attachment_is_not_staged_by_default(self) -> None:
        session = FakeSession()
        result = self._run(session, attachments={"sales.csv": b"month\n"})
        names = {artifact["name"] for artifact in result.artifacts}
        self.assertEqual(names, {"summary.json"})

    def test_oversized_artifact_is_rejected_before_download(self) -> None:
        class OversizedSession(FakeSession):
            def list_files(self) -> dict[str, object]:
                return {
                    "value": [
                        {
                            "name": "summary.json",
                            "size": staging.MAX_ARTIFACT_BYTES + 1,
                        }
                    ]
                }

            def download(self, name: str) -> bytes:
                raise AssertionError("oversized artifact must not download")

        result = self._run(
            OversizedSession(),
            expected_outputs=("summary.json",),
        )
        self.assertFalse(result.succeeded)
        self.assertIn("허용 범위", result.stderr)

    def test_partial_artifact_set_is_never_promoted(self) -> None:
        class MixedSession(FakeSession):
            def list_files(self) -> dict[str, object]:
                return {
                    "value": [
                        {"name": "small.json", "size": 100},
                        {
                            "name": "large.bin",
                            "size": staging.MAX_ARTIFACT_BYTES + 1,
                        },
                    ]
                }

            def download(self, name: str) -> bytes:
                if name == "large.bin":
                    raise AssertionError("large artifact must not download")
                return b'{"ok":true}'

        result = self._run(
            MixedSession(),
            expected_outputs=("small.json", "large.bin"),
            approve=True,
            approver="user-demo",
        )
        self.assertFalse(result.succeeded)
        self.assertEqual(len(result.artifacts), 1)
        self.assertFalse(result.promotions[0]["promoted"])

    def test_audit_records_execution_lifecycle(self) -> None:
        session = FakeSession()
        result = self._run(session, expected_outputs=("summary.json",))
        steps = [entry["step"] for entry in result.audit]
        self.assertIn("execution-allocated", steps)
        self.assertIn("execution-bootstrap", steps)
        self.assertIn("execution-deleted", steps)
        self.assertIn("policy", steps)

    def test_cleanup_error_does_not_mask_successful_result(self) -> None:
        class DeleteFailingSession(FakeSession):
            def delete(self) -> int:
                raise subprocess.CalledProcessError(1, ["az"])

        result = self._run(
            DeleteFailingSession(),
            expected_outputs=("summary.json",),
        )
        self.assertTrue(result.succeeded)
        steps = [entry["step"] for entry in result.audit]
        self.assertIn("execution-delete-failed", steps)


class AgentIdentifierTests(unittest.TestCase):
    def test_identifier_is_unpredictable_and_prefixed(self) -> None:
        first = ids.new_identifier("py")
        second = ids.new_identifier("py")
        self.assertTrue(first.startswith("py-"))
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first.split("-", 1)[1]), 32)

    def test_identifier_does_not_embed_user_input(self) -> None:
        identifier = ids.new_identifier("py")
        self.assertNotIn("user", identifier)


if __name__ == "__main__":
    unittest.main()
