---
name: fleet-worker
description: "Fleet worker: works its goal file, never manages lanes."
mainAgent: true
subagent: false
inheritMcp: true
tools: [read_file, edit_file, write_file, run_command, manage_task, search_web]
---
