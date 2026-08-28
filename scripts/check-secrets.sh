#!/usr/bin/env bash
# Refuse to commit anything that looks like a live credential.
#
# Installed as a pre-commit hook by scripts/install-hooks.sh, and run in CI over
# the full history. It scans STAGED CONTENT, not files on disk, so a key can't
# slip through in a partially-staged file.
set -uo pipefail

RED=$'\033[31m'; GRN=$'\033[32m'; RST=$'\033[0m'

# Provider key shapes. Deliberately broad: a false positive costs one --no-verify,
# a false negative costs a live key in a public repo.
PATTERNS=(
  'AIza[0-9A-Za-z_-]{35}'                    # Google / Gemini
  'sk-ant-[0-9A-Za-z_-]{20,}'                # Anthropic
  'sk-[0-9A-Za-z]{32,}'                      # OpenAI
  'gh[pousr]_[0-9A-Za-z]{36,}'               # GitHub
  'AKIA[0-9A-Z]{16}'                         # AWS access key id
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'       # any private key
)

mode="${1:-staged}"
found=0

scan() {                                     # scan <label> <content-producer>
  local label="$1"; shift
  local tmp; tmp="$(mktemp)"
  "$@" >"$tmp" 2>/dev/null || { rm -f "$tmp"; return 0; }
  for pat in "${PATTERNS[@]}"; do
    # `--` is required: the private-key pattern starts with dashes and grep would
    # otherwise parse it as flags. `-a` treats binary blobs as text instead of
    # choking on null bytes.
    if grep -Eaq -e "$pat" -- "$tmp"; then
      echo "${RED}SECRET DETECTED${RST} in ${label} (pattern: ${pat:0:28})"
      found=1
    fi
  done
  rm -f "$tmp"
}

if [ "$mode" = "history" ]; then
  # Every blob ever committed, not just the current tree.
  while read -r sha path; do
    [ -z "$sha" ] && continue
    scan "history: $path" git cat-file blob "$sha"
  done < <(
    git rev-list --all --objects |
      git cat-file --batch-check='%(objecttype) %(objectname) %(rest)' |
      awk '$1=="blob" && $3!="" {print $2, $3}'
  )
else
  # Staged content only. .env is gitignored, but a `git add -f` would bypass that.
  while read -r path; do
    [ -z "$path" ] && continue
    case "$path" in
      *.lock|*package-lock.json|*uv.lock) continue ;;   # hashes trip the broad patterns
    esac
    scan "staged: $path" git show ":$path"
  done < <(git diff --cached --name-only --diff-filter=ACM)

  if git diff --cached --name-only | grep -qE '(^|/)\.env$'; then
    echo "${RED}REFUSING${RST}: .env is staged. It holds real credentials."
    found=1
  fi
fi

if [ "$found" -ne 0 ]; then
  echo
  echo "Commit blocked. If this is a placeholder and not a live key, bypass with:"
  echo "  git commit --no-verify"
  exit 1
fi

echo "${GRN}no secrets detected${RST} ($mode)"
