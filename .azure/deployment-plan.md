# Azure Deployment Plan

## 1. Status

Deployed and Verified

## 2. Deployment Context

- Mode: Redeploy and validate existing Azure CLI-based lab resources
- Subscription: `ME-MngEnvMCAP757124-junwoojeong-1` (`51531604-2337-4c05-bc05-3c3d4ff154e5`)
- Region: `koreacentral`
- Resource Group: `rg-ai-workspace-sandbox-lab`
- Recipe: Azure CLI (`scripts/python-lab.sh`, `scripts/office-lab.sh`, `scripts/agent-lab.sh`)

## 3. Scope

- Reuse and update the existing Python Code Interpreter pool.
- Rebuild the Office image and update the existing Custom Container pool.
- Verify RBAC, quota, egress, session isolation, files, conversion, editing, monitoring, and cleanup behavior.
- Verify the user-facing gateways against live Azure resources.
- Do not delete the Resource Group or existing resources.

## 4. Cost and Safety

- Office image build and ready session incur Azure charges.
- Existing resource names and region are retained.
- No resource deletion, purge, public exposure, or scale-up is planned.

## 5. Execution Steps

- [x] Run live subscription, quota, RBAC, and resource preflight.
- [x] Run repository validation.
- [x] Redeploy and validate the Python pool.
- [x] Rebuild and redeploy the Office Custom Container pool.
- [x] Run agent and gateway live validation.
- [x] Verify resource provisioning state, endpoints, metrics, and RBAC.
- [x] Record validation evidence.

## 6. Rollback

- Stop any temporary sessions created by validation.
- Keep the last successfully deployed pool and image if an update fails.
- Do not delete the Resource Group without separate confirmation.

## 7. Validation Proof

### Requirements

- Classification: PoC/lab validation
- Scale: Small, single-region
- Budget: Cost-optimized; reuse existing resources and keep one Office ready session
- Compliance: Existing tenant governance policies apply; no production data or credentials are placed in sessions

### Existing Resources

- Python pool: `ai-workspace-python-sbx` (`Succeeded`)
- Office pool: `ai-workspace-office-sbx` (`Succeeded`)
- Container Apps Environment: `env-ai-workspace-sandbox`
- ACR: `aiws5153160423374c05bc05`
- Image pull identity: `id-ai-workspace-office-acr-pull`
- Log Analytics: `log-ai-workspace-sandbox`

### Policy Constraints

- Azure Security Baseline
- MCAPSGov audit, deny, deploy, and modify initiatives
- `Block Azure RM Resource Creation`
- Existing resources are reused to avoid new ARM resource creation; any policy denial stops the deployment without deletion or workaround

### Validation Commands Completed

- `bash scripts/check-prereqs.sh`
- `bash scripts/validate-repo.sh`
- `ensure_role_assignment` existing-role and missing-role branch checks
- Azure subscription and Resource Group lookup
- Existing session pool and resource inventory
- Azure Policy assignment lookup
- Regional quota lookup
- Session Executor and AcrPull live role verification
- Bicep/template validation: Not applicable; this repository uses existing Azure CLI deployment scripts

### 2026-07-28 Validation Proof

- `bash scripts/check-prereqs.sh`: Azure CLI 2.86.0, target subscription and login valid
- `bash scripts/validate-repo.sh`: Python sources parsed and 76 offline tests passed
- `bash -n scripts/*.sh`: all deployment and validation scripts parsed
- `python3 -m compileall -q ...`: Python packages and Office server compiled
- Local Docker smoke: not run because Docker Desktop daemon is unavailable; ACR cloud build and live Custom Container session validation are required deployment gates
- Resource Group `rg-ai-workspace-sandbox-lab`: `Succeeded`, Korea Central
- Python and Office session pools: `Succeeded`
- Office pool: `EgressDisabled`, ready sessions 1, max sessions 5, node count 1, Startup and Liveness probes
- Regional quota: Session pools 48 available, managed environments 48 available
- Live RBAC: Session Executor on both pools and AcrPull on the ACR
- ACR admin user: disabled
- Session diagnostic categories: console, lifecycle, and pool event logs available
- Azure MCP resource lookup used a different Entra context and returned 403; deployment validation uses the authenticated Azure CLI context recorded in this plan

### Results

- Azure CLI `2.86.0`
- Confirmed subscription and `koreacentral`
- Repository validation passed
- 76 offline tests passed
- Both existing session pools are `Succeeded`
- Regional quota: managed environments 48 available, session pools 48 available
- Session Executor exists on both pools; AcrPull exists on ACR
- Existing role reuse and missing role creation branches passed
- Python live validation passed
- Python timeout and memory limit probes passed
- Office image `aiws5153160423374c05bc05.azurecr.io/office-sandbox:20260727224541` deployed and validated
- Live `gpt-5.6-terra` orchestration passed with Entra authentication
- Python and Office user Gateway REST flows passed
- Temporary validation sessions were stopped
- Python session list confirmed zero remaining sessions after trap cleanup
- No destructive action was performed

### 2026-07-28 Deployment and Live Verification

- ACR cloud build run `dee`: succeeded
- Deployed image: `aiws5153160423374c05bc05.azurecr.io/office-sandbox:20260727224541`
- Image digest: `sha256:719165f1725599562221736110d300c40cdaf2e3aa8d61dd6eb535e5d840ed2b`
- Office pool update: `Succeeded`; `EgressDisabled`, ready sessions 1, max sessions 5, node count 1
- Release marker: `/health.release == 20260727224541`
- Office live validation: four-format generation, allowlisted conversion, DOCX/PPTX/XLSX editing, hash, logs, metrics, and session stop passed
- Python live validation: execution, upload/download, egress block, session isolation, error correction, and cleanup passed
- Actual LLM validation: `gpt-5.6-terra` with Entra authentication passed
- Python user Gateway: natural language + CSV, download, approve, and delete passed
- Office user Gateway: create, edit, PPTX download, approve, and delete passed
- Cleanup regression: Python delete-after-read and Office stop verification fixed and reproduced
- Final active sessions: Python 0, Office 0
- Final RBAC: Session Executor on both pools; AcrPull on the ACR
- Final repository validation: 76 tests passed
- Git commit and push: intentionally not performed

### Deployed Endpoints

- Python pool: `https://koreacentral.dynamicsessions.io/subscriptions/51531604-2337-4c05-bc05-3c3d4ff154e5/resourceGroups/rg-ai-workspace-sandbox-lab/sessionPools/ai-workspace-python-sbx`
- Office pool: `https://ai-workspace-office-sbx.whitewater-a8d9a9cf.koreacentral.azurecontainerapps.io`
