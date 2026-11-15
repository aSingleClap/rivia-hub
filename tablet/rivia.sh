#!/data/data/com.termux/files/usr/bin/bash
# rivia.sh: run RIVIA's MCP server on the tablet (Termux).
#
# One-time setup (Termux):
#   bash ~/storage/shared/rivia/rivia.sh install-cli
#   (puts `rivia` on PATH as $PREFIX/bin/rivia). Or:
#   echo "alias rivia='bash ~/storage/shared/rivia/rivia.sh'" >> ~/.bashrc && source ~/.bashrc
#
# Then:
#   rivia start     start the server, the tunnel and the watchdog in the background
#   rivia stop      stop all three
#   rivia status    is it running, does it answer locally and at mcp.rivia.observer
#   rivia test      run both test files (do this after editing rivia_auth.py)
#   rivia sched selftest   run the planner checks (must pass after any planner change)
#   rivia sched google-login  sign in to Google Calendar (read-only) on the tablet
#   rivia sched events <day>  print primary-calendar events (today, tomorrow, weekday, or YYYY-MM-DD)
#   rivia logs      last lines of the server and tunnel logs
#   rivia regs      what clients tried to sign in (and which redirects were refused)
#   rivia revoke    kill switch: revoke every token (re-add the connector after)
#   rivia restart   stop + start (do this after editing any .py file)
#   rivia setup     make new secrets in ~/.rivia/.env (only if you have none)
#   rivia token     make the Rivia app's token (replaces the old one)
#   rivia install-cli  put `rivia` on PATH ($PREFIX/bin on Termux, else ~/.local/bin)
#   rivia ask BOT "request"   ask a bot through its webhook and wait for its post_reply

CODE="$HOME/storage/shared/rivia"   # the code you edit
VENV="$HOME/rivia-venv"             # Python packages (must stay in Termux home)
DATA="$HOME/rivia"                  # auth.db and logs (private, not shared storage)
LOGS="$DATA/logs"
PORT=8787
PUBLIC="https://mcp.rivia.observer/health"
WATCH_S="${RIVIA_WATCH_S:-60}"      # watchdog checks this often (seconds)
export PYTHONDONTWRITEBYTECODE=1    # no __pycache__ clutter in the shared folder

mkdir -p "$LOGS"

die() { echo "rivia: $*" >&2; exit 1; }

load_venv() {
    [ -f "$VENV/bin/activate" ] || die "no venv at $VENV. Rebuild it (see README, 'Rebuild the venv')."
    # shellcheck disable=SC1091
    . "$VENV/bin/activate"
}

load_env() {
    local f
    for f in "$HOME/.rivia/.env" "$HOME/rivia/.env"; do
        if [ -f "$f" ]; then
            set -a; . "$f"; set +a
            return 0
        fi
    done
    die "no .env in ~/.rivia or ~/rivia. Run: rivia setup"
}

alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

get() {  # get URL -> prints body or ERROR
    python - "$1" <<'EOF'
import sys, urllib.request
try:
    print(urllib.request.urlopen(sys.argv[1], timeout=8).read().decode()[:200])
except Exception as e:
    print("ERROR", type(e).__name__, e)
EOF
}

start() {
    load_venv; load_env
    [ -f "$CODE/rivia_server.py" ] || die "can't see $CODE/rivia_server.py. Run termux-setup-storage."
    termux-wake-lock 2>/dev/null
    # Watchdog first, so a server or tunnel that fails to start below gets retried.
    if ! alive "$DATA/watch.pid"; then
        nohup bash "$0" watch-loop > /dev/null 2>&1 &
        echo $! > "$DATA/watch.pid"
        echo "watchdog: started (checks every ${WATCH_S}s)"
    fi
    if alive "$DATA/server.pid"; then
        echo "server: already running"
    else
        cd "$CODE" || die "can't cd to $CODE"
        nohup python rivia_server.py --public >> "$LOGS/server.log" 2>&1 &
        echo $! > "$DATA/server.pid"
        sleep 4
        alive "$DATA/server.pid" || { tail -n 20 "$LOGS/server.log"; die "server didn't start (log above)"; }
        echo "server: started"
    fi
    if alive "$DATA/tunnel.pid"; then
        echo "tunnel: already running"
    else
        command -v cloudflared >/dev/null || die "cloudflared isn't installed (pkg install cloudflared)"
        # RIVIA_TUNNEL in .env names the tunnel; empty means ~/.cloudflared/config.yml decides.
        nohup cloudflared tunnel run ${RIVIA_TUNNEL:+"$RIVIA_TUNNEL"} >> "$LOGS/tunnel.log" 2>&1 &
        echo $! > "$DATA/tunnel.pid"
        sleep 6
        alive "$DATA/tunnel.pid" || { tail -n 20 "$LOGS/tunnel.log"; die "tunnel didn't start (log above)"; }
        echo "tunnel: started"
    fi
    status
}

# Restarts the server or tunnel if either dies. Started by `start`, stopped by `stop`.
watch_loop() {
    while true; do
        sleep "$WATCH_S"
        if ! alive "$DATA/server.pid" || ! alive "$DATA/tunnel.pid"; then
            echo "$(date '+%F %T') server or tunnel down; restarting" >> "$LOGS/watch.log"
            ( start ) >> "$LOGS/watch.log" 2>&1
        fi
    done
}

stop() {
    local p
    for p in watch tunnel server; do
        if alive "$DATA/$p.pid"; then kill "$(cat "$DATA/$p.pid")" && echo "$p: stopped"; else echo "$p: wasn't running"; fi
        rm -f "$DATA/$p.pid"
    done
    termux-wake-unlock 2>/dev/null
}

status() {
    load_venv
    alive "$DATA/server.pid" && echo "server: running" || echo "server: NOT running"
    alive "$DATA/tunnel.pid" && echo "tunnel: running" || echo "tunnel: NOT running"
    alive "$DATA/watch.pid" && echo "watchdog: running" || echo "watchdog: NOT running"
    echo "local:  $(get "http://127.0.0.1:$PORT/health")"
    echo "public: $(get "$PUBLIC")"
}

# Planner / sched helpers. Resolve code next to this script (repo tablet/ on a
# dev box, or ~/storage/shared/rivia when the script is deployed there).
sched_code_and_python() {
    # Sets $code and $py, then cds to the code dir. Prefer the folder this
    # script lives in (run it with bash; shared storage is noexec).
    local here
    here="$(cd "$(dirname "$0")" && pwd)"
    if [ -f "$here/rivia_planner.py" ]; then
        code="$here"
    elif [ -f "$CODE/rivia_planner.py" ]; then
        code="$CODE"
    else
        die "can't see rivia_planner.py next to $0 or in $CODE"
    fi
    if [ -x "$VENV/bin/python" ]; then
        # shellcheck disable=SC1091
        . "$VENV/bin/activate"
        py=python
    elif command -v python3 >/dev/null; then
        py=python3
    elif command -v python >/dev/null; then
        py=python
    else
        die "no python3 (and no venv at $VENV)"
    fi
    cd "$code" || die "can't cd to $code"
}

sched_selftest() {
    sched_code_and_python
    "$py" rivia_planner.py selftest
}

sched_gcal() {
    sched_code_and_python
    if [ -z "${RIVIA_GOOGLE_CLIENT_ID:-}" ] || [ -z "${RIVIA_GOOGLE_CLIENT_SECRET:-}" ]; then
        load_env
    fi
    "$py" rivia_gcal.py "$@"
}

sched() {
    case "${1:-}" in
        selftest) sched_selftest ;;
        google-login) sched_gcal "$@" ;;
        events)
            [ -n "${2:-}" ] || die 'usage: rivia sched events <day>'
            sched_gcal "$@"
            ;;
        *) die 'usage: rivia sched selftest | google-login | events <day>' ;;
    esac
}

# Put `rivia` on PATH. Writes a bash wrapper (not a symlink: shared storage
# is noexec on the tablet). Idempotent. Termux: $PREFIX/bin; else ~/.local/bin.
install_cli() {
    local here dest dir
    here="$(cd "$(dirname "$0")" && pwd)"
    [ -f "$here/rivia.sh" ] || die "can't see $here/rivia.sh"
    if [ -n "${PREFIX:-}" ]; then
        dir="$PREFIX/bin"
    else
        dir="$HOME/.local/bin"
    fi
    mkdir -p "$dir"
    dest="$dir/rivia"
    # Shared storage is mounted noexec on Android, so a symlink to the copy
    # there fails with "Permission denied". Write a small wrapper that runs
    # rivia.sh through bash instead (safe to re-run; replaces an old symlink).
    rm -f "$dest"
    printf '#!/usr/bin/env bash\nexec bash "%s/rivia.sh" "$@"\n' "$here" > "$dest"
    chmod 755 "$dest"
    echo "installed $dest (runs bash $here/rivia.sh)"
    case ":$PATH:" in
        *":$dir:"*) ;;
        *) echo "note: $dir is not on PATH yet; add it (Termux already has \$PREFIX/bin)." ;;
    esac
}

case "${1:-}" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; sleep 1; start ;;
    status)  status ;;
    watch-loop) watch_loop ;;
    test)    load_venv
             here="$(cd "$(dirname "$0")" && pwd)"
             if [ -f "$here/test_rivia_auth.py" ]; then testdir="$here"; else testdir="$CODE"; fi
             cd "$testdir" || exit 1
             for t in test_rivia_auth.py test_public_e2e.py test_bots.py test_sched.py test_gcal.py; do
                 if python "$t" > "$LOGS/test.log" 2>&1; then echo "$t: PASS"
                 else echo "$t: FAILED"; tail -n 15 "$LOGS/test.log"; exit 1; fi
             done ;;
    logs)    echo "== watchdog =="; tail -n 5 "$LOGS/watch.log" 2>/dev/null
             echo "== boot =="; tail -n 5 "$LOGS/boot.log" 2>/dev/null
             echo "== server =="; tail -n 25 "$LOGS/server.log" 2>/dev/null
             echo "== tunnel =="; tail -n 15 "$LOGS/tunnel.log" 2>/dev/null ;;
    regs)    load_venv; cd "$CODE" && python rivia_auth.py registrations ;;
    revoke)  load_venv; cd "$CODE" && python rivia_auth.py revoke-all && python rivia_auth.py prune-clients ;;
    setup)   load_venv; mkdir -p "$HOME/.rivia"; chmod 700 "$HOME/.rivia"
             [ -f "$HOME/.rivia/.env" ] && die "~/.rivia/.env already exists. Delete it first if you really want new secrets."
             umask 077; cd "$CODE" || exit 1
             if python rivia_auth.py setup > "$HOME/.rivia/.env.new"; then
                 mv "$HOME/.rivia/.env.new" "$HOME/.rivia/.env"
                 echo "Saved ~/.rivia/.env. Add RIVIA_TOTP_SECRET from it to your authenticator app."
             else rm -f "$HOME/.rivia/.env.new"; die "setup failed; nothing saved"; fi ;;
    token)   load_venv; load_env; cd "$CODE" && python rivia_auth.py mint-app-token "${2:-365}" ;;
    sched)   sched "${@:2}" ;;
    install-cli) install_cli ;;
    ask)     [ -n "${2:-}" ] && [ -n "${3:-}" ] || die 'usage: rivia ask ard "What does tomorrow look like? Don'"'"'t write anything."'
             load_venv; load_env; cd "$CODE" || exit 1
             python - "$2" "$3" <<'PYEOF'
import asyncio, json, sys, time
import rivia_bots as b
r = asyncio.run(b.ask_bot(sys.argv[1], sys.argv[2]))
print(json.dumps(r, indent=1))
if not r.get("ok"):
    sys.exit(1)
t0 = time.time()
while True:
    c = b.check_reply(r["ticket"])
    if c.get("status") != "pending":
        print(json.dumps(c, indent=1))
        print("answer took %d seconds" % (time.time() - t0))
        break
    time.sleep(5)
PYEOF
             ;;
    *)       sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
