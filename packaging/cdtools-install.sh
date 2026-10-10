#!/usr/bin/env bash
# Root half of the music CD tools. Started by riparr-cdtools.service, which is triggered
# by riparr-cdtools.path when the web service -- or apply-system.sh after an update --
# drops a request file.
#
# Everything here is an ordinary Debian package, so unlike the DVD tools nothing is
# built:
#
#   cd-paranoia       reads the audio, re-reading anything that does not verify
#   libcdio-utils     cd-info, which reads the CD-Text burning software writes
#   flac, lame        the FLAC and MP3 encoders
#   opus-tools        opusenc, for Opus
#   vorbis-tools      oggenc, for Ogg Vorbis
#   libarchive-tools  bsdtar, which copies MP3s off a data CD without mounting it
#   ffmpeg            AAC and ALAC -- ONLY when asked for (see below)
#
# cd-paranoia rather than Xiph's cdparanoia: it is the same program ported onto libcdio,
# it is the version Riparr was tested against (10.2+2.0.2, identical in Debian trixie
# and on the development machine), and its progress output is what music.py parses.
#
# Same shape as dvdtools-install.sh: the unprivileged side can ask for this to run and
# cannot say what runs. THIS FILE MUST LIVE OUTSIDE /opt/riparr (apply-system.sh puts it
# in /usr/local/lib/riparr as root:root 0755).
#
# Idempotent: anything already installed is left alone, so a second request is cheap.
set -uo pipefail

RUN=/run/riparr
STATE="$RUN/cdtools.state"
LOG="$RUN/cdtools.log"

mkdir -p "$RUN"

say() {   # phase, progress (0..1), message, [detail]
  printf '{"phase":"%s","progress":%s,"message":%s,"detail":%s}\n' \
    "$1" "$2" \
    "$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$3")" \
    "$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "${4:-}")" \
    > "$STATE.tmp"
  mv -f "$STATE.tmp" "$STATE"
  chmod 0644 "$STATE"
}

fail() {
  say error 0 "$1" "$(tail -n 20 "$LOG")"
  exit 1
}

: > "$LOG"
chmod 0644 "$LOG"

# binary -> package. The binary is what music.py looks for, so it is what is checked.
declare -A PKG=(
  [cd-paranoia]=cd-paranoia
  [cd-info]=libcdio-utils
  [flac]=flac
  [lame]=lame
  [opusenc]=opus-tools
  [oggenc]=vorbis-tools
  [bsdtar]=libarchive-tools
)

need=()
for bin in "${!PKG[@]}"; do
  command -v "$bin" >/dev/null 2>&1 || need+=("${PKG[$bin]}")
done

# ffmpeg is a hundred and more megabytes of codec libraries, wanted only for AAC and
# ALAC, so it is installed only when the web service asked for it. riparr-cdtools.service
# renames the service's request to .wanted before this runs, so the path unit cannot
# fire on it again; only its existence is read, never its contents.
WANT_FFMPEG="$RUN/cdtools-ffmpeg.wanted"
wanted_ffmpeg=0
if [ -e "$WANT_FFMPEG" ]; then
  rm -f "$WANT_FFMPEG"
  wanted_ffmpeg=1
  command -v ffmpeg >/dev/null 2>&1 || need+=(ffmpeg)
fi

if [ ${#need[@]} -eq 0 ]; then
  say done 1 "Music CDs are ready." ""
  exit 0
fi

say starting 0.05 "Installing the music CD tools"

# A MakeMKV build or the DVD tools may be running apt at the same moment -- on a fresh
# box all three are asked for within a few minutes. Waiting for the lock is right.
APT=(apt-get -o DPkg::Lock::Timeout=1200 -y -q --no-install-recommends)
"${APT[@]}" update >>"$LOG" 2>&1 || true

say installing 0.4 "Installing the music CD tools" "${need[*]}"
# One at a time after a failed batch, so a package one release renames does not take
# the other four down with it. The ripper and FLAC are what matter; the rest degrade.
if ! "${APT[@]}" install "${need[@]}" >>"$LOG" 2>&1; then
  for p in "${need[@]}"; do
    "${APT[@]}" install "$p" >>"$LOG" 2>&1 || echo "could not install $p" >>"$LOG"
  done
fi

# Prove it rather than trusting apt's exit status.
missing=()
for bin in cd-paranoia flac; do
  command -v "$bin" >/dev/null 2>&1 || missing+=("$bin")
done
if [ ${#missing[@]} -gt 0 ]; then
  fail "The music CD tools couldn't be installed (${missing[*]} missing)."
fi
if [ "$wanted_ffmpeg" = 1 ] && ! command -v ffmpeg >/dev/null 2>&1; then
  fail "AAC and ALAC need ffmpeg, which couldn't be installed. FLAC, MP3, Opus and Ogg still work."
fi
optional=()
for bin in cd-info lame opusenc oggenc bsdtar; do
  command -v "$bin" >/dev/null 2>&1 || optional+=("$bin")
done
if [ ${#optional[@]} -gt 0 ]; then
  say done 1 "Music CDs are ready." "Not installed: ${optional[*]}"
else
  say done 1 "Music CDs are ready." ""
fi
