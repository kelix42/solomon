#!/usr/bin/env bash
# Solomon installer. Idempotent. Safe to re-run.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/kelix42/solomon/main/install.sh | bash
#   bash install.sh           # from a checkout
#   bash install.sh --dry-run # show every step, run nothing

set -euo pipefail

DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "Unknown flag: $arg" >&2; exit 2 ;;
    esac
done

BLUE='\033[1;36m'; GREEN='\033[1;32m'; YELLOW='\033[1;33m'; RED='\033[1;31m'; NC='\033[0m'
say()  { printf "${BLUE}== %s${NC}\n" "$*"; }
ok()   { printf "${GREEN}✓ %s${NC}\n" "$*"; }
warn() { printf "${YELLOW}! %s${NC}\n" "$*"; }
err()  { printf "${RED}✗ %s${NC}\n" "$*"; }

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf "  [dry-run] %s\n" "$*"
    else
        eval "$@"
    fi
}

[ "$DRY_RUN" -eq 1 ] && warn "DRY RUN — printing every step, executing nothing."

# ---- Preflight: OS, git, and (after detecting Hermes) Python version ---------

case "${OSTYPE:-}" in
    msys*|cygwin*|win32*)
        cat <<'EOF'

Solomon doesn't run on native Windows yet.

The good news is you can run Solomon on Windows through WSL (Windows
Subsystem for Linux). Once you have WSL set up with a Linux distribution
like Ubuntu, open it and run this same installer in there:

  curl -fsSL https://raw.githubusercontent.com/kelix42/solomon/main/install.sh | bash

If you'd rather not use WSL, native Windows support is on the roadmap.
For now, the supported platforms are macOS and Linux (WSL counts as Linux).

EOF
        exit 1
        ;;
esac

if ! command -v git >/dev/null 2>&1; then
    cat <<'EOF'

Solomon needs git installed.

Solomon keeps a history of every change — what rules you've captured,
what got edited, what you've rejected. It uses git to track those changes
so you can roll back anything at any time. Without git, that history
won't work.

Install git, then run the Solomon installer again:

  - macOS:   brew install git
             (or: xcode-select --install)
  - Linux:   sudo apt install git        (Debian / Ubuntu)
             sudo dnf install git        (Fedora)

EOF
    exit 1
fi
ok "git detected ($(command -v git))"

# ---- 1. Detect Hermes (don't install it ourselves) ---------------------------

find_hermes_py() {
    for p in \
        "$HOME/.hermes/hermes-agent/venv/bin/python3" \
        /usr/local/lib/hermes-agent/venv/bin/python3 \
        /opt/homebrew/lib/hermes-agent/venv/bin/python3; do
        [ -x "$p" ] && { echo "$p"; return 0; }
    done
    return 1
}

HERMES_PY=""
if HERMES_PY="$(find_hermes_py)"; then
    ok "Hermes detected (Python at $HERMES_PY)"
elif [ "$DRY_RUN" -eq 1 ]; then
    warn "Hermes not found — would normally exit with a message and stop here."
    HERMES_PY="/usr/local/lib/hermes-agent/venv/bin/python3"
else
    cat <<'EOF'

Solomon needs Hermes installed first.

Solomon is built on top of Hermes — it adds a personal business brain to it.
So Hermes has to be set up first before Solomon can do anything.

Three steps:

  1. Install Hermes:

     curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash

  2. Open Hermes once. It will ask you to pick a channel — Telegram, the
     desktop chat, SMS, or whichever one you want to use. Follow Hermes's
     setup. You'll know it's done when you can send Hermes a message and
     get a reply back.

  3. Come back here and run this same Solomon installer again:

     curl -fsSL https://raw.githubusercontent.com/kelix42/solomon/main/install.sh | bash

That's it. The Solomon installer will pick up where it left off — you
don't lose any progress by stopping here.

EOF
    exit 1
fi

# Verify the Hermes Python is at least 3.10 (Solomon's minimum).
if [ "$DRY_RUN" -eq 0 ]; then
    HERMES_PY_VERSION="$("$HERMES_PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "unknown")"
    if ! "$HERMES_PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        cat <<EOF

Solomon needs Python 3.10 or newer.

Your Hermes installation is running on Python $HERMES_PY_VERSION, which is
older than what Solomon supports.

You have two ways forward:

  1. Reinstall Hermes on a newer Python. Hermes's own docs explain how
     to point its installer at a specific Python version.

  2. Install a newer Python on this machine, then reinstall Hermes:

       - macOS:  brew install python@3.12
       - Linux:  sudo apt install python3.12   (Debian / Ubuntu)
                 sudo dnf install python3.12   (Fedora)
                 or use pyenv / asdf to manage versions

Once Hermes is running on Python 3.10 or newer, come back and run the
Solomon installer again.

EOF
        exit 1
    fi
    ok "Hermes Python is $HERMES_PY_VERSION (>= 3.10)"
fi

# ---- 2. Bootstrap pip in the Hermes venv if missing --------------------------

if [ "$DRY_RUN" -eq 0 ] && ! "$HERMES_PY" -m pip --version >/dev/null 2>&1; then
    say "Bootstrapping pip in Hermes venv..."
    "$HERMES_PY" -m ensurepip --upgrade >/dev/null 2>&1 || {
        err "ensurepip failed. Bootstrap manually: $HERMES_PY -m ensurepip --upgrade"
        exit 1
    }
    "$HERMES_PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
    ok "pip bootstrapped."
fi

# ---- 3. Install solomon-brain ------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || pwd)"
SOLOMON_REPO="${SOLOMON_REPO:-https://github.com/kelix42/solomon.git}"
SOLOMON_REF="${SOLOMON_REF:-main}"

solomon_installed() {
    [ "$DRY_RUN" -eq 1 ] && return 1
    # Use pip show, not `python -c 'import solomon'`. The latter is a false
    # positive when this script runs from the solomon checkout — cwd ends up
    # on sys.path, so the local package imports even when it was never pip
    # installed into the Hermes venv.
    "$HERMES_PY" -m pip show solomon-brain >/dev/null 2>&1
}

if solomon_installed; then
    ok "Solomon already installed."
else
    say "Installing solomon-brain..."
    if [ -f "$SCRIPT_DIR/pyproject.toml" ] && grep -q '"solomon-brain"' "$SCRIPT_DIR/pyproject.toml" 2>/dev/null; then
        run "\"$HERMES_PY\" -m pip install --quiet -e \"$SCRIPT_DIR\""
    else
        run "\"$HERMES_PY\" -m pip install --quiet --upgrade \"solomon-brain @ git+${SOLOMON_REPO}@${SOLOMON_REF}\""
    fi
    ok "solomon-brain installed."
fi

# ---- 4. Wrap the CLI on PATH -------------------------------------------------

SOLOMON_BIN="$(dirname "$HERMES_PY")/solomon"
if [ "$DRY_RUN" -eq 0 ] && [ ! -x "$SOLOMON_BIN" ]; then
    err "solomon CLI not found at $SOLOMON_BIN. Check the pip install above."
    exit 1
fi

# Pick a writable directory on PATH. Prefer ~/.local/bin (no sudo needed).
target_dir=""
for d in "$HOME/.local/bin" "/usr/local/bin"; do
    if [ "$DRY_RUN" -eq 0 ] && [ ! -d "$d" ]; then
        mkdir -p "$d" 2>/dev/null || continue
    fi
    if [ -w "$d" ] || [ "$DRY_RUN" -eq 1 ]; then
        target_dir="$d"
        break
    fi
done

if [ -z "$target_dir" ]; then
    warn "No writable directory on PATH for the solomon wrapper. Skipping."
    warn "You can still run: $SOLOMON_BIN doctor"
elif [ "$DRY_RUN" -eq 1 ]; then
    printf "  [dry-run] write %s/solomon wrapper that execs %s\n" "$target_dir" "$SOLOMON_BIN"
    ok "solomon would be available at $target_dir/solomon"
else
    cat > "$target_dir/solomon" <<EOF
#!/bin/bash
exec "$SOLOMON_BIN" "\$@"
EOF
    chmod +x "$target_dir/solomon"
    ok "solomon command available at $target_dir/solomon"
    case ":$PATH:" in
        *":$target_dir:"*) ;;
        *) warn "Note: $target_dir is not on your PATH. Add this to your shell rc:"
           warn "    export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
    esac
fi

# ---- 5. Scaffold ~/.hermes/solomon/ via the CLI ------------------------------

if [ "$DRY_RUN" -eq 0 ]; then
    "$HERMES_PY" -m solomon.cli init
else
    printf "  [dry-run] %s -m solomon.cli init\n" "$HERMES_PY"
fi

# ---- 6. Install the skill files ---------------------------------------------

SKILL_SRC=""
if "$HERMES_PY" -c "import solomon; import os; print(os.path.join(os.path.dirname(solomon.__file__), 'skills'))" >/dev/null 2>&1; then
    SKILL_SRC="$("$HERMES_PY" -c "import solomon; import os; print(os.path.join(os.path.dirname(solomon.__file__), 'skills'))" 2>/dev/null)"
fi
SKILL_DST="$HOME/.hermes/skills/solomon"
if [ -n "$SKILL_SRC" ] && [ -d "$SKILL_SRC" ]; then
    run "mkdir -p \"$SKILL_DST\""
    run "cp \"$SKILL_SRC\"/*.md \"$SKILL_DST/\""
    ok "Solomon skill files installed to $SKILL_DST"
else
    warn "Could not locate skill source directory inside the solomon package."
fi

# ---- 7. Register the plugin via Hermes's own CLI ----------------------------
#
# We back up ~/.hermes/config.yaml once (so `solomon uninstall` can restore
# it cleanly), then create a tiny shim directory at ~/.hermes/plugins/solomon/
# that gives Hermes a directory-based plugin presence (its `plugins enable`
# CLI only validates directory plugins, not entry-point ones). The shim's
# __init__.py just imports register() from the pip-installed solomon package.
# Either discovery path (entry-point or directory) loads the same code.

HERMES_CFG="$HOME/.hermes/config.yaml"
if [ -f "$HERMES_CFG" ] && [ ! -f "$HERMES_CFG.pre-solomon" ]; then
    [ "$DRY_RUN" -eq 0 ] && cp "$HERMES_CFG" "$HERMES_CFG.pre-solomon"
fi

PLUGIN_SHIM="$HOME/.hermes/plugins/solomon"
if [ "$DRY_RUN" -eq 0 ]; then
    mkdir -p "$PLUGIN_SHIM"
    # plugin.yaml ships inside the package — copy from the pip-installed location
    # so the shim matches the installed version.
    PLUGIN_YAML="$("$HERMES_PY" -c "import solomon, os; print(os.path.join(os.path.dirname(solomon.__file__), 'plugin.yaml'))" 2>/dev/null)"
    if [ -n "$PLUGIN_YAML" ] && [ -f "$PLUGIN_YAML" ]; then
        cp "$PLUGIN_YAML" "$PLUGIN_SHIM/plugin.yaml"
    fi
    cat > "$PLUGIN_SHIM/__init__.py" <<'PYEOF'
"""Hermes plugin shim for Solomon.

Hermes discovers plugins by directory. The real code lives in the
pip-installed `solomon` package; this shim just re-exports `register`
so Hermes's directory-based discovery finds it.
"""
from solomon.plugin import register  # noqa: F401
PYEOF
    ok "Plugin shim installed at $PLUGIN_SHIM"
else
    printf "  [dry-run] write %s/{plugin.yaml,__init__.py}\n" "$PLUGIN_SHIM"
fi

run "\"$HERMES_PY\" -m hermes_cli.main plugins enable solomon"

# ---- 8. Register cron jobs with Hermes (NOT system crontab) ------------------
#
# Per the v3 design, Solomon registers its 17 scheduled jobs through Hermes's
# own cron API (cron.jobs.create_job). Hermes's gateway scheduler fires them
# every 60 seconds. This is portable (Windows-compatible when Hermes is),
# survives reboots without user crontab edits, and integrates with Hermes's
# auth/credentials/logging.

run "\"$HERMES_PY\" -m solomon.cli register-crons"

# ---- 9. Re-confirm plugins.enabled --------------------------------------------
#
# If a Hermes gateway is running concurrently with this install, it may
# rewrite config.yaml from its in-memory snapshot mid-install. Re-issuing
# `plugins enable solomon` after cron registration ensures the final
# on-disk state has Solomon enabled. The command is idempotent.

run "\"$HERMES_PY\" -m hermes_cli.main plugins enable solomon"

# Detect a running gateway and tell the user it needs to restart for
# Solomon to be picked up. The cron jobs we registered DO get picked
# up automatically (Hermes polls the cron DB every 60s), but the
# plugin contract (tools/hooks/commands) only loads at session start.
if [ "$DRY_RUN" -eq 0 ] && [ -f "$HOME/.hermes/gateway.pid" ]; then
    GW_PID="$(grep -o '"pid": *[0-9]*' "$HOME/.hermes/gateway.pid" 2>/dev/null | grep -o '[0-9]*')"
    if [ -n "$GW_PID" ] && ps -p "$GW_PID" >/dev/null 2>&1; then
        warn "Hermes gateway is running (pid $GW_PID). Restart it for Solomon"
        warn "to be picked up:  hermes gateway restart"
    fi
fi

# ---- Done -------------------------------------------------------------------

cat <<EOF

$(printf "${GREEN}Solomon is installed.${NC}")

Where things live:
  Home:        ~/.hermes/solomon/
  Skills:      ~/.hermes/skills/solomon/
  CLI:         solomon (or $SOLOMON_BIN)

Next steps:
  1. Open Hermes (Telegram, the desktop chat, or whichever channel you use).
  2. Type ${BLUE}/onboard${NC} to start the first foundation interview.
  3. Run ${BLUE}solomon doctor${NC} any time to confirm everything is wired.

That's it. You can stop and resume the interviews at any time.
EOF

[ "$DRY_RUN" -eq 1 ] && warn "DRY RUN complete. Re-run without --dry-run to install for real."
