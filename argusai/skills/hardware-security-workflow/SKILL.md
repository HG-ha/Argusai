---
name: hardware-security-workflow
description: Plans identification-only hardware assessment and offline UART/flash evidence analysis without erase, program, write, unlock, voltage changes, or automatic elevation. Use for UART, JTAG, SWD, OpenOCD, CHIPSEC, sigrok, flash dump, 硬件安全, 串口, 闪存, or 逻辑分析仪.
trigger-keywords:
  - "\\b(hardware security|UART|JTAG|SWD|OpenOCD|CHIPSEC|sigrok|flash dump)\\b"
  - "硬件安全|串口|闪存|逻辑分析仪|调试接口|芯片安全"
trigger-tools:
  - hardware_preflight
  - hardware_uart_parse
  - hardware_readonly_plan
---

## Hardware Security Workflow

Hardware access can damage devices and bypass trust boundaries. Require an
owned/authorized test unit, board revision, power/voltage limits, connection
diagram, backup, and recovery procedure before any live interaction.

### Offline-first

1. Create a board/flash/UART Case and hash every supplied dump/capture.
2. Use `hardware_uart_parse` for an existing UART capture and
   `hardware_flash_diff` for existing flash images.
3. Use `hardware_sigrok_plan` only to plan decoding of an existing capture.
4. Keep secret material redacted and treat boot logs, offsets, and device IDs
   as sensitive evidence.

### Identification-only planning

1. Call `hardware_preflight`.
2. Use `hardware_readonly_plan` for allowlisted identification operations.
3. Call `hardware_action_check` before proposing adjacent operations. A
   blocked verdict must not be bypassed with a shell tool.
4. CHIPSEC requires manual installation/elevation and must be limited to
   reviewed read-only modules. OpenOCD is limited to identification/read-only
   planning; sigrok is limited to existing captures.

### Prohibited actions

Never erase, program, flash, unlock, fuse, write memory/registers, change
voltage/clock, auto-detect electrical pinouts, elevate automatically, or run
vendor scripts from an untrusted image. Do not claim a live hardware test when
only a plan or offline capture was reviewed.

### Evidence

Record board/tool/adapter versions, wiring and voltage supplied by the user,
capture/dump hash, address/offset or timestamp, exact parser/plan, observed
behavior, and safety/coverage limits. Confirm impact through non-destructive
evidence and propose remediation without applying it.
