#!/usr/bin/env python3
"""fleet ui serve: one loopback page for every desk, on one port with one token (Q700).

  fleet ui serve [--port P] [--bind 127.0.0.1|::1] [--open] [--tab decide|learn|watch|route]
  fleet ui url [--json] [--tab T]     the hub URL (token included) when the hub answers, else exit 3

Tabs, each a mount of the desk page's own handler (the hub forks none of their code or HTML):
  /decide/   the DECISIONS page (fleet_decisions.make_handler, with its Hub thread for live updates and notify)
  /learn/    the LEARN page (fleet_learn_serve.make_handler)
  /watch/    the WATCH page (fleet_watch_serve.make_handler)
  /route/    the ROUTE board: the roster and the last answers in decisions-log.jsonl, read only
  /          redirects to /decide/

`fleet decisions serve`, `fleet learn serve` and `fleet watch serve` are aliases (bin/fleet): when the hub answers they
print its URL for their tab and exit; otherwise they start the hub. The standalone page servers stay reachable as
modules (python3 lib/fleet_learn_serve.py ...) for their own tests.

Security: the same rules as each page. Loopback bind only; a fresh token per launch, 403 without it; a GET takes it
as ?t= or the X-Fleet-Token header, a POST takes the header only, checks Host and refuses a cross-origin Origin.
The guard is LEARN's human_only (fleet_learn_serve.human_only): any FLEET_ROLE is refused (exit 4). One token opens
every tab, DECIDE answers included, so no lane role serves it, not even decide or watch.

The URL goes to $FLEET_STATE/ui.url (mode 0600) and stdout; the file is removed on exit.
Port: --port, else $FLEET_STATE/app.json {"ports": {"ui": N}}, else 7343 (the standalone pages use 7340-7342).
Python 3.8+ stdlib only.
"""
import argparse
import hmac
import html
import http.server
import json
import os
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.realpath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import fleet_mesh as mesh  # noqa: E402

LOOPBACK = ("127.0.0.1", "::1", "localhost")
DEFAULT_PORT = 7343
URL_FILE = "ui.url"
# tab -> (title, the old serve command's banner word). Menu order.
TABS = {
    "decide": ("DECIDE", "DECISIONS"),
    "learn": ("LEARN", "LEARN"),
    "watch": ("WATCH", "WATCH"),
    "route": ("ROUTE", "ROUTE"),
}
# the alias names bin/fleet passes (fleet <desk> serve) -> tab
ALIAS = {"decisions": "decide", "decide": "decide", "learn": "learn", "watch": "watch", "route": "route"}
ROUTE_LOG_ROWS = 30
PROBE_TIMEOUT = 1.5


def human_only(verb):
    """The one guard (LEARN's, Q695): any FLEET_ROLE is refused."""
    import fleet_learn_serve as fls
    try:
        fls.human_only(verb)
    except PermissionError:
        raise PermissionError("role %s can't %s: the fleet UI hub and its one token are the human's" % (
            os.environ.get("FLEET_ROLE"), verb))


def _state(name):
    return os.path.join(mesh.state_dir(), name)


def configured_port():
    try:
        with open(_state("app.json")) as f:
            p = (json.load(f).get("ports") or {}).get("ui")
        if p:
            return int(p)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return DEFAULT_PORT


def tab_url(base, tab):
    """http://127.0.0.1:P/?t=X -> http://127.0.0.1:P/<tab>/?t=X"""
    u = urllib.parse.urlsplit(base)
    return urllib.parse.urlunsplit((u.scheme, u.netloc, "/%s/" % tab, u.query, ""))


def read_url():
    try:
        with open(_state(URL_FILE)) as f:
            u = f.read().strip()
    except OSError:
        return None
    return u if u.startswith("http://127.0.0.1:") or u.startswith("http://[::1]:") else None


def alive(url):
    """The hub answers 200 on /route/ with this URL's token (a stale file from a dead hub does not)."""
    if not url:
        return False
    try:
        with urllib.request.urlopen(tab_url(url, "route"), timeout=PROBE_TIMEOUT) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


# ---------------------------------------------------------------- the ROUTE board (read only)

def route_state(limit=ROUTE_LOG_ROWS):
    roster = mesh.load_roster()
    lanes = []
    for name in sorted(roster):
        e = roster[name] if isinstance(roster[name], dict) else {}
        lanes.append({"name": name, "role": e.get("role") or "", "cli": e.get("cli") or "",
                      "status": e.get("status") or "", "pane": e.get("pane") or "",
                      "goal": e.get("goal") or e.get("goalFile") or "", "dir": e.get("dir") or ""})
    rows = []
    try:
        with open(mesh.decisions_log_path()) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    rows.append({"id": str(r.get("id") or ""), "at": str(r.get("at") or ""),
                                 "asker": str(r.get("asker") or ""), "verbatim": str(r.get("verbatim") or ""),
                                 "by": str(r.get("by") or "")})
    except OSError:
        pass
    return {"lanes": lanes, "log": rows[-limit:][::-1]}


def _tab_strip(active, token):
    q = "?t=" + urllib.parse.quote(token)
    links = []
    for t, (title, _) in TABS.items():
        cls = ' class="on"' if t == active else ""
        links.append('<a%s href="/%s/%s">%s</a>' % (cls, t, q, title))
    return ('<nav id="fleet-tabs" style="display:flex;gap:4px;padding:6px 10px;background:#111827;'
            'font:13px system-ui,sans-serif;position:sticky;top:0;z-index:99">%s</nav>'
            '<style>#fleet-tabs a{color:#cbd5e1;text-decoration:none;padding:4px 12px;border-radius:6px}'
            '#fleet-tabs a.on{background:#334155;color:#fff}</style>' % "".join(links))


def route_page(token):
    s = route_state()
    esc = html.escape
    lane_rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
        esc(l["name"]), esc(l["role"]), esc(l["cli"]), esc(l["status"]), esc(l["goal"] or l["dir"]))
        for l in s["lanes"]) or '<tr><td colspan="5">no lanes in the roster</td></tr>'
    log_rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
        esc(r["at"]), esc(r["id"]), esc(r["asker"]), esc(r["verbatim"][:300]))
        for r in s["log"]) or '<tr><td colspan="4">no answers yet</td></tr>'
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><title>Fleet ROUTE board</title>"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<style>body{margin:0;font:14px system-ui,sans-serif;background:#0b1020;color:#e2e8f0}"
            "main{padding:12px 16px}table{border-collapse:collapse;width:100%%;margin-bottom:20px}"
            "td,th{border-bottom:1px solid #1e293b;padding:4px 8px;text-align:left;vertical-align:top}"
            "th{color:#94a3b8;font-weight:600}h2{font-size:15px;color:#94a3b8}</style></head><body>%s<main>"
            "<h2>Lanes (roster.json, %d)</h2><table><tr><th>name</th><th>role</th><th>cli</th><th>status</th>"
            "<th>goal / dir</th></tr>%s</table>"
            "<h2>Last answers (decisions-log.jsonl)</h2><table><tr><th>at</th><th>id</th><th>asker</th>"
            "<th>answer</th></tr>%s</table>"
            "<p style=\"color:#64748b\">read only; in the pane: fleet ls, fleet tick</p></main></body></html>" % (
                _tab_strip("route", token), len(s["lanes"]), lane_rows, log_rows))


# ---------------------------------------------------------------- page HTML with the tab strip

PAGES = {"decide": "decisions-ui.html", "learn": "learn-ui.html", "watch": "watch-ui.html"}
PAGE_PATHS = {"decide": ("/", "/refresh"), "learn": ("/",), "watch": ("/",)}


def tab_page(tab, token):
    """The desk's own HTML with BASE set (the page prefixes its fetch, EventSource and path checks with it) and the
    tab strip after <body>."""
    with open(os.path.join(HERE, PAGES[tab])) as f:
        page = f.read()
    inject = "<script>window.FLEET_BASE=%s;</script>%s" % (json.dumps("/" + tab), _tab_strip(tab, token))
    i = page.find("<body")
    j = page.find(">", i) + 1 if i >= 0 else 0
    return page[:j] + inject + page[j:]


# ---------------------------------------------------------------- the server

HEADERS = (("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"))
CSP = ("default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
       "frame-ancestors 'none'")


def make_handler(token, allowed_hosts, mounts):
    """mounts: tab -> the desk page's own handler class (built with this token and allowed_hosts). A request under
    /<tab>/ runs that class's do_GET/do_POST on this same connection object with the prefix stripped: the class is
    swapped for that one request and restored after, so a keep-alive connection can move between tabs."""

    class H(http.server.BaseHTTPRequestHandler):
        server_version = "fleet-ui"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # never log the token (it rides in the query string)
            mesh.log("fleet-ui %s %s" % (self.command, urllib.parse.urlsplit(self.path).path))

        def _send(self, code, body, ctype="application/json", extra=()):
            data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode(
                "utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            for k, v in HEADERS:
                self.send_header(k, v)
            if ctype == "text/html":
                self.send_header("Content-Security-Policy", CSP)
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self):
            return (self.headers.get("Host") or "") in allowed_hosts

        def _token_ok(self, allow_query):
            got = self.headers.get("X-Fleet-Token") or ""
            if not got and allow_query:
                got = (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("t") or [""])[0]
            return bool(got) and hmac.compare_digest(got.encode("utf-8"), token.encode("utf-8"))

        def _split(self):
            """(tab, path inside the tab, query) or (None, path, query)."""
            u = urllib.parse.urlsplit(self.path)
            parts = u.path.split("/", 2)  # "", tab, rest
            if len(parts) >= 2 and parts[1] in TABS:
                return parts[1], "/" + (parts[2] if len(parts) > 2 else ""), u.query
            return None, u.path, u.query

        def _mount(self, tab, inner, query, verb):
            cls = mounts.get(tab)
            if cls is None:
                return self._send(503, {"error": "the %s page is not available in this hub" % tab})
            mine = self.__class__
            self.path = inner + ("?" + query if query else "")
            self.__class__ = cls
            try:
                getattr(cls, verb)(self)
            finally:
                self.__class__ = mine

        def do_GET(self):
            if not self._host_ok() or not self._token_ok(allow_query=True):
                return self._send(403, {"error": "forbidden"})
            tab, inner, query = self._split()
            if tab is None:
                if inner == "/":
                    return self._send(302, b"", "text/plain",
                                      extra=(("Location", "/decide/" + ("?" + query if query else "")),))
                return self._send(404, {"error": "not found"})
            if tab == "route":
                if inner == "/":
                    return self._send(200, route_page(token), "text/html")
                if inner == "/api/state":
                    return self._send(200, route_state())
                return self._send(404, {"error": "not found"})
            if inner in PAGE_PATHS.get(tab, ()):
                try:
                    return self._send(200, tab_page(tab, token), "text/html")
                except OSError as e:
                    return self._send(500, {"error": str(e)})
            return self._mount(tab, inner, query, "do_GET")

        def do_POST(self):
            origin = self.headers.get("Origin")
            if not self._host_ok() or not self._token_ok(allow_query=False) or (
                    origin and origin != "http://%s" % self.headers.get("Host")):
                return self._send(403, {"error": "forbidden"})
            tab, inner, query = self._split()
            if tab is None or tab == "route":
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                return self._send(404, {"error": "not found"})
            return self._mount(tab, inner, query, "do_POST")

    return H


def build_mounts(token, allowed, url_for):
    """The three desk page handlers, each built the way its own serve() builds it. A page whose core is missing
    is left out (its tab answers 503) instead of taking the hub down. Returns (mounts, decisions Hub or None)."""
    mounts, dhub = {}, None
    try:
        import fleet_decisions as fd
        dhub = fd.Hub(fd.load_config(), lambda cid=None: url_for("decide", cid))
        mounts["decide"] = fd.make_handler(token, dhub, allowed)
    except Exception as e:  # noqa: BLE001 - one page failing must not stop the others
        mesh.log("fleet-ui: decide tab off: %s" % e)
    try:
        import fleet_learn_serve as fls
        mounts["learn"] = fls.make_handler(token, allowed)
    except Exception as e:  # noqa: BLE001
        mesh.log("fleet-ui: learn tab off: %s" % e)
    try:
        import fleet_watch_serve as fws
        mounts["watch"] = fws.make_handler(token, fws.load_watch(), fws.state_dir(), allowed)
    except Exception as e:  # noqa: BLE001
        mesh.log("fleet-ui: watch tab off: %s" % e)
    return mounts, dhub


def banner(url, tab=None):
    lines = []
    if tab:
        lines.append("%s UI: %s" % (TABS[tab][1], tab_url(url, tab)))
    lines.append("FLEET UI: %s" % url)
    for t in TABS:
        lines.append("  %-6s %s" % (t, tab_url(url, t)))
    return "\n".join(lines) + "\n"


def serve(port, bind, open_page, tab=None, out=sys.stdout):
    human_only("serve the fleet UI hub")
    if bind not in LOOPBACK:
        raise ValueError("--bind %s refused: the fleet UI binds loopback only (127.0.0.1 or ::1)" % bind)
    if port is None:
        port = configured_port()
    token = secrets.token_urlsafe(24)
    srv_cls = http.server.ThreadingHTTPServer
    if bind == "::1":
        class V6(srv_cls):
            address_family = socket.AF_INET6
        srv_cls = V6
    host = "[::1]" if bind == "::1" else "127.0.0.1"
    holder = {}

    def url_for(t=None, cid=None):
        base = "http://%s:%d/?t=%s" % (host, holder["port"], token)
        return (tab_url(base, t) if t else base) + ("#" + cid if cid else "")

    allowed = set()  # filled once the port is known
    mounts, dhub = build_mounts(token, allowed, url_for)
    try:
        srv = srv_cls(("localhost" if bind == "localhost" else bind, port), make_handler(token, allowed, mounts))
    except OSError as e:
        raise ValueError("can't listen on %s:%s (%s); pick another --port or set ports.ui in app.json" % (
            bind, port, e.strerror or e))
    srv.daemon_threads = True
    holder["port"] = srv.server_address[1]
    for h in ("127.0.0.1", "localhost", "[::1]"):
        allowed.add("%s:%d" % (h, holder["port"]))
    url = url_for()

    def _term(signum, frame):  # SIGTERM cleans up like Ctrl-C: the URL file goes away. Set before the file is
        raise KeyboardInterrupt  # written, so a kill right after the banner still removes it.
    signal.signal(signal.SIGTERM, _term)
    urlfile = _state(URL_FILE)
    os.makedirs(os.path.dirname(urlfile), exist_ok=True)
    fd_ = os.open(urlfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd_, "w") as f:
        f.write(url + "\n")
    stop = threading.Event()
    try:  # everything after the URL file is written runs inside this, so any SIGTERM removes the file
        out.write(banner(url, tab))
        out.flush()
        if dhub is not None:
            dhub.tick()
            threading.Thread(target=dhub.loop, args=(stop,), daemon=True).start()
        if open_page:
            opener = os.environ.get("FLEET_OPEN_CMD") or ("open -g" if sys.platform == "darwin" else "xdg-open")
            try:
                subprocess.Popen(shlex.split(opener) + [url_for(tab)], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except OSError as e:
                sys.stderr.write("could not open a browser (%s); the URL is above\n" % e)
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _cleanup(srv, stop, urlfile)
    return 0


def _cleanup(srv, stop, urlfile):
    stop.set()
    srv.server_close()
    try:
        os.remove(urlfile)
    except OSError:
        pass


def alias(desk, argv):
    """`fleet <desk> serve [args]`: the hub's URL for that tab when the hub answers, else start the hub."""
    tab = ALIAS[desk]
    human_only("serve the %s page" % TABS[tab][0])
    url = read_url()
    if alive(url):
        sys.stdout.write("%s UI: %s\n(the fleet UI hub is already running: %s)\n" % (TABS[tab][1], tab_url(url, tab),
                                                                                     url))
        return 0
    return main(["serve", "--tab", tab] + list(argv))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["alias"] and len(argv) >= 2 and argv[1] in ALIAS:
        try:
            return alias(argv[1], argv[2:])
        except PermissionError as e:
            sys.stderr.write("FAIL %s\n" % e)
            return 4
    ap = argparse.ArgumentParser(prog="fleet ui", description="one loopback page for every desk (one port, one token)")
    sub = ap.add_subparsers(dest="verb")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=None, help="port (default %d; 0 picks a free one)" % DEFAULT_PORT)
    s.add_argument("--bind", default="127.0.0.1", help="127.0.0.1 (default), ::1 or localhost")
    s.add_argument("--open", action="store_true", help="open the page in the browser")
    s.add_argument("--tab", choices=list(TABS), help="print (and open) this tab's URL first")
    u = sub.add_parser("url")
    u.add_argument("--json", action="store_true")
    u.add_argument("--tab", choices=list(TABS))
    a = ap.parse_args(argv)
    if not a.verb:
        ap.print_help()
        return 2
    try:
        if a.verb == "serve":
            return serve(a.port, a.bind, a.open, a.tab)
        human_only("print the fleet UI URL")
        url = read_url()
        if not alive(url):
            sys.stderr.write("fleet ui: the hub is not running (fleet ui serve)\n")
            return 3
        if a.json:
            print(json.dumps({"url": url, "tabs": {t: tab_url(url, t) for t in TABS}}))
        else:
            print(tab_url(url, a.tab) if a.tab else url)
        return 0
    except PermissionError as e:
        sys.stderr.write("FAIL %s\n" % e)
        return 4
    except ValueError as e:
        sys.stderr.write("FAIL %s\n" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
