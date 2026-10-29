---
name: mobile-reverse
description: Use for Android APK, iOS IPA, mobile app reverse engineering, Frida/JADX/APKTool planning, mobile API extraction, certificate pinning analysis, and mobile traffic evidence workflows.
---

## Mobile Reverse Workflow

Mobile analysis runs on the **Reverse Agent Runtime**: samples live in `ReverseJob` folders as artifacts; static triage and toolchain status are first-class Agent tools.

Built-in static tools (JADX/APKTool/Frida/capa/yara/Ghidra/objection are not vendored; they live on host PATH, `settings.toolchain.paths`, or the managed `data_root/tools/reverse/` install root):

- `reverse_toolchain_scan` — probe PATH / custom paths for frida/jadx/adb/apktool/capa/yara/ghidra/objection. Each row also reports `can_install` and `docs_url`.
- `reverse_toolchain_install(tool=…, force=false)` — with user confirmation, download a missing CLI into `data_root/tools/reverse/` and persist `settings.toolchain.paths.<tool>`. Supports jadx, frida, apktool, adb, capa, yara, ghidra, zipalign, apksigner, objection. jarsigner / class-dump remain manual — point at `docs_url` instead.
- `reverse_sample_import` — import APK/IPA into a job + optional static triage
- `android_apk_analyze` / `ios_ipa_analyze` / `windows_pe_analyze` / `elf_analyze` — zip/manifest/plist/PE/ELF triage
- `ios_class_dump` — class-dump -H on main Mach-O (macOS + class-dump on PATH only)
- `android_jadx_decompile` — JADX Java/resources decompile → `decompiled/jadx-*` + index artifact
- `android_apktool_decode` — APKTool smali/resources decode → `decompiled/apktool-*` + index artifact
- `android_apktool_build` — APKTool rebuild from job apktool project → `built/apktool-build-*.apk` (auto-validates zip/native-lib layout)
- `android_apk_sign` — zipalign + apksigner/jarsigner → `built/signed-*.apk` (use `use_debug_keystore=true` for lab installs; auto-validates signature + native libs)
- `android_apk_validate` — offline installability checks (compressed `lib/**/*.so`, zipalign, `apksigner verify`) before `adb_install`
- `adb_devices` / `adb_install` / `adb_pull` / `adb_push` / `adb_logcat` — Android device bridge via host `adb` (install/push/pull require confirmation)
- `binary_capa_analyze` / `binary_yara_scan` — PE/ELF/APK capability rules + YARA matches (binary domain)
- `binary_list_functions` / `binary_disassemble` / `binary_decompile` — Ghidra headless analysis for extracted native libraries
- `android_objection_run` — one-shot objection command (device + frida-server required)
- `mobile_frida_recipe_list` / `mobile_frida_recipe_apply` — install pre-baked Frida scripts as `frida_script` artifacts
- `frida_list_devices` / `frida_list_processes` — discovery only (read-only)

Filesystem bridging (no confirmation card needed):

- `reverse_job_open_dir(job_id)` — open the job's `reverse_jobs/<id>/` folder in the OS file manager. Use when the user asks "where is this on disk?".
- `reverse_artifact_reveal(job_id, artifact_id)` — open the OS file manager with the artifact file selected.
- `reverse_artifact_copy_to(job_id, artifact_id, dest_path="")` — copy a single artifact out. Empty `dest_path` lands in the current session sandbox under `imports/`, so a subsequent `export_workspace_bundle` picks it up.
- `reverse_artifact_extract_strings(job_id, artifact_id, kind='url'|…)` — extract URLs/tokens from a job artifact. Decompile index JSON (`decompile-*.json`) scans the whole JADX/APKTool `output_dir`; pass `relative_path` to narrow to one file.
- `reverse_artifact_grep(job_id, pattern=…)` — grep inside `decompiled/` or a decompile index tree without guessing absolute paths (wraps `grep_text_dir` with job containment).
- `reverse_job_link_proxy_flow(job_id, flow_id=…)` — attach a MITM proxy flow as HAR artifact + `evidence_ref` (use after mobile app traffic is captured).
- `reverse_job_export(job_id, dest_dir="", include_decompiled=False, as_zip=True)` — zip the whole job (artifacts/hooks/findings/reproduce). `decompiled/` is opt-in.

Dynamic Frida tools (**require user confirmation every time**):

- `frida_attach` / `frida_spawn` / `frida_run_script` — run hook scripts; logs → `frida_log` artifact
- `frida_session_list` / `frida_session_stop` — manage background CLI sessions

### Workflow

1. `reverse_toolchain_scan(job_id=…)` to see what the host can run — **always do this before any CLI bridge** (`android_jadx_decompile`, `android_apktool_decode`, capa, yara, frida, objection).
2. `reverse_job_create(title=…, target_type='android'|'ios', platform=…)` or `reverse_sample_import(path=…)` (import auto-runs toolchain scan and returns `decompile_guidance`).
3. Run `android_apk_analyze` / `ios_ipa_analyze` on the imported artifact (`job_id` + `artifact_id` or `path=`).
4. **Decompile only when the tool is available:**
   - `jadx` in `toolchain_available` → `android_jadx_decompile(job_id=…, artifact_id=…)` for Java review.
   - `apktool` in `toolchain_available` → `android_apktool_decode(…)` for smali/resources.
   - **Never call `android_jadx_decompile` when jadx is missing** — the tool returns `blocked: true`; use apktool-only path or ask the user to install JADX in settings.
5. **Repack/sign inside the ReverseJob (do not use run_shell_snippet for apktool paths):**
   - Edit resources under `decompiled/apktool-*` (e.g. `res/values/strings.xml`).
   - `android_apktool_build(job_id=…)` → unsigned APK in `built/` (check `validation` in the result).
   - `android_apk_sign(job_id=…, use_debug_keystore=true)` or user keystore params (check `validation` before install).
   - Optional re-check: `android_apk_validate(job_id=…, artifact_id=…)` — catches compressed native libs (`INSTALL_FAILED_INVALID_APK`) and signature issues offline.
   - `adb_install(job_id=…, artifact_id=…)` to sideload the signed APK on a connected device.
   - If decode/build returns `partial: true`, output is still usable — continue; do not retry blindly.
6. For mobile hooks: `mobile_frida_recipe_list(platform='android')` → `mobile_frida_recipe_apply(job_id=…, recipe_id=…)` → confirmed `frida_spawn`/`frida_attach`.
7. Use `reverse_artifact_grep` / `reverse_artifact_extract_strings` / `read_text_file` on decompile output — do not paste full trees.
8. When proxy traffic is relevant: `get_proxy_traffic` → `reverse_job_link_proxy_flow(job_id=…, flow_id=…)` → `har_import_analyze` or `web_request_replay`. External captures: `proxy_import_har(path=…)` / `proxy_import_burp_log(path=…)` (optional `job_id` to attach artifacts).
9. Promote conclusions with `reverse_finding_add`; link proxy/replay evidence via job refs when applicable.
10. Bridge captured API/signing logic to Web replay (`web_request_replay`) when the backend is HTTP.

### Rules

- Do not inline full decompiled trees, APKs, or traffic captures.
- **Do not call CLI bridge tools without checking `reverse_toolchain_scan` / import `toolchain_available` first.**
- **When a CLI is missing and `can_install: true` in the scan result**, prefer the managed install path: ask the user, then call `reverse_toolchain_install(tool=…)` (or have them click **Settings → Reverse toolchain → Install**). Installs persist under `data_root/tools/reverse/` and update `settings.toolchain.paths` so the next GUI launch finds them.
- **Never** use `run_shell_snippet` / `sandbox_pip_install` / system `pip install` / `brew install` to install reverse CLIs. Those bypass `settings.toolchain.paths`, so subsequent launches will not find the binary.
- For `can_install: false` rows (jarsigner / class-dump), point the user at `docs_url` — do not call `reverse_toolchain_install`.
- If JADX is missing, you may either offer `reverse_toolchain_install(tool='jadx')` or fall back to `android_apktool_decode` + static triage. Do not retry JADX without installing first.
- Prefer `artifact_ref` (`rev://…`) and decompile index JSON over pasting logs.
- If runtime hooks are required, document device/profile, app version, and hook script — and wait for confirmed dynamic tools.
- Treat certificate pinning bypass and auth/session material as sensitive evidence.
- Do not claim JADX/APKTool decompilation ran unless `attached.index_artifact_ref` or decompile output exists on disk.

### Evidence

Evidence should connect mobile-side behavior to an impact: leaked secret, weak storage, insecure transport, API auth bypass, signing oracle, or reproducible request.

### Toolchain paths

If PATH detection fails:

1. Read `reverse_toolchain_scan` output — each tool reports `available`, `can_install`, `docs_url`.
2. If `can_install: true`, ask the user, then call `reverse_toolchain_install(tool='jadx')` (etc.) — the install root is `data_root/tools/reverse/` and `settings.toolchain.paths` is updated automatically.
3. If `can_install: false`, link `docs_url` and ask the user to install manually, then click **Settings → Reverse toolchain → Scan PATH**, or fill the path field.

JSON layout for the same paths if the user prefers manual editing:

```json
"toolchain": {
  "paths": {
    "jadx": "",
    "apktool": "",
    "apksigner": "",
    "zipalign": ""
  }
}
```

Use absolute paths only when PATH scan does not find the tool. Do not assume Windows (`C:\\...`) or macOS (`/opt/homebrew/...`) — use whatever path the user's scan returns.

Then re-run `reverse_toolchain_scan`.
