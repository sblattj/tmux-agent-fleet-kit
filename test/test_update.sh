#!/usr/bin/env bash
# test_update.sh: `fleet update` / `tafk update` against a local bare origin and file:// release fixtures.
# Never touches the network, the real HOME or ~/.local/bin. One PASS/FAIL line per assertion; exit 1 on any FAIL.
# FLEET_TEST_ROOT=<an older kit tree> runs the same checks against that tree (the red half of red/green).
set -u
HERE=$(cd "$(dirname "$0")" && pwd -P)
ROOT=${FLEET_TEST_ROOT:-$(cd "$HERE/.." && pwd -P)}
tmp=$(mktemp -d "${TMPDIR:-/tmp}/fleet-test-update.XXXXXX"); tmp=$(cd "$tmp" && pwd -P)
trap 'rm -rf "$tmp"' EXIT
fails=0
pass() { printf 'PASS %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }
t() { local label=$1; shift; if "$@" >/dev/null 2>&1; then pass "$label"; else fail "$label"; fi; }
eq() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (want '$3' got '$2')"; fi; }
has() { case $1 in *"$2"*) return 0 ;; esac; return 1; }

export HOME=$tmp/home; mkdir -p "$HOME"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid
export GIT_CONFIG_NOSYSTEM=1 FLEET_UPDATE_TIMEOUT=10
unset FLEET_ROLE FLEET_UPDATE_API FLEET_UPDATE_TARBALLS FLEET_UPDATE_REPO
PFX=$tmp/prefix; export FLEET_PREFIX=$PFX
g() { git -C "$1" "${@:2}"; }

# ---- fixture: a source repo holding this kit's tree, v0.8.1 then v0.9.0, pushed to a bare origin
src=$tmp/src; mkdir -p "$src"
(cd "$ROOT" && tar --exclude .git --exclude __pycache__ -cf - .) | (cd "$src" && tar -xf -)
g "$src" init -q -b main; printf '0.8.1\n' > "$src/VERSION"
g "$src" add -A; g "$src" commit -q -m c1; g "$src" tag -a v0.8.1 -m v0.8.1
git init -q --bare -b main "$tmp/origin.git"; g "$src" remote add origin "$tmp/origin.git"
g "$src" push -q origin main v0.8.1
inst=$tmp/inst; git clone -q "$tmp/origin.git" "$inst"          # the install knows only v0.8.1
printf '0.9.0\n' > "$src/VERSION"; g "$src" commit -q -am c2; g "$src" tag -a v0.9.0 -m v0.9.0
g "$src" push -q origin main v0.9.0
mkdir -p "$PFX/bin"; ln -s "$inst/bin/fleet" "$PFX/bin/fleet"; ln -s "$inst/bin/fleet-goal" "$PFX/bin/fleet-goal"
ln -s /usr/bin/true "$PFX/bin/fleet-foreign"
F=$PFX/bin/fleet
state() { { g "$1" rev-parse HEAD; g "$1" symbolic-ref -q HEAD; g "$1" for-each-ref; g "$1" status --porcelain
  for l in "$PFX"/bin/*; do printf '%s -> %s\n' "$l" "$(readlink "$l")"; done; } 2>&1; }

t "fixture: install at v0.8.1 with no v0.9.0 tag yet" test "$(g "$inst" describe --tags)" = v0.8.1 -a -z "$(g "$inst" tag -l v0.9.0)"

# ---- tafk is fleet under a second name
t "bin/tafk is a symlink to fleet" test "$(readlink "$ROOT/bin/tafk")" = fleet
eq "tafk version == fleet version" "$("$inst/bin/tafk" version 2>&1)" "$("$inst/bin/fleet" version 2>&1)"
eq "fleet version reads the tag: v0.8.1" "$("$F" version 2>&1)" v0.8.1
eq "tafk help == fleet help" "$("$inst/bin/tafk" help 2>&1)" "$("$inst/bin/fleet" help 2>&1)"
t "fleet help names update and tafk" bash -c '"$1" help 2>&1 | grep -q "fleet update \[--check\] \[--dry-run\]" && "$1" help 2>&1 | grep -q tafk' _ "$F"

# ---- --check: report only
before=$(state "$inst")
out=$("$F" update --check 2>&1); rc=$?
eq "check (newer): exit 0" "$rc" 0
t "check (newer): says update available v0.8.1 -> v0.9.0" has "$out" "update available: v0.8.1 -> v0.9.0"
eq "check: install unchanged (HEAD, refs, tree, links)" "$(state "$inst")" "$before"
eq "tafk update --check prints the same as fleet update --check" "$("$inst/bin/tafk" update --check 2>&1)" "$out"

# ---- --dry-run: the plan, writes nothing
out=$("$F" update --dry-run 2>&1); rc=$?
eq "dry-run: exit 0" "$rc" 0
t "dry-run: would fetch v0.9.0" has "$out" "would fetch: tag v0.9.0"
t "dry-run: would fast-forward main" has "$out" "would fast-forward: main"
t "dry-run: would link tafk beside fleet" has "$out" "would link: $PFX/bin/tafk -> $inst/bin/tafk"
t "dry-run: ends 'dry run: v0.8.1 -> v0.9.0, nothing changed'" has "$out" "dry run: v0.8.1 -> v0.9.0, nothing changed"
eq "dry-run: install unchanged" "$(state "$inst")" "$before"

# ---- dirty tree: refused, nothing changed
printf 'local edit\n' >> "$inst/README.md"; before=$(state "$inst")
out=$("$F" update 2>&1); rc=$?
eq "dirty (tracked edit): exit 1" "$rc" 1
t "dirty: names the uncommitted changes and 'nothing changed'" bash -c 'case $1 in *uncommitted*"nothing changed"*) exit 0;; esac; exit 1' _ "$out"
eq "dirty: install unchanged" "$(state "$inst")" "$before"
g "$inst" checkout -q -- README.md
printf 'x\n' > "$inst/stray.txt"; before=$(state "$inst")
out=$("$F" update 2>&1); rc=$?
eq "dirty (untracked file): exit 1" "$rc" 1
eq "dirty (untracked): install unchanged" "$(state "$inst")" "$before"
rm -f "$inst/stray.txt"

# ---- offline: origin unreachable, refused, nothing changed
g "$inst" remote set-url origin "$tmp/no-such-origin.git"; before=$(state "$inst")
out=$("$F" update 2>&1); rc=$?
eq "offline: exit 1" "$rc" 1
t "offline: says it can't reach origin, nothing changed" bash -c 'case $1 in *"can'"'"'t reach origin"*"nothing changed"*) exit 0;; esac; exit 1' _ "$out"
eq "offline: install unchanged" "$(state "$inst")" "$before"
out=$("$F" update --check 2>&1); rc=$?
eq "offline --check: exit 1 (it can't know)" "$rc" 1
g "$inst" remote set-url origin "$tmp/origin.git"

# ---- diverged: a local commit on top of v0.8.1, refused, branch and tree unchanged
div=$tmp/div; git clone -q "$tmp/origin.git" "$div"; g "$div" reset -q --hard v0.8.1
printf 'mine\n' > "$div/mine.txt"; g "$div" add mine.txt; g "$div" commit -q -m mine
dsha=$(g "$div" rev-parse HEAD)
out=$("$div/bin/fleet" update 2>&1); rc=$?
eq "diverged: exit 1" "$rc" 1
t "diverged: says diverged, fast-forward only, nothing changed" bash -c 'case $1 in *diverged*"fast-forward only"*"nothing changed"*) exit 0;; esac; exit 1' _ "$out"
eq "diverged: HEAD unchanged" "$(g "$div" rev-parse HEAD)" "$dsha"
eq "diverged: tree clean, still on main" "$(g "$div" status --porcelain)|$(g "$div" symbolic-ref --short HEAD)" "|main"
out=$("$div/bin/fleet" update --dry-run 2>&1); rc=$?
eq "diverged --dry-run: refused too once the tag is local (exit 1)" "$rc" 1

# ---- newer: fast-forward to v0.9.0, tafk linked, foreign link left alone
out=$("$F" update 2>&1); rc=$?
eq "newer: exit 0" "$rc" 0
t "newer: prints updated: v0.8.1 -> v0.9.0" has "$out" "updated: v0.8.1 -> v0.9.0"
eq "newer: HEAD is the v0.9.0 commit" "$(g "$inst" rev-parse HEAD)" "$(g "$src" rev-parse 'v0.9.0^{commit}')"
eq "newer: still on branch main" "$(g "$inst" symbolic-ref --short HEAD)" main
eq "newer: VERSION 0.9.0" "$(cat "$inst/VERSION")" 0.9.0
eq "newer: tafk linked beside fleet" "$(readlink "$PFX/bin/tafk")" "$inst/bin/tafk"
eq "newer: fleet link unchanged" "$(readlink "$PFX/bin/fleet")" "$inst/bin/fleet"
eq "newer: a link that is not this kit's is left alone" "$(readlink "$PFX/bin/fleet-foreign")" /usr/bin/true
eq "newer: tafk version through the new link" "$("$PFX/bin/tafk" version 2>&1)" v0.9.0

# ---- current: already at, nothing changed
before=$(state "$inst")
out=$("$PFX/bin/tafk" update 2>&1); rc=$?
eq "current: exit 0" "$rc" 0
t "current: already at v0.9.0" has "$out" "already at v0.9.0"
eq "current: install unchanged" "$(state "$inst")" "$before"
printf 'more\n' > "$inst/later.txt"; g "$inst" add later.txt; g "$inst" commit -q -m later   # HEAD past the tag
out=$("$F" update 2>&1); rc=$?
t "HEAD past the latest tag: already at v0.9.0, exit 0" test "$rc" = 0 -a -n "$(has "$out" "already at v0.9.0" && echo y)"
g "$inst" reset -q --hard v0.9.0

# ---- tag ordering: prerelease tags skipped, numeric compare (v0.10.0 > v0.9.0), never a downgrade
g "$src" tag -a v1.0.0-rc.1 -m rc; g "$src" push -q origin v1.0.0-rc.1
out=$("$F" update --check 2>&1)
t "a prerelease tag (v1.0.0-rc.1) is not a release: still already at v0.9.0" has "$out" "already at v0.9.0"
printf '0.10.0\n' > "$src/VERSION"; g "$src" commit -q -am c3; g "$src" tag -a v0.10.0 -m v0.10.0; g "$src" push -q origin main v0.10.0
out=$("$F" update --check 2>&1)
t "numeric compare: v0.9.0 -> v0.10.0 available" has "$out" "update available: v0.9.0 -> v0.10.0"
dn=$tmp/down; git clone -q "$tmp/origin.git" "$dn"; printf 'a\n' > "$dn/a.txt"; g "$dn" add a.txt; g "$dn" commit -q -m a; g "$dn" tag -a v0.11.0 -m local
out=$("$dn/bin/fleet" update 2>&1); rc=$?
t "never a downgrade: a local v0.11.0 against origin v0.10.0 is already at v0.11.0" test "$rc" = 0 -a -n "$(has "$out" "already at v0.11.0" && echo y)"

# ---- detached HEAD install (git clone --branch vX): moves to the tag, stays detached
det=$tmp/det; git clone -q -c advice.detachedHead=false --branch v0.8.1 "$tmp/origin.git" "$det" 2>/dev/null
out=$("$det/bin/fleet" update 2>&1); rc=$?
t "detached: updated v0.8.1 -> v0.10.0" test "$rc" = 0 -a -n "$(has "$out" "updated: v0.8.1 -> v0.10.0" && echo y)"
eq "detached: HEAD is v0.10.0, still detached" "$(g "$det" rev-parse HEAD)|$(g "$det" symbolic-ref -q HEAD || echo detached)" "$(g "$src" rev-parse 'v0.10.0^{commit}')|detached"

# ---- copied tree (no .git): releases/latest API + tarball, swap with the old tree kept
tp=$tmp/tprefix; mkdir -p "$tp/bin" "$tmp/api/releases" "$tmp/tarballs" "$tmp/copies"
kit=$tmp/copies/kit; (cd "$src" && g "$src" archive --prefix=kit/ v0.8.1) | tar -xf - -C "$tmp/copies"
ln -s "$kit/bin/fleet" "$tp/bin/fleet"
printf '{"tag_name": "v0.9.0", "name": "v0.9.0"}\n' > "$tmp/api/releases/latest"
g "$src" archive --format=tar.gz --prefix=tmux-agent-fleet-kit-0.9.0/ -o "$tmp/tarballs/v0.9.0.tar.gz" v0.9.0
TE() { env FLEET_PREFIX="$tp" FLEET_UPDATE_API="file://$tmp/api" FLEET_UPDATE_TARBALLS="file://$tmp/tarballs" "$@"; }
tstate() { { find "$tmp/copies" -maxdepth 1 | sort; cat "$kit/VERSION"; for l in "$tp"/bin/*; do printf '%s -> %s\n' "$l" "$(readlink "$l")"; done; } 2>&1; }
before=$(tstate)
out=$(TE "$tp/bin/fleet" update --check 2>&1); rc=$?
t "copied tree --check: update available v0.8.1 -> v0.9.0 (exit 0)" test "$rc" = 0 -a -n "$(has "$out" "update available: v0.8.1 -> v0.9.0" && echo y)"
out=$(TE "$tp/bin/fleet" update --dry-run 2>&1); rc=$?
t "copied tree --dry-run: would download and swap, exit 0" test "$rc" = 0 -a -n "$(has "$out" "would download: file://$tmp/tarballs/v0.9.0.tar.gz" && has "$out" "would swap" && echo y)"
eq "copied tree check/dry-run: nothing changed" "$(tstate)" "$before"
out=$(env FLEET_PREFIX="$tp" FLEET_UPDATE_API="file://$tmp/no-api" "$tp/bin/fleet" update 2>&1); rc=$?
t "copied tree offline: exit 1, can't read the latest release" test "$rc" = 1 -a -n "$(has "$out" "can't read the latest release" && echo y)"
eq "copied tree offline: nothing changed" "$(tstate)" "$before"
cp "$tmp/tarballs/v0.9.0.tar.gz" "$tmp/good.tgz"
g "$src" archive --format=tar.gz --prefix=x/ -o "$tmp/tarballs/v0.9.0.tar.gz" v0.8.1    # wrong VERSION inside
out=$(TE "$tp/bin/fleet" update 2>&1); rc=$?
t "copied tree, tarball VERSION mismatch: exit 1, refused" test "$rc" = 1 -a -n "$(has "$out" "VERSION says v0.8.1" && echo y)"
eq "copied tree, bad tarball: nothing changed, no staging dir left" "$(tstate)" "$before"
mv "$tmp/good.tgz" "$tmp/tarballs/v0.9.0.tar.gz"
out=$(TE "$tp/bin/fleet" update 2>&1); rc=$?
eq "copied tree newer: exit 0" "$rc" 0
t "copied tree newer: updated v0.8.1 -> v0.9.0, old tree kept" bash -c 'case $1 in *"updated: v0.8.1 -> v0.9.0"*"kept at"*) exit 0;; esac; exit 1' _ "$out"
eq "copied tree newer: VERSION 0.9.0" "$(cat "$kit/VERSION")" 0.9.0
eq "copied tree newer: old tree at kit.prev-v0.8.1" "$(cat "$kit.prev-v0.8.1/VERSION")" 0.8.1
eq "copied tree newer: tafk linked" "$(readlink "$tp/bin/tafk")" "$kit/bin/tafk"
eq "copied tree newer: no staging dir left" "$(find "$tmp/copies" -maxdepth 1 -name '.fleet-update.*' | wc -l | tr -d ' ')" 0
out=$(TE "$tp/bin/tafk" update 2>&1); rc=$?
t "copied tree current: already at v0.9.0, exit 0" test "$rc" = 0 -a -n "$(has "$out" "already at v0.9.0" && echo y)"

# ---- VERSION matches the top CHANGELOG heading
top=$(sed -n 's/^## v\([0-9][0-9.]*\) .*/\1/p' "$ROOT/CHANGELOG.md" | head -1)
eq "VERSION ($(cat "$ROOT/VERSION" 2>/dev/null)) is the top CHANGELOG version or the one before an unreleased section" \
  "$(cat "$ROOT/VERSION" 2>/dev/null)" "${top:-none}"

[ "$fails" = 0 ] && echo "ALL PASS" || echo "$fails FAIL"
[ "$fails" = 0 ]
