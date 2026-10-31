#!/bin/sh
set -eu
if [ "$(id -u)" = 0 ]; then
    echo "Riparr must run as a non-root user. Set the runtime UID/GID and writable state/staging volumes." >&2
    exit 1
fi
mkdir -p "$HOME/.MakeMKV" /srv/staging /run/riparr
for directory in "$HOME" /srv/staging /run/riparr; do
    [ -w "$directory" ] || { echo "Not writable: $directory. Check volume UID/GID ownership." >&2; exit 1; }
done
if [ "$#" -gt 0 ]; then
    exec "$@"
fi
exec /opt/riparr/.venv/bin/python -m uvicorn riparr.main:app \
    --host 0.0.0.0 --port "${RIPARR_PORT:-9797}" --timeout-graceful-shutdown 30