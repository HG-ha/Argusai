---
name: desktop-automation
description: Use when the user asks to observe or control local desktop applications outside the browser, including screenshots, mouse, keyboard, hotkeys, opening QQ/WeChat/music apps, or macOS/Windows desktop automation permissions.
trigger-keywords:
  - "\\b(desktop_observe|desktop_click|desktop_mouse_move|desktop_type|desktop_hotkey|desktop_launch_app)\\b"
  - "\\bdesktop automation\\b|桌面自动化|模拟点击|前台窗口"
  - "\\b(qq|wechat|微信)\\b.*(打开|发送|消息|click)"
trigger-tools:
  - desktop_observe
  - desktop_launch_app
  - desktop_focus_window
  - desktop_uia_find
  - desktop_uia_action
  - desktop_mouse_move
  - desktop_click
  - desktop_type
  - desktop_hotkey
---

# Desktop Automation

ArgusAI has built-in OS desktop tools. Do not tell the user to install pyautogui, uiautomation, or a separate remote-control package.

Timed desktop authorization is retired. Do **not** call `desktop_action_session_start` first. Use mouse/keyboard tools directly.

## Preferred workflow

1. `desktop_observe(mode='windows', screenshot=true)` once for `hwnd` / `monitor_index`.
   - Default width auto-fits the captured surface (one monitor or the stitched virtual desktop, up to 3840). If UI text is still soft: `screenshot_quality=98`, `screenshot_max_width=0` (native), or `screenshot_path='_media/shot.png'` (lossless).
2. Windows: prefer UIA — `desktop_uia_find` then `desktop_uia_action`.
3. For Chromium/QQ/WeChat search hits: use `action='click'` (rect center). Do **not** retry `invoke`/`select` when the UI is unchanged.
4. If `screenshot_looks_black` on hwnd capture, use `monitor=<window.monitor_index>` once — do not re-capture black hwnd images.
5. After launch, stop prior oneshots; do not stack duplicate GUIs.

## Vision fallback — when UIA can't reach a control

Some apps (Electron overlays, games, custom-drawn canvases) expose little or no
UIA tree. `desktop_observe` flags this with `uia_warning` and a `vision_fallback`
directive, and every screenshot carries a `screenshot_grounding` map.

Fallback loop (model-driven absolute positioning):

1. `desktop_observe(hwnd=…, screenshot=true)` — read the attached image.
2. Identify the target visually and read its **pixel position** `(image_x, image_y)` in that image.
3. Click it the safe way — let the tool convert:
   `desktop_click(image_x=…, image_y=…, grounding=<screenshot_grounding from step 1>)`.
   (Equivalent manual form: `desktop_x = origin_x + image_x*scale_x`, then `desktop_click(x, y)`.)
4. Re-observe once to confirm the UI changed before the next click. Do not spray clicks.

Keep using UIA whenever a control has a name/automation_id — vision is the fallback, not the default.

## Mouse

- `desktop_mouse_move(x, y)` or `desktop_mouse_move(hwnd, x, y)` moves the cursor without clicking.
- `desktop_click(x, y)` or `desktop_click(hwnd, x, y)` clicks. Prefer UIA click when a named control exists.

## IM / named recipient — short path

Goal: open the named contact and send with **few** tool calls. Do not observe+vision after every micro-step.

1. `desktop_focus_window(hwnd)`.
2. Open search: UIA `edit` named 搜索 / focus it (or `Ctrl+F` / `Ctrl+K` once). `desktop_type` the contact (or `set_value` if Value works).
3. `desktop_uia_find(control_type='listitem')` → pick the best match → **`desktop_uia_action(action='click', runtime_id=…)`** (not invoke).
4. **Identity (prefer UIA, not vision):** `desktop_uia_find` for button/text whose `name` matches the contact (chat header). Send only if it matches. Use vision **at most once**, unprimed (“quote the chat header verbatim”), and only if UIA has no header name. Ignore the app’s own nickname / signature in the left chrome.
5. Composer: `desktop_click(hwnd, …)` using `window_action_hints.chat_input_relative` (or click left of a UIA 发送 button) → `desktop_type` message → UIA `name_contains='发送'` **`action='click'`** (or Enter).
6. One final observe/vision only if you must confirm send. Trust clipboard `desktop_type` for exact text — do **not** clear/retype because vision misread a digit (`2`↔`4`).

**Long structured payloads** (tables, multi-line lists, code): build the full string and pass it once via `desktop_type` / `set_value`. Do **not** stream the whole table into assistant prose first — that burns tokens and can trip stream-loop guards. A one-line status (“正在粘贴九九表”) is enough.

Never assume the currently open chat is the target. Never hunt the left session list from memory when search exists.

## Hotkeys

- Good: `keys=["enter"]`, `["down"]`, `["escape"]`, `["ctrl","a"]`, or `"ctrl+a"`
- Never invent `["e","n","t","e","r"]`

## macOS permissions

Accessibility for input; Screen Recording for screenshots. Surface the exact missing permission instead of retrying blindly.

## Reporting

Keep status concise: window, action, confirmation. Do not narrate every mouse move.
