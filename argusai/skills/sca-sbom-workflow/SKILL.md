---
name: sca-sbom-workflow
description: Performs offline software composition analysis, local dependency inventory, SBOM generation/diff, and vulnerability-result review with Syft, Trivy, and OSV-Scanner. Use for SCA, SBOM, CycloneDX, SPDX, dependency vulnerabilities, software supply chain, Syft, Trivy, OSV, 软件成分分析, 依赖漏洞, 组件清单, 物料清单, or 软件供应链.
trigger-keywords:
  - "\\b(SCA|SBOM|CycloneDX|SPDX|Syft|Trivy|OSV(-Scanner)?|software composition analysis|dependency vulnerabilities|software supply chain)\\b"
  - "软件成分分析|依赖漏洞|组件清单|物料清单|软件供应链|供应链安全"
trigger-tools:
  - sca_tool_inventory
  - syft_plan_inventory
  - trivy_plan_scan
  - osv_plan_scan
---

## Offline SCA and SBOM Workflow

This workflow only examines local repositories, lockfiles, and SBOM files.
The wrappers do not query registries or vulnerability APIs, update databases,
download CLIs, or install dependencies.

### Start

1. Confirm the authorized local source root and the desired outcome:
   inventory/SBOM, vulnerability review, or comparison between two builds.
2. Create or reuse a Case with `target_type='repository'`; keep generated
   summaries as Case artifacts when they support findings or release evidence.
3. Call `sca_tool_inventory` once.
4. A missing CLI returns `blocked=true`, `do_not_retry=true`, and
   `can_install=false`. Stop retrying and tell the user which host tool is
   required; ArgusAI does not install it.

### Inventory and SBOM with Syft

1. Call `syft_plan_inventory` and verify that the source is a resolved local
   file/directory and that update checks are disabled.
2. Run `syft_run_inventory` (or `syft_generate_sbom`).
3. Review package names, versions, package URLs, types, licenses, and
   truncation. A package entry is inventory evidence, not a vulnerability.
4. Prefer CycloneDX JSON for portable handoff; Syft JSON is useful when
   Syft-specific locations are needed. SPDX JSON is also accepted.

Syft catalogs local content. It does not prove a dependency is reachable,
loaded in production, or vulnerable.

### Offline Vulnerability Review

For Trivy:

1. Call `trivy_plan_scan`; verify `--offline-scan`, `--skip-db-update`, and
   `--skip-java-db-update`.
2. Use `mode='filesystem'` for a repository or `mode='sbom'` for an existing
   SBOM, then run `trivy_run_scan`.
3. Review vulnerability ID, installed/fixed version, severity, target, and
   parser truncation.

For OSV-Scanner:

1. Call `osv_plan_scan`; verify explicit offline mode and a local source/SBOM.
2. Run `osv_run_scan`.
3. Review package ecosystem/version, vulnerability IDs/aliases, source path,
   and parser truncation.

Neither scanner may fall back to online lookup. If the local vulnerability
database is missing, stop and report the blocked coverage. Database setup must
happen outside these tools.

### SBOM Diff

1. Use `sbom_diff` with two local Syft, CycloneDX, or SPDX JSON documents.
2. Review added, removed, and changed packages separately.
3. Treat version, license, and package-URL changes as review leads.
4. Explain identity limitations when an SBOM lacks a package URL; fallback
   matching uses package type/ecosystem plus name.
5. Diff results show composition drift, not whether a release is exploitable.

### Finding Standard

For every dependency finding, include:

- Repository/build/SBOM identity and Case artifact reference
- Package name, ecosystem, installed version, and package URL when available
- Vulnerability identifier and fixed version from the local scanner output
- Local database freshness if known; otherwise mark it unknown
- Reachability/runtime status: unknown, source-only, or independently verified
- Remediation and a reproducible re-scan/diff step

Do not raise a confirmed vulnerability from a package-name keyword or an
unpinned dependency alone.

### Limits

- Syft, Trivy, and OSV-Scanner are external, not bundled, and not managed by
  ArgusAI. CLI syntax/support can vary by installed version and platform.
- Trivy and OSV results are only as complete and current as their already
  available local databases. Offline mode can miss newly published advisories.
- Native/system packages, vendored code, generated lockfiles, monorepos,
  private ecosystems, and language-specific resolution can produce incomplete
  or duplicate inventory.
- The wrappers do not build the project, execute package-manager install
  scripts, contact private registries, calculate runtime reachability, verify
  signatures/provenance, or generate attestations.
- Output and previews are capped. Truncation is partial coverage and must be
  disclosed.
