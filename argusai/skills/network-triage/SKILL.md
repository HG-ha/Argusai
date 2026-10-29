---
name: network-triage
description: Use for system and network security triage, host/service discovery, CIDR assessment, port/service evidence, protocol review, and pcap-oriented analysis.
---

## Network Triage

Use this for host, CIDR, service, and pcap work. Network tasks still use the common case/evidence workflow.

### Workflow

1. Create or select a case with `target_type='host'|'cidr'|'service'|'pcap'`.
2. Run `domain_inventory(domain='network')` to confirm available and planned capabilities.
3. Confirm explicit scope before network activity. If scope is missing, ask for it.
4. Use scoped tools first: `network_discover_hosts`, `network_profile_host`, `network_assess_exposure`, `ping_host`, `dns_lookup`, `trace_route`, `nmap_scan`, `whois_lookup`.
5. Prefer `network_profile_host` for structured port/service/OS hints; use `nmap_scan(service_detection=true)` when nmap detail or CPE output is needed.
6. Use `weak_credential_audit_plan` to decide whether weak/default credential checks are appropriate. Only move to `weak_credential_check` after explicit authorization.
7. Save meaningful command output or host profiles as artifacts with `artifact_add`.
8. Add evidence only when the output supports a security conclusion.

### Rules

- Respect scope. Do not scan outside the authorized host/CIDR.
- Prefer low-risk discovery before service detection or aggressive scans.
- Treat banners and open ports as leads until validated.
- Use `network_assess_exposure` for non-destructive risk findings before proposing any verification step.
- Do not run `pentest_verify_finding` unless the user has explicitly authorized the target and provided the required confirmation phrase.
- Do not run broad password spraying, credential stuffing, or brute force. `weak_credential_check` is limited to small, low-rate, authorized checks for `http_basic`, `ftp`, and `redis`; SSH/RDP/SMB should be handled as a manual lockout-safe audit plan.
- For pcap or large logs, store files as artifacts and read small slices.

### Evidence

Network evidence should include target, scope, tool, command arguments, timestamp, output excerpt, and why it matters.
