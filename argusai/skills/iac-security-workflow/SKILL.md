---
name: iac-security-workflow
description: Performs offline Infrastructure-as-Code review with Trivy and Checkov over authorized local Terraform, CloudFormation, Kubernetes, Helm, Dockerfile, ARM, Bicep, and CI configuration. Use for IaC, Terraform, Checkov, misconfiguration, 基础设施即代码, 配置审计, or 云配置代码.
trigger-keywords:
  - "\\b(IaC|Infrastructure as Code|Terraform|CloudFormation|Checkov|Trivy config|Helm|Bicep)\\b"
  - "基础设施即代码|云配置代码|Terraform审计|配置审计|IaC安全"
trigger-tools:
  - iac_tool_inventory
  - trivy_iac_plan
  - checkov_scan_plan
---

## IaC Security Workflow

This workflow reads local source only. It does not initialize Terraform,
download providers/modules/checks, render Helm against a cluster, or apply a
configuration.

### Flow

1. Confirm the repository root and create/reuse a repository or `iac_repo`
   Case.
2. Use `iac_tool_inventory`. A missing scanner is a coverage blocker, not a
   reason to run arbitrary package-manager commands.
3. Review exclusions, generated files, vendored modules, and environment
   overlays before scanning.
4. Plan with `trivy_iac_plan` or `checkov_scan_plan`; verify the resolved local
   path, framework, offline flags, and output format.
5. Run `trivy_iac_scan` or `checkov_run_scan` after confirmation.
6. Trace each high-value lead to the exact resource and line range. Review
   variables, defaults, module inputs, overlays, and policy exceptions before
   confirming impact.

### Evidence standard

Store the report as an artifact and record scanner/version, commit or source
identity, path, framework, check ID, resource, line range, effective
environment assumptions, and remediation. Scanner severity is advisory; rate
the finding from the actual exposure and deployment context.

### Boundaries

- Never run `terraform apply/destroy/import`, Helm install/upgrade, kubectl
  mutation, provider login, or state modification.
- State files, plans, variable files, and CI logs may contain secrets. Do not
  paste them into chat or save them as evidence without redaction.
- Static analysis cannot prove that a resource is deployed or reachable.
  Correlate with `cloud-security-workflow` only when deployed access is
  separately authorized.
