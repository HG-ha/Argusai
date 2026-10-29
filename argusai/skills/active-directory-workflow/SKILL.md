---
name: active-directory-workflow
description: Reviews authorized Active Directory environments through offline SharpHound/PingCastle report import and bounded Kerberos/no-password Certipy AD CS inventory. Use for Active Directory, AD CS, Certipy, BloodHound, SharpHound, PingCastle, LDAP, 域控, 活动目录, 域安全, or 证书服务.
trigger-keywords:
  - "\\b(Active Directory|AD CS|Certipy|BloodHound|SharpHound|PingCastle|domain controller|LDAP)\\b"
  - "活动目录|域控|域安全|域环境|证书服务|域权限"
trigger-tools:
  - ad_tool_inventory
  - ad_sharphound_import
  - ad_certipy_find_plan
---

## Active Directory Workflow

Prefer existing authorized reports. Live directory collection is sensitive and
must have explicit domain/controller scope, a designated account, time window,
and rate limits.

### Offline-first

1. Create an `ad_domain` or `dc` Case and store report identity/hash.
2. Use `ad_sharphound_import` for JSON/ZIP summaries and
   `ad_pingcastle_import` for XML risk summaries.
3. These importers return aggregate counts and omit identities. Review the
   original report in an approved local analysis environment when a path must
   be validated.
4. Treat graph edges and rule scores as leads. Confirm the effective
   permission, object state, and reachable path before reporting.

### Bounded AD CS inventory

1. Call `ad_tool_inventory`.
2. Review `ad_certipy_find_plan`; it allows only `certipy find` with Kerberos
   cache/no-password authentication and JSON output.
3. Run `ad_certipy_find_run` only after confirmation and only against the
   approved domain/controller.
4. Validate certificate-template findings against enrollment rights, EKUs,
   manager approval, authorized signatures, CA policy, and issuance path.

### Prohibited actions

Do not perform password spraying, credential dumping, relay, coercion, ticket
forging, DCSync, lateral movement, persistence, certificate enrollment, or
directory modification. Do not accept passwords/tokens as tool arguments.

### Evidence

Preserve report/tool version, collection time and scope, domain/controller,
object IDs in a redacted artifact, the exact permission/path, prerequisites,
and impact. Separate stale, theoretical, and verified paths.
