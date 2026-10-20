#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
ENV_FILE="$ROOT/local/.env"
PYTHON="${PULSEFEED_PYTHON:-python3}"
fail() { printf '%s\n' "$*" >&2; exit 1; }
load_env() {
  [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail 'Run ./local/control.sh init-env first (local/.env must be a regular file).'
  "$PYTHON" - "$ENV_FILE" <<'PY'
import os, stat, sys
if stat.S_IMODE(os.stat(sys.argv[1]).st_mode) & 0o077:
    raise SystemExit('local/.env must be private: chmod 600 local/.env')
PY
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in ''|'#'*) continue ;; esac
    [[ "$line" == *=* ]] || fail 'Invalid local/.env line; expected raw KEY=value.'
    name="${line%%=*}"; value="${line#*=}"
    case "$name" in
      LOCAL_WORKER_TOKEN|LOCAL_RESULT_CAPACITY|SESSION_SECRET|ADMIN_ACCESS_PARAM|ADMIN_ACCESS_TOKEN|ADMIN_SESSION_SECONDS|PUBLIC_ACCESS_ENABLED|VISITOR_LIMIT_PER_MINUTE|HF_TOKEN|LOCAL_QWEN_URL|LOCAL_FLUX_URL|APP_BASE_URL|DATABASE_PATH|APP_PORT|QWEN_CACHE_PATH|FLUX_CACHE_PATH|QWEN_CACHE_OFFLINE|FLUX_CACHE_OFFLINE) export "$name=$value" ;;
      *) fail 'Unexpected local/.env key; see local/.env.example.' ;;
    esac
  done < "$ENV_FILE"
  [[ ${#LOCAL_WORKER_TOKEN} -ge 32 && "$LOCAL_WORKER_TOKEN" != GENERATED_BY_INIT_ENV ]] || fail 'LOCAL_WORKER_TOKEN must contain at least 32 characters.'
  [[ ${#SESSION_SECRET} -ge 32 && "$SESSION_SECRET" != GENERATED_BY_INIT_ENV ]] || fail 'SESSION_SECRET must contain at least 32 characters.'
}
compose() { docker compose --env-file "$ENV_FILE" -f "$ROOT/local/compose.yaml" "$@"; }
case "${1:-help}" in
  init-env)
    "$PYTHON" - "$ROOT/local/.env.example" "$ENV_FILE" <<'PY'
import os, secrets, sys
text = open(sys.argv[1]).read()
text = text.replace('LOCAL_WORKER_TOKEN=GENERATED_BY_INIT_ENV', 'LOCAL_WORKER_TOKEN=' + secrets.token_urlsafe(48))
text = text.replace('SESSION_SECRET=GENERATED_BY_INIT_ENV', 'SESSION_SECRET=' + secrets.token_urlsafe(48))
text = text.replace('ADMIN_ACCESS_TOKEN=GENERATED_BY_INIT_ENV', 'ADMIN_ACCESS_TOKEN=' + secrets.token_urlsafe(48))
try:
    fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    raise SystemExit('local/.env already exists; preserving its secrets.')
with os.fdopen(fd, 'w') as stream:
    stream.write(text)
print('Created private local/.env. Edit HF_TOKEN privately if model access requires it.')
PY
    ;;
  preflight)
    [[ "$(uname -s)" == Linux ]] || fail 'Spark preflight requires Linux.'
    case "$(uname -m)" in aarch64|arm64) ;; *) fail 'Spark builds require native ARM64; no x86 emulation.' ;; esac
    command -v docker >/dev/null || fail 'Docker is required.'
    docker info --format 'Docker server: {{.ServerVersion}}'
    docker compose version --short
    command -v nvidia-smi >/dev/null || fail 'NVIDIA driver tools are required.'
    nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
    command -v nvidia-ctk >/dev/null || fail 'NVIDIA Container Toolkit (nvidia-ctk) is required.'
    nvidia-ctk --version
    load_env; compose config --quiet
    printf '%s\n' 'Preflight passed. GPU kernels, available unified memory and image compatibility still require the real smoke test.'
    ;;
  config) load_env; compose config --quiet ;;
  build)
    load_env
    [[ "$(uname -s)" == Linux && "$(uname -m)" == aarch64 ]] || fail 'Build natively on the Spark (Linux aarch64).'
    # Qwen's vendor base needs explicit, reviewed corrections. Prepare only its
    # allowlisted inputs in a private directory; never send the checkout's .env or
    # model caches to Docker. Remove temporary archives even when a build fails.
    QWEN_PREPARED_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/pulsefeed-qwen-build.XXXXXXXX")"
    trap 'rm -rf -- "$QWEN_PREPARED_ROOT"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    "$PYTHON" "$ROOT/workers/qwen/prepare_build.py" --platform arm64 \
      --output-dir "$QWEN_PREPARED_ROOT/qwen"
    export QWEN_BUILD_CONTEXT="$QWEN_PREPARED_ROOT/qwen"
    compose build
    ;;
  prefetch)
    [[ $# -le 2 ]] || fail 'Usage: ./local/control.sh prefetch [qwen|flux|all]'
    target="${2:-all}"
    case "$target" in qwen|flux|all) ;; *) fail 'Choose qwen, flux, or all for prefetch.' ;; esac
    load_env
    [[ "$(uname -s)" == Linux && "$(uname -m)" == aarch64 ]] || fail 'Prefetch uses the final native Spark images (Linux aarch64).'
    stages=("$target")
    if [[ "$target" == all ]]; then stages=(qwen flux); fi
    for stage in "${stages[@]}"; do
      # Resolve only this service's image: `config --images flux` also lists its
      # dependency. Parse privately; never print the secret-bearing config JSON.
      image="$(compose config --format json "$stage" | "$PYTHON" -c 'import json,sys; config=json.load(sys.stdin); stage=sys.argv[1]; print(config["services"][stage].get("image") or config["name"]+"-"+stage)' "$stage")"
      [[ -n "$image" && "$image" != *$'\n'* ]] || fail 'Cannot identify the built worker image; run build first.'
      docker image inspect --format '{{.Id}}' "$image" >/dev/null || fail 'Worker image is not built locally; run build first.'
      compose run --rm --no-deps -T --pull never \
        -e HF_HUB_DOWNLOAD_TIMEOUT=300 \
        -v "$ROOT/local/prefetch_models.py:/gateway/prefetch_models.py:ro" \
        --entrypoint python3 "$stage" /gateway/prefetch_models.py "$stage"
    done
    ;;
  start) load_env; compose up -d --no-build --pull never ;;
  status) load_env; compose ps ;;
  stop) load_env; compose down ;;
  smoke) shift; load_env; exec "$PYTHON" "$ROOT/local/smoke.py" --real-gpu "$@" ;;
  laptop|cli|admin-url)
    action="$1"; shift; load_env
    export INFERENCE_PROVIDER=local_http PULSEFEED_FAKE_MODE=false COOKIE_SECURE=false
    export PYTHONPATH="$ROOT/app${PYTHONPATH:+:$PYTHONPATH}"
    if [[ "$DATABASE_PATH" != /* ]]; then export DATABASE_PATH="$ROOT/$DATABASE_PATH"; fi
    # Operator-only output. Do not call this command in shared logs or screenshots.
    if [[ "$action" == admin-url ]]; then
      exec "$PYTHON" -c 'from urllib.parse import urlencode; from pulsefeed.config import Config; c=Config.from_env(); assert c.admin_access_token, "ADMIN_ACCESS_TOKEN is not configured"; print(c.base_url + "/?" + urlencode({c.admin_access_param: c.admin_access_token}))'
    fi
    if [[ "$action" == cli ]]; then exec "$PYTHON" -m pulsefeed.cli "$@"; fi
    exec "$PYTHON" -m uvicorn pulsefeed.main:create_app --factory --host 127.0.0.1 --port "${APP_PORT:-8000}" --workers 1 --no-access-log
    ;;
  *) printf '%s\n' 'Usage: ./local/control.sh {init-env|preflight|config|build|prefetch [qwen|flux|all]|start|status|stop|smoke|laptop|admin-url|cli ...}' ;;
esac
