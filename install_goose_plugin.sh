#!/usr/bin/env bash
# HardTruth -> Goose plugin installer (standalone; also called from install.sh).
# Idempotent. Safe to re-run. No-op (exit 0) when goose is not installed.
#
# Behavior:
#   - goose NOT found  -> prints a note, exits 0. Nothing else changes.
#   - goose found      -> installs the hardtruth-goose plugin so every NEW
#                         goose session runs the HardTruth gate automatically.
#   - goose added later -> just re-run this script (or install.sh) again.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_SRC="$REPO_ROOT/goose-plugin-hardtruth"
PLUGIN_NAME="hardtruth-goose"

# 1. Detect goose (PATH, plus the fork's typical ~/.local/bin location).
GOOSE_BIN="$(command -v goose || true)"
if [ -z "$GOOSE_BIN" ] && [ -x "$HOME/.local/bin/goose" ]; then
    GOOSE_BIN="$HOME/.local/bin/goose"
fi

if [ -z "$GOOSE_BIN" ]; then
    echo "INFO: goose not detected - skipping goose plugin (HardTruth still installed for agy)."
    echo "      If you add goose later, re-run:  bash $REPO_ROOT/install_goose_plugin.sh"
    exit 0
fi
echo "OK: goose found: $GOOSE_BIN"

# 2. Replace any existing install (back it up first, keep it idempotent).
DEST="$HOME/.agents/plugins/$PLUGIN_NAME"
if [ -e "$DEST" ]; then
    BK="$DEST.bak-$(date +%s)"
    echo "OK: backing up existing plugin -> $BK"
    mv "$DEST" "$BK"
fi

echo "OK: installing hardtruth-goose from $PLUGIN_SRC ..."
"$GOOSE_BIN" plugin install "file://$PLUGIN_SRC"

# 3. Sanity: confirm the loader-visible pieces landed.
if [ ! -f "$DEST/hooks/hooks.json" ]; then
    echo "ERROR: plugin installed but hooks/hooks.json missing - see $DEST" >&2
    exit 1
fi
chmod +x "$DEST/hardtruth_hook_goose.py" 2>/dev/null || true

echo "OK: hardtruth-goose installed at $DEST"
echo "    Gate is active for NEW goose sessions; restart any currently-running goose session to pick it up."