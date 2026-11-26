#!/usr/bin/env bash
# Smoke-проверка эндпоинтов Launch Control.
# Возвращает 0, только если все проверки прошли; иначе 1.
#
#   ./scripts/smoke.sh                       # образ launch-control:step4
#   ./scripts/smoke.sh launch-control:step4  # явно указать образ
#   SMOKE_PORT=18081 ./scripts/smoke.sh      # другой порт на хосте
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

IMAGE="${1:-launch-control:step4}"
PORT="${SMOKE_PORT:-18080}"
CONTAINER="launch-control-smoke-$$"
BASE="http://127.0.0.1:${PORT}"
BODY="/tmp/smoke-body.$$"

failed=0
total=0

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  rm -f "$BODY"
}
trap cleanup EXIT

check() { # check <описание> <код-возврата-условия>
  local desc="$1" rc="$2"
  total=$((total + 1))
  if [ "$rc" -eq 0 ]; then
    printf '  \033[32m✓\033[0m %s\n' "$desc"
  else
    printf '  \033[31m✗\033[0m %s\n' "$desc"
    failed=$((failed + 1))
  fi
}

http_code() { curl -s -o "$BODY" -w '%{http_code}' "$@"; }

json_get() { # json_get <файл> <python-выражение над объектом d>
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2], {"d": d}))' "$1" "$2" 2>/dev/null || echo "-1"
}

echo
echo "=== smoke: $IMAGE ==="
echo

if ! docker run -d --name "$CONTAINER" -p "127.0.0.1:${PORT}:8000" "$IMAGE" >/dev/null; then
  echo "  ✗ контейнер не запустился"
  exit 1
fi

ready=1
for _ in $(seq 1 60); do
  if [ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/health")" = "200" ]; then
    ready=0
    break
  fi
  sleep 0.5
done
check "сервис поднялся и отвечает на /health" "$ready"

code=$(http_code "$BASE/health")
check "GET /health -> 200 (получено: $code)" "$([ "$code" = "200" ] && echo 0 || echo 1)"

code=$(http_code "$BASE/api/v1/launches")
count=$(json_get "$BODY" 'len(d)')
check "GET /api/v1/launches -> 200, 5 объектов (код: $code, объектов: $count)" \
  "$([ "$code" = "200" ] && [ "$count" = "5" ] && echo 0 || echo 1)"

code=$(http_code -X POST "$BASE/api/v1/launches/LC-101/countdown")
tminus=$(json_get "$BODY" 'd.get("t_minus_seconds")')
check "POST /api/v1/launches/LC-101/countdown -> 200, t_minus_seconds=600 (код: $code, t_minus: $tminus)" \
  "$([ "$code" = "200" ] && [ "$tminus" = "600" ] && echo 0 || echo 1)"

code=$(http_code -X POST "$BASE/api/v1/launches/LC-102/countdown")
check "POST /api/v1/launches/LC-102/countdown -> 409 (получено: $code)" \
  "$([ "$code" = "409" ] && echo 0 || echo 1)"

echo
echo "  Готово: $((total - failed))/${total}"
echo

[ "$failed" -eq 0 ]
