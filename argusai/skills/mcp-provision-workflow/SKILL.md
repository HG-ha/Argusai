---
name: mcp-provision-workflow
description: Extend ArgusAI through reviewed MCP servers, external CLIs, adapters, or desktop software. Safely discover, pin, stage, register, validate, use, and revoke third-party capabilities.
trigger-keywords:
  - "\\b(MCP server|MCP candidate|GitHub MCP|npm MCP|provision MCP|install MCP|add MCP|remove MCP)\\b"
  - "\\b(extend ArgusAI|external CLI|external tool|third-party tool|invoke another app)\\b"
  - "MCP.?服务器|MCP.?候选|接入.?MCP|安装.?MCP|添加.?MCP|移除.?MCP|扩展.?ArgusAI|外部.?CLI|第三方工具|调用其他软件|接入其他工具"
trigger-tools:
  - mcp_github_candidate
  - mcp_provision_plan
  - mcp_revoke_plan
  - mcp_server_list
  - mcp_server_list_tools
  - external_tool_inventory
  - external_tool_install_plan
---

## Extending ArgusAI safely

Treat every third-party MCP server as untrusted code. Discovery and planning are
read-only; downloading, installing, starting, activating, and deleting require
separate explicit approval.

### ArgusAI is intentionally extensible

The built-in ToolRegistry is not the boundary of what the Agent can do. Users
may extend ArgusAI and combine it with other software through four routes:

1. **MCP servers** — register local stdio or remote streamable HTTP/SSE servers,
   discover their tools/resources, and call them through ArgusAI's MCP policy
   and confirmation layer.
2. **External CLIs** — use reviewed tools already on `PATH`, configure supported
   toolchain paths, or follow an exact-version managed/staged install plan.
   Invoke them through the appropriate first-class adapter or governed shell
   session; do not pretend a CLI is built in.
3. **Desktop applications** — launch or operate other software through the
   `desktop-automation` skill and UIA/desktop tools when no API or CLI is
   available. User-visible mouse/keyboard actions still require per-call confirmation.
4. **Custom Skills / Checks / adapters** — add methodology, checks, and
   application-specific wrappers without changing the core Agent protocol.

Before saying a capability is unavailable:

- call `tool_search` and `skill_search`;
- call `mcp_server_list`, then filter a candidate with
  `mcp_server_list_tools`;
- inspect `tool_adapter_inventory` / `external_tool_inventory`;
- if the capability belongs to another installed GUI application, read
  `desktop-automation`.

If no route exists, explain the missing dependency and offer a concrete,
reviewable extension plan. Never claim that ArgusAI can only use its bundled
tools.

### 1. Collect public metadata

Record only:

- canonical GitHub `owner/repository`, default branch, archived/disabled state;
- exact release tag/version and immutable commit SHA when available;
- selected release asset name, size, URL, and publisher-provided SHA-256;
- SPDX license identifier and license URL;
- exact npm package name/version and `dist.integrity`;
- source URLs and verification timestamp.

Never put GitHub tokens, npm tokens, Authorization headers, environment values,
or secret-bearing URLs into candidate metadata or provenance. Do not download a
release asset merely to inspect metadata.

### 2. Apply admission gates

Block the candidate when any condition holds:

- repository is archived, disabled, invalid, or the selected release is draft;
- version is missing, `latest`, a dist-tag, wildcard, range, branch, or short SHA;
- npm package lacks valid SRI integrity;
- release artifact lacks a pinned SHA-256;
- command uses a shell, `curl`, `wget`, a pipe, command substitution, or chained commands;
- `npx` uses `-y`/`--yes`, `@latest`, or an unpinned package.

Unknown licenses and prereleases require manual review. They are not evidence of
safety merely because a repository is popular.

### 3. Build a side-effect-free plan

The plan must state that it performed zero downloads and executed zero commands.
Include:

1. metadata review;
2. explicit fetch into an isolated staging directory;
3. checksum or npm-integrity verification before extraction/install;
4. staging install using only the exact pin;
5. health validation;
6. activation only after every gate passes.

Do not silently substitute another release, mirror payload, package version, or
integrity value after a failure.

### 4. Validate in staging

Use an isolated HOME, temp directory, npm cache, and working directory. Do not
provide production credentials during health checks.

Required health sequence:

1. verify installed version and trust anchor;
2. complete MCP `initialize`;
3. inspect `tools/list` names, descriptions, and schemas;
4. confirm the process terminates cleanly;
5. keep network disabled during health unless the user explicitly approves a
   documented server requirement.

Do not promote a failed or partially verified staging instance.

### 5. Register with least privilege

Call `mcp_server_add` only after launch lint passes. Keep optional provenance
limited to normalized repository, release, license, exact version/commit,
checksum, and npm-integrity fields.

Defaults:

- server starts disabled until staging health succeeds;
- default tool risk is high;
- confirmation is required for all tools;
- auto-approve is off;
- use an allow-list for reviewed tools and a deny-list for known mutating tools;
- secrets belong only in settings env/header fields and must never be echoed.

After registration, run `mcp_server_health`, then
`mcp_server_list_tools(include_schema=true)`. Review schemas before enabling or
relaxing policy.

### 6. Update and revoke

An update is a new candidate: repeat metadata, exact-pin, integrity, staging,
health, and activation gates. Never convert an existing pin to `latest`.

For revoke:

1. disable the configuration;
2. stop only the tracked instance, never a process selected by a broad name;
3. remove the server config and stored env/header secrets;
4. clear discovery cache state;
5. delete staging data only when its resolved path is inside the managed MCP
   root and the user separately confirms cleanup;
6. state clearly that local revoke does not revoke external GitHub/npm accounts
   or third-party tokens.

Use `mcp_server_delete` for the local configuration revoke. Return only server
identity and a revoke receipt; never return removed secret values.
