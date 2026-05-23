#!/usr/bin/env bash
# Solomon one-line installer.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/kelix42/solomon/main/install.sh | bash
#   bash install.sh                  # from a checkout
#   bash install.sh --dry-run        # show every command, run nothing
#   bash install.sh --help
#
# What it does:
#   1. Locates (or installs) Hermes — Solomon plugs into Hermes's Python venv.
#   2. pip-installs solomon-brain (-e . if run from a repo checkout).
#   3. Runs `solomon init` to provision the DB, scaffold ~/.hermes/solomon/,
#      enable the plugin in Hermes config, register the sleep cron.
#   4. Prints the onboarding next-steps (7 interview sessions, corpus drop,
#      mentoring review, autonomy ladder).
#
# Re-running this script is safe and idempotent. Each step short-circuits
# with a "✓ already installed" line when its preconditions are already met.

set -euo pipefail

# ---- options ---------------------------------------------------------------

DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -40
            exit 0
            ;;
        *)
            printf "Unknown flag: %s\n" "$arg" >&2
            exit 2
            ;;
    esac
done

# ---- styling ---------------------------------------------------------------

BLUE='\033[1;36m'; GREEN='\033[1;32m'; YELLOW='\033[1;33m'; RED='\033[1;31m'; NC='\033[0m'
say()  { printf "${BLUE}== %s${NC}\n" "$*"; }
ok()   { printf "${GREEN}✓ %s${NC}\n" "$*"; }
warn() { printf "${YELLOW}! %s${NC}\n" "$*"; }
err()  { printf "${RED}✗ %s${NC}\n" "$*"; }

# Run a command, or just print it in dry-run mode.
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf "  [dry-run] %s\n" "$*"
    else
        eval "$@"
    fi
}

if [ "$DRY_RUN" -eq 1 ]; then
    warn "DRY RUN — printing commands, executing nothing."
fi

# ---- detect Hermes Python --------------------------------------------------

find_hermes_py() {
    local candidate
    for candidate in \
        /usr/local/lib/hermes-agent/venv/bin/python3 \
        /opt/homebrew/lib/hermes-agent/venv/bin/python3 \
        "$HOME/.hermes/hermes-agent/venv/bin/python3"; do
        [ -x "$candidate" ] && { printf "%s" "$candidate"; return 0; }
    done
    return 1
}

HERMES_PY=""
if HERMES_PY="$(find_hermes_py)"; then
    ok "Hermes already installed (Python at $HERMES_PY)"
else
    warn "Hermes Python venv not found. Solomon needs Hermes installed first."
    say  "Installing Hermes..."
    HERMES_INSTALL_URL="${HERMES_INSTALL_URL:-https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh}"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf "  [dry-run] curl -fsSL %s | bash\n" "$HERMES_INSTALL_URL"
    else
        curl -fsSL "$HERMES_INSTALL_URL" | bash
        if ! HERMES_PY="$(find_hermes_py)"; then
            err "Hermes install completed but venv still not found. Install manually:"
            err "  pip install hermes-agent"
            exit 1
        fi
        ok "Hermes installed (Python at $HERMES_PY)"
    fi
fi

# Make a best-guess HERMES_PY available even in dry-run so subsequent
# commands print sensible paths.
if [ -z "${HERMES_PY:-}" ]; then
    HERMES_PY="/usr/local/lib/hermes-agent/venv/bin/python3"
fi

# ---- install solomon-brain -------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || pwd)"
SOLOMON_REPO="${SOLOMON_REPO:-https://github.com/kelix42/solomon.git}"
SOLOMON_REF="${SOLOMON_REF:-main}"

solomon_already_installed() {
    [ "$DRY_RUN" -eq 1 ] && return 1
    "$HERMES_PY" -c 'import solomon' >/dev/null 2>&1
}

if solomon_already_installed; then
    ok "solomon-brain already installed in $HERMES_PY"
else
    say "Installing solomon-brain..."
    if [ -f "$SCRIPT_DIR/pyproject.toml" ] && grep -q '"solomon-brain"' "$SCRIPT_DIR/pyproject.toml" 2>/dev/null; then
        run "\"$HERMES_PY\" -m pip install -e \"$SCRIPT_DIR\""
    else
        run "\"$HERMES_PY\" -m pip install --upgrade \"solomon-brain @ git+${SOLOMON_REPO}@${SOLOMON_REF}\""
    fi
    ok "solomon-brain installed."
fi

# ---- locate the solomon CLI ------------------------------------------------

SOLOMON_BIN="$(dirname "$HERMES_PY")/solomon"
if [ "$DRY_RUN" -eq 0 ] && [ ! -x "$SOLOMON_BIN" ]; then
    err "solomon CLI not found at $SOLOMON_BIN. Re-check the pip install above."
    exit 1
fi

# ---- run solomon init (idempotent inside the CLI) --------------------------

solomon_init_already_done() {
    [ "$DRY_RUN" -eq 1 ] && return 1
    [ -d "${HOME}/.hermes/solomon" ] && \
        [ -f "${HOME}/.hermes/solomon/solomon.db" -o -f "${HOME}/.hermes/.env" ]
}

if solomon_init_already_done; then
    ok "solomon init already run (~/.hermes/solomon exists). Re-run manually if needed: solomon init"
else
    say "Running solomon init..."
    run "\"$SOLOMON_BIN\" init"
fi

# ---- restart Hermes gateway if running -------------------------------------

if command -v hermes >/dev/null 2>&1; then
    if [ "$DRY_RUN" -eq 1 ]; then
        printf "  [dry-run] hermes gateway restart (if running)\n"
    elif hermes gateway status >/dev/null 2>&1; then
        say "Restarting Hermes gateway so Solomon attaches..."
        hermes gateway restart || warn "Gateway restart failed; restart manually."
    fi
fi

# ---- prompt for onboarding -------------------------------------------------

cat <<EOF

$(printf "${GREEN}Solomon is installed.${NC}")

Next steps walk you from a blank Solomon to an autonomy-L0 chief of staff.
Each is a manual command — install.sh does not drive these (they're long,
interactive, and user-paced).

$(printf "${BLUE}1. Foundation interview (~7 sessions, 20–40 min each)${NC}")
   Start here:
       solomon onboard session_0
   Then proceed through session_1 ... session_6 at your own pace.
   Resume-on-Ctrl-C works; sessions stay open until you complete them.

$(printf "${BLUE}2. Drop your historical material into the corpus${NC}")
   Move SOPs, emails, transcripts, docs, slides, sheets into:
       ~/.hermes/solomon/corpus/inbox/
   Then either run a one-shot ingest:
       solomon corpus ingest <path>
   …or start the always-on watcher:
       solomon corpus watch

$(printf "${BLUE}3. Review the rules Solomon found in your material${NC}")
       solomon mentoring review
   You'll see one proposed rule at a time: approve / reject / edit / skip.
   Approve = it joins the heuristics table and starts influencing decisions.

$(printf "${BLUE}4. You're now in observe-only mode (autonomy L0)${NC}")
   Solomon will watch and suggest, but won't act on its own. Promote
   autonomy per-scope as you build trust:
       solomon autonomy set <scope> L1   # (lands in a later session)

EOF

if [ "$DRY_RUN" -eq 1 ]; then
    warn "DRY RUN complete. Re-run without --dry-run to actually install."
fi
