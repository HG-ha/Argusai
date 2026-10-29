---
name: cloud-security-workflow
description: Reviews authorized cloud accounts, container images, and Kubernetes clusters with read-only Trivy, Prowler, and kubectl workflows. Use for AWS, Azure, GCP, Kubernetes, K8s, container image, cloud posture, 云安全, 云账户, 容器镜像, or 集群安全.
trigger-keywords:
  - "\\b(cloud security|cloud posture|AWS|Azure|GCP|Prowler|Kubernetes|K8s|kubectl|container image)\\b"
  - "云安全|云账户|云配置|容器镜像|集群安全|Kubernetes安全"
trigger-tools:
  - cloud_tool_inventory
  - prowler_cloud_plan
  - kubectl_readonly_plan
---

## Cloud Security Workflow

Start from an explicit account, subscription, project, cluster context, region,
and permitted credential profile. Never infer authorization from credentials
already present on the host.

### Preflight

1. Create a Case with the matching `cloud_account`, `cluster`, or
   `container_image` target.
2. Call `cloud_tool_inventory`; use `external_tool_inventory` only when a CLI
   path/version needs diagnosis.
3. Missing tools are blockers. Do not substitute shell commands or install
   unpinned packages.
4. Record credential source by profile/context name only. Never place keys,
   tokens, kubeconfig content, or passwords in tool arguments or evidence.

### Assessment

- Container/repository/filesystem: review `trivy_cloud_plan`, then run
  `trivy_cloud_scan`. Offline mode may have stale or missing databases.
- Cloud posture: review provider, profile, regions, and check allow-list in
  `prowler_cloud_plan`; run `prowler_cloud_scan` only after confirmation.
- Kubernetes: use `kubectl_readonly_plan` before `kubectl_readonly_run`.
  Allowed operations are bounded inventory queries. Secrets, logs, exec,
  attach, port-forward, auth material, and mutation are excluded.
- IaC source must follow `iac-security-workflow`; do not confuse deployed
  posture with source configuration.

### Evidence

Preserve tool/version, account or cluster identity, region/namespace, exact
plan, report artifact, and parser limits. A scanner failure is a lead until the
affected resource and control are independently verified. Deduplicate the same
root cause across regions/resources while retaining affected instances.

### Stop conditions

Stop on ambiguous scope, unexpected account/context, expired credentials,
throttling, mutating command requirements, or output containing secrets.
Never remediate cloud resources automatically.
