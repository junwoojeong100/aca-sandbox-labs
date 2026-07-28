#!/usr/bin/env python3
"""Delete ACA Sandboxes lab data-plane objects and the SandboxGroup."""

from __future__ import annotations

import os
import subprocess
import sys

try:
    from azure.containerapps.sandbox import (
        SandboxGroupClient,
        SandboxGroupManagementClient,
        endpoint_for_region,
    )
    from azure.core.exceptions import ResourceNotFoundError
    from azure.identity import AzureCliCredential
except ImportError as error:
    raise SystemExit(
        "Run with .work/aca-sandboxes/venv/bin/python or use "
        "scripts/aca-sandboxes/quickstart.sh first."
    ) from error


def run_az(*args: str) -> str:
    try:
        result = subprocess.run(
            ["az", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"az {' '.join(args)} timed out after 180 seconds"
        ) from error
    except subprocess.CalledProcessError as error:
        message = error.stderr.strip() or error.stdout.strip() or str(error)
        raise RuntimeError(
            f"az {' '.join(args)} failed: {message}"
        ) from error
    return result.stdout.strip()


def main() -> None:
    if os.environ.get("CONFIRM_DELETE") != "yes":
        raise RuntimeError(
            "Set CONFIRM_DELETE=yes to delete ACA Sandboxes lab resources."
        )

    subscription_id = os.environ.get("SUBSCRIPTION_ID") or run_az(
        "account", "show", "--query", "id", "--output", "tsv"
    )
    resource_group = os.environ.get(
        "RESOURCE_GROUP",
        "rg-ai-workspace-aca-sandboxes-lab",
    )
    location = os.environ.get("LOCATION", "koreacentral")
    sandbox_group = os.environ.get(
        "SANDBOX_GROUP_NAME",
        "ai-workspace-sandboxes",
    )

    credential = AzureCliCredential()
    management = SandboxGroupManagementClient(
        credential,
        subscription_id=subscription_id,
        resource_group=resource_group,
    )
    try:
        try:
            management.get_group(sandbox_group)
        except ResourceNotFoundError:
            print(f"SandboxGroup not found: {sandbox_group}")
            return

        client = SandboxGroupClient(
            endpoint_for_region(location),
            credential,
            subscription_id=subscription_id,
            resource_group=resource_group,
            sandbox_group=sandbox_group,
        )
        try:
            for resource in list(client.list_sandboxes()):
                sandbox = client.get_sandbox_client(resource.id)
                try:
                    sandbox.begin_delete().result()
                    print(f"Deleted Sandbox: {resource.id}")
                finally:
                    sandbox.close()

            for snapshot in list(client.list_snapshots()):
                client.begin_delete_snapshot(snapshot.id).result()
                print(f"Deleted snapshot: {snapshot.id}")

            for disk_image in list(client.list_disk_images()):
                client.begin_delete_disk_image(disk_image.id).result()
                print(f"Deleted disk image: {disk_image.id}")
        finally:
            client.close()

        management.begin_delete_group(sandbox_group).result()
        print(f"Deleted SandboxGroup: {sandbox_group}")
    finally:
        management.close()
        credential.close()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
