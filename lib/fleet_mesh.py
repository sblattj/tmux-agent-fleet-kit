#!/usr/bin/env python3
"""fleet-mesh: stdio MCP server + CLI for peer messaging and decisions between fleet lanes.

Python 3.8+ stdlib only. See INTERFACES.md for the contract.
  fleet_mesh.py serve | send <from> <to> <text> | inbox <name> | roster | mirror (hook JSON on stdin)
                | wake-later <to> | pane-state <pane>
"""
import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.request

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2025-06-18"


# ---------------------------------------------------------------- paths / env

def state_dir():
    s = os.environ.get("FLEET_STATE")
    if s:
        return s
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "fleet")


def desk_name():
    """DECIDE desk lane: $FLEET_DECIDE, else DECIDE."""
    return os.environ.get("FLEET_DECIDE") or "DECIDE"


def orch_name():
    """ROUTE desk lane: $FLEET_ROUTE, else ROUTE."""
    return os.environ.get("FLEET_ROUTE") or "ROUTE"


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    line = "%s %s" % (now(), msg)
    try:
        sys.stderr.write("fleet-mesh: %s\n" % msg)
        sys.stderr.flush()
    except Exception:
        pass
    try:
        d = os.path.join(state_dir(), "log")
        os.makedirs(d, exist_ok=True)
        _append_line(os.path.join(d, "fleet.log"), line)
    except Exception:
        pass


# ---------------------------------------------------------------- file store

def _append_line(path, line):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line.rstrip("\n") + "\n")
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _atomic_write(path, text):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.%d.tmp" % (os.path.basename(path), os.getpid()))
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_lines(path):
    try:
        with open(path) as f:
            return [ln for ln in f.read().split("\n") if ln.strip()]
    except FileNotFoundError:
        return []


def _parse_lines(path):
    out = []
    for ln in _read_lines(path):
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def load_roster():
    try:
        with open(os.path.join(state_dir(), "roster.json")) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _inbox_path(name):
    return os.path.join(state_dir(), "inbox", "%s.jsonl" % name)


def _read_json(path):
    try:
        with open(path) as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _has_fleet(doc, key="mcpServers"):
    servers = doc.get(key)
    return isinstance(servers, dict) and "fleet" in servers


def has_mesh(name, entry):
    """True when the lane's CLI was given the fleet MCP (so it has peer_inbox). Evidence, in order: the roster's
    `mesh` bool; claude: the launch record ($FLEET_STATE/launch/<name>.sh passes --mcp-config with fleet_mesh.py),
    a user or per-project mcpServers.fleet in ~/.claude.json ($FLEET_CLAUDE_JSON), or <dir>/.mcp.json; codex,
    opencode, kiro, agy: a fleet server in the CLI's global MCP config."""
    m = entry.get("mesh")
    if isinstance(m, bool):
        return m
    cli, home = entry.get("cli"), os.path.expanduser("~")
    if cli == "claude":
        try:
            with open(os.path.join(state_dir(), "launch", "%s.sh" % name)) as f:
                if "fleet_mesh.py" in f.read():
                    return True
        except OSError:
            pass
        doc = _read_json(os.environ.get("FLEET_CLAUDE_JSON") or os.path.join(home, ".claude.json"))
        d = entry.get("dir") or ""
        proj = (doc.get("projects") or {}).get(d) if isinstance(doc.get("projects"), dict) else None
        return (_has_fleet(doc) or (isinstance(proj, dict) and _has_fleet(proj))
                or bool(d and _has_fleet(_read_json(os.path.join(d, ".mcp.json")))))
    if cli == "codex":
        try:
            with open(os.path.join(os.environ.get("CODEX_HOME") or os.path.join(home, ".codex"), "config.toml")) as f:
                return "[mcp_servers.fleet]" in f.read()
        except OSError:
            return False
    if cli == "opencode":
        xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        return _has_fleet(_read_json(os.path.join(xdg, "opencode", "opencode.json")), "mcp")
    if cli == "kiro":
        kh = os.environ.get("KIRO_HOME") or os.path.join(home, ".kiro")
        return _has_fleet(_read_json(os.path.join(kh, "settings", "mcp.json")))
    if cli == "agy":
        ah = os.environ.get("FLEET_AGY_HOME") or os.path.join(home, ".gemini")
        return _has_fleet(_read_json(os.path.join(ah, "config", "mcp_config.json")))
    return False


def wake_note(frm, to, entry):
    """The pointer line typed into a pane. A lane with no fleet MCP (not started by `fleet launch`) has no
    peer_inbox tool, so its line also names the inbox file to read by hand."""
    note = "[fleet] message from %s: run peer_inbox to read it" % frm
    if has_mesh(to, entry):
        return note
    return note + " (no peer_inbox tool? read %s)" % _inbox_path(to)


def _cursor_path(name):
    return os.path.join(state_dir(), "inbox", "%s.cursor" % name)


def _check_name(name):
    if not isinstance(name, str) or not name or not all(c.isalnum() or c in "._-" for c in name):
        raise ValueError("invalid lane name: %r" % (name,))
    return name


def read_cursor(name):
    try:
        with open(_cursor_path(name)) as f:
            return int(f.read().strip() or 0)
    except (FileNotFoundError, ValueError):
        return 0


def unread_count(name):
    return max(0, len(_read_lines(_inbox_path(name))) - read_cursor(name))


def read_inbox(name, all_msgs=False):
    _check_name(name)
    path = _inbox_path(name)
    lines = _read_lines(path)
    if all_msgs:
        start = 0
    else:
        start = read_cursor(name)
    msgs = []
    for ln in lines[start:]:
        try:
            msgs.append(json.loads(ln))
        except ValueError:
            msgs.append({"text": ln})
    if not all_msgs:
        _atomic_write(_cursor_path(name), "%d\n" % len(lines))
    return msgs


# ---------------------------------------------------------------- delivery

def _run(argv):
    """Run argv (no shell) with a 15 s timeout. Returns (ok, detail)."""
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
                           universal_newlines=True)
    except subprocess.TimeoutExpired:
        return False, "timeout after 15s"
    except OSError as e:
        return False, str(e)
    if p.returncode != 0:
        return False, "exit %d: %s" % (p.returncode, (p.stderr or p.stdout or "").strip()[:200])
    return True, "ok"


# agy screens a typed wake must never land on: its Enter would answer the screen for the human (agy's Terms of Service
# screen has a data-sharing opt-in pre-checked, so Enter would accept both). Same strings as bin/fleet agy_screen.
AGY_BLOCKING_SCREENS = ("Do you trust the contents", "Choose your color scheme", "Terms of Service",
                        "Waiting for authentication")


def _pane_text(pane):
    """capture-pane -p for pane; returns the text, or None when tmux fails."""
    try:
        p = subprocess.run(_tmux_argv("capture-pane", "-p", "-t", pane), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=15, universal_newlines=True)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return p.stdout if p.returncode == 0 else None


def _tmux_argv(*args):
    argv = ["tmux"]
    sock = os.environ.get("FLEET_TMUX_SOCKET")
    if sock:
        argv += ["-L", sock]
    return argv + list(args)


def wake(frm, to, text, entry):
    cli = entry.get("cli")
    if cli == "codex":
        binary = os.environ.get("FLEET_CODEX_BIN") or "codex"
        thread = entry.get("session_id") or to  # thread uuid when known, else the /rename name (both PROVEN)
        argv = [binary, "queue", "--thread", thread, "--message", "[fleet message from %s] %s" % (frm, text)]
        ok, detail = _run(argv)
        return "codex queue", ok, detail
    if cli == "opencode":
        port, sid = entry.get("port"), entry.get("session_id")
        if not port or not sid:
            return "opencode prompt_async", False, "roster entry lacks port or session_id"
        url = "http://127.0.0.1:%s/session/%s/prompt_async" % (port, sid)
        body = json.dumps({"parts": [{"type": "text",
                                      "text": "[fleet message from %s] %s" % (frm, text)}]}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                code = r.getcode()
            return "opencode prompt_async", 200 <= code < 300, "http %s" % code
        except Exception as e:
            return "opencode prompt_async", False, str(e)
    if cli in ("claude", "kiro", "agy"):  # kiro, agy: typed text + a separate Enter starts a turn on an idle lane (PROVEN)
        pane = entry.get("pane")
        if not pane:
            return "tmux send-keys", False, "roster entry lacks pane"
        if cli == "agy":  # look before typing; fail closed when the pane can't be read
            screen = _pane_text(pane)
            if screen is None:
                return "tmux send-keys", False, "could not read agy pane %s; not typing into it" % pane
            hit = next((m for m in AGY_BLOCKING_SCREENS if m in screen), None)
            if hit:
                return "tmux send-keys", False, "agy pane %s shows a %r screen; not typing into it" % (pane, hit)
        ok, detail = type_wake(pane, wake_note(frm, to, entry))
        if ok:
            mark_woken(to)
        if ok is None:  # the pane is busy: never type into it now; a detached waker retries, then leaves a notice
            if spawn_waker(to):
                return "tmux send-keys", True, "deferred (%s); retrying for up to %ds, then a tmux notice" % (
                    detail, int(_env_float("FLEET_WAKE_DEFER_MAX", DEFER_MAX)))
            return "tmux send-keys", True, "deferred (%s); no retry started, the message waits unread" % detail
        return "tmux send-keys", ok, detail
    return "none", False, "unknown cli %r" % (cli,)


def wake_result(prefix, frm, to, how, ok, detail):
    """The result line of deliver() and fleet_decisions.rewake()."""
    if ok and detail.startswith("deferred"):
        return "%sdelivered %s -> %s (inbox written; typed wake %s)" % (prefix, frm, to, detail)
    if ok:
        return "%sdelivered %s -> %s (inbox + wake via %s ok)" % (prefix, frm, to, how)
    return "%sdelivered %s -> %s (inbox written; wake via %s FAILED: %s)" % (prefix, frm, to, how, detail)


# ---------------------------------------------------------------- typed wakes that never splice
#
# A typed wake (`tmux send-keys`) must never land in text a human is typing. Before typing, the pane must show an
# EMPTY prompt row at the cursor (an allow-list: a prompt glyph, then nothing but a faint or coloured placeholder; any
# other screen defers), and no attached client may have sent it keys in the last FLEET_WAKE_QUIET seconds. A busy
# pane gets a detached waker (`fleet_mesh.py wake-later <to>`) that retries with backoff for FLEET_WAKE_DEFER_MAX
# seconds, then leaves a notice that types nothing (display-message plus the pane option @fleet_unread). The inbox
# line is written before any of this, so a message is never lost, only its doorbell waits.
#
# Residual race: the human can start typing between the last check and send-keys (two tmux calls, milliseconds).
# Enter is only sent once the input row reads exactly the pointer, so a collision leaves the pointer in the box
# unsent instead of submitting the human's half sentence; it cannot be undone without deleting their keys.

PROMPT_GLYPHS = ("❯", "›", ">")  # Claude Code, kiro-cli, agy (captured from each, 2026-11-23)
DEFER_MAX = 900  # seconds a deferred wake keeps retrying
NATIVE_GRACE = 120  # seconds a Claude -> Claude message waits for its native SendMessage before a typed wake
_SGR = re.compile(r"\x1b\[([0-9;:]*)m")
_ESC = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][0-9A-Za-z]|.)")


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def _apply_sgr(params, faint, colored):
    """Track SGR state: faint, and whether an explicit foreground colour is set. Typed text is drawn in the
    default foreground by Claude Code, kiro-cli and agy (captured 2026-11-23); their placeholders and hints are
    faint (Claude), grey truecolor (kiro) or a 256-colour (agy), so either flag marks placeholder text."""
    ps = re.split("[;:]", params) if params else ["0"]
    i = 0
    while i < len(ps):
        n = int(ps[i]) if ps[i].isdigit() else 0
        if n == 0:
            faint = colored = False
        elif n == 2:
            faint = True
        elif n == 22:
            faint = False
        elif n == 39:
            colored = False
        elif 30 <= n <= 37 or 90 <= n <= 97:
            colored = True
        elif n in (38, 48) and i + 1 < len(ps):
            if n == 38:
                colored = True
            i += 2 if ps[i + 1] == "5" else 4 if ps[i + 1] == "2" else 0
        i += 1
    return faint, colored


def screen_cells(line):
    """One `capture-pane -e` row as [(char, dim, width)]; dim = faint or coloured (placeholder, not typed text)."""
    cells, faint, colored, i = [], False, False, 0
    while i < len(line):
        if line[i] == "\x1b":
            m = _SGR.match(line, i)
            if m:
                faint, colored = _apply_sgr(m.group(1), faint, colored)
                i = m.end()
                continue
            m = _ESC.match(line, i)
            i = m.end() if m else i + 1
            continue
        ch = line[i]
        w = 0 if unicodedata.combining(ch) else (2 if unicodedata.east_asian_width(ch) in "WF" else 1)
        cells.append((ch, faint or colored, w))
        i += 1
    return cells


def _solid(cells):
    return any(not dim and not ch.isspace() for ch, dim, _ in cells)


def _plain(cells):
    return "".join(ch for ch, _, _ in cells)


def _is_rule(text):
    t = text.strip()
    return len(t) >= 10 and t.count("─") >= 0.6 * len(t)


def input_state(screen, cx, cy):
    """Classify a captured screen (`capture-pane -p -e`) with the cursor at (cx, cy).

    Returns ("empty", why) only for a prompt row whose text left of the cursor is just a prompt glyph and whose
    other text is dim placeholder; every other screen is ("busy", why): typed text, a dialog, vim NORMAL mode."""
    lines = screen.split("\n")
    if any("-- NORMAL --" in _plain(screen_cells(ln)) for ln in lines[-8:]):
        return "busy", "vim NORMAL mode"
    if not 0 <= cy < len(lines):
        return "busy", "cursor outside the captured screen"
    col, left, right = 0, [], []
    for c in screen_cells(lines[cy]):
        (left if col < cx else right).append(c)
        col += c[2]
    lt = _plain(left).strip()
    if lt not in PROMPT_GLYPHS:
        if lt[:1] in PROMPT_GLYPHS:
            return "busy", "typed text in the input"
        return "busy", "no empty prompt at the cursor (a dialog, a menu or another screen)"
    if _solid(right):
        return "busy", "typed text in the input"
    for ln in lines[cy + 1:cy + 9]:  # a multi-line draft continues below the cursor row, up to the box's rule
        cells = screen_cells(ln)
        if not _plain(cells).strip() or _is_rule(_plain(cells)):
            break
        if _solid(cells):
            return "busy", "more input lines below the prompt"
    return "empty", "empty prompt"


def input_text(screen, cy, every=False):
    """The solid (non-placeholder) text of the input box holding the cursor row, prompt glyph dropped; or None.
    every=True keeps placeholder-styled cells too: Claude Code draws a typed slash command (`/compact`) in colour."""
    lines = screen.split("\n")
    if not 0 <= cy < len(lines):
        return None
    top = None
    for r in range(cy, max(-1, cy - 9), -1):
        t = _plain(screen_cells(lines[r])).strip()
        if t[:1] in PROMPT_GLYPHS:
            top = r
            break
        if not t or _is_rule(t):
            return None
    if top is None:
        return None
    out = []
    for r in range(top, min(len(lines), top + 12)):
        cells = screen_cells(lines[r])
        t = _plain(cells)
        if r > top and (not t.strip() or _is_rule(t)):
            break
        out.append("".join(ch for ch, dim, _ in cells if every or not dim))
    text = "".join(out).strip()
    return text[1:].strip() if text[:1] in PROMPT_GLYPHS else text


def _tmux_out(*args):
    try:
        p = subprocess.run(_tmux_argv(*args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
                           universal_newlines=True)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return p.stdout if p.returncode == 0 else None


def _read_screen(pane):
    """(screen, cx, cy, in_mode) for pane, or None when tmux can't read it."""
    info = _tmux_out("display-message", "-p", "-t", pane, "#{cursor_x}\t#{cursor_y}\t#{pane_in_mode}")
    screen = _tmux_out("capture-pane", "-p", "-e", "-t", pane)
    try:
        cx, cy, mode = info.strip("\n").split("\t")
        return screen, int(cx), int(cy), mode not in ("", "0")
    except (AttributeError, ValueError):
        return None


def human_active(pane, quiet=None):
    """True when an attached client whose current pane is `pane` sent keys in the last `quiet` seconds."""
    quiet = _env_float("FLEET_WAKE_QUIET", 3) if quiet is None else quiet
    if quiet <= 0:
        return False
    out = _tmux_out("list-clients", "-F", "#{pane_id}\t#{client_activity}") or ""
    t = time.time()
    for ln in out.splitlines():
        p, _, act = ln.partition("\t")
        if p == pane and act.isdigit() and t - int(act) < quiet:
            return True
    return False


def pane_busy(pane):
    """(busy, why). Fails closed: a pane tmux can't read counts as busy."""
    if human_active(pane):
        return True, "keys from an attached client in the last %ss" % os.environ.get("FLEET_WAKE_QUIET", "3")
    got = _read_screen(pane)
    if got is None or got[0] is None:
        return True, "could not read pane %s" % pane
    screen, cx, cy, in_mode = got
    if in_mode:
        return True, "pane is in copy mode"
    state, why = input_state(screen, cx, cy)
    if state == "empty" and _claude_vim() and _claude_prompt_row(screen, cy) and \
            not any("-- INSERT --" in _plain(screen_cells(ln)) for ln in screen.split("\n")[-8:]):
        # Claude Code's vim mode shows `-- INSERT --` while inserting and nothing in NORMAL mode, where typed keys
        # would run as vim commands (`[fleet] mes` eaten up to the `s`, measured 2026-11-23)
        return True, "vim NORMAL mode (no -- INSERT -- shown)"
    return state != "empty", why


def _claude_vim():
    """True when Claude Code's editorMode is vim (~/.claude.json, or $FLEET_CLAUDE_JSON)."""
    path = os.environ.get("FLEET_CLAUDE_JSON") or os.path.join(os.path.expanduser("~"), ".claude.json")
    return _read_json(path).get("editorMode") == "vim"


def _claude_prompt_row(screen, cy):
    lines = screen.split("\n")
    return 0 <= cy < len(lines) and _plain(screen_cells(lines[cy])).lstrip()[:1] == "\u276f"


_POINTER = re.compile(r"^\[fleet\]messagefrom\S+?:runpeer_inboxtoreadit(\(nopeer_inboxtool\?read\S+\))?$")


class _PaneLock(object):
    """flock on wake/pane-<id>.lock, so two senders never type into one pane at once (waits up to `wait` s)."""

    def __init__(self, pane, wait=8.0):
        self.path = os.path.join(state_dir(), "wake", "pane-%s.lock" % re.sub(r"[^A-Za-z0-9_-]", "_", pane))
        self.wait, self.f = wait, None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.f = open(self.path, "a")
        end = time.time() + self.wait
        while True:
            try:
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.time() > end:
                    return False
                time.sleep(0.05)

    def __exit__(self, *exc):
        self.f.close()  # closing drops the flock
        return False


def _leftover_pointer(pane):
    """True when the input holds nothing but a fleet pointer whose Enter never went (an earlier race or timeout)."""
    s = _read_screen(pane)
    if not s or s[0] is None or s[3]:
        return False
    text = input_text(s[0], s[2])
    return bool(text) and bool(_POINTER.match("".join(text.split())))


def type_wake(pane, note):
    """Type `note` + Enter only into an empty, quiet prompt. Returns (True|False, detail), or (None, why) if busy."""
    with _PaneLock(pane) as got:
        if not got:
            return None, "another wake is typing into %s" % pane
        return _type_wake_locked(pane, note)


def _type_wake_locked(pane, note):
    busy, why = pane_busy(pane)
    if busy and not why.startswith("keys from") and _POINTER.match("".join(note.split())) and _leftover_pointer(pane):
        log("type_wake %s: the input holds an unsent fleet pointer; sending its Enter" % pane)
        ok, detail = _run(_tmux_argv("send-keys", "-t", pane, "Enter"))
        return ok, ("sent Enter for a pointer left unsent earlier" if ok else detail)
    if busy:
        return None, why
    ok, detail = _run(_tmux_argv("send-keys", "-t", pane, "-l", note))
    if not ok:
        return False, detail
    # race guard: Enter only once the input reads exactly the pointer; a human key in between means no Enter
    want, got, end = "".join(note.split()), None, time.time() + 2.0
    while True:
        s = _read_screen(pane)
        got = input_text(s[0], s[2]) if s and s[0] is not None else None
        if got is not None and "".join(got.split()) == want:
            break
        every = input_text(s[0], s[2], every=True) if s and s[0] is not None else None
        if every is not None and "".join(every.split()) == want:  # a coloured slash command (Claude `/compact`)
            break
        if time.time() > end:
            log("type_wake %s: input read %r after typing, not the pointer alone; Enter not sent" % (pane, got))
            return False, ("typed the pointer, but the input then read %r, not the pointer alone; Enter not sent"
                           " (a human may have typed at the same moment)" % (got or "")[:80])
        time.sleep(0.05)
    return _run(_tmux_argv("send-keys", "-t", pane, "Enter"))


def _waker_lock_path(to):
    return os.path.join(state_dir(), "wake", "%s.lock" % to)


def spawn_waker(to):
    """Start a detached `wake-later <to>` (it exits at once if one already holds the lock for `to`)."""
    if os.environ.get("FLEET_WAKE_SPAWN", "1") == "0":
        return False
    try:
        d = os.path.join(state_dir(), "log")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "wake-later.log"), "a") as errf:
            subprocess.Popen([sys.executable, os.path.abspath(__file__), "wake-later", to], stdin=subprocess.DEVNULL,
                             stdout=errf, stderr=errf, start_new_session=True, close_fds=True)
        return True
    except OSError as e:
        log("spawn_waker %s failed: %s" % (to, e))
        return False


def _native_confirmed(to):
    try:
        with open(os.path.join(state_dir(), "inbox", "%s.native" % to)) as f:
            return set(ln.strip() for ln in f if ln.strip())
    except OSError:
        return set()


def _woken_path(to):
    return os.path.join(state_dir(), "wake", "%s.woken" % to)


def mark_woken(to):
    """Record that a typed pointer reached `to` covering every inbox line so far (no second pointer for them)."""
    try:
        os.makedirs(os.path.dirname(_woken_path(to)), exist_ok=True)
        with open(_woken_path(to) + ".tmp", "w") as f:
            f.write(str(len(_read_lines(_inbox_path(to)))))
        os.replace(_woken_path(to) + ".tmp", _woken_path(to))
    except OSError as e:
        log("mark_woken %s: %s" % (to, e))


def _woken(to):
    try:
        with open(_woken_path(to)) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _wake_needed(to):
    """(need, wait, frm): unread lines that still need a typed wake, and whether a native grace is still running."""
    lines = _read_lines(_inbox_path(to))[max(read_cursor(to), _woken(to)):]
    done, grace = _native_confirmed(to), _env_float("FLEET_NATIVE_GRACE", NATIVE_GRACE)
    need, wait, frm = 0, False, None
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            rec = {}
        if rec.get("native") == "pending":
            if rec.get("id") in done:
                continue
            if time.time() - _parse_ts(rec.get("at")) < grace:
                wait = True
                continue
        need += 1
        frm = rec.get("from") or frm
    return need, wait, frm


def _parse_ts(ts):
    try:
        return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def notice(to, entry, frm, n, why):
    """The non-typing fallback: a display-message on every attached client, and @fleet_unread on the pane."""
    pane = entry.get("pane")
    msg = "[fleet] %s has %d unread (latest from %s); wake not typed: %s" % (to, n, frm or "?", why)
    for c in (_tmux_out("list-clients", "-F", "#{client_name}") or "").splitlines():
        if c:
            _run(_tmux_argv("display-message", "-c", c, "-d", "15000", msg))
    if pane:
        _run(_tmux_argv("set-option", "-p", "-t", pane, "@fleet_unread", "%d from %s" % (n, frm or "?")))
    log("notice %s: %s" % (to, msg))


def clear_notice(name):
    pane = (load_roster().get(name) or {}).get("pane")
    if pane:
        _run(_tmux_argv("set-option", "-p", "-u", "-t", pane, "@fleet_unread"))


def wake_later(to):
    """Detached waker: retry the typed wake with backoff until it lands, the lane reads its inbox, or the bound
    passes; then leave a notice. One per target (flock); returns a short result string."""
    _check_name(to)
    path = _waker_lock_path(to)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lk = open(path, "a")
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return "another waker holds %s" % to
    try:
        bound = _env_float("FLEET_WAKE_DEFER_MAX", DEFER_MAX)  # counted from when a typed wake is first needed
        start, step = None, _env_float("FLEET_WAKE_BACKOFF", 2)
        why, frm, need = "input busy", None, 0
        while True:
            need, wait, frm = _wake_needed(to)
            if not need and not wait:
                log("wake-later %s: nothing left to wake for" % to)
                return "nothing to wake"
            entry = load_roster().get(to) or {}
            if need and start is None:
                start = time.time()
            if need and entry.get("cli") in ("claude", "kiro", "agy") and entry.get("pane"):
                ok, why = type_wake(entry["pane"], wake_note(frm or "?", to, entry))
                if ok:
                    mark_woken(to)
                    log("wake-later %s: typed wake landed after %ds" % (to, time.time() - start))
                    return "woke"
                if ok is False:
                    why = "typing failed: %s" % why
            if start is not None and time.time() - start > bound:
                if need:
                    notice(to, entry, frm, need, why)
                    return "notice"
                return "gave up"
            time.sleep(step)
            step = min(step * 2, 30)
    finally:
        lk.close()


def fleet_bin():
    return os.environ.get("FLEET_BIN") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "fleet")


def reopen(to):
    """Run `fleet reopen <to>` (timeout 180 s). Returns (ok, detail)."""
    try:
        p = subprocess.run([fleet_bin(), "reopen", to], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=180, universal_newlines=True)
    except subprocess.TimeoutExpired:
        return False, "timeout after 180s"
    except OSError as e:
        return False, str(e)
    if p.returncode != 0:
        return False, "exit %d: %s" % (p.returncode, (p.stderr or p.stdout or "").strip()[:200])
    return True, "ok"


def deliver(frm, to, text, native_ok=False):
    """Append to inbox/<to>.jsonl, then wake by roster cli. Returns a result string.

    native_ok (or an MCP tools/call in progress: peer_send, decision_open, decision_answer): the result goes back to
    the calling agent, so a Claude -> Claude send may ask it to follow up with SendMessage instead of typing into the
    target (native_route). CLI sends and the WATCH, probe and DECISIONS engines never do.

    A parked or done target is reopened first (`fleet reopen <to>`), then woken as usual."""
    _check_name(to)
    ts = now()
    msg_id = "%s-%s-%d" % (ts, to, len(_read_lines(_inbox_path(to))) + 1)
    rec = {"id": msg_id, "at": ts, "from": frm, "to": to, "text": text}
    roster = load_roster()
    entry = roster.get(to)
    native = (native_ok or _MCP_CALL[0]) and native_route(frm, to, entry, roster)
    if native:
        rec["native"] = "pending"
    _append_line(_inbox_path(to), json.dumps(rec))
    if native:
        spawn_waker(to)  # safety net: a typed wake after the grace, once the input is empty, if no SendMessage came
        log("deliver %s -> %s native pending (no typed wake)" % (frm, to))
        return ("delivered %s -> %s (inbox written; no typed wake into a Claude pane. Now call SendMessage with "
                "to=\"%s\" and this same text (for a decision or an answer, the question or answer line): it reaches %s "
                "natively without touching its input box, and the mirror hook links it to this inbox line. If no SendMessage follows within %ds, a typed pointer goes once "
                "%s's input is empty)" % (frm, to, to, to, int(_env_float("FLEET_NATIVE_GRACE", NATIVE_GRACE)), to))
    if not entry:
        log("deliver %s -> %s queued (no live lane)" % (frm, to))
        return "queued (no live lane): %s -> %s" % (frm, to)
    prefix = ""
    if entry.get("status") in ("parked", "done"):
        r_ok, r_detail = reopen(to)
        log("deliver %s -> %s reopen ok=%s %s" % (frm, to, r_ok, r_detail))
        if not r_ok:
            return "delivered %s -> %s (inbox written; reopen FAILED: %s)" % (frm, to, r_detail)
        prefix = "reopened %s; " % to
        entry = load_roster().get(to) or entry
    how, ok, detail = wake(frm, to, text, entry)
    log("deliver %s -> %s wake=%s ok=%s %s" % (frm, to, how, ok, detail))
    return wake_result(prefix, frm, to, how, ok, detail)


def native_route(frm, to, entry, roster):
    """True when this send should ride Claude Code's own cross-session messaging instead of a typed wake: the
    caller is a fleet Claude lane's own MCP server (CLAUDECODE=1, FLEET_NAME=frm, roster cli claude) and the
    target is a live fleet Claude lane. Every fleet Claude lane runs bypass, so the native message is same-class
    and delivers at once (PROVEN 2.1.330); a parked lane has no socket, so it keeps the reopen + typed path."""
    if os.environ.get("CLAUDECODE") != "1" or os.environ.get("FLEET_NAME") != frm or frm == to:
        return False
    if os.environ.get("FLEET_NATIVE", "1") == "0":
        return False
    if (roster.get(frm) or {}).get("cli") != "claude" or not entry or entry.get("cli") != "claude":
        return False
    return entry.get("status") not in ("parked", "done") and bool(entry.get("pane"))


# ---------------------------------------------------------------- mirror (native SendMessage -> mesh)

def claude_sessions_dir():
    return os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude"),
                        "sessions")


def _lane_by_socket(path, roster):
    """Roster lane whose Claude session advertises messaging socket `path` (matched by session id), or None."""
    want = os.path.realpath(path)
    for name, e in roster.items():
        if e.get("cli") != "claude" or not e.get("pid"):
            continue
        try:
            with open(os.path.join(claude_sessions_dir(), "%s.json" % int(e["pid"]))) as f:
                sess = json.load(f)
        except (OSError, ValueError, TypeError):
            continue
        if e.get("session_id") and sess.get("sessionId") != e.get("session_id"):
            continue
        sock = sess.get("messagingSocketPath")
        if sock and os.path.realpath(sock) == want:
            return name
    return None


def mirror_target(to, roster):
    """Map a SendMessage `to` (a lane name, `name [ref]`, or `uds:<socket>`) to a roster lane name, else None."""
    if not isinstance(to, str) or not to.strip():
        return None
    to = to.strip()
    if to.startswith("uds:"):
        return _lane_by_socket(to[4:], roster)
    name = to.split(" [", 1)[0].strip()
    return name if name in roster else None


def mirror(payload):
    """PostToolUse hook body for native SendMessage: append a copy to the mesh inbox of the fleet lane it went to.

    No wake and no reopen (the native message already reached the lane). Returns a result string; never raises."""
    try:
        if payload.get("tool_name") != "SendMessage":
            return "skip: tool %r" % (payload.get("tool_name"),)
        ti = payload.get("tool_input") or {}
        text = ti.get("message")
        frm = os.environ.get("FLEET_NAME")
        if not frm:
            return "skip: FLEET_NAME unset (not a fleet lane)"
        if not isinstance(text, str) or not text:
            return "skip: no message body"
        to = mirror_target(ti.get("to"), load_roster())
        if not to or to == frm:
            return "skip: %r is not another fleet lane" % (ti.get("to"),)
        _check_name(to)
        pending = _oldest_pending(frm, to)
        if pending:
            _append_line(os.path.join(state_dir(), "inbox", "%s.native" % to), pending)
            log("mirror %s -> %s linked to %s (native SendMessage after peer_send)" % (frm, to, pending))
            return "linked %s -> %s %s" % (frm, to, pending)
        ts = now()
        rec = {"id": "%s-%s-%d" % (ts, to, len(_read_lines(_inbox_path(to))) + 1), "at": ts, "from": frm,
               "to": to, "text": text, "via": "native"}
        _append_line(_inbox_path(to), json.dumps(rec))
        log("mirror %s -> %s (native SendMessage)" % (frm, to))
        return "mirrored %s -> %s" % (frm, to)
    except Exception as e:
        log("mirror error: %s" % e)
        return "mirror error: %s" % e


def _oldest_pending(frm, to, max_age=1800):
    """Id of the oldest native-pending inbox line frm -> to not yet linked to a SendMessage, or None."""
    done = _native_confirmed(to)
    for ln in _read_lines(_inbox_path(to))[-200:]:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if (rec.get("native") == "pending" and rec.get("from") == frm and rec.get("id") not in done
                and time.time() - _parse_ts(rec.get("at")) < max_age):
            return rec.get("id")
    return None


# ---------------------------------------------------------------- decisions

def _decisions_path():
    return os.path.join(state_dir(), "decisions.jsonl")


def decisions():
    return _parse_lines(_decisions_path())


def _sender(args):
    s = args.get("from") or os.environ.get("FLEET_NAME")
    if not s:
        raise ValueError("no sender: pass `from` or set FLEET_NAME")
    return s


# ---------------------------------------------------------------- tools

def t_peer_list(args):
    roster = load_roster()
    if not roster:
        return "roster empty"
    rows = []
    for name in sorted(roster):
        e = roster[name]
        rows.append("%s cli=%s mode=%s pane=%s session=%s port=%s unread=%d" % (
            name, e.get("cli"), e.get("mode"), e.get("pane"), e.get("session_id"),
            e.get("port"), unread_count(name)))
    return "\n".join(rows)


def side_run():
    """(side id, parent lane) when this process is a side run (FLEET_SIDE set), else None.

    A side run never calls the decisions desk and messages only its parent lane (issue #12)."""
    sid = os.environ.get("FLEET_SIDE")
    if not sid:
        return None
    parent = os.environ.get("FLEET_PARENT")
    if not parent:
        raise ValueError("side run %s has no FLEET_PARENT: it can't send or ask anything" % sid)
    return sid, parent


def side_refuse(verb):
    """Raise when a side run tries a decisions-desk call (verb: open, answer)."""
    sr = side_run()
    if sr:
        raise ValueError("side run %s can't %s decisions; send it to your parent lane %s with peer_send"
                         % (sr[0], verb, sr[1]))


def t_peer_send(args):
    to, text = args.get("to"), args.get("text")
    if not to or not isinstance(text, str) or not text:
        raise ValueError("`to` and `text` are required")
    sr = side_run()
    if sr:
        sid, parent = sr
        if to != parent:
            raise ValueError("side run %s can send only to its parent lane %s, not %s" % (sid, parent, to))
        return deliver("%s/%s" % (parent, sid), to, text)
    return deliver(_sender(args), to, text)


def t_peer_inbox(args):
    name = args.get("name") or os.environ.get("FLEET_NAME")
    if not name:
        raise ValueError("no inbox name: pass `name` or set FLEET_NAME")
    msgs = read_inbox(name, bool(args.get("all")))
    clear_notice(name)
    if not msgs:
        return "no unread messages"
    return "\n".join("[%s] from %s%s: %s" % (m.get("at", ""), m.get("from", "?"),
                                          " (native copy)" if m.get("via") == "native" else "", m.get("text", ""))
                     for m in msgs)


ALLOW_TYPES = ("pick", "edit", "own", "snooze", "refuse")
WATCH_KEYS = ("sourceKind", "source", "ref", "url", "eid")  # the WATCH object a call may carry (four-desks 2.4)
_WATCH_URL = re.compile(r"^https?://[^\s\"'<>\\]+$", re.I)


def _clean_watch(w):
    """Keep WATCH_KEYS only, as one-line strings; drop a url that is not http(s). None when nothing is left."""
    if w is None:
        return None
    if not isinstance(w, dict):
        raise ValueError("`watch` must be an object {sourceKind, source, ref, url, eid}")
    out = {}
    for k in WATCH_KEYS:
        v = w.get(k)
        if isinstance(v, str) and v.strip() and len(v) <= 500 and "\n" not in v:
            out[k] = v.strip()
    if "url" in out and not _WATCH_URL.match(out["url"]):
        del out["url"]
    return out or None


def t_decision_open(args):
    side_refuse("open")
    q = args.get("question")
    if not q:
        raise ValueError("`question` is required")
    asker = _sender(args)
    opts = args.get("options") or []
    if not isinstance(opts, list):
        raise ValueError("`options` must be a list of strings")
    allow = args.get("allow")
    if allow is not None:
        if not isinstance(allow, list) or not set(allow) <= set(ALLOW_TYPES):
            raise ValueError("`allow` must be a list drawn from %s" % ", ".join(ALLOW_TYPES))
        allow = [str(a) for a in allow]
    watch = _clean_watch(args.get("watch"))
    path = _decisions_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            n = len([ln for ln in f.read().split("\n") if ln.strip()]) + 1
            rec = {"id": "Q%d" % n, "at": now(), "from": asker, "question": q,
                   "options": [str(o) for o in opts], "status": "open",
                   "answer": None, "answeredAt": None}
            if allow is not None:
                rec["allow"] = allow
            if watch:
                rec["watch"] = watch
            f.seek(0, 2)
            f.write(json.dumps(rec) + "\n")
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    text = "[decision Q%d from %s] %s" % (n, asker, q)
    if opts:
        text += " options: " + " | ".join(str(o) for o in opts)
    res = deliver(asker, desk_name(), text)
    return "opened Q%d; %s" % (n, res)


def t_decision_list(args):
    status = args.get("status")
    rows = [d for d in decisions() if not status or d.get("status") == status]
    if not rows:
        return "no decisions"
    out = []
    for d in rows:
        line = "%s [%s] from %s: %s" % (d.get("id"), d.get("status"), d.get("from"), d.get("question"))
        if d.get("options"):
            line += " options: " + " | ".join(d["options"])
        if d.get("answer"):
            line += " -> " + d["answer"]
        out.append(line)
    return "\n".join(out)


def mark_decision(did, answer, extra=None, only_open=False):
    """Mark decision `did` answered in decisions.jsonl under its file lock. Returns the updated record.

    only_open=True refuses an id that is no longer open (raises AlreadyAnswered), so two front ends racing on the
    same call relay once. `extra` fields (answerKind, via, ...) are merged into the record."""
    path = _decisions_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            rows = []
            for ln in f.read().split("\n"):
                if ln.strip():
                    try:
                        rows.append(json.loads(ln))
                    except ValueError:
                        pass
            target = None
            for r in rows:
                if r.get("id") == did:
                    target = r
            if target is None:
                raise ValueError("no such decision: %s" % did)
            if only_open and target.get("status") != "open":
                raise AlreadyAnswered("%s is already %s" % (did, target.get("status")))
            target.update({"status": "answered", "answer": answer, "answeredAt": now()})
            if extra:
                target.update(extra)
            f.seek(0)
            f.truncate()
            f.write("".join(json.dumps(r) + "\n" for r in rows))
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return target


class AlreadyAnswered(ValueError):
    pass


def decisions_log_path():
    return os.path.join(state_dir(), "decisions-log.jsonl")


def reports_path():
    return os.path.join(state_dir(), "decisions-reports.jsonl")


def t_decision_answer(args):
    side_refuse("answer")
    did, answer = args.get("id"), args.get("answer")
    if not did or not answer:
        raise ValueError("`id` and `answer` are required")
    target = mark_decision(did, answer)
    frm = os.environ.get("FLEET_NAME") or desk_name()
    asker = target.get("from")
    _append_line(decisions_log_path(), json.dumps(
        {"id": did, "at": now(), "asker": asker, "verbatim": answer, "relayedTo": [asker] if asker else [], "by": frm}))
    if not asker:
        return "answered %s; no asker to deliver to" % did
    return "answered %s; %s" % (did, deliver(frm, asker, "[answer %s] %s" % (did, answer)))


def t_decision_report(args):
    """The asker reports whether it acted on an answer: appends decisions-reports.jsonl."""
    did = args.get("id")
    ok = args.get("ok")
    if not did or not isinstance(ok, bool):
        raise ValueError("`id` and boolean `ok` are required")
    rec = {"id": did, "at": now(), "from": _sender(args), "ok": ok, "note": str(args.get("note") or "")}
    _append_line(reports_path(), json.dumps(rec))
    return "reported %s %s" % (did, "acted on" if ok else "act failed")


def _norm(text):
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in (text or "")).split())


def find_decisions(text):
    """Answered decisions whose question matches `text`: exact (normalized) repeats first, then substring hits.

    Returns [{id, question, verbatim, at, asker, exact}] from decisions.jsonl joined with decisions-log.jsonl."""
    want = _norm(text)
    if not want:
        return []
    latest = {}
    for rec in _parse_lines(decisions_log_path()):
        if rec.get("id"):
            latest[rec["id"]] = rec
    hits = []
    for d in decisions():
        if d.get("status") != "answered":
            continue
        q = _norm(d.get("question"))
        exact = q == want
        if not exact and want not in q and q not in want:
            continue
        lg = latest.get(d.get("id")) or {}
        hits.append({"id": d.get("id"), "question": d.get("question"), "verbatim": lg.get("verbatim") or d.get("answer"),
                     "at": lg.get("at") or d.get("answeredAt"), "asker": d.get("from"), "exact": exact})
    hits.sort(key=lambda h: (not h["exact"], h.get("at") or ""), reverse=False)
    return hits


def t_decision_find(args):
    text = args.get("text")
    if not text:
        raise ValueError("`text` is required")
    hits = find_decisions(text)
    if not hits:
        return "no past decision matches"
    return "\n".join('%s%s [%s] %s -> "%s"' % (h["id"], " (exact repeat)" if h["exact"] else "", h.get("at"),
                                              h.get("question"), h.get("verbatim")) for h in hits)


def _side_mcp():
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import fleet_side_mcp  # lazy: fleet_side imports this module
    return fleet_side_mcp


def t_side_start(args):
    return _side_mcp().side_start(args)


def t_side_end(args):
    return _side_mcp().side_end(args)


def t_side_list(args):
    return _side_mcp().side_list(args)


def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


_S = {"type": "string"}
TOOLS = [
    ("peer_list", "List roster lanes with unread inbox counts.", _schema({}), t_peer_list),
    ("peer_send", "Send a message to another lane (inbox plus wake).",
     _schema({"to": _S, "text": _S, "from": _S}, ["to", "text"]), t_peer_send),
    ("peer_inbox", "Read unread messages (advances cursor); all=true returns everything.",
     _schema({"name": _S, "all": {"type": "boolean"}}), t_peer_inbox),
    ("decision_open", "Open a decision and deliver it to the decisions desk.",
     _schema({"question": _S, "options": {"type": "array", "items": _S}, "from": _S,
              "allow": {"type": "array", "items": {"type": "string", "enum": list(ALLOW_TYPES)}},
              "watch": {"type": "object", "description": "WATCH source of the call: sourceKind, source, ref, url "
                        "(http/https only), eid. Other keys are dropped.",
                        "properties": {k: _S for k in WATCH_KEYS}}}, ["question"]),
     t_decision_open),
    ("decision_list", "List decisions, optionally filtered by status (open|answered).",
     _schema({"status": {"type": "string", "enum": ["open", "answered"]}}), t_decision_list),
    ("decision_answer", "Answer a decision; delivers to the asker only and appends decisions-log.jsonl.",
     _schema({"id": _S, "answer": _S}, ["id", "answer"]), t_decision_answer),
    ("decision_report", "Report whether you acted on an answer (ok=true acted on, false act failed).",
     _schema({"id": _S, "ok": {"type": "boolean"}, "note": _S, "from": _S}, ["id", "ok"]), t_decision_report),
    ("decision_find", "Search past answered decisions before opening a call; an exact repeat returns its quote.",
     _schema({"text": _S}, ["text"]), t_decision_find),
    ("side_start", "Register a side run (a process you started outside the fleet panes) as your child; returns its id.",
     _schema({"host": _S, "pid": {"type": "integer"}, "model": _S, "log": _S, "result": _S,
              "bound": {"type": "string", "description": "90s, 30m, 2h or seconds; default 30m, max 12h"},
              "parent": {"type": "string", "description": "lane that owns the run; default $FLEET_NAME"},
              "sessionId": _S}, ["host", "pid"]), t_side_start),
    ("side_end", "Mark a side run ended (status done|failed, optional exit code). Idempotent.",
     _schema({"id": _S, "status": {"type": "string", "enum": ["done", "failed"]}, "exit": {"type": "integer"}},
             ["id"]), t_side_end),
    ("side_list", "List side runs, one line each: id, status, host, model, elapsed/bound, tokens. all=true adds "
     "ended runs.", _schema({"parent": {"type": "string", "description": "default $FLEET_NAME; * for every lane"},
                             "all": {"type": "boolean"}}), t_side_list),
]
TOOL_MAP = {t[0]: t for t in TOOLS}


_MCP_CALL = [False]  # True while an MCP tools/call runs: its result goes back to the calling agent


def call_tool(name, args):
    if name not in TOOL_MAP:
        return {"content": [{"type": "text", "text": "unknown tool: %s" % name}], "isError": True}
    try:
        _MCP_CALL[0] = True
        try:
            text = TOOL_MAP[name][3](args or {})
        finally:
            _MCP_CALL[0] = False
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as e:
        log("tool %s error: %s" % (name, e))
        return {"content": [{"type": "text", "text": "error: %s" % e}], "isError": True}


# ---------------------------------------------------------------- JSON-RPC

def handle(msg):
    """Return a response dict, or None for notifications."""
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    method, mid = msg.get("method"), msg.get("id")
    is_note = "id" not in msg
    params = msg.get("params") or {}
    if method == "initialize":
        req = params.get("protocolVersion")
        ver = req if req in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
        result = {"protocolVersion": ver, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fleet-mesh", "version": "1.0.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s, _ in TOOLS]}
    elif method == "tools/call":
        result = call_tool(params.get("name"), params.get("arguments"))
    elif is_note:
        return None
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": "Method not found: %s" % method}}
    if is_note:
        return None
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            try:
                resp = handle(msg)
            except Exception as e:
                log("internal error: %s" % e)
                resp = {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                        "error": {"code": -32603, "message": "Internal error: %s" % e}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


# ---------------------------------------------------------------- CLI

def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "serve":
        serve()
        return 0
    if cmd == "send" and len(argv) >= 5:
        print(deliver(argv[2], argv[3], " ".join(argv[4:])))
        return 0
    if cmd == "inbox" and len(argv) == 3:
        for m in read_inbox(argv[2]):
            print("[%s] from %s: %s" % (m.get("at", ""), m.get("from", "?"), m.get("text", "")))
        return 0
    if cmd == "mirror":  # PostToolUse hook: stdin is the hook JSON; always exit 0 so a hook never blocks a send
        try:
            payload = json.load(sys.stdin)
        except ValueError:
            payload = {}
        print(mirror(payload if isinstance(payload, dict) else {}))
        return 0
    if cmd == "wake-later" and len(argv) == 3:  # detached deferred waker (spawn_waker)
        print(wake_later(argv[2]))
        return 0
    if cmd == "pane-state" and len(argv) == 3:  # "empty <why>" (exit 0) or "busy <why>" (exit 1) for a pane
        busy, why = pane_busy(argv[2])
        print("%s %s" % ("busy" if busy else "empty", why))
        return 1 if busy else 0
    if cmd == "type" and len(argv) == 4:  # guarded type + Enter (fleet park's /compact): exit 0 sent, 1 failed, 3 busy
        ok, why = type_wake(argv[2], argv[3])
        print("%s %s" % ({True: "sent", False: "failed", None: "busy"}[ok], why))
        return {True: 0, False: 1, None: 3}[ok]
    if cmd == "mesh":  # one line per roster lane: "<name> yes|no" (fleet ls)
        for n, e in sorted(load_roster().items()):
            print("%s %s" % (n, "yes" if has_mesh(n, e if isinstance(e, dict) else {}) else "no"))
        return 0
    if cmd == "roster":
        print(json.dumps(load_roster(), indent=2, sort_keys=True))
        return 0
    sys.stderr.write("usage: fleet_mesh.py serve | send <from> <to> <text> | inbox <name> | roster | mesh | mirror"
                     " | wake-later <to> | pane-state <pane> | type <pane> <text>\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
