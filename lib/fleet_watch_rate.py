"""WATCH per-source request rate limit (docs/design/four-desks.md 3.6): a token bucket per source, default
RATE_PER_MIN (30) requests a minute, set per source or in `defaults` with `ratePerMin`.

Every adapter makes its HTTP calls through `urllib.request.urlopen`. While the engine polls one source it installs a
gate (thread-local, so a server thread polling another source is not affected) and every urlopen call in that poll
takes one token first:
  - a token is there: the call goes out at once;
  - the next token is at most RATE_MAX_WAIT_SEC away: wait for it (the engine's injected sleep), then call;
  - further away: raise RateDeferred(wait) without calling out. The engine keeps the cursor, leaves failStreak alone
    and sets nextPollAt to when the bucket can pay for a poll again.
A response (or HTTP error) carrying `X-RateLimit-Remaining: 0` empties the bucket, so the rest of the poll waits or
defers instead of spending a request the server will refuse. Retry-After and X-Poll-Interval stay with the adapter
(SourceError.retry_after, next_poll_sec), as the contract says.

The bucket state is numbers only ({"tokens", "at"}), kept in cursors.json[name].bucket between runs.
Python 3.8+ stdlib only.
"""
import threading
import urllib.error
import urllib.request

RATE_MAX_WAIT_SEC = 5.0


class RateDeferred(Exception):
    """The bucket cannot pay for this request soon enough; nothing was sent."""

    def __init__(self, wait):
        super().__init__("rate limit: next request in %.0fs" % wait)
        self.wait = wait


class TokenBucket:
    def __init__(self, per_min, tokens=None, at=None):
        self.per_min = float(per_min)
        self.capacity = float(per_min)
        self.tokens = self.capacity if tokens is None else min(float(tokens), self.capacity)
        self.at = at  # epoch seconds of the last refill, None = never used

    @classmethod
    def from_dict(cls, per_min, d):
        d = d if isinstance(d, dict) else {}
        try:
            return cls(per_min, d.get("tokens"), d.get("at"))
        except (TypeError, ValueError):
            return cls(per_min)

    def to_dict(self):
        return {"tokens": round(self.tokens, 3), "at": self.at}

    def refill(self, t):
        if self.at is not None and t > self.at:
            self.tokens = min(self.capacity, self.tokens + (t - self.at) * self.per_min / 60.0)
        if self.at is None or t > self.at:
            self.at = t

    def wait_for(self, t, n=1.0):
        """Seconds until n tokens are there (0 = now). Does not take."""
        self.refill(t)
        short = n - self.tokens
        return 0.0 if short <= 1e-9 else short * 60.0 / self.per_min

    def take(self, t):
        """Take one token if there is one; return 0, else the wait in seconds (nothing taken)."""
        w = self.wait_for(t)
        if w == 0:
            self.tokens -= 1.0
        return w

    def drain(self, t):
        self.refill(t)
        self.tokens = min(self.tokens, 0.0)


class Gate:
    """One source's poll: the bucket, the clock (epoch seconds), and the sleep to wait with."""

    def __init__(self, bucket, clock, sleep, max_wait=RATE_MAX_WAIT_SEC):
        self.bucket = bucket
        self.clock = clock
        self.sleep = sleep
        self.max_wait = max_wait
        self.calls = 0
        self.waited = 0.0
        self.remaining = None

    def before(self):
        w = self.bucket.take(self.clock())
        if w == 0:
            return
        if w > self.max_wait:
            raise RateDeferred(w)
        self.sleep(w)
        self.waited += w
        if self.bucket.take(self.clock()) != 0:  # the sleep did not cover it (a clock that did not move)
            raise RateDeferred(self.bucket.wait_for(self.clock()))

    def after(self, headers):
        try:
            rem = headers.get("X-RateLimit-Remaining") if headers is not None else None
        except Exception:
            rem = None
        if rem is None:
            return
        try:
            self.remaining = int(str(rem).strip())
        except ValueError:
            return
        if self.remaining <= 0:
            self.bucket.drain(self.clock())


_local = threading.local()
_install_lock = threading.Lock()


def _wrap(inner):
    def urlopen(*a, **kw):
        g = getattr(_local, "gate", None)
        if g is None:
            return inner(*a, **kw)
        g.before()
        g.calls += 1
        try:
            resp = inner(*a, **kw)
        except urllib.error.HTTPError as e:
            g.after(e.headers)
            raise
        g.after(getattr(resp, "headers", None))
        return resp
    urlopen._fleet_watch_gate = inner
    return urlopen


def install():
    """Wrap whatever urllib.request.urlopen is now (idempotent). Without an active gate the wrapper passes through."""
    with _install_lock:
        if not getattr(urllib.request.urlopen, "_fleet_watch_gate", None):
            urllib.request.urlopen = _wrap(urllib.request.urlopen)


class gated:
    """with gated(gate): ... every urlopen call on this thread goes through the gate."""

    def __init__(self, gate):
        self.gate = gate

    def __enter__(self):
        install()
        self.prev = getattr(_local, "gate", None)
        _local.gate = self.gate
        return self.gate

    def __exit__(self, *exc):
        _local.gate = self.prev
        return False


def rate_per_min(cfg, src, default):
    """ratePerMin: the source entry < cfg.defaults < default. A bad value falls back to the default."""
    for v in (src.get("ratePerMin"), (cfg.get("defaults") or {}).get("ratePerMin")):
        if v is not None:
            return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else default
    return default


def config_errors(cfg):
    errs = []
    blocks = [("defaults", cfg.get("defaults") or {})] + [
        ("sources[%s]" % s.get("name"), s) for s in cfg.get("sources") or [] if isinstance(s, dict)]
    for where, b in blocks:
        v = b.get("ratePerMin") if isinstance(b, dict) else None
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0):
            errs.append("%s.ratePerMin must be a positive number" % where)
    return errs
