---
name: fleet-probe
description: "Fleet probe lane: read-only look at one target for DECIDE, reports once with probe_report, never acts. REFUSED host: fleet launch --role probe --cli agy fails closed."
mainAgent: true
subagent: false
inheritMcp: false
tools: [read_file, search_web]
---
