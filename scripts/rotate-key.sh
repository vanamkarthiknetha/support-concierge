#!/usr/bin/env bash
# Swap the Gemini API key in .env without touching anything else.
#
#   bash scripts/rotate-key.sh AIzaSy...new-key
#
# Get a new key at https://aistudio.google.com/apikey (revoke the old one there
# too -- rotating locally does nothing if the old key is still live).
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
env_file="$root/.env"
new="${1:-}"

if [ -z "$new" ]; then
  echo "usage: bash scripts/rotate-key.sh <new-key>"
  echo
  echo "1. Create a new key:  https://aistudio.google.com/apikey"
  echo "2. DELETE the old one in that same console -- this script only updates"
  echo "   your local .env, it cannot revoke anything."
  exit 1
fi

[ -f "$env_file" ] || { echo "no .env at $env_file"; exit 1; }

cp "$env_file" "$env_file.bak"
# Both names are set: google-genai reads GEMINI_API_KEY, langchain reads GOOGLE_API_KEY.
sed -i.tmp -E "s|^(GEMINI_API_KEY|GOOGLE_API_KEY)=.*|\1=${new}|" "$env_file"
rm -f "$env_file.tmp"

echo "updated GEMINI_API_KEY and GOOGLE_API_KEY in .env (backup: .env.bak)"
echo
echo "verify it works:"
echo "  cd backend && uv run python scripts/smoke_test.py"
echo
echo "Reminder: revoke the OLD key at https://aistudio.google.com/apikey"
