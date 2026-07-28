"""ACA Sandboxes Python execution client."""

from __future__ import annotations

import posixpath
import secrets
import shlex
from typing import Any

from agent import execution
from . import cleanup, config

MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024


class PythonSession:
    def __init__(
        self,
        settings: config.Settings,
        *,
        group_client: Any | None = None,
        credential: Any | None = None,
    ) -> None:
        self.settings = settings
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
                    from azure.core.pipeline.transport import RequestsTransport
                    from azure.identity import DefaultAzureCredential
                except ImportError as error:
                    raise execution.ExecutionError(
                        "ACA Sandboxes SDK is not installed"
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
                    transport=RequestsTransport(
                        connection_timeout=30,
                        read_timeout=(
                            settings.execution_timeout_seconds + 60
                        ),
                    ),
                )

            self._sandbox = self._create_sandbox()
            self.identifier = str(self._sandbox.sandbox_id)
            try:
                self._sandbox.mkdir("/mnt/data")
            except Exception as error:
                if "FileAlreadyExists" not in str(error):
                    raise
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
            if isinstance(error, execution.ExecutionError):
                raise
            suffix = (
                "; server-side auto-delete will retry cleanup"
                if cleanup_error is not None
                else ""
            )
            raise execution.ExecutionError(
                f"ACA Sandbox creation failed: {error}{suffix}"
            ) from error

    def _create_sandbox(self) -> Any:
        try:
            from azure.containerapps.sandbox import (
                AutoDeletePolicy,
                AutoSuspendPolicy,
                EgressPolicy,
                LifecyclePolicy,
            )
        except ImportError as error:
            raise execution.ExecutionError(
                "ACA Sandboxes SDK is not installed"
            ) from error

        disk_image_id = (
            self.settings.python_disk_image_id
            or self._latest_python_disk_image()
        )
        if not disk_image_id:
            raise execution.ExecutionError(
                "No Ready python-code-interpreter disk image"
            )
        request_label = secrets.token_hex(8)
        sandbox = None
        try:
            sandbox = self._client.begin_create_sandbox(
                disk=None,
                disk_id=disk_image_id,
                cpu="1000m",
                memory="2048Mi",
                auto_suspend_seconds=1800,
                auto_suspend_mode="Memory",
                egress_policy=EgressPolicy(
                    default_action="Deny",
                    traffic_inspection="Full",
                ),
                labels={
                    "component": "python-gateway",
                    "gateway-request": request_label,
                },
            ).result()
            sandbox.set_lifecycle_policy(
                LifecyclePolicy(
                    auto_suspend=AutoSuspendPolicy(
                        enabled=True,
                        interval=1800,
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

    def _latest_python_disk_image(self) -> str | None:
        images = [
            image
            for image in self._client.list_disk_images()
            if image.labels.get("name", "").startswith(
                "python-code-interpreter-"
            )
            and image.status
            and image.status.state in {"Ready", "Succeeded"}
        ]
        if not images:
            return None
        return max(images, key=lambda image: image.labels["name"]).id

    @staticmethod
    def _safe_name(name: str) -> str:
        if (
            not name
            or posixpath.basename(name) != name
            or name in {".", ".."}
        ):
            raise execution.ExecutionError(f"Unsafe file name: {name}")
        return name

    def execute(
        self,
        code: str,
        *,
        timeout: int = 300,
    ) -> execution.ExecutionResult:
        if self._closed or self._sandbox is None:
            raise execution.ExecutionError("ACA Sandbox is closed")
        script_path = f"/tmp/ai-workspace-{secrets.token_hex(8)}.py"
        try:
            self._sandbox.write_file(script_path, code)
            result = self._sandbox.exec(
                f"timeout --signal=KILL {max(1, int(timeout))}s "
                f"python3 {shlex.quote(script_path)}"
            )
        except Exception as error:
            raise execution.ExecutionError(
                f"ACA Sandbox Python execution failed: {error}"
            ) from error
        finally:
            try:
                self._sandbox.delete_file(script_path)
            except Exception:
                pass
        stderr = str(result.stderr or "")
        if result.exit_code == 124:
            stderr = stderr or f"Execution timed out after {timeout} seconds"
        return execution.ExecutionResult(
            status="Succeeded" if result.exit_code == 0 else "Failed",
            stdout=str(result.stdout or ""),
            stderr=stderr,
            result={"exitCode": result.exit_code},
            raw={"exitCode": result.exit_code},
        )

    def upload(self, name: str, content: bytes) -> dict[str, Any]:
        name = self._safe_name(name)
        if len(content) > 128 * 1024 * 1024:
            raise execution.ExecutionError(f"Upload exceeds 128MB: {name}")
        try:
            self._sandbox.write_file(f"/mnt/data/{name}", content)
        except Exception as error:
            raise execution.ExecutionError(f"Upload failed for {name}: {error}") from error
        return {"name": name, "size": len(content)}

    def list_files(self) -> dict[str, Any]:
        try:
            listing = self._sandbox.list_files("/mnt/data")
        except Exception as error:
            raise execution.ExecutionError(f"List files failed: {error}") from error
        return {
            "value": [
                {
                    "name": entry.name,
                    "size": entry.size,
                    "isDirectory": entry.is_directory,
                }
                for entry in listing.entries
                if not entry.is_directory
            ]
        }

    def download(self, name: str) -> bytes:
        name = self._safe_name(name)
        source_path = f"/mnt/data/{name}"
        snapshot_path = f"/tmp/download-{secrets.token_hex(16)}"
        try:
            copied = self._sandbox.exec(
                f"test -f {shlex.quote(source_path)} && "
                f"test ! -L {shlex.quote(source_path)} && "
                f"timeout --signal=KILL 30s head -c "
                f"{MAX_DOWNLOAD_BYTES + 1} -- "
                f"{shlex.quote(source_path)} > {shlex.quote(snapshot_path)}"
            )
            if copied.exit_code != 0:
                raise execution.ExecutionError(
                    f"Bounded snapshot failed for {name}: {copied.stderr}"
                )
            metadata = self._sandbox.stat_file(snapshot_path)
            if (
                not isinstance(metadata.size, int)
                or metadata.size < 0
                or metadata.size > MAX_DOWNLOAD_BYTES
            ):
                raise execution.ExecutionError(
                    f"Download size is invalid for {name}: {metadata.size}"
                )
            payload = self._sandbox.read_file(snapshot_path)
            if len(payload) != metadata.size:
                raise execution.ExecutionError(
                    f"Download snapshot changed for {name}"
                )
            return payload
        except Exception as error:
            if isinstance(error, execution.ExecutionError):
                raise
            raise execution.ExecutionError(
                f"Download failed for {name}: {error}"
            ) from error
        finally:
            try:
                self._sandbox.delete_file(snapshot_path)
            except Exception:
                pass

    def info(self) -> dict[str, Any]:
        resource = self._sandbox.get()
        return {
            "id": self.identifier,
            "state": resource.state,
            "region": resource.region,
        }

    def delete(self) -> int:
        if self._closed:
            return 0
        try:
            if self._sandbox is not None:
                self._sandbox.delete()
        except Exception as error:
            if not cleanup._is_not_found(error):
                raise execution.ExecutionError(
                    f"ACA Sandbox delete failed: {error}"
                ) from error
        self._closed = True
        if self._sandbox is not None:
            self._sandbox.close()
        self._close_clients()
        return 204

    def _close_clients(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
        if self._owns_credential and self._credential is not None:
            close = getattr(self._credential, "close", None)
            if callable(close):
                close()
