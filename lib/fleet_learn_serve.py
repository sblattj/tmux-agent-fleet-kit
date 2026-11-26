#!/usr/bin/env python3
"""fleet learn serve: the LEARN desk's web page (phase 7, docs/design/four-desks.md 4.5 and 4.6).

  fleet learn serve [--port P] [--bind 127.0.0.1|::1] [--open]   loopback page with a per-launch token

The page is a superset of the pane (Q698): every `fleet learn` verb has a page route (PANE_ACTIONS), plus graphics
(score sparklines, the term map, waiting lessons per topic). It shows proof per origin (never blended), score history
per topic, the heard-only rows, recent reviews, the lesson for any waiting topic, a session on one topic, the same
quiz as `fleet learn quiz`, and a box that runs any other pane command and shows its text. It reads fleet_learn.snapshot() and answers through fleet_learn.review(),
the verb the terminal quiz calls, so an answer in the page and one in the TUI end in the same state.
Writes nothing itself except $FLEET_STATE/learn-ui.url (mode 0600); every learn/ write goes through fleet_learn.

Security, the same as `fleet decisions serve`: loopback bind only, a fresh token per launch, 403 without it.
GET takes the token as ?t= or the X-Fleet-Token header; POST takes the header only (a link click can't answer),
checks Host, and refuses a cross-origin Origin. Only the human serves it: a lane role (FLEET_ROLE set) is refused
(exit 4, as in `fleet decisions serve`), since a lane answering quiz cards would fake proof of learning.

Port: --port, else learn.json {"servePort": N}, else 7341 (decisions serve uses 7340). Python 3.8+ stdlib only.
"""
import argparse
import contextlib
import datetime
import io
import hmac
import http.server
import json
import os
import secrets
import subprocess
import sys
import threading
import urllib.parse

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import fleet_mesh as mesh  # noqa: E402
import fleet_learn as fl  # noqa: E402

HTML = os.path.join(HERE, "learn-ui.html")
LOOPBACK = ("127.0.0.1", "::1", "localhost")
DEFAULT_PORT = 7341
HISTORY_DAYS = 60


class NotDue(Exception):
    pass


def human_only(verb):
    """One guard for the page and the CLI (fleet_learn.human_only, Q695): any FLEET_ROLE is refused."""
    fl.human_only(verb)


# Q698: the page is a SUPERSET of the pane. Every `fleet learn` verb (fleet_learn.cli_verbs()) has a page route here,
# and the pane keeps its full text version (the phone over mosh). test_learn_serve.py checks both directions.
# quiz and session read stdin in the pane, so the page has its own routes for them; every other verb also runs
# as-is through POST /api/cli, the same fleet_learn.main() the pane calls, with its text output shown in the page.
PANE_ACTIONS = {
    "serve": "GET /",
    "snapshot": "GET /api/state",
    "due": "GET /api/state",
    "lessons": "GET /api/lessons",
    "lesson": "GET /api/lessons?topic= + POST /api/seen",
    "session": "GET /api/session?topic= + POST /api/seen + POST /api/review",
    "quiz": "POST /api/review + POST /api/reveal",
    "review": "POST /api/review",
}
INTERACTIVE = ("quiz", "session", "serve")  # stdin or the page itself: dedicated routes only
_CLI_LOCK = threading.Lock()


def cli_verbs():
    return [v for v in fl.cli_verbs() if v not in INTERACTIVE]


def actions():
    """GET /api/actions: every pane verb and the page route that does it."""
    out = {v: PANE_ACTIONS.get(v, "POST /api/cli") for v in fl.cli_verbs()}
    return {"actions": out, "cli": cli_verbs()}


def cli(argv):
    """POST /api/cli {argv}: one pane verb run through fleet_learn.main(), output captured as text. The human's
    guards apply as in the pane (the page refuses to start under a role)."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise fl.Refused("argv must be a non-empty list of strings")
    if argv[0] not in cli_verbs():
        raise fl.Refused("%s is not a page command (use its own route: %s)" % (
            argv[0], PANE_ACTIONS.get(argv[0], "none")))
    o, e = io.StringIO(), io.StringIO()
    with _CLI_LOCK, contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
        try:
            rc = fl.main(list(argv))
        except SystemExit as x:  # argparse usage errors
            rc = x.code if isinstance(x.code, int) else 2
    return {"argv": argv, "rc": rc or 0, "out": o.getvalue(), "err": e.getvalue()}


def lessons_state(now=None, topic=None):
    """GET /api/lessons[?topic=T]: the topics waiting, and the first one (or T) as a preview model (nothing recorded)."""
    import fleet_learn_lesson as les
    ls = les.lessons()
    pick = [d for d in ls if d["topic"] == topic] if topic else ls
    first = None
    if pick:
        rows = [fl.item(l) for l in pick[0]["lids"]][:les.MAX_ROWS]
        first = les.lesson_model(rows)
        first["topic"] = pick[0]["topic"]
    return {"waiting": ls, "lesson": first}


def session_state(topic, since=None, now=None):
    """GET /api/session?topic=T[&since=ISO]: the pane's `session --topic T` as a page model: T's lesson (preview),
    the cards due on T only, and the same one result line, counting the reviews on T since the session started."""
    if not topic:
        raise fl.Refused("session needs a topic")
    lesson = lessons_state(now, topic=topic)["lesson"]
    if lesson and lesson.get("topic") != topic:
        lesson = None
    quiz = [_public(c) for c in fl.due(now, topic=topic)]
    lids = {r["lid"] for r in fl.items() if fl.on_topic(r, topic)}
    revs = [r for r in fl.reviews() if r.get("lid") in lids and (not since or (r.get("at") or "") >= since)]
    pf = fl.rebuild_proof()
    learned = len([l for l in lids if (pf.get(l) or {}).get("learned")])
    p = len([r for r in revs if r.get("grade") == "pass"])
    seen_n = len([r for r in fl.seen_rows() if r.get("lid") in lids and (not since or (r.get("at") or "") >= since)])
    line = "session %s: %d lesson(s), %d card(s), %d pass, %d fail, %d learned" % (
        topic, seen_n, len(revs), p, len(revs) - p, learned)
    return {"topic": topic, "lesson": lesson, "quiz": quiz, "line": line}


def seen(lids):
    """POST /api/seen {lids}: record the lesson the page showed as seen (the 24 h clock starts here)."""
    human_only("mark lessons seen")
    if not isinstance(lids, list) or not lids or not all(isinstance(x, str) for x in lids):
        raise fl.Refused("lids must be a non-empty list of row ids")
    live = {r["lid"] for r in fl.items()}
    bad = [x for x in lids if x not in live]
    if bad:
        raise fl.Refused("no row %s" % ", ".join(bad))
    return {"marked": [r["lid"] for r in fl.mark_seen(lids, via="page")]}


# ---------------------------------------------------------------- read side

def _topic_of():
    """lid -> (topic, origin), including retired rows so old reviews still land in their topic."""
    out = {}
    for r in fl.items(include_retired=True):
        out[r["lid"]] = (r.get("topic") or "", r.get("origin"))
    return out


def history(now=None):
    """Score history per topic and origin: one point per day with reviews, {day, reviews, passes, score}.
    score = passes / reviews that day, as a percent. Built from reviews.jsonl (the record), read only."""
    now = fl._now(now)
    since = (now - datetime.timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    topic = _topic_of()
    buckets = {}
    for rv in fl.reviews():
        at, lid = rv.get("at") or "", rv.get("lid")
        if not at or lid not in topic or at[:10] < since:
            continue
        key = topic[lid]
        day = buckets.setdefault(key, {}).setdefault(at[:10], {"day": at[:10], "reviews": 0, "passes": 0})
        day["reviews"] += 1
        day["passes"] += 1 if rv.get("grade") == "pass" else 0
    out = []
    for (t, o), days in sorted(buckets.items(), key=lambda kv: (kv[0][1] or "", kv[0][0])):
        pts = [days[d] for d in sorted(days)]
        for p in pts:
            p["score"] = int(round(100.0 * p["passes"] / p["reviews"]))
        out.append({"topic": t, "origin": o, "points": pts})
    return out


def _public(c):
    """A due card as the page sees it before answering: never the answer or its quote."""
    v = {k: c.get(k) for k in ("cid", "lid", "format", "origin", "prompt")}
    if c.get("format") == "choice":
        v["options"] = list(c.get("options") or [])
    return v  # the source (a lane or ref) shows only after grading, in answer() / reveal()


def state(now=None):
    return {"snapshot": fl.snapshot(now), "history": history(now), "quiz": [_public(c) for c in fl.due(now)]}


def _due_card(cid):
    for c in fl.due():
        if c.get("cid") == cid:
            return c
    fl.card(cid)  # raises Refused "no card" for an unknown id
    raise NotDue("%s is not due" % cid)


# ---------------------------------------------------------------- verbs (the same ones the TUI uses)

def reveal(cid):
    """Recall only, after the attempt: the TUI prints the source quote before asking for a rating."""
    c = _due_card(cid)
    if c.get("format") != "recall":
        raise fl.Refused("only recall cards reveal before grading (%s is %s)" % (cid, c.get("format")))
    return {"cid": cid, "answer": c.get("answer"), "source": (c.get("grounding") or {}).get("source")}


def answer(cid, text, rating=None, latency_ms=None):
    """One quiz answer through fleet_learn.review(). Only due cards, like the TUI, so a double submit is a 409."""
    c = _due_card(cid)
    if c.get("format") == "recall" and rating not in fl.RATINGS:
        raise fl.Refused("recall needs a rating: again, hard, good or easy")
    lm = latency_ms if isinstance(latency_ms, int) and latency_ms >= 0 else None
    row = fl.review(cid, text or "", rating=rating if c.get("format") == "recall" else None, latencyMs=lm)
    return {"review": row, "answer": c.get("answer"), "source": (c.get("grounding") or {}).get("source")}


# ---------------------------------------------------------------- web server

def make_handler(token, allowed_hosts):
    with open(HTML) as f:
        page = f.read().encode("utf-8")

    class H(http.server.BaseHTTPRequestHandler):
        server_version = "fleet-learn"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # never log the token (it rides in the query string)
            mesh.log("learn-ui %s %s" % (self.command, urllib.parse.urlsplit(self.path).path))

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if ctype == "text/html":
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                 "connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self):
            return (self.headers.get("Host") or "") in allowed_hosts

        def _token_ok(self, allow_query):
            got = self.headers.get("X-Fleet-Token") or ""
            if not got and allow_query:
                got = (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("t") or [""])[0]
            return bool(got) and hmac.compare_digest(got, token)

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if not self._host_ok() or not self._token_ok(allow_query=True):
                return self._send(403, {"error": "forbidden"})
            try:
                if path == "/":
                    return self._send(200, page, "text/html")
                if path == "/api/state":
                    return self._send(200, state())
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                if path == "/api/lessons":
                    return self._send(200, lessons_state(topic=(q.get("topic") or [None])[0]))
                if path == "/api/session":
                    return self._send(200, session_state((q.get("topic") or [""])[0], (q.get("since") or [None])[0]))
                if path == "/api/actions":
                    return self._send(200, actions())
            except fl.Refused as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                mesh.log("learn-ui %s error: %s" % (path, e))
                return self._send(500, {"error": str(e)})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            origin = self.headers.get("Origin")
            if not self._host_ok() or not self._token_ok(allow_query=False) or (
                    origin and origin != "http://%s" % self.headers.get("Host")):
                return self._send(403, {"error": "forbidden"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            path = urllib.parse.urlsplit(self.path).path
            try:
                if path == "/api/review":
                    res = answer(body.get("cid"), body.get("answer"), rating=body.get("rating"),
                                 latency_ms=body.get("latencyMs"))
                elif path == "/api/reveal":
                    res = reveal(body.get("cid"))
                elif path == "/api/seen":
                    res = seen(body.get("lids"))
                elif path == "/api/cli":
                    res = cli(body.get("argv"))
                else:
                    return self._send(404, {"error": "not found"})
            except NotDue as e:
                return self._send(409, {"error": str(e)})
            except PermissionError as e:
                return self._send(403, {"error": str(e)})
            except fl.Refused as e:
                return self._send(404 if str(e).startswith("no card") else 400, {"error": str(e)})
            except Exception as e:
                mesh.log("learn-ui %s error: %s" % (path, e))
                return self._send(500, {"error": str(e)})
            return self._send(200, res)

    return H


def _config_port():
    try:
        with open(fl._p("learn.json")) as f:
            p = json.load(f).get("servePort")
        return int(p) if p is not None else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def serve(port, bind, open_page):
    human_only("serve the LEARN page")
    if bind not in LOOPBACK:
        raise ValueError("--bind %s refused: the LEARN page binds loopback only (127.0.0.1 or ::1)" % bind)
    if port is None:
        port = _config_port() or DEFAULT_PORT
    token = secrets.token_urlsafe(24)
    srv_cls = http.server.ThreadingHTTPServer
    if bind == "::1":
        import socket

        class V6(srv_cls):
            address_family = socket.AF_INET6
        srv_cls = V6
    allowed = set()
    try:
        srv = srv_cls((bind, port), make_handler(token, allowed))
    except OSError as e:
        raise ValueError("can't listen on %s:%s (%s); pick another --port or set servePort in learn/learn.json" % (
            bind, port, e.strerror or e))
    srv.daemon_threads = True
    real = srv.server_address[1]
    for h in ("127.0.0.1", "localhost", "[::1]"):
        allowed.add("%s:%d" % (h, real))
    url = "http://%s:%d/?t=%s" % ("[::1]" if bind == "::1" else "127.0.0.1", real, token)
    urlfile = os.path.join(mesh.state_dir(), "learn-ui.url")
    os.makedirs(os.path.dirname(urlfile), exist_ok=True)
    fd = os.open(urlfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(url + "\n")
    sys.stdout.write("LEARN UI: %s\n" % url)
    sys.stdout.flush()
    if open_page:
        opener = ["open", "-g", url] if sys.platform == "darwin" else ["xdg-open", url]
        try:
            subprocess.Popen(opener, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            sys.stderr.write("could not open a browser (%s); the URL is above\n" % e)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(prog="fleet learn serve", description="LEARN desk web page (loopback + token)")
    p.add_argument("--port", type=int)
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--open", action="store_true")
    args = p.parse_args(argv)
    try:
        return serve(args.port, args.bind, args.open)
    except PermissionError as e:
        sys.stderr.write("FAIL %s\n" % e)
        return 4
    except ValueError as e:
        sys.stderr.write("FAIL %s\n" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
