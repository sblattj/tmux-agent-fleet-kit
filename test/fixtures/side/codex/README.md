Codex rollout fixtures for test/test_side_usage.py.

Checked on real codex rollouts (cli_version 0.153.4 through 0.166.0, 2026-11-23): a helper (subagent)
session's first line is `{"type":"session_meta","payload":{...}}` and names its parent twice:
`payload.parent_thread_id` and `payload.source.subagent.thread_spawn.parent_thread_id`
(also `payload.thread_source == "subagent"`). Top-level sessions have neither and `source` is a string
such as "exec" or "cli". lib/fleet_side_usage.py reads either parent field.

main:     11111111-aaaa-7000-8000-000000000001, last token_count total 1000 in / 100 out
helper A: parent = main (top-level field), 200 in / 20 out
helper B: parent = helper A (nested source field only, depth 2), 30 in / 3 out
other:    parent = some other session, 9999 in / 9999 out (must not count)
