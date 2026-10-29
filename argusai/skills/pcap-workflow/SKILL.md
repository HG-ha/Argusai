---
name: pcap-workflow
description: Performs safe offline packet-capture triage with TShark summaries, display filters, and clear-text HTTP metadata extraction. Use for PCAP/PCAPNG files, packet captures, Wireshark/TShark analysis, network forensics, 流量包, 抓包分析, 数据包分析, 网络取证, or 离线流量分析.
trigger-keywords:
  - "\\b(pcap|pcapng|packet capture|traffic capture|wireshark|tshark|network forensics)\\b"
  - "流量包|抓包分析|数据包分析|网络取证|离线流量|流量分析"
trigger-tools:
  - pcap_tool_inventory
  - pcap_summary_plan
  - pcap_summary
---

## PCAP Offline Workflow

This workflow analyzes an existing local capture. It never starts live capture,
opens sockets, downloads software, or sends captured data to a remote service.

### Start

1. Confirm the capture is authorized and identify the question to answer.
2. Prefer a Case with `target_type='pcap'` and `domain='network'`; attach the
   original capture as a `pcap` artifact when a durable evidence chain matters.
3. Call `pcap_tool_inventory` or `tshark_status` once.
4. If TShark is missing, stop CLI attempts. The result is
   `blocked=true`, `do_not_retry=true`, and `can_install=false`; ask the user to
   install Wireshark/TShark manually.

### Triage

1. Use `pcap_summary_plan` to review the exact local-only argv and packet cap.
2. Run `pcap_summary` for protocol counts, endpoints, time bounds, byte totals,
   and a bounded packet preview.
3. Create focused follow-up questions from that evidence: unusual peers,
   unexpected ports, protocol concentration, or a narrow time window.
4. Use `pcap_filter_plan` before `pcap_filter`. Prefer precise display filters
   and a small `max_packets`; broaden only when the result is empty for a
   defensible reason.

Examples of useful filter intent (not findings by themselves):

- DNS requests or a specific queried name
- A host pair, port, TCP reset, or retransmission
- TLS handshake metadata
- One protocol in a bounded time range

### HTTP Metadata

1. Use `pcap_http_extract_plan`, then `pcap_http_extract`.
2. Review request methods, hosts, URIs, response codes, content types, and user
   agents.
3. Treat extracted URLs and headers as potentially sensitive. Store decisive
   output as a Case artifact; avoid pasting large traffic excerpts into chat.
4. Correlate a conclusion to frame numbers and capture timestamps.

### Evidence Standard

For each lead, record:

- Capture artifact reference and filter used
- Relevant frame numbers and timestamps
- Source/destination and protocol
- What is observed versus inferred
- Coverage limit (`max_packets`, truncation, encrypted traffic, missing keys)

Do not label an endpoint malicious from protocol or port alone.

### Limits

- TShark is external and not bundled or managed by ArgusAI.
- This workflow does not capture live interfaces and does not require Npcap for
  reading ordinary files, though the local Wireshark package may have
  platform-specific dependencies.
- TLS and QUIC application payloads are opaque unless the user's own TShark
  installation is separately configured with valid decryption keys. These
  tools do not request, discover, or manage keys.
- HTTP extraction covers traffic TShark decodes as HTTP; it is not complete for
  encrypted HTTP/2, HTTP/3, malformed captures, unsupported link types, or
  truncated packets.
- Command output and parsed rows are intentionally capped. A capped result is
  partial coverage, not proof that no relevant traffic exists.
