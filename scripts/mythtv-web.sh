#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="$PROJECT_DIR/.run"
PID_FILE="$RUN_DIR/gunicorn.pid"
GUNICORN="$PROJECT_DIR/.venv/bin/gunicorn"
PYTHON="$PROJECT_DIR/.venv/bin/python"
LOG_RUNNER="$SCRIPT_DIR/daily_gunicorn.py"
APP_TARGET='mythtv_web:create_app()'

pid_is_managed() {
    local pid="$1"
    local command_line

    command_line="$(ps -ww -p "$pid" -o args= 2>/dev/null || true)"
    [[ "$command_line" == *"$GUNICORN"* && "$command_line" == *"$PID_FILE"* && "$command_line" == *"$APP_TARGET"* ]]
}

read_pid() {
    [[ -f "$PID_FILE" ]] || return 1
    local pid
    pid="$(<"$PID_FILE")"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "$pid"
}

start_server() {
    if [[ ! -x "$GUNICORN" ]]; then
        printf 'Gunicorn not found at %s; install requirements in .venv first.\n' "$GUNICORN" >&2
        return 1
    fi
    if [[ ! -x "$PYTHON" ]]; then
        printf 'Python not found at %s; create the project .venv first.\n' "$PYTHON" >&2
        return 1
    fi

    mkdir -p "$RUN_DIR"
    if pid="$(read_pid)" && kill -0 "$pid" 2>/dev/null; then
        if pid_is_managed "$pid"; then
            printf 'MythTV web server is already running (pid %s).\n' "$pid"
            return 0
        fi
        printf 'Refusing to use PID file: process %s is not this Gunicorn app.\n' "$pid" >&2
        return 1
    fi
    rm -f "$PID_FILE"

    cd "$PROJECT_DIR"
    nohup "$PYTHON" "$LOG_RUNNER" >>/dev/null 2>&1 </dev/null &
    local launch_pid=$!

    for attempt in {1..50}; do
        if pid="$(read_pid)" && kill -0 "$pid" 2>/dev/null && pid_is_managed "$pid"; then
            printf 'MythTV web server started (pid %s, listening on 127.0.0.1:6555).\n' "$pid"
            return 0
        fi
        if ! kill -0 "$launch_pid" 2>/dev/null; then
            printf 'Gunicorn exited during startup. Check dated logs under %s.\n' "$RUN_DIR" >&2
            return 1
        fi
        sleep 0.1
    done

    printf 'Timed out waiting for Gunicorn to start. Check dated logs under %s.\n' "$RUN_DIR" >&2
    return 1
}

stop_server() {
    local pid
    if ! pid="$(read_pid)"; then
        printf 'MythTV web server is not running.\n'
        return 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$PID_FILE"
        printf 'Removed stale PID file; server was not running.\n'
        return 0
    fi
    if ! pid_is_managed "$pid"; then
        printf 'Refusing to stop PID %s: it is not this Gunicorn app.\n' "$pid" >&2
        return 1
    fi

    kill -TERM "$pid"
    for attempt in {1..100}; do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$PID_FILE"
            printf 'MythTV web server stopped.\n'
            return 0
        fi
        sleep 0.1
    done

    printf 'Gunicorn (pid %s) did not stop within 10 seconds.\n' "$pid" >&2
    return 1
}

show_status() {
    local pid
    if pid="$(read_pid)" && kill -0 "$pid" 2>/dev/null && pid_is_managed "$pid"; then
        printf 'MythTV web server is running (pid %s).\n' "$pid"
        return 0
    fi
    printf 'MythTV web server is not running.\n'
}

case "${1:-}" in
    start)
        start_server
        ;;
    stop)
        stop_server
        ;;
    restart)
        stop_server
        start_server
        ;;
    status)
        show_status
        ;;
    *)
        printf 'Usage: %s {start|stop|restart|status}\n' "${0##*/}" >&2
        exit 2
        ;;
esac
