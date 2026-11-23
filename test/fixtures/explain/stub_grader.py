"""Stub `command` grader for test/test_explain.py: copies the request JSON from stdin to $GRADER_CAPTURE
(when set) and prints the lines in the file named by argv[1]. Exit code from argv[2] (default 0)."""
import json
import os
import sys

req = json.load(sys.stdin)
cap = os.environ.get("GRADER_CAPTURE")
if cap:
    with open(cap, "w") as f:
        json.dump(req, f)
with open(sys.argv[1]) as f:
    sys.stdout.write(f.read())
sys.exit(int(sys.argv[2]) if len(sys.argv) > 2 else 0)
