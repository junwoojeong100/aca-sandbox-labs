"""ACA Sandboxes backend, cleanup, and entrypoint tests."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent import execution, policy as common_policy, staging
from aca_sandboxes import (
    cli,
    cleanup,
    config,
    office_client,
    office_gateway,
    policy as backend_policy,
    python_gateway,
    python_session,
)
from office_gateway.service import GatewayError


class ACASandboxesConfigTests(unittest.TestCase):
    def test_subscription_id_is_resolved_and_cached(self) -> None:
        settings = config.Settings(subscription_id=None)
        with mock.patch.object(
            config.subprocess,
            "run",
            return_value=SimpleNamespace(stdout="subscription-test\n"),
        ) as run:
            first = settings.resolved_subscription_id()
            second = settings.resolved_subscription_id()
        self.assertEqual(first, "subscription-test")
        self.assertEqual(second, first)
        run.assert_called_once()
        self.assertIn("account", run.call_args.args[0])

    def test_execution_timeout_uses_aca_environment(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"ACA_EXECUTION_TIMEOUT_SECONDS": "750"},
        ):
            settings = config.Settings()
        self.assertEqual(settings.execution_timeout_seconds, 750)


class ACASandboxesPolicyTests(unittest.TestCase):
    @staticmethod
    def _input(text: str, **kwargs) -> common_policy.PolicyInput:
        return common_policy.PolicyInput(
            tenant_id="tenant",
            user_id="user",
            request_text=text,
            **kwargs,
        )

    def test_python_requests_get_aca_sandbox_controls(self) -> None:
        decision = backend_policy.classify(self._input("매출을 집계해줘"))
        self.assertEqual(decision.route, common_policy.Route.PYTHON)
        self.assertEqual(
            decision.controls["runtime"],
            "aca-python-sandbox",
        )
        self.assertEqual(decision.controls["network"], "Deny")

    def test_execution_limit_matches_reference_timeout(self) -> None:
        allowed = backend_policy.classify(
            self._input("분석해줘", estimated_seconds=900)
        )
        rejected = backend_policy.classify(
            self._input("분석해줘", estimated_seconds=901)
        )
        self.assertTrue(allowed.allowed)
        self.assertEqual(
            allowed.controls["maxExecutionSeconds"],
            900,
        )
        self.assertEqual(rejected.classification, "C")
        self.assertFalse(rejected.allowed)

    def test_aggregate_attachment_limit_routes_to_async_compute(self) -> None:
        decision = backend_policy.classify(
            self._input(
                "두 CSV를 비교해줘",
                attachment_names=("a.csv", "b.csv"),
                attachment_sizes=(70 * 1024 * 1024,) * 2,
            )
        )
        self.assertEqual(decision.classification, "C")
        self.assertEqual(
            decision.route,
            common_policy.Route.ASYNC_COMPUTE,
        )
        self.assertFalse(decision.allowed)


class ACAPythonSessionTests(unittest.TestCase):
    def test_python_image_work_directories_are_writable_by_app(self) -> None:
        dockerfile = (
            Path(__file__).resolve().parents[2]
            / "aca_sandboxes"
            / "images"
            / "python"
            / "Dockerfile"
        ).read_text(encoding="utf-8")
        self.assertIn("mkdir -p /mnt/data /work", dockerfile)
        self.assertIn("chown -R app:app /mnt/data /work", dockerfile)

    @staticmethod
    def _session(sandbox) -> python_session.PythonSession:
        session = python_session.PythonSession.__new__(
            python_session.PythonSession
        )
        session.settings = config.Settings(subscription_id="sub")
        session.identifier = "sandbox-test"
        session._closed = False
        session._sandbox = sandbox
        session._client = None
        session._credential = None
        session._owns_client = False
        session._owns_credential = False
        return session

    def test_execute_keeps_code_out_of_command(self) -> None:
        class FakeSandbox:
            def __init__(self) -> None:
                self.writes = []
                self.commands = []
                self.deleted_files = []

            def write_file(self, path, content) -> None:
                self.writes.append((path, content))

            def exec(self, command):
                self.commands.append(command)
                return SimpleNamespace(
                    exit_code=0,
                    stdout="ok",
                    stderr="",
                )

            def delete_file(self, path) -> None:
                self.deleted_files.append(path)

        sandbox = FakeSandbox()
        session = self._session(sandbox)
        code = "print('safe'); __import__('os').system('rm -rf /')"
        result = session.execute(code)
        self.assertTrue(result.succeeded)
        self.assertEqual(sandbox.writes[0][1], code)
        self.assertNotIn("rm -rf", sandbox.commands[0])
        self.assertEqual(sandbox.deleted_files, [sandbox.writes[0][0]])

    def test_unsafe_upload_name_is_rejected_before_write(self) -> None:
        sandbox = mock.Mock()
        session = self._session(sandbox)
        with self.assertRaises(execution.ExecutionError):
            session.upload("../secret.csv", b"secret")
        sandbox.write_file.assert_not_called()

    def test_download_rejects_oversized_snapshot_before_read(self) -> None:
        sandbox = mock.Mock()
        sandbox.exec.return_value = SimpleNamespace(exit_code=0, stderr="")
        sandbox.stat_file.return_value = SimpleNamespace(
            size=python_session.MAX_DOWNLOAD_BYTES + 1
        )
        session = self._session(sandbox)
        with self.assertRaises(execution.ExecutionError):
            session.download("report.json")
        sandbox.read_file.assert_not_called()

    def test_download_wraps_sdk_failure(self) -> None:
        sandbox = mock.Mock()
        sandbox.exec.return_value = SimpleNamespace(exit_code=0, stderr="")
        sandbox.stat_file.return_value = SimpleNamespace(size=4)
        sandbox.read_file.side_effect = RuntimeError("sdk failure")
        client = python_session.PythonSession.__new__(
            python_session.PythonSession
        )
        client._sandbox = sandbox
        with self.assertRaises(execution.ExecutionError):
            client.download("summary.json")
        sandbox.delete_file.assert_called_once()

    def test_latest_ready_python_image_is_selected(self) -> None:
        client = mock.Mock()
        session = self._session(mock.Mock())
        session._client = client
        session._client.list_disk_images.return_value = [
            SimpleNamespace(
                id="old",
                labels={"name": "python-code-interpreter-20260101"},
                status=SimpleNamespace(state="Ready"),
            ),
            SimpleNamespace(
                id="new",
                labels={"name": "python-code-interpreter-20260201"},
                status=SimpleNamespace(state="Succeeded"),
            ),
            SimpleNamespace(
                id="building",
                labels={"name": "python-code-interpreter-20260301"},
                status=SimpleNamespace(state="Building"),
            ),
        ]
        self.assertEqual(session._latest_python_disk_image(), "new")

    def test_delete_can_retry_after_transient_failure(self) -> None:
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

        sandbox = FakeSandbox()
        session = self._session(sandbox)
        with self.assertRaises(execution.ExecutionError):
            session.delete()
        self.assertFalse(session._closed)
        self.assertEqual(session.delete(), 204)
        self.assertTrue(session._closed)
        self.assertEqual(sandbox.delete_calls, 2)
        self.assertEqual(sandbox.close_calls, 1)

    def test_init_failure_deletes_allocated_sandbox(self) -> None:
        class FakeSandbox:
            sandbox_id = "sandbox-test"

            def __init__(self) -> None:
                self.deleted = False
                self.closed = False

            def mkdir(self, path: str) -> None:
                raise RuntimeError("mount failed")

            def delete(self) -> None:
                self.deleted = True

            def close(self) -> None:
                self.closed = True

        sandbox = FakeSandbox()
        with mock.patch.object(
            python_session.PythonSession,
            "_create_sandbox",
            return_value=sandbox,
        ):
            with self.assertRaises(execution.ExecutionError):
                python_session.PythonSession(
                    config.Settings(subscription_id="sub"),
                    group_client=object(),
                    credential=object(),
                )
        self.assertTrue(sandbox.deleted)
        self.assertTrue(sandbox.closed)


class ACAOfficeClientTests(unittest.TestCase):
    def test_office_image_contains_reference_operations(self) -> None:
        dockerfile = (
            Path(__file__).resolve().parents[2]
            / "aca_sandboxes"
            / "images"
            / "office"
            / "Dockerfile"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "COPY office_gateway/container_api.py /app/server.py",
            dockerfile,
        )

    @staticmethod
    def _client(sandbox) -> office_client.OfficeClient:
        client = office_client.OfficeClient.__new__(
            office_client.OfficeClient
        )
        client.settings = config.Settings(subscription_id="sub")
        client.identifier = "office-test"
        client._closed = False
        client._sandbox = sandbox
        client._client = None
        client._credential = None
        client._owns_client = False
        client._owns_credential = False
        return client

    def test_payload_is_written_to_file_not_embedded_in_command(self) -> None:
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

        sandbox = FakeSandbox()
        client = self._client(sandbox)
        payload = {"title": "title; rm -rf /", "content": "draft"}
        response = client._invoke("generate", payload)
        self.assertEqual(response["jobId"], "job")
        self.assertNotIn("rm -rf", sandbox.commands[0])
        self.assertIn("rm -rf", sandbox.writes[0][1])
        self.assertEqual(sandbox.deleted_files, [sandbox.writes[0][0]])

    def test_download_rejects_unsafe_path(self) -> None:
        client = self._client(object())
        with self.assertRaises(GatewayError) as context:
            client.download("/files/job/../../secret")
        self.assertEqual(context.exception.status, 400)

    def test_download_rejects_oversized_file_before_read(self) -> None:
        sandbox = mock.Mock()
        sandbox.exec.return_value = SimpleNamespace(exit_code=0, stderr="")
        sandbox.stat_file.return_value = SimpleNamespace(
            size=staging.MAX_ARTIFACT_BYTES + 1
        )
        client = self._client(sandbox)
        with self.assertRaises(GatewayError) as context:
            client.download("/files/job/report.pdf")
        self.assertEqual(context.exception.status, 413)
        sandbox.read_file.assert_not_called()

    def test_download_rejects_unknown_file_size(self) -> None:
        sandbox = mock.Mock()
        sandbox.exec.return_value = SimpleNamespace(exit_code=0, stderr="")
        sandbox.stat_file.return_value = SimpleNamespace(size=None)
        client = self._client(sandbox)
        with self.assertRaises(GatewayError) as context:
            client.download("/files/job/report.pdf")
        self.assertEqual(context.exception.status, 413)
        sandbox.read_file.assert_not_called()

    def test_stop_can_retry_after_transient_failure(self) -> None:
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

        sandbox = FakeSandbox()
        client = self._client(sandbox)
        with self.assertRaises(GatewayError):
            client.stop()
        self.assertFalse(client._closed)
        client.stop()
        self.assertTrue(client._closed)
        self.assertEqual(sandbox.delete_calls, 2)
        self.assertEqual(sandbox.close_calls, 1)

    def test_init_failure_deletes_allocated_sandbox(self) -> None:
        class FakeSandbox:
            sandbox_id = "sandbox-test"

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
        settings = config.Settings(
            subscription_id="sub",
            office_disk_image_id="disk",
        )
        with (
            mock.patch.object(
                office_client.OfficeClient,
                "_create_sandbox",
                return_value=sandbox,
            ),
            mock.patch.object(
                Path,
                "read_text",
                return_value="runner",
            ),
        ):
            with self.assertRaises(GatewayError):
                office_client.OfficeClient(
                    settings,
                    "office-test",
                    group_client=object(),
                    credential=object(),
                )
        self.assertTrue(sandbox.deleted)
        self.assertTrue(sandbox.closed)


class ACACleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = config.Settings(subscription_id="sub")
        self.old = datetime.now(timezone.utc) - timedelta(hours=2)

    def test_cleanup_deletes_matching_stopped_sandbox(self) -> None:
        matching = SimpleNamespace(
            id="matching",
            labels={"component": "python-gateway"},
            state="Stopped",
            created_at=self.old.isoformat().replace("+00:00", "Z"),
        )
        other = SimpleNamespace(
            id="other",
            labels={"component": "office-gateway"},
            state="Stopped",
            created_at=self.old,
        )
        sandbox = mock.Mock()
        group = mock.Mock()
        group.list_sandboxes.return_value = [matching, other]
        group.get_sandbox_client.return_value = sandbox
        deleted = cleanup.cleanup_gateway_sandboxes(
            self.settings,
            "python-gateway",
            group_client=group,
            credential=object(),
        )
        self.assertEqual(deleted, 1)
        group.get_sandbox_client.assert_called_once_with("matching")
        sandbox.delete.assert_called_once()
        sandbox.close.assert_called_once()

    def test_cleanup_keeps_recent_running_and_excluded_sandboxes(self) -> None:
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
            created_at=self.old,
        )
        excluded = SimpleNamespace(
            id="active-office",
            labels={"component": "python-gateway"},
            state="Suspended",
            created_at=self.old,
        )
        group = mock.Mock()
        group.list_sandboxes.return_value = [recent, running, excluded]
        deleted = cleanup.cleanup_gateway_sandboxes(
            self.settings,
            "python-gateway",
            group_client=group,
            credential=object(),
            exclude_ids={"active-office"},
        )
        self.assertEqual(deleted, 0)
        group.get_sandbox_client.assert_not_called()

    def test_delete_by_label_waits_for_late_visibility(self) -> None:
        resource = SimpleNamespace(
            id="late",
            labels={"gateway-request": "request"},
        )
        sandbox = mock.Mock()
        group = mock.Mock()
        group.list_sandboxes.side_effect = [[], [], [], [resource]]
        group.get_sandbox_client.return_value = sandbox
        cleanup.delete_by_label(
            group,
            "gateway-request",
            "request",
            attempts=4,
            delay_seconds=0,
        )
        self.assertEqual(group.list_sandboxes.call_count, 4)
        sandbox.delete.assert_called_once()
        sandbox.close.assert_called_once()

    def test_delete_by_label_preserves_unresolved_error(self) -> None:
        known = mock.Mock()
        known.delete.side_effect = RuntimeError("still deleting")
        group = mock.Mock()
        group.list_sandboxes.return_value = []
        with self.assertRaises(execution.ExecutionError):
            cleanup.delete_by_label(
                group,
                "gateway-request",
                "request",
                known_sandbox=known,
                attempts=2,
                delay_seconds=0,
            )
        self.assertEqual(known.delete.call_count, 2)
        known.close.assert_called_once()

    def test_orphan_cleanup_continues_after_delete_failure(self) -> None:
        resources = [
            SimpleNamespace(
                id=name,
                labels={"component": "python-gateway"},
                state="Stopped",
                created_at=self.old,
            )
            for name in ("first", "second")
        ]
        failing = mock.Mock()
        failing.delete.side_effect = RuntimeError("transient")
        succeeding = mock.Mock()
        group = mock.Mock()
        group.list_sandboxes.return_value = resources
        group.get_sandbox_client.side_effect = [failing, succeeding]
        with self.assertRaises(execution.ExecutionError):
            cleanup.cleanup_gateway_sandboxes(
                self.settings,
                "python-gateway",
                group_client=group,
                credential=object(),
            )
        succeeding.delete.assert_called_once()
        succeeding.close.assert_called_once()


class ACAGatewayEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_python_gateway_builds_aca_sandboxes_service(self) -> None:
        backend_settings = config.Settings(subscription_id="sub")
        runner = object()
        session = object()
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "PYTHON_USER_WORK_DIR": str(self.root / "python"),
                    "MAX_ACTIVE_PYTHON_JOBS": "4",
                },
            ),
            mock.patch.object(
                python_gateway.config,
                "Settings",
                return_value=backend_settings,
            ),
            mock.patch.object(
                python_gateway.orchestrator,
                "Orchestrator",
                return_value=runner,
            ) as orchestrator_type,
            mock.patch.object(
                python_gateway,
                "PythonSession",
                return_value=session,
            ) as session_type,
        ):
            gateway = python_gateway.build_service()
            self.assertIs(gateway.runner_factory(), runner)
            session_factory = orchestrator_type.call_args.args[0]
            self.assertIs(session_factory(), session)
        self.assertEqual(gateway.backend, "aca-sandboxes")
        self.assertEqual(gateway.max_active_jobs, 4)
        self.assertEqual(
            orchestrator_type.call_args.args[1],
            "aca-sandboxes",
        )
        self.assertEqual(
            orchestrator_type.call_args.args[3].execution_timeout_seconds,
            900,
        )
        session_type.assert_called_once_with(backend_settings)

    def test_office_gateway_builds_aca_sandboxes_service(self) -> None:
        backend_settings = config.Settings(subscription_id="sub")
        client = object()
        with (
            mock.patch.dict(
                "os.environ",
                {"OFFICE_USER_WORK_DIR": str(self.root / "office")},
            ),
            mock.patch.object(
                office_gateway.config,
                "Settings",
                return_value=backend_settings,
            ),
            mock.patch.object(
                office_gateway,
                "OfficeClient",
                return_value=client,
            ) as client_type,
        ):
            gateway = office_gateway.build_service()
            self.assertIs(gateway.client_factory("office-test"), client)
        self.assertEqual(gateway.backend, "aca-sandboxes")
        client_type.assert_called_once_with(
            backend_settings,
            "office-test",
        )

    def test_cli_honors_artifact_directory_overrides(self) -> None:
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "STAGING_DIR": str(self.root / "custom-staging"),
                    "APPROVED_DIR": str(self.root / "custom-approved"),
                },
            ),
            mock.patch.object(
                cli.cli_common,
                "run",
                return_value=0,
            ) as run,
        ):
            self.assertEqual(cli.main([]), 0)
        settings = run.call_args.kwargs["settings"]
        self.assertEqual(
            settings.staging_dir,
            self.root / "custom-staging",
        )
        self.assertEqual(
            settings.approved_dir,
            self.root / "custom-approved",
        )


if __name__ == "__main__":
    unittest.main()
