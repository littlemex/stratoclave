#!/usr/bin/env bash
# install-git-hooks.sh — optional helper to install local pre-commit hook
# that runs the shape-based secret scan before every commit.
#
# Usage:
#   ./scripts/install-git-hooks.sh
#
# To skip the hook for a single commit:
#   git commit --no-verify ...
#
# To uninstall:
#   rm .git/hooks/pre-commit

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$ROOT/.git/hooks/pre-commit"

if [[ ! -d "$ROOT/.git" ]]; then
  echo "[ERROR] not a git worktree: $ROOT" >&2
  exit 1
fi

cat > "$HOOK" <<'HOOK_BODY'
#!/usr/bin/env bash
# Stratoclave pre-commit hook: block hard-coded deployment identifiers.
set -e
SCRIPT="$(git rev-parse --show-toplevel)/scripts/check-no-hardcoded-secrets.sh"
if [[ -x "$SCRIPT" ]]; then
  "$SCRIPT"
fi
HOOK_BODY

chmod +x "$HOOK"
echo "[OK] pre-commit hook installed at $HOOK"
echo "     Runs: scripts/check-no-hardcoded-secrets.sh before every commit."
echo "     Bypass with: git commit --no-verify"
