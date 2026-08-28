#!/usr/bin/env bash
# One-time: wire the secret scanner in as a pre-commit hook.
set -euo pipefail
root="$(git rev-parse --show-toplevel)"
mkdir -p "$root/.git/hooks"
cat > "$root/.git/hooks/pre-commit" <<'HOOK'
#!/usr/bin/env bash
exec "$(git rev-parse --show-toplevel)/scripts/check-secrets.sh" staged
HOOK
chmod +x "$root/.git/hooks/pre-commit" "$root/scripts/check-secrets.sh"
echo "pre-commit hook installed -> scripts/check-secrets.sh"
