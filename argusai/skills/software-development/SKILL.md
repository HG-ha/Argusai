---
name: software-development
description: Use when the user asks to write code, create a script/app/tool, refactor code, add unit tests, package software, follow AGENTS.md / repo agent rules, or document a program they want built.
inject-priority: 8
trigger-keywords:
  - "\\b(unit test|write code|software development)\\b"
  - "\\bcreate (a )?(script|app|tool)\\b"
  - "\\bbuild (a |an |the )?(script|app|tool|program|project)\\b"
  - "\\brefactor (the )?(code|script|function)\\b"
  - "\\b(AGENTS\\.md|CLAUDE\\.md|CODEOWNERS)\\b"
  - "编写代码|写代码|写个(脚本|程序|工具)|实现功能|修复 bug|单元测试|自动化脚本|生成程序"
  - "仓库规范|开发规范|Agent 规范|遵循规范"
trigger-tools:
  - codebase_scope_set
  - codebase_write_file
  - codebase_patch_file
  - codebase_run_shell_snippet
  - codebase_run_python_snippet
  - sandbox_write_file
  - sandbox_patch_file
  - run_python_script
  - run_shell_script
  - skill_search
  - skill_read
---

# Software Development Workflow

Build useful, reviewable software rather than only describing an implementation.

## Choose the right workspace

| Situation | Workspace | Tools |
|---|---|---|
| Edit / test an **existing user repository** | **Codebase scope** (primary) | `codebase_scope_set` → `codebase_read` / `search` → `codebase_patch_file` / `write_file` → `codebase_run_shell_*` |
| Greenfield script, PoC, throwaway experiment | Session **sandbox** | `sandbox_write_file` / `patch` → `run_python_*` / `run_shell_*` |
| Host-only capability after sandbox failed | Host escalation | `run_host_python_*` (always confirm) |

- For an existing repository: set `codebase_scope_set(root=...)` first. Then edit and run **inside that root** — do not copy the whole project into the sandbox.
- Do not silently modify the user repository without the normal confirmation cards.
- Keep sandbox deliverables (new toys, converters, labs) in a clear subdirectory with README + smoke test.

## Repository agent conventions (AI-era workflow)

When editing an **existing** repository, treat agent-facing project docs as the working contract **before** inventing structure or touching contracts.

1. **Discover governance docs** (scoped search/read): `AGENTS.md` / `agents.md`, `CLAUDE.md` / `GEMINI.md`, `.cursor/rules/`, `.cursorrules`, `.github/copilot-instructions.md`, `docs/` ADR / CODE-STYLE when pointed at, root `README` for product context only.
2. **Discover project skills** under `skills/`, `.cursor/skills/`, `.claude/skills/`, etc. Use `skill_search` / `skill_read` instead of reinventing playbooks.
3. **Obey immutable / public contracts** (exported APIs, tool names, persisted ids, settings shapes, CLI entry points). Breaking changes need an explicit migration path.
4. **Match local norms**: module boundaries, test layout, commit/PR style, safety defaults.
5. **Before changing agent runtime surfaces**, re-read governing docs and source of truth.

### Missing conventions — suggest, don't ignore

After `codebase_scope_set`, cheaply check for governance files. If almost none exist:

1. Briefly tell the user the gap (style drift / no Agent contract).
2. Offer to generate a minimal `AGENTS.md` (optional `.cursor/rules`) from repo signals: languages, test commands (`package.json` scripts / `pytest` / `go test`), directory layout, unsafe areas to avoid, commit style from `git log` if available.
3. Write only after the user confirms (`codebase_write_file`). If they refuse or say "fix the bug first", continue and state you are using generic engineering defaults.
4. Do not block the user's urgent task on generating norms.

## Implementation loop (codebase)

1. Restate deliverable and acceptance criteria when ambiguous.
2. `codebase_scope_set` → tree/search/read + governance scan.
3. Smallest change that works; preserve public APIs unless the user approves a break.
4. Prefer `codebase_patch_file` (exact `old_string` from `codebase_read`) over full rewrites; use `codebase_write_file` for new files.
5. Verify with **`codebase_run_shell_snippet` / `script`** using the repo's own commands (polyglot):
   - JS/TS: `npm test` / `pnpm test` / `yarn test` / `npx …`
   - Go: `go test ./…`
   - Rust: `cargo test`
   - Java: `mvn test` / `gradle test`
   - .NET: `dotnet test`
   - Python convenience: `codebase_run_python_snippet` / `script` (prefers `scope/.venv`)
6. Report changed paths, verification command, result, remaining limits.

Do **not** invent per-language runners. Do not install world-wide SDKs inside the assistant; if `go`/`cargo`/`javac` is missing, report that and ask the user to install or use CI.

## Autonomy and delegation

- Inspect the repository, manifests, tests, and nearby code before asking discoverable details.
- Safe reversible defaults: state briefly and continue; do not ask about helper names / file trivia.
- Parallel readonly investigation: read `multi-agent-orchestration` and `spawn_agent` when useful; parent owns edits and final answer.
- Disk-write and run confirmations remain mandatory; do not ask for a duplicate prose confirmation before the tool card.

## Language and dependency rules

- Prefer standard library / existing project deps. Add packages only when they materially help — via the project's package manager through **confirmed** `codebase_run_shell_*` (e.g. `npm i foo`), not silent host installs.
- Keep secrets out of source; never write `.env` / key material via codebase tools (blocked).
- Networked code: timeouts, bounded retries, clear errors.
- Long jobs: checkpoints on disk; idempotent reruns where practical.

## Testing and deliverables

- Focused tests for parsing, transforms, edge cases — not only happy paths.
- Prefer the repo's test layout and runners.
- Sandbox-only prototypes: open/reveal or export bundle when the user wants the artifact.
- User-repo work: leave changes on disk under scope; summarize paths and how to verify.
