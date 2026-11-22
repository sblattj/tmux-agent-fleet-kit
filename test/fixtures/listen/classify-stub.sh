#!/bin/sh
# Test classifier: reads the request JSON on stdin, prints classifier lines.
# Records each call so the test can count calls and see the tail size.
cat > "${STUB_LOG:-/dev/null}.last"
printf 'x\n' >> "${STUB_LOG:-/dev/null}"
echo "ASK: can we get the retry change into this week?"
echo "ACTION: I'll need the staging numbers before Friday | owner: lane-a | due: Friday"
echo "ACTION: a quote nobody said on the call"
echo "SAY: p95 went from 840 ms to 310 ms | source: lane-a report"
echo "this line has no prefix"
echo "MAYBE: unknown prefix"
