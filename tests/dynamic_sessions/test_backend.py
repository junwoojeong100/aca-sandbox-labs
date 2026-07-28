"""Dynamic Sessions backend and entrypoint tests."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent import execution, policy as common_policy
from dynamic_sessions import (
    cli,
    config,
    office_client,
    office_gateway,
    policy as backend_policy,
    python_gateway,
    python_session,
)
from office_gateway.service import GatewayError


class DynamicSessionsConfigTests(unittest.TestCase):
    def test_python_endpoint_is_resolved_and_cached(self) -> None:
        completed = SimpleNamespace(
            stdout="https://python.sessions.example/\n"
        )
        settings = config.Settings(
            resource_group="rg-test",
            python_pool_name="python-pool",
        )
        with mock.patch.object(
            config.subprocess,
            "run",
            return_value=completed,
        ) as run:
            first = settings.resolved_python_endpoint()
            second = settings.resolved_python_endpoint()
        self.assertEqual(first, "https://python.sessions.example/")
        self.assertEqual(second, first)
        run.assert_called_once()
        self.assertIn("python-pool", run.call_args.args[0])
        self.assertIn("rg-test", run.call_args.args[0])

    def test_empty_pool_endpoint_is_rejected(self) -> None:
        with mock.patch.object(
            config.subprocess,
            "run",
            return_value=SimpleNamespace(stdout=" \n"),
        ):
            with self.assertRaises(RuntimeError):
                config._pool_endpoint("python-pool", "rg-test")


class DynamicSessionsPolicyTests(unittest.TestCase):
    @staticmethod
    def _input(text: str, **kwargs) -> common_policy.PolicyInput:
        return common_policy.PolicyInput(
            tenant_id="tenant",
            user_id="user",
            request_text=text,
            **kwargs,
        )

    def test_long_running_python_request_routes_to_async_compute(self) -> None:
        decision = backend_policy.classify(
            self._input("대량 배치", estimated_seconds=600)
        )
        self.assertEqual(decision.classification, "C")
        self.assertEqual(
            decision.route,
            common_policy.Route.ASYNC_COMPUTE,
        )
        self.assertFalse(decision.allowed)

    def test_per_file_and_aggregate_upload_limits_are_enforced(self) -> None:
        oversized = backend_policy.classify(
            self._input(
                "분석해줘",
                attachment_sizes=(200 * 1024 * 1024,),
            )
        )
        aggregate = backend_policy.classify(
            self._input(
                "두 CSV를 비교해줘",
                attachment_names=("a.csv", "b.csv"),
                attachment_sizes=(70 * 1024 * 1024,) * 2,
            )
        )
        self.assertEqual(oversized.classification, "C")
        self.assertEqual(aggregate.classification, "C")
        self.assertIn("전체 크기", aggregate.reason)

    def test_office_policy_exposes_only_supported_operations(self) -> None:
        decision = backend_policy.classify(
            self._input("결과를 pptx 보고서로 만들어줘")
        )
        self.assertEqual(decision.route, common_policy.Route.OFFICE)
        self.assertEqual(
            decision.controls["allowedOperations"],
            ["generate", "convert", "edit"],
        )


class DynamicPythonSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = config.Settings(
            python_endpoint="https://sessions.example",
            python_api_version="python-preview",
            session_api_version="session-preview",
        )

    def test_execute_retries_with_properties_wrapper_for_api_drift(self) -> None:
        requests: list[dict[str, object]] = []

        def fake_request(method, url, **kwargs):
            requests.append(json.loads(kwargs["body"]))
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

        with mock.patch.object(
            python_session,
            "_request",
            side_effect=fake_request,
        ):
            result = python_session.PythonSession(
                self.settings,
                "py-test",
            ).execute("print('ok')")

        self.assertTrue(result.succeeded)
        self.assertNotIn("properties", requests[0])
        self.assertIn("properties", requests[1])

    def test_execute_returns_shared_execution_result(self) -> None:
        payload = (
            b'{"status":"Succeeded","result":'
            b'{"stdout":"ok","stderr":"warning","executionResult":42}}'
        )
        with mock.patch.object(
            python_session,
            "_request",
            return_value=(200, payload, "application/json"),
        ):
            result = python_session.PythonSession(
                self.settings,
                "py-test",
            ).execute("print('ok')")
        self.assertIsInstance(result, execution.ExecutionResult)
        self.assertEqual(result.result, 42)
        self.assertEqual(result.warnings, "warning")

    def test_urls_keep_identifier_and_api_versions_backend_only(self) -> None:
        session = python_session.PythonSession(self.settings, "py opaque")
        execution_url = session._url("executions")
        info_url = session._url(
            "session",
            self.settings.session_api_version,
        )
        self.assertIn("identifier=py+opaque", execution_url)
        self.assertIn("api-version=python-preview", execution_url)
        self.assertIn("api-version=session-preview", info_url)

    def test_oversized_upload_is_rejected_before_request(self) -> None:
        session = python_session.PythonSession(self.settings, "py-test")
        with (
            mock.patch.object(
                python_session,
                "_request",
            ) as request,
            self.assertRaises(execution.ExecutionError),
        ):
            session.upload("large.csv", b"x" * (128 * 1024 * 1024 + 1))
        request.assert_not_called()

    def test_delete_is_idempotent(self) -> None:
        session = python_session.PythonSession(self.settings, "py-test")
        with mock.patch.object(
            python_session,
            "_request",
            return_value=(204, b"", ""),
        ) as request:
            self.assertEqual(session.delete(), 204)
            self.assertEqual(session.delete(), 0)
        request.assert_called_once()
        self.assertIn("api-version=session-preview", request.call_args.args[1])

    def test_endpoint_resolution_failure_is_translated(self) -> None:
        settings = config.Settings()
        with mock.patch.object(
            config.subprocess,
            "run",
            side_effect=subprocess.CalledProcessError(1, ["az"]),
        ):
            with self.assertRaises(execution.ExecutionError):
                python_session.PythonSession(settings)

    def test_transport_timeout_is_translated(self) -> None:
        with (
            mock.patch.object(
                python_session.auth,
                "get_token",
                return_value="token",
            ),
            mock.patch.object(
                python_session.urllib.request,
                "urlopen",
                side_effect=TimeoutError("timed out"),
            ),
        ):
            with self.assertRaises(execution.ExecutionError):
                self.client = python_session.PythonSession(
                    self.settings,
                    "py-test",
                )
                self.client.list_files()

    def test_failed_delete_can_be_retried(self) -> None:
        session = python_session.PythonSession(self.settings, "py-test")
        with mock.patch.object(
            python_session,
            "_request",
            side_effect=[
                (500, b"failed", ""),
                (204, b"", ""),
            ],
        ) as request:
            with self.assertRaises(execution.ExecutionError):
                session.delete()
            self.assertEqual(session.delete(), 204)
        self.assertEqual(request.call_count, 2)


class DynamicOfficeClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = config.Settings(
            office_endpoint="https://office.sessions.example",
            session_api_version="session-preview",
        )
        self.client = office_client.OfficeClient(
            self.settings,
            "office-test",
        )

    def test_generate_uses_backend_json_contract(self) -> None:
        with mock.patch.object(
            self.client,
            "_request",
            return_value=(
                200,
                b'{"jobId":"job","files":[]}',
                "application/json",
            ),
        ) as request:
            response = self.client.generate("Title", "Draft")
        self.assertEqual(response["jobId"], "job")
        self.assertEqual(request.call_args.args[:2], ("POST", "/generate"))
        self.assertEqual(
            request.call_args.kwargs["payload"],
            {"title": "Title", "content": "Draft"},
        )

    def test_backend_error_preserves_allowed_operation_details(self) -> None:
        with mock.patch.object(
            self.client,
            "_request",
            return_value=(
                400,
                b'{"error":"invalid target","allowed":["pdf"]}',
                "application/json",
            ),
        ):
            with self.assertRaises(GatewayError) as context:
                self.client.convert("job", "report.docx", "exe")
        self.assertEqual(context.exception.status, 400)
        self.assertEqual(context.exception.details["allowed"], ["pdf"])

    def test_non_json_success_is_rejected(self) -> None:
        with mock.patch.object(
            self.client,
            "_request",
            return_value=(200, b"not-json", "text/plain"),
        ):
            with self.assertRaises(GatewayError) as context:
                self.client.generate("Title", "Draft")
        self.assertEqual(context.exception.status, 502)

    def test_stop_accepts_not_found_for_expired_session(self) -> None:
        with mock.patch.object(
            self.client,
            "_request",
            return_value=(404, b"", ""),
        ) as request:
            self.client.stop()
        self.assertEqual(
            request.call_args.kwargs["query"],
            {"api-version": "session-preview"},
        )

    def test_token_command_failure_is_not_exposed_as_internal_path(self) -> None:
        with mock.patch.object(
            office_client.auth,
            "get_token",
            side_effect=subprocess.TimeoutExpired(
                ["/Users/internal/az"],
                120,
            ),
        ):
            with self.assertRaises(GatewayError) as context:
                self.client.generate("Title", "Draft")
        self.assertEqual(context.exception.status, 502)
        self.assertNotIn(
            "/Users/internal",
            context.exception.message,
        )

    def test_endpoint_resolution_failure_is_translated(self) -> None:
        settings = config.Settings()
        with mock.patch.object(
            config.subprocess,
            "run",
            side_effect=subprocess.CalledProcessError(1, ["az"]),
        ):
            with self.assertRaises(GatewayError) as context:
                office_client.OfficeClient(settings, "office-test")
        self.assertEqual(context.exception.status, 502)

    def test_transport_timeout_is_translated(self) -> None:
        with (
            mock.patch.object(
                office_client.auth,
                "get_token",
                return_value="token",
            ),
            mock.patch.object(
                office_client.urllib.request,
                "urlopen",
                side_effect=TimeoutError("timed out"),
            ),
        ):
            with self.assertRaises(GatewayError) as context:
                self.client.generate("Title", "Draft")
        self.assertEqual(context.exception.status, 502)


class DynamicGatewayEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_python_gateway_builds_dynamic_sessions_service(self) -> None:
        backend_settings = config.Settings(
            python_endpoint="https://sessions.example"
        )
        runner = object()
        session = object()
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "PYTHON_USER_WORK_DIR": str(self.root / "python"),
                    "MAX_ACTIVE_PYTHON_JOBS": "3",
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
        self.assertEqual(gateway.backend, "dynamic-sessions")
        self.assertEqual(gateway.max_active_jobs, 3)
        self.assertEqual(
            orchestrator_type.call_args.args[1],
            "dynamic-sessions",
        )
        session_type.assert_called_once_with(backend_settings)

    def test_office_gateway_builds_dynamic_sessions_service(self) -> None:
        backend_settings = config.Settings(
            office_endpoint="https://office.sessions.example"
        )
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
        self.assertEqual(gateway.backend, "dynamic-sessions")
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
