"""WATCH credential resolver (docs/design/four-desks.md 3.4).

A credential in watch.json is a reference, never a value:
  env:NAME            the environment variable NAME
  keychain:<service>  macOS `security find-generic-password -s <service> -w`; Linux `secret-tool lookup service <service>`
  cmd:<argv>          the first stdout line of <argv> (split with shlex, no shell)

resolve_auth() turns one auth block into {"type", "token", "user", ...} for a single poll. The caller holds the result in
memory only; nothing here logs, prints or stores a value. A literal value is refused (Refused), and the error text
names the key only, never the value.

Python 3.8+ stdlib only.
"""
import os
import shlex
import subprocess
import sys

_LIB = os.path.dirname(os.path.abspath(__file__))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import fleet_watch_contract as C  # noqa: E402

RESOLVE_TIMEOUT_SEC = 10
REF_KEYS = C.SECRET_KEYS + ("user",)  # user may be a reference too; a literal user name is allowed


class Refused(ValueError):
    """A literal credential, or a reference that does not resolve. The message never holds a value."""


def _run(argv):
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, timeout=RESOLVE_TIMEOUT_SEC, start_new_session=True)
    except FileNotFoundError:
        raise Refused("%s not found" % argv[0])
    except subprocess.TimeoutExpired:
        raise Refused("%s timed out after %ds" % (argv[0], RESOLVE_TIMEOUT_SEC))
    if p.returncode != 0:
        raise Refused("%s exited %d" % (argv[0], p.returncode))
    line = (p.stdout or "").split("\n", 1)[0].strip()
    if not line:
        raise Refused("%s printed nothing" % argv[0])
    return line


def keychain_argv(service, platform=None):
    platform = platform or sys.platform
    if platform == "darwin":
        return ["security", "find-generic-password", "-s", service, "-w"]
    return ["secret-tool", "lookup", "service", service]


def resolve_ref(ref, env=None):
    """Resolve one reference to its value. Raises Refused for a literal or an unresolvable reference."""
    env = os.environ if env is None else env
    try:
        scheme, arg = C.parse_ref(ref)
    except ValueError:
        raise Refused("not a credential reference (use env:, keychain: or cmd:)")
    if scheme == "env":
        v = env.get(arg)
        if not v:
            raise Refused("env:%s is not set" % arg)
        return v
    if scheme == "keychain":
        return _run(keychain_argv(arg))
    try:
        argv = shlex.split(arg)
    except ValueError:
        raise Refused("cmd: reference does not parse")
    if not argv:
        raise Refused("cmd: reference is empty")
    return _run(argv)


def resolve_auth(auth, env=None):
    """One auth block -> resolved dict for this poll. None/{} or type none -> {}."""
    if not auth:
        return {}
    errs = C.auth_errors(auth, "auth")
    if errs:
        raise Refused("; ".join(errs))
    out = {"type": auth.get("type", "bearer")}
    if out["type"] == "none":
        return {}
    for k, v in auth.items():
        if k == "type":
            continue
        if k in C.SECRET_KEYS:
            out[k] = resolve_ref(v, env)
        elif k == "user" and C.is_credential_ref(v):
            out[k] = resolve_ref(v, env)
        else:
            out[k] = v
    return out


def check_refs(cfg, env=None, resolve=False):
    """[(where, ok, note)] for every auth block in a watch.json dict. Notes never hold a value.
    resolve=False only checks the shape; resolve=True also resolves each reference (runs keychain/cmd)."""
    rows = []
    blocks = [("sources[%s]" % s.get("name"), s.get("auth")) for s in cfg.get("sources") or [] if isinstance(s, dict)]
    if isinstance(cfg.get("probe"), dict):
        blocks.append(("probe", cfg["probe"].get("auth")))
    for where, auth in blocks:
        if not auth:
            continue
        errs = C.auth_errors(auth, where)
        if errs:
            rows.extend((where, False, e) for e in errs)
            continue
        if not resolve:
            rows.append((where, True, "references only"))
            continue
        try:
            got = resolve_auth(auth, env)
            keys = ", ".join(sorted(k for k in got if k != "type")) or "none"
            rows.append((where, True, "resolves (%s)" % keys))
        except Refused as e:
            rows.append((where, False, str(e)))
    return rows
