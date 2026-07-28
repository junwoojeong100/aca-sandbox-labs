"""ACA Sandbox cleanup and orphan-reaper helpers."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any

from agent import execution
from . import config


def _is_not_found(error: Exception) -> bool:
    text = str(error)
    return "NotFound" in text or "not found" in text.lower()


def delete_by_label(
    group_client: Any,
    key: str,
    value: str,
    *,
    known_sandbox: Any | None = None,
    attempts: int = 12,
    delay_seconds: int = 5,
) -> None:
    last_error: Exception | None = None
    known_delete_error: Exception | None = None
    if known_sandbox is not None:
        try:
            for attempt in range(attempts):
                try:
                    known_sandbox.delete()
                    return
                except Exception as error:
                    if _is_not_found(error):
                        return
                    last_error = error
                    known_delete_error = error
                    if attempt + 1 < attempts:
                        time.sleep(delay_seconds)
        finally:
            known_sandbox.close()

    for attempt in range(attempts):
        try:
            resources = [
                resource
                for resource in group_client.list_sandboxes()
                if getattr(resource, "labels", {}).get(key) == value
            ]
            if known_delete_error is None:
                last_error = None
        except Exception as error:
            last_error = error
            resources = []
        if resources:
            all_deleted = True
            for resource in resources:
                sandbox = group_client.get_sandbox_client(resource.id)
                try:
                    sandbox.delete()
                except Exception as error:
                    if not _is_not_found(error):
                        last_error = error
                        all_deleted = False
                finally:
                    sandbox.close()
            if all_deleted:
                return
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    if last_error is not None:
        raise execution.ExecutionError(
            f"Partial ACA Sandbox cleanup failed: {last_error}"
        ) from last_error


def cleanup_gateway_sandboxes(
    settings: config.Settings,
    component: str,
    *,
    group_client: Any | None = None,
    credential: Any | None = None,
    exclude_ids: set[str] | None = None,
) -> int:
    owns_credential = credential is None
    owns_client = group_client is None
    if group_client is None:
        try:
            from azure.containerapps.sandbox import (
                SandboxGroupClient,
                endpoint_for_region,
            )
            from azure.identity import DefaultAzureCredential
        except ImportError as error:
            raise execution.ExecutionError(
                "ACA Sandboxes SDK is not installed"
            ) from error
        if credential is None:
            credential = DefaultAzureCredential(
                exclude_interactive_browser_credential=True
            )
        group_client = SandboxGroupClient(
            endpoint_for_region(settings.location),
            credential,
            subscription_id=settings.resolved_subscription_id(),
            resource_group=settings.resource_group,
            sandbox_group=settings.sandbox_group_name,
        )
    deleted = 0
    cleanup_errors: list[str] = []
    now = time.time()
    excluded = exclude_ids or set()
    try:
        for resource in list(group_client.list_sandboxes()):
            if getattr(resource, "labels", {}).get("component") != component:
                continue
            if resource.id in excluded:
                continue
            if getattr(resource, "state", None) not in {
                "Stopped",
                "Suspended",
                "Failed",
            }:
                continue
            created_timestamp = _created_timestamp(
                getattr(resource, "created_at", None)
            )
            if created_timestamp is None or now - created_timestamp < 3600:
                continue
            sandbox = group_client.get_sandbox_client(resource.id)
            try:
                sandbox.delete()
                deleted += 1
            except Exception as error:
                if not _is_not_found(error):
                    cleanup_errors.append(f"{resource.id}: {error}")
            finally:
                sandbox.close()
    finally:
        if owns_client:
            group_client.close()
        if owns_credential and credential is not None:
            close = getattr(credential, "close", None)
            if callable(close):
                close()
    if cleanup_errors:
        raise execution.ExecutionError(
            "ACA Sandbox orphan cleanup failed: "
            + "; ".join(cleanup_errors)
        )
    return deleted


def start_cleanup_loop(
    settings: config.Settings,
    component: str,
    *,
    interval_seconds: int = 300,
    exclude_ids_provider: Any | None = None,
) -> threading.Thread:
    def cleanup_loop() -> None:
        while True:
            time.sleep(interval_seconds)
            try:
                excluded = (
                    set(exclude_ids_provider())
                    if callable(exclude_ids_provider)
                    else set()
                )
                cleanup_gateway_sandboxes(
                    settings,
                    component,
                    exclude_ids=excluded,
                )
            except Exception as error:
                print(
                    json.dumps(
                        {
                            "level": "warning",
                            "message": f"{component} orphan cleanup failed",
                            "error": str(error),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    thread = threading.Thread(
        target=cleanup_loop,
        name=f"{component}-sandbox-reaper",
        daemon=True,
    )
    thread.start()
    return thread


def _created_timestamp(value: Any) -> float | None:
    if isinstance(value, datetime):
        normalized = (
            value
            if value.tzinfo is not None
            else value.replace(tzinfo=timezone.utc)
        )
        return normalized.timestamp()
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None
