---
name: wireless-security-workflow
description: Performs passive wireless inventory and offline 802.11 capture triage without deauthentication, injection, cracking, association, or live capture. Use for Wi-Fi, WLAN, 802.11, radiotap, wireless PCAP, 无线安全, 无线网卡, 无线流量, or Wi-Fi审计.
trigger-keywords:
  - "\\b(Wi-?Fi|WLAN|802\\.11|radiotap|wireless security|wireless PCAP)\\b"
  - "无线安全|无线网卡|无线流量|Wi-Fi审计|无线抓包"
trigger-tools:
  - wireless_preflight
  - wireless_wlan_inventory
  - wireless_pcap_plan
---

## Wireless Security Workflow

This workflow is passive. It inventories host-visible WLAN metadata or plans
analysis of an existing authorized capture.

### Flow

1. Record the authorized SSID/BSSID/site/time window and whether the source is
   host inventory or an existing capture.
2. Call `wireless_preflight`.
3. Use `wireless_wlan_inventory` only after confirmation. It runs an
   allowlisted platform inventory command and must not connect/disconnect or
   change profiles.
4. Use the parsing tools for already captured command output when possible.
5. For PCAP/PCAPNG, call `wireless_pcap_plan`, then follow `pcap-workflow` for
   bounded offline analysis.
6. Use `wireless_action_check` before proposing any adjacent action. A blocked
   result is a hard policy boundary.

### Review points

Assess encryption/authentication mode, open or legacy networks, unexpected
BSSIDs/channels, management-frame protection metadata when present, and
association anomalies visible in the capture. SSID names, signal strength, or
vendor guesses alone are not findings.

### Prohibited actions

Never expose deauthentication, injection, handshake capture, cracking,
credential testing, evil-twin setup, jamming, association, live capture, or
profile/key extraction. Aircrack-ng presence does not authorize those actions.

### Evidence

Preserve sanitized SSID/BSSID, source/time, platform/tool version, capture
artifact/filter/frame numbers, observed security mode, and coverage limits.
Treat hidden SSIDs and MAC addresses as potentially sensitive.
