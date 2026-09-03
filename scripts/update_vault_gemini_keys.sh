#!/usr/bin/env bash
# update_vault_gemini_keys.sh
# Usage: VAULT_TOKEN=ghp_xxx GEMINI_KEYS="key1,key2" ./scripts/update_vault_gemini_keys.sh
# This script clones the private vault repo, writes secrets/gemini_keys.txt (one per line), commits and pushes.
set -euo pipefail
if [ -z "${VAULT_TOKEN:-}" ]; then
  echo "ERROR: VAULT_TOKEN environment variable must be set (a fine-grained PAT with repo access to the vault)."
  exit 1
fi
if [ -z "${GEMINI_KEYS:-}" ]; then
  echo "ERROR: GEMINI_KEYS environment variable must be set (comma-separated keys)."
  exit 1
fi
VAULT_URL="https://github.com/dubbishpsychic0000/ai-job-finder-vault.git"
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT
echo "Cloning vault into $TMPDIR"
git clone --depth 1 "https://x-access-token:${VAULT_TOKEN}@${VAULT_URL#https://}" "$TMPDIR/vault"
cd "$TMPDIR/vault"
mkdir -p secrets
# Normalize keys: split on comma, one per line, strip CRLF
echo "Writing secrets/gemini_keys.txt"
{ IFS=','; for k in $GEMINI_KEYS; do echo "$k"; done } | sed 's/\r$//' > secrets/gemini_keys.txt
chmod 600 secrets/gemini_keys.txt

git add secrets/gemini_keys.txt
if git diff --staged --quiet; then
  echo "No changes to commit (keys identical)."
else
  git commit -m "vault: update gemini keys (added via helper)"
  git push origin HEAD:main
  echo "Pushed updated gemini_keys.txt to vault."
fi

echo "Done. Please revoke any keys exposed in chat and rotate them if needed."