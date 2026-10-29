---
name: binary-triage
description: Use for EXE, DLL, ELF, process dump, malware triage, native reverse engineering, decompiling/disassembling a function, understanding code, unpacking installers / setup.exe (Inno Setup), and PATCHING / modifying a binary (NOP a check, change a jump, redirect a call). Covers strings/imports/capa/yara, Ghidra headless analysis, installer unpacking, and the decompile→understand→patch→verify loop.
---

## Native Binary Reverse Engineering

Two tracks share the case/evidence model: **triage** (what is this binary?) and the
**reverse loop** (read a function → understand it → modify it → verify). Binaries,
dumps, strings, imports, disassembly, pseudocode, and patched copies are all artifacts.

### Toolchain

`binary_disassemble` / `binary_decompile` / `binary_list_functions` need
**Ghidra headless** (`analyzeHeadless` on PATH or
`settings.toolchain.paths.ghidra`). If `reverse_toolchain_scan` shows it missing,
offer `reverse_toolchain_install(tool='ghidra')` after the user confirms. Ghidra
is a large download and needs JDK 21+ on PATH. Its first call analyzes the whole
binary; later calls reuse the cached project. capa / yara / frida / innoextract
are also managed installs.

**Never install reverse tools on the user's host** (no `winget` / `choco` / `pip install`
/ `brew` via `run_shell_snippet`). Use `reverse_toolchain_install(tool=...)` — it installs
under `data_root/tools/reverse/` and persists in settings, or the tool is already bundled.
If a tool genuinely has no managed install on this platform (for example innoextract on macOS),
surface the exact command for the **user** to run; do not run it on their machine yourself.

### Installers (setup.exe / Inno Setup)

Many Windows "setup.exe" are **Inno Setup** installers. Do NOT hand-roll Python on the raw
overlay. Use `installer_unpack(path=...)` (innoextract) — `list_only=True` to triage, then
extract. The bundled app `.exe`/`.dll` inside is the real reverse target → feed it to
`binary_list_functions` / `binary_decompile`.

- **Encrypted Inno Setup installers**: innoextract needs the install `password`. Without it,
  the overlay is **cryptographically unextractable — not an argusai limitation**. Do NOT try
  to "decrypt" it in Python. The product-key / license check almost always lives in the
  **installed main exe**, not the installer: install in a lab VM and **Frida-hook the
  validation** (`frida_*`). Pivot to dynamic instead of rabbit-holing on the overlay.
- **NSIS / InstallShield / MSI**: innoextract is Inno-only. For NSIS use 7-Zip; for MSI use
  the matching extractor. If the tool is missing, ask the user to install it (see rule above).

### Triage workflow

1. `reverse_sample_import(path=...)` to create a job and auto-run static analysis (PE/ELF).
2. `windows_pe_analyze` (PE) or `elf_analyze` (ELF/SO); optional `binary_capa_analyze`,
   `binary_yara_scan` for capabilities and rule matches.
3. Attach findings; keep raw material referenced, not pasted.

### Static vs dynamic analysis

- **Static** (`binary_list_functions` / `binary_disassemble` / `binary_decompile`):
  Ghidra imports and analyzes the sample once, then reuses the cached project for later
  function lookups. Prefer this before executing an unknown binary.
- **Dynamic** (`frida_attach` / `frida_spawn` / `frida_run_script`): use only when
  static evidence is insufficient and the user authorized the target environment.
  These tools can attach to or spawn a process and always require confirmation.

### Decompile → Understand → Patch → Verify

1. **Map the code.** `binary_list_functions` lists functions (name / address / size).
   Pick the one of interest (e.g. a license
   check, a version string comparison).
2. **Read it.** `binary_decompile(function=... | address=0x...)` for C-like pseudocode,
   and/or `binary_disassemble(...)` for exact instructions + offsets. Cross-check both —
   the pseudocode tells you intent, the disassembly tells you the exact bytes to change.
   All three tools use **Ghidra**. If it is missing, offer
   `reverse_toolchain_install(tool='ghidra')` (large download, needs a JDK 21+ on PATH).
   Ghidra's first call analyzes the whole binary, then later functions reuse the cached
   project.
3. **Plan the patch.** Decide the precise change: NOP a conditional jump, flip `jz`↔`jnz`
   (`74`↔`75`, `0f84`↔`0f85`), force a return, or redirect a call. Note the **file offset
   or VA** and the **original bytes**.
4. **Patch.** `binary_patch` writes a NEW copy (never in place):
   - target with `offset` (file offset) or `rva`/`va` (PE, mapped via the section table)
   - replacement via `bytes` (hex), `nop`+`count`, or `assemble` (needs keystone)
   - ALWAYS pass `expect` (hex of the original bytes) per patch — the batch aborts if it
     does not match, so you never patch the wrong location
   - it requires confirmation (`confirm_label=PATCH_BINARY`) — destructive
5. **Verify.** Re-run `binary_disassemble` / `binary_decompile` plus
   `windows_pe_analyze` or `elf_analyze` on the patched artifact to confirm the change
   landed and the file still parses. Only
   then suggest the user run it.

### Rules

- Never execute unknown binaries unless the user explicitly authorized the environment.
  Prefer static triage before dynamic behavior.
- `binary_patch` is destructive and needs explicit confirmation. It NEVER overwrites the
  input — it produces `<name>.patched.<ext>` (or a job `patched` artifact).
- Always assert original bytes (`expect`) before patching; verify after patching.
- Patching software you do not own / are not authorized to modify can be illegal and may
  violate licenses. Patch only binaries the user owns or is authorized to analyze.
- Do not paste large disassembly / pseudocode / string dumps into chat — attach as artifacts.
- Record tool versions and sample hashes with evidence.

### Evidence

Include sample identity, artifact refs (disasm / decompiled / patched), the exact code
location (function + offset/VA), original→patched bytes, confidence, and the next step.
