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
  hits=$(cd "$ROOT" && grep $flags --exclude-dir=.git --exclude="$(basename "$self")" -e "$pat" . 2>/dev/null | grep -v "^\./$self:" || true)
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

echo "---"
if [ $fails -gt 0 ]; then echo "FAIL test_privacy.sh: $fails category(ies) with hits"; exit 1; fi
echo "PASS test_privacy.sh: no hits"
