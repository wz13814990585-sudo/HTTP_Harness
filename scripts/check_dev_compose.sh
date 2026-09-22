#!/usr/bin/env bash
set -euo pipefail

# Test-only values. Never use this script as a real-model or production demo.
export HNH_DB_PASSWORD='hnh_smoke_db_only'
export HNH_DEV_TOKEN='hnh_smoke_token_only'
export HNH_DEEPSEEK_API_KEY='hnh_smoke_not_a_real_key'
export HNH_DEEPSEEK_MODEL='deepseek-flash'
export HNH_DEEPSEEK_REASONING_EFFORT='high'
export HNH_HTTP_PORT="${HNH_DEV_SMOKE_PORT:-18080}"

project_name="hnh-smoke-$(date +%s)-$$"
export HNH_IMAGE_TAG="$project_name"
compose=(docker compose -p "$project_name" -f deploy/dev/compose.yaml)
created=0

cleanup() {
  if [[ "$created" == 1 ]]; then
    "${compose[@]}" down --volumes
  fi
}
trap cleanup EXIT

"${compose[@]}" config --quiet
if [[ -n "$("${compose[@]}" ps --all -q)" ]]; then
  echo 'refusing to reuse an existing Compose smoke project' >&2
  exit 2
fi

created=1
"${compose[@]}" up --build -d --wait --wait-timeout 120
curl --fail --silent --show-error "http://127.0.0.1:$HNH_HTTP_PORT/readyz" \
  | .venv/bin/python -c 'import json,sys; data=json.load(sys.stdin); assert data["core_ready"] is True and data["components"]["database"] == "ready" and data["components"]["model_configuration"] == "missing"'
curl --fail --silent --show-error --output /dev/null \
  -H "Authorization: Bearer $HNH_DEV_TOKEN" \
  "http://127.0.0.1:$HNH_HTTP_PORT/v1/capabilities"
echo '{"compose_smoke":"passed","model":"placeholder_not_called","mcp":"not_configured"}'
