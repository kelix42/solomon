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
    "$HERMES_PY" -c 'import solomon' >/dev/null 2>&1
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

# ---- 7. Register the plugin in Hermes config --------------------------------

HERMES_CFG="$HOME/.hermes/config.yaml"
if [ "$DRY_RUN" -eq 0 ]; then
    if [ -f "$HERMES_CFG" ]; then
        # Back up the original once.
        [ ! -f "$HERMES_CFG.pre-solomon" ] && cp "$HERMES_CFG" "$HERMES_CFG.pre-solomon"
        # Add solomon to plugins.enabled if not already there.
        if ! grep -q "^  - solomon$" "$HERMES_CFG" 2>/dev/null && ! grep -q "^- solomon$" "$HERMES_CFG" 2>/dev/null; then
            # Simple append. Users with complex configs can edit by hand.
            {
                echo ""
                echo "# Added by Solomon installer"
                echo "plugins:"
                echo "  enabled:"
                echo "    - solomon"
            } >> "$HERMES_CFG"
            ok "Solomon added to $HERMES_CFG"
        else
            ok "Solomon already in $HERMES_CFG"
        fi
    else
        warn "$HERMES_CFG does not exist. Hermes may need to be started once first."
    fi
else
    printf "  [dry-run] append 'solomon' to plugins.enabled in %s\n" "$HERMES_CFG"
fi

# ---- 8. Install cron jobs ---------------------------------------------------

if command -v crontab >/dev/null 2>&1; then
    add_cron() {
        local marker="$1" schedule="$2" command="$3"
        if crontab -l 2>/dev/null | grep -q "$marker"; then
            return 0
        fi
        ( crontab -l 2>/dev/null; echo "$schedule $command # $marker" ) | crontab -
    }
    if [ "$DRY_RUN" -eq 0 ]; then
        add_cron "solomon-brain-daily"   "0 2 * * *"  "$HERMES_PY -m solomon.cli daily   >> $HOME/.hermes/solomon/logs/cron.log 2>&1"
        add_cron "solomon-brain-weekly"  "0 3 * * 0"  "$HERMES_PY -m solomon.cli weekly  >> $HOME/.hermes/solomon/logs/cron.log 2>&1"
        add_cron "solomon-brain-checkin" "0 15 * * 5" "$HERMES_PY -m solomon.cli checkin >> $HOME/.hermes/solomon/logs/cron.log 2>&1"
        ok "Crons installed (daily 02:00, weekly Sun 03:00, checkin Fri 15:00)"
    else
        printf "  [dry-run] add three crontab entries: daily / weekly / checkin\n"
    fi
else
    warn "crontab not on PATH. Skipping cron setup. You can still run 'solomon daily' manually."
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
