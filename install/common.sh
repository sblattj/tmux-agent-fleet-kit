#!/usr/bin/env bash
# common.sh: helpers shared by install/claude.sh, codex.sh and opencode.sh. Source it, do not run it.
# Portable to bash 3.2 and 5. Every write goes through fc_commit so --dry-run can print instead of write.

FC_REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
FC_DRY=0
FC_PREFIX=${FLEET_PREFIX:-$HOME/.local}
FC_TRUST=""
FC_HUMAN=${FLEET_HUMAN:-the human}
FC_DESK=${FLEET_DECIDE:-DECIDE}
FC_ORCH=${FLEET_ROUTE:-ROUTE}
FC_TMP=""
# Role files (roles/<role>.codex.toml, roles/<role>.opencode.json); override for tests.
FC_ROLES_DIR=${FLEET_ROLES_DIR:-$FC_REPO/roles}

fc_say() {
  if [ "$FC_DRY" = 1 ]; then printf '[dry-run] %s\n' "$*"; else printf '%s\n' "$*"; fi
}
fc_warn() { printf 'WARN: %s\n' "$*" >&2; }
fc_die() { printf 'FAIL %s\n' "$*" >&2; exit 1; }

fc_need() {
  command -v "$1" >/dev/null 2>&1 || fc_die "$1 is required but not on PATH"
}

fc_mktemp() { FC_TMP=$(mktemp "${TMPDIR:-/tmp}/fleet-install.XXXXXX"); }

# Write $1 (temp file) to $2 (target) when different; backs up an existing target first.
# Uses cat > target so a symlinked target (dotfiles) stays a symlink and keeps its mode.
fc_commit() {
  local tmp=$1 target=$2 label=$3
  if [ -f "$target" ] && cmp -s "$tmp" "$target"; then
    fc_say "unchanged: $label ($target)"; rm -f "$tmp"; return 0
  fi
  if [ "$FC_DRY" = 1 ]; then
    fc_say "would write: $label ($target)"; rm -f "$tmp"; return 0
  fi
  mkdir -p "$(dirname "$target")"
  # one backup per second: several edits to one file in a run keep the pre-run copy, not an intermediate one
  local bak
  bak=$target.fleet-bak.$(date +%Y%m%d%H%M%S)
  if [ -f "$target" ] && [ ! -e "$bak" ]; then cp -p "$target" "$bak"; fi
  cat "$tmp" > "$target"
  rm -f "$tmp"
  fc_say "wrote: $label ($target)"
}

# Print a sed-safe version of $1 for use in s|a|b|.
fc_esc() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }

# Render rules/fleet-rules.md with the placeholder values into a temp file ($FC_TMP).
fc_render_rules() {
  [ -f "$FC_REPO/rules/fleet-rules.md" ] || fc_die "missing $FC_REPO/rules/fleet-rules.md"
  fc_mktemp
  sed -e "s|{{HUMAN}}|$(fc_esc "$FC_HUMAN")|g" \
      -e "s|{{DECIDE}}|$(fc_esc "$FC_DESK")|g" \
      -e "s|{{ROUTE}}|$(fc_esc "$FC_ORCH")|g" \
      "$FC_REPO/rules/fleet-rules.md" > "$FC_TMP"
}

# Replace the block between the fleet-rules markers in $1, or append it. Idempotent.
fc_upsert_rules() {
  local target=$1 blk out b e
  fc_render_rules; blk=$FC_TMP
  fc_mktemp; out=$FC_TMP
  if [ -f "$target" ]; then
    b=$(grep -c 'fleet-rules:begin' "$target" || true)
    e=$(grep -c 'fleet-rules:end' "$target" || true)
    if [ "$b" = 1 ] && [ "$e" = 1 ]; then
      awk -v blockfile="$blk" '
        /<!-- fleet-rules:begin -->/ { while ((getline line < blockfile) > 0) print line; close(blockfile); inb = 1; next }
        /<!-- fleet-rules:end -->/ { if (inb) { inb = 0; next } }
        inb { next }
        { print }
      ' "$target" > "$out"
    elif [ "$b" = 0 ] && [ "$e" = 0 ]; then
      { cat "$target"; if [ -s "$target" ]; then printf '\n'; fi; cat "$blk"; } > "$out"
    else
      rm -f "$blk" "$out"
      fc_die "$target has $b begin and $e end fleet-rules markers; fix by hand (expected 0/0 or 1/1)"
    fi
  else
    cp "$blk" "$out"
  fi
  rm -f "$blk"
  fc_commit "$out" "$target" "fleet rules block"
}

# fc_jq_edit <target.json> <label> <jq args...> <filter>   (target created as {} when missing)
fc_jq_edit() {
  local target=$1 label=$2; shift 2
  local in out
  fc_mktemp; out=$FC_TMP
  if [ -f "$target" ]; then
    in=$target
    jq -e . "$in" >/dev/null 2>&1 || { rm -f "$out"; fc_die "$target is not plain JSON (comments?); edit by hand: $label"; }
  else
    fc_mktemp; in=$FC_TMP; printf '{}\n' > "$in"
  fi
  if ! jq "$@" "$in" > "$out"; then
    [ "$in" = "$target" ] || rm -f "$in"
    rm -f "$out"; fc_die "jq edit failed for $target ($label)"
  fi
  [ "$in" = "$target" ] || rm -f "$in"
  fc_commit "$out" "$target" "$label"
}

# Copy every skills/<name>/ into $1/<name>/.
fc_copy_skills() {
  local dest=$1 d n
  for d in "$FC_REPO"/skills/*/; do
    [ -d "$d" ] || continue
    n=$(basename "$d")
    if [ -d "$dest/$n" ] && diff -rq "$d" "$dest/$n" >/dev/null 2>&1; then
      fc_say "unchanged: skill $n ($dest/$n)"
    elif [ "$FC_DRY" = 1 ]; then
      fc_say "would copy: skill $n -> $dest/$n"
    else
      mkdir -p "$dest/$n"
      cp -R "$d". "$dest/$n/"
      chmod +x "$dest/$n"/*.sh 2>/dev/null || true
      fc_say "copied: skill $n -> $dest/$n"
    fi
  done
}

# Symlink bin/fleet and bin/fleet-goal into $FC_PREFIX/bin.
fc_link_bins() {
  local b link target
  for b in fleet fleet-goal; do
    link=$FC_PREFIX/bin/$b; target=$FC_REPO/bin/$b
    if [ -L "$link" ] && [ "$(readlink "$link")" = "$target" ]; then
      fc_say "unchanged: symlink $link"
    elif [ -e "$link" ] && [ ! -L "$link" ]; then
      fc_warn "$link exists and is not a symlink; leaving it alone"
    elif [ "$FC_DRY" = 1 ]; then
      fc_say "would link: $link -> $target"
    else
      mkdir -p "$FC_PREFIX/bin"
      ln -sfn "$target" "$link"
      fc_say "linked: $link -> $target"
    fi
  done
  case ":$PATH:" in
    *":$FC_PREFIX/bin:"*) ;;
    *) fc_say "note: $FC_PREFIX/bin is not on PATH; add it to your shell profile" ;;
  esac
}

# Handle the flags every installer shares. Returns 0 when it consumed the flag ($FC_SHIFT = args used).
FC_SHIFT=0
fc_common_flag() {
  case $1 in
    --dry-run) FC_DRY=1; FC_SHIFT=1 ;;
    --prefix) [ $# -ge 2 ] || fc_die "--prefix needs a directory"; FC_PREFIX=$2; FC_SHIFT=2 ;;
    --trust) [ $# -ge 2 ] || fc_die "--trust needs a directory"; FC_TRUST=$2; FC_SHIFT=2 ;;
    *) return 1 ;;
  esac
  return 0
}

# Resolve the --trust dir to a real path; dies if it is missing.
fc_trust_dir() {
  [ -d "$FC_TRUST" ] || fc_die "--trust: $FC_TRUST is not a directory"
  (cd "$FC_TRUST" && pwd -P)
}

# Roles renamed in v0.2.0 (desk -> decide, orchestrator -> route, listener -> watch). An upgrade retires
# their installed files and agent entries, whose prompts point at roles/*.md files that no longer exist.
FC_RETIRED_ROLES="desk orchestrator listener"

# fc_retire_role_files <dir> <suffix> <label>: move $dir/fleet-<old><suffix> aside to a .fleet-bak copy.
fc_retire_role_files() {
  local dir=$1 suffix=$2 label=$3 r f
  for r in $FC_RETIRED_ROLES; do
    f=$dir/fleet-$r$suffix
    [ -e "$f" ] || continue
    if [ "$FC_DRY" = 1 ]; then fc_say "would retire: stale $label fleet-$r ($f)"; continue; fi
    mv "$f" "$f.fleet-bak.$(date +%Y%m%d%H%M%S)"
    fc_say "retired: stale $label fleet-$r ($f, renamed in v0.2.0)"
  done
}

# Write $CODEX_HOME/fleet-<role>.config.toml from each roles/<role>.codex.toml. Idempotent.
fc_install_codex_roles() {
  local home=${CODEX_HOME:-$HOME/.codex} f role found=0
  fc_retire_role_files "$home" .config.toml "codex role profile"
  for f in "$FC_ROLES_DIR"/*.codex.toml; do
    [ -f "$f" ] || continue
    found=1
    role=$(basename "$f" .codex.toml)
    fc_mktemp; cp "$f" "$FC_TMP"
    fc_commit "$FC_TMP" "$home/fleet-$role.config.toml" "codex role profile fleet-$role"
  done
  if [ "$found" = 0 ]; then fc_say "no role profiles found in $FC_ROLES_DIR (roles/*.codex.toml); skipping"; fi
}

# Merge each roles/<role>.opencode.json into .agent["fleet-<role>"] of $1 (opencode.json), rewriting
# "{file:./roles/<role>.md}" to the absolute roles dir. Idempotent. Drops the agents of retired roles
# (FC_RETIRED_ROLES), and fails when any {file:...} it writes, or any left on a fleet-* agent, is missing:
# OpenCode rejects the whole config over one bad {file:} path, so every lane on it would exit 1.
fc_install_opencode_roles() {
  local target=$1 f role found=0 ref missing old
  old=$(for r in $FC_RETIRED_ROLES; do printf '"fleet-%s",' "$r"; done); old="[${old%,}]"
  if [ -f "$target" ] && jq -e --argjson o "$old" '(.agent // {}) | keys | any(. as $k | $o | index($k))' "$target" >/dev/null 2>&1; then
    fc_jq_edit "$target" "retired opencode agents $(jq -r --argjson o "$old" '[(.agent // {}) | keys[] | select(. as $k | $o | index($k))] | join(", ")' "$target")" \
      --argjson o "$old" '.agent |= with_entries(select(.key as $k | $o | index($k) | not))'
  fi
  for f in "$FC_ROLES_DIR"/*.opencode.json; do
    [ -f "$f" ] || continue
    found=1
    role=$(basename "$f" .opencode.json)
    jq -e . "$f" >/dev/null 2>&1 || fc_die "$f is not valid JSON"
    for ref in $(jq -r '.. | strings | scan("\\{file:\\./roles/([^}]+)\\}") | .[0]' "$f"); do
      [ -f "$FC_ROLES_DIR/$ref" ] || fc_die "$f points at {file:./roles/$ref}, but $FC_ROLES_DIR/$ref does not exist"
    done
    fc_jq_edit "$target" "opencode agent fleet-$role" --arg r "fleet-$role" --arg d "$FC_ROLES_DIR" \
      --slurpfile a "$f" \
      '.agent[$r] = ($a[0] | walk(if type == "string" then (split("{file:./roles/") | join("{file:" + $d + "/")) else . end))'
  done
  if [ "$found" = 0 ]; then fc_say "no role agents found in $FC_ROLES_DIR (roles/*.opencode.json); skipping"; fi
  # The probe verbs are for fleet-probe only. OpenCode 1.19 turns the global tools["fleet-probe_*"]=false into a
  # deny rule that the global permission (e.g. "allow" from --bypass) overrides, last match wins, so every agent
  # was offered them. An agent's own permission comes after the global ones, so each agent but fleet-probe gets
  # the deny there: every agent in the config plus the built-in build, plan and general.
  fc_jq_edit "$target" "fleet-probe_* denied on every agent but fleet-probe" \
    '.agent = ((.agent // {}) as $a | reduce ((($a | keys) + ["build", "plan", "general"]) | unique[]
        | select(. != "fleet-probe")) as $k ($a; .[$k] = ((.[$k] // {})
        | .permission = (((.permission // {}) | if type == "string" then {"*": .} else . end) + {"fleet-probe_*": "deny"}))))'
  if [ "$FC_DRY" = 1 ] || [ ! -f "$target" ]; then return 0; fi
  missing=$(jq -r '(.agent // {}) | to_entries[] | select(.key | startswith("fleet-")) | .key as $k
      | .value | .. | strings | scan("\\{file:([^}]+)\\}") | "\($k) \(.[0])"' "$target" |
    while read -r k path; do
      [ "${path#\~/}" = "$path" ] || path=$HOME/${path#\~/}
      [ -f "$path" ] || printf '  agent %s: {file:%s}\n' "$k" "$path"
    done)
  [ -z "$missing" ] || fc_die "$target has fleet agents whose {file:...} prompt does not exist; OpenCode would reject the config:
$missing"
}

# Write $1/fleet-<role>.json (agents dir) from each roles/<role>.kiro.json: .name = "fleet-<role>" and
# .prompt = the INLINE text of roles/<role>.md (Kiro rejects absolute file:// prompts and silently falls
# back to the default agent, dropping the deny rules). Idempotent; reuses fc_jq_edit for backups.
# fleet-probe sets includeMcpJson false, so it gets its servers here: the mesh (inbox) and the read-only
# fleet-probe verbs (docs/design/four-desks.md 2.5).
fc_install_kiro_roles() {
  local dir=$1 f role found=0
  fc_retire_role_files "$dir" .json "kiro agent"
  for f in "$FC_ROLES_DIR"/*.kiro.json; do
    [ -f "$f" ] || continue
    found=1
    role=$(basename "$f" .kiro.json)
    jq -e . "$f" >/dev/null 2>&1 || fc_die "$f is not valid JSON"
    [ -f "$FC_ROLES_DIR/$role.md" ] || fc_die "missing $FC_ROLES_DIR/$role.md (prompt text for kiro agent fleet-$role)"
    fc_jq_edit "$dir/fleet-$role.json" "kiro agent fleet-$role" --arg n "fleet-$role" \
      --rawfile p "$FC_ROLES_DIR/$role.md" --slurpfile a "$f" \
      --arg m "$FC_REPO/lib/fleet_mesh.py" --arg q "$FC_REPO/lib/fleet_probe.py" \
      '$a[0] | .name = $n | .prompt = $p
       | if $n == "fleet-probe" then .mcpServers = {fleet:{command:"python3",args:[$m,"serve"]},"fleet-probe":{command:"python3",args:[$q,"serve"]}} else . end'
  done
  if [ "$found" = 0 ]; then fc_say "no role agents found in $FC_ROLES_DIR (roles/*.kiro.json); skipping"; fi
}

# Write $1/fleet-<role>.md (agy agents dir) from each roles/<role>.agy.md (YAML frontmatter: name, tools,
# inheritMcp) followed by the text of roles/<role>.md as the agent's system prompt. Idempotent.
# The deny rules are not here: fleet launch passes roles/<role>.agy.json as the lane's --settings.
fc_install_agy_roles() {
  local dir=$1 f role found=0
  fc_retire_role_files "$dir" .md "agy agent"
  for f in "$FC_ROLES_DIR"/*.agy.md; do
    [ -f "$f" ] || continue
    found=1
    role=$(basename "$f" .agy.md)
    [ -f "$FC_ROLES_DIR/$role.md" ] || fc_die "missing $FC_ROLES_DIR/$role.md (prompt text for agy agent fleet-$role)"
    head -1 "$f" | grep -qx -- '---' || fc_die "$f does not start with a --- frontmatter line"
    fc_mktemp
    { cat "$f"; printf '\n'; cat "$FC_ROLES_DIR/$role.md"; } > "$FC_TMP"
    fc_commit "$FC_TMP" "$dir/fleet-$role.md" "agy agent fleet-$role"
  done
  if [ "$found" = 0 ]; then fc_say "no role agents found in $FC_ROLES_DIR (roles/*.agy.md); skipping"; fi
}
