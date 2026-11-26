#!/usr/bin/env python3
"""fleet-listen: the WATCH UI. A tmux pane that follows a live call or chat and routes what it hears.

Python 3.8+ stdlib only. Contract in INTERFACES.md ("WATCH UI").

  fleet_listen.py [--source file|stdin|command|replay] [ARG] [--speakers "A,B"] [--config FILE]
                  [--speed N] [--poll SEC] [--title T] [--name NAME] [--page] [--port N]
                  [--pane-right-of NAME | --here] [--plain] [--exit-at-eof]

A source adapter yields one utterance per line: JSON {"at","speaker","text","final"} (WhisperLive-style
{"start","end","speaker","text","completed"} and whosaid-style {"clock","name"|"who","text"} also parse)
or a text line "[hh:mm:ss] Speaker: text". Only final lines are routed:
  asks of the human   -> DECIDE via decision_open (quote, speaker, time)
  action items        -> ROUTE via the mesh inbox (owner or "owner: unresolved"), batched
  decided on the call -> ROUTE + decisions-log.jsonl as decided-on-call (never closes a desk call)
  uncertain           -> ROUTE, marked uncertain
  things to say       -> the SAY THIS box (and the --page explainer)
  secrets             -> nowhere; the pane says one appeared and the text is dropped
"""
import argparse
import collections
import datetime
import glob
import html
import importlib.util
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import queue

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("fleet_mesh", os.path.join(HERE, "fleet_mesh.py"))
mesh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mesh)

KINDS = ("ASK", "ACTION", "SAY", "DECIDED", "NOTE")
QUOTED = ("ASK", "ACTION", "DECIDED")  # must quote the transcript verbatim to be sent
DIM, RESET = "\x1b[2m", "\x1b[0m"


# ---------------------------------------------------------------- config

def _strip_jsonc(text):
    out = []
    for ln in text.split("\n"):
        s = ln.lstrip()
        if s.startswith("//"):
            continue
        out.append(ln)
    return "\n".join(out)


def read_config_file(path):
    with open(path) as f:
        return json.loads(_strip_jsonc(f.read()))


def load_config(raw=None):
    """Fill defaults into a listener.json dict. Returns a new dict."""
    c = dict(raw or {})
    c.setdefault("source", {})
    sp = c.get("speakers") or []
    if isinstance(sp, str):
        sp = [s.strip() for s in sp.split(",") if s.strip()]
    c["speakers"] = list(sp)
    c.setdefault("me", os.environ.get("FLEET_HUMAN") or "")
    c["speakerAliases"] = dict(c.get("speakerAliases") or {})
    route = dict(c.get("route") or {})
    route["asks"] = mesh.desk_name()  # decision_open always goes to the desk; not configurable
    route.setdefault("actions", mesh.orch_name())
    c["route"] = route
    cl = dict(c.get("classifier") or {"kind": "rules"})
    cl.setdefault("kind", "rules")
    cl.setdefault("timeoutSec", 10)
    cl.setdefault("maxTokens", 300)
    cl.setdefault("minIntervalSec", 5)
    cl.setdefault("tailChars", 1800)
    c["classifier"] = cl
    c["watchers"] = list(c.get("watchers") or [])
    c["facts"] = list(c.get("facts") or [])
    c["factFiles"] = list(c.get("factFiles") or [])
    c["glossary"] = dict(c.get("glossary") or {})
    c.setdefault("batchSec", 5)
    c.setdefault("title", "call")
    c.setdefault("name", "WATCH")
    return c


# ---------------------------------------------------------------- parsing

TEXT_LINE = re.compile(r"^\s*\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*([^:]{1,80}?):\s*(.+?)\s*$")


def _clock_from_seconds(s):
    s = int(max(0, float(s)))
    return "%02d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)


def _seconds_from_clock(c):
    parts = [int(p) for p in c.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_utterance(raw, aliases=None):
    """One source line -> {"clock","t","base","speaker","text","final"} or None.

    t is seconds on a time base ("epoch" for ISO `at`, "offset" for `start`/clock) used for replay pacing."""
    if not isinstance(raw, str):
        return None
    line = raw.strip()
    if not line:
        return None
    aliases = aliases or {}
    if line.startswith("{"):
        try:
            d = json.loads(line)
        except ValueError:
            return None
        if not isinstance(d, dict):
            return None
        text = d.get("text")
        speaker = d.get("speaker") or d.get("name") or d.get("who")
        if not isinstance(text, str) or not text.strip() or not speaker:
            return None
        if "final" in d:
            final = bool(d["final"])
        elif "completed" in d:
            final = bool(d["completed"])
        else:
            final = True
        t, base, clock = None, None, ""
        at = d.get("at")
        if isinstance(at, str) and at:
            try:
                dt = datetime.datetime.strptime(at.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
                t, base, clock = dt.timestamp(), "epoch", dt.strftime("%H:%M:%S")
            except ValueError:
                clock = at[-9:-1] if at.endswith("Z") else at
        elif isinstance(d.get("start"), (int, float)):
            t, base, clock = float(d["start"]), "offset", _clock_from_seconds(d["start"])
        elif isinstance(d.get("clock"), str) and re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", d["clock"]):
            t, base, clock = float(_seconds_from_clock(d["clock"])), "offset", d["clock"]
        speaker = str(speaker)
        u = {"clock": clock, "t": t, "base": base, "speaker": aliases.get(speaker, speaker),
             "text": " ".join(text.split()), "final": final}
        if d.get("unnamed") is True:  # the source has no speaker name for this line (teams-transcript)
            u["unnamed"] = True
        return u
    m = TEXT_LINE.match(line)
    if not m:
        return None
    clock, speaker, text = m.group(1), m.group(2).strip(), m.group(3)
    return {"clock": clock, "t": float(_seconds_from_clock(clock)), "base": "offset",
            "speaker": aliases.get(speaker, speaker), "text": " ".join(text.split()), "final": True}


SECRET_PATTERNS = [re.compile(p, re.I) for p in (
    r"\bsk-[A-Za-z0-9_-]{16,}",
    r"\bgh[pousr]_[A-Za-z0-9]{20,}",
    r"\bgithub_pat_[A-Za-z0-9_]{20,}",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bxox[abposr]-[A-Za-z0-9-]{10,}",
    r"\bAIza[0-9A-Za-z_-]{30,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
    # "the password is hunter2!": the value needs a digit or symbol, so "the token is expired" stays speech
    r"\b(?:password|passwd|passphrase|api[ _-]?key|secret|access[ _-]?token|auth[ _-]?token|token)\s*(?:is|=|:)\s*"
    r"(?=\S*[\d_!@#$%^&*+=~])\S{6,}",
)] + [
    # long random-looking string: upper AND lower AND digit (case-sensitive, so hex SHAs, UUIDs and slugs pass)
    re.compile(r"(?<![\w/.-])(?=[A-Za-z0-9+_=]*\d)(?=[A-Za-z0-9+_=]*[a-z])(?=[A-Za-z0-9+_=]*[A-Z])[A-Za-z0-9+_=]{32,}(?![\w/.-])"),
]


def is_secret(text):
    return any(p.search(text or "") for p in SECRET_PATTERNS)


def norm(s):
    return " ".join((s or "").lower().split())


def parse_items(lines):
    """Classifier/watcher output -> (items, ignored). Item: {"kind","text","fields","uncertain"}.

    Contract: one item per line, `KIND: text [| key: value]... [| uncertain]` with KIND in ASK ACTION SAY
    DECIDED NOTE. Anything else is ignored and counted."""
    items, ignored = [], 0
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        m = re.match(r"^(ASK|ACTION|SAY|DECIDED|NOTE):\s*(.*)$", ln)
        if not m or not m.group(2).strip():
            ignored += 1
            continue
        parts = [p.strip() for p in m.group(2).split(" | ")]
        fields, unc = {}, False
        for p in parts[1:]:
            if p.lower() == "uncertain":
                unc = True
                continue
            k, sep, v = p.partition(":")
            if sep:
                fields[k.strip().lower()] = v.strip()
        items.append({"kind": m.group(1), "text": parts[0], "fields": fields, "uncertain": unc})
    return items, ignored


# ---------------------------------------------------------------- classifiers

DAYS = r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|today|tonight|eod|end of (?:the )?(?:day|week|month)|next week|\d{1,2}/\d{1,2}"
R_DUE = re.compile(r"\b(?:by|before|due|until)\s+(%s)\b" % DAYS, re.I)
R_HEDGE = re.compile(r"\b(maybe|might|not sure|probably|i guess|perhaps|possibly)\b", re.I)
R_DECIDED = re.compile(r"(^(ok(ay)?,?\s+)?let'?s\s+(go with|do it|bundle|ship|keep|drop|use|stick with|move forward)"
                       r"|\b(we'?ll|we will) go with\b|\b(decided|agreed|it'?s settled|that'?s settled|final call)\b)", re.I)
R_ASK = re.compile(r"^(?:so,?\s+|and\s+|hey,?\s+)?(can|could|would|will|should|shall|do|are|is)\s+(we|you|they)\b", re.I)
R_QUESTION = re.compile(r"^(?:so,?\s+|and\s+)?(what|how|when|where|which|who|why|did|does|is|was|were)\b", re.I)
R_ACTION = re.compile(r"(\b(i'?ll|i will|we'?ll|we will|i need to|i'?ll need|i have to|action item|to-?do)\b"
                      r"|\b[\w.-]+ will\b|\bshould follow up\b|\bfollow up on\b|\bneeds? to\b)", re.I)
STOP = set("the a an and or of to in on at for is was were be been are what when where how who why which did does "
           "do it its this that after before with from by we you they our your can could would should will "
           "there here have has had get got any some".split())


def keywords(text):
    return [w for w in re.findall(r"[a-z0-9][a-z0-9.-]*", text.lower()) if len(w) >= 3 and w not in STOP]


class RulesClassifier:
    """Default keyword classifier. Emits the same line contract as a command classifier."""
    kind = "rules"

    def __init__(self, cfg):
        self.cfg = cfg
        extra = {}
        rf = cfg["classifier"].get("rulesFile")
        if rf:
            extra = read_config_file(rf)
        self.extra = {k: [re.compile(p, re.I) for p in v] for k, v in extra.items() if isinstance(v, list)}

    def _x(self, kind, text):
        return any(p.search(text) for p in self.extra.get(kind, []))

    def lookup(self, text):
        low = text.lower()
        for f in self.cfg["facts"]:
            terms = f.get("match") or []
            if isinstance(terms, str):
                terms = [terms]
            if terms and all(t.lower() in low for t in terms):
                return f.get("say", ""), f.get("source", "facts")
        kws = keywords(text)
        best = (0, None, None)
        for pat in self.cfg["factFiles"]:
            for path in sorted(glob.glob(os.path.expanduser(pat))):
                try:
                    with open(path, errors="replace") as fh:
                        for i, ln in enumerate(fh, 1):
                            score = sum(1 for k in kws if k in ln.lower())
                            if score > best[0]:
                                best = (score, ln.strip()[:160], "%s:%d" % (os.path.basename(path), i))
                except OSError:
                    continue
        if kws and best[0] >= max(2, (len(kws) + 1) // 2):
            return best[1], best[2]
        return None, None

    def owner(self, text):
        low = text.lower()
        for lane in sorted(mesh.load_roster(), key=len, reverse=True):
            if re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(lane.lower()), low):
                return lane
        return "unresolved"

    def classify(self, utts, ctx=None):
        out = []
        for u in utts:
            t = u["text"]
            hedge = " | uncertain" if R_HEDGE.search(t) else ""
            if R_DECIDED.search(t) or self._x("decided", t):
                out.append("DECIDED: %s%s" % (t, hedge))
            elif (R_ASK.search(t) and "?" in t) or self._x("ask", t):
                out.append("ASK: %s%s" % (t, hedge))
            elif (R_QUESTION.search(t) and t.rstrip().endswith("?")) or self._x("question", t):
                say, src = self.lookup(t)
                if say:
                    out.append("SAY: %s | source: %s" % (say, src))
                else:
                    out.append("NOTE: question, no answer on file")
            elif R_ACTION.search(t) or self._x("action", t):
                due = R_DUE.search(t)
                line = "ACTION: %s | owner: %s" % (t, self.owner(t))
                if due:
                    line += " | due: %s" % due.group(1)
                out.append(line + hedge)
        return out


def run_bounded(cmd, payload, timeout):
    """Run a shell command with JSON on stdin; kill its whole process group on timeout.

    Returns (ok, stdout_lines, error_text, elapsed_ms)."""
    t0 = time.time()
    try:
        p = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError as e:
        return False, [], "could not start: %s" % e, 0
    try:
        out, err = p.communicate(json.dumps(payload), timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.communicate()
        return False, [], "timeout after %gs" % timeout, int((time.time() - t0) * 1000)
    ms = int((time.time() - t0) * 1000)
    if p.returncode != 0:
        return False, out.splitlines(), "exit %d" % p.returncode, ms
    return True, out.splitlines(), "", ms


class CommandClassifier:
    """Any command: reads the request JSON on stdin, prints classifier lines on stdout."""
    kind = "command"

    def __init__(self, cfg):
        self.cfg = cfg
        self.c = cfg["classifier"]


def make_classifier(cfg):
    k = cfg["classifier"]["kind"]
    if k == "rules":
        return RulesClassifier(cfg)
    if k == "command":
        if not cfg["classifier"].get("run"):
            raise ValueError("classifier kind command needs `run`")
        return CommandClassifier(cfg)
    raise ValueError("unknown classifier kind: %s" % k)


# ---------------------------------------------------------------- watchers

def parse_trigger(s):
    s = (s or "").strip()
    m = re.match(r"^every\s+(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds|m|min|mins|minutes)?$", s, re.I)
    if m:
        n = float(m.group(1))
        if (m.group(2) or "s").lower().startswith("m"):
            n *= 60
        return ("every", n)
    m = re.match(r"^on\s+speaker\s+(.+)$", s, re.I)
    if m:
        return ("speaker", m.group(1).strip())
    m = re.match(r"^on\s+keyword\s+(.+)$", s, re.I)
    if m:
        return ("keyword", m.group(1).strip())
    raise ValueError("unknown watcher trigger: %r (every Ns | on speaker X | on keyword K)" % s)


class Watcher:
    def __init__(self, spec, lis):
        self.name = spec.get("name") or "watcher"
        self.trigger = parse_trigger(spec.get("trigger"))
        self.run = spec.get("run")
        if not self.run:
            raise ValueError("watcher %s needs `run`" % self.name)
        self.route_to = (spec.get("routeTo") or "route").lower()
        self.timeout = float(spec.get("timeoutSec", 10))
        self.lis = lis
        self.stop = threading.Event()
        self.threads = []

    def start(self):
        if self.trigger[0] == "every":
            th = threading.Thread(target=self._loop, daemon=True)
            th.start()
            self.threads.append(th)

    def _loop(self):
        while not self.stop.wait(self.trigger[1]):
            self.fire(None)

    def matches(self, u):
        k, v = self.trigger
        if k == "speaker":
            return u["speaker"] == v
        if k == "keyword":
            return v.lower() in u["text"].lower()
        return False

    def on_utterance(self, u):
        if self.matches(u):
            th = threading.Thread(target=self.fire, args=(u,), daemon=True)
            th.start()
            self.threads.append(th)

    def fire(self, u):
        payload = self.lis.request([u] if u else [])
        payload["watcher"] = self.name
        ok, lines, err, ms = run_bounded(self.run, payload, self.timeout)
        if not ok:
            self.lis.set_status("watcher " + self.name, "error: " + err)
            return
        items, ignored = parse_items(lines)
        self.lis.apply(items, ignored, source=self.name, route_to=self.route_to)

    def close(self):
        self.stop.set()
        for th in list(self.threads):
            th.join(self.timeout + 1)


# ---------------------------------------------------------------- the listener core

class Listener:
    def __init__(self, cfg, emit=None):
        self.cfg = cfg
        self.name = cfg.get("name") or "WATCH"
        self.speakers = list(cfg["speakers"])
        self.me = cfg.get("me") or ""
        self.emit = emit  # plain mode: callable(str)
        self.lock = threading.RLock()
        self.lines = []  # {"n","clock","speaker","text","notes":[]} or {"n","note"}
        self.tail = collections.OrderedDict()  # unconfirmed text per speaker
        self.transcript = []  # final, non-secret utterances (dicts)
        self.say = []
        self.routed = []
        self.marks = []
        self.counts = dict.fromkeys(("ask", "action", "decided", "uncertain", "say", "secret", "ignored",
                                     "ungrounded", "dup"), 0)
        self.status = collections.OrderedDict()
        self.latency_ms = None
        self.pending = []
        self.last_call = 0.0
        self.outbox = []
        self.batch_t0 = None
        self.seen = set()
        self.started = time.time()
        self.page_url = None
        self.classifier = make_classifier(cfg)
        self.watchers = [Watcher(w, self) for w in cfg["watchers"]]
        for w in self.watchers:
            w.start()

    # -- helpers
    def _out(self, s):
        if self.emit:
            self.emit(s)

    def set_status(self, key, msg):
        with self.lock:
            if self.status.get(key) == msg:
                return
            self.status[key] = msg
        self._out("STATUS %s: %s" % (key, msg))

    def set_filter(self, text):
        with self.lock:
            self.speakers = [s.strip() for s in (text or "").split(",") if s.strip()]
        self._out("FILTER %s" % (", ".join(self.speakers) or "(everyone)"))

    def mark(self):
        with self.lock:
            c = time.strftime("%H:%M:%S")
            self.marks.append(c)
            self._add_note("MARK %s" % c)

    def _add_note(self, note):
        self.lines.append({"n": len(self.lines), "note": note})
        self._out("NOTE %s" % note)

    def note(self, note):
        """A source notice (panel closed, gap, no speaker names): shown in the pane, never routed."""
        with self.lock:
            self._add_note(note)

    def shown(self, e):
        """Display filter: notes always, transcript lines only for the chosen speakers (+ me). A line the source
        could not name (teams-transcript) is always shown, since no filter can match it; it is routed only with no filter."""
        return "note" in e or bool(e.get("unnamed")) or self._routable(e)

    def _routable(self, u):
        return not self.speakers or u["speaker"] in self.speakers or (self.me and u["speaker"] == self.me)

    def tail_text(self, limit=None):
        limit = limit or int(self.cfg["classifier"].get("tailChars", 1800))
        with self.lock:
            rows = ["%s %s: %s" % (u["clock"], u["speaker"], u["text"]) for u in self.transcript if self._routable(u)]
        s = "\n".join(rows)
        return s[-limit:] if len(s) > limit else s

    def request(self, utts):
        return {"utterances": [{"clock": u["clock"], "speaker": u["speaker"], "text": u["text"]} for u in utts],
                "utterance": utts[-1]["text"] if utts else "", "tail": self.tail_text(), "speakers": list(self.speakers),
                "me": self.me, "maxTokens": int(self.cfg["classifier"].get("maxTokens", 300)),
                "labels": list(KINDS)}

    # -- input
    def feed(self, u):
        if not u:
            return
        with self.lock:
            if is_secret(u["text"]):
                self.tail.pop(u["speaker"], None)
                if u["final"]:
                    self.counts["secret"] += 1
                    self._add_note("a secret or credential appeared (%s %s); the text was dropped"
                                   % (u["clock"], u["speaker"]))
                return
            if not u["final"]:
                self.tail[u["speaker"]] = u
                return
            self.tail.pop(u["speaker"], None)
            entry = {"n": len(self.lines), "clock": u["clock"], "speaker": u["speaker"], "text": u["text"], "notes": []}
            if u.get("unnamed"):
                entry["unnamed"] = True
            u = dict(u, entry=entry)
            self.lines.append(entry)
            self.transcript.append(u)
            self._out("%s %s  %s" % (u["clock"], u["speaker"], u["text"]))
            routable = self._routable(u)
            if routable:
                self.pending.append(u)
        for w in self.watchers:
            w.on_utterance(u)
        self.classify()
        self.tick()

    def classify(self, force=False):
        with self.lock:
            if not self.pending:
                return
            if isinstance(self.classifier, RulesClassifier):
                batch, self.pending = self.pending, []
                lines = self.classifier.classify(batch)
                items, ignored = parse_items(lines)
                self._apply_locked(items, ignored, "rules", "route")
                return
            gap = float(self.cfg["classifier"].get("minIntervalSec", 5))
            if not force and time.time() - self.last_call < gap:
                return
            batch, self.pending = self.pending, []
            self.last_call = time.time()
            payload = self.request(batch)
        c = self.cfg["classifier"]
        ok, lines, err, ms = run_bounded(c["run"], payload, float(c.get("timeoutSec", 10)))
        with self.lock:
            self.latency_ms = ms
        self.set_status("classifier", "classifier ok" if ok else "classifier %s" % err)
        items, ignored = parse_items(lines) if ok else ([], 0)
        self.apply(items, ignored, "classifier", "route")

    def tick(self):
        """Periodic: run a throttled classifier call that is due, flush a batch whose window closed."""
        if self.pending and not isinstance(self.classifier, RulesClassifier):
            self.classify()
        with self.lock:
            due = self.outbox and (float(self.cfg["batchSec"]) <= 0 or time.time() - self.batch_t0 >= float(self.cfg["batchSec"]))
        if due:
            self.flush_outbox()

    def flush(self):
        self.classify(force=True)
        self.flush_outbox()

    def close(self):
        for w in self.watchers:
            w.close()
        self.flush()

    # -- routing
    def apply(self, items, ignored=0, source="", route_to="route"):
        with self.lock:
            self._apply_locked(items, ignored, source, route_to)
        self.tick()

    def _ground(self, quote):
        q = norm(quote).strip(" \"'")
        if not q:
            return None
        for u in reversed(self.transcript):
            if q in norm(u["text"]):
                return u
        return None

    def _apply_locked(self, items, ignored, source, route_to):
        self.counts["ignored"] += ignored
        for it in items:
            kind, text, f = it["kind"], it["text"], it["fields"]
            if is_secret(text) or any(is_secret(v) for v in f.values()):
                self.counts["secret"] += 1
                self._add_note("a classifier line carried a secret; dropped")
                continue
            if route_to == "say" and kind not in ("SAY", "NOTE"):
                self.counts["ignored"] += 1
                continue
            if kind == "NOTE":
                if source not in ("rules",):
                    self._add_note("%s: %s" % (source, text))
                continue
            if kind == "SAY":
                key = ("SAY", norm(text))
                if key in self.seen:
                    self.counts["dup"] += 1
                    continue
                self.seen.add(key)
                src = f.get("source") or ("" if source in ("rules", "classifier") else source)
                self.say.append({"text": text, "source": src, "at": time.strftime("%H:%M:%S")})
                self.counts["say"] += 1
                self._out("SAY %s%s" % (text, " [%s]" % src if src else ""))
                continue
            u = self._ground(text)
            if u is None:
                self.counts["ungrounded"] += 1
                self._add_note("not sent (%s quote not in the transcript): %s" % (kind, text[:80]))
                continue
            key = (kind, norm(text))
            if key in self.seen:
                self.counts["dup"] += 1
                continue
            self.seen.add(key)
            self._route(kind, text, f, u, it["uncertain"])

    def _note_on(self, u, note):
        u["entry"]["notes"].append(note)
        self._out("         -> %s" % note)

    def _queue(self, line):
        if not self.outbox:
            self.batch_t0 = time.time()
        self.outbox.append(line)

    def _route(self, kind, quote, f, u, uncertain):
        who = "%s, %s" % (u["speaker"], u["clock"] or "?")
        orch, desk = self.cfg["route"]["actions"], self.cfg["route"]["asks"]
        rec = {"kind": kind, "quote": quote, "speaker": u["speaker"], "clock": u["clock"], "uncertain": uncertain}
        if uncertain:
            self.counts["uncertain"] += 1
            self._queue("%s, marked uncertain: \"%s\" (%s) owner: %s" % (kind, quote, who, f.get("owner", "unresolved")))
            rec.update(to=orch)
            self._note_on(u, "%s?  sent to %s marked uncertain" % (kind, orch))
        elif kind == "ASK":
            if self.me and u["speaker"] == self.me:
                return
            opts = [o.strip() for o in (f.get("options") or "yes; no; not now").split(";") if o.strip()]
            r = f.get("rec")
            opts = [o + " (Recommended)" if r and o == r else o for o in opts]
            q = "Heard on the call: %s asked \"%s\" (%s). WATCH reading, check before answering." % (
                u["speaker"], quote, u["clock"] or "time unknown")
            try:
                res = mesh.t_decision_open({"from": self.name, "question": q, "options": opts})
                ref = res.split()[1].rstrip(";") if res.startswith("opened ") else "?"
            except Exception as e:  # never let a routing failure kill the pane
                self.set_status("route", "decision_open failed: %s" % e)
                return
            self.counts["ask"] += 1
            rec.update(to=desk, ref=ref)
            self._note_on(u, "ASK  sent to %s as %s" % (desk, ref))
        elif kind == "ACTION":
            owner = f.get("owner") or "unresolved"
            line = "ACTION \"%s\" (%s) owner: %s" % (quote, who, owner)
            if f.get("due"):
                line += ", due: %s" % f["due"]
            self._queue(line)
            self.counts["action"] += 1
            rec.update(to=orch, owner=owner)
            self._note_on(u, "ACTION  sent to %s (owner: %s)" % (orch, owner))
        elif kind == "DECIDED":
            self._queue("DECIDED \"%s\" (%s) decided-on-call; the desk confirms, no desk call is closed" % (quote, who))
            try:
                mesh._append_line(os.path.join(mesh.state_dir(), "decisions-log.jsonl"), json.dumps({
                    "id": None, "at": mesh.now(), "asker": u["speaker"], "verbatim": quote, "relayedTo": [orch],
                    "kind": "decided-on-call", "confirmed": False, "from": self.name, "clock": u["clock"]}))
            except OSError as e:
                self.set_status("route", "decisions log write failed: %s" % e)
            self.counts["decided"] += 1
            rec.update(to=orch)
            self._note_on(u, "DECIDED  sent to %s (decided-on-call)" % orch)
        self.routed.append(rec)

    def flush_outbox(self):
        with self.lock:
            lines, self.outbox = self.outbox, []
        if not lines:
            return
        orch = self.cfg["route"]["actions"]
        text = lines[0] if len(lines) == 1 else "[listener] %d items from the call:\n- %s" % (len(lines), "\n- ".join(lines))
        try:
            mesh.deliver(self.name, orch, "[listener] " + text if len(lines) == 1 else text)
        except Exception as e:
            self.set_status("route", "send to %s failed: %s" % (orch, e))

    # -- explainer (page)
    def explainer(self):
        with self.lock:
            recent = [u for u in self.transcript[-30:] if self._routable(u)]
            words = collections.Counter(w for u in recent for w in keywords(u["text"]))
            out = []
            if words:
                out.append("Talking about: " + ", ".join(w for w, _ in words.most_common(6)))
            alltext = " ".join(norm(u["text"]) for u in self.transcript)
            for term, meaning in sorted(self.cfg["glossary"].items()):
                if norm(term) in alltext:
                    out.append("S-T: %s = %s" % (term, meaning))
            for r in self.routed[-5:]:
                out.append("%s from %s: \"%s\"" % (r["kind"].title(), r["speaker"], r["quote"]))
            return out


# ---------------------------------------------------------------- rendering

def _wrap(s, w, indent):
    """Break s into rows of at most w chars; continuation rows start with indent spaces."""
    rows, first = [], True
    while True:
        lim = w if first else w - indent
        body = s if first else s.lstrip()
        if len(body) <= lim or lim < 10:
            rows.append(body if first else " " * indent + body)
            return rows
        cut = body.rfind(" ", 0, lim + 1)
        if cut <= lim // 2:
            cut = lim
        rows.append(body[:cut] if first else " " * indent + body[:cut])
        s, first = body[cut:], False


def _fit(s, w):
    return s[:w] if len(s) > w else s + " " * (w - len(s))


def _status_segments(lis):
    c = lis.counts
    segs = ["routed: %d ask%s, %d action%s" % (c["ask"], "" if c["ask"] == 1 else "s", c["action"],
                                                "" if c["action"] == 1 else "s"),
            "unsure: %d" % c["uncertain"]]
    if c["decided"]:
        segs.append("decided: %d" % c["decided"])
    if c["secret"]:
        segs.append("secrets dropped: %d" % c["secret"])
    if c["ungrounded"]:
        segs.append("ungrounded: %d" % c["ungrounded"])
    if c["ignored"]:
        segs.append("ignored: %d" % c["ignored"])
    for k, v in lis.status.items():
        segs.append(v if k == "classifier" else "%s: %s" % (k, v))
    if lis.latency_ms is not None:
        segs.append("classifier %d ms" % lis.latency_ms)
    segs.append("page %s" % lis.page_url if lis.page_url else "[o] page")
    return segs


def render(lis, width, height, color=True, edit=None, paused_at=None, help_on=False):
    w = max(40, width) - 2
    out = []
    bar = lambda: "+" + "-" * w + "+"  # noqa: E731
    row = lambda s: "|" + _fit(" " + s, w) + "|"  # noqa: E731
    with lis.lock:
        el = int(time.time() - lis.started)
        title = " WATCH: %s " % lis.cfg["title"]
        clock = " %02d:%02d:%02d " % (el // 3600, (el % 3600) // 60, el % 60)
        out.append("+" + ("--" + title).ljust(w - len(clock), "-")[:w - len(clock)] + clock + "+")
        if edit is not None:
            out.append(row("filter> %s_   (Enter to apply, Esc to cancel)" % edit))
        else:
            out.append(row("filter: %s   [f]ilter  [p]ause  [m]ark  [o] page  [q]uit  [?]" % (
                ", ".join(lis.speakers) or "(everyone)")))
        if help_on:
            out.append(row("f: speakers to route (comma list, empty = everyone)  p: freeze the view  m: mark now"))
            out.append(row("o: serve the explainer page on 127.0.0.1  q: quit (source stops)"))
        out.append(bar())
        stream = []
        lines = lis.lines if paused_at is None else lis.lines[:paused_at]
        tw = w - 1
        for e in lines:
            if not lis.shown(e):
                continue
            if "note" in e:
                stream += [("note", r) for r in _wrap("         ! %s" % e["note"], tw, 11)]
                continue
            stream += [("line", r) for r in _wrap("%-8s %-14s %s" % (e["clock"], e["speaker"], e["text"]), tw, 24)]
            for n in e["notes"]:
                stream += [("note", r) for r in _wrap("         -> %s" % n, tw, 12)]
        if paused_at is None:
            for sp, u in lis.tail.items():
                if lis._routable(u):
                    stream += [("dim", r) for r in _wrap("%-8s %-14s %s ..." % (u["clock"], sp, u["text"]), tw, 24)]
        else:
            stream.append(("note", "         PAUSED (%d new lines held, [p] to resume)" % (len(lis.lines) - paused_at)))
        say = [r for s in lis.say[-5:] for r in
               _wrap(" * %s%s" % (s["text"], " (%s)" % s["source"] if s["source"] else ""), tw, 3)]
        segs = _status_segments(lis)
        status_rows, cur = [], ""
        for s in segs:
            cand = s if not cur else cur + " | " + s
            if len(cand) > w - 2 and cur:
                status_rows.append(cur)
                cur = s
            else:
                cur = cand
        status_rows.append(cur)
        fixed = len(out) + 1 + 1 + max(1, len(say)) + 1 + len(status_rows) + 1
        room = max(3, height - fixed)
        for kind, s in stream[-room:]:
            r = row(s)
            if kind == "dim" and color:
                r = "|" + DIM + _fit(" " + s, w) + RESET + "|"
            out.append(r)
        out.append(bar())
        out.append(row("SAY THIS"))
        if say:
            for s in say:
                out.append(row(s))
        else:
            out.append(row(" (nothing yet)"))
        out.append(bar())
        for s in status_rows:
            out.append(row(s))
        out.append(bar())
    return "\n".join(out)


# ---------------------------------------------------------------- explainer page (loopback only)

PAGE_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>WATCH: %(title)s</title>
<style>body{font:15px/1.45 system-ui,sans-serif;margin:24px;max-width:980px;color:#1b1b1b}
h1{font-size:20px}h2{font-size:15px;margin-top:22px;text-transform:uppercase;letter-spacing:.05em}
.say li{background:#eef8f2;margin:4px 0;padding:6px 8px;border-radius:6px;list-style:none}
.line{font-family:ui-monospace,monospace;font-size:13px;white-space:pre-wrap}.note{color:#666}
button{margin:8px 0}</style></head><body>
<h1>WATCH: %(title)s</h1>
<h2>SAY THIS</h2><ul class="say" id="say"></ul>
<h2>What is being discussed</h2><ul id="exp"></ul>
<h2>Calls and actions created</h2><ul id="routed"></ul>
<h2>Transcript</h2><button id="more">load more</button><div id="lines"></div>
<script>
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let oldest=null;
function lineHtml(e){if(e.note)return '<div class="line note">! '+esc(e.note)+'</div>';
 return '<div class="line">'+esc(e.clock)+' '+esc(e.speaker)+'  '+esc(e.text)+(e.notes||[]).map(n=>'<div class="note">   -> '+esc(n)+'</div>').join('')+'</div>';}
async function refresh(){const s=await (await fetch('state.json')).json();
 say.innerHTML=s.say.map(x=>'<li>'+esc(x.text)+(x.source?' <small>('+esc(x.source)+')</small>':'')+'</li>').join('')||'<li>(nothing yet)</li>';
 exp.innerHTML=s.explainer.map(x=>'<li>'+esc(x)+'</li>').join('');
 routed.innerHTML=s.routed.map(r=>'<li>'+esc(r.kind)+(r.ref?' '+esc(r.ref):'')+' to '+esc(r.to)+': "'+esc(r.quote)+'" ('+esc(r.speaker)+')</li>').join('');
 lines.innerHTML=s.lines.map(lineHtml).join(''); oldest=s.lines.length?s.lines[0].n:null;}
more.onclick=async()=>{if(oldest===null||oldest<=0)return;
 const r=await (await fetch('lines.json?before='+oldest+'&limit=50')).json();
 lines.insertAdjacentHTML('afterbegin',r.lines.map(lineHtml).join('')); if(r.lines.length)oldest=r.lines[0].n;};
refresh();setInterval(refresh,3000);
</script></body></html>"""


class Page:
    """Local explainer page. Binds 127.0.0.1 only; there is no host option."""

    def __init__(self, lis, port=0):
        self.lis, self.port, self.server, self.thread = lis, int(port or 0), None, None

    def start(self):
        import http.server
        import urllib.parse
        lis = self.lis

        class H(http.server.BaseHTTPRequestHandler):
            def _send(self, code, body, ctype):
                b = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(u.query)
                if u.path == "/":
                    return self._send(200, PAGE_HTML % {"title": html.escape(lis.cfg["title"])}, "text/html; charset=utf-8")
                if u.path == "/state.json":
                    with lis.lock:
                        st = {"title": lis.cfg["title"], "say": lis.say, "routed": lis.routed, "marks": lis.marks,
                              "explainer": lis.explainer(), "counts": lis.counts,
                              "lines": [e for e in lis.lines if lis.shown(e)][-50:]}
                        body = json.dumps(st)
                    return self._send(200, body, "application/json")
                if u.path == "/lines.json":
                    try:
                        before = int(qs.get("before", ["0"])[0])
                        limit = max(1, min(200, int(qs.get("limit", ["50"])[0])))
                    except ValueError:
                        return self._send(400, '{"error":"bad query"}', "application/json")
                    with lis.lock:
                        rows = [e for e in lis.lines if e["n"] < before and lis.shown(e)][-limit:]
                        body = json.dumps({"lines": rows})
                    return self._send(200, body, "application/json")
                return self._send(404, '{"error":"not found"}', "application/json")

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        url = "http://127.0.0.1:%d/" % self.server.server_address[1]
        lis.page_url = url
        return url

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


# ---------------------------------------------------------------- source adapters

TEAMS_TRANSCRIPT_RUN = "whosaid teams transcript-live --jsonl"
TEAMS_NO_SPEAKER = "unknown"
TEAMS_NO_SPEAKER_NOTE = ("teams-transcript lines carry no speaker names; they show as %r, and a speaker filter "
                         "cannot match them: they stay visible but are routed only with no filter" % TEAMS_NO_SPEAKER)
TEAMS_PANEL_CLOSED_NOTE = "teams transcript panel closed; reopen it with: whosaid teams open-transcript"


def source_note(raw):
    """A {"note": "..."} line with no text (fleet feed writes its status this way): the note, else None.
    parse_utterance and the WATCH call source skip it, since it has no text."""
    line = raw.strip()
    if not line.startswith("{") or '"note"' not in line:
        return None
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if isinstance(d, dict) and isinstance(d.get("note"), str) and "text" not in d:
        return d["note"]
    return None


class Source:
    """Runs an adapter in a thread; puts ("line", raw) / ("status", msg) / ("note", msg) / ("eof", None) on a queue."""

    def __init__(self, kind, arg, q, speed=1.0, poll=1.0, aliases=None):
        self.kind, self.arg, self.q, self.speed, self.poll = kind, arg, q, float(speed), float(poll)
        self.stop = threading.Event()
        self.proc = None
        self.aliases = aliases or {}

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _status(self, msg):
        self.q.put(("status", "%s %s" % (self.kind, msg)))

    def _run(self):
        try:
            getattr(self, "_" + self.kind.replace("-", "_"))()
        except Exception as e:  # an adapter crash is a status, never a dead pane
            self._status("error: %s" % e)
        self.q.put(("eof", None))

    def _stdin(self):
        self._status("ok")
        for raw in sys.stdin:
            if self.stop.is_set():
                return
            self.q.put(("line", raw))
        self._status("ended")

    def _command(self):
        self.proc = subprocess.Popen(self.arg, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL, text=True, start_new_session=True)
        self._status("ok")
        for raw in self.proc.stdout:
            if self.stop.is_set():
                break
            self.q.put(("line", raw))
        rc = self.proc.wait()
        if self.stop.is_set():
            return
        self._status("ended" if rc == 0 else "error: exit %d" % rc)

    def _teams_transcript(self):
        """whosaid's Teams transcript-panel source through the command adapter: rows become lines,
        its status changes become the source status (each once), a gap or a closed panel becomes a note."""
        cmd = self.arg or TEAMS_TRANSCRIPT_RUN
        if re.fullmatch(r"\d{1,5}", cmd.strip()):  # a bare number is the Teams DevTools port
            cmd = "%s --port %s" % (TEAMS_TRANSCRIPT_RUN, cmd.strip())
        self.proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL, text=True, start_new_session=True)
        last, named_note = None, False
        self._status("started")
        for raw in self.proc.stdout:
            if self.stop.is_set():
                break
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            if "status" in ev:
                st = str(ev["status"])
                if st != last:
                    last = st
                    self._status(st)
                    if st == "panel closed":
                        self.q.put(("note", TEAMS_PANEL_CLOSED_NOTE))
                continue
            if ev.get("gap"):
                self.q.put(("note", "teams-transcript gap: rows scrolled past between %s and %s may be missing"
                            % (ev.get("after") or "?", ev.get("before") or "?")))
                continue
            if not str(ev.get("text") or "").strip():
                continue
            if not str(ev.get("speaker") or "").strip():
                ev["speaker"], ev["unnamed"] = TEAMS_NO_SPEAKER, True
                if not named_note:
                    named_note = True
                    self.q.put(("note", TEAMS_NO_SPEAKER_NOTE))
            ev.setdefault("final", True)
            self.q.put(("line", json.dumps(ev) + "\n"))
        rc = self.proc.wait()
        if self.stop.is_set():
            return
        self._status("ended" if rc == 0 else "error: exit %d" % rc)

    def _replay(self):
        with open(self.arg) as f:
            raws = f.readlines()
        self._status("ok")
        prev = None
        for raw in raws:
            if self.stop.is_set():
                return
            u = parse_utterance(raw)
            if u and prev and self.speed > 0 and u["t"] is not None and prev["t"] is not None \
                    and u["base"] == prev["base"]:
                gap = u["t"] - prev["t"]
                if 0 < gap <= 300 and self.stop.wait(gap / self.speed):
                    return
            if u and u["t"] is not None:
                prev = u
            self.q.put(("line", raw))
        self._status("ended")

    def _file(self):
        """Follow a growing or whole-file-rewritten transcript; emit each complete line once."""
        seen = collections.Counter()
        while not self.stop.is_set():
            try:
                with open(self.arg, errors="replace") as f:
                    data = f.read()
                self._status("ok")
            except OSError as e:
                self._status("error: %s" % (e.strerror or e))
                self.stop.wait(self.poll)
                continue
            lines = data.split("\n")[:-1]  # only complete lines
            run = collections.Counter()
            for ln in lines:
                run[ln] += 1
                if run[ln] > seen[ln]:
                    seen[ln] += 1
                    note = source_note(ln)
                    self.q.put(("note", note) if note is not None else ("line", ln + "\n"))
            self.stop.wait(self.poll)

    def close(self):
        self.stop.set()
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                pass


# ---------------------------------------------------------------- terminal UI

class TTY:
    def __init__(self):
        import termios
        import tty
        self.termios, self.tty = termios, tty
        self.fd = os.open("/dev/tty", os.O_RDONLY)
        self.old = termios.tcgetattr(self.fd)

    def __enter__(self):
        self.tty.setcbreak(self.fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l")
        sys.stdout.flush()
        return self

    def __exit__(self, *a):
        try:  # after SIGHUP the terminal is gone; cleanup (flush, stop the source) must still run
            self.termios.tcsetattr(self.fd, self.termios.TCSADRAIN, self.old)
            sys.stdout.write("\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
        except (OSError, ValueError):
            pass
        os.close(self.fd)

    def key(self, timeout):
        import select
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        return os.read(self.fd, 32).decode(errors="replace")


def ui_loop(lis, src, q, page_port, exit_at_eof):
    import shutil
    state = {"edit": None, "paused": None, "help": False, "quit": False, "eof": False}
    page = [None]

    def consume():
        while not state["quit"]:
            try:
                kind, val = q.get(timeout=0.2)
            except queue.Empty:
                lis.tick()
                continue
            if kind == "line":
                lis.feed(parse_utterance(val, lis.cfg["speakerAliases"]))
            elif kind == "status":
                lis.set_status("source", val)
            elif kind == "note":
                lis.note(val)
            elif kind == "eof":
                lis.flush()
                state["eof"] = True
                if exit_at_eof:
                    state["quit"] = True

    threading.Thread(target=consume, daemon=True).start()
    sys.stderr = open(os.devnull, "w")  # mesh logs to stderr and fleet.log; keep the full-screen pane clean
    with TTY() as t:
        last = ""
        while not state["quit"]:
            size = shutil.get_terminal_size((100, 40))
            frame = render(lis, size.columns, size.lines, edit=state["edit"], paused_at=state["paused"],
                           help_on=state["help"])
            if frame != last:
                sys.stdout.write("\x1b[H\x1b[2J" + frame.replace("\n", "\r\n"))
                sys.stdout.flush()
                last = frame
            k = t.key(0.25)
            if not k:
                continue
            for ch in k:
                if state["edit"] is not None:
                    if ch in ("\r", "\n"):
                        lis.set_filter(state["edit"])
                        state["edit"] = None
                    elif ch == "\x1b":
                        state["edit"] = None
                    elif ch in ("\x7f", "\b"):
                        state["edit"] = state["edit"][:-1]
                    elif ch.isprintable():
                        state["edit"] += ch
                    continue
                if ch == "f":
                    state["edit"] = ", ".join(lis.speakers)
                elif ch == "p":
                    state["paused"] = None if state["paused"] is not None else len(lis.lines)
                elif ch == "m":
                    lis.mark()
                elif ch == "?":
                    state["help"] = not state["help"]
                elif ch == "o" and page[0] is None:
                    page[0] = Page(lis, page_port)
                    lis.set_status("page", page[0].start())
                elif ch == "q":
                    state["quit"] = True
    src.close()
    lis.close()
    if page[0]:
        page[0].stop()


def plain_loop(lis, src, q, exit_at_eof):
    while True:
        try:
            kind, val = q.get(timeout=0.2)
        except queue.Empty:
            lis.tick()
            continue
        if kind == "line":
            lis.feed(parse_utterance(val, lis.cfg["speakerAliases"]))
        elif kind == "status":
            lis.set_status("source", val)
        elif kind == "note":
            lis.note(val)
        elif kind == "eof":
            lis.flush()
            if exit_at_eof:
                break
    src.close()
    lis.close()
    print(render(lis, 100, 60, color=False), flush=True)


# ---------------------------------------------------------------- placement (tmux) and CLI

PASS_ENV = ("FLEET_STATE", "FLEET_TMUX_SOCKET", "FLEET_TMUX_SESSION", "FLEET_DECIDE", "FLEET_ROUTE",
            "FLEET_HUMAN", "XDG_STATE_HOME", "PATH", "HOME")


def place_in_tmux(argv, right_of, name):
    """Open the listener as a tmux pane (right of a lane) or a window in the fleet session."""
    sess = os.environ.get("FLEET_TMUX_SESSION") or "fleet"
    env = ["%s=%s" % (k, shlex.quote(os.environ[k])) for k in PASS_ENV if os.environ.get(k)]
    env += ["FLEET_NAME=%s" % shlex.quote(name), "FLEET_ROLE=watch"]  # listener is now watch
    cmd = "env %s %s %s %s --here" % (" ".join(env), shlex.quote(sys.executable), shlex.quote(os.path.abspath(__file__)),
                                      " ".join(shlex.quote(a) for a in argv))
    if right_of:
        pane = (mesh.load_roster().get(right_of) or {}).get("pane")
        if not pane:
            raise SystemExit("FAIL no lane named %s with a pane in the roster" % right_of)
        args = ["split-window", "-h", "-d", "-t", pane, "-c", os.getcwd(), "-P", "-F", "#{pane_id}", cmd]
    else:
        has = subprocess.run(mesh._tmux_argv("has-session", "-t", sess), capture_output=True)
        if has.returncode == 0:
            args = ["new-window", "-d", "-t", sess + ":", "-n", name, "-c", os.getcwd(), "-P", "-F", "#{pane_id}", cmd]
        else:
            args = ["new-session", "-d", "-s", sess, "-n", name, "-c", os.getcwd(), "-P", "-F", "#{pane_id}", cmd]
    p = subprocess.run(mesh._tmux_argv(*args), capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit("FAIL tmux: %s" % (p.stderr.strip() or p.returncode))
    pane_id = p.stdout.strip()
    mesh.log("listener %s opened pane=%s" % (name, pane_id))
    print("OK listener %s pane=%s" % (name, pane_id))
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="fleet listen", allow_abbrev=False,
                                 description="WATCH UI: follow a live call, route asks, actions and things to say.")
    ap.add_argument("--source", choices=("file", "stdin", "command", "replay", "teams-transcript"))
    ap.add_argument("arg", nargs="?", help="path (file, replay), shell command (command), or for teams-transcript a "
                    "DevTools port or a command replacing %r" % TEAMS_TRANSCRIPT_RUN)
    ap.add_argument("--speakers", help='comma list, e.g. "Alice_Example,Bob_Example"; empty = everyone')
    ap.add_argument("--config", help="listener.json (default $FLEET_STATE/listener.json when present)")
    ap.add_argument("--speed", type=float, help="replay speed multiple (1 = real time, 0 = no delay)")
    ap.add_argument("--poll", type=float, default=1.0, help="file source poll seconds")
    ap.add_argument("--title")
    ap.add_argument("--name", help="sender name on the mesh (default WATCH)")
    ap.add_argument("--page", action="store_true", help="serve the explainer page on 127.0.0.1")
    ap.add_argument("--port", type=int, default=0, help="page port (default: a free one)")
    ap.add_argument("--pane-right-of", dest="right_of", help="open as a tmux pane right of this lane")
    ap.add_argument("--here", action="store_true", help="run in this terminal instead of opening a tmux pane")
    ap.add_argument("--plain", action="store_true", help="no full-screen UI: print events as lines (pipes, tests)")
    ap.add_argument("--exit-at-eof", action="store_true", help="exit when the source ends")
    return ap


def main(argv):
    ap = build_parser()
    a = ap.parse_args(argv)
    raw = {}
    cfgpath = a.config or os.path.join(mesh.state_dir(), "listener.json")
    if a.config or os.path.exists(cfgpath):
        try:
            raw = read_config_file(cfgpath)
        except (OSError, ValueError) as e:
            ap.error("bad config %s: %s" % (cfgpath, e))
    if a.speakers is not None:
        raw["speakers"] = a.speakers
    if a.title:
        raw["title"] = a.title
    if a.name:
        raw["name"] = a.name
    cfg = load_config(raw)
    srccfg = cfg.get("source") or {}
    kind = a.source or srccfg.get("kind")
    arg = a.arg or srccfg.get("path") or srccfg.get("run")
    if not kind:
        ap.error("no source: pass --source or set source.kind in listener.json")
    if kind in ("file", "replay", "command") and not arg:
        ap.error("source %s needs an argument" % kind)
    speed = a.speed if a.speed is not None else float(srccfg.get("speed", 1))

    if not (a.here or a.plain or kind == "stdin"):
        return place_in_tmux(argv, a.right_of, cfg["name"])

    if os.environ.get("TMUX_PANE") and not a.plain:  # fleet cleanup skips windows holding a desk pane
        subprocess.run(mesh._tmux_argv("set-option", "-p", "-t", os.environ["TMUX_PANE"], "@fleet-desk", "watch"),
                       capture_output=True)
    q = queue.Queue()
    lis = Listener(cfg, emit=(lambda s: print(s, flush=True)) if a.plain else None)
    src = Source(kind, arg, q, speed=speed, poll=a.poll, aliases=cfg["speakerAliases"])
    page = None
    if a.page:
        page = Page(lis, a.port)
        url = page.start()
        lis.set_status("page", url)
    src.start()
    for sig in (signal.SIGTERM, signal.SIGHUP):  # kill-pane / closing the window sends SIGHUP
        signal.signal(sig, lambda *x: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        if a.plain:
            plain_loop(lis, src, q, a.exit_at_eof)
        else:
            ui_loop(lis, src, q, a.port, a.exit_at_eof)
    except KeyboardInterrupt:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        src.close()
        lis.close()  # flushes the batched actions to ROUTE
    finally:
        if page:
            page.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
