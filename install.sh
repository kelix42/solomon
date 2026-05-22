#!/usr/bin/env bash
# Solomon one-line installer.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/YOUR_GH/solomon/main/install.sh | bash
#
# What it does:
#   1. Checks for Hermes; offers to install if missing.
#   2. pip-installs solomon-brain into the Hermes Python environment.
#   3. Runs `solomon init` to provision the database and configure Hermes.
#   4. Walks the user through the first onboarding session (optional).
#
# Re-running this script is safe and idempotent.

set -euo pipefail

BLUE='\033[1;36m'; GREEN='\033[1;32m'; YELLOW='\033[1;33m'; RED='\033[1;31m'; NC='\033[0m'

say()  { printf "${BLUE}== %s${NC}\n" "$*"; }
ok()   { printf "${GREEN}✓ %s${NC}\n" "$*"; }
warn() { printf "${YELLOW}! %s${NC}\n" "$*"; }
err()  { printf "${RED}✗ %s${NC}\n" "$*"; }

# ---- detect Python ---------------------------------------------------------

HERMES_PY=""
for candidate in \
    /usr/local/lib/hermes-agent/venv/bin/python3 \
    /opt/homebrew/lib/hermes-agent/venv/bin/python3 \
    "$HOME/.hermes/hermes-agent/venv/bin/python3"; do
    [ -x "$candidate" ] && HERMES_PY="$candidate" && break
done

if [ -z "${HERMES_PY:-}" ]; then
    warn "Hermes Python venv not found. Solomon needs Hermes installed first."
    read -rp "Install Hermes now? [Y/n] " ans
    ans=${ans:-Y}
    if [[ "$ans" =~ ^[Yy] ]]; then
        say "Installing Hermes..."
        curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
        # Try detection again.
        for candidate in \
            /usr/local/lib/hermes-agent/venv/bin/python3 \
            /opt/homebrew/lib/hermes-agent/venv/bin/python3 \
            "$HOME/.hermes/hermes-agent/venv/bin/python3"; do
            [ -x "$candidate" ] && HERMES_PY="$candidate" && break
        done
    fi
fi

if [ -z "${HERMES_PY:-}" ]; then
    err "Could not find Hermes Python venv. Install Hermes first, then re-run this script."
    exit 1
fi

ok "Using Python at $HERMES_PY"

# ---- install solomon-brain -------------------------------------------------

say "Installing solomon-brain..."
SOLOMON_REPO="${SOLOMON_REPO:-https://github.com/YOUR_GH/solomon.git}"
SOLOMON_REF="${SOLOMON_REF:-main}"

# If we're being run from inside a checkout, install from there. Else pip
# install from git.
if [ -f "$(dirname "$0")/pyproject.toml" ] && grep -q '"solomon-brain"' "$(dirname "$0")/pyproject.toml"; then
    "$HERMES_PY" -m pip install --upgrade "$(cd "$(dirname "$0")" && pwd)"
else
    "$HERMES_PY" -m pip install --upgrade "git+${SOLOMON_REPO}@${SOLOMON_REF}"
fi

ok "solomon-brain installed."

# ---- locate the solomon CLI ------------------------------------------------

SOLOMON_BIN="$(dirname "$HERMES_PY")/solomon"
if [ ! -x "$SOLOMON_BIN" ]; then
    err "solomon CLI not found at $SOLOMON_BIN. Re-check the pip install above."
    exit 1
fi

# ---- run solomon init ------------------------------------------------------

say "Running solomon init..."
"$SOLOMON_BIN" init

# ---- restart Hermes gateway if running -------------------------------------

if command -v hermes >/dev/null 2>&1; then
    if hermes gateway status >/dev/null 2>&1; then
        say "Restarting Hermes gateway so Solomon attaches..."
        hermes gateway restart || warn "Gateway restart failed; restart manually."
    fi
fi

cat <<'EOF'

╔══════════════════════════════════════════════════════════════╗
║                  Solomon is installed.                       ║
║                                                              ║
║  Every Hermes session now flows through Solomon.             ║
║                                                              ║
║  Start the foundation interview:                             ║
║      solomon onboard session_1                               ║
║                                                              ║
║  Or just open Hermes and start talking:                      ║
║      hermes                                                  ║
║                                                              ║
║  Need to opt out of logging for one conversation?            ║
║      /private                                                ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
EOF
