#!/usr/bin/env bash
# Solomon one-line installer.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/kelix42/solomon/main/install.sh | bash
#   bash install.sh                  # from a checkout
#   bash install.sh --dry-run        # show every command, run nothing
#   bash install.sh --help
#
# Run the same curl command again later to UPDATE — the script detects an
# existing install and reinstalls from the latest commit on main (or whatever
# you set SOLOMON_REF= to). Editable installs (pip install -e from a local
# checkout) are detected and left alone so local edits aren't lost.
#
# What it does:
#   1. Locates (or installs) Hermes — Solomon plugs into Hermes's Python venv.
#   2. pip-installs (or upgrades) solomon-brain. Skips when an editable
#      install is detected — pull the latest with `git pull` in that case.
#   3. Drops a /bin/bash wrapper next to the existing `hermes` binary on PATH
#      so `solomon ...` works from any shell.
#   4. Runs `solomon init` to provision the DB, scaffold ~/.hermes/solomon/,
#      enable the plugin in Hermes config, register the sleep cron.
#   5. Restarts the Hermes gateway if it's running.
#   6. Prints the onboarding next-steps.
#
# Re-running this script is safe and idempotent. Each step short-circuits
# with a "✓ already installed" line when its preconditions are already met,
# except the pip step which always upgrades from git when not editable.

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

# Detect three states:
#   1. Editable install (pip install -e) → leave alone; the user has a local
#      checkout that is the source of truth. Overwriting it would silently
#      lose their work.
#   2. Local checkout next to install.sh → reinstall editable from this
#      checkout. Picks up any local changes.
#   3. Anything else (the common curl-one-liner case) → upgrade from git
#      with --force-reinstall, so re-running the curl command always pulls
#      the latest commit on main (or the requested SOLOMON_REF).

solomon_is_editable() {
    # Detection is read-only — safe to run even in dry-run mode so the
    # script's dry-run output reflects reality.
    "$HERMES_PY" -m pip show solomon-brain 2>/dev/null \
        | grep -q '^Editable project location:'
}

solomon_is_installed() {
    # Detection is read-only — safe to run even in dry-run mode.
    "$HERMES_PY" -c 'import solomon' >/dev/null 2>&1
}

if solomon_is_editable; then
    ed_path="$("$HERMES_PY" -m pip show solomon-brain 2>/dev/null \
        | awk -F': ' '/^Editable project location:/ {print $2}')"
    ok "solomon-brain editable install detected at $ed_path"
    warn "Skipping pip step so your local edits are preserved."
    warn "To pull upstream changes here, run: cd \"$ed_path\" && git pull"
elif [ -f "$SCRIPT_DIR/pyproject.toml" ] && grep -q '"solomon-brain"' "$SCRIPT_DIR/pyproject.toml" 2>/dev/null; then
    say "Installing solomon-brain from local checkout ($SCRIPT_DIR)..."
    run "\"$HERMES_PY\" -m pip install -e \"$SCRIPT_DIR\""
    ok "solomon-brain installed (editable)."
else
    if solomon_is_installed; then
        say "Upgrading solomon-brain from ${SOLOMON_REPO}@${SOLOMON_REF}..."
    else
        say "Installing solomon-brain from ${SOLOMON_REPO}@${SOLOMON_REF}..."
    fi
    # --force-reinstall guarantees we pick up new commits even when the
    # version string in pyproject.toml hasn't been bumped (the common case
    # during fast iteration on a tester branch).
    run "\"$HERMES_PY\" -m pip install --upgrade --force-reinstall \"solomon-brain @ git+${SOLOMON_REPO}@${SOLOMON_REF}\""
    ok "solomon-brain installed."
fi

# ---- locate the solomon CLI ------------------------------------------------

SOLOMON_BIN="$(dirname "$HERMES_PY")/solomon"
if [ "$DRY_RUN" -eq 0 ] && [ ! -x "$SOLOMON_BIN" ]; then
    err "solomon CLI not found at $SOLOMON_BIN. Re-check the pip install above."
    exit 1
fi

# ---- expose `solomon` on the system PATH -----------------------------------
#
# pip installs the entry point inside the Hermes venv (above), which is not on
# the default user PATH. Without a wrapper the install completes but
# `solomon onboard session_0` fails with command-not-found.
#
# Strategy: place a thin wrapper next to the existing `hermes` binary on PATH
# (matches Hermes's own /usr/local/bin/hermes shim). Falls back to
# /usr/local/bin if `hermes` isn't on PATH yet. Idempotent — overwrites any
# existing wrapper.

place_path_wrapper() {
    local target_dir=""
    local venv_bin
    venv_bin="$(dirname "$SOLOMON_BIN")"

    # First choice: alongside the existing `hermes` binary on PATH.
    # Resolve via `command -v`, but reject the venv bin itself — if the venv
    # is on PATH (dev boxes, some Hermes installs) `command -v` returns
    # the venv hermes, which would make our wrapper a self-referencing loop.
    if command -v hermes >/dev/null 2>&1; then
        local hermes_path
        hermes_path="$(command -v hermes)"
        local hermes_dir
        hermes_dir="$(dirname "$hermes_path")"
        if [ "$hermes_dir" != "$venv_bin" ]; then
            target_dir="$hermes_dir"
        fi
    fi

    # Fallback to /usr/local/bin if the hermes-sibling directory is missing,
    # is the venv itself, or isn't writable.
    if [ -z "$target_dir" ] || { [ "$DRY_RUN" -eq 0 ] && [ ! -w "$target_dir" ]; }; then
        target_dir=/usr/local/bin
    fi

    # Final writability check (skipped in dry-run since we're not really writing).
    if [ "$DRY_RUN" -eq 0 ] && [ ! -w "$target_dir" ]; then
        err "Cannot write to $target_dir (need sudo?). Re-run with sudo, or"
        err "manually create the wrapper:"
        err "    sudo ln -sf $SOLOMON_BIN /usr/local/bin/solomon"
        return 1
    fi

    local wrapper="$target_dir/solomon"

    # Defence-in-depth: a wrapper that exec's itself would loop forever.
    # This can't happen given the venv-bin check above, but cheap to verify.
    if [ "$wrapper" = "$SOLOMON_BIN" ]; then
        err "Refusing to write wrapper at $wrapper — same path as the venv entry point."
        return 1
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
        printf "  [dry-run] write wrapper to %s exec'ing %s\n" "$wrapper" "$SOLOMON_BIN"
        printf "  [dry-run] chmod +x %s\n" "$wrapper"
        ok "solomon command would be available at $wrapper"
        return 0
    fi

    # Heredoc: $SOLOMON_BIN expands now (resolves to the venv path), but
    # \$@ stays literal so the wrapper forwards arguments at run time.
    cat > "$wrapper" <<EOF
#!/bin/bash
exec "$SOLOMON_BIN" "\$@"
EOF
    chmod +x "$wrapper"
    ok "solomon command available on PATH ($wrapper)"
}

place_path_wrapper || exit 1

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

$(printf "${BLUE}5. To update later${NC}")
   Run the same curl command again — it detects an existing install and
   reinstalls from the latest commit on main:
       curl -fsSL https://raw.githubusercontent.com/kelix42/solomon/main/install.sh | bash
   Your foundation files, corpus, and database are not touched.

EOF

if [ "$DRY_RUN" -eq 1 ]; then
    warn "DRY RUN complete. Re-run without --dry-run to actually install."
fi
