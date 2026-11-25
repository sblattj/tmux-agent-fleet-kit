#!/usr/bin/env bash
# Issue #40: bootstrap.sh / fleet bootstrap. Everything runs against a throwaway HOME with a minimal PATH: stub brew,
# stub agent CLIs (claude, codex, gemini), a stub fleet (FLEET_BIN) that logs `desks up` and passes `remote` through to
# the real bin/fleet-remote inside FLEET_REMOTE_SANDBOX, a tripwire sudo that fails if called, and a stub ssh (OpenSSH
# reads the real ~/.ssh whatever HOME says). Never touches the real ~/.zshenv, ~/.ssh, CLI configs or the live fleet.
# One PASS/FAIL line per assertion; exit 1 on any FAIL.
set -u
for v in $(env | sed -n 's/^\(FLEET_[A-Za-z0-9_]*\)=.*/\1/p'); do unset "$v"; done
unset CODEX_HOME KIRO_HOME XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
REAL_HOME=$HOME
T=$(mktemp -d "${TMPDIR:-/tmp}/fleetboot.XXXXXX"); T=$(cd "$T" && pwd -P)
case $T in /*/fleetboot.*) ;; *) echo "FAIL bad scratch dir '$T'"; exit 1 ;; esac
trap '[ -n "${KEEP:-}" ] || rm -rf "${T:?}"' EXIT
fails=0
pass() { printf 'PASS %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }
t() { local label=$1; shift; if "$@" >/dev/null 2>&1; then pass "$label"; else fail "$label"; fi; }
has() { printf '%s' "$1" | grep -qF -- "$2"; }
hasnt() { ! printf '%s' "$1" | grep -qF -- "$2"; }
count() { local n; n=$(grep -F -c -- "$1" "$2" 2>/dev/null || true); printf '%s' "${n:-0}"; }
tree_sum() { (cd "$1" && find . -print0 2>/dev/null | sort -z | xargs -0 ls -ld 2>/dev/null; find . -type f -print0 | sort -z | xargs -0 cat 2>/dev/null) | cksum; }
real_sum() { { cat "$REAL_HOME/.zshenv" 2>/dev/null; cat "$REAL_HOME/.ssh/authorized_keys" 2>/dev/null; ls -la /etc/ssh/sshd_config.d 2>/dev/null; } | cksum; }
REAL_BEFORE=$(real_sum)

LOG=$T/calls.log; TRIP=$T/trip.log; : > "$LOG"; : > "$TRIP"
export STUBLOG=$LOG TRIPLOG=$TRIP STUBT=$T

# ---- minimal PATH: the system dirs, the tools the kit needs, and stubs. No real agent CLI, brew, sudo or ssh.
TOOLS=$T/tools STUBS=$T/stubs BREW=$T/brew
mkdir -p "$TOOLS" "$STUBS" "$BREW/bin"
for n in jq python3; do p=$(command -v "$n") && ln -s "$p" "$TOOLS/$n"; done # tmux and mosh come from the stub brew
for n in sudo ssh; do
  printf '#!/bin/sh\necho "TRIP %s $*" >> "$TRIPLOG"\nexit 97\n' "$n" > "$STUBS/$n"
done
cat > "$STUBS/brew" <<EOS
#!/bin/sh
echo "brew \$*" >> "\$STUBLOG"
case \$1 in
  --prefix) echo "$BREW" ;;
  install) shift; for f in "\$@"; do case \$f in mosh) b=mosh-server ;; *) b=\$f ;; esac
             printf '#!/bin/sh\necho "%s 9.9"\n' "\$b" > "$BREW/bin/\$b"; chmod +x "$BREW/bin/\$b"; done ;;
esac
EOS
for c in claude codex gemini; do printf '#!/bin/sh\necho "%s $*" >> "$STUBLOG"\nexit 0\n' "$c" > "$STUBS/$c"; done
# stub fleet: `desks up` is logged (never the live fleet); everything else goes to the real kit
cat > "$STUBS/fleet" <<EOS
#!/bin/sh
if [ "\$1" = desks ]; then echo "fleet \$*" >> "\$STUBLOG"; exit 0; fi
exec bash "$ROOT/bin/fleet" "\$@"
EOS
chmod +x "$STUBS"/*
clear_brew() { find "${BREW:?}/bin" -mindepth 1 -delete; } # the stub brew's installs, so tmux/mosh are missing again
BASEPATH=$STUBS:$BREW/bin:$TOOLS:/usr/bin:/bin:/usr/sbin:/sbin

# ---- the fleet-remote sandbox (same shape as test/test_remote.sh): stub sshd/systemsetup/socketfilterfw/nc/ssh-keygen
SB=$T/sb
mk_sandbox() {
  rm -rf "${SB:?}"; mkdir -p "$SB/bin" "$SB/etc/ssh/sshd_config.d"
  cat > "$SB/bin/sshd" <<'EOS'
#!/bin/sh
SB=$(cd "$(dirname "$0")/.." && pwd -P)
case $1 in
  -t) echo "RUN sshd -t" >> "$STUBLOG"; exit "${SSHD_T_RC:-0}" ;;
  -T) if grep -qx 'PasswordAuthentication no' "$SB"/etc/ssh/sshd_config.d/*.conf 2>/dev/null; then
        printf 'passwordauthentication no\nkbdinteractiveauthentication no\n'
      else printf 'passwordauthentication yes\n'; fi ;;
esac
EOS
  cat > "$SB/bin/systemsetup" <<'EOS'
#!/bin/sh
echo "RUN systemsetup $*" >> "$STUBLOG"
case "$*" in *"remotelogin on"*) touch "$STUBT/sshd-on" ;; esac
EOS
  printf '#!/bin/sh\necho "RUN launchctl $*" >> "$STUBLOG"\n' > "$SB/bin/launchctl"
  cat > "$SB/bin/socketfilterfw" <<'EOS'
#!/bin/sh
A=$STUBT/fw-apps; touch "$A"
case $1 in
  --getglobalstate) echo "Firewall is enabled. (State = 1)" ;;
  --listapps) cat "$A" ;;
  --getappblocked) if grep -qxF -- "$2" "$A"; then echo "$2 is permitted"; else echo "$2 is blocked"; fi ;;
  --add) echo "RUN socketfilterfw $*" >> "$STUBLOG"; grep -qxF -- "$2" "$A" || echo "$2" >> "$A" ;;
  --unblockapp) echo "RUN socketfilterfw $*" >> "$STUBLOG" ;;
esac
EOS
  printf '#!/bin/sh\n[ -f "$STUBT/sshd-on" ]\n' > "$SB/bin/nc"
  cat > "$SB/bin/ssh-keygen" <<'EOS'
#!/bin/sh
SB=$(cd "$(dirname "$0")/.." && pwd -P)
if [ "$1" = -A ]; then echo "RUN ssh-keygen -A" >> "$STUBLOG"; touch "$SB/etc/ssh/ssh_host_ed25519_key.pub"; exit 0; fi
exec /usr/bin/ssh-keygen "$@"
EOS
  # ssh inside the sandbox: `ssh ... localhost tmux -V` runs the command with the non-interactive ssh PATH
  cat > "$SB/bin/ssh" <<'EOS'
#!/bin/sh
echo "RUN ssh $*" >> "$STUBLOG"
[ -f "$STUBT/sshd-on" ] || { echo "ssh: connect to host localhost port 22: Connection refused" >&2; exit 255; }
while [ $# -gt 0 ]; do case $1 in -o|-i|-p|-l) shift 2 ;; -*) shift ;; *) break ;; esac; done
shift # the host
exec env -i HOME="$HOME" PATH=/usr/bin:/bin:/usr/sbin:/sbin /bin/zsh -c "$*"
EOS
  printf '#!/bin/sh\necho "TRIP sandbox sudo $*" >> "$TRIPLOG"\nexit 97\n' > "$SB/bin/sudo"
  chmod +x "$SB/bin"/*
  rm -f "$T/sshd-on" "$T/fw-apps"
}

newhome() { H=$T/home.$1; rm -rf "${H:?}"; mkdir -p "$H"; }
# run bootstrap as the CLI would see it: throwaway HOME, minimal PATH, stub fleet, sandboxed remote
B() {
  ( export HOME=$H PATH=$BASEPATH SHELL=/bin/zsh FLEET_BIN=$STUBS/fleet FLEET_REMOTE_SANDBOX=$SB FLEET_REMOTE_OS=Darwin \
      FLEET_REMOTE_SSH_SHELL=/bin/zsh FLEET_BOOTSTRAP_OS=Darwin FLEET_TMUX_SOCKET=bootstest-$$ FLEET_TMUX_SESSION=bsess \
      FLEET_CLAUDE_BIN=claude FLEET_CODEX_BIN=codex LANG=en_US.UTF-8
    "$@" )
}
BS() { B /bin/bash "$ROOT/bootstrap.sh" "$@" 2>&1; }

# =========================================================== (1) first run
mk_sandbox; newhome first; : > "$LOG"
out=$(BS); rc=$?
t "first run exits 0" test "$rc" = 0
t "brew installs only the missing tools (tmux mosh; jq is present)" grep -qx 'brew install tmux mosh' "$LOG"
t "brew is never asked for jq" bash -c '! grep -q "brew install.*jq" "$1"' _ "$LOG"
t "detects claude and runs install/claude.sh" has "$out" "install/claude.sh"
t "detects codex and runs install/codex.sh" has "$out" "install/codex.sh"
t "claude rules block landed in the throwaway HOME" test -f "$H/.claude/CLAUDE.md"
t "gemini is found but has no installer: printed as skipped" has "$out" "gemini: found"
t "opencode/kiro/agy not on PATH: not installed" test ! -e "$H/.config/opencode/opencode.json" -a ! -d "$H/.kiro"
t "installers ran with --no-desks (one desks step at the end)" test "$(grep -c '^fleet desks up' "$LOG")" = 1
t "desks come up with the first detected CLI" grep -qx 'fleet desks up --cli claude' "$LOG"
t "~/.zshenv gets the brew bin dir once" test "$(count "export PATH=\"$BREW/bin:\$PATH\"" "$H/.zshenv")" = 1
t "the zshenv block is marker-guarded" grep -qF '# >>> fleet bootstrap' "$H/.zshenv"
t "root script written under FLEET_STATE" test -f "$H/.local/state/fleet/bootstrap-root.sh"
t "prints a ! line for the human to run the root script" has "$out" "! sudo bash $H/.local/state/fleet/bootstrap-root.sh"
t "points at fleet bootstrap --verify for afterwards" has "$out" "fleet bootstrap --verify"
t "no sudo was called" test ! -s "$TRIP"
t "every step is printed" bash -c 'for s in "packages" "agent CLIs" "ssh PATH" "desks" "root steps"; do printf "%s" "$1" | grep -qF "$s" || exit 1; done' _ "$out"

# =========================================================== (2) idempotent second run
cp "$H/.zshenv" "$T/zshenv.1"; cp "$H/.claude/CLAUDE.md" "$T/claude.1"; : > "$LOG"
out=$(BS); rc=$?
t "second run exits 0" test "$rc" = 0
t "second run: brew not called to install" bash -c '! grep -q "^brew install" "$1"' _ "$LOG"
t "second run: ~/.zshenv unchanged (no duplicate line)" cmp -s "$T/zshenv.1" "$H/.zshenv"
t "second run: CLAUDE.md unchanged" cmp -s "$T/claude.1" "$H/.claude/CLAUDE.md"
t "second run: says tmux is already on the ssh PATH" has "$out" "already on the non-interactive ssh PATH"
t "second run: no sudo" test ! -s "$TRIP"

# =========================================================== (3) dry run writes nothing
mk_sandbox; newhome dry; clear_brew; : > "$LOG"
before=$(tree_sum "$H"); sbefore=$(tree_sum "$SB"); bbefore=$(tree_sum "$BREW")
out=$(BS --dry-run); rc=$?
t "dry run exits 0" test "$rc" = 0
t "dry run: HOME byte-identical" test "$before" = "$(tree_sum "$H")"
t "dry run: sandbox /etc untouched" test "$sbefore" = "$(tree_sum "$SB")"
t "dry run: brew not run" test "$bbefore" = "$(tree_sum "$BREW")" -a "$(grep -c '^brew install' "$LOG")" = 0
t "dry run: prints the brew command" has "$out" "would run: brew install tmux mosh"
t "dry run: prints the zshenv append" has "$out" "would append"
t "dry run: prints the root script body instead of writing it" has "$out" "010-fleet-remote-keyonly.conf"; [ -z "${DEBUG:-}" ] || printf "%s\n" "$out" > "$T/dry.out"
t "dry run: desks not brought up" test "$(grep -c '^fleet desks' "$LOG")" = 0
t "dry run: no sudo" test ! -s "$TRIP"

# =========================================================== (4) managed ~/.zshenv: print, never edit
mk_sandbox; newhome symlink; mkdir -p "$H/dotfiles"; printf '# my zshenv\n' > "$H/dotfiles/zshenv"; ln -s "$H/dotfiles/zshenv" "$H/.zshenv"
out=$(BS --no-desks)
t "symlinked ~/.zshenv: target unchanged" test "$(cat "$H/dotfiles/zshenv")" = "# my zshenv"
t "symlinked ~/.zshenv: still a symlink" test -L "$H/.zshenv"
t "symlinked ~/.zshenv: prints the line to add" has "$out" "export PATH=\"$BREW/bin:\$PATH\""
t "symlinked ~/.zshenv: says why" has "$out" "symlink"
: > "$LOG"; mk_sandbox; newhome marker; printf '# managed by chezmoi\nexport FOO=1\n' > "$H/.zshenv"; cp "$H/.zshenv" "$T/zm"
out=$(BS --no-desks)
t "dotfiles-marked ~/.zshenv: unchanged" cmp -s "$T/zm" "$H/.zshenv"
t "dotfiles-marked ~/.zshenv: prints the line" has "$out" "export PATH=\"$BREW/bin:\$PATH\""
t "--no-desks: no desks step" test "$(grep -c '^fleet desks' "$LOG")" = 0

# =========================================================== (5) root script: content, never run, and it works
mk_sandbox; newhome root
out=$(BS --no-desks); RS=$H/.local/state/fleet/bootstrap-root.sh
t "root script: never ran sudo" test ! -s "$TRIP"
real_rs=$( ( export HOME=$H PATH=$BASEPATH FLEET_REMOTE_OS=Darwin; unset FLEET_REMOTE_SANDBOX; bash "$ROOT/bin/fleet-remote" root-script - ) 2>&1 )
t "root script: outside a sandbox it refuses to run without root" has "$real_rs" '[ "$(id -u)" = 0 ] ||'
t "root script: outside a sandbox it targets /etc/ssh" has "$real_rs" "DROPIN=/etc/ssh/sshd_config.d/010-fleet-remote-keyonly.conf"
t "root script: writes the key-only drop-in" grep -qF 'PasswordAuthentication no' "$RS"
t "root script: validates with sshd -t and rolls back" bash -c 'grep -qF -- " -t" "$1" && grep -qi "roll" "$1"' _ "$RS"
t "root script: turns Remote Login on" grep -qF 'setremotelogin on' "$RS"
t "root script: allows mosh-server in the firewall" grep -qF -- '--unblockapp' "$RS"
t "root script: never invokes sudo (only comments and the refusal message name it)" bash -c '! printf "%s\n" "$1" | grep -v "^[[:space:]]*#" | grep -v "run me as root" | grep -qw sudo' _ "$real_rs"
t "root script: bash -n clean" bash -n "$RS"
: > "$LOG"
( export HOME=$H PATH=$BASEPATH STUBLOG=$LOG STUBT=$T; bash "$RS" ) > "$T/rs.out" 2>&1; rc=$?
t "root script runs green against the sandbox" test "$rc" = 0
t "root script: drop-in present afterwards" test -f "$SB/etc/ssh/sshd_config.d/010-fleet-remote-keyonly.conf"
t "root script: drop-in and sshd -t come before Remote Login" bash -c 'a=$(grep -n "RUN sshd -t" "$1" | head -1 | cut -d: -f1); b=$(grep -n "setremotelogin on" "$1" | head -1 | cut -d: -f1); [ -n "$a" ] && [ -n "$b" ] && [ "$a" -lt "$b" ]' _ "$LOG"
t "root script: firewall allow ran" grep -qF 'socketfilterfw --unblockapp' "$LOG"
( export HOME=$H PATH=$BASEPATH STUBLOG=$LOG STUBT=$T; bash "$RS" ) > "$T/rs2.out" 2>&1; rc=$?
t "root script: second run exits 0 (idempotent)" test "$rc" = 0
mk_sandbox; : > "$LOG"
( export HOME=$H PATH=$BASEPATH STUBLOG=$LOG STUBT=$T SSHD_T_RC=1; bash "$RS" ) > "$T/rs3.out" 2>&1; rc=$?
t "root script: sshd -t failure exits non-zero" test "$rc" != 0
t "root script: sshd -t failure rolls the drop-in back" test ! -f "$SB/etc/ssh/sshd_config.d/010-fleet-remote-keyonly.conf"
t "root script: sshd -t failure leaves Remote Login off" bash -c '! grep -q setremotelogin "$1"' _ "$LOG"

# =========================================================== (6) --verify, unprivileged
mk_sandbox; newhome verify
out=$(BS --verify); rc=$?
t "verify before the root script: exits non-zero" test "$rc" != 0
t "verify: reports sshd off" has "$out" "sshd: not listening"
t "verify: never calls sudo" test ! -s "$TRIP"
out=$(BS --no-desks); RS=$H/.local/state/fleet/bootstrap-root.sh
( export HOME=$H PATH=$BASEPATH STUBLOG=$LOG STUBT=$T; bash "$RS" ) >/dev/null 2>&1
mkdir -p "$H/.ssh"; /usr/bin/ssh-keygen -q -t ed25519 -N '' -f "$H/.ssh/id_ed25519" >/dev/null
cp "$H/.ssh/id_ed25519.pub" "$H/.ssh/authorized_keys"
: > "$LOG"
out=$(BS --verify); rc=$?; [ -z "${DEBUG:-}" ] || printf "%s\n" "$out" > "$T/verify.out"
t "verify after the root script: exits 0" test "$rc" = 0
t "verify: sshd listening" has "$out" "sshd: listening"
t "verify: drop-in present" has "$out" "drop-in: present"
t "verify: bare ssh localhost tmux -V works" has "$out" "ssh localhost tmux -V: tmux 9.9"
t "verify: ssh is BatchMode and writes no known_hosts" grep -q 'BatchMode=yes.*UserKnownHostsFile=/dev/null\|UserKnownHostsFile=/dev/null.*BatchMode=yes' "$LOG"
t "verify: mosh-server on the ssh PATH" has "$out" "mosh-server on the non-interactive ssh PATH"
t "verify: never calls sudo" test ! -s "$TRIP"
rm -f "$H/.ssh/authorized_keys" "$H/.ssh"/id_*
out=$(BS --verify)
t "verify with no key: ssh check skipped, not failed" has "$out" "skipped"

# =========================================================== (7) fleet bootstrap = the same logic
mk_sandbox; newhome cli; clear_brew; : > "$LOG"
out=$(B bash "$ROOT/bin/fleet" bootstrap --dry-run 2>&1); rc=$?
t "fleet bootstrap --dry-run exits 0" test "$rc" = 0
t "fleet bootstrap runs the same steps" has "$out" "would run: brew install tmux mosh"
t "fleet bootstrap --help names --verify" bash -c 'bash "$1" bootstrap --help | grep -q -- --verify' _ "$ROOT/bin/fleet"

# =========================================================== (8) curl | bash
mk_sandbox; newhome pipe; clear_brew; : > "$LOG"
# git stub: clone = copy this checkout (no network); the clone runs its own bootstrap.sh
cat > "$STUBS/git" <<EOS
#!/bin/sh
echo "git \$*" >> "\$STUBLOG"
if [ "\$1" = clone ]; then for a; do last=\$a; done; mkdir -p "\$last"; cp -R "$ROOT"/. "\$last"/; rm -rf "\$last/.git"; mkdir "\$last/.git"; exit 0; fi
exit 0
EOS
chmod +x "$STUBS/git"
out=$(B /bin/bash -s -- --no-desks < "$ROOT/bootstrap.sh" 2>&1); rc=$?
t "curl|bash: exits 0 under /bin/bash" test "$rc" = 0
t "curl|bash: clones the kit to the default dir" test -x "$H/.local/share/tmux-agent-fleet-kit/bin/fleet"
t "curl|bash: hands over to the clone's bootstrap.sh" has "$out" "$H/.local/share/tmux-agent-fleet-kit"
t "curl|bash: installs from the clone (bin/fleet symlink points into it)" test "$(readlink "$H/.local/bin/fleet")" = "$H/.local/share/tmux-agent-fleet-kit/bin/fleet"
t "curl|bash: no sudo" test ! -s "$TRIP"
out=$(B /bin/bash -s -- --no-desks < "$ROOT/bootstrap.sh" 2>&1); rc=$?
t "curl|bash: second run reuses the clone" bash -c '[ "$(grep -c "^git clone" "$1")" = 1 ]' _ "$LOG"
newhome trunc; : > "$LOG"
head -c "$(( $(wc -c < "$ROOT/bootstrap.sh") - 40 ))" "$ROOT/bootstrap.sh" > "$T/trunc.sh"
out=$(B /bin/bash -s < "$T/trunc.sh" 2>&1)
t "curl|bash: a truncated download runs nothing" bash -c '[ -z "$(ls -A "$1")" ] && [ ! -s "$2" ]' _ "$H" "$LOG"
newhome pipedry; before=$(tree_sum "$H")
out=$(B /bin/bash -s -- --dry-run < "$ROOT/bootstrap.sh" 2>&1); rc=$?
t "curl|bash --dry-run: exits 0" test "$rc" = 0
t "curl|bash --dry-run: would clone, clones nothing" bash -c 'printf "%s" "$1" | grep -q "would clone" && [ "$2" = "$3" ]' _ "$out" "$before" "$(tree_sum "$H")"
rm -f "$STUBS/git"

# =========================================================== (9) nothing escaped
t "tripwire: sudo never called anywhere" test ! -s "$TRIP"
t "real ~/.zshenv, ~/.ssh and /etc/ssh untouched" test "$REAL_BEFORE" = "$(real_sum)"

echo
if [ "$fails" = 0 ]; then echo "ALL PASS"; exit 0; else echo "$fails FAILED"; exit 1; fi
