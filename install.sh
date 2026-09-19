#!/bin/bash
set -e

echo "=========================================================="
echo "  HardTruth: Autonomous Anti-Hallucination & Truth Shield"
echo "=========================================================="

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK_SRC="$REPO_ROOT/client/hardtruth_hook.py"

# 1. Antigravity Configuration
ANTIGRAVITY_CONFIG_DIR="$HOME/.gemini/config"
if [ -d "$HOME/.gemini" ]; then
    echo "Configuring Antigravity global hooks..."
    mkdir -p "$ANTIGRAVITY_CONFIG_DIR"
    cp "$HOOK_SRC" "$ANTIGRAVITY_CONFIG_DIR/hardtruth_hook.py"
    chmod +x "$ANTIGRAVITY_CONFIG_DIR/hardtruth_hook.py"
    
    cat > "$ANTIGRAVITY_CONFIG_DIR/hooks.json" << 'EOF'
{
  "hardtruth": {
    "enabled": true,
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.gemini/config/hardtruth_hook.py post_tool",
            "timeout": 5
          }
        ]
      }
    ],
    "Stop": [
      {
        "type": "command",
        "command": "python3 ~/.gemini/config/hardtruth_hook.py stop",
        "timeout": 10
      }
    ]
  }
}
EOF
    echo "✓ Antigravity configured with HardTruth Stop and PostToolUse hooks."
fi

# 2. Global Git Hooks Configuration
GLOBAL_HOOKS_DIR="$HOME/.hardtruth/hooks"
mkdir -p "$GLOBAL_HOOKS_DIR"
cat > "$GLOBAL_HOOKS_DIR/pre-commit" << EOF
#!/bin/bash
# HardTruth Global Pre-Commit Gate
python3 -c "
import sys, os
sys.path.insert(0, '${REPO_ROOT}')
from client.hardtruth_client import HardTruthClient
client = HardTruthClient()
# Check all staged python files for vacuous stubs
import subprocess
files = subprocess.check_output(['git', 'diff', '--cached', '--name-only'], text=True).splitlines()
stubs = []
for f in files:
    if f.endswith('.py') and os.path.exists(f):
        stubs.extend(client.check_ast_stubs(f))
if stubs:
    print('🚨 HardTruth rejected commit: Unimplemented dummy stub detected:', stubs[0], file=sys.stderr)
    sys.exit(1)
"
EOF
chmod +x "$GLOBAL_HOOKS_DIR/pre-commit"
git config --global core.hooksPath "$GLOBAL_HOOKS_DIR"
echo "✓ Configured global Git pre-commit hook in $GLOBAL_HOOKS_DIR."

echo "=========================================================="
echo "✓ HardTruth installed successfully."
echo "  Run 'python3 tests/test_live.py' to verify execution."
echo "=========================================================="
