"""FSRS spaced-repetition scheduling for fleet learn, stdlib only.

Attribution: the memory model (the formulas for stability, difficulty and retrievability, the learning and
relearning step rules, and the 21 default weights) follows py-fsrs by Open Spaced Repetition,
https://github.com/open-spaced-repetition/py-fsrs (MIT License, Copyright (c) 2022 Open Spaced Repetition),
read at commit e40a5a74799a12c9b15e01559a9b2c9f6be7a648 (v6.3.3). This file is a fresh stdlib reimplementation
over plain dict schedules; it does not import or vendor that package. Fuzz is left out on purpose so a replay of
reviews.jsonl always gives the same schedule.

Hook: fleet_learn.use_scheduler(fleet_fsrs.schedule). The contract shape is
    schedule(sched: dict, rating: str, now: datetime) -> dict
rating is one of again|hard|good|easy (or 1..4). Keys this module owns inside `sched`:
    due, stability, difficulty, state (learning|review|relearning), step, lastReview, reps, lapses, scheduler
Any other key (for example phase 3's fixed-interval `step`) is copied through untouched.
"""
import json
import math
import os
from datetime import datetime, timedelta, timezone

# py-fsrs DEFAULT_PARAMETERS (FSRS-6), e40a5a7 fsrs/scheduler.py. w[20] is the forgetting-curve decay.
DEFAULT_WEIGHTS = (
    0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194, 0.001, 1.8722, 0.1666, 0.796,
    1.4835, 0.0614, 0.2629, 1.6483, 0.6014, 1.8729, 0.5425, 0.0912, 0.0658, 0.1542,
)
DESIRED_RETENTION = 0.9
LEARNING_STEPS = (timedelta(minutes=1), timedelta(minutes=10))
RELEARNING_STEPS = (timedelta(minutes=10),)
MAXIMUM_INTERVAL = 36500
STABILITY_MIN = 0.001
D_MIN, D_MAX = 1.0, 10.0

AGAIN, HARD, GOOD, EASY = 1, 2, 3, 4
RATINGS = {"again": AGAIN, "hard": HARD, "good": GOOD, "easy": EASY}
RATING_NAMES = {v: k for k, v in RATINGS.items()}
LEARNING, REVIEW, RELEARNING = "learning", "review", "relearning"


def parse_rating(r):
    if isinstance(r, bool):
        raise ValueError("rating must be again|hard|good|easy or 1..4, got %r" % (r,))
    if isinstance(r, int) and r in RATING_NAMES:
        return r
    if isinstance(r, str) and r.strip().lower() in RATINGS:
        return RATINGS[r.strip().lower()]
    raise ValueError("rating must be again|hard|good|easy or 1..4, got %r" % (r,))


def iso(dt):
    """Whole-second UTC, the only form fleet_learn's timestamp parser reads."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    if s is None or isinstance(s, datetime):
        return s
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class FSRS:
    """One scheduler configuration. Pure: review() takes a schedule dict and returns a new one."""

    def __init__(self, weights=DEFAULT_WEIGHTS, desired_retention=DESIRED_RETENTION,
                 learning_steps=LEARNING_STEPS, relearning_steps=RELEARNING_STEPS,
                 maximum_interval=MAXIMUM_INTERVAL):
        w = tuple(float(x) for x in weights)
        if len(w) != 21:
            raise ValueError("FSRS needs 21 weights, got %d" % len(w))
        if not 0 < desired_retention < 1:
            raise ValueError("desired retention must be in (0, 1), got %r" % (desired_retention,))
        if w[20] <= 0:
            raise ValueError("weight 20 (decay) must be positive")
        self.w = w
        self.desired_retention = float(desired_retention)
        self.learning_steps = tuple(learning_steps)
        self.relearning_steps = tuple(relearning_steps)
        self.maximum_interval = int(maximum_interval)
        self.decay = -w[20]
        # The curve is anchored so that R = 0.9 when elapsed days == stability (a fixed 0.9, not the target).
        self.factor = 0.9 ** (1 / self.decay) - 1

    # ---- the memory model
    def retrievability(self, stability, elapsed_days):
        return (1 + self.factor * max(0, elapsed_days) / stability) ** self.decay

    def init_stability(self, g):
        return max(self.w[g - 1], STABILITY_MIN)

    def init_difficulty(self, g, clamp=True):
        d = self.w[4] - math.exp(self.w[5] * (g - 1)) + 1
        return min(max(d, D_MIN), D_MAX) if clamp else d

    def next_difficulty(self, d, g):
        delta = -self.w[6] * (g - 3)
        damped = d + (10.0 - d) * delta / 9.0
        target = self.init_difficulty(EASY, clamp=False)
        nd = self.w[7] * target + (1 - self.w[7]) * damped
        return min(max(nd, D_MIN), D_MAX)

    def short_term_stability(self, s, g):
        inc = math.exp(self.w[17] * (g - 3 + self.w[18])) * s ** -self.w[19]
        if g != AGAIN:
            inc = max(inc, 1.0)
        return max(s * inc, STABILITY_MIN)

    def recall_stability(self, d, s, r, g):
        hard = self.w[15] if g == HARD else 1.0
        easy = self.w[16] if g == EASY else 1.0
        return s * (1 + math.exp(self.w[8]) * (11 - d) * s ** -self.w[9]
                    * (math.exp((1 - r) * self.w[10]) - 1) * hard * easy)

    def forget_stability(self, d, s, r):
        long_term = (self.w[11] * d ** -self.w[12] * ((s + 1) ** self.w[13] - 1)
                     * math.exp((1 - r) * self.w[14]))
        short_term = s / math.exp(self.w[17] * self.w[18])
        return min(long_term, short_term)

    def next_stability(self, d, s, r, g):
        ns = self.forget_stability(d, s, r) if g == AGAIN else self.recall_stability(d, s, r, g)
        return max(ns, STABILITY_MIN)

    def interval_days(self, s):
        ivl = s / self.factor * (self.desired_retention ** (1 / self.decay) - 1)
        return min(max(round(ivl), 1), self.maximum_interval)

    # ---- steps
    def _step_interval(self, steps, step, g):
        """(new_state_is_review, new_step, interval) for a learning or relearning card."""
        if not steps or (step >= len(steps) and g != AGAIN):
            return True, None, None
        if g == AGAIN:
            return False, 0, steps[0]
        if g == HARD:
            if step == 0 and len(steps) == 1:
                return False, step, steps[0] * 1.5
            if step == 0:
                return False, step, (steps[0] + steps[1]) / 2.0
            return False, step, steps[step]
        if g == GOOD:
            if step + 1 == len(steps):
                return True, None, None
            return False, step + 1, steps[step + 1]
        return True, None, None  # EASY

    def review(self, sched, rating, now):
        g = parse_rating(rating)
        now = parse_iso(now)
        if now is None:
            raise ValueError("review needs now=")
        out = dict(sched or {})
        fresh = out.get("state") not in (LEARNING, REVIEW, RELEARNING) or out.get("stability") is None \
            or out.get("difficulty") is None
        state = LEARNING if fresh else out["state"]
        step = 0 if fresh else out.get("step")
        last = None if fresh else parse_iso(out.get("lastReview"))
        days = (now - last).days if last is not None else None  # floored, like timedelta.days
        s, d = out.get("stability"), out.get("difficulty")

        if fresh:
            s, d = self.init_stability(g), self.init_difficulty(g)
        elif days is not None and days < 1:
            s = self.short_term_stability(s, g)
            d = self.next_difficulty(d, g)
        else:
            r = self.retrievability(s, days if days is not None else 0)
            s = self.next_stability(d, s, r, g)
            d = self.next_difficulty(d, g)

        if state == REVIEW:
            if g == AGAIN and self.relearning_steps:
                state, step, ivl = RELEARNING, 0, self.relearning_steps[0]
            else:
                step, ivl = None, timedelta(days=self.interval_days(s))
        else:
            steps = self.learning_steps if state == LEARNING else self.relearning_steps
            if step is None:
                step = 0
            graduate, step, ivl = self._step_interval(steps, step, g)
            if graduate:
                state, step, ivl = REVIEW, None, timedelta(days=self.interval_days(s))

        if g == AGAIN and not fresh and (sched or {}).get("state") == REVIEW:
            out["lapses"] = int(out.get("lapses") or 0) + 1
        out["lapses"] = int(out.get("lapses") or 0)
        out["reps"] = int(out.get("reps") or 0) + 1
        out.update({"due": iso(now + ivl), "lastReview": iso(now), "state": state, "step": step,
                    "stability": s, "difficulty": d, "scheduler": "fsrs"})
        return out


_DEFAULT = FSRS()


def scheduler_from_config(cfg):
    """learn.json {"fsrs": {"desiredRetention": 0.9, "weights": [...21], "maximumInterval": 36500}}."""
    c = (cfg or {}).get("fsrs") or {}
    return FSRS(weights=c.get("weights") or DEFAULT_WEIGHTS,
                desired_retention=c.get("desiredRetention", DESIRED_RETENTION),
                maximum_interval=c.get("maximumInterval", MAXIMUM_INTERVAL))


def _state_dir():
    return os.environ.get("FLEET_STATE") or os.path.expanduser("~/.local/state/fleet")


_CACHE = {"key": None, "fsrs": _DEFAULT}


def configured():
    """The FSRS built from $FLEET_STATE/learn/learn.json (re-read when the file changes); defaults if absent.

    A malformed fsrs section raises ValueError: a bad retention must not silently fall back to 0.9."""
    path = os.path.join(_state_dir(), "learn", "learn.json")
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        return _DEFAULT
    if _CACHE["key"] != key:
        try:
            with open(path) as f:
                cfg = json.load(f)
        except ValueError as e:
            raise ValueError("learn.json is not valid JSON: %s" % e)
        _CACHE["fsrs"], _CACHE["key"] = scheduler_from_config(cfg), key
    return _CACHE["fsrs"]


def schedule(sched, rating, now):
    """The use_scheduler hook: fn(sched, rating, now) -> sched. Weights and desired retention from learn.json."""
    return configured().review(sched, rating, now)


def retrievability(sched, now, fsrs=None):
    """Predicted recall probability now; 0.0 for a card never reviewed under FSRS."""
    f = fsrs or configured()
    last = parse_iso((sched or {}).get("lastReview"))
    s = (sched or {}).get("stability")
    if last is None or s is None:
        return 0.0
    return f.retrievability(s, (parse_iso(now) - last).days)
