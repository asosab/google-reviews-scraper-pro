#!/bin/bash
# service.sh — inicia/detiene procesos del pipeline googleReviews (estilo pyPlay).
#
# Uso:
#   ./service.sh start [--] <comando...>   inicia el proceso en segundo plano
#   ./service.sh stop                      detiene el proceso
#   ./service.sh restart                   reinicia con el último comando usado
#   ./service.sh status                    estado + último comando
#   ./service.sh logs                      sigue el log (tail -f)
#   ./service.sh investigacion [--] <args> lanza una investigación completa (ver scripts/investigar.py)
#
# El comando se ejecuta con cwd = esta carpeta y el .venv en el PATH
# (por eso basta `python start.py ...` o `python scripts/rasterize.py ...`).
# Para scraping con Chrome, antepón `xvfb-run -a -s "-screen 0 1600x1200x24"`.

if grep -qi microsoft /proc/version 2>/dev/null; then
    APP_DIR="/mnt/d/empresas/Statetty/desarrollo/googleReviews"   # WSL local
else
    APP_DIR="/opt/statetty/googleReviews"                          # VPS Contabo
fi
PID_FILE="$APP_DIR/googleReviews.pid"
CMD_FILE="$APP_DIR/googleReviews.cmd"
LOG_FILE="$APP_DIR/googleReviews.log"

start() {
    if [ $# -eq 0 ]; then
        echo "Falta el comando. Uso: $0 start [--] <comando...>"
        return 1
    fi

    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "googleReviews ya está ejecutándose (PID $PID)"
            return 0
        fi
        rm -f "$PID_FILE"
    fi

    echo "Iniciando googleReviews: $*"

    cd "$APP_DIR" || exit 1
    export PATH="$APP_DIR/.venv/bin:$PATH"

    setsid "$@" >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    printf '%s\n' "$@" > "$CMD_FILE"

    sleep 2

    if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "googleReviews iniciado correctamente (PID $(cat "$PID_FILE")). Log: $LOG_FILE"
    else
        echo "No se pudo iniciar googleReviews. Ver log: $LOG_FILE"
        rm -f "$PID_FILE"
    fi
}

stop() {
    if [ ! -f "$PID_FILE" ]; then
        echo "googleReviews no está en ejecución."
        return 0
    fi

    PID=$(cat "$PID_FILE")
    echo "Deteniendo googleReviews (PID $PID)..."

    kill -- -"$PID" 2>/dev/null || kill "$PID" 2>/dev/null

    sleep 2

    if kill -0 "$PID" 2>/dev/null; then
        echo "No respondió, forzando cierre..."
        kill -9 -- -"$PID" 2>/dev/null || kill -9 "$PID" 2>/dev/null
    fi

    rm -f "$PID_FILE"
    echo "googleReviews detenido."
}

status() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "googleReviews en ejecución (PID $(cat "$PID_FILE"))"
        [ -f "$CMD_FILE" ] && echo "Comando: $(tr '\n' ' ' < "$CMD_FILE")"
    else
        echo "googleReviews detenido."
    fi
}

restart() {
    if [ ! -f "$CMD_FILE" ]; then
        echo "No hay comando guardado; usa start."
        return 1
    fi
    stop
    sleep 1
    mapfile -t CMD < "$CMD_FILE"
    start "${CMD[@]}"
}

logs() {
    tail -f "$LOG_FILE"
}

case "$1" in
    start)
        shift
        [ "$1" = "--" ] && shift
        start "$@"
        ;;
    investigacion)
        shift
        start python scripts/investigar.py "$@"
        ;;
    stop)
        stop
        ;;
    restart)
        restart
        ;;
    status)
        status
        ;;
    logs)
        logs
        ;;
    *)
        echo "Uso: $0 {start [--] <comando...>|investigacion [--] <args>|stop|restart|status|logs}"
        exit 1
        ;;
esac
