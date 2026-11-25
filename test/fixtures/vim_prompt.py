"""A tiny stand-in for Claude Code's input box in vim editor mode (issue #38), for test_wake_splice.py.

Draws `❯ <text>` between two rules, with a `-- INSERT --` footer only in INSERT mode, like Claude Code 2.1.332.
Bracketed paste is on (ESC[?2004h): a paste lands verbatim in either mode. In NORMAL mode typed keys are vim
commands: `i a A s S` enter INSERT, `0` goes home, `D` deletes to the end, everything else is eaten, so typing
`[fleet] message ...` keys leaves `sage ...` (the `s` of `message` enters INSERT). Enter submits from either mode
and appends `SUBMIT:<text>` to the log. FIXTURE_EAT=N drops the first N characters of every paste and every typed
burst, a TUI that keeps eating keys whatever we do. argv: log [normal|insert]
"""
import os
import select
import sys
import termios
import tty

log, mode = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "normal")
eat = int(os.environ.get("FIXTURE_EAT", "0"))
buf, cur = "", 0
fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
tty.setraw(fd)
out = sys.stdout


def draw():
    out.write("\x1b[?2004h\x1b[H\x1b[2J" + "─" * 40 + "\r\n❯ " + buf + "\r\n" + "─" * 40 + "\r\n")
    out.write("  " + ("-- INSERT -- " if mode == "insert" else "") + "bypass permissions on\r\n")
    out.write("\x1b[2;%dH" % (3 + cur))
    out.flush()


def ready(t):
    return bool(select.select([fd], [], [], t)[0])


def readc():
    return os.read(fd, 1).decode("utf-8", "replace")


def insert(text):
    global buf, cur
    buf, cur = buf[:cur] + text + buf[cur:], cur + len(text)


def burst(first):
    """first char plus whatever else already arrived: the unit FIXTURE_EAT eats from."""
    s = first
    while ready(0.02):
        c = os.read(fd, 1)
        if c in (b"\x1b", b"\r", b"\n"):
            return s, c
        s += c.decode("utf-8", "replace")
    return s, None


draw()
pending = None
try:
    while True:
        c = pending.decode() if pending else readc()
        pending = None
        if c == "\x1b":
            seq = ""
            if ready(0.05):
                while ready(0.02) and len(seq) < 5:
                    seq += readc()
                    if seq in ("[200~",) or not "[200~".startswith(seq):
                        break
            if seq == "[200~":
                text = ""
                while not text.endswith("\x1b[201~"):
                    text += readc()
                insert(text[:-len("\x1b[201~")][eat:])
            elif not seq:
                mode, cur = "normal", max(0, cur - 1) if mode == "insert" else cur
        elif c in ("\r", "\n"):
            with open(log, "a") as f:
                f.write("SUBMIT:%s\n" % buf)
            buf, cur = "", 0
        elif mode == "insert":
            if c == "\x01":
                cur = 0
            elif c == "\x0b":
                buf = buf[:cur]
            elif c == "\x15":
                buf, cur = buf[cur:], 0
            elif c == "\x7f":
                if cur:
                    buf, cur = buf[:cur - 1] + buf[cur:], cur - 1
            elif c >= " ":
                s, pend = burst(c)
                pending = pend
                insert(s[eat:])
        else:  # NORMAL: a vim command per key
            keys, pend = burst(c)
            pending = pend
            k = 0
            for ch in keys[eat:] if eat else keys:
                if mode == "insert":
                    insert(ch)
                elif ch in "ia":
                    mode = "insert"
                    cur = min(len(buf), cur + (1 if ch == "a" and buf else 0))
                elif ch == "A":
                    mode, cur = "insert", len(buf)
                elif ch == "s":
                    buf, mode = buf[:cur] + buf[cur + 1:], "insert"
                elif ch == "S":
                    buf, cur, mode = "", 0, "insert"
                elif ch == "0":
                    cur = 0
                elif ch == "D":
                    buf = buf[:cur]
                    cur = max(0, len(buf) - 1) if buf else 0
        draw()
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
