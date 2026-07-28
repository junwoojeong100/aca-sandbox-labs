"""Agent orchestration 계층의 offline 테스트.

Azure 없이 정책, 산출물 검사, 승인 게이트, 오류 재시도 루프를 검증한다.
실행: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import broker, config, llm, orchestrator, policy, staging  # noqa: E402


class PolicyTests(unittest.TestCase):
    def _input(self, text: str, **kwargs) -> policy.PolicyInput:
        return policy.PolicyInput(
            tenant_id="t1", user_id="u1", request_text=text, **kwargs
        )

    def test_general_python_request_is_class_a(self) -> None:
        decision = policy.classify(self._input("매출 데이터를 월별로 집계해줘"))
        self.assertEqual(decision.classification, "A")
        self.assertEqual(decision.route, policy.Route.PYTHON_POOL)
        self.assertTrue(decision.allowed)

    def test_office_request_is_class_b(self) -> None:
        decision = policy.classify(self._input("결과를 pptx 보고서로 만들어줘"))
        self.assertEqual(decision.classification, "B")
        self.assertEqual(decision.route, policy.Route.OFFICE_POOL)
        self.assertIn("edit", decision.controls["allowedOperations"])

    def test_office_attachment_routes_to_office_pool(self) -> None:
        decision = policy.classify(
            self._input("이 파일 정리해줘", attachment_names=("plan.docx",))
        )
        self.assertEqual(decision.route, policy.Route.OFFICE_POOL)
        self.assertFalse(decision.allowed)
        self.assertFalse(decision.controls["inputUploadImplemented"])

    def test_office_request_with_csv_attachment_is_rejected(self) -> None:
        decision = policy.classify(
            self._input(
                "이 CSV로 pptx를 만들어줘",
                attachment_names=("data.csv",),
                attachment_sizes=(1024,),
            )
        )
        self.assertEqual(decision.route, policy.Route.OFFICE_POOL)
        self.assertFalse(decision.allowed)

    def test_long_running_request_is_class_c(self) -> None:
        decision = policy.classify(self._input("대량 배치", estimated_seconds=600))
        self.assertEqual(decision.classification, "C")
        self.assertEqual(decision.route, policy.Route.ASYNC_COMPUTE)
        self.assertFalse(decision.allowed)

    def test_oversized_attachment_is_class_c(self) -> None:
        decision = policy.classify(
            self._input("분석해줘", attachment_sizes=(200 * 1024 * 1024,))
        )
        self.assertEqual(decision.classification, "C")

    def test_office_attachment_is_rejected_before_code_interpreter_limits(self) -> None:
        decision = policy.classify(
            self._input(
                "첨부한 PPTX를 편집해줘",
                attachment_names=("deck.pptx",),
                attachment_sizes=(200 * 1024 * 1024,),
                estimated_seconds=600,
            )
        )
        self.assertEqual(decision.classification, "B")
        self.assertEqual(decision.route, policy.Route.OFFICE_POOL)
        self.assertFalse(decision.allowed)
        self.assertNotIn("maxExecutionSeconds", decision.controls)

    def test_reference_gateway_also_has_aggregate_attachment_limit(self) -> None:
        decision = policy.classify(
            self._input(
                "두 CSV를 비교해줘",
                attachment_names=("a.csv", "b.csv"),
                attachment_sizes=(70 * 1024 * 1024, 70 * 1024 * 1024),
            )
        )
        self.assertEqual(decision.classification, "C")
        self.assertFalse(decision.allowed)
        self.assertIn("요청 body 한도", decision.reason)

    def test_internet_request_is_class_d(self) -> None:
        decision = policy.classify(self._input("https://example.com 에서 받아와줘"))
        self.assertEqual(decision.classification, "D")
        self.assertEqual(decision.route, policy.Route.CONTROLLED_EGRESS)
        self.assertFalse(decision.allowed)

    def test_admin_request_is_class_e(self) -> None:
        decision = policy.classify(self._input("production database 를 수정해줘"))
        self.assertEqual(decision.classification, "E")
        self.assertEqual(decision.route, policy.Route.DENY)

    def test_admin_rule_wins_over_office_rule(self) -> None:
        decision = policy.classify(
            self._input("production database 백업을 xlsx로 만들어줘")
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


class ExecutionResultTests(unittest.TestCase):
    """실행 성공 판정 회귀 테스트.

    실제 gpt-5.6-terra 실행에서 matplotlib/pandas 경고가 stderr로 나오면
    성공한 코드가 실패로 처리되던 버그가 있었다.
    """

    def test_warnings_on_stderr_do_not_fail_the_run(self) -> None:
        result = broker.ExecutionResult(
            "Succeeded",
            "done\n",
            "UserWarning: Could not infer format, falling back to dateutil.",
        )
        self.assertTrue(result.succeeded)
        self.assertIn("UserWarning", result.warnings)

    def test_failed_status_is_a_failure_even_without_stderr(self) -> None:
        result = broker.ExecutionResult("Failed", "", "")
        self.assertFalse(result.succeeded)
        self.assertEqual(result.warnings, "")

    def test_clean_success_has_no_warnings(self) -> None:
        result = broker.ExecutionResult("Succeeded", "ok\n", "")
        self.assertTrue(result.succeeded)
        self.assertEqual(result.warnings, "")

    def test_execute_retries_with_properties_wrapper_for_api_drift(self) -> None:
        requests = []
        original_request = broker._request
        original_get_token = broker.get_token

        def fake_request(method, url, **kwargs):
            requests.append(kwargs["body"])
            if len(requests) == 1:
                return (
                    400,
                    b'{"error":{"code":"SessionPropertiesMissing"}}',
                    "application/json",
                )
            return (
                200,
                b'{"status":"Succeeded","result":{"stdout":"ok","stderr":""}}',
                "application/json",
            )

        broker._request = fake_request
        broker.get_token = lambda *_args, **_kwargs: "token"
        try:
            result = broker.PythonSession("https://example.invalid").execute(
                "print('ok')"
            )
        finally:
            broker._request = original_request
            broker.get_token = original_get_token

        self.assertTrue(result.succeeded)
        self.assertNotIn("properties", json.loads(requests[0]))
        self.assertIn("properties", json.loads(requests[1]))


class FakeSession:
    """Dynamic Sessions API를 흉내내는 test double."""

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

    def execute(self, code: str, timeout: int = 300) -> broker.ExecutionResult:
        if code.startswith("import json\nimport os"):
            return broker.ExecutionResult("Succeeded", '{"files": []}', "")
        self.executions += 1
        if self.executions <= self.failures:
            return broker.ExecutionResult("Failed", "", "KeyError: 'sales_amount'")
        return broker.ExecutionResult("Succeeded", "done\n", "")

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
            python_endpoint="https://example.invalid",
            staging_dir=root / "staging",
            approved_dir=root / "approved",
        )
        self.settings.llm_provider = "stub"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, session: FakeSession, llm_client=None, **kwargs):
        original = broker.PythonSession
        broker.PythonSession = lambda endpoint, identifier=None: session  # type: ignore[assignment]
        try:
            runner = orchestrator.Orchestrator(
                self.settings, llm_client or llm.StubClient()
            )
            return runner.run("매출 CSV를 집계해줘", **kwargs)
        finally:
            broker.PythonSession = original  # type: ignore[assignment]

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
        runner = orchestrator.Orchestrator(self.settings, llm.StubClient())
        result = runner.run("production database 를 지워줘")
        self.assertFalse(result.succeeded)
        self.assertEqual(result.decision["classification"], "E")
        self.assertEqual(result.session_identifier, "")

    def test_user_view_hides_session_identifier(self) -> None:
        session = FakeSession()
        result = self._run(session, expected_outputs=("summary.json",))
        self.assertNotIn("sessionIdentifier", result.user_view())
        self.assertIn("sessionIdentifier", result.audit_view())

    def test_user_view_hides_request_id_and_internal_paths(self) -> None:
        session = FakeSession()
        result = self._run(session, expected_outputs=("summary.json",))
        result.plan = "Read /mnt/data/sales.csv"
        result.stdout = f"saved /tmp/result.json in {result.session_identifier}"
        view = result.user_view()
        self.assertNotIn("requestId", view)
        self.assertNotIn("/mnt/data", view["plan"])
        self.assertNotIn("/tmp", view["stdout"])
        self.assertNotIn(result.session_identifier, view["stdout"])

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

    def test_audit_records_session_deletion(self) -> None:
        session = FakeSession()
        result = self._run(session, expected_outputs=("summary.json",))
        steps = [entry["step"] for entry in result.audit]
        self.assertIn("session-deleted", steps)
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
        self.assertIn("session-delete-failed", steps)


class BrokerIdentifierTests(unittest.TestCase):
    def test_identifier_is_unpredictable_and_prefixed(self) -> None:
        first = broker.new_session_identifier("py")
        second = broker.new_session_identifier("py")
        self.assertTrue(first.startswith("py-"))
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first.split("-", 1)[1]), 32)

    def test_identifier_does_not_embed_user_input(self) -> None:
        identifier = broker.new_session_identifier("py")
        self.assertNotIn("user", identifier)


class ExecutionBackendTests(unittest.TestCase):
    def test_dynamic_sessions_backend_uses_python_session(self) -> None:
        settings = config.Settings(
            execution_backend="dynamic-sessions",
            python_endpoint="https://sessions.example",
        )
        sentinel = object()
        with mock.patch.object(
            broker,
            "PythonSession",
            return_value=sentinel,
        ) as session_type:
            self.assertIs(broker.create_python_session(settings), sentinel)
        session_type.assert_called_once_with("https://sessions.example")

    def test_sandboxes_backend_uses_sandbox_session(self) -> None:
        settings = config.Settings(execution_backend="sandboxes")
        sentinel = object()
        with mock.patch.object(
            broker,
            "SandboxesPythonSession",
            return_value=sentinel,
        ) as session_type:
            self.assertIs(broker.create_python_session(settings), sentinel)
        session_type.assert_called_once_with(settings)

    def test_sandbox_backend_requires_code_interpreter_image(self) -> None:
        session = broker.SandboxesPythonSession.__new__(
            broker.SandboxesPythonSession
        )
        session.settings = config.Settings(execution_backend="sandboxes")
        session._client = mock.Mock()
        session._client.list_disk_images.return_value = []
        with self.assertRaises(broker.BrokerError):
            session._create_sandbox()
        session._client.begin_create_sandbox.assert_not_called()

    def test_unknown_execution_backend_is_rejected(self) -> None:
        settings = config.Settings(execution_backend="unknown")
        with self.assertRaises(config.ConfigError):
            broker.create_python_session(settings)

    def test_sandbox_delete_can_retry_after_transient_failure(self) -> None:
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

        session = broker.SandboxesPythonSession.__new__(
            broker.SandboxesPythonSession
        )
        session._closed = False
        session._sandbox = FakeSandbox()
        session._client = None
        session._credential = None
        session._owns_client = False
        session._owns_credential = False
        with self.assertRaises(broker.BrokerError):
            session.delete()
        self.assertFalse(session._closed)
        self.assertEqual(session.delete(), 204)
        self.assertTrue(session._closed)
        self.assertEqual(session._sandbox.delete_calls, 2)
        self.assertEqual(session._sandbox.close_calls, 1)

    def test_startup_cleanup_deletes_matching_gateway_sandboxes(self) -> None:
        class FakeSandbox:
            def __init__(self) -> None:
                self.deleted = False
                self.closed = False

            def delete(self) -> None:
                self.deleted = True

            def close(self) -> None:
                self.closed = True

        matching = SimpleNamespace(
            id="matching",
            labels={"component": "python-gateway"},
            state="Stopped",
            created_at=(
                datetime.now(timezone.utc) - timedelta(hours=2)
            ).isoformat().replace("+00:00", "Z"),
        )
        other = SimpleNamespace(
            id="other",
            labels={"component": "office-gateway"},
            state="Stopped",
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        sandbox = FakeSandbox()
        group = mock.Mock()
        group.list_sandboxes.return_value = [matching, other]
        group.get_sandbox_client.return_value = sandbox
        settings = config.Settings(execution_backend="sandboxes")
        deleted = broker.cleanup_gateway_sandboxes(
            settings,
            "python-gateway",
            group_client=group,
            credential=object(),
        )
        self.assertEqual(deleted, 1)
        group.get_sandbox_client.assert_called_once_with("matching")
        self.assertTrue(sandbox.deleted)
        self.assertTrue(sandbox.closed)

    def test_startup_cleanup_keeps_recent_or_running_sandboxes(self) -> None:
        recent = SimpleNamespace(
            id="recent",
            labels={"component": "python-gateway"},
            state="Stopped",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        running = SimpleNamespace(
            id="running",
            labels={"component": "python-gateway"},
            state="Running",
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        suspended = SimpleNamespace(
            id="suspended",
            labels={"component": "other-gateway"},
            state="Suspended",
            created_at=(
                datetime.now(timezone.utc) - timedelta(hours=2)
            ).isoformat(),
        )
        group = mock.Mock()
        group.list_sandboxes.return_value = [recent, running, suspended]
        settings = config.Settings(execution_backend="sandboxes")
        deleted = broker.cleanup_gateway_sandboxes(
            settings,
            "python-gateway",
            group_client=group,
            credential=object(),
        )
        self.assertEqual(deleted, 0)
        group.get_sandbox_client.assert_not_called()

    def test_startup_cleanup_deletes_old_suspended_sandbox(self) -> None:
        resource = SimpleNamespace(
            id="suspended",
            labels={"component": "python-gateway"},
            state="Suspended",
            created_at=(
                datetime.now(timezone.utc) - timedelta(hours=2)
            ).isoformat(),
        )
        sandbox = mock.Mock()
        group = mock.Mock()
        group.list_sandboxes.return_value = [resource]
        group.get_sandbox_client.return_value = sandbox
        settings = config.Settings(execution_backend="sandboxes")
        deleted = broker.cleanup_gateway_sandboxes(
            settings,
            "python-gateway",
            group_client=group,
            credential=object(),
        )
        self.assertEqual(deleted, 1)
        sandbox.delete.assert_called_once()
        sandbox.close.assert_called_once()

    def test_partial_creation_cleanup_waits_for_late_visibility(self) -> None:
        resource = SimpleNamespace(
            id="late",
            labels={"gateway-request": "request"},
        )

        class FakeSandbox:
            def __init__(self) -> None:
                self.deleted = False
                self.closed = False

            def delete(self) -> None:
                self.deleted = True

            def close(self) -> None:
                self.closed = True

        sandbox = FakeSandbox()
        group = mock.Mock()
        group.list_sandboxes.side_effect = [[], [], [], [resource]]
        group.get_sandbox_client.return_value = sandbox
        broker.delete_sandboxes_by_label(
            group,
            "gateway-request",
            "request",
            attempts=4,
            delay_seconds=0,
        )
        self.assertEqual(group.list_sandboxes.call_count, 4)
        self.assertTrue(sandbox.deleted)
        self.assertTrue(sandbox.closed)

    def test_partial_creation_cleanup_preserves_unresolved_error(self) -> None:
        class FailingSandbox:
            def delete(self) -> None:
                raise RuntimeError("still deleting")

            def close(self) -> None:
                return

        group = mock.Mock()
        group.list_sandboxes.return_value = []
        with self.assertRaises(broker.BrokerError):
            broker.delete_sandboxes_by_label(
                group,
                "gateway-request",
                "request",
                known_sandbox=FailingSandbox(),
                attempts=2,
                delay_seconds=0,
            )

    def test_orphan_cleanup_continues_after_one_delete_failure(self) -> None:
        first = SimpleNamespace(
            id="first",
            labels={"component": "python-gateway"},
            state="Stopped",
            created_at=(
                datetime.now(timezone.utc) - timedelta(hours=2)
            ).isoformat(),
        )
        second = SimpleNamespace(
            id="second",
            labels={"component": "python-gateway"},
            state="Stopped",
            created_at=(
                datetime.now(timezone.utc) - timedelta(hours=2)
            ).isoformat(),
        )
        failing = mock.Mock()
        failing.delete.side_effect = RuntimeError("transient")
        succeeding = mock.Mock()
        group = mock.Mock()
        group.list_sandboxes.return_value = [first, second]
        group.get_sandbox_client.side_effect = [failing, succeeding]
        settings = config.Settings(execution_backend="sandboxes")
        with self.assertRaises(broker.BrokerError):
            broker.cleanup_gateway_sandboxes(
                settings,
                "python-gateway",
                group_client=group,
                credential=object(),
            )
        succeeding.delete.assert_called_once()
        succeeding.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
