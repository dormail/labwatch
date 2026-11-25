#!/usr/bin/env bash
# Pre-commit guard: refuse to commit things that look like secrets or site-specific
# config. Enable it in your clone with:
#
#   ln -sf ../../scripts/check-secrets.sh .git/hooks/pre-commit
#
# Or run it by hand before committing:  scripts/check-secrets.sh
set -euo pipefail

fail=0
staged=$(git diff --cached --name-only --diff-filter=ACM)

for f in $staged; do
  case "$f" in
    .env|config.yaml|*.sqlite|*[Kk]ey*.txt)
      echo "refusing to commit $f (secrets / local config)"; fail=1 ;;
  esac
done

patterns=(
  'discord(app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]{20,}'      # Discord webhook
  '[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}'                               # Telegram bot token
  '^\+[[:space:]]*api_key:[[:space:]]*[0-9a-f]{8}-[0-9a-f]{4}-'     # literal API key in YAML
  'hc-ping\.com/[0-9a-f-]{30,}'                                    # healthchecks ping URL
  '^\+[[:space:]]*password:[[:space:]]*[^$[:space:]]'              # literal password in YAML
)
diff=$(git diff --cached -U0 -- . ':(exclude)scripts/check-secrets.sh' | grep '^+' || true)
for p in "${patterns[@]}"; do
  if grep -qE "$p" <<<"$diff"; then
    echo "staged changes match secret pattern: $p"; fail=1
  fi
done

# Values from the local .env must never be committed.
if [[ -f .env ]]; then
  while IFS='=' read -r k v; do
    [[ -z "$k" || "$k" == \#* ]] && continue
    v="${v%%#*}"; v="${v//[[:space:]]/}"; v="${v%\"}"; v="${v#\"}"
    [[ ${#v} -ge 8 ]] || continue
    if grep -qF -- "$v" <<<"$diff"; then
      echo "staged changes contain the value of $k from .env"; fail=1
    fi
  done < .env
fi

if [[ $fail -ne 0 ]]; then
  echo "commit blocked by scripts/check-secrets.sh (use --no-verify only if you are sure)"
  exit 1
fi
echo "check-secrets: ok"
