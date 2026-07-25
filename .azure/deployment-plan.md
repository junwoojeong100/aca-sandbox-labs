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
- `bash scripts/smoke-office-local.sh`
- `ensure_role_assignment` existing-role and missing-role branch checks
- Azure subscription and Resource Group lookup
- Existing session pool and resource inventory
- Azure Policy assignment lookup
- Regional quota lookup
- Session Executor and AcrPull live role verification
- Bicep/template validation: Not applicable; this repository uses existing Azure CLI deployment scripts

### Results

- Azure CLI `2.86.0`
- Confirmed subscription and `koreacentral`
- Repository validation passed
- 70 offline tests passed
- Local Office container build and smoke test passed
- Both existing session pools are `Succeeded`
- Regional quota: managed environments 48 available, session pools 48 available
- Session Executor exists on both pools; AcrPull exists on ACR
- Existing role reuse and missing role creation branches passed
- Python live validation passed
- Python timeout and memory limit probes passed
- Office image `aiws5153160423374c05bc05.azurecr.io/office-sandbox:20260725120350` deployed and validated
- Live `gpt-5.6-terra` orchestration passed with Entra authentication
- Python and Office user Gateway REST flows passed
- Temporary validation sessions were stopped
- Python session list confirmed zero remaining sessions after trap cleanup
- No destructive action was performed

### Deployed Endpoints

- Python pool: `https://koreacentral.dynamicsessions.io/subscriptions/51531604-2337-4c05-bc05-3c3d4ff154e5/resourceGroups/rg-ai-workspace-sandbox-lab/sessionPools/ai-workspace-python-sbx`
- Office pool: `https://ai-workspace-office-sbx.whitewater-a8d9a9cf.koreacentral.azurecontainerapps.io`
