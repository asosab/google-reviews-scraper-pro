#!/bin/bash
# watch_then.sh <pid_file> -- <comando...>
#
# Espera a que el proceso del pid_file termine y ejecuta el comando.
# Uso (encadenar el sync de equipamientos a una investigación):
#   mkdir -p .run
#   setsid nohup scripts/watch_then.sh googleReviews.pid -- \
#     .venv/bin/python scripts/equipamientos_sync.py --investigacion "X" \
#     --categoria aparcamiento > .run/watch.log 2>&1 &
#   echo $! > .run/watch.pid
PID_FILE="$1"; shift
[ "$1" = "--" ] && shift
if [ -z "$PID_FILE" ] || [ $# -eq 0 ]; then
    echo "Uso: $0 <pid_file> -- <comando...>" >&2
    exit 1
fi
echo "watch_then: esperando PID de $PID_FILE antes de correr: $*"
while [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; do
    sleep 60
done
echo "watch_then: proceso terminado ($(date -Iseconds)), ejecutando: $*"
exec "$@"
