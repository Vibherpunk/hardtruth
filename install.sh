#!/bin/bash
set -e

echo "=========================================================="
echo "  HardTruth: Autonomous Anti-Hallucination & Truth Shield"
echo "=========================================================="

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARDTRUTH_DIR="$HOME/.hardtruth"
HARDTRUTH_LIB="$HARDTRUTH_DIR/lib/hardtruth"
GLOBAL_HOOKS_DIR="$HARDTRUTH_DIR/hooks"
HALTS_DIR="$HARDTRUTH_DIR/halts"

# 1. Install library package to ~/.hardtruth/lib
mkdir -p "$HARDTRUTH_LIB"
mkdir -p "$GLOBAL_HOOKS_DIR"
mkdir -p "$HALTS_DIR"
chmod 700 "$HALTS_DIR"

cp "$REPO_ROOT/client/ast_checker.py" "$HARDTRUTH_LIB/ast_checker.py"
cp "$REPO_ROOT/client/hardtruth_client.py" "$HARDTRUTH_LIB/hardtruth_client.py"
cp "$REPO_ROOT/client/hardtruth_hook.py" "$HARDTRUTH_LIB/hardtruth_hook.py"
cp "$REPO_ROOT/daemon/ledger.py" "$HARDTRUTH_LIB/ledger.py"
cp "$REPO_ROOT/daemon/tier2_runner.py" "$HARDTRUTH_LIB/tier2_runner.py"
touch "$HARDTRUTH_LIB/__init__.py"
# Also install at ~/.hardtruth/lib/ for direct top-level access
cp "$REPO_ROOT/client/hardtruth_hook.py" "$HARDTRUTH_DIR/lib/hardtruth_hook.py"
cp "$REPO_ROOT/client/ast_checker.py" "$HARDTRUTH_DIR/lib/ast_checker.py"
cp "$REPO_ROOT/daemon/ledger.py" "$HARDTRUTH_DIR/lib/ledger.py"
cp "$REPO_ROOT/daemon/tier2_runner.py" "$HARDTRUTH_DIR/lib/tier2_runner.py"
echo "✓ HardTruth client library installed to $HARDTRUTH_LIB"

# 2. Antigravity Configuration
ANTIGRAVITY_CONFIG_DIR="$HOME/.gemini/config"
if [ -d "$HOME/.gemini" ]; then
    echo "Configuring Antigravity global hooks..."
    mkdir -p "$ANTIGRAVITY_CONFIG_DIR"
    cp "$REPO_ROOT/client/hardtruth_hook.py" "$ANTIGRAVITY_CONFIG_DIR/hardtruth_hook.py"
    cp "$REPO_ROOT/client/ast_checker.py" "$ANTIGRAVITY_CONFIG_DIR/ast_checker.py"
    cp "$REPO_ROOT/daemon/ledger.py" "$ANTIGRAVITY_CONFIG_DIR/ledger.py"
    cp "$REPO_ROOT/daemon/tier2_runner.py" "$ANTIGRAVITY_CONFIG_DIR/tier2_runner.py"
    chmod +x "$ANTIGRAVITY_CONFIG_DIR/hardtruth_hook.py"
    
    HOOKS_FILE="$ANTIGRAVITY_CONFIG_DIR/hooks.json"
    if [ -f "$HOOKS_FILE" ]; then
        cp "$HOOKS_FILE" "$HOOKS_FILE.bak.$(date +%s)"
    fi
    python3 -c "
import json, os
p = os.path.expanduser('~/.gemini/config/hooks.json')
try:
    with open(p, 'r') as f:
        data = json.load(f)
except Exception:
    data = {}
data['hardtruth'] = {
    'enabled': True,
    'PostToolUse': [{'matcher': '*', 'hooks': [{'type': 'command', 'command': 'python3 ~/.gemini/config/hardtruth_hook.py post_tool', 'timeout': 20}]}],
    # NB: Stop must be FLAT (no matcher wrapper) and 300s: 90s gets SIGKILLed
    # under load (stophooks.go:62), silently skipping the end-of-turn gate.
    'Stop': [{'type': 'command', 'command': 'python3 ~/.gemini/config/hardtruth_hook.py stop', 'timeout': 300}]
}
with open(p, 'w') as f:
    json.dump(data, f, indent=2)
"
    echo "✓ Antigravity configured with HardTruth Stop and PostToolUse hooks."
fi

# 2b. Goose plugin (auto-detected; no-op when goose is absent)
bash "$REPO_ROOT/install_goose_plugin.sh"

# 3. Global Git Hooks Configuration
cat > "$GLOBAL_HOOKS_DIR/pre-commit" << 'EOF'
#!/bin/bash
# HardTruth Global Pre-Commit Gate

# If a previous hooks path was configured, chain and execute its pre-commit hook first
PREV_HOOKS_FILE="$HOME/.hardtruth/previous_hooksPath"
if [ -f "$PREV_HOOKS_FILE" ]; then
    PREV_DIR="$(cat "$PREV_HOOKS_FILE" | tr -d '\r\n')"
    if [ -n "$PREV_DIR" ] && [ -x "$PREV_DIR/pre-commit" ]; then
        "$PREV_DIR/pre-commit" "$@"
        PREV_EC=$?
        if [ $PREV_EC -ne 0 ]; then
            exit $PREV_EC
        fi
    fi
fi

python3 -c "
import sys, os, subprocess, re
sys.path.insert(0, os.path.expanduser('~/.hardtruth/lib'))
from hardtruth.ast_checker import check_ast_stubs

try:
    files = subprocess.check_output(
        ['git', 'diff', '--cached', '--name-only', '--diff-filter=ACM'], text=True).splitlines()
except Exception:
    sys.exit(0)

violations = []
for f in files:
    if os.path.isfile(f) and f.endswith(('.py', '.ts', '.js', '.rs', '.go')):
        try:
            diff_out = subprocess.check_output(
                ['git', 'diff', '--cached', '-U0', '--', f], text=True)
            changed_lines = set()
            for line in diff_out.splitlines():
                if line.startswith('@@'):
                    m = re.search(r'\+(\d+)(?:,(\d+))?', line)
                    if m:
                        start = int(m.group(1))
                        cnt = int(m.group(2)) if m.group(2) is not None else 1
                        for ln in range(start, start + max(cnt, 1)):
                            changed_lines.add(ln)
            violations.extend(check_ast_stubs(f, modified_lines=changed_lines if changed_lines else None))
        except Exception:
            violations.extend(check_ast_stubs(f))

if violations:
    print('🚨 HARDTRUTH COMMIT GATE REJECTED: Code contains stubs.')
    for v in violations:
        print(f'   - {v}')
    sys.exit(1)
"
EOF
chmod +x "$GLOBAL_HOOKS_DIR/pre-commit"
echo "✓ Pre-commit hook written to $GLOBAL_HOOKS_DIR/pre-commit"

# 4. Claude Code Configuration (if ~/.claude exists)
if [ -d "$HOME/.claude" ]; then
    echo "Configuring Claude Code global hooks..."
    CLAUDE_SETTINGS="$HOME/.claude/settings.json"
    if [ -f "$CLAUDE_SETTINGS" ]; then
        cp "$CLAUDE_SETTINGS" "$CLAUDE_SETTINGS.bak.$(date +%s)"
    else
        echo "{}" > "$CLAUDE_SETTINGS"
    fi
    # Safely merge or write HardTruth hooks for Claude Code without destroying existing hooks
    python3 -c "
import json, os
p = os.path.expanduser('~/.claude/settings.json')
try:
    with open(p, 'r') as f:
        data = json.load(f)
except Exception:
    data = {}
hooks = data.setdefault('hooks', {})
post_hooks = hooks.setdefault('PostToolUse', [])
stop_hooks = hooks.setdefault('Stop', [])

ht_post = {'matcher': '*', 'hooks': [{'type': 'command', 'command': 'HARDTRUTH_HARNESS=claude_code python3 ~/.hardtruth/lib/hardtruth_hook.py post_tool', 'timeout': 20}]}
ht_stop = {'matcher': '*', 'hooks': [{'type': 'command', 'command': 'HARDTRUTH_HARNESS=claude_code python3 ~/.hardtruth/lib/hardtruth_hook.py stop', 'timeout': 90}]}

if not any('hardtruth_hook.py' in str(h) for h in post_hooks):
    post_hooks.append(ht_post)
if not any('hardtruth_hook.py' in str(h) for h in stop_hooks):
    stop_hooks.append(ht_stop)

with open(p, 'w') as f:
    json.dump(data, f, indent=2)
"
    echo "✓ Claude Code configured with HardTruth hooks."
fi

# Parse CLI flags for opt-in global git hook installation
INSTALL_GLOBAL_HOOK=false
for arg in "$@"; do
    if [ "$arg" == "--global" ] || [ "$arg" == "--install-global-hook" ]; then
        INSTALL_GLOBAL_HOOK=true
    fi
done

# If not passed via flag and running interactively, prompt user with clear warning
if [ "$INSTALL_GLOBAL_HOOK" = false ] && [ -t 0 ]; then
    echo ""
    echo "⚠️  WARNING: Setting core.hooksPath overrides git hooks for EVERY repository on this machine."
    echo "   (HardTruth chains previously configured global hooks, but per-repo .git/hooks may be superseded)."
    read -p "Install machine-wide git hook via 'git config --global core.hooksPath'? [y/N]: " choice
    if [[ "$choice" =~ ^[Yy]$ ]]; then
        INSTALL_GLOBAL_HOOK=true
    fi
fi

if [ "$INSTALL_GLOBAL_HOOK" = true ]; then
    CURRENT_HOOKS="$(git config --global --get core.hooksPath 2>/dev/null || true)"
    if [ -n "$CURRENT_HOOKS" ] && [ "$CURRENT_HOOKS" != "$GLOBAL_HOOKS_DIR" ]; then
        echo "⚠️  WARNING: An existing global core.hooksPath is set: $CURRENT_HOOKS"
        echo "   Backing up previous hooks path to $HARDTRUTH_DIR/previous_hooksPath"
        echo "$CURRENT_HOOKS" > "$HARDTRUTH_DIR/previous_hooksPath"
    fi
    git config --global core.hooksPath "$GLOBAL_HOOKS_DIR"
    echo "✓ Enabled machine-wide git commit gate: git config --global core.hooksPath $GLOBAL_HOOKS_DIR"
else
    echo "ℹ️  Global git hooksPath not modified."
    echo "   To enable machine-wide git commit protection, run:"
    echo "     bash install.sh --global"
fi

echo "=========================================================="
echo "✓ HardTruth installed successfully."
echo "  Run 'python3 tests/test_live.py' to verify execution."
echo "=========================================================="
