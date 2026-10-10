"""
Music CDs: from the disc in the tray to tagged FLAC (or MP3) files with cover art.

A music CD is nothing like the rest of what Riparr reads. There is no filesystem, no
volume label, no encryption and nothing MakeMKV will open -- just a table of contents
and a stream of raw audio sectors. So it gets its own tools and its own module, and the
pipeline (`rip.py`) treats it as one more job that produces a folder of files: the same
staging, the same never-overwrite rule, the same verification.

## What people want from a rip, and where each part comes from

* **Lossless files that play everywhere.** FLAC, from `flac`. MP3 (V0) from `lame` for
  anybody whose car stereo predates FLAC.
* **A rip that is right, not merely finished.** `cd-paranoia` (libcdio's port of
  cdparanoia) re-reads anything that does not verify, and reports what it could not fix.
  Those reports are kept and shown -- a track with a skip may click, and somebody
  should be told which one.
* **Correct names, in the shape every library expects.** The disc's MusicBrainz disc ID
  is computed from the table of contents (no tool needed) and looked up. A hit gives
  the album, every track, the year, the label and the MusicBrainz IDs -- which are what
  Plex, Jellyfin, Navidrome, Lidarr and beets use to match a file without guessing.
* **Burned CDs.** MusicBrainz will not know a mix somebody burned in 2006, but the
  burning software usually wrote CD-Text onto it, and `cd-info` reads that. Failing
  both, the files are still ripped and named "Unknown Album [discid]" rather than
  being refused -- on a box nobody is watching, a refused disc is a disc that has to be
  fed in again.
* **Album art,** from the Cover Art Archive, saved as `cover.jpg` beside the tracks and
  embedded in each file.
* **MP3 CDs.** A burned data CD full of MP3s is copied, folders and all. Nothing is
  re-encoded or re-tagged: those files already are what somebody chose.

## What is deliberately not done

* **AccurateRip.** Checking a rip against other people's rips needs the drive's read
  offset and a CRC over every sample. Without a way to test it against a real drive,
  shipping it means shipping either a check that cannot fail or one that cries wolf on
  every disc. cd-paranoia's own verification is what this relies on instead.
* **Hidden track one audio** (audio in the pregap before track 1) is not ripped.
  It exists on a few hundred albums and needs drive support most USB drives lack.
"""
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__, optical as OPT, platform as P

MUSIC = "music"          # a job that rips audio tracks
FILES = "files"          # a job that copies music files off a data disc

SECTOR_BYTES = 2352      # one CD audio sector: 588 stereo 16-bit samples
WAV_HEADER = 44          # cd-paranoia's WAV header, measured against its output
SECTORS_PER_SECOND = 75

# MusicBrainz asks for a meaningful User-Agent with a way to reach the author, and at
# most one request a second. Both are conditions of using the service, not courtesies.
UA = "Riparr/%s ( https://github.com/jackharvest/riparr )" % __version__
MB = "https://musicbrainz.org/ws/2"
CAA = "https://coverartarchive.org"
VARIOUS_ARTISTS = "89ad4ac3-39f7-470e-963a-56509c546377"

# The tools, by binary, with the Debian package that provides each. cd-info is optional:
# without it a burned CD has no CD-Text and is named "Unknown Album", which is a worse
# name and still a rip.
TOOLS = {
    "cd-paranoia": "cd-paranoia",
    "cd-info": "libcdio-utils",
    "flac": "flac",
    "lame": "lame",
    "bsdtar": "libarchive-tools",
}

# What counts as music on a data disc.
AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wma",
                    ".wav", ".aif", ".aiff", ".ape", ".wv", ".alac", ".mpc", ".dsf"}
# What else on a music data disc is worth keeping beside it: the art and the playlists
# somebody made. Kept only in folders that hold music.
COMPANION_EXTENSIONS = {".jpg", ".jpeg", ".png", ".m3u", ".m3u8", ".cue", ".log",
                        ".txt", ".nfo", ".pls"}


class MusicFailed(Exception):
    pass


class MusicCancelled(Exception):
    pass


# ─────────────────────────────── the disc's identity ───────────────────────────────

def read_toc(device):
    """The disc's table of contents with lengths filled in, or None."""
    if P.MOCK:
        toc = _mock_toc()
    else:
        toc = OPT.read_toc(device)
    if not toc or not toc.get("tracks"):
        return None
    return OPT.toc_layout(toc)


def disc_id(toc):
    """The MusicBrainz disc ID: SHA-1 over the audio session's TOC, in MusicBrainz's
    base64 alphabet. Matches libdiscid, and is tested against both worked examples in
    MusicBrainz's own documentation (the plain CD and the Enhanced CD).

    Offsets are LBA + 150, because the first track of every CD starts two seconds in.
    An Enhanced CD's data track is not part of the ID; its lead-out is the audio
    session's (`toc_layout`). A mixed-mode CD's leading data track is.
    """
    lay = toc if "audio_leadout" in toc else OPT.toc_layout(toc)
    tracks = lay["tracks"][:-1] if lay["multisession"] else lay["tracks"]
    first, last = tracks[0]["number"], tracks[-1]["number"]
    offsets = [lay["audio_leadout"] + 150] + [t["lba"] + 150 for t in tracks]
    offsets += [0] * (100 - len(offsets))
    text = "%02X%02X" % (first, last) + "".join("%08X" % o for o in offsets[:100])
    digest = hashlib.sha1(text.encode("ascii")).digest()
    return (base64.b64encode(digest).decode("ascii")
            .replace("+", ".").replace("/", "_").replace("=", "-"))


def mb_toc(toc):
    """The `toc=` parameter for MusicBrainz's fuzzy lookup: first last leadout offsets."""
    lay = toc if "audio_leadout" in toc else OPT.toc_layout(toc)
    tracks = lay["tracks"][:-1] if lay["multisession"] else lay["tracks"]
    parts = [tracks[0]["number"], tracks[-1]["number"], lay["audio_leadout"] + 150]
    parts += [t["lba"] + 150 for t in tracks]
    return " ".join(str(p) for p in parts)


def freedb_id(toc):
    """The old CDDB/freedb ID: eight hex digits, readable, stable per pressing. Used to
    name a disc nothing knows, so a re-rip of the same unknown CD lands in the same
    folder instead of a new one."""
    lay = toc if "audio_leadout" in toc else OPT.toc_layout(toc)
    tracks = lay["tracks"]

    def digits(n):
        return sum(int(c) for c in str(n))

    n = sum(digits((t["lba"] + 150) // SECTORS_PER_SECOND) for t in tracks)
    length = (lay["leadout"] - tracks[0]["lba"]) // SECTORS_PER_SECOND
    return "%08x" % (((n % 0xFF) << 24) | (length << 8) | len(tracks))


def fingerprint(toc):
    """What the discs table knows a music CD by. The disc ID is a better identity than
    anything the film path has -- exact, instant, and the same for every copy of the
    pressing -- so it is used as is."""
    return "cd:" + disc_id(toc)


# ─────────────────────────────── CD-Text ───────────────────────────────

_CDTEXT_HEAD = re.compile(r"^CD-TEXT for (Disc|Track\s+(\d+)):\s*$")
_CDTEXT_FIELD = re.compile(r"^\s+([A-Z_]+):\s?(.*)$")


def read_cdtext(device):
    """{"album", "artist", "tracks": {n: {"title", "artist"}}} from the disc, or {}.

    Through `cd-info`, whose CD-Text block was checked against libcdio's own output for
    a cue sheet carrying it. **`--no-analyze` must not be passed**: it suppresses the
    CD-Text block along with the filesystem analysis, and a burned CD would silently
    lose the only names it has.
    """
    binary = shutil.which("cd-info")
    if not binary or (P.MOCK and not device.endswith(".cue")):
        return {}
    source = ["--cue-file", device] if device.endswith(".cue") else ["-C", device]
    try:
        p = subprocess.run([binary, "--no-header", "--no-device-info", "--no-disc-mode",
                            "--no-vcd", "--no-ioctl", "-q"] + source,
                           capture_output=True, text=True, timeout=60, errors="replace")
    except Exception:
        return {}
    return parse_cdtext(p.stdout or "")


def parse_cdtext(text):
    """Pure parse of cd-info's CD-Text block. Only the first language block is read:
    multi-language CD-Text exists on a few Japanese releases and the first block is the
    disc's own primary language."""
    out = {"album": "", "artist": "", "tracks": {}}
    where = None
    languages = 0
    for line in text.splitlines():
        if line.startswith("Language "):
            languages += 1
            if languages > 1:
                break
            continue
        m = _CDTEXT_HEAD.match(line.strip("\r"))
        if m:
            where = "disc" if m.group(1) == "Disc" else int(m.group(2))
            continue
        m = _CDTEXT_FIELD.match(line)
        if m and where is not None:
            key, value = m.group(1), _clean_text(m.group(2))
            if key not in ("TITLE", "PERFORMER") or not value:
                continue
            if where == "disc":
                out["album" if key == "TITLE" else "artist"] = value
            else:
                row = out["tracks"].setdefault(where, {})
                row["title" if key == "TITLE" else "artist"] = value
            continue
        if line.strip() and not line.startswith((" ", "\t")):
            where = None if not line.startswith("CD-TEXT") else where
    if not out["album"] and not out["artist"] and not out["tracks"]:
        return {}
    return out


def _clean_text(s):
    s = (s or "").replace("\x00", "").strip()
    return re.sub(r"\s+", " ", s)


# ─────────────────────────────── MusicBrainz ───────────────────────────────

_mb_lock = threading.Lock()
_mb_last = [0.0]


def _mb_get(path, params):
    """One MusicBrainz request, at most one a second. Returns parsed JSON, or None for
    "not found". Raises on anything else -- the caller decides that no network means
    "name it from CD-Text", not "fail the rip"."""
    with _mb_lock:
        wait = 1.1 - (time.time() - _mb_last[0])
        if wait > 0:
            time.sleep(wait)
        _mb_last[0] = time.time()
    url = "%s/%s?%s" % (MB, path, urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


_INC = "artist-credits+recordings+release-groups+labels+isrcs"


def lookup(toc, cdtext=None, log=None):
    """Find this disc on MusicBrainz. Returns (album_or_None, candidates, how).

    `how` is "discid" for an exact match, "toc" for a match on track lengths, or "".

    Exact disc ID first. That is an identity, not a guess: the same pressing has the
    same ID everywhere.

    The fuzzy lookup by track lengths is the fallback, and it is **only trusted when the
    disc's own CD-Text agrees with it.** Tried against the first disc in MusicBrainz's
    documentation with its ID withheld, it returned that disc and also a Korean pop
    album from 2012 with the same number of tracks of similar lengths. On a burned CD
    with nothing written on it, there is no way to tell those apart, and filing
    somebody's mixtape as somebody else's album is worse than calling it Unknown.
    """
    did = disc_id(toc)
    got = _mb_get("discid/%s" % did, {"inc": _INC, "fmt": "json"})
    if got and got.get("releases"):
        cands = _candidates(got["releases"], did, toc)
        if cands:
            return cands[0], cands, "discid"
    if not cdtext or not (cdtext.get("album") or cdtext.get("artist")):
        return None, [], ""
    got = _mb_get("discid/-", {"toc": mb_toc(toc), "cdstubs": "no", "inc": _INC,
                               "fmt": "json"})
    if not got or not got.get("releases"):
        return None, [], ""
    cands = [c for c in _candidates(got["releases"], None, toc) if _agrees(c, cdtext)]
    if not cands:
        if log:
            log.info("MusicBrainz had %d near matches by track length and none agreed "
                     "with the CD-Text; not using any.", len(got["releases"]))
        return None, [], ""
    return cands[0], cands, "toc"


def _similar(a, b):
    import difflib
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b or a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _norm(s):
    s = (s or "").lower()
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"^the ", "", s.strip())


def _agrees(album, cdtext):
    """Does the CD-Text name the same album? Title and artist each count; either alone
    at a high match is enough, because burning software often writes only one."""
    t = _similar(album.get("album"), cdtext.get("album")) if cdtext.get("album") else 0
    a = (_similar(album.get("album_artist"), cdtext.get("artist"))
         if cdtext.get("artist") else 0)
    return t >= 0.85 or (a >= 0.85 and t >= 0.5) or (t >= 0.85 and a >= 0.5)


def _credit(credits):
    return "".join("%s%s" % (c.get("name") or (c.get("artist") or {}).get("name") or "",
                             c.get("joinphrase") or "")
                   for c in (credits or [])).strip()


def _credit_ids(credits):
    return [((c.get("artist") or {}).get("id") or "") for c in (credits or [])
            if (c.get("artist") or {}).get("id")]


def _candidates(releases, did, toc):
    """Every release that could be this disc, best first, each as an album dict.

    Several releases share one disc ID whenever a label reissued the same pressing, so
    there is usually more than one and they differ only in year and label. The order:
    official releases first, then ones with cover art (an album with no art is a worse
    library entry), then the earliest -- which is the original release more often than
    not -- and the MusicBrainz ID last so the same disc picks the same release every
    time it is read.
    """
    audio = OPT.toc_layout(toc)["audio"] if "audio" not in toc else toc["audio"]
    out = []
    for r in releases:
        media = r.get("media") or []
        medium = None
        if did:
            medium = next((m for m in media
                           if any(d.get("id") == did for d in m.get("discs") or [])), None)
        if medium is None:
            # The fuzzy lookup does not say which medium matched. Take the one with the
            # right number of tracks, which on a multi-disc set is usually unique.
            fits = [m for m in media if len(m.get("tracks") or []) == len(audio)]
            medium = fits[0] if len(fits) == 1 else (media[0] if len(media) == 1 else None)
        if medium is None:
            continue
        out.append(_album(r, medium, len(media), did))
    out.sort(key=lambda a: (a["status"] != "Official", not a["has_art"],
                            a["date"] or "9999", a["release_id"]))
    return out


def _album(r, medium, disc_total, did):
    group = r.get("release-group") or {}
    label = (r.get("label-info") or [{}])[0] if r.get("label-info") else {}
    artist = _credit(r.get("artist-credit"))
    artist_ids = _credit_ids(r.get("artist-credit"))
    original = group.get("first-release-date") or r.get("date") or ""
    album = {
        "release_id": r.get("id") or "",
        "release_group_id": group.get("id") or "",
        "album": r.get("title") or "",
        "album_artist": artist,
        "album_artist_ids": artist_ids,
        "album_artist_sort": ((r.get("artist-credit") or [{}])[0].get("artist") or {})
                             .get("sort-name") or "",
        "date": r.get("date") or "",
        "original_date": original,
        "year": (original or r.get("date") or "")[:4],
        "country": r.get("country") or "",
        "status": r.get("status") or "",
        "release_type": (group.get("primary-type") or "").lower(),
        "secondary_types": [t.lower() for t in group.get("secondary-types") or []],
        "label": ((label.get("label") or {}).get("name") or "") if label else "",
        "catalog": (label.get("catalog-number") or "") if label else "",
        "barcode": r.get("barcode") or "",
        "disc": int(medium.get("position") or 1),
        "discs": int(disc_total or 1),
        "disc_title": medium.get("title") or "",
        "has_art": bool((r.get("cover-art-archive") or {}).get("front")),
        "compilation": VARIOUS_ARTISTS in artist_ids,
        "tracks": [],
    }
    for t in medium.get("tracks") or []:
        rec = t.get("recording") or {}
        credits = t.get("artist-credit") or rec.get("artist-credit")
        album["tracks"].append({
            "position": int(t.get("position") or 0),
            "title": t.get("title") or rec.get("title") or "",
            "artist": _credit(credits) or artist,
            "artist_ids": _credit_ids(credits),
            "recording_id": rec.get("id") or "",
            "track_id": t.get("id") or "",
            "isrc": (rec.get("isrcs") or [""])[0],
        })
    return album


def cover_art(release_id, folder):
    """Download the release's front cover into `folder` as cover.jpg (or cover.png, if
    that is what the archive holds). Returns the path, or None.

    1200 px where the archive has it, which is big enough for any TV a media server
    will show it on and small enough (a few hundred KB) to embed in every track. The
    archive redirects to archive.org, which urllib follows."""
    if not release_id:
        return None
    for size in ("front-1200", "front-500", "front"):
        url = "%s/release/%s/%s" % (CAA, release_id, size)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read(20 * 2 ** 20)
        except Exception:
            continue
        # A JPEG or a PNG, by its first bytes. The archive is trustworthy; a captive
        # portal that answers every request with an HTML page is not.
        if data[:3] == b"\xff\xd8\xff":
            dest = os.path.join(folder, "cover.jpg")
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            dest = os.path.join(folder, "cover.png")
        else:
            continue
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        return dest
    return None


# ─────────────────────────────── putting it together ───────────────────────────────

def identify(toc, device, s, remembered=None, log=None):
    """Everything known about this disc, as the plan the pipeline rips from.

    Never raises for want of a name. Every source of names is optional, and the order
    is how much each one can be trusted: a remembered correction, an exact MusicBrainz
    match, CD-Text confirmed by MusicBrainz, CD-Text alone, and nothing.
    """
    remembered = remembered or {}
    warnings = []
    cdtext = read_cdtext(device) if s.get("music_cdtext", True) else {}
    album, cands, how = None, [], ""
    if s.get("music_metadata", True):
        try:
            album, cands, how = lookup(toc, cdtext, log=log)
        except Exception as e:
            if log:
                log.warning("MusicBrainz lookup failed: %s", e)
            warnings.append("MusicBrainz couldn't be reached, so this was named from "
                            "the disc itself.")
        want = remembered.get("release_id")
        if want and cands:
            album = next((c for c in cands if c["release_id"] == want), album)

    did = disc_id(toc)
    audio = toc["audio"]
    plan = {"disc_id": did, "freedb_id": freedb_id(toc), "kind": OPT.cd_kind(toc),
            "candidates": [{k: c[k] for k in ("release_id", "album", "album_artist",
                                              "date", "country", "label", "catalog")}
                           for c in cands[:8]]}
    if album:
        plan.update({k: v for k, v in album.items() if k != "tracks"})
        plan["source"] = "musicbrainz" if how == "discid" else "musicbrainz-toc"
        meta = album["tracks"]
    elif cdtext:
        plan.update({"album": cdtext.get("album") or "", "release_id": "",
                     "album_artist": cdtext.get("artist") or "", "year": "",
                     "disc": 1, "discs": 1, "has_art": False, "compilation": False})
        plan["source"] = "cdtext"
        meta = []
    else:
        plan.update({"album": "", "album_artist": "", "release_id": "", "year": "",
                     "disc": 1, "discs": 1, "has_art": False, "compilation": False})
        plan["source"] = "none"
        meta = []
    if not plan.get("album"):
        plan["album"] = "Unknown Album [%s]" % plan["freedb_id"]
        plan["unknown"] = True
    if not plan.get("album_artist"):
        artists = {(cdtext.get("tracks") or {}).get(t["number"], {}).get("artist")
                   for t in audio} - {None, ""}
        plan["album_artist"] = (artists.pop() if len(artists) == 1
                                else "Various Artists" if artists else "Unknown Artist")
        if plan["album_artist"] == "Various Artists":
            plan["compilation"] = True

    rows = []
    for i, t in enumerate(audio):
        m = meta[i] if i < len(meta) else {}
        ct = (cdtext.get("tracks") or {}).get(t["number"], {}) if cdtext else {}
        rows.append({
            "number": t["number"],                 # on the disc, what cd-paranoia asks for
            "position": i + 1,                     # in the album, what the tags say
            "lba": t["lba"], "sectors": t["sectors"],
            "seconds": t["sectors"] // SECTORS_PER_SECOND,
            "preemphasis": t.get("preemphasis", False),
            "title": m.get("title") or ct.get("title") or "Track %02d" % (i + 1),
            "artist": m.get("artist") or ct.get("artist") or plan["album_artist"],
            "artist_ids": m.get("artist_ids") or [],
            "recording_id": m.get("recording_id") or "",
            "track_id": m.get("track_id") or "",
            "isrc": m.get("isrc") or "",
            "state": "pending",
        })
    if meta and len(meta) != len(audio):
        warnings.append("MusicBrainz lists %d tracks for this disc and the disc has %d, "
                        "so some names may be on the wrong tracks."
                        % (len(meta), len(audio)))
    if plan["source"] == "musicbrainz-toc":
        warnings.append("Matched on MusicBrainz by track lengths and the disc's own "
                        "CD-Text, not by its exact disc ID — usually a burned copy of "
                        "the album.")
    if plan["source"] == "none":
        warnings.append("Nothing knew this disc — not MusicBrainz, and there was no "
                        "CD-Text on it — so it was saved as \u201c%s\u201d with numbered "
                        "tracks." % plan["album"])
    if any(r["preemphasis"] for r in rows):
        warnings.append("This disc was mastered with pre-emphasis, which a few "
                        "early-80s CDs were. The files are exact copies and may sound "
                        "slightly bright on players that ignore the flag.")
    plan["tracks"] = rows
    plan["warnings"] = warnings
    return plan


# ─────────────────────────────── naming ───────────────────────────────

DEFAULT_TEMPLATE = "{AlbumArtist}/{Album} ({Year})/{DiscTrack} - {Title}"
EXTENSIONS = {"flac": ".flac", "mp3": ".mp3"}

_TRACK_TOKEN = re.compile(r"\{(Track|Disc)(?::(0+))?\}")


def track_path(template, plan, row, fmt, sanitise):
    """The album-relative and full share-relative pieces of one track's path:
    (album_folder, filename). The template is the whole path without an extension;
    everything up to the last slash is the album's folder.

    Values are sanitised **before** they are joined, because an artist called AC/DC is
    one folder, not two -- the slash in a name is not the slash in a template.

    `{DiscTrack}` is `01` on a single disc and `2-01` on disc two of a set, which keeps
    every disc of an album in one folder without two tracks called 01 colliding --
    beets' and Lidarr's convention, and what Plex and Jellyfin read correctly because
    the tags carry the disc number anyway.
    """
    multi = int(plan.get("discs") or 1) > 1
    disc = int(plan.get("disc") or 1)
    pos = int(row["position"])
    album_artist = plan.get("album_artist") or "Unknown Artist"
    values = {
        "AlbumArtist": sanitise(album_artist),
        "Artist": sanitise(row.get("artist") or album_artist),
        "Album": sanitise(plan.get("album") or "Unknown Album"),
        "Year": str(plan.get("year") or ""),
        "Title": sanitise(row.get("title") or "Track %02d" % pos),
        "DiscTrack": ("%d-%02d" % (disc, pos)) if multi else "%02d" % pos,
        "DiscTotal": str(plan.get("discs") or 1),
        "TrackTotal": str(len(plan.get("tracks") or [])),
    }

    def pad(m):
        width = len(m.group(2) or "") or 1
        return "%0*d" % (width, pos if m.group(1) == "Track" else disc)

    out = _TRACK_TOKEN.sub(pad, template or DEFAULT_TEMPLATE)
    for k, v in values.items():
        out = out.replace("{%s}" % k, v)
    out = re.sub(r"\s*\(\)", "", out)            # no year: "Album ()" -> "Album"
    out = re.sub(r"\s*\[\]", "", out)
    out = re.sub(r"(^|/)\s*-\s+", r"\1", out)     # a leading token that rendered empty
    out = re.sub(r"\s{2,}", " ", out)
    segs = [seg.strip().rstrip(". ") for seg in out.split("/")]
    segs = [seg for seg in segs if seg] or ["%02d" % pos]
    name = segs[-1] + EXTENSIONS.get(fmt, ".flac")
    return "/".join(segs[:-1]), name


# ─────────────────────────────── tags ───────────────────────────────

def vorbis_tags(plan, row):
    """The tags for one track, in the names Picard writes -- which are the names every
    music server and library manager reads. The MusicBrainz IDs are the important part:
    with them, Lidarr and beets match a file exactly instead of by its name."""
    tags = [
        ("TITLE", row.get("title")), ("ARTIST", row.get("artist")),
        ("ALBUM", plan.get("album")), ("ALBUMARTIST", plan.get("album_artist")),
        ("ALBUMARTISTSORT", plan.get("album_artist_sort")),
        ("TRACKNUMBER", str(row["position"])),
        ("TRACKTOTAL", str(len(plan.get("tracks") or []))),
        ("TOTALTRACKS", str(len(plan.get("tracks") or []))),
        ("DISCNUMBER", str(plan.get("disc") or 1)),
        ("DISCTOTAL", str(plan.get("discs") or 1)),
        ("TOTALDISCS", str(plan.get("discs") or 1)),
        ("DISCSUBTITLE", plan.get("disc_title")),
        ("DATE", plan.get("date") or plan.get("year")),
        ("ORIGINALDATE", plan.get("original_date")),
        ("ORIGINALYEAR", (plan.get("original_date") or "")[:4]),
        ("LABEL", plan.get("label")), ("CATALOGNUMBER", plan.get("catalog")),
        ("BARCODE", plan.get("barcode")), ("RELEASECOUNTRY", plan.get("country")),
        ("RELEASESTATUS", (plan.get("status") or "").lower()),
        ("RELEASETYPE", plan.get("release_type")),
        ("MEDIA", "CD"), ("ISRC", row.get("isrc")),
        ("COMPILATION", "1" if plan.get("compilation") else ""),
        ("MUSICBRAINZ_ALBUMID", plan.get("release_id")),
        ("MUSICBRAINZ_RELEASEGROUPID", plan.get("release_group_id")),
        ("MUSICBRAINZ_TRACKID", row.get("recording_id")),
        ("MUSICBRAINZ_RELEASETRACKID", row.get("track_id")),
        ("MUSICBRAINZ_DISCID", plan.get("disc_id")),
    ]
    for aid in row.get("artist_ids") or []:
        tags.append(("MUSICBRAINZ_ARTISTID", aid))
    for aid in plan.get("album_artist_ids") or []:
        tags.append(("MUSICBRAINZ_ALBUMARTISTID", aid))
    return [(k, _tag_value(v)) for k, v in tags if _tag_value(v)]


def _tag_value(v):
    # A newline in a tag value is legal in Vorbis comments and breaks flac's
    # --tag=NAME=VALUE argument parsing on some builds; nothing a CD carries needs one.
    return re.sub(r"[\r\n\x00]+", " ", str(v or "")).strip()


def encode_command(fmt, wav, out, plan, row, cover=None):
    """The encoder's argv. Exposed so the tests can read it."""
    if fmt == "mp3":
        cmd = ["lame", "--quiet", "--nohist", "-V", "0", "--id3v2-only", "--id3v2-utf16",
               "--tt", row.get("title") or "", "--ta", row.get("artist") or "",
               "--tl", plan.get("album") or "",
               "--tn", "%d/%d" % (row["position"], len(plan.get("tracks") or [])),
               "--tv", "TPE2=%s" % _tag_value(plan.get("album_artist")),
               "--tv", "TPOS=%d/%d" % (plan.get("disc") or 1, plan.get("discs") or 1)]
        if plan.get("year"):
            cmd += ["--ty", str(plan["year"])]
        if plan.get("compilation"):
            cmd += ["--tv", "TCMP=1"]
        for key, frame in (("release_id", "MusicBrainz Album Id"),
                           ("release_group_id", "MusicBrainz Release Group Id"),
                           ("disc_id", "MusicBrainz Disc Id")):
            if plan.get(key):
                cmd += ["--tv", "TXXX=%s=%s" % (frame, plan[key])]
        if row.get("track_id"):
            cmd += ["--tv", "TXXX=MusicBrainz Release Track Id=%s" % row["track_id"]]
        if (plan.get("album_artist_ids") or []):
            cmd += ["--tv", "TXXX=MusicBrainz Album Artist Id=%s"
                    % plan["album_artist_ids"][0]]
        if (row.get("artist_ids") or []):
            cmd += ["--tv", "TXXX=MusicBrainz Artist Id=%s" % row["artist_ids"][0]]
        if cover:
            cmd += ["--ti", cover]
        return cmd + [wav, out]
    cmd = ["flac", "--silent", "-5", "-f", "-o", out]
    cmd += ["--tag=%s=%s" % kv for kv in vorbis_tags(plan, row)]
    if cover:
        cmd.append("--picture=%s" % cover)
    return cmd + [wav]


def encode(fmt, wav, out, plan, row, cover=None):
    """Start encoding one track. Returns the Popen; `finish_encode` collects it.

    Asynchronous because the drive is the slow part: the next track is read while this
    one encodes, which takes the encode time out of every track but the last."""
    cmd = encode_command(fmt, wav, out, plan, row, cover)
    cmd[0] = shutil.which(cmd[0]) or cmd[0]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, errors="replace")


def finish_encode(proc, out, timeout=1800):
    try:
        _, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise MusicFailed("The encoder stopped responding on %s." % os.path.basename(out))
    if proc.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 128:
        raise MusicFailed("Couldn't encode %s: %s" % (os.path.basename(out),
                                                       (err or "").strip()[-300:]
                                                       or "the encoder failed"))


# ─────────────────────────────── reading the disc ───────────────────────────────

# cd-paranoia's callback names, from libcdio-paranoia's own source. Only these mean the
# audio is not what is on the disc; the rest are corrections it made successfully.
_UNCORRECTED = {"skip"}
_NOTEWORTHY = {"scratch", "drift", "dropped", "duped", "transport error"}
_CALLBACK = re.compile(r"^##:\s*-?\d+\s+\[([a-z ]+)\]\s+@\s+(\d+)")

# Long enough for a badly scratched track that cd-paranoia is slowly getting through;
# short enough that a drive that has wedged does not hold the queue for a whole night.
STALL_SECONDS = 15 * 60


def source(device):
    """What the tools read from: the drive, or in the tests a cue image standing in."""
    if P.MOCK and os.environ.get("RIPARR_MOCK_CD_IMAGE"):
        return os.environ["RIPARR_MOCK_CD_IMAGE"]
    return device or "/dev/sr0"


def rip_command(device, row, wav, gentle=False):
    """cd-paranoia's argv for one track.

    **Every track is addressed by an explicit sector span**, `N[.0]-N[.len-1]`, rather
    than by its number alone. Measured against cd-paranoia 10.2+2.0.2 (the version in
    Debian trixie) on a synthesized image: the span is inclusive and reads exactly
    `len` sectors, byte for byte what the image holds. Using the number alone would
    leave the end of the track to libcdio, which on an Enhanced CD runs the last audio
    track into the 11400 sectors between the sessions -- sectors that do not exist.

    `gentle` is the retry for a track that failed outright: lighter checking (-Y) gets
    through some discs the full paranoia cannot, at a small cost in certainty that the
    log records.
    """
    span = "%d[.0]-%d[.%d]" % (row["number"], row["number"], max(0, row["sectors"] - 1))
    source = device
    cmd = ["cd-paranoia", "-e", "-w", "-d", source]
    if gentle:
        cmd.append("-Y")
    return cmd + ["--", span, wav]


def rip_track(device, row, wav, cancel, on_progress=None, gentle=False, log=None):
    """Read one track to a WAV file. Returns {"skips", "notes"}; raises MusicFailed.

    Progress is the WAV's size against what the track must come to -- the same answer
    the film path settled on, and for the same reason: it is the one number that is
    true whatever the tool says. The callback lines are read for what they can say that
    the size cannot, which is where the drive struggled.
    """
    expect = row["sectors"] * SECTOR_BYTES + WAV_HEADER
    if P.MOCK and not os.environ.get("RIPARR_MOCK_CD_IMAGE"):
        return _mock_rip_track(row, wav, cancel, on_progress, expect)
    cmd = rip_command(device, row, wav, gentle=gentle)
    cmd[0] = shutil.which(cmd[0]) or cmd[0]
    if log:
        log.info("Reading track %d: %s", row["number"], " ".join(cmd))
    try:
        os.unlink(wav)
    except OSError:
        pass
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, errors="replace", bufsize=1)
    counts = {}
    tail = []
    state = {"size": 0, "moved": time.time()}
    done = threading.Event()

    def watch():
        # stderr is read on this thread, so a cd-paranoia that goes silent cannot stop
        # the cancel and the stall check from running.
        while not done.wait(1.0):
            if cancel.is_set():
                proc.terminate()
                return
            try:
                size = os.path.getsize(wav)
            except OSError:
                size = 0
            if size > state["size"]:
                state["size"], state["moved"] = size, time.time()
            elif time.time() - state["moved"] > STALL_SECONDS:
                state["stalled"] = True
                proc.kill()
                return
            if on_progress:
                on_progress(min(size, expect), expect)

    t = threading.Thread(target=watch, name="riparr-cd-watch", daemon=True)
    t.start()
    try:
        for line in proc.stderr:
            m = _CALLBACK.match(line.strip())
            if m:
                name = m.group(1)
                if name in _UNCORRECTED or name in _NOTEWORTHY:
                    counts[name] = counts.get(name, 0) + 1
                continue
            if line.strip():
                tail = (tail + [line.strip()])[-6:]
    finally:
        rc = proc.wait()
        done.set()
        t.join(timeout=5)
    if cancel.is_set():
        raise MusicCancelled()
    if state.get("stalled"):
        raise MusicFailed("The drive stopped making progress on track %d for %d minutes."
                          % (row["number"], STALL_SECONDS // 60))
    got = os.path.getsize(wav) if os.path.exists(wav) else 0
    if rc != 0 or got < expect:
        why = next((l for l in reversed(tail) if not l.startswith("(")), "") or \
            "cd-paranoia exited with %s" % rc
        raise MusicFailed("Couldn't read track %d (%d of %d bytes): %s"
                          % (row["number"], got, expect, why))
    if on_progress:
        on_progress(expect, expect)
    return {"skips": counts.get("skip", 0),
            "notes": {k: v for k, v in counts.items() if k != "skip"}}


# ─────────────────────────────── data discs ───────────────────────────────

def data_audio_files(device):
    """Every music file on a data disc, as [(path, size)], or [] if there are none.

    Through bsdtar, which reads ISO 9660 with its Joliet and Rock Ridge long names
    straight off the block device -- no mount, so no root. That matters: this service
    has no privileges and must never be given them.
    """
    binary = shutil.which("bsdtar")
    if not binary:
        return []
    try:
        p = subprocess.run([binary, "-tvf", device], capture_output=True, text=True,
                           timeout=300, errors="replace")
    except Exception:
        return []
    if p.returncode != 0:
        return []
    return [(path, size) for path, size, is_dir in _parse_listing(p.stdout or "")
            if not is_dir and os.path.splitext(path)[1].lower() in AUDIO_EXTENSIONS]


# `bsdtar -tv` prints an ls -l line: mode, links, owner, group, size, three date
# fields, then the path. The path is everything after the date, so names with spaces
# survive.
_LISTING = re.compile(r"^([dl\-][rwxsStT\-]{9})\s+\d+\s+\S+\s+\S+\s+(\d+)\s+"
                      r"\S+\s+\S+\s+\S+\s+(.+)$")


def _parse_listing(text):
    out = []
    for line in text.splitlines():
        m = _LISTING.match(line.rstrip("\n"))
        if not m:
            continue
        path = m.group(3)
        if m.group(1).startswith("l"):
            path = path.split(" -> ")[0]
        # A prefix, not a set of characters: lstrip("./") would turn ".hidden.mp3"
        # into "hidden.mp3", a file that does not exist.
        while path.startswith("./"):
            path = path[2:]
        path = path.rstrip("/")
        if not path or path == "." or path.startswith("/") \
                or ".." in path.split("/"):
            continue                       # never write outside the job's folder
        out.append((path, int(m.group(2)), m.group(1).startswith("d")))
    return out


def copy_data_disc(device, dest, cancel, on_progress=None, total=0):
    """Extract a data disc's music into `dest`, keeping its folders. Returns the list of
    relative paths written.

    Everything is extracted and then everything that is not music is removed, rather
    than naming the files to extract: bsdtar reads its arguments as patterns, and a
    perfectly ordinary file called "Track [Live].mp3" is a pattern that does not match
    itself."""
    binary = shutil.which("bsdtar")
    if not binary:
        raise MusicFailed("bsdtar isn't installed, so data discs can't be read.")
    os.makedirs(dest, exist_ok=True)
    proc = subprocess.Popen([binary, "-xf", device, "-C", dest, "--no-same-owner",
                             "--no-same-permissions"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                            errors="replace")
    while proc.poll() is None:
        if cancel.is_set():
            proc.terminate()
            proc.wait()
            raise MusicCancelled()
        if on_progress and total:
            on_progress(min(_tree_bytes(dest), total), total)
        time.sleep(1)
    err = (proc.stderr.read() or "").strip() if proc.stderr else ""
    kept = []
    music_dirs = set()
    for root, _dirs, files in os.walk(dest):
        for f in files:
            if os.path.splitext(f)[1].lower() in AUDIO_EXTENSIONS:
                music_dirs.add(root)
    for root, _dirs, files in os.walk(dest):
        for f in files:
            p = os.path.join(root, f)
            ext = os.path.splitext(f)[1].lower()
            if ext in AUDIO_EXTENSIONS or (ext in COMPANION_EXTENSIONS
                                           and root in music_dirs):
                os.chmod(p, 0o644)
                kept.append(os.path.relpath(p, dest).replace(os.sep, "/"))
            else:
                os.unlink(p)
    for root, dirs, _files in os.walk(dest, topdown=False):
        for d in dirs:
            try:
                os.rmdir(os.path.join(root, d))     # only succeeds when empty
            except OSError:
                pass
    if not any(os.path.splitext(k)[1].lower() in AUDIO_EXTENSIONS for k in kept):
        raise MusicFailed("Couldn't copy the music off this disc%s."
                          % (": " + err[-200:] if err else ""))
    return sorted(kept)


def _tree_bytes(root):
    n = 0
    for d, _dirs, files in os.walk(root):
        for f in files:
            try:
                n += os.path.getsize(os.path.join(d, f))
            except OSError:
                pass
    return n


# ─────────────────────────────── are the tools here ───────────────────────────────

RUNDIR = "/run/riparr"
REQUEST = os.path.join(RUNDIR, "cdtools.request")
STATE = os.path.join(RUNDIR, "cdtools.state")
BRIDGE_UNIT = "/etc/systemd/system/riparr-cdtools.path"


def tools():
    """{binary: bool} for every tool, and what each kind of rip needs."""
    if P.MOCK:
        have = {k: os.environ.get("RIPARR_MOCK_CDTOOLS", "1") == "1" for k in TOOLS}
    else:
        have = {k: bool(shutil.which(k)) for k in TOOLS}
    return have


def ready(fmt="flac", data=False):
    """(True, "") if a disc of this kind can be ripped now, else (False, what is missing)."""
    have = tools()
    need = ["bsdtar"] if data else ["cd-paranoia", "lame" if fmt == "mp3" else "flac"]
    missing = [n for n in need if not have.get(n)]
    if not missing:
        return True, ""
    return False, ", ".join(missing)


def _read_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def installing():
    return (_read_state() or {}).get("phase") in ("starting", "installing")


def bridge_available():
    return os.path.exists(BRIDGE_UNIT) and os.path.isdir(RUNDIR) and os.access(
        RUNDIR, os.W_OK)


def status():
    """Everything the settings page shows about the music tools."""
    have = tools()
    st = _read_state() or {}
    ok, _ = ready("flac")
    return {"ready": ok and have.get("lame") and have.get("bsdtar"), "tools": have,
            "installing": installing(), "phase": st.get("phase"),
            "message": st.get("message"), "detail": st.get("detail"),
            "can_install": P.MOCK or bridge_available()}


def request_install():
    """Ask the root side to install the CD tools. One empty file, like every other
    privileged action: the request carries nothing the root side reads."""
    if P.MOCK:
        return {"ok": True, "message": "Off-hardware: nothing to install."}
    if status()["ready"]:
        return {"ok": True, "message": "Already installed."}
    if installing():
        return {"ok": True, "message": "Already installing."}
    if not bridge_available():
        return {"ok": False,
                "error": "This box is missing the part that installs them. "
                         "System → Tasks can install the system parts."}
    try:
        # A stale state file from a failed attempt would read as "already tried" to
        # anything that checks it; the root script rewrites it as soon as it starts.
        with open(REQUEST, "w") as f:
            f.write("%d\n" % int(time.time()))
    except OSError as e:
        return {"ok": False, "error": "Could not ask the system to install them: %s" % e}
    return {"ok": True, "message": "Installing — a minute or two."}


def wait_for_tools(fmt, data, cancelled, on_wait=None, timeout=25 * 60):
    """Block until the tools this disc needs are installed, asking for them if nobody
    has. Returns (True, "") or (False, why).

    Holding the disc is the right answer on a box that is about to be able to rip it:
    the first music CD on a box that updated from the web is very likely to arrive
    before the tools have, and "put it in again in five minutes" is a message for
    somebody who is not there.
    """
    ok, missing = ready(fmt, data)
    if ok:
        return True, ""
    asked = False
    started = time.time()
    while time.time() - started < timeout:
        if cancelled():
            return False, "cancelled"
        if not installing() and not asked:
            st = _read_state() or {}
            # One failed attempt this boot is not retried in a loop -- it is reported.
            if st.get("phase") == "error" and not asked:
                return False, "the CD tools couldn't be installed: %s" % (
                    st.get("message") or "unknown error")
            r = request_install()
            asked = True
            if not r.get("ok"):
                return False, r.get("error") or "the CD tools aren't installed"
        if on_wait:
            on_wait()
        time.sleep(5)
        ok, missing = ready(fmt, data)
        if ok:
            return True, ""
        st = _read_state() or {}
        if asked and st.get("phase") == "error":
            return False, "the CD tools couldn't be installed: %s" % (
                st.get("message") or "unknown error")
    return False, "the CD tools are still not installed (%s)" % missing


def start():
    """Ask for the CD tools once per boot if they are missing -- the same two-caller
    arrangement as the DVD tools (see backup.start)."""
    if P.MOCK or not P.IS_APPLIANCE:
        return

    def later():
        time.sleep(120)
        try:
            if status()["ready"] or _read_state() is not None:
                return
            if bridge_available():
                request_install()
        except Exception:
            pass

    threading.Thread(target=later, name="riparr-cdtools", daemon=True).start()


# ─────────────────────────────── the mock disc ───────────────────────────────
#
# RIPARR_MOCK_CD = audio | enhanced | unknown | data
#
# "audio" and "enhanced" are the two worked examples from MusicBrainz's disc ID
# documentation, so the mock drive holds discs that really exist and really look up.
# RIPARR_MOCK_CD_IMAGE=<file.cue> replaces the TOC with a real image's and makes the rip
# run the real cd-paranoia against it -- which is how the pipeline test exercises the
# actual tool rather than a stand-in.

_MOCK_TOCS = {
    "audio": (1, 6, 95462, [150, 15363, 32314, 46592, 63414, 80489], []),
    "enhanced": (1, 8, None, [150, 14109, 33586, 53077, 65781, 77892, 99174, 125974],
                 [8]),
    "unknown": (1, 4, 61234, [150, 13000, 29873, 45001], []),
    "data": (1, 1, 300000, [150], [1]),
}


def _mock_toc():
    image = os.environ.get("RIPARR_MOCK_CD_IMAGE")
    if image and image.lower().endswith(".cue"):
        return _image_toc(image)
    which = os.environ.get("RIPARR_MOCK_CD", "audio")
    first, last, leadout, offsets, data = _MOCK_TOCS.get(which, _MOCK_TOCS["audio"])
    if leadout is None:
        leadout = offsets[-1] + 30000
    return {"first": first, "last": last, "leadout": leadout - 150,
            "tracks": [{"number": n, "lba": o - 150, "audio": n not in data,
                        "preemphasis": False}
                       for n, o in zip(range(first, last + 1), offsets)]}


_IMAGE_TRACK = re.compile(r"^\s*(\d+):\s+\d\d:\d\d:\d\d\s+(\d+)\s+(audio|data|leadout)",
                          re.I)


def _image_toc(cue):
    """A cue image's TOC, read by cd-info -- for the tests, which have no drive."""
    try:
        p = subprocess.run(["cd-info", "--no-header", "--no-device-info", "-q",
                            "--cue-file", cue], capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    tracks, leadout = [], None
    for line in (p.stdout or "").splitlines():
        m = _IMAGE_TRACK.match(line)
        if not m:
            continue
        if m.group(3).lower() == "leadout":
            leadout = int(m.group(2))
        else:
            tracks.append({"number": int(m.group(1)), "lba": int(m.group(2)),
                           "audio": m.group(3).lower() == "audio", "preemphasis": False})
    if not tracks or leadout is None:
        return None
    return {"first": tracks[0]["number"], "last": tracks[-1]["number"],
            "leadout": leadout, "tracks": tracks}


def _mock_rip_track(row, wav, cancel, on_progress, expect):
    """A short real WAV, so the encoder downstream runs for real. Two seconds of a tone
    per track, whatever the TOC says, because a test does not want 700 MB of audio."""
    import math
    import struct
    import wave
    frames = 2 * 44100
    freq = 220 + 40 * int(row["number"])
    with wave.open(wav, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        chunk = []
        for i in range(frames):
            if cancel.is_set():
                raise MusicCancelled()
            v = int(6000 * math.sin(2 * math.pi * freq * i / 44100))
            chunk.append(struct.pack("<hh", v, v))
        w.writeframes(b"".join(chunk))
    if on_progress:
        on_progress(expect, expect)
    return {"skips": 0, "notes": {}}
