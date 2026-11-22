#!/bin/sh
# Test watcher fired on a timer: counts its runs.
cat >/dev/null
printf 'x\n' >> "${TICK_LOG:-/dev/null}"
echo "NOTE: tick"
