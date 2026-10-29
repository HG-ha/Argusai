---
name: settings-guide
description: Help users configure AI endpoints and application settings. Use when the user asks about setting up AI, adding endpoints, configuring models, API keys, proxy settings, user habits/profile preferences, or troubleshooting AI connection issues.
trigger-keywords:
  - "\\b(settings|endpoint|api key|model config|ai connection)\\b"
  - "端点|模型配置|API.?密钥|连接测试|设置页"
---

## Settings Guide

### AI Endpoints
Add endpoints in Settings — two types available:
- **OpenAI Compatible**: works with OpenAI, DeepSeek, Ollama, vLLM, Qwen, and any `/v1/chat/completions` endpoint.
- **Anthropic**: for Anthropic-compatible endpoints.

User fills: base URL, API key, model name. Each endpoint has a priority — higher tried first.

### Connection Issues
- Check base URL reachable, API key valid, model name exists, no firewall/proxy block.

### Proxy Config
Default `127.0.0.1:8866`. Change port if in use, host to `0.0.0.0` for all interfaces.

### Browser
- **Browser binary** card: pick which Chromium-family browser the managed headless instance should use. Empty channel + empty executable_path = auto-detect (system Chrome / Edge / Brave first, Playwright bundle as fallback). Use the "Rescan" button to refresh detection and "Download Playwright Chromium" if no system browser is installed.
- **Reuse my running browser (CDP attach)** card: connect the assistant to the user's real running browser so it can see open tabs and reuse the logged-in session. Two paths:
  - "Relaunch & attach": closes the current Edge/Chrome (warn the user to save unsaved work) and restarts it with `--remote-debugging-port` + the user's default profile.
  - "Connect to endpoint": when the user already started the browser themselves with `--remote-debugging-port=9222`, just enter the endpoint URL.
  - Use "Detach" when finished. The browser keeps running; only the CDP websocket is released.
- For the full workflow, decision rules, and safety guardrails see `browser-attach-workflow`.

### Reverse toolchain (JADX / Frida / APKTool / capa / …)
- **Settings → Reverse toolchain paths**: optional absolute paths when tools are not on PATH. **Scan PATH** detects installed binaries; **Install** downloads supported tools into `data_root/tools/reverse/` and saves paths (needs network + user click).
- With user confirmation, the Agent may call **`reverse_toolchain_install(tool=…)`** for the same managed installs (frida, jadx, apktool, adb, capa, yara, ghidra, innoextract, zipalign, apksigner, objection). **class-dump / jarsigner** remain manual-only — point to docs.
- When a reverse tool returns `blocked: true` / `toolchain_required` and `can_install: true`, prefer managed install over ad-hoc shell/pip. **Do not** `run_shell_snippet` or `sandbox_pip_install` for reverse CLIs.
- Runtime exceptions are different: **managed Python/Node/webcrack/WABT** install from Settings → Sandbox runtimes (or `runtime_install`) and follow `session-sandbox-workflow` / the WASM workflow; bundled **sqlmap** follows the SQLi workflow — none of them belong to the reverse toolchain list above.

### User Habits Profile
- The user habits profile lives in `settings.json` under `user_habits`; it is separate from the knowledge base and from session todos.
- Non-empty profile fields are injected into the assistant system prompt each turn through `profile_prompt_summary`, so the AI should follow them without calling a read tool first.
- The AI should automatically summarize durable user preferences into update suggestions. Examples: preferred response language/style, desired report format, risk tolerance, audit focus, workflows to avoid, or repeated corrections such as "do not do X".
- Do not store one-off task facts, credentials, secrets, target-specific vulnerability details, or temporary instructions in the profile.
- The Settings page section is collapsed by default. Users can edit it manually there.
- Tool governance:
  - `user_profile_read` is read-only.
  - `user_profile_suggest_update` proposes a patch and goes through a confirmation card; use it proactively when a durable preference is observed.
  - `user_profile_apply_update` writes settings and must go through the confirmation framework; the runtime injects `confirm_label=APPLY_USER_PROFILE` only after approval.
  - Do not ask the model to provide `confirm_label` manually. Do not treat global auto-allow as permission to bypass profile-write confirmation.

### Persistence
Settings live in the user-level `settings.json` managed by `PathManager`; they survive restarts. Call `get_settings` before changing.
