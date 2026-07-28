"""ACA Sandboxes Office document client."""

from __future__ import annotations

import json
import mimetypes
import re
import shlex
import uuid
from pathlib import Path
from typing import Any

from agent import staging
from office_gateway.service import GatewayError
from . import cleanup, config


class OfficeClient:
    DOWNLOAD_PATH = re.compile(
        r"^/files/([A-Za-z0-9]{1,64})/([A-Za-z0-9._-]{1,100})$"
    )

    @property
    def sandbox_id(self) -> str:
        return str(self._sandbox.sandbox_id)

    def __init__(
        self,
        settings: config.Settings,
        identifier: str,
        *,
        group_client: Any | None = None,
        credential: Any | None = None,
    ) -> None:
        self.settings = settings
        self.identifier = identifier
        self._closed = False
        self._credential = credential
        self._owns_credential = credential is None
        self._client = group_client
        self._owns_client = group_client is None
        self._sandbox: Any | None = None
        try:
            if self._client is None:
                try:
                    from azure.containerapps.sandbox import (
                        SandboxGroupClient,
                        endpoint_for_region,
                    )
                    from azure.identity import DefaultAzureCredential
                except ImportError as error:
                    raise GatewayError(
                        500,
                        "ACA Sandboxes SDK is not installed",
                    ) from error
                if self._credential is None:
                    self._credential = DefaultAzureCredential(
                        exclude_interactive_browser_credential=True
                    )
                self._client = SandboxGroupClient(
                    endpoint_for_region(settings.location),
                    self._credential,
                    subscription_id=settings.resolved_subscription_id(),
                    resource_group=settings.resource_group,
                    sandbox_group=settings.sandbox_group_name,
                )
            disk_image_id = (
                settings.office_disk_image_id
                or self._latest_office_disk_image()
            )
            self._sandbox = self._create_sandbox(disk_image_id)
            runner_source = (
                Path(__file__)
                .with_name("office_runner.py")
                .read_text(encoding="utf-8")
            )
            self._sandbox.write_file(
                "/tmp/office_gateway_runner.py",
                runner_source,
            )
        except Exception as error:
            cleanup_error: Exception | None = None
            if self._sandbox is not None:
                try:
                    self._sandbox.delete()
                except Exception as failure:
                    cleanup_error = failure
                finally:
                    self._sandbox.close()
            self._close_clients()
            if isinstance(error, GatewayError):
                raise
            raise GatewayError(
                502,
                (
                    "Office Sandbox creation failed"
                    + (
                        "; server-side auto-delete will retry cleanup"
                        if cleanup_error is not None
                        else ""
                    )
                ),
            ) from error

    def _latest_office_disk_image(self) -> str:
        images = [
            image
            for image in self._client.list_disk_images()
            if image.labels.get("name", "").startswith("office-")
            and image.status
            and image.status.state in {"Ready", "Succeeded"}
        ]
        if not images:
            raise GatewayError(500, "No Ready Office disk image")
        return max(images, key=lambda image: image.labels["name"]).id

    def _create_sandbox(self, disk_image_id: str) -> Any:
        try:
            from azure.containerapps.sandbox import (
                AutoDeletePolicy,
                AutoSuspendPolicy,
                EgressPolicy,
                LifecyclePolicy,
            )
        except ImportError as error:
            raise GatewayError(500, "ACA Sandboxes SDK is not installed") from error
        request_label = uuid.uuid4().hex
        sandbox = None
        try:
            sandbox = self._client.begin_create_sandbox(
                disk=None,
                disk_id=disk_image_id,
                cpu="2000m",
                memory="4096Mi",
                auto_suspend_seconds=300,
                auto_suspend_mode="Memory",
                egress_policy=EgressPolicy(
                    default_action="Deny",
                    traffic_inspection="Full",
                ),
                labels={
                    "component": "office-gateway",
                    "gateway-request": request_label,
                },
            ).result()
            sandbox.set_lifecycle_policy(
                LifecyclePolicy(
                    auto_suspend=AutoSuspendPolicy(
                        enabled=True,
                        interval=300,
                        mode="Memory",
                    ),
                    auto_delete=AutoDeletePolicy(
                        enabled=True,
                        delete_interval_seconds=3600,
                    ),
                )
            )
            return sandbox
        except Exception:
            cleanup.delete_by_label(
                self._client,
                "gateway-request",
                request_label,
                known_sandbox=sandbox,
            )
            raise

    def _invoke(
        self,
        action: str,
        payload: dict[str, object],
    ) -> dict[str, Any]:
        if self._closed or self._sandbox is None:
            raise GatewayError(410, "Office Sandbox is closed")
        try:
            self._sandbox.ensure_running()
        except Exception as error:
            raise GatewayError(502, "Office Sandbox resume failed") from error
        request_path = f"/tmp/office-request-{uuid.uuid4().hex}.json"
        try:
            self._sandbox.write_file(
                request_path,
                json.dumps(payload, ensure_ascii=False),
            )
            command = " ".join(
                shlex.quote(value)
                for value in (
                    "python3",
                    "/tmp/office_gateway_runner.py",
                    action,
                    request_path,
                )
            )
            result = self._sandbox.exec(command)
        except Exception as error:
            raise GatewayError(502, "Office Sandbox execution failed") from error
        finally:
            try:
                self._sandbox.delete_file(request_path)
            except Exception:
                pass
        try:
            response = json.loads((result.stdout or "").strip())
        except json.JSONDecodeError as error:
            raise GatewayError(
                502,
                "Office Sandbox returned invalid JSON",
            ) from error
        if result.exit_code != 0:
            status = response.get("status")
            message = response.get("error")
            details = {
                key: response[key]
                for key in ("allowed",)
                if key in response
            }
            raise GatewayError(
                int(status) if isinstance(status, int) else 502,
                str(message or "Office Sandbox operation failed"),
                details,
            )
        if not isinstance(response, dict):
            raise GatewayError(502, "Office Sandbox response must be an object")
        return response

    def generate(self, title: str, content: str) -> dict[str, Any]:
        return self._invoke(
            "generate",
            {"title": title, "content": content},
        )

    def convert(
        self,
        job_id: str,
        source: str,
        target: str,
    ) -> dict[str, Any]:
        return self._invoke(
            "convert",
            {"jobId": job_id, "source": source, "target": target},
        )

    def edit(
        self,
        job_id: str,
        operations: list[dict[str, object]],
    ) -> dict[str, Any]:
        return self._invoke(
            "edit",
            {"jobId": job_id, "operations": operations},
        )

    def download(self, path: str) -> tuple[bytes, str]:
        match = self.DOWNLOAD_PATH.fullmatch(path)
        if match is None:
            raise GatewayError(400, "Invalid Office result path")
        job_id, filename = match.groups()
        source_path = f"/work/{job_id}/{filename}"
        snapshot_path = f"/tmp/download-{uuid.uuid4().hex}"
        try:
            self._sandbox.ensure_running()
            copied = self._sandbox.exec(
                f"test -f {shlex.quote(source_path)} && "
                f"test ! -L {shlex.quote(source_path)} && "
                f"timeout --signal=KILL 30s head -c "
                f"{staging.MAX_ARTIFACT_BYTES + 1} -- "
                f"{shlex.quote(source_path)} > {shlex.quote(snapshot_path)}"
            )
            if copied.exit_code != 0:
                raise GatewayError(502, "Office result snapshot failed")
            metadata = self._sandbox.stat_file(snapshot_path)
            if (
                not isinstance(metadata.size, int)
                or metadata.size < 0
                or metadata.size > staging.MAX_ARTIFACT_BYTES
            ):
                raise GatewayError(413, f"Invalid size metadata: {filename}")
            payload = self._sandbox.read_file(snapshot_path)
            if len(payload) != metadata.size:
                raise GatewayError(409, f"Snapshot size changed: {filename}")
        except Exception as error:
            if isinstance(error, GatewayError):
                raise
            raise GatewayError(404, "Office result not found") from error
        finally:
            try:
                self._sandbox.delete_file(snapshot_path)
            except Exception:
                pass
        return (
            payload,
            mimetypes.guess_type(filename)[0]
            or "application/octet-stream",
        )

    def stop(self) -> None:
        if self._closed:
            return
        try:
            if self._sandbox is not None:
                self._sandbox.delete()
        except Exception as error:
            if not cleanup._is_not_found(error):
                raise GatewayError(
                    502,
                    "Office Sandbox delete failed",
                ) from error
        self._closed = True
        if self._sandbox is not None:
            self._sandbox.close()
        self._close_clients()

    def abandon(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._sandbox is not None:
            self._sandbox.close()
        self._close_clients()

    def _close_clients(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
        if self._owns_credential and self._credential is not None:
            close = getattr(self._credential, "close", None)
            if callable(close):
                close()
