#!/usr/bin/env bash
set -euo pipefail
visibility="${1:-}"
name="${2:-generation-orchestrator}"
if [[ "$visibility" != "--public" && "$visibility" != "--private" ]]; then
  echo 'Usage: ./scripts/publish.sh --public|--private [repository-name]' >&2
  exit 2
fi
if ! command -v gh >/dev/null 2>&1; then
  echo 'GitHub CLI (gh) is not installed. Install it, authenticate with gh auth login, then rerun.' >&2
  exit 2
fi
cd "$(dirname "$0")/.."
git rev-parse --is-inside-work-tree >/dev/null
if git remote get-url origin >/dev/null 2>&1; then
  echo 'origin already exists; refusing to replace an existing remote automatically.' >&2
  exit 2
fi
gh auth status
gh repo create "$name" "$visibility" --source=. --remote=origin --push
