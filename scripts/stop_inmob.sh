#!/usr/bin/env bash
PIDFILE="/mnt/d/empresas/Statetty/desarrollo/.run/inmobiliarias_scz.pid"
if [ ! -f "$PIDFILE" ]; then
  echo "No pid file $PIDFILE"
  exit 1
fi
PGID=$(cat "$PIDFILE")
if kill -0 -$PGID 2>/dev/null; then
  kill -TERM -- -$PGID
  sleep 3
  if kill -0 -$PGID 2>/dev/null; then
    kill -KILL -- -$PGID
  fi
fi
rm -f "$PIDFILE"
