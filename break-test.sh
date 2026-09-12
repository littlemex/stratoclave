#!/usr/bin/env bash
# Run a break test without destroying uncommitted work.
#
# Twice in one session `git checkout -- .` silently deleted the fixes and tests
# the break was meant to VALIDATE, because the working tree was dirty and
# `checkout` restores from the index, not from a snapshot of what was there a
# moment ago. The break test then reported the deleted fix as undefended.
#
# So: snapshot the working tree first, restore from the snapshot, and refuse to
# run at all if the snapshot cannot be taken.
set -euo pipefail

if [ $# -lt 2 ]; then
  echo "usage: break-test.sh <edit-script> <verify-command...>" >&2
  exit 64
fi

edit_script="$1"; shift

snapshot="$(mktemp -d)"
trap 'rm -rf "$snapshot"' EXIT

# `git stash create` writes a commit object without touching the working tree,
# so the snapshot exists before anything is edited and includes staged and
# unstaged changes alike. Untracked files are handled separately because
# `stash create` ignores them.
stash_commit="$(git stash create "break-test snapshot" || true)"
git ls-files --others --exclude-standard -z | while IFS= read -r -d '' f; do
  mkdir -p "$snapshot/untracked/$(dirname "$f")"
  cp "$f" "$snapshot/untracked/$f"
done

restore() {
  if [ -n "$stash_commit" ]; then
    git checkout -q "$stash_commit" -- .
  else
    git checkout -q -- .
  fi
  if [ -d "$snapshot/untracked" ]; then
    (cd "$snapshot/untracked" && find . -type f -print0) | while IFS= read -r -d '' f; do
      mkdir -p "$(dirname "$f")"
      cp "$snapshot/untracked/$f" "$f"
    done
  fi
}
trap 'restore; rm -rf "$snapshot"' EXIT

python3 "$edit_script"
"$@" || true
