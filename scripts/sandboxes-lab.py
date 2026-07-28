#!/usr/bin/env python3
"""Live validation for Azure Container Apps Sandboxes Python execution."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from azure.containerapps.sandbox import (
        AutoDeletePolicy,
        AutoSuspendPolicy,
        EgressPolicy,
        LifecyclePolicy,
        RegistryCredentials,
        SandboxGroupClient,
        SandboxGroupManagementClient,
        endpoint_for_region,
    )
    from azure.core.exceptions import ResourceNotFoundError
    from azure.identity import AzureCliCredential
except ImportError as error:
    raise SystemExit(
        "Install the preview SDK first: pip3 install azure-containerapps-sandbox"
    ) from error


def run_az(*args: str) -> str:
    try:
        result = subprocess.run(
            ["az", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=1200,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"az {' '.join(args)} timed out after 1200 seconds"
        ) from error
    except subprocess.CalledProcessError as error:
        message = error.stderr.strip() or error.stdout.strip() or str(error)
        raise RuntimeError(
            f"az {' '.join(args)} failed: {message}"
        ) from error
    return result.stdout.strip()


def acr_credentials(acr_name: str) -> RegistryCredentials:
    token = json.loads(
        run_az(
            "acr",
            "login",
            "--name",
            acr_name,
            "--expose-token",
            "--output",
            "json",
        )
    )
    return RegistryCredentials(
        username=token["username"],
        token=token["refreshToken"],
    )


def list_disk_images_with_retry(
    client: SandboxGroupClient,
) -> list[object]:
    for attempt in range(1, 19):
        try:
            return list(client.list_disk_images())
        except Exception as error:
            status_code = getattr(error, "status_code", None)
            response = getattr(error, "response", None)
            status_code = status_code or getattr(
                response,
                "status_code",
                None,
            )
            message = str(error).lower()
            authorization_pending = status_code == 403 or any(
                marker in message
                for marker in (
                    "authorizationfailed",
                    "forbidden",
                    "does not have authorization",
                )
            )
            if not authorization_pending or attempt == 18:
                raise
            print(
                "SandboxGroup Data Owner 역할 전파 대기 "
                f"({attempt}/18)...",
                flush=True,
            )
            time.sleep(10)
    raise RuntimeError("SandboxGroup role propagation retry exhausted")


def caller_object_id() -> str:
    token = run_az(
        "account",
        "get-access-token",
        "--resource",
        "https://management.azure.com",
        "--query",
        "accessToken",
        "--output",
        "tsv",
    )
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    object_id = claims.get("oid")
    if not isinstance(object_id, str) or not object_id:
        raise RuntimeError("The Azure access token does not contain an object ID")
    return object_id


def ensure_data_owner(subscription_id: str, resource_group: str) -> None:
    role = "Container Apps SandboxGroup Data Owner"
    scope = f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    object_id = os.environ.get("CALLER_OBJECT_ID") or caller_object_id()
    principal_type = os.environ.get("CALLER_PRINCIPAL_TYPE", "User")
    existing = run_az(
        "role",
        "assignment",
        "list",
        "--assignee-object-id",
        object_id,
        "--scope",
        scope,
        "--query",
        f"[?roleDefinitionName=='{role}'].id | [0]",
        "--output",
        "tsv",
    )
    if existing:
        return
    run_az(
        "role",
        "assignment",
        "create",
        "--assignee-object-id",
        object_id,
        "--assignee-principal-type",
        principal_type,
        "--role",
        role,
        "--scope",
        scope,
        "--output",
        "none",
    )


def ensure_acr(
    acr_name: str,
    resource_group: str,
    location: str,
) -> None:
    run_az(
        "provider",
        "register",
        "--namespace",
        "Microsoft.ContainerRegistry",
        "--wait",
    )
    existing = run_az(
        "acr",
        "list",
        "--resource-group",
        resource_group,
        "--query",
        f"[?name=='{acr_name}'].id | [0]",
        "--output",
        "tsv",
    )
    if existing:
        return

    run_az(
        "acr",
        "create",
        "--name",
        acr_name,
        "--resource-group",
        resource_group,
        "--location",
        location,
        "--sku",
        "Basic",
        "--admin-enabled",
        "false",
        "--tags",
        "purpose=ai-workspace-sandboxes",
        "--output",
        "none",
    )


def acr_tag_exists(
    acr_name: str,
    repository: str,
    image_tag: str,
) -> bool:
    repositories = run_az(
        "acr",
        "repository",
        "list",
        "--name",
        acr_name,
        "--query",
        f"[?@=='{repository}'] | [0]",
        "--output",
        "tsv",
    )
    if repositories != repository:
        return False
    tag = run_az(
        "acr",
        "repository",
        "show-tags",
        "--name",
        acr_name,
        "--repository",
        repository,
        "--query",
        f"[?@=='{image_tag}'] | [0]",
        "--output",
        "tsv",
    )
    return tag == image_tag


def ensure_python_disk_image(
    client: SandboxGroupClient,
    *,
    acr_name: str,
    resource_group: str,
    location: str,
    repository: str,
    repo_root: Path,
    evidence: dict[str, object],
) -> object:
    python_images = [
        image
        for image in list_disk_images_with_retry(client)
        if image.labels.get("name", "").startswith(
            "python-code-interpreter-"
        )
        and image.status
        and image.status.state in {"Ready", "Succeeded"}
    ]
    existing = max(
        python_images,
        key=lambda image: image.labels["name"],
        default=None,
    )
    if existing is not None:
        evidence["source_image"] = str(existing.image.base)
        evidence["disk_image_auth"] = "existing disk image"
        return existing

    ensure_acr(acr_name, resource_group, location)
    requested_tag = os.environ.get("PYTHON_IMAGE_TAG")
    image_tag = requested_tag or datetime.now(timezone.utc).strftime(
        "%Y%m%d%H%M%S"
    )
    if not requested_tag or not acr_tag_exists(
        acr_name,
        repository,
        requested_tag,
    ):
        print(
            f"Building {repository}:{image_tag} in ACR...",
            flush=True,
        )
        run_az(
            "acr",
            "build",
            "--registry",
            acr_name,
            "--image",
            f"{repository}:{image_tag}",
            "--file",
            str(repo_root / "python-sandbox" / "Dockerfile"),
            str(repo_root / "python-sandbox"),
            "--output",
            "none",
        )

    image = f"{acr_name}.azurecr.io/{repository}:{image_tag}"
    disk_image_name = f"python-code-interpreter-{image_tag}"
    print(f"Registering disk image from {image}...", flush=True)
    disk_image = client.begin_create_disk_image(
        image,
        name=disk_image_name,
        entrypoint=["/bin/sh", "-c"],
        cmd=["sleep infinity"],
        registry_credentials=acr_credentials(acr_name),
        polling_timeout=1200,
        polling_interval=10,
    ).result()
    evidence["source_image"] = image
    evidence["disk_image_auth"] = "temporary ACR refresh token"
    return disk_image


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--provision-only",
        action="store_true",
        help="Create the Resource Group, RBAC, and SandboxGroup, then exit.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    subscription_id = os.environ.get("SUBSCRIPTION_ID") or run_az(
        "account", "show", "--query", "id", "--output", "tsv"
    )
    resource_group = os.environ.get(
        "RESOURCE_GROUP", "rg-ai-workspace-sandbox-lab"
    )
    location = os.environ.get("LOCATION", "koreacentral")
    sandbox_group = os.environ.get(
        "SANDBOX_GROUP_NAME", "ai-workspace-sandboxes"
    )
    acr_name = os.environ.get(
        "ACR_NAME", f"aiwssbx{subscription_id.replace('-', '')[:20]}"
    )
    repository = os.environ.get(
        "PYTHON_IMAGE_REPOSITORY",
        "python-code-interpreter",
    )
    work_dir = Path(
        os.environ.get("SANDBOX_WORK_DIR", ".work/sandboxes-live")
    )
    work_dir.mkdir(parents=True, exist_ok=True)

    run_az("account", "set", "--subscription", subscription_id)
    run_az(
        "provider",
        "register",
        "--namespace",
        "Microsoft.App",
        "--wait",
    )
    run_az(
        "group",
        "create",
        "--name",
        resource_group,
        "--location",
        location,
        "--tags",
        "purpose=ai-workspace-sandbox-lab",
        "--output",
        "none",
    )
    ensure_data_owner(subscription_id, resource_group)

    credential = AzureCliCredential()
    management = SandboxGroupManagementClient(
        credential,
        subscription_id=subscription_id,
        resource_group=resource_group,
    )
    try:
        try:
            group = management.get_group(sandbox_group)
        except ResourceNotFoundError:
            group = management.begin_create_group(
                sandbox_group,
                location,
                tags={"purpose": "ai-workspace-sandboxes-live-validation"},
            ).result()
        if group.properties.get("provisioningState") != "Succeeded":
            raise RuntimeError(
                f"SandboxGroup provisioning failed: {group.properties}"
            )
    finally:
        management.close()

    client = SandboxGroupClient(
        endpoint_for_region(location),
        credential,
        subscription_id=subscription_id,
        resource_group=resource_group,
        sandbox_group=sandbox_group,
    )
    active = []
    evidence: dict[str, object] = {
        "sandbox_group_id": group.id,
        "sandbox_group_state": group.properties["provisioningState"],
    }
    try:
        if args.provision_only:
            evidence["result"] = "provisioned"
            print("ACA SandboxGroup provisioning passed.", flush=True)
            return

        policy = EgressPolicy(
            default_action="Deny",
            traffic_inspection="Full",
        )
        requested_disk_image_id = os.environ.get(
            "PYTHON_SANDBOX_DISK_ID"
        )
        if requested_disk_image_id:
            source = {
                "disk": None,
                "disk_id": requested_disk_image_id,
            }
            evidence["disk_image_id"] = requested_disk_image_id
            evidence["disk_image_name"] = "environment override"
            evidence["disk_image_auth"] = "not applicable"
        else:
            python_image = ensure_python_disk_image(
                client,
                acr_name=acr_name,
                resource_group=resource_group,
                location=location,
                repository=repository,
                repo_root=repo_root,
                evidence=evidence,
            )
            source = {"disk": None, "disk_id": python_image.id}
            evidence["disk_image_id"] = python_image.id
            evidence["disk_image_name"] = python_image.labels["name"]

        print("Creating primary Python sandbox...", flush=True)
        primary = client.begin_create_sandbox(
            **source,
            cpu="1000m",
            memory="2048Mi",
            auto_suspend_seconds=1800,
            auto_suspend_mode="Memory",
            egress_policy=policy,
            labels={"lab": "python-live-validation"},
        ).result()
        active.append(primary)
        primary.set_lifecycle_policy(
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
        evidence["primary_id"] = primary.sandbox_id
        evidence["primary_state"] = primary.get().state

        version = primary.exec(
            "python3 -c 'import platform; print(platform.python_version())'"
        )
        if version.exit_code != 0:
            raise RuntimeError(version.stderr)
        evidence["python_version"] = version.stdout.strip()
        libraries = primary.exec(
            "python3 -c 'import matplotlib,numpy,pandas;"
            'print("analysis-libraries=ok")\''
        )
        if (
            libraries.exit_code != 0
            or "analysis-libraries=ok" not in libraries.stdout
        ):
            raise RuntimeError(
                f"Analysis libraries missing: {libraries.stderr}"
            )

        primary.write_file(
            "/work/sales.csv",
            "month,amount\n2026-01,200\n2026-02,240\n2026-03,240\n",
        )
        primary.write_file(
            "/work/analyze.py",
            (
                "import csv,json\n"
                "with open('/work/sales.csv',newline='') as f:\n"
                " rows=list(csv.DictReader(f))\n"
                "summary={r['month']:float(r['amount']) for r in rows}\n"
                "with open('/work/summary.json','w') as f:\n"
                " json.dump(summary,f,sort_keys=True)\n"
                "print(sum(summary.values()))\n"
            ),
        )
        analysis = primary.exec("python3 /work/analyze.py")
        if analysis.exit_code != 0 or "680.0" not in analysis.stdout:
            raise RuntimeError(
                f"Analysis failed: {analysis.stdout}\n{analysis.stderr}"
            )
        summary_bytes = primary.read_file("/work/summary.json")
        summary = json.loads(summary_bytes)
        expected = {
            "2026-01": 200.0,
            "2026-02": 240.0,
            "2026-03": 240.0,
        }
        if summary != expected:
            raise RuntimeError(f"Unexpected summary: {summary}")
        evidence["summary"] = summary
        evidence["summary_sha256"] = hashlib.sha256(
            summary_bytes
        ).hexdigest()
        evidence["files"] = [
            entry.name for entry in primary.list_files("/work").entries
        ]

        egress = primary.exec(
            "python3 -c \"import urllib.request;"
            "\ntry:"
            "\n urllib.request.urlopen('https://example.com',timeout=5);"
            " print('UNEXPECTED_EGRESS_ALLOWED')"
            "\nexcept Exception as e:"
            "\n print('EGRESS_BLOCKED',type(e).__name__)\""
        )
        if (
            "EGRESS_BLOCKED" not in egress.stdout
            or "UNEXPECTED_EGRESS_ALLOWED" in egress.stdout
        ):
            raise RuntimeError(f"Egress was not blocked: {egress.stdout}")
        evidence["egress"] = egress.stdout.strip()

        print("Creating isolation sandbox...", flush=True)
        secondary = client.begin_create_sandbox(
            **source,
            egress_policy=policy,
            labels={"lab": "python-live-isolation"},
        ).result()
        active.append(secondary)
        secondary.set_lifecycle_policy(
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
        isolation = secondary.exec(
            "test ! -e /work/sales.csv && echo ISOLATED"
        )
        if isolation.exit_code != 0 or "ISOLATED" not in isolation.stdout:
            raise RuntimeError("Sandbox file isolation failed")
        evidence["isolation"] = "verified"
        secondary.delete()
        active.remove(secondary)
        secondary.close()

        before = primary.read_file("/work/summary.json")
        primary.begin_stop().result()
        stopped = primary.get().state
        primary.begin_resume().result()
        resumed = primary.get().state
        after = primary.read_file("/work/summary.json")
        if stopped != "Stopped" or resumed != "Running" or before != after:
            raise RuntimeError("Suspend/resume did not preserve state")
        evidence["suspend_resume"] = {
            "stopped": stopped,
            "resumed": resumed,
            "state_preserved": True,
        }
        evidence["result"] = "passed"
        print("Python Sandbox live validation passed.", flush=True)
    finally:
        for sandbox in list(active):
            try:
                sandbox.delete()
            except Exception as error:
                print(
                    f"WARNING: Sandbox cleanup will rely on auto-delete: {error}",
                    file=sys.stderr,
                )
            finally:
                sandbox.close()
        client.close()
        (work_dir / "python-validation.json").write_text(
            json.dumps(evidence, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
