#!/bin/sh
# Keep the box cool -- the job the kernel's thermal governor should be doing and isn't.
#
# Found 2026-10-13 on the Orange Pi Zero 2W: 65 C idle, 88-91 C ripping, and the CPU at
# full clock the whole time. Its thermal zones have passive trip points at 60 and 70 C
# and a critical one at 100 C, but no cooling device is bound to them -- so nothing
# would have acted before the emergency cut-off, and the Wi-Fi chip millimetres away
# failed twice under that load the same night. Two things, each every few seconds:
#
#   1. A floor under everything, rip or not, setting or not: above HOT the CPU ceiling
#      steps down one frequency at a time; below COOL it steps back up. What the missing
#      cooling map would have done.
#
#   2. With "Keep the box cool" on (the default), while something is reading a disc:
#      the ceiling is held at RIP_FREQ and the cores beyond the second are taken
#      offline. Measured the same night: neither slows a Blu-ray rip -- MakeMKV waits on
#      the disc and the network, not the CPU -- and together they take a few degrees
#      off. Everything goes back the moment the rip ends.
#
# Pausing between episodes to let the board cool is the third part, and lives in the
# rip engine, which knows where the gaps between titles are (rip._cool_down).
#
# Root, because cpufreq and CPU hotplug are; a status file in /run tells the web page
# what it is doing. Every write is guarded: a board without cpufreq or hotplug simply
# gets less of this, never an error loop.
set -u

HOT="${RIPARR_THERMAL_HOT:-85000}"
COOL="${RIPARR_THERMAL_COOL:-75000}"
RIP_FREQ="${RIPARR_THERMAL_RIP_FREQ:-1008000}"
INTERVAL="${RIPARR_THERMAL_INTERVAL:-5}"
DB="${RIPARR_DB:-/var/lib/riparr/riparr.db}"
PY="${RIPARR_PYTHON:-/opt/riparr/.venv/bin/python}"
[ -x "$PY" ] || PY=python3
# RIPARR_THERMAL_ROOT points the whole thing at a fake /sys, for packaging/thermal.test.py.
ROOT="${RIPARR_THERMAL_ROOT:-}"
STATUS="${RIPARR_THERMAL_STATUS:-/run/riparr-thermal.json}"
CPU="$ROOT/sys/devices/system/cpu"
POL="$CPU/cpufreq/policy0"
[ -d "$POL" ] || POL="$CPU/cpu0/cpufreq"

log() { logger -t riparr-thermal -p daemon.notice -- "$@"; echo "riparr-thermal: $*"; }

# Hottest zone, in millidegrees. Every zone on the reference board reads within a degree
# of the others; taking the maximum means the worst part decides.
temp() {
    t=0
    for z in "$ROOT"/sys/class/thermal/thermal_zone*/temp; do
        v=$(cat "$z" 2>/dev/null) || continue
        [ "$v" -gt "$t" ] 2>/dev/null && t=$v
    done
    echo "$t"
}

cool_setting() {
    [ -n "${RIPARR_THERMAL_COOL_SETTING:-}" ] && { echo "$RIPARR_THERMAL_COOL_SETTING"; return; }
    "$PY" - "$DB" <<'EOF' 2>/dev/null || echo 1
import json, sqlite3, sys
try:
    c = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
    row = c.execute("SELECT value FROM settings WHERE key='cool_running'").fetchone()
    print(1 if (json.loads(row[0]) if row else True) else 0)
except Exception:
    print(1)
EOF
}

ripping() {
    if [ -n "${RIPARR_THERMAL_RIPPING_FILE:-}" ]; then [ -f "$RIPARR_THERMAL_RIPPING_FILE" ]; return; fi
    pgrep -x makemkvcon >/dev/null 2>&1 || pgrep -f 'makemkvcon ' >/dev/null 2>&1 \
        || pgrep -x cd-paranoia >/dev/null 2>&1
}

# The frequencies this CPU offers, lowest first.
FREQS=$(tr ' ' '\n' < "$POL/scaling_available_frequencies" 2>/dev/null | grep . | sort -n)
FULL=$(cat "$POL/cpuinfo_max_freq" 2>/dev/null || echo 0)
LOW=$(echo "$FREQS" | head -1)
[ -n "$LOW" ] || LOW=$(cat "$POL/cpuinfo_min_freq" 2>/dev/null || echo 0)

step() {   # step down|up from $1
    if [ "$2" = down ]; then
        n=$(echo "$FREQS" | awk -v c="$1" '$1 < c' | tail -1)
        echo "${n:-$LOW}"
    else
        n=$(echo "$FREQS" | awk -v c="$1" '$1 > c' | head -1)
        echo "${n:-$FULL}"
    fi
}

set_cores() {   # set_cores N -- keep cpu0..N-1 online, the rest off
    for c in "$CPU"/cpu[0-9]*; do
        i=${c##*cpu}
        [ "$i" -eq 0 ] && continue
        [ -w "$c/online" ] || continue
        want=1
        [ "$i" -ge "$1" ] && want=0
        [ "$(cat "$c/online")" = "$want" ] || echo "$want" > "$c/online" 2>/dev/null
    done
}

ncpu=$(ls -d "$CPU"/cpu[0-9]* 2>/dev/null | wc -l)
cap=$FULL
cool=1
tick=0
state=""
log "Watching temperature: step down above $((HOT/1000)) C, up below $((COOL/1000)) C; while ripping, ${RIP_FREQ%000} MHz and 2 cores."

while :; do
    [ $((tick % 12)) -eq 0 ] && cool=$(cool_setting)
    tick=$((tick + 1))
    t=$(temp)

    if [ "$t" -ge "$HOT" ] && [ "$cap" -gt "$LOW" ]; then
        cap=$(step "$cap" down)
    elif [ "$t" -le "$COOL" ] && [ "$cap" -lt "$FULL" ]; then
        cap=$(step "$cap" up)
    fi

    limit=$cap
    now="normal"
    if [ "$cool" = 1 ] && ripping; then
        [ "$RIP_FREQ" -lt "$limit" ] && limit=$RIP_FREQ
        [ "$ncpu" -ge 4 ] && set_cores 2
        now="ripping"
    else
        set_cores "$ncpu"
    fi
    [ "$cap" -lt "$FULL" ] && now="$now, throttled"

    if [ "$FULL" -gt 0 ] && [ "$(cat "$POL/scaling_max_freq" 2>/dev/null)" != "$limit" ]; then
        echo "$limit" > "$POL/scaling_max_freq" 2>/dev/null
    fi
    if [ "$now" != "$state" ]; then
        log "Now $now: $((t/1000)) C, CPU ceiling $((limit/1000)) MHz, $(cat $CPU/online) online."
        state=$now
    fi
    printf '{"temp_c": %d, "max_mhz": %d, "full_mhz": %d, "online": "%s", "state": "%s", "cool_running": %s, "at": %d}\n' \
        $((t/1000)) $((limit/1000)) $((FULL/1000)) "$(cat $CPU/online)" "$now" \
        "$([ "$cool" = 1 ] && echo true || echo false)" "$(date +%s)" > "$STATUS.tmp" \
        && mv "$STATUS.tmp" "$STATUS"
    sleep "$INTERVAL"
done
