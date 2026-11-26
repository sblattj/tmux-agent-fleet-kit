#!/usr/bin/env python3
"""fleet app: the text side of Fleet Desk, the Mac app in app/desk-ui (Tauri v2).

The app forks none of the desk pages. It runs these verbs and points its window at the live 127.0.0.1 page, so
every app action has a pane command (Q698: the UI is a superset of the pane, the pane keeps the full text):

  fleet app urls [--json]          each desk page: its URL (token included) and whether it answers
  fleet app serve [--json]         start the desk servers that are not answering, then print what urls prints
  fleet app notify [--json] [--dry-run]
                                   new DECIDE calls and new lessons waiting, each one once (deduped in
                                   $FLEET_STATE/app-notified.json, so a restart or a second front end never re-fires)
  fleet app tunnel [HOST]          the one ssh -L command that brings every desk page to a phone, and the URLs to open
  fleet app build                  build the unsigned local Fleet Desk.app (bunx @tauri-apps/cli, cargo)
  fleet app open                   open the built app (in the background)

Every verb is the human's: any FLEET_ROLE is refused (the URLs carry the page tokens). Python 3.8+ stdlib only.
"""
import argparse
import datetime
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet_mesh as mesh  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "app", "desk-ui")

# One row per entry in the app's Desks menu, in menu order. `server` is the desk server that owns the page (a
# desk that is a tab of another page, like Lesson, shares its server); `pane` is the same thing in the pane.
DESKS = [
    {"name": "decisions", "title": "Decisions", "server": "decisions", "anchor": "", "key": "1",
     "pane": "fleet decisions tui"},
    {"name": "learn", "title": "Learn", "server": "learn", "anchor": "", "key": "2",
     "pane": "fleet learn lessons; fleet learn quiz"},
    {"name": "lesson", "title": "Lesson", "server": "learn", "anchor": "#lesson", "key": "3",
     "pane": "fleet learn lesson"},
    {"name": "watch", "title": "Watch", "server": "watch", "anchor": "", "key": "4",
     "pane": "fleet watch status"},
]
# The servers: the file each writes its URL to, its default port and how the pane starts it.
SERVERS = {
    "decisions": {"urlfile": "decisions-ui.url", "port": 7340, "argv": ["decisions", "serve"]},
    "learn": {"urlfile": "learn-ui.url", "port": 7341, "argv": ["learn", "serve"]},
    "watch": {"urlfile": "watch-ui.url", "port": 7342, "argv": ["watch", "serve"]},
}
# Q698 for the app: every app action and the pane verb that does the same.
APP_ACTIONS = {
    "Desks menu / home page Open": "fleet app urls, then the desk's own pane command",
    "start missing desk servers": "fleet app serve",
    "native notification": "fleet app notify",
    "phone tunnel on the home page": "fleet app tunnel",
}
# Every #[tauri::command] in app/desk-ui/src-tauri/src/main.rs and the `fleet app` verb it runs (test_app.py
# checks main.rs against this, so an app-only command can't slip in).
COMMANDS = {"desks": "urls|serve", "open_desk": "serve", "tunnel": "tunnel"}
VERBS = ("urls", "serve", "notify", "tunnel", "build", "open")
SEEN_FILE = "app-notified.json"
MAX_BURST = 4  # more new items than this in one poll become one summary notification
PROBE_TIMEOUT = 1.5
START_WAIT = 8.0


class NotHuman(PermissionError):
    pass


def human_only(verb):
    role = os.environ.get("FLEET_ROLE") or ""
    if role:
        raise NotHuman("role %s can't %s: Fleet Desk and its URLs are the human's" % (role, verb))


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _state(name):
    return os.path.join(mesh.state_dir(), name)


# ---------------------------------------------------------------- urls / serve

def read_url(server):
    try:
        with open(_state(SERVERS[server]["urlfile"])) as f:
            u = f.read().strip()
    except OSError:
        return None
    return u if u.startswith("http://127.0.0.1:") or u.startswith("http://[::1]:") else None


def port_of(url):
    try:
        return urllib.parse.urlsplit(url).port
    except ValueError:
        return None


def configured_port(server):
    """The port `fleet app serve` starts the server on: $FLEET_STATE/app.json {"ports": {server: N}}, else the
    server's own config (decisions-ui.json port, learn/learn.json servePort), else its default."""
    try:
        with open(_state("app.json")) as f:
            p = (json.load(f).get("ports") or {}).get(server)
        if p:
            return int(p)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    try:
        if server == "decisions":
            with open(_state("decisions-ui.json")) as f:
                return int(json.load(f).get("port") or SERVERS[server]["port"])
        if server == "learn":
            with open(_state(os.path.join("learn", "learn.json"))) as f:
                return int(json.load(f).get("servePort") or SERVERS[server]["port"])
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return SERVERS[server]["port"]


def alive(url):
    """The page answers 200 with this URL's token (a stale file from a dead server, or another server that took
    the port, does not)."""
    if not url:
        return False
    try:
        with urllib.request.urlopen(url.split("#")[0], timeout=PROBE_TIMEOUT) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def desks():
    up = {s: read_url(s) for s in SERVERS}
    ok = {s: alive(u) for s, u in up.items()}
    out = []
    for d in DESKS:
        u = up[d["server"]]
        out.append({"name": d["name"], "title": d["title"], "key": d["key"], "pane": d["pane"],
                    "server": d["server"], "url": (u + d["anchor"]) if u else None, "alive": ok[d["server"]]})
    return out


def serve(wait=START_WAIT):
    """Start every desk server that is not answering, detached, its output in $FLEET_STATE/app-<server>.log."""
    human_only("start the desk servers")
    fleet = os.path.join(ROOT, "bin", "fleet")
    started = []
    for s, spec in SERVERS.items():
        if alive(read_url(s)):
            continue
        log = open(_state("app-%s.log" % s), "a")
        env = dict(os.environ)
        env.pop("FLEET_ROLE", None)
        subprocess.Popen([fleet] + spec["argv"] + ["--port", str(configured_port(s))], stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                         start_new_session=True, close_fds=True)
        started.append(s)
    deadline = time.time() + wait
    while started and time.time() < deadline:
        started = [s for s in started if not alive(read_url(s))]
        if started:
            time.sleep(0.25)
    return desks()


# ---------------------------------------------------------------- notify

def _load_seen(f):
    try:
        f.seek(0)
        data = json.loads(f.read() or "{}")
        return data.get("seen") or {} if isinstance(data, dict) else {}
    except ValueError:
        return {}


def open_calls():
    import fleet_decisions as fd
    cards, _ = fd.open_cards(include_snoozed=False)
    return cards


def waiting_lessons():
    import fleet_learn_lesson as les
    return les.lessons()


def _decisions_notifies_itself():
    """`fleet decisions serve` already notifies new calls when decisions-ui.json sets notifyCommand; the app
    then stays quiet on calls (still marking them) so a call never fires twice."""
    try:
        import fleet_decisions as fd
        cfg = fd.load_config()
    except Exception:
        return False
    return bool(cfg.get("notifyCommand")) and cfg.get("notify", True) and alive(read_url("decisions"))


def candidates():
    """Every item that may notify now: [(id, title, body)]. A source that fails is skipped, never fatal."""
    out = []
    try:
        for c in open_calls():
            q = (c.get("question") or "").strip().split("\n")[0]
            out.append(("call:%s" % c["id"], "DECIDE: %s from %s" % (c["id"], c.get("from") or "?"), q[:200]))
    except Exception as e:
        mesh.log("fleet app notify: calls skipped: %s" % e)
    try:
        for t in waiting_lessons():
            out.append(("lessons:%s" % t["topic"], "LEARN: lesson waiting on %s" % t["topic"], list(t["lids"])))
    except Exception as e:
        mesh.log("fleet app notify: lessons skipped: %s" % e)
    return out


def notify(dry_run=False):
    """The new items since the last call, each once: [{id, title, body}]. Calls key on the call id; lessons key on
    each lesson id (a topic notifies again only when it gets a lesson not notified before)."""
    human_only("take Fleet Desk notifications")
    path = _state(SEEN_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd_ = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd_, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        seen = _load_seen(f)
        quiet_calls = _decisions_notifies_itself()
        notes, mark = [], []
        for cid, title, body in candidates():
            if cid.startswith("lessons:"):
                new = [l for l in body if "lesson:%s" % l not in seen]
                if not new:
                    continue
                mark += ["lesson:%s" % l for l in new]
                notes.append({"id": "%s:%s" % (cid, new[0]), "title": title,
                              "body": "%d new lesson row(s). Open Lesson, or in the pane: fleet learn lesson --topic %s"
                                      % (len(new), cid.split(":", 1)[1])})
            elif cid not in seen:
                mark.append(cid)
                if not quiet_calls:
                    notes.append({"id": cid, "title": title, "body": body})
        if len(notes) > MAX_BURST:
            calls = len([n for n in notes if n["id"].startswith("call:")])
            notes = [{"id": "summary:%s" % _now(), "title": "Fleet Desk",
                      "body": "%d new call(s) for DECIDE, %d topic(s) with lessons waiting" % (
                          calls, len(notes) - calls)}]
        if mark and not dry_run:
            at = _now()
            for m in mark:
                seen[m] = at
            f.seek(0)
            f.truncate()
            f.write(json.dumps({"seen": seen}, indent=1, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())
    return notes


# ---------------------------------------------------------------- tunnel / build / open

def tunnel(host="<user>@<mac>"):
    """The one command for the phone. Same port on both ends, bound to 127.0.0.1 on both ends: the pages check
    the Host header against 127.0.0.1:<their port>, and nothing listens beyond loopback."""
    human_only("print the desk URLs")
    ports = []
    for s in SERVERS:
        p = port_of(read_url(s) or "") or configured_port(s)
        ports.append((s, p))
    fwd = " ".join("-L 127.0.0.1:%d:127.0.0.1:%d" % (p, p) for _, p in ports)
    lines = ["ssh -N %s %s" % (fwd, host), "", "then open on the phone (tokens change when a server restarts):"]
    for d in desks():
        lines.append("  %-9s %s" % (d["title"], d["url"] or "(not running: fleet app serve)"))
    return "\n".join(lines)


def app_path():
    target = os.environ.get("CARGO_TARGET_DIR") or os.path.join(
        os.path.expanduser("~"), ".cache", "fleet-desk", "target")
    return os.path.join(target, "release", "bundle", "macos", "Fleet Desk.app")


def build():
    """Unsigned local .app. The cargo target dir stays outside the repo (the privacy lint scans the whole tree)."""
    bunx = shutil.which("bunx")
    if not bunx:
        raise RuntimeError("bunx not found: install Bun (https://bun.sh)")
    env = dict(os.environ)
    env.setdefault("CARGO_TARGET_DIR", os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        app_path())))))
    rc = subprocess.call([bunx, "@tauri-apps/cli@2", "build", "--bundles", "app"], cwd=APP_DIR, env=env)
    if rc == 0:
        print("OK built %s" % app_path())
    return rc


def open_app():
    p = app_path()
    if not os.path.isdir(p):
        raise RuntimeError("%s not built yet: fleet app build" % p)
    return subprocess.call(["open", "-g", p])


# ---------------------------------------------------------------- cli

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(prog="fleet app", description="Fleet Desk, the Mac app, from the pane")
    sub = p.add_subparsers(dest="verb")
    for v in ("urls", "serve"):
        sub.add_parser(v).add_argument("--json", action="store_true")
    n = sub.add_parser("notify")
    n.add_argument("--json", action="store_true")
    n.add_argument("--dry-run", action="store_true")
    sub.add_parser("tunnel").add_argument("host", nargs="?", default="<user>@<mac>")
    sub.add_parser("build")
    sub.add_parser("open")
    a = p.parse_args(argv)
    if not a.verb:
        p.print_help()
        return 2
    try:
        if a.verb in ("urls", "serve"):
            if a.verb == "urls":
                human_only("print the desk URLs")
            rows = serve() if a.verb == "serve" else desks()
            if a.json:
                print(json.dumps(rows))
            else:
                for d in rows:
                    print("%-9s %-4s %s   pane: %s" % (d["title"], "up" if d["alive"] else "down",
                                                      d["url"] or "-", d["pane"]))
        elif a.verb == "notify":
            notes = notify(dry_run=a.dry_run)
            if a.json:
                print(json.dumps(notes))
            else:
                for x in notes:
                    print("%s  %s" % (x["title"], x["body"]))
        elif a.verb == "tunnel":
            print(tunnel(a.host))
        elif a.verb == "build":
            return build()
        elif a.verb == "open":
            return open_app()
    except PermissionError as e:
        sys.stderr.write("FAIL %s\n" % e)
        return 4
    except RuntimeError as e:
        sys.stderr.write("FAIL %s\n" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
