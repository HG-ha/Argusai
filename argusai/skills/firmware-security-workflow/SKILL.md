---
name: firmware-security-workflow
description: Performs offline, non-executing firmware triage over an authorized image or already extracted root filesystem. Use for firmware, rootfs, binwalk, embedded Linux, IoT image, 固件分析, 物联网固件, 文件系统提取, or 嵌入式安全.
trigger-keywords:
  - "\\b(firmware|rootfs|binwalk|embedded Linux|IoT image|firmware filesystem)\\b"
  - "固件分析|物联网固件|嵌入式安全|文件系统提取|固件镜像"
trigger-tools:
  - iot_firmware_preflight
  - firmware_path_validate
  - firmware_analysis_plan
---

## Firmware Security Workflow

Analyze a copy of the image or an already extracted filesystem. Do not boot,
emulate, flash, mount untrusted filesystems with elevated privileges, or run
firmware binaries.

### Flow

1. Create a firmware Case; record product/model/version/source and image hash.
2. Call `iot_firmware_preflight` and `firmware_path_validate`.
3. Use `firmware_analysis_plan` to review supported offline stages and blocked
   dependencies.
4. If an extracted rootfs is available, use `firmware_rootfs_grep` with narrow
   patterns and `firmware_service_map` to locate service/configuration leads.
5. Use existing YARA/capa/decompile tools only on selected files and preserve
   their paths/hashes.
6. Validate findings against startup scripts, effective permissions,
   configuration defaults, update path, and remotely reachable services.

### Review points

Look for embedded credentials/keys, unsafe update verification, writable
startup paths, exposed debug services, insecure defaults, weak crypto use, and
outdated components. Strings and filenames are leads; confirm semantics and
reachability before reporting.

### Boundaries and evidence

Extraction may need external tools or a Linux/WSL/container environment; a
missing tool is a blocker, not permission to install or execute arbitrary
helpers. Do not expose Binwalk execution that runs extractors automatically.

Preserve image hash, extraction provenance, exact internal path, file hash,
relevant bounded snippet, service/update chain, exploit prerequisites, and
tool/version. Redact private keys and credentials; prove presence without
returning secret values.
