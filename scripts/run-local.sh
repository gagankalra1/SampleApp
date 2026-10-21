#!/usr/bin/env bash
#
# Runs the VoltMart services directly on your machine, without Docker.
#
#   ./scripts/run-local.sh          start everything
#   ./scripts/run-local.sh stop     stop everything
#   ./scripts/run-local.sh status   show what is running
#
# Use this when you are editing service code: it starts each service with
# uvicorn's reloader, so a save restarts just that service in about a second.
# `docker compose up` is the better choice for simply running the app.
#
# Everything binds to 127.0.0.1 and uses the in-memory storage backend, so
# there is nothing to clean up and no AWS account involved — except for the
# support agent, which needs Bedrock credentials in your environment.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO_ROOT"

VENV="${REPO_ROOT}/.venv"
RUN_DIR="${REPO_ROOT}/.build/local"
LOG_DIR="${RUN_DIR}/logs"

# service:port
SERVICES=(
  "catalog:8081"
  "inventory:8082"
  "cart:8083"
  "orders:8084"
  "payment:8085"
  "checkout:8086"
  "support-agent:8087"
)

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

port_of()    { printf '%s' "${1#*:}"; }
name_of()    { printf '%s' "${1%%:*}"; }
pid_file()   { printf '%s/%s.pid' "$RUN_DIR" "$1"; }

# ---------------------------------------------------------------------------
stop_all() {
  log "Stopping services"
  local stopped=0
  for entry in "${SERVICES[@]}"; do
    local name port file
    name=$(name_of "$entry")
    port=$(port_of "$entry")
    file=$(pid_file "$name")

    if [[ -f "$file" ]]; then
      local pid
      pid=$(cat "$file")
      if kill "$pid" 2>/dev/null; then
        info "stopped ${name} (pid ${pid})"
        stopped=$((stopped + 1))
      fi
      rm -f "$file"
    fi

    # Catch anything left holding the port — a previous run killed with
    # Ctrl-C, say, which never removed its pid file.
    if command -v lsof >/dev/null 2>&1; then
      local orphan
      orphan=$(lsof -ti "tcp:${port}" 2>/dev/null || true)
      if [[ -n "$orphan" ]]; then
        kill -9 "$orphan" 2>/dev/null && info "freed port ${port}"
      fi
    fi
  done
  [[ $stopped -eq 0 ]] && info "nothing was running"
  return 0
}

status_all() {
  log "VoltMart local services"
  for entry in "${SERVICES[@]}"; do
    local name port
    name=$(name_of "$entry")
    port=$(port_of "$entry")
    printf '    %-16s :%s  ' "$name" "$port"
    if curl --silent --fail --max-time 2 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
      printf '\033[1;32mup\033[0m\n'
    else
      printf '\033[1;31mdown\033[0m\n'
    fi
  done
  printf '\n    logs: %s\n\n' "$LOG_DIR"
}

# ---------------------------------------------------------------------------
case "${1:-start}" in
  stop)   stop_all; exit 0 ;;
  status) status_all; exit 0 ;;
  start)  ;;
  *)      die "unknown command: $1 (use start, stop or status)" ;;
esac

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
log "Preparing the Python environment"

PYTHON=""
for candidate in python3.12 python3.13 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then PYTHON="$candidate"; break; fi
done
[[ -n "$PYTHON" ]] || die "no suitable Python found (3.11 or newer required)"
info "using $("$PYTHON" --version)"

if [[ ! -d "$VENV" ]]; then
  info "creating ${VENV}"
  "$PYTHON" -m venv "$VENV"
fi

# The support agent's requirements are a superset of the others', so one
# install covers every service.
info "installing dependencies (first run takes a minute)"
"${VENV}/bin/pip" install --quiet --upgrade pip
"${VENV}/bin/pip" install --quiet -r services/support-agent/requirements.txt
"${VENV}/bin/pip" install --quiet -r services/orders/requirements.txt

mkdir -p "$RUN_DIR" "$LOG_DIR"
stop_all

# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------
log "Starting services"

export STORAGE_BACKEND=memory
export BIND_HOST=127.0.0.1
export LOG_LEVEL=info
export DEPLOYMENT_ENVIRONMENT=local
export OTEL_SERVICE_NAMESPACE=voltmart
export OTEL_AWS_XRAY_ID_GENERATOR=false
export CATALOG_URL=http://127.0.0.1:8081
export INVENTORY_URL=http://127.0.0.1:8082
export CART_URL=http://127.0.0.1:8083
export ORDERS_URL=http://127.0.0.1:8084
export PAYMENT_URL=http://127.0.0.1:8085
export CHECKOUT_URL=http://127.0.0.1:8086

# Export to a collector only if one is listening. Without this, every service
# logs connection-refused errors every few seconds and the real output is
# impossible to read.
if nc -z 127.0.0.1 4317 2>/dev/null; then
  export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4317
  export OTEL_EXPORTER_OTLP_INSECURE=true
  info "exporting telemetry to 127.0.0.1:4317"
else
  unset OTEL_EXPORTER_OTLP_ENDPOINT || true
  warn "nothing listening on 4317; spans will be recorded but not exported"
  warn "for a trace viewer: docker run --rm -p 16686:16686 -p 4317:4317 \\"
  warn "    -e COLLECTOR_OTLP_ENABLED=true jaegertracing/all-in-one:1.62.0"
fi

for entry in "${SERVICES[@]}"; do
  name=$(name_of "$entry")
  port=$(port_of "$entry")

  (
    cd "services/${name}" || exit 1
    # Both the service directory and the shared library, mirroring the
    # PYTHONPATH the container image sets up.
    export PYTHONPATH="${PWD}:${REPO_ROOT}/services/shared"
    export PORT="$port"
    export OTEL_SERVICE_NAME="voltmart-${name}"

    nohup "${VENV}/bin/python" -m uvicorn main:app \
      --host 127.0.0.1 --port "$port" \
      --reload --reload-dir . --reload-dir "${REPO_ROOT}/services/shared" \
      --no-access-log \
      > "${LOG_DIR}/${name}.log" 2>&1 &

    echo $! > "$(pid_file "$name")"
  )
  info "${name} starting on :${port}"
done

# ---------------------------------------------------------------------------
# Wait and report
# ---------------------------------------------------------------------------
log "Waiting for health checks"
all_ready=true
for entry in "${SERVICES[@]}"; do
  name=$(name_of "$entry")
  port=$(port_of "$entry")
  ready=false
  # The support agent imports a lot more than the others, so it needs longer.
  attempts=$([[ "$name" == "support-agent" ]] && echo 40 || echo 20)

  for _ in $(seq 1 "$attempts"); do
    if curl --silent --fail --max-time 2 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
      ready=true
      break
    fi
    sleep 1
  done

  if $ready; then
    printf '    \033[1;32m✓\033[0m %-16s http://127.0.0.1:%s\n' "$name" "$port"
  else
    printf '    \033[1;31m✗\033[0m %-16s check %s/%s.log\n' "$name" "$LOG_DIR" "$name"
    all_ready=false
  fi
done

log "Ready"
cat <<EOF
    API docs for any service:  http://127.0.0.1:8081/docs
    Test cards:                http://127.0.0.1:8085/test-cards
    Agent diagnostics:         http://127.0.0.1:8087/agent/diagnostics

    Start the storefront with hot reload:
      cd frontend && npm install && npm run dev
      then open http://127.0.0.1:5173

    Smoke test the whole purchase path:
      ./scripts/smoke-test.sh

    Stop everything:
      ./scripts/run-local.sh stop

EOF

$all_ready || exit 1
