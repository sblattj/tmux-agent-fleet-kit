#!/usr/bin/env bash
# test_privacy.sh: the repo must carry no personal data. Any hit = FAIL, exit 1.
# FLEET_PRIVACY_TERMS: pipe-separated extra terms (person names, account handles, hostnames, private
# tool names, machine-specific ports), default empty. Keep that list outside the repo.
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
self=test/test_privacy.sh
fails=0
emdash=$(printf '\342\200\224')

scan() { # label, fixed-string pattern (case-insensitive when $3 = i)
  local label=$1 pat=$2 ci=${3:-} hits flags=-rInF
  [ "$ci" = i ] && flags=-rInFi
  hits=$(cd "$ROOT" && grep $flags --exclude-dir=.git --exclude=.git --exclude="$(basename "$self")" -e "$pat" . 2>/dev/null | grep -v "^\./$self:" || true)
  if [ -n "$hits" ]; then
    printf 'FAIL privacy: %s\n' "$label"
    printf '%s\n' "$hits" | sed 's/^/    /'
    fails=$((fails + 1))
  else
    printf 'PASS privacy: no %s\n' "$label"
  fi
}

scan "/Users/ paths" "/Users/"
scan "@gmail" "@gmail" i
scan "em dash (U+2014)" "$emdash"

terms=${FLEET_PRIVACY_TERMS:-}
if [ -n "$terms" ]; then
  old=$IFS; IFS='|'
  for t in $terms; do
    [ -n "$t" ] && scan "term '$t'" "$t" i
  done
  IFS=$old
else
  echo "PASS privacy: FLEET_PRIVACY_TERMS empty, name-list scan skipped"
fi

# a literal credential in watch.json is refused by `fleet watch check` (control: a reference passes)
wtmp=$(mktemp -d)
printf '{"sources":[{"name":"x","kind":"command","argv":["true"],"auth":{"type":"bearer","token":"literal-value-1"}}]}' > "$wtmp/watch.json"
if FLEET_STATE=$wtmp "$ROOT/bin/fleet" watch check >/dev/null 2>&1; then
  printf 'FAIL privacy: fleet watch check accepted a literal token\n'; fails=$((fails + 1))
else
  printf 'PASS privacy: fleet watch check refuses a literal token\n'
fi
printf '{"sources":[{"name":"x","kind":"command","argv":["true"],"auth":{"type":"bearer","token":"env:X_TOKEN"}}]}' > "$wtmp/watch.json"
if FLEET_STATE=$wtmp "$ROOT/bin/fleet" watch check >/dev/null 2>&1; then
  printf 'PASS privacy: fleet watch check takes a reference (control)\n'
else
  printf 'FAIL privacy: fleet watch check refused a reference\n'; fails=$((fails + 1))
fi
rm -rf "$wtmp"

echo "---"
if [ $fails -gt 0 ]; then echo "FAIL test_privacy.sh: $fails category(ies) with hits"; exit 1; fi
echo "PASS test_privacy.sh: no hits"
