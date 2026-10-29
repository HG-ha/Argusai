---
name: mobile-dast-workflow
description: Plans observation-first dynamic testing of authorized Android or iOS applications with an existing Appium server, test device, proxy, and bounded logs. Use for Appium, mobile DAST, Android dynamic testing, iOS dynamic testing, 移动动态测试, 手机应用测试, or 真机流量分析.
trigger-keywords:
  - "\\b(Appium|mobile DAST|Android dynamic|iOS dynamic|device traffic|mobile runtime test)\\b"
  - "移动动态测试|手机应用测试|真机测试|模拟器测试|移动端流量"
trigger-tools:
  - mobile_dast_tool_inventory
  - appium_status
  - mobile_dast_plan
---

## Mobile DAST Workflow

Use only a designated test device/emulator and authorized application build.
The implemented tools provide readiness checks and plans; they do not create
Appium sessions or drive application actions automatically.

### Preflight and plan

1. Record platform, build/package identity, device identifier, test account,
   reset/rollback procedure, and proxy scope in a Case.
2. Call `mobile_dast_tool_inventory`.
3. Use `appium_status` without probing for local inventory; set
   `probe_server=true` only for an approved server. Remote Appium requires
   explicit opt-in.
4. Build `mobile_dast_plan`. Capabilities are allowlisted and force
   `appium:noReset=true`.
5. Verify that the plan contains no install, uninstall, reset, permission
   grant, device unlock, executable override, or destructive UI action.

### Observe

Drive the approved test flow manually. Correlate timestamps across proxy
traffic, bounded device logs, screenshots, and server-side effects. Use the
existing mobile reverse workflow for static package analysis and Frida only
when separately authorized.

### Evidence

Record app version/hash, platform/device state, account role, exact user
action, request/response or log artifact, and reproducible impact. A modelled
plan or UI message is not evidence that an action occurred.

### Safety

Never automate purchases, submissions, credential changes, data deletion,
permission grants, passcode entry, device reset, or application installation.
Stop when a flow reaches real user data, production side effects, certificate
pinning bypass requirements, or an unexpected device/server identity.
