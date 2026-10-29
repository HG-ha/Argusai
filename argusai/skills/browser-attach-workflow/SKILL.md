---
name: browser-attach-workflow
description: "Choose between the hidden automation browser (watched in the Browser sidebar) and attaching the user's real Chrome/Edge. Launching a debug browser does not attach it; call browser_attach_existing next. Team members share that one attached browser."
trigger-keywords:
  - "\\b(browser_attach|browser_launch_for_attach|cdp attach|devtools protocol)\\b"
  - "\\b(my browser|real chrome|logged[- ]?in session|open tabs)\\b"
  - "已登录|真实浏览器|附着|attach.*浏览器"
---

# Browser Attach Workflow (CDP)

Three different things. Do not mix them.

| User means | What to do | Where the human looks |
|---|---|---|
| 打开网页、截图、点页面，没说要用他正在用的浏览器 | `browser_probe` / `browser_evaluate` on the managed hidden browser. Do not attach. | Sidebar **浏览器**. Tabs appear there. There is no OS window. |
| 看我正在看的页、用我已登录的 Chrome/Edge | Attach once, then `browser_list_tabs` / `browser_open_tab` / `browser_use_tab`. | Same **浏览器** panel, after attach succeeds. `show_window=true` only if they also want the real window on screen. |
| 我来操作 / 接管 / 还你 | `browser_resume_control` gives control back to the agent. The human takes or releases control with the buttons on that panel. This is not a new browser. | The panel that is already showing the session. |

`browser_launch_for_attach` only starts a process with a debug port. It does **not** attach. The next call must be `browser_attach_existing(endpoint=...)`. Until that returns ok, later browser tools stay on the hidden managed browser and the Browser panel stays empty.

Attaching is process-wide. After it succeeds, the main agent and every team member's Chromium tools drive that same browser. Members do not each get a separate real Chrome. A member that never attaches and calls `browser_probe` on its own gets a separate hidden browser; that one also shows up in the Browser panel.

For security analysis, pair attach mode with `case-evidence-workflow`: save decisive page state, request traces, hook hits, or extracted session material as artifacts. Do not rely on the live browser as the only copy of evidence.

## Session model (read this first)

CDP attach is a **session**, not a per-call resource. Think of it like an SSH connection: you establish it once, reuse it for many operations, and only close it when the work is done. Concretely:

- **One attach per chat.** When the user asks for something that needs their browser, check `browser_attach_status`. If already attached, USE IT — do not re-attach. If not attached, attach once.
- **Many tools per attach.** After attaching, every `browser_probe`, `browser_evaluate`, `browser_trace`, `browser_list_tabs`, `browser_open_tab`, `browser_use_tab` call rides on the same connection automatically. You don't refresh, re-attach, or check status between every tool call. Just use them.
- **Detach is rare.** Only call `browser_detach` when (1) the user says they're done with their browser, (2) you're switching to a totally different audit target, or (3) the user asks to release. Detach is not a cleanup step you perform between operations.
- **Transient disconnects heal automatically.** If the CDP websocket drops mid-session, the next tool call silently reconnects. You only see an actual failure when the browser itself is gone (closed by the user). At that point an error message tells you exactly what to do.

The wrong mental model is "attach, do one thing, detach, repeat." That's expensive (each attach is a confirmation card the user has to click) and noisy. The right model is "attach once, work freely, detach when done."

## When to choose each mode

Default to the **managed headless browser** (`browser_probe`, `browser_evaluate`, `browser_trace`) unless one of the following is true:

- The target needs a real user session (SSO, OAuth callback, MFA already completed) that only exists in the user's browser.
- The user explicitly asks you to look at "the page I'm currently viewing" or to act on a specific open tab.
- The flow can't be reproduced cleanly from cold (interactive captcha, hardware key, anti-bot challenge).
- The user wants to confirm a finding inside their own logged-in account.

If none of these apply, do not attach — managed headless is faster, cleaner, and never risks touching the user's real session.

## Detecting current state

Always call `browser_attach_status` before deciding. It returns `attached`, `endpoint`, `alive`, and (when not attached) a `discovered` list of CDP endpoints already running on 9222-9230. If an endpoint is already alive, prefer `browser_attach_existing` over relaunching anything.

## Two ways to get attached

### A. One-click relaunch (`browser_launch_for_attach` + `browser_attach_existing`)

Use when the user has NOT started their browser with `--remote-debugging-port`. This will close their running Edge/Chrome and relaunch it with the debug port plus their existing user-data-dir. Warn them first: any unsaved drafts or in-progress forms will be lost (modern Edge/Chrome will reopen the previous tabs on next launch if "continue where I left off" is enabled in their browser settings).

Required confirmation steps in chat before calling `browser_launch_for_attach`:

1. State which browser/channel will be killed (`msedge`, `chrome`, etc.).
2. Tell the user to save unsaved work.
3. Mention that you will then `connect_over_cdp` and reroute every later browser tool through their real session.

Then call:

- `browser_launch_for_attach(channel="msedge", port=9222, use_my_profile=True, kill_existing=True)`
- On success, `browser_attach_existing(endpoint=<returned endpoint>)` to install the global attached hub.

### B. Connect to a user-managed instance (`browser_attach_existing` alone)

Use when `browser_attach_status` returned a `discovered` endpoint, or when the user says "I already started Edge with `--remote-debugging-port=9222`". Skip `browser_launch_for_attach` entirely — never kill a browser the user managed themselves.

## Operating after attach

Once the global attached hub is installed, every Chromium-family tool routes through the user's real browser:

- `browser_list_tabs()` — enumerate tabs and windows. It returns `windows` (grouped by browser window) and flat `tabs`. Each tab may include `target_id`, `window_id`, `window_index`, `tab_index`, and `ai_owned`.
- `browser_open_tab(url=..., reuse_existing=true, expression=...)` — create or reuse a ArgusAI-owned tab. Use this for NEW navigation unless the user explicitly asked you to use their current tab. The returned tab is marked `ai_owned=true`.
- `browser_use_tab(target_id=... | window_id=..., tab_index=... | url_contains="..." | title_contains="...", expression="...")` — inspect or operate on a specific existing tab. Use it without `navigate_to` when reading the user's live page. Use `navigate_to` only on a ArgusAI-owned tab or when the user explicitly told you to navigate that existing tab.
- `browser_close_owned_tabs(target_id=...)` — close only ArgusAI-owned tabs. Leave `target_id` empty to close all `ai_owned=true` tabs. Popups opened from an owned tab inherit ownership. If tabs were opened via `browser_use_tab(navigate_to=...)` on a user tab, call `browser_mark_tabs_owned(target_ids=[...])` when the user asks to discard them, or `browser_close_owned_tabs(target_ids=[...], force=true)` after explicit user confirmation.
- `browser_probe` / `browser_evaluate` — with no attach, these use the hidden managed browser. After a successful attach they ride the same real browser as `browser_open_tab`. Prefer `browser_open_tab` when you need a ArgusAI-owned tab the user can see in the Browser panel.

## Tab discipline — protect user tabs

The wrong pattern: `browser_use_tab(index=<user tab>, navigate_to=<new url>)` without explicit user consent. That overwrites a tab the user opened.

The right pattern for new navigation:

```
# First call: create or reuse a ArgusAI-owned tab
browser_open_tab(
  url=<first url>,
  reuse_existing=true,
  expression=<extract data>,
)

# Later calls: reuse the ArgusAI-owned tab, either with browser_open_tab
# or with the returned target_id.
browser_open_tab(
  url=<next url>,
  reuse_existing=true,
  expression=<extract>,
)
```

Result: the user sees one ArgusAI tab moving through URLs, and their original tabs are not overwritten. Pair navigation with `wait_until` (`load` / `domcontentloaded` / `networkidle` / `commit`) and `extra_wait_ms` for SPAs that hydrate after load.

After finishing a browser-driven task, clean up only the tabs ArgusAI created:

```text
browser_list_tabs()
browser_close_owned_tabs()
```

Never close a tab unless `browser_list_tabs` marks it `ai_owned=true`. Do not close user tabs just because they look related to the task.

When the user is already on the exact page you want to inspect (e.g. "look at this tab I'm on"), use `browser_use_tab` with `url_contains` / `title_contains` and NO `navigate_to` — you read the live tab without disturbing it.

## Multi-window selection

When `browser_list_tabs` shows more than one window, do not use the legacy global `index` unless there is no better metadata. Pick the intended window and tab from `windows`:

```text
browser_list_tabs()
browser_use_tab(target_id=<tab.target_id>, expression=<read-only extraction>)
# or
browser_use_tab(window_id=<window.window_id>, tab_index=<tab.tab_index>, expression=<read-only extraction>)
```

Use `target_id` when available because it is more stable than positional indexes. Use `window_id + tab_index` when you are reasoning about a particular visible browser window. Use global `index` only as a fallback.

## Efficient extraction — don't dump HTML and re-parse

The common anti-pattern is: call 1 grabs 15KB of HTML, call 2 writes JS to extract the same data after staring at the HTML, call 3 fixes a bug in the JS. That's three calls for what should take one or two. Do this instead:

### One-call extraction (preferred)

When you already know (or can reasonably guess) the page structure, write a single targeted expression on the FIRST call and skip HTML entirely. `browser_use_tab` will auto-disable HTML capture as soon as you provide `expression`.

```javascript
// Good — returns ONLY the structured data needed:
() => [...document.querySelectorAll('.widgetcontainer-lab-link')].map(el => ({
  title: el.querySelector('h3, .lab-title')?.textContent?.trim() ?? '',
  url:   el.getAttribute('href') ?? '',
  // Always check positive class membership — never infer "done" from
  // the absence of "not-done":
  solved: el.classList.contains('is-solved'),
}))
```

### Two-call extraction (when structure is unknown)

If you genuinely don't know the page layout, the first call is a tiny structural probe — NOT a full HTML dump. Either:

- Run a recon expression that returns class/tag counts:
  ```javascript
  () => {
    const c = {};
    document.querySelectorAll('*').forEach(el => {
      const cls = (el.className && typeof el.className === 'string')
        ? el.className.split(/\s+/).filter(Boolean).slice(0,2).join('.')
        : '';
      const k = el.tagName + (cls ? '.' + cls : '');
      c[k] = (c[k] || 0) + 1;
    });
    return Object.entries(c).sort((a,b) => b[1]-a[1]).slice(0, 30);
  }
  ```
- Or grab a SMALL HTML snippet (`capture_html=true, max_html_chars=3000-5000`) just to learn the class names — never 15KB for "recon".

Then make exactly one precise extraction call.

## Common page-state conventions

When asked "what's done / what's left to do", check these class/attribute patterns BEFORE writing speculative JS:

- **PortSwigger Web Security Academy lab list** — each lab card is `.widgetcontainer-lab-link`. Solved labs add `is-solved` to classList; unsolved labs add `is-notsolved`. Trust `classList.contains('is-solved')` as the source of truth; do NOT treat "any class starting with is-" as "completed".
- **Generic completion UI** — `.completed`, `.is-completed`, `.solved`, `.done`, `.checked`, `data-status="done"`, `aria-checked="true"`, or child icons like `.icon-check`, `.fa-check`, `.material-icons:contains('done')`.
- **Progress trackers** — look for `[data-progress]`, `.progress-bar[style*="100%"]`, or `aria-valuenow` attributes.

If the user just asked "which lab should I do next", return a clean ordered list with a clear ✓ / ✗ per item plus the URL — don't paginate raw HTML into the chat.

## Autonomous lab progression — do NOT ask for URLs you can discover

When the user says "继续下一个", "把 SQL 的都做完", "do the next one", "finish them all", or anything else that implies "walk through a series of items I already opened the door to", the rule is: **discover the URLs yourself, don't ask the user**. You are already attached to their logged-in browser, so the per-account solve status is right there.

### PortSwigger Web Security Academy — concrete pattern

The topic index pages have stable, well-known URLs. Use them as your source of truth instead of asking the user for "the next lab URL":

| Topic | Index page (driver tab loads this) |
|---|---|
| SQL injection | `https://portswigger.net/web-security/sql-injection` |
| XSS | `https://portswigger.net/web-security/cross-site-scripting` |
| CSRF | `https://portswigger.net/web-security/csrf` |
| SSRF | `https://portswigger.net/web-security/ssrf` |
| Authentication | `https://portswigger.net/web-security/authentication` |
| Access control | `https://portswigger.net/web-security/access-control` |
| File upload | `https://portswigger.net/web-security/file-upload` |
| OS command injection | `https://portswigger.net/web-security/os-command-injection` |
| Path traversal | `https://portswigger.net/web-security/file-path-traversal` |
| Business logic | `https://portswigger.net/web-security/logic-flaws` |
| (others) | `https://portswigger.net/web-security/<slug>` |

The driver/iteration loop for "finish all the SQL labs" is:

1. `browser_list_tabs()` once to understand the user's windows/tabs. Do not overwrite any user tab for catalog navigation.
2. `browser_open_tab(url='https://portswigger.net/web-security/sql-injection', reuse_existing=true, expression=...)` where `expression` returns the structured lab list, e.g.:
   ```javascript
   () => [...document.querySelectorAll('.widgetcontainer-lab-link')].map(el => ({
     title:    el.querySelector('h3, .lab-title')?.textContent?.trim() ?? '',
     href:     el.getAttribute('href') ?? '',
     solved:   el.classList.contains('is-solved'),
     unsolved: el.classList.contains('is-notsolved'),
   }))
   ```
3. Pick the first item where `solved=false`. Its `href` is a relative path like `/web-security/sql-injection/lab-login-bypass`. Resolve against `https://portswigger.net`.
4. `browser_open_tab(url='https://portswigger.net/web-security/sql-injection/lab-login-bypass', reuse_existing=true)` — the lab description page in the ArgusAI-owned tab.
5. On that page, the `Access the lab` button is a button or link that opens the per-user lab instance. Extract its target:
   ```javascript
   () => document.querySelector('a.button-orange, a[href*="web-security-academy.net"]')?.getAttribute('href') ?? null
   ```
   Then either click it in the ArgusAI-owned tab (`expression: () => document.querySelector('a.button-orange').click()`) and wait, or grab the discovered subdomain and open it with `browser_open_tab(url=<lab instance>, reuse_existing=true)`.
6. Solve the lab using the techniques in the `web-attack-knowledge` skill. When the lab page itself flips to "Solved" (usually a banner appears), loop back to step 2 for the next unsolved lab.
7. Stop only when (a) the user says stop, (b) all labs in the topic are `solved=true`, or (c) the per-turn round ceiling pauses you — in which case tell the user "scanned N/M, currently solving X, send `继续`" and resume.

### When asking IS appropriate

Only ask the user for a URL when:

- The target is **not** a public well-known catalog (their internal staging, a private CTF, a customer's app).
- The user explicitly named a topic that doesn't map to a known index page.
- The user contradicted the inferred next step (e.g., "skip lab 2, do lab 5 instead" — now you do need confirmation of which one).

For PortSwigger Academy, HackTheBox Academy, TryHackMe rooms, or any platform with a stable lab index that the user has clearly signed into, your default is to navigate there yourself.

## Safety rules

- **Never close a tab the user opened.** `browser_use_tab` already enforces this internally — don't write tool calls that try to close it from JS either (no `window.close()`).
- **Never reset cookies / storage / context.** The attached `BrowserHub` ignores `fresh_context=True` and `browser_reset`, but you should not request them anyway.
- **Treat the session as scope-bound.** If the user authorized you to test `example.com`, do not navigate the attached browser to other origins. The user's real session may contain bank / email / corporate tabs.
- **Never read tokens you don't need.** Do not dump every cookie or localStorage entry "just in case." Read only what the task requires and explain in the message what you read and why.
- **Persist only what matters.** If a token/session/header is needed for role comparison, save a named role with `session_vault_save` rather than repeatedly reading browser storage.
- **Confirm every destructive action.** Clicking buttons, submitting forms, making POSTs that change state — call them out explicitly in the user-visible message before triggering them. `browser_use_tab` is gated by the standard confirmation flow; do not try to chain a sequence of write actions in a single batch without giving the user a chance to see what each one does.

## Recovery from transient disconnects

The CDP websocket can drop for many ordinary reasons (page reload, network blip, devtools panel toggle in the user's browser). The hub auto-reconnects to the same endpoint on the NEXT browser tool call, so most disconnects heal themselves invisibly. Do NOT preemptively call `browser_attach_existing` or `browser_launch_for_attach` just because one tool call surfaced a disconnect — simply retry the same operation and the reconnect kicks in.

Only escalate when a tool returns an explicit `CDP attach lost: cannot reconnect to ...` error. At that point:

1. Call `browser_attach_status` to check whether the endpoint is still alive (a quick HTTP probe — no side effects).
2. If `attached=true` and `alive=true`, retry the previous tool call once.
3. If `attached=true` and `alive=false`, ask the user before calling `browser_launch_for_attach`. The user's browser is gone; we can either relaunch or let them open it manually. Do NOT relaunch without asking — `browser_launch_for_attach` runs `taskkill /F /IM msedge.exe` (or equivalent) and a relaunch destroys any work the user has in other windows of the same browser.

`browser_launch_for_attach` is destructive. Only call it when (a) the user explicitly asked to attach and there's no live endpoint, or (b) a previous attach is confirmed dead and the user has agreed to relaunch.

## When you're finished

Call `browser_detach()` when:

- The user says they're done.
- You're switching back to scanning a different target where you don't want the real session.
- The task is complete and there's no follow-up that needs the real browser.

`browser_detach` only disconnects the CDP websocket — the user's browser keeps running and all tabs stay open. After detach, the next `browser_probe` reverts to the managed headless Chromium.

## Quick decision flow

When the user asks something that needs their browser session:

1. **First time only**: `browser_attach_status`. If `attached=true` skip to step 4. If `attached=false`, continue.
2. **If endpoint discovered** (status returns `discovered` list): ask user → `browser_attach_existing(endpoint=<discovered>)`. Done.
3. **If nothing discovered**: warn the user (their current browser will be closed), confirm, then `browser_launch_for_attach(channel=...)`. Default `show_window=false` keeps it off-screen. Then **must** call `browser_attach_existing(endpoint=<returned endpoint>)`. Launch alone does not attach.
4. **Already in session** (status returned `attached=true`): just run the tool you need. No re-attach. No status check before every call.
5. **Iterating URLs**: use `browser_open_tab(url=..., reuse_existing=true)` so navigation happens in a ArgusAI-owned tab. Only use `browser_use_tab(navigate_to=...)` on a user tab when the user explicitly asked you to navigate that tab.
6. **At end** (only when user signals they're done): `browser_detach`. The browser keeps running.

If something errors with `CDP attach lost: cannot reconnect`, then and only then: call `browser_attach_status` to find out the real state and ask the user whether to relaunch. Transient errors that auto-heal show no "attach lost" message — just retry the same call.
