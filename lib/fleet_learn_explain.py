"""Explain back and apply for fleet learn (phase 5, docs/design/four-desks.md 4.3).

The human explains a ledger row in their own words (explain) or answers a short scenario (apply). A `command`
grader grades the answer against the card's verbatim quote; without one (or when it fails) the human grades
themselves with the source shown. The result goes to fleet_learn.review(); this module never writes under
$FLEET_STATE/learn/ itself.

Grader contract (same shape as the WATCH classifier, lib/fleet_listen.py CommandClassifier):
  stdin   one JSON: {"card", "answer", "quote", "rubric", "format", "maxTokens"}
  stdout  one item per line, `KIND: text | quote: <verbatim words from the source> [| minor]`
          KIND in COVERED MISSING WRONG NOTE. Anything else is ignored and counted.
  bound   killed after `timeoutSec` (whole process group). A model behind the command needs its own
          max_tokens and deadline; maxTokens is passed through for it.
Grounding: an item's quote must be a normalized substring of the card's grounding text (the card's
grounding.quote, plus the rubric for apply cards). A WRONG line with no quote, or with a quote not in the source, is
dropped; the same rule applies to COVERED and MISSING, so no item is graded from the grader's own memory.
Rating handed to the scheduler: good on a pass, again on any fail.
Pass = at least one grounded COVERED, no grounded WRONG, no grounded core MISSING (`| minor` marks non-core).
A grader that times out, exits non-zero, or leaves no grounded item hands the card to self-grading; it never
yields a pass by silence.

$FLEET_STATE/learn/learn.json (read-only here): {"grader": {"kind": "command", "run": "...", "timeoutSec": 20, "maxTokens": 400}}
"""
import importlib.util
import json
import os
import re
import subprocess
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FORMATS = ("explain", "apply")
KINDS = ("COVERED", "MISSING", "WRONG", "NOTE")
LINE = re.compile(r"^(COVERED|MISSING|WRONG|NOTE):\s*(.*)$")
RATINGS = ("again", "hard", "good", "easy")
DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_TOKENS = 400

# Secret patterns: reuse WATCH's list so a grader cannot smuggle a token into reviews.jsonl.
try:
    _spec = importlib.util.spec_from_file_location("fleet_listen", os.path.join(HERE, "fleet_listen.py"))
    _listen = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_listen)
    is_secret = _listen.is_secret
except Exception:  # pragma: no cover - fleet_listen ships beside this file
    def is_secret(text):
        return False


def norm(s):
    return " ".join((s or "").lower().split())


def load_config(state=None):
    state = state or os.environ.get("FLEET_STATE") or os.path.expanduser("~/.local/state/fleet")
    path = os.path.join(state, "learn", "learn.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def grader_config(cfg):
    g = (cfg or {}).get("grader") or {}
    if g.get("kind") != "command" or not g.get("run"):
        return None
    return {"run": g["run"], "timeoutSec": float(g.get("timeoutSec", DEFAULT_TIMEOUT)),
            "maxTokens": int(g.get("maxTokens", DEFAULT_MAX_TOKENS))}


def run_bounded(cmd, payload, timeout):
    """Run a shell command with JSON on stdin; kill its process group on timeout. -> (ok, lines, error, ms)."""
    t0 = time.time()
    try:
        p = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError as e:
        return False, [], "could not start: %s" % e, 0
    try:
        out, _err = p.communicate(json.dumps(payload), timeout=timeout)
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


def grounding_texts(card):
    g = card.get("grounding") or {}
    texts = [g.get("quote") or ""]
    if card.get("format") == "apply":
        texts.append(card.get("answer") or "")
    return [norm(t) for t in texts if norm(t)]


def parse_lines(lines):
    """-> (items, ignored). Item: {"kind", "text", "quote", "minor"}."""
    items, ignored = [], 0
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        m = LINE.match(ln)
        if not m or not m.group(2).strip():
            ignored += 1
            continue
        parts = [p.strip() for p in m.group(2).split(" | ")]
        quote, minor = "", False
        for p in parts[1:]:
            if p.lower() == "minor":
                minor = True
                continue
            k, sep, v = p.partition(":")
            if sep and k.strip().lower() == "quote":
                quote = v.strip().strip("\"'")
        items.append({"kind": m.group(1), "text": parts[0], "quote": quote, "minor": minor})
    return items, ignored


def ground(items, card):
    """Keep only items whose quote is in the source. -> (kept, dropped list with reasons)."""
    texts = grounding_texts(card)
    kept, dropped = [], []
    for it in items:
        if it["kind"] == "NOTE":
            continue
        line = "%s %s" % (it["text"], it["quote"])
        if is_secret(line):
            dropped.append(dict(it, reason="secret"))
            continue
        q = norm(it["quote"])
        if not q:
            dropped.append(dict(it, reason="no quote"))
        elif not any(q in t for t in texts):
            dropped.append(dict(it, reason="quote not in source"))
        else:
            kept.append(it)
    return kept, dropped


def verdict(kept):
    covered = [i for i in kept if i["kind"] == "COVERED"]
    missing = [i for i in kept if i["kind"] == "MISSING"]
    wrong = [i for i in kept if i["kind"] == "WRONG"]
    core_missing = [i for i in missing if not i["minor"]]
    if wrong:
        grade, rating = "fail", "again"
    elif not covered:
        grade, rating = "fail", "again"
    elif core_missing:
        grade, rating = "fail", "again"  # a fail is a lapse for proof; schedule it soon too
    else:
        grade, rating = "pass", "good"
    detail = {k: [{"text": i["text"], "quote": i["quote"], **({"minor": True} if i["minor"] else {})}
                  for i in lst] for k, lst in (("covered", covered), ("missing", missing), ("wrong", wrong))}
    return grade, rating, detail


def request(card, answer, maxTokens):
    g = card.get("grounding") or {}
    return {"card": {k: card.get(k) for k in ("cid", "lid", "format", "prompt", "grounding")},
            "answer": answer, "quote": g.get("quote") or "",
            "rubric": card.get("answer") or "", "format": card.get("format"), "maxTokens": maxTokens}


def grade_with_command(card, answer, gcfg):
    """-> result dict. result["fallback"] is a reason string when the grader could not grade."""
    ok, lines, err, ms = run_bounded(gcfg["run"], request(card, answer, gcfg["maxTokens"]), gcfg["timeoutSec"])
    if not ok:
        return {"fallback": "grader failed: %s" % err, "latencyMs": ms}
    items, ignored = parse_lines(lines)
    kept, dropped = ground(items, card)
    notes = [i["text"] for i in items if i["kind"] == "NOTE" and not is_secret(i["text"])]
    if not kept:
        return {"fallback": "grader gave no grounded item (%d dropped, %d ignored)" % (len(dropped), ignored),
                "latencyMs": ms, "dropped": dropped, "ignored": ignored}
    grade, rating, detail = verdict(kept)
    detail["dropped"] = [{"kind": d["kind"], "text": "[redacted]" if d["reason"] == "secret" else d["text"],
                          "reason": d["reason"]} for d in dropped]
    detail["ignored"] = ignored
    if notes:
        detail["notes"] = notes
    return {"grade": grade, "rating": rating, "gradedBy": "command", "detail": detail, "latencyMs": ms}


def self_grade(card, rating, reason=None):
    r = (rating or "").strip().lower()
    if r not in RATINGS:
        raise ValueError("self grade must be one of %s" % "|".join(RATINGS))
    detail = {"selfRated": r}
    if reason:
        detail["fallback"] = reason
    return {"grade": "pass" if r in ("good", "easy") else "fail", "rating": r, "gradedBy": "self",
            "detail": detail}


def grade(card, answer, cfg=None, ask=None):
    """Grade one explain/apply answer. ask(card, reason) -> rating string, used for self-grading."""
    if card.get("format") not in FORMATS:
        raise ValueError("fleet_learn_explain grades explain and apply cards, not %r" % card.get("format"))
    if not (answer or "").strip():
        raise ValueError("empty answer")
    gcfg = grader_config(cfg)
    reason = "no grader configured"
    if gcfg:
        res = grade_with_command(card, answer, gcfg)
        if "fallback" not in res:
            return res
        reason = res["fallback"]
    if ask is None:
        raise RuntimeError("needs self-grading (%s) but no way to ask" % reason)
    return self_grade(card, ask(card, reason), reason)


def submit(learn, card, answer, cfg=None, ask=None, now=None):
    """Grade, then record through the learn API (fleet_learn.review). Returns review()'s result."""
    res = grade(card, answer, cfg=cfg, ask=ask)
    return learn.review(card["cid"], answer=answer, rating=res["rating"], now=now, grade=res["grade"],
                        gradedBy=res["gradedBy"], detail=res["detail"], latencyMs=res.get("latencyMs"))


# ---------------------------------------------------------------- terminal

def read_answer(inp, out):
    out.write("Your answer (1 to 5 sentences; blank line ends):\n")
    out.flush()
    lines = []
    for ln in inp:
        if not ln.strip():
            if lines:
                break
            continue
        lines.append(ln.rstrip("\n"))
    return " ".join(lines).strip()


def terminal_ask(inp, out):
    def ask(card, reason):
        g = card.get("grounding") or {}
        out.write("\nSelf-grade (%s).\n" % reason)
        out.write("Expected: %s\n" % (card.get("answer") or ""))
        out.write("Source %s: \"%s\"\n" % (g.get("source") or "?", g.get("quote") or ""))
        while True:
            out.write("Rate: [a]gain [h]ard [g]ood [e]asy > ")
            out.flush()
            ln = inp.readline()
            if not ln:
                raise EOFError("no rating given")
            k = ln.strip().lower()
            for r in RATINGS:
                if k and r.startswith(k):
                    return r
    return ask


def quiz_card(learn, card, cfg=None, inp=None, out=None, now=None, show_prompt=True):
    """Show one explain/apply card, read the answer, grade and record it. Used by `fleet learn quiz`
    (pass show_prompt=False when the caller already printed the card's prompt)."""
    inp, out = inp or sys.stdin, out or sys.stdout
    if show_prompt:
        out.write("\n[%s %s]\n%s\n" % (card["format"], card.get("cid"), card.get("prompt") or ""))
    answer = read_answer(inp, out)
    row = submit(learn, card, answer, cfg=cfg, ask=terminal_ask(inp, out), now=now)
    row = row if isinstance(row, dict) else {}
    d = row.get("detail") or {}
    out.write("%s (%s)\n" % (str(row.get("grade", "")).upper(), row.get("gradedBy", "")))
    for k in ("covered", "missing", "wrong"):
        for it in d.get(k) or []:
            out.write("  %s: %s  (\"%s\")\n" % (k, it["text"], it["quote"]))
    out.flush()
    return row
