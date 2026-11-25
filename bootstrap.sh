#!/usr/bin/env bash
# bootstrap.sh: set up tmux-agent-fleet-kit on a new computer, in two halves (issue #40). Also `fleet bootstrap`.
#
#   curl -fsSL https://raw.githubusercontent.com/sblattj/tmux-agent-fleet-kit/main/bootstrap.sh | bash
#   curl -fsSL .../bootstrap.sh | bash -s -- --dry-run
#   fleet bootstrap [--dry-run] [--verify] [--no-desks] [--cli C] [--pubkey KEY|FILE]
#   fleet bootstrap --verify
#
# No-sudo half (runs now, idempotent, prints every step):
#   1. packages    brew install tmux mosh jq, only the missing ones (no Docker; for containers use OrbStack)
#   2. agent CLIs  each installed CLI (claude, codex, opencode, kiro-cli, agy) gets its install/<cli>.sh --no-desks
#   3. ssh PATH    the brew bin dir on the non-interactive ssh PATH: one marker-guarded block in ~/.zshenv. A symlinked
#                  or dotfiles-managed ~/.zshenv is never edited; the line is printed for you to add
#   4. pubkey      --pubkey appends a phone key to ~/.ssh/authorized_keys (fleet remote add-key)
#   5. desks       fleet desks up --cli <first CLI found> (the step every installer ends with); --no-desks skips it
# Sudo half (never run here): the root steps (key-only sshd drop-in checked with sshd -t, Remote Login on, the
#   mosh-server firewall allow) are written to $FLEET_STATE/bootstrap-root.sh by `fleet remote root-script`, and the
#   ! line to run it is printed. Afterwards `fleet bootstrap --verify` checks the effect without privileges.
#
# --dry-run   print every step, write nothing (no clone, no brew, no files, no root script; its body is printed)
# --verify    unprivileged checks only (fleet remote verify): sshd listening, the drop-in, ssh localhost tmux -V, mosh-server
# --no-desks  skip step 5 (also FLEET_NO_DESKS=1)
# --cli C     the CLI the desks run on (default: the first one found, in the order above)
# --pubkey K  an ssh public key (or a file holding one) to authorize for the phone
# Piped from curl there is no checkout: the kit is cloned to $FLEET_KIT_DIR (default ~/.local/share/tmux-agent-fleet-kit)
# from $FLEET_KIT_REPO, and that clone's bootstrap.sh takes over. An existing clone is reused, never pulled.
#
# Safe for curl | bash: everything sits in one function called on the last line, so a cut-off download runs nothing.

fleet_bootstrap() {
  set -u
  local dry=0 verify=0 nodesks=${FLEET_NO_DESKS:-0} cli= pubkey= os=${FLEET_BOOTSTRAP_OS:-$(uname -s)}
  local args=("$@")
  while [ $# -gt 0 ]; do
    case $1 in
      --dry-run|-n) dry=1; shift ;;
      --verify) verify=1; shift ;;
      --no-desks) nodesks=1; shift ;;
      --cli) [ $# -ge 2 ] || { echo "ERROR: --cli needs a name" >&2; return 2; }; cli=$2; shift 2 ;;
      --pubkey) [ $# -ge 2 ] || { echo "ERROR: --pubkey needs KEY or FILE" >&2; return 2; }; pubkey=$2; shift 2 ;;
      -h|--help) fb_usage; return 0 ;;
      *) echo "ERROR: unknown arg $1 (see --help)" >&2; return 2 ;;
    esac
  done

  # ---- the kit: this checkout, or a clone when piped from curl
  local src=${BASH_SOURCE[0]:-} kit=
  if [ -n "$src" ] && [ -f "$src" ] && [ -f "$(dirname "$src")/bin/fleet-remote" ]; then
    kit=$(cd "$(dirname "$src")" && pwd -P)
  else
    kit=${FLEET_KIT_DIR:-$HOME/.local/share/tmux-agent-fleet-kit}
    local repo=${FLEET_KIT_REPO:-https://github.com/sblattj/tmux-agent-fleet-kit.git}
    fb_step "kit"
    if [ -f "$kit/bootstrap.sh" ] && [ -f "$kit/bin/fleet-remote" ]; then
      fb_ok "using the existing clone $kit (update it with: git -C $kit pull --ff-only)"
    elif [ "$dry" = 1 ]; then
      fb_plan "would clone $repo -> $kit, then run $kit/bootstrap.sh${args[*]:+ ${args[*]}}"
      fb_step "dry run: nothing changed"; return 0
    else
      command -v git >/dev/null 2>&1 || { fb_fail "git is required to fetch the kit (macOS: xcode-select --install)"; return 1; }
      mkdir -p "$(dirname "$kit")" && git clone --depth 1 "$repo" "$kit" || { fb_fail "git clone $repo failed"; return 1; }
      fb_ok "cloned $repo -> $kit"
    fi
    echo "==> handing over to $kit/bootstrap.sh"
    bash "$kit/bootstrap.sh" ${args[@]+"${args[@]}"}
    return $?
  fi

  local fleet=${FLEET_BIN:-$kit/bin/fleet}
  if [ "$verify" = 1 ]; then "$fleet" remote verify; return $?; fi

  local state=${FLEET_STATE:-$HOME/.local/state/fleet} bad=0
  local root_script=$state/bootstrap-root.sh
  [ "$dry" = 0 ] || echo "==> dry run: every step is printed, nothing is written"

  # ---- 1. packages
  fb_step "1/5 packages: tmux mosh jq"
  local missing=() t b
  for t in tmux mosh jq; do
    b=$t; [ "$t" != mosh ] || b=mosh-server
    if command -v "$b" >/dev/null 2>&1; then fb_ok "$t: $(command -v "$b")"; else missing+=("$t"); fi
  done
  if [ ${#missing[@]} = 0 ]; then
    fb_ok "nothing to install"
  elif command -v brew >/dev/null 2>&1; then
    if [ "$dry" = 1 ]; then fb_plan "would run: brew install ${missing[*]}"
    else
      echo "    running: brew install ${missing[*]}"
      brew install "${missing[@]}" || { fb_fail "brew install ${missing[*]} failed"; return 1; }
    fi
  elif [ "$os" = Darwin ]; then
    fb_fail "Homebrew is missing; install it first (it asks for your password itself), then rerun:"
    echo '        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    return 1
  else
    fb_fail "missing ${missing[*]}; install them with your package manager (e.g. apt install ${missing[*]}), then rerun"
    return 1
  fi
  echo "    (no Docker needed; if you ever want containers next to the fleet, use OrbStack)"

  # ---- 2. agent CLIs
  fb_step "2/5 agent CLIs"
  local found=() spec name bin inst
  for spec in "claude:${FLEET_CLAUDE_BIN:-claude}:claude.sh" "codex:${FLEET_CODEX_BIN:-codex}:codex.sh" \
              "opencode:${FLEET_OPENCODE_BIN:-opencode}:opencode.sh" "kiro:${FLEET_KIRO_BIN:-kiro-cli}:kiro.sh" \
              "agy:${FLEET_AGY_BIN:-agy}:agy.sh" "gemini:gemini:"; do
    name=${spec%%:*}; bin=${spec#*:}; inst=${bin#*:}; bin=${bin%%:*}
    command -v "$bin" >/dev/null 2>&1 || continue
    if [ -z "$inst" ] || [ ! -f "$kit/install/$inst" ]; then
      fb_ok "$name: found ($(command -v "$bin")); this kit has no install/$name.sh, skipped"; continue
    fi
    found+=("$name")
    local ia=(--no-desks); [ "$dry" = 0 ] || ia+=(--dry-run)
    echo "    running: $kit/install/$inst ${ia[*]}"
    if bash "$kit/install/$inst" "${ia[@]}" 2>&1 | sed 's/^/        /'; [ "${PIPESTATUS[0]}" = 0 ]; then fb_ok "$name installed"
    else fb_warn "install/$inst failed (above); rerun it by hand"; bad=1; fi
  done
  [ ${#found[@]} -gt 0 ] || fb_warn "no supported agent CLI on PATH (claude, codex, opencode, kiro-cli, agy); install one and rerun"

  # ---- 3. the non-interactive ssh PATH (a phone's `ssh host cmd` and mosh's server start)
  fb_step "3/5 ssh PATH: tmux and mosh-server for ssh host cmd"
  fb_ssh_path "$fleet" "$dry" || bad=1

  # ---- 4. phone key
  fb_step "4/5 phone key"
  if [ -n "$pubkey" ]; then
    local ka=(remote add-key "$pubkey"); [ "$dry" = 0 ] || ka+=(--dry-run)
    "$fleet" "${ka[@]}" || bad=1
  else
    fb_ok "no --pubkey; ~/.ssh/authorized_keys untouched (generate a key in Blink, then: fleet bootstrap --pubkey '<key>')"
  fi

  # ---- 5. desks (install/common.sh fc_desks_up runs the same command at the end of every installer)
  fb_step "5/5 desks"
  [ -n "$cli" ] || cli=${found[0]:-}
  if [ "$nodesks" = 1 ]; then fb_ok "skipped (--no-desks)"
  elif [ -z "$cli" ]; then fb_warn "no agent CLI found, so no desks; rerun once one is installed"
  elif [ "$dry" = 1 ]; then fb_plan "would run: $fleet desks up --cli $cli"
  else
    echo "    running: $fleet desks up --cli $cli"
    "$fleet" desks up --cli "$cli" || { fb_warn "fleet desks up failed; rerun: fleet desks up --cli $cli"; bad=1; }
  fi

  # ---- sudo half: write the root steps to a script for the human; never run them
  fb_step "root steps (sudo half): written to a script for you, never run here"
  if [ "$dry" = 1 ]; then
    fb_plan "would write $root_script:"
    "$fleet" remote root-script - | sed 's/^/        /'
  else
    mkdir -p "$state"
    "$fleet" remote root-script "$root_script" || { fb_fail "could not write $root_script"; return 1; }
    fb_ok "wrote $root_script"
    echo "    run it yourself (it asks for your password), then check it without sudo:"
    echo "        ! sudo bash $root_script"
    echo "        fleet bootstrap --verify"
  fi

  fb_step "attach from the phone (after --verify passes)"
  "$fleet" remote attach-line | sed 's/^/    /'
  if [ "$dry" = 1 ]; then fb_step "dry run: nothing changed"; else fb_step "done"; fi
  return $bad
}

fb_step() { printf '==> %s\n' "$*"; }
fb_ok() { printf '    ok: %s\n' "$*"; }
fb_warn() { printf '    warn: %s\n' "$*"; }
fb_plan() { printf '    [dry-run] %s\n' "$*"; }
fb_fail() { printf 'FAIL %s\n' "$*" >&2; }
fb_usage() { sed -n '2,29p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null | sed 's/^# \{0,1\}//' | grep . || echo "usage: bootstrap.sh [--dry-run] [--verify] [--no-desks] [--cli C] [--pubkey KEY|FILE]"; }

# Markers that mean "a dotfiles tool owns this file": never edit it, print the line instead.
FB_MANAGED_RE='dotfiles|chezmoi|yadm|home-manager|nix-darwin|stow|managed by|do not edit'
FB_BEGIN='# >>> fleet bootstrap: brew bin on the non-interactive ssh PATH (tmux-agent-fleet-kit) >>>'
FB_END='# <<< fleet bootstrap <<<'

fb_ssh_path() { # fleet dry: tmux and mosh-server on the ssh PATH, via ~/.zshenv when needed
  local fleet=$1 dry=$2 bin sh line f=$HOME/.zshenv
  if "$fleet" remote ssh-path --check >/dev/null 2>&1; then
    fb_ok "tmux and mosh-server are already on the non-interactive ssh PATH"
    "$fleet" remote ssh-path --check | sed 's/^/        /'
    return 0
  fi
  bin=$("$fleet" remote ssh-path --brew-bin 2>/dev/null)
  [ -n "$bin" ] || bin=/opt/homebrew/bin
  line="export PATH=\"$bin:\$PATH\""
  sh=$(basename "${FLEET_REMOTE_SSH_SHELL:-${SHELL:-/bin/zsh}}")
  if [ "$sh" != zsh ]; then
    fb_warn "login shell is $sh, not zsh: add this where $sh reads it for ssh host cmd (bootstrap only edits ~/.zshenv):"
    printf '        %s\n' "$line"; return 0
  fi
  if [ -f "$f" ] && grep -qF "$FB_BEGIN" "$f"; then
    fb_ok "~/.zshenv already has the fleet bootstrap block"; return 0
  fi
  if [ -L "$f" ]; then
    fb_warn "~/.zshenv is a symlink (dotfiles?), so it is left alone; add this line to its source yourself:"
    printf '        %s\n' "$line"; return 0
  fi
  if [ -f "$f" ] && grep -qiE "$FB_MANAGED_RE" "$f"; then
    fb_warn "~/.zshenv looks managed ($(grep -oiE "$FB_MANAGED_RE" "$f" | head -1)), so it is left alone; add this line yourself:"
    printf '        %s\n' "$line"; return 0
  fi
  if [ "$dry" = 1 ]; then fb_plan "would append to ~/.zshenv: $line"; return 0; fi
  { if [ -s "$f" ] && [ -n "$(tail -c1 "$f")" ]; then printf '\n'; fi
    printf '%s\n%s\n%s\n' "$FB_BEGIN" "$line" "$FB_END"; } >> "$f"
  fb_ok "appended to ~/.zshenv: $line"
  "$fleet" remote ssh-path --check | sed 's/^/        /' || fb_warn "still not on the ssh PATH after the append; check ~/.zshenv"
  return 0
}

fleet_bootstrap "$@"
