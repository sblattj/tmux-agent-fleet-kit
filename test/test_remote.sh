#!/usr/bin/env bash
# Issue #22: fleet remote setup|status|qr|off. Everything runs in a sandbox: scratch HOME, FLEET_STATE and sshd dir,
# a private tmux socket, stub sudo/sshd/systemsetup/socketfilterfw/launchctl/nc/ssh-keygen/ifconfig/pmset, and a
# tripwire PATH that fails any call to a real privileged tool. Nothing here runs sudo or touches this host's sshd,
# firewall or ~/.ssh. Exit 1 on any FAIL.
set -u
for v in $(env | sed -n 's/^\(FLEET_[A-Za-z0-9_]*\)=.*/\1/p'); do unset "$v"; done
HERE=$(cd "$(dirname "$0")" && pwd -P)
FLEET=$HERE/../bin/fleet
REAL_HOME=$HOME
T=$(mktemp -d "${TMPDIR:-/tmp}/fleetremote.XXXXXX")
T=$(cd "$T" && pwd -P)
export FLEET_TMUX_SOCKET=fleetremote-$$
export FLEET_TMUX_SESSION=rsess
export FLEET_STATE=$T/state
export FLEET_REMOTE_OS=Darwin
export LANG=en_US.UTF-8
LOG=$T/calls.log
TRIP=$T/tripwire.log
export STUBLOG=$LOG TRIPLOG=$TRIP STUBT=$T

# real-host fingerprints, compared at the end
fp_real() {
  { [ -f "$REAL_HOME/.ssh/authorized_keys" ] && cksum < "$REAL_HOME/.ssh/authorized_keys" || echo none
    ls -la /etc/ssh/sshd_config.d 2>/dev/null || echo none; } | cksum
}
REAL_BEFORE=$(fp_real)

cleanup() { tmux -L "$FLEET_TMUX_SOCKET" kill-server >/dev/null 2>&1; rm -rf "$T"; }
trap cleanup EXIT

# ---- tripwire: the real privileged tools, first on PATH, log and fail
mkdir -p "$T/trip"
for n in sudo systemsetup socketfilterfw launchctl osascript sshd systemctl pmset; do
  printf '#!/bin/sh\necho "TRIP %s $*" >> "$TRIPLOG"\nexit 97\n' "$n" > "$T/trip/$n"; chmod +x "$T/trip/$n"
done

# ---- mosh-server: a symlink into a fake versioned Cellar path, outside the default non-login PATH
mkdir -p "$T/cellar/mosh/9.9.9/bin" "$T/path"
printf '#!/bin/sh\nexit 0\n' > "$T/cellar/mosh/9.9.9/bin/mosh-server"; chmod +x "$T/cellar/mosh/9.9.9/bin/mosh-server"
ln -s "$T/cellar/mosh/9.9.9/bin/mosh-server" "$T/path/mosh-server"
export PATH="$T/trip:$T/path:$PATH"

# ---- sandbox stubs
mk_sandbox() { # mk_sandbox DIR: fresh sandbox (stub bin, empty etc/ssh)
  local d=$1
  rm -rf "$d"; mkdir -p "$d/bin" "$d/etc/ssh/sshd_config.d"
  cat > "$d/bin/sudo" <<'EOS'
#!/bin/sh
while [ $# -gt 0 ]; do case $1 in -A|-n) shift ;; -v) echo "RO sudo -v" >> "$STUBLOG"; exit 0 ;; *) break ;; esac; done
[ "$1" = true ] && exit 0
case $(basename "$1") in tee|chmod|mkdir|rm|cp|install) echo "MUT sudo $*" >> "$STUBLOG" ;; *) echo "RO sudo $*" >> "$STUBLOG" ;; esac
exec "$@"
EOS
  cat > "$d/bin/sshd" <<'EOS'
#!/bin/sh
SB=$(cd "$(dirname "$0")/.." && pwd -P)
case $1 in
  -t) exit "${SSHD_T_RC:-0}" ;;
  -T) if [ -z "${SSHD_FORCE_PW:-}" ] && grep -qx 'PasswordAuthentication no' "$SB"/etc/ssh/sshd_config.d/*.conf 2>/dev/null; then
        printf 'passwordauthentication no\nkbdinteractiveauthentication no\n'
      else printf 'passwordauthentication yes\nkbdinteractiveauthentication yes\n'; fi ;;
esac
EOS
  cat > "$d/bin/systemsetup" <<'EOS'
#!/bin/sh
echo "MUT systemsetup $*" >> "$STUBLOG"
case "$*" in *"remotelogin on"*) touch "$STUBT/sshd-on" ;; *"remotelogin off"*) rm -f "$STUBT/sshd-on" ;; esac
EOS
  cat > "$d/bin/launchctl" <<'EOS'
#!/bin/sh
echo "MUT launchctl $*" >> "$STUBLOG"
EOS
  cat > "$d/bin/socketfilterfw" <<'EOS'
#!/bin/sh
A=$STUBT/fw-apps; touch "$A"
case $1 in
  --getglobalstate) echo "Firewall is enabled. (State = 1)" ;;
  --listapps) cat "$A" ;;
  --getappblocked) if grep -qxF -- "$2" "$A"; then echo "$2 is permitted"; else echo "$2 is blocked"; fi ;;
  --add) echo "MUT socketfilterfw $*" >> "$STUBLOG"; grep -qxF -- "$2" "$A" || echo "$2" >> "$A" ;;
  --unblockapp) echo "MUT socketfilterfw $*" >> "$STUBLOG" ;;
esac
EOS
  cat > "$d/bin/nc" <<'EOS'
#!/bin/sh
[ -f "$STUBT/sshd-on" ]
EOS
  cat > "$d/bin/ssh-keygen" <<'EOS'
#!/bin/sh
SB=$(cd "$(dirname "$0")/.." && pwd -P)
if [ "$1" = -A ]; then echo "MUT ssh-keygen -A" >> "$STUBLOG"; touch "$SB/etc/ssh/ssh_host_ed25519_key.pub"; exit 0; fi
exit 1
EOS
  cat > "$d/bin/ifconfig" <<'EOS'
#!/bin/sh
cat <<'EOF'
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
	inet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	inet 198.51.100.20 netmask 0xffffff00 broadcast 198.51.100.255
feth4242: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 2800
	inet 192.0.2.10 netmask 0xffffff00 broadcast 192.0.2.255
EOF
EOS
  cat > "$d/bin/pmset" <<'EOS'
#!/bin/sh
printf 'System-wide power settings:\n sleep                0\n'
EOS
  cat > "$d/bin/osascript" <<'EOS'
#!/bin/sh
echo "MUT osascript" >> "$STUBLOG"; exit 1
EOS
  chmod +x "$d"/bin/*
}

FAILS=0
t() { # label cmd...
  local label=$1; shift
  if "$@" >/dev/null 2>&1; then echo "PASS $label"; else echo "FAIL $label"; FAILS=$((FAILS+1)); return 1; fi
}
has() { printf '%s' "$1" | grep -qF -- "$2"; }
hasnt() { ! printf '%s' "$1" | grep -qF -- "$2"; }
mut() { grep -c '^MUT' "$LOG" 2>/dev/null || true; }
fresh() { # fresh: new sandbox, scratch HOME and state, sshd off, empty logs
  export FLEET_REMOTE_SANDBOX=$T/sb
  mk_sandbox "$FLEET_REMOTE_SANDBOX"
  export HOME=$T/home; rm -rf "$HOME" "$FLEET_STATE" "$T/sshd-on" "$T/fw-apps"; mkdir -p "$HOME" "$FLEET_STATE"
  : > "$LOG"
}
R() { "$FLEET" remote "$@" 2>&1 </dev/null; }
DROPIN=$T/sb/etc/ssh/sshd_config.d/010-fleet-remote-keyonly.conf
MOSH_REAL=$T/cellar/mosh/9.9.9/bin/mosh-server

# a fleet tmux server on the private socket, window-size not yet latest
tmux -L "$FLEET_TMUX_SOCKET" -f /dev/null new-session -d -s "$FLEET_TMUX_SESSION" 'sleep 600'
tmux -L "$FLEET_TMUX_SOCKET" set -g window-size smallest
ssh-keygen -q -t ed25519 -N '' -C phone -f "$T/phonekey" >/dev/null
PUB=$T/phonekey.pub

# ---- (0) dispatch and help
out=$("$FLEET" remote --help 2>&1)
t "fleet remote --help lists setup|status|qr|off" bash -c 'for w in "remote setup" "remote status" "remote qr" "remote off"; do printf "%s" "$1" | grep -qF "$w" || exit 1; done' _ "$out"
t "help warns never to put the tmux socket on raw TCP" has "$out" "NEVER expose the tmux socket over raw TCP"

# ---- (1) --dry-run changes nothing
fresh
out=$(R setup --pubkey "$PUB" --dry-run); rc=$?
t "dry-run exits 0" test "$rc" = 0
t "dry-run logs no mutating stub call" test "$(mut)" = 0
t "dry-run writes no drop-in" test ! -e "$DROPIN"
t "dry-run leaves authorized_keys alone" test ! -e "$HOME/.ssh/authorized_keys"
t "dry-run writes no remote.json" test ! -e "$FLEET_STATE/remote.json"
t "dry-run leaves window-size" test "$(tmux -L "$FLEET_TMUX_SOCKET" show -gv window-size)" = smallest
t "dry-run prints the drop-in body" has "$out" "PasswordAuthentication no"
t "dry-run prints the Remote Login step" has "$out" "[dry-run] would: turn on Remote Login"
t "dry-run says nothing changed" has "$out" "dry run: nothing changed"

# ---- (2) setup
fresh
out=$(R setup --pubkey "$PUB"); rc=$?
t "setup exits 0" test "$rc" = 0 || printf '%s\n' "$out" | sed 's/^/    /'
t "drop-in installed, key-only" grep -qx 'PasswordAuthentication no' "$DROPIN"
t "drop-in turns off keyboard-interactive" grep -qx 'KbdInteractiveAuthentication no' "$DROPIN"
t "drop-in mode 644" test "$(stat -f %Lp "$DROPIN" 2>/dev/null || stat -c %a "$DROPIN")" = 644
dl=$(grep -n 'MUT sudo tee .*010-fleet-remote' "$LOG" | head -1 | cut -d: -f1)
rl=$(grep -n 'MUT systemsetup -setremotelogin on' "$LOG" | head -1 | cut -d: -f1)
t "drop-in is written before Remote Login is switched on" test -n "$dl" -a -n "$rl" -a "${dl:-9}" -lt "${rl:-0}"
t "host keys generated when missing (ssh-keygen -A)" grep -qx 'MUT ssh-keygen -A' "$LOG"
t "sshd -t validated the drop-in" grep -q 'RO sudo .*/sshd -t' "$LOG"
t "authorized_keys holds the key once" test "$(grep -cF "$(cut -d' ' -f2 "$PUB")" "$HOME/.ssh/authorized_keys")" = 1
t "authorized_keys mode 600" test "$(stat -f %Lp "$HOME/.ssh/authorized_keys" 2>/dev/null || stat -c %a "$HOME/.ssh/authorized_keys")" = 600
t "~/.ssh mode 700" test "$(stat -f %Lp "$HOME/.ssh" 2>/dev/null || stat -c %a "$HOME/.ssh")" = 700
t "firewall gets the resolved Cellar path, not the symlink" grep -qxF "MUT socketfilterfw --add $MOSH_REAL" "$LOG"
t "firewall unblocks the resolved path" grep -qxF "MUT socketfilterfw --unblockapp $MOSH_REAL" "$LOG"
t "no firewall call names the symlink" bash -c '! grep -F "socketfilterfw --add $1" "$2"' _ "$T/path/mosh-server" "$LOG"
t "upgrade warning printed" has "$out" "rerun setup after upgrading mosh"
t "window-size set to latest" test "$(tmux -L "$FLEET_TMUX_SOCKET" show -gv window-size)" = latest
t "remote.json records sshd was off before setup" grep -q '"sshd_was_on_before_setup": false' "$FLEET_STATE/remote.json"
t "overlay IP detected from the ZeroTier interface" has "$out" "overlay address: 192.0.2.10 (zerotier)"
t "attach line uses --server for an off-PATH mosh-server" has "$out" "mosh --server=$T/path/mosh-server $(id -un)@192.0.2.10 -- "
t "attach line targets the private socket and session" has "$out" "-L $FLEET_TMUX_SOCKET attach -t $FLEET_TMUX_SESSION"
t "setup repeats the raw-TCP warning" has "$out" "never expose the tmux socket over raw TCP"

# ---- (3) running it twice is safe
: > "$LOG"
out=$(R setup --pubkey "$PUB"); rc=$?
t "second setup exits 0" test "$rc" = 0
t "second setup makes no mutating call" test "$(mut)" = 0 || cat "$LOG" | sed 's/^/    /'
t "second setup never asks sudo for a password" bash -c '! grep -q "sudo -v" "$1"' _ "$LOG"
t "key still present exactly once" test "$(grep -cF "$(cut -d' ' -f2 "$PUB")" "$HOME/.ssh/authorized_keys")" = 1
t "second setup reports everything in place" has "$out" "drop-in already in place"

# ---- (4) status after setup
out=$(R status)
t "status: sshd listening" has "$out" "sshd: listening"
t "status: key-only from sshd -T" has "$out" "auth: key-only (sshd -T)"
t "status: says sshd listens on every interface" has "$out" "key-only auth is the guard"
t "status: mosh-server off the non-login PATH" has "$out" "not on a non-login ssh PATH"
t "status: firewall allows mosh-server" has "$out" "firewall mosh-server: allowed"
t "status: overlay address" has "$out" "overlay: 192.0.2.10 (zerotier)"
t "status: window-size latest" has "$out" "window-size: latest"
t "status: sleep" has "$out" "sleep: off"

# ---- (5) qr
out=$(R qr)
t "qr prints the attach command" has "$out" "mosh --server=$T/path/mosh-server $(id -un)@192.0.2.10 -- "
t "qr without qrencode says how to get a QR code" has "$out" "install qrencode"
t "qr names Blink and Termius" bash -c 'printf "%s" "$1" | grep -q Blink && printf "%s" "$1" | grep -q Termius' _ "$out"
out=$(R qr --read-only)
t "qr --read-only adds -r to the attach" bash -c 'printf "%s\n" "$1" | head -1 | grep -q "attach -t $2 -r$"' _ "$out" "$FLEET_TMUX_SESSION"
printf '#!/bin/sh\necho "QR[$*]"\n' > "$T/path/qrencode"; chmod +x "$T/path/qrencode"
out=$(R qr)
t "qr pipes the command to qrencode when present" has "$out" "QR[-t ANSIUTF8 -- mosh "
rm -f "$T/path/qrencode"
out=$(R qr --ip 203.0.113.5)
t "qr --ip overrides the detected address" has "$out" "@203.0.113.5 -- "

# ---- (6) Tailscale detection: CLI first, then the CGNAT range on an interface
printf '#!/bin/sh\n[ "$1 $2" = "ip -4" ] && echo 100.64.0.7\n' > "$T/sb/bin/tailscale"; chmod +x "$T/sb/bin/tailscale"
out=$(R qr)
t "tailscale ip -4 wins when the CLI is there" has "$out" "@100.64.0.7 -- "
rm -f "$T/sb/bin/tailscale"
cat > "$T/sb/bin/ifconfig" <<'EOS'
#!/bin/sh
printf 'en0: flags=1\n\tinet 198.51.100.20 netmask 0xffffff00\nutun4: flags=1\n\tinet 100.101.102.103 --> 100.101.102.103\n'
EOS
out=$(R status)
t "a 100.64/10 interface address reads as tailscale" has "$out" "overlay: 100.101.102.103 (tailscale)"
printf '#!/bin/sh\nprintf "en0: flags=1\\n\\tinet 198.51.100.20 netmask 0xffffff00\\n"\n' > "$T/sb/bin/ifconfig"
out=$(R qr)
t "no overlay: qr warns and prints a placeholder" bash -c 'printf "%s" "$1" | grep -q "pass --ip" && printf "%s" "$1" | grep -q "@<overlay-ip> -- "' _ "$out"
t "a LAN address is never taken for the overlay" hasnt "$out" "198.51.100.20"

# ---- (7) off: sshd was off before setup, so off turns it off and removes the drop-in
: > "$LOG"
out=$(R off --dry-run)
t "off --dry-run makes no mutating call" test "$(mut)" = 0
t "off --dry-run keeps the drop-in" test -f "$DROPIN"
out=$(R off); rc=$?
t "off exits 0" test "$rc" = 0
t "off turns Remote Login off (non-interactive -f)" grep -qx 'MUT systemsetup -f -setremotelogin off' "$LOG"
t "off removes the drop-in" test ! -e "$DROPIN"
t "off removes remote.json" test ! -e "$FLEET_STATE/remote.json"

# ---- (8) off when sshd was already on before setup leaves sshd on
fresh; touch "$T/sshd-on"
R setup >/dev/null
t "remote.json records sshd was on" grep -q '"sshd_was_on_before_setup": true' "$FLEET_STATE/remote.json"
t "setup with sshd already on never calls systemsetup" bash -c '! grep -q systemsetup "$1"' _ "$LOG"
: > "$LOG"; out=$(R off)
t "off leaves a pre-existing sshd on" has "$out" "sshd was on before setup; leaving it on"
t "off made no systemsetup call" bash -c '! grep -q systemsetup "$1"' _ "$LOG"
t "off still removes the drop-in" test ! -e "$DROPIN"
t "off warns sshd runs without the drop-in" has "$out" "without the key-only drop-in"

# ---- (9) rollback: sshd -t fails -> drop-in removed, sshd never switched on
fresh
out=$(SSHD_T_RC=1 R setup); rc=$?
t "failed sshd -t exits non-zero" test "$rc" != 0
t "failed sshd -t rolls the new drop-in back" test ! -e "$DROPIN"
t "failed sshd -t never switches Remote Login on" bash -c '! grep -q "setremotelogin on" "$1"' _ "$LOG"
t "failed sshd -t says it rolled back" has "$out" "drop-in rolled back"
fresh
printf 'PasswordAuthentication no\n# an older managed body\n' > "$DROPIN"
SSHD_T_RC=1 R setup >/dev/null
t "failed sshd -t restores the previous drop-in" grep -qx '# an older managed body' "$DROPIN"

fresh
out=$(SSHD_FORCE_PW=1 R setup); rc=$?
t "sshd -t passes but sshd -T still allows passwords (no Include): exits non-zero" test "$rc" != 0
t "passwords still on: drop-in rolled back" test ! -e "$DROPIN"
t "passwords still on: Remote Login never switched on" bash -c '! grep -q "setremotelogin on" "$1"' _ "$LOG"

# ---- (10) the sandbox fails closed
fresh; rm -f "$T/sb/bin/sshd"
out=$(R setup); rc=$?
t "a stub missing from the sandbox is an error" test "$rc" != 0
t "the error names the missing stub" has "$out" "sandbox has no bin/sshd"
t "nothing switched on after the error" bash -c '! grep -q setremotelogin "$1"' _ "$LOG"

# ---- (11) nothing escaped the sandbox
t "tripwire: no real privileged tool was called" test ! -s "$TRIP" || sed 's/^/    /' "$TRIP"
HOME=$REAL_HOME
t "this host's authorized_keys and sshd_config.d are unchanged" test "$(fp_real)" = "$REAL_BEFORE"

echo "---"
if [ $FAILS -gt 0 ]; then echo "FAIL test_remote.sh: $FAILS"; exit 1; fi
echo "PASS test_remote.sh: all assertions"
