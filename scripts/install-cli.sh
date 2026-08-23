#!/usr/bin/env bash
#
# One-line installer for the Stratoclave CLI.
#
#   curl -fsSL https://raw.githubusercontent.com/littlemex/stratoclave/main/scripts/install-cli.sh \
#     | STRATOCLAVE_URL=https://d111111abcdef8.cloudfront.net bash
#
# Give it only the deployment's CloudFront URL. It clones the repo, builds the
# Rust CLI, installs it as both `stratoclave` and the short alias `sclv`, adds
# the install dir to your PATH, and runs `stratoclave setup <url>` so the CLI is
# usable immediately (default model + codex model come from the deployment).
#
# Optional environment variables:
#   STRATOCLAVE_URL      (required) the CloudFront URL of the deployment.
#   STRATOCLAVE_BIN_DIR  install dir (default: ~/.local/bin).
#   STRATOCLAVE_REF      git ref to build (default: main).
set -euo pipefail

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YEL=$'\033[1;33m'; NC=$'\033[0m'
info() { printf '%s[stratoclave]%s %s\n' "$GREEN" "$NC" "$1"; }
warn() { printf '%s[stratoclave]%s %s\n' "$YEL" "$NC" "$1"; }
die()  { printf '%s[stratoclave]%s %s\n' "$RED" "$NC" "$1" >&2; exit 1; }

URL="${STRATOCLAVE_URL:-}"
[ -n "$URL" ] || die "Set STRATOCLAVE_URL to the deployment's CloudFront URL (e.g. https://xxxx.cloudfront.net)."
case "$URL" in https://*) ;; *) die "STRATOCLAVE_URL must start with https:// (got: $URL)";; esac

BIN_DIR="${STRATOCLAVE_BIN_DIR:-$HOME/.local/bin}"
REF="${STRATOCLAVE_REF:-main}"
REPO="https://github.com/littlemex/stratoclave.git"

command -v git >/dev/null || die "git is required."
command -v cargo >/dev/null || die "Rust (cargo) is required. Install from https://rustup.rs and re-run."

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
info "Cloning $REPO ($REF)…"
git clone --depth 1 --branch "$REF" "$REPO" "$WORK/stratoclave" >/dev/null 2>&1 \
  || git clone --depth 1 "$REPO" "$WORK/stratoclave" >/dev/null 2>&1 \
  || die "clone failed."

info "Building the CLI (cargo build --release)… this is the slow part, ~1-2 min."
( cd "$WORK/stratoclave/cli" && cargo build --release >/dev/null ) || die "cargo build failed."

mkdir -p "$BIN_DIR"
install -m 0755 "$WORK/stratoclave/cli/target/release/stratoclave" "$BIN_DIR/stratoclave"
ln -sf "$BIN_DIR/stratoclave" "$BIN_DIR/sclv"
info "Installed: $BIN_DIR/stratoclave and $BIN_DIR/sclv"

# Put BIN_DIR on PATH persistently for the user's shell, and for this session.
add_path_line="export PATH=\"$BIN_DIR:\$PATH\""
for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
  if [ -f "$rc" ] && ! grep -qF "$BIN_DIR" "$rc"; then
    printf '\n# added by stratoclave install-cli.sh\n%s\n' "$add_path_line" >> "$rc"
    info "Added $BIN_DIR to PATH in $rc"
  fi
done
export PATH="$BIN_DIR:$PATH"

info "Pointing the CLI at $URL (stratoclave setup)…"
"$BIN_DIR/stratoclave" setup "$URL" --force

info "Done. 'sclv' is ready."
warn "Open a new shell (or 'source ~/.zshrc') so 'sclv' is on PATH, then:"
printf '    sclv auth login --email you@example.com\n'
printf '    sclv claude -- "summarize this repo"\n'
