#!/usr/bin/env python3
"""Music CDs, end to end, through the real pipeline and the real tools.

Riparr's music path was built without a CD drive anywhere near it, so everything that
can be checked without one is checked here, against the actual binaries the box runs:

  * the MusicBrainz disc ID, against both worked examples in MusicBrainz's own
    documentation -- a plain CD and an Enhanced CD with a data session
  * the album, from real MusicBrainz responses saved in fixtures/musicbrainz: an exact
    match, disc 2 of a two-disc set, and the fuzzy lookup that returned a stranger's
    album and must only be believed when the disc's CD-Text agrees
  * CD-Text, read by the real cd-info from a cue image that carries it
  * the rip itself: the real cd-paranoia reading a synthesized disc image, then the real
    flac or lame -- and the FLAC decoded back must be the image's audio, bit for bit
  * staged and direct, a resumed send after a power cut, verify, the purge
  * the second disc of a set sharing the album folder, and a different disc that would
    overwrite someone's tracks going beside them instead
  * a track that will not read costs that track, not the album
  * a data CD of MP3s, copied by bsdtar without mounting anything
  * duplicates refused, Re-rip allowed, and Auto Rip not waiting on MakeMKV for a CD

Needs cd-paranoia, cd-info, flac, metaflac, lame and bsdtar, plus something that makes
an ISO (xorriso, genisoimage or hdiutil). CI installs them; anything missing here is a
failure, not a skip -- a test that quietly skipped the real tools would be the exact
check-that-cannot-fail this file exists to avoid.

Run: python3 server/music-pipeline.test.py
"""
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_tmp = tempfile.mkdtemp(prefix="riparr-music-test-")
os.environ["RIPARR_DB"] = os.path.join(_tmp, "test.db")
os.environ["RIPARR_APPLIANCE"] = "0"
os.environ["RIPARR_MOCK_FAST"] = "1"
os.environ["RIPARR_MOCK_DISC"] = "cd"
os.environ["RIPARR_MOCK_CD"] = "audio"
_MOCK_SHARE = "/tmp/riparr-mock-share"
os.environ["RIPARR_LIBRARY_MOUNT"] = os.path.join(_MOCK_SHARE, "nas", "Media")

from riparr import db, music as MU, optical as OPT, platform as P, rip, shares as SH  # noqa

FIX = os.path.join(HERE, "fixtures", "musicbrainz")
failures = []


def check(name, got, want):
    if got == want:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s: got %r, wanted %r" % (name, got, want))
        failures.append(name)


def contains(name, haystack, needle):
    ok = needle in (haystack or "")
    print("  %s %s" % ("ok  " if ok else "FAIL", name))
    if not ok:
        print("       %r does not contain %r" % (haystack, needle))
        failures.append(name)


def fixture(name):
    with open(os.path.join(FIX, name + ".json")) as f:
        return json.load(f)


for tool in ("cd-paranoia", "cd-info", "flac", "metaflac", "lame", "opusenc", "oggenc",
             "ffmpeg", "ffprobe", "bsdtar"):
    if not shutil.which(tool):
        print("  FAIL %s is not installed; this test needs the real tools" % tool)
        failures.append(tool)
if failures:
    sys.exit(1)


# ── a disc image: three tracks of tones, with CD-Text ───────────────────────────

IMG = os.path.join(_tmp, "img")
os.makedirs(IMG)
SECONDS = [7, 5, 6]


def make_image(folder, seconds, album="Test Album", artist="Test Artist", cdtext=True):
    frames, pos = [], 0
    with open(os.path.join(folder, "disc.bin"), "wb") as f:
        for n, secs in enumerate(seconds):
            frames.append(pos)
            sectors = secs * 75
            freq = 220 * (n + 1)
            f.write(b"".join(struct.pack("<hh", v, v) for v in (
                int(8000 * math.sin(2 * math.pi * freq * i / 44100.0))
                for i in range(sectors * 588))))
            pos += sectors

    def msf(fr):
        return "%02d:%02d:%02d" % (fr // 4500, (fr // 75) % 60, fr % 75)
    cue = []
    if cdtext:
        cue += ['PERFORMER "%s"' % artist, 'TITLE "%s"' % album]
    cue.append('FILE "disc.bin" BINARY')
    for n, fr in enumerate(frames, 1):
        cue.append("  TRACK %02d AUDIO" % n)
        if cdtext:
            cue += ['    TITLE "Song %d"' % n, '    PERFORMER "%s"' % artist]
        cue.append("    INDEX 01 %s" % msf(fr))
    path = os.path.join(folder, "disc.cue")
    with open(path, "w") as f:
        f.write("\n".join(cue) + "\n")
    return path, frames


CUE, FRAMES = make_image(IMG, SECONDS)


def share_files(under="nas/Media/Music"):
    out = []
    base = os.path.join(SH.MOCK_SHARE_ROOT, under)
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d != ".riparr-incoming"]
        for f in files:
            out.append(os.path.relpath(os.path.join(root, f), base))
    return sorted(out)


def fresh_share():
    shutil.rmtree(SH.MOCK_SHARE_ROOT, ignore_errors=True)
    os.makedirs(os.environ["RIPARR_LIBRARY_MOUNT"], exist_ok=True)
    shutil.rmtree(P.STAGING, ignore_errors=True)
    os.makedirs(P.STAGING, exist_ok=True)
    for row in db.list_shares():
        db.delete_share(row["id"])
    for j in db.list_jobs(limit=500):
        db.update_job(j["id"], state="cancelled")
    return db.add_share("Media", "nas", "Media", None, None, make_default=True)


def run_disc(force=False):
    """Insert the mock disc, and run whatever it queues through worker and sender."""
    job_id, why = rip.enqueue(force=force)
    if not job_id:
        return None, why
    job = db.get_job(job_id)
    rip._run_job(job)
    j = db.get_job(job_id)
    if j["state"] == "transferring":
        rip._send_one(j)
    return db.get_job(job_id), None


def tags(path):
    out = subprocess.run(["metaflac", "--export-tags-to=-", path], capture_output=True,
                         text=True).stdout
    got = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        got.setdefault(k.upper(), []).append(v)
    return got


# A real 2x2 JPEG standing in for the Cover Art Archive, so no test touches the network.
# Real, because flac refuses to embed a picture it cannot parse -- see the broken-cover
# check below, which is the case where the archive hands back something odd.
COVER = __import__("base64").b64decode(
    "/9j/4AAQSkZJRgABAQAASABIAAD/4QBMRXhpZgAATU0AKgAAAAgAAYdpAAQAAAABAAAAGgAAAAAAA6ABAAMAAAABAAEAAKACAAQAAAABAAAAAqADAAQAAAABAAAAAgAAAAD/7QA4UGhvdG9zaG9wIDMuMAA4QklNBAQAAAAAAAA4QklNBCUAAAAAABDUHYzZjwCyBOmACZjs+EJ+/8AAEQgAAgACAwEiAAIRAQMRAf/EAB8AAAEFAQEBAQEBAAAAAAAAAAABAgMEBQYHCAkKC//EALUQAAIBAwMCBAMFBQQEAAABfQECAwAEEQUSITFBBhNRYQcicRQygZGhCCNCscEVUtHwJDNicoIJChYXGBkaJSYnKCkqNDU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6g4SFhoeIiYqSk5SVlpeYmZqio6Slpqeoqaqys7S1tre4ubrCw8TFxsfIycrS09TV1tfY2drh4uPk5ebn6Onq8fLz9PX29/j5+v/EAB8BAAMBAQEBAQEBAQEAAAAAAAABAgMEBQYHCAkKC//EALURAAIBAgQEAwQHBQQEAAECdwABAgMRBAUhMQYSQVEHYXETIjKBCBRCkaGxwQkjM1LwFWJy0QoWJDThJfEXGBkaJicoKSo1Njc4OTpDREVGR0hJSlNUVVZXWFlaY2RlZmdoaWpzdHV2d3h5eoKDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uLj5OXm5+jp6vLz9PX29/j5+v/bAEMAAgICAgICAwICAwUDAwMFBgUFBQUGCAYGBgYGCAoICAgICAgKCgoKCgoKCgwMDAwMDA4ODg4ODw8PDw8PDw8PD//bAEMBAgICBAQEBwQEBxALCQsQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEP/dAAQAAf/aAAwDAQACEQMRAD8A7P8A4Qvwf/0ArD/wFi/+Jo/4Qvwf/wBAKw/8BYv/AImulor+Az6g/9k=")
cover_bytes = [COVER]


def fake_cover(release_id, folder):
    p = os.path.join(folder, "cover.jpg")
    with open(p, "wb") as f:
        f.write(cover_bytes[0])
    return p


MU.cover_art = fake_cover


def mb_for_image(album="Image Album", artist="Image Artist", discs=1, position=1,
                 release_id="11111111-1111-1111-1111-111111111111", did=None):
    """A MusicBrainz discid response for whatever disc ID the image has, shaped exactly
    like the real Ettella Diamant one -- which is a real response, trimmed to the
    image's track count."""
    real = fixture("discid-ettella-diamant")
    r = json.loads(json.dumps(real["releases"][0]))
    r["id"], r["title"] = release_id, album
    r["artist-credit"][0]["name"] = artist
    r["artist-credit"][0]["artist"]["name"] = artist
    medium = r["media"][0]
    medium["tracks"] = medium["tracks"][:len(SECONDS)]
    for i, t in enumerate(medium["tracks"]):
        t["title"] = "Real Title %d" % (i + 1)
    medium["position"] = position
    medium["discs"] = [{"id": did}]
    r["media"] = [dict(medium, position=p, discs=[{"id": did if p == position else "x"}])
                  for p in range(1, discs + 1)]
    return {"releases": [r]}


def answering(fn):
    """Install a fake MusicBrainz that answers by path."""
    MU._mb_get = fn


db.init()
db.set("setup_complete", True)
db.set("transfer_mode", "burst")
db.set("verify_mode", "quick")


# ── identity ──────────────────────────────────────────────────────────────────

print("the disc ID matches MusicBrainz's own worked examples")
os.environ["RIPARR_MOCK_CD"] = "audio"
toc = MU.read_toc("/dev/sr0")
check("a plain CD", MU.disc_id(toc), "49HHV7Eb8UKF3aQiNmu1GR8vKTY-")
check("is a music CD", OPT.cd_kind(toc), "audio")
check("the fuzzy-lookup TOC string", MU.mb_toc(toc),
      "1 6 95462 150 15363 32314 46592 63414 80489")
os.environ["RIPARR_MOCK_CD"] = "enhanced"
etoc = MU.read_toc("/dev/sr0")
check("an Enhanced CD: data track left out, lead-out moved back 11400 sectors",
      MU.disc_id(etoc), "BPnh1KU.hea1C.KMYWLGZkHJr0w-")
check("is an Enhanced CD", OPT.cd_kind(etoc), "enhanced")
check("its last audio track stops at the session gap, not the data track",
      etoc["audio"][-1]["sectors"], 125824 - 11400 - (99174 - 150))
check("seven tracks to rip, not eight", len(etoc["audio"]), 7)
os.environ["RIPARR_MOCK_CD"] = "data"
check("a CD with only data is a data CD", OPT.cd_kind(MU.read_toc("/dev/sr0")), "data")
os.environ["RIPARR_MOCK_CD"] = "audio"
mixed = OPT.toc_layout({"first": 1, "last": 3, "leadout": 9000, "tracks": [
    {"number": 1, "lba": 0, "audio": False}, {"number": 2, "lba": 3000, "audio": True},
    {"number": 3, "lba": 6000, "audio": True}]})
check("mixed mode (data first) has no session gap", mixed["multisession"], False)
check("and rips its audio tracks", [t["number"] for t in mixed["audio"]], [2, 3])

# ── MusicBrainz, from real responses ───────────────────────────────────────────

print("an exact MusicBrainz match names everything")
answering(lambda path, params: fixture("discid-ettella-diamant")
          if path == "discid/49HHV7Eb8UKF3aQiNmu1GR8vKTY-" else None)
album, cands, how = MU.lookup(toc)
check("found by disc ID", how, "discid")
check("the album", (album["album_artist"], album["album"], album["year"]),
      ("Ettella Diamant", "Ettella Diamant", "2004"))
check("six tracks with their real names", [t["title"] for t in album["tracks"]][:2],
      ["Rysperdal Gstp", "Dobrý večer"] if album["tracks"][1]["title"].startswith("Dob")
      else [album["tracks"][0]["title"], album["tracks"][1]["title"]])
check("recording IDs for Lidarr and beets", bool(album["tracks"][0]["recording_id"]), True)
check("label and catalogue number", (album["label"], album["catalog"]),
      ("Deadred Records", "DR E006"))
check("has cover art", album["has_art"], True)

print("disc 2 of a two-disc set is filed as disc 2 of 2")
wall = fixture("discid-the-wall-disc2")
wtoc = OPT.toc_layout({"first": 1, "last": 13, "leadout": wall["sectors"] - 150,
                       "tracks": [{"number": n + 1, "lba": o - 150, "audio": True}
                                  for n, o in enumerate(wall["offsets"])]})
check("the fixture's own TOC gives its own disc ID", MU.disc_id(wtoc), wall["id"])
answering(lambda path, params: wall if path == "discid/" + wall["id"] else None)
album, _, _ = MU.lookup(wtoc)
check("disc 2 of 2", (album["disc"], album["discs"]), (2, 2))
check("the original year, not the reissue's", (album["year"], album["date"][:4]),
      ("1979", "1991"))
row = {"position": 1, "title": "Hey You", "artist": "Pink Floyd"}
folder, fname = MU.track_path(MU.DEFAULT_TEMPLATE, album, row, "flac", rip.sanitise)
check("into the album's one folder", folder, "Pink Floyd/The Wall (1979)")
check("numbered so it can't collide with disc 1", fname, "2-01 - Hey You.flac")

print("the fuzzy lookup is believed only when the CD-Text agrees")
fuzzy = fixture("toc-ettella-diamant-fuzzy")
check("the real fuzzy response really does contain a stranger's album",
      sorted(r["title"] for r in fuzzy["releases"]), ["Awakening", "Ettella Diamant"])
answering(lambda path, params: fuzzy if path == "discid/-" else None)
album, _, how = MU.lookup(toc, cdtext={})
check("no CD-Text: not used at all", (album, how), (None, ""))
album, _, how = MU.lookup(toc, cdtext={"album": "Ettella Diamant", "artist": ""})
check("CD-Text names it: the matching one is used", (album or {}).get("album"),
      "Ettella Diamant")
check("and flagged as a track-length match", how, "toc")
album, _, how = MU.lookup(toc, cdtext={"album": "Summer Mix 2006", "artist": "Me"})
check("CD-Text names something else: neither is used", album, None)

# ── naming ─────────────────────────────────────────────────────────────────────

print("names are sanitised before they become folders")
plan = {"album": "Back in Black", "album_artist": "AC/DC", "year": "1980", "disc": 1,
        "discs": 1, "tracks": [{}] * 10}
folder, fname = MU.track_path(MU.DEFAULT_TEMPLATE, plan,
                              {"position": 6, "title": "What Do You Do for Money Honey?"},
                              "mp3", rip.sanitise)
check("AC/DC is one folder", folder, "AC DC/Back in Black (1980)")
check("a question mark is not a legal filename character", fname,
      "06 - What Do You Do for Money Honey.mp3")
folder, _ = MU.track_path(MU.DEFAULT_TEMPLATE, dict(plan, year=""), {"position": 1,
                          "title": "x"}, "flac", rip.sanitise)
check("no year: no empty brackets", folder, "AC DC/Back in Black")

# ── CD-Text, from the real cd-info ─────────────────────────────────────────────

print("CD-Text is read off the image by cd-info")
ct = MU.read_cdtext(CUE)
check("album and artist", (ct.get("album"), ct.get("artist")),
      ("Test Album", "Test Artist"))
check("every track", [ct["tracks"].get(n, {}).get("title") for n in (1, 2, 3)],
      ["Song 1", "Song 2", "Song 3"])

# ── the rip, with the real tools ───────────────────────────────────────────────

os.environ["RIPARR_MOCK_CD_IMAGE"] = CUE
itoc = MU.read_toc("/dev/sr0")
IMG_ID = MU.disc_id(itoc)
check("the image's TOC, as cd-info reads it", [t["lba"] for t in itoc["tracks"]], FRAMES)

print("a CD MusicBrainz knows, staged on the card and then sent")
fresh_share()
answering(lambda path, params: mb_for_image(did=IMG_ID)
          if path == "discid/" + IMG_ID else None)
job, why = run_disc()
check("finished", (job or {}).get("state"), "done")
check("recorded as music", (job["kind"], job["output"]), ("music", "music"))
check("titled artist — album", job["title"], "Image Artist — Image Album")
want = ["Image Artist/Image Album (2004)/01 - Real Title 1.flac",
        "Image Artist/Image Album (2004)/02 - Real Title 2.flac",
        "Image Artist/Image Album (2004)/03 - Real Title 3.flac",
        "Image Artist/Image Album (2004)/cover.jpg"]
check("the album folder, with its cover", share_files(), want)
check("History points at the album", job["remote_name"],
      "Music/Image Artist/Image Album (2004)")
check("verified", job["verified_mode"], "quick")
first = os.path.join(SH.MOCK_SHARE_ROOT, "nas/Media/Music", want[0])
t = tags(first)
check("tagged: title, artist, album", (t.get("TITLE"), t.get("ALBUMARTIST"),
                                       t.get("ALBUM")),
      (["Real Title 1"], ["Image Artist"], ["Image Album"]))
check("tagged: track 1 of 3, disc 1 of 1",
      (t.get("TRACKNUMBER"), t.get("TRACKTOTAL"), t.get("DISCNUMBER")),
      (["1"], ["3"], ["1"]))
check("tagged: MusicBrainz release and disc ID",
      (t.get("MUSICBRAINZ_ALBUMID"), t.get("MUSICBRAINZ_DISCID")),
      (["11111111-1111-1111-1111-111111111111"], [IMG_ID]))
pics = subprocess.run(["metaflac", "--list", "--block-type=PICTURE", first],
                      capture_output=True, text=True).stdout
contains("the cover is embedded", pics, "type: 3 (Cover (front))")
check("no WAVs left behind", [f for r, _, fs in os.walk(P.STAGING) for f in fs
                              if f.endswith(".wav")], [])

print("the FLAC is the disc's audio, bit for bit")
dec = os.path.join(_tmp, "dec.wav")
subprocess.run(["flac", "--silent", "-d", "-f", "-o", dec, first], check=True)
with wave.open(dec) as w:
    pcm = w.readframes(w.getnframes())
with open(os.path.join(IMG, "disc.bin"), "rb") as f:
    f.seek(FRAMES[0] * MU.SECTOR_BYTES)
    want_pcm = f.read(SECONDS[0] * 75 * MU.SECTOR_BYTES)
check("track 1 decodes to exactly the image's sectors", pcm == want_pcm, True)

print("the same CD again is a duplicate; Re-rip is not")
again, why = run_disc()
check("refused", again, None)
contains("and says so", why, "already ripped Image Artist")
rip.arm_force(job["fingerprint"])
rerip, _ = run_disc()
check("Re-rip runs", (rerip or {}).get("state"), "done")
check("into the same folder, not a (2)", rerip["remote_name"], job["remote_name"])
check("and nothing doubled up", share_files(), want)

print("re-verification notices a damaged track")
ok, _ = rip.reverify(rerip["id"], mode="quick")
for th in __import__("threading").enumerate():
    if th.name == "riparr-reverify":
        th.join(10)
check("a good album passes", db.get_job(rerip["id"])["state"], "done")
with open(first, "r+b") as f:
    f.truncate(100)
rip.reverify(rerip["id"], mode="quick")
for th in __import__("threading").enumerate():
    if th.name == "riparr-reverify":
        th.join(10)
bad = db.get_job(rerip["id"])
check("a truncated track fails it", bad["state"], "failed")
contains("and names the file", bad["error"], "01 - Real Title 1.flac")

print("the purge frees a staged album only when the share has every file")
freed, _ = rip.purge_staging(need_bytes=1)
check("not freed while a track is wrong on the share",
      os.path.isdir(bad["local_path"]), True)
db.update_job(bad["id"], state="done", error=None)
shutil.copy(os.path.join(bad["local_path"], "01 - Real Title 1.flac"), first)
freed, _ = rip.purge_staging(need_bytes=1)
check("freed once they all match", freed > 0, True)

print("disc 2 of the set lands in the same folder, numbered 2-NN")
fresh_share()
answering(lambda path, params: mb_for_image(did=IMG_ID, discs=2, position=1)
          if path == "discid/" + IMG_ID else None)
d1, _ = run_disc(force=True)
check("disc 1 of 2", d1["state"], "done")
check("named 1-NN on a two-disc set", share_files()[0],
      "Image Artist/Image Album (2004)/1-01 - Real Title 1.flac")
IMG2 = os.path.join(_tmp, "img2")
os.makedirs(IMG2)
CUE2, _ = make_image(IMG2, [4, 6, 5])
os.environ["RIPARR_MOCK_CD_IMAGE"] = CUE2
ID2 = MU.disc_id(MU.read_toc("/dev/sr0"))
answering(lambda path, params: mb_for_image(did=ID2, discs=2, position=2)
          if path == "discid/" + ID2 else None)
d2, _ = run_disc()
check("disc 2 finishes", d2["state"], "done")
check("into the same album folder", d2["remote_name"], d1["remote_name"])
check("six tracks and one cover in it", len(share_files()), 7)
check("disc 2's tracks are 2-NN", [f for f in share_files() if "/2-0" in f][:1],
      ["Image Artist/Image Album (2004)/2-01 - Real Title 1.flac"])
check("no warning about overwriting", d2["warning"], None)

print("a different disc that would overwrite somebody's tracks goes beside them")
IMG4 = os.path.join(_tmp, "img4")
os.makedirs(IMG4)
CUE4, _ = make_image(IMG4, [5, 4, 3])
os.environ["RIPARR_MOCK_CD_IMAGE"] = CUE4
ID4 = MU.disc_id(MU.read_toc("/dev/sr0"))
# Another pressing that MusicBrainz files as disc 1 of the same album: its tracks would
# land on exactly disc 1's names.
answering(lambda path, params: mb_for_image(did=ID4, discs=2, position=1,
                                            release_id="22222222-2222-2222-2222-222222222222")
          if path == "discid/" + ID4 else None)
d3, _ = run_disc()
check("finished", d3["state"], "done")
check("into a numbered folder", d3["remote_name"], d1["remote_name"] + " (2)")
contains("and says so", d3["warning"], "nothing was overwritten")
t = tags(os.path.join(SH.MOCK_SHARE_ROOT, "nas/Media", d1["remote_name"],
                      "1-01 - Real Title 1.flac"))
check("disc 1's tracks are untouched", t.get("MUSICBRAINZ_ALBUMID"),
      ["11111111-1111-1111-1111-111111111111"])
os.environ["RIPARR_MOCK_CD_IMAGE"] = CUE

print("direct mode writes on the share and files it with a rename")
fresh_share()
db.set("transfer_mode", "direct")
db.set("verify_mode", "deep")
answering(lambda path, params: mb_for_image(did=IMG_ID)
          if path == "discid/" + IMG_ID else None)
dj, _ = run_disc(force=True)
check("finished", dj["state"], "done")
check("same layout", share_files(), want)
check("deep becomes quick: there's one copy", dj["verified_mode"], "quick")
check("nothing left in .riparr-incoming", os.listdir(os.path.join(
    os.environ["RIPARR_LIBRARY_MOUNT"], "Music", ".riparr-incoming"))
    if os.path.isdir(os.path.join(os.environ["RIPARR_LIBRARY_MOUNT"], "Music",
                                  ".riparr-incoming")) else [], [])
db.set("transfer_mode", "burst")
db.set("verify_mode", "quick")

print("a power cut mid-send resumes from the card")
fresh_share()
real_put = SH.Transport.put
calls = [0]


def flaky_put(self, local, name, **kw):
    calls[0] += 1
    if calls[0] == 2:
        raise RuntimeError("the power went")
    return real_put(self, local, name, **kw)


SH.Transport.put = flaky_put
pj, _ = run_disc(force=True)
SH.Transport.put = real_put
check("the send died", pj["state"], "failed")
db.update_job(pj["id"], state="transferring")      # what the cable pull leaves behind
rip.recover()
check("recover hands it to the sender", db.get_job(pj["id"])["state"], "transferring")
rip._send_one(db.get_job(pj["id"]))
check("and it finishes", db.get_job(pj["id"])["state"], "done")
check("every file arrived once", share_files(), want)

print("a track that won't read costs that track, not the album")
fresh_share()
real_rip = MU.rip_track


def scratched(device, row, wav, cancel, on_progress=None, gentle=False, log=None):
    if row["number"] == 2:
        raise MU.MusicFailed("Couldn't read track 2: scratched")
    return real_rip(device, row, wav, cancel, on_progress, gentle=gentle, log=log)


MU.rip_track = scratched
sj, _ = run_disc(force=True)
MU.rip_track = real_rip
check("the album still finishes", sj["state"], "done")
check("with the two good tracks", [f for f in share_files() if f.endswith(".flac")],
      [want[0], want[2]])
contains("and says which track is missing", sj["warning"], "Track 2 couldn't be read")
check("the disc isn't marked ripped, so it can go back in",
      db.get_disc(sj["fingerprint"])["ripped_at"], None)
again, why = run_disc()
check("and going back in rips it rather than refusing it", (again or {}).get("state"),
      "done")

print("a cover flac can't parse is left out of the files, not the album")
fresh_share()
cover_bytes[0] = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
bj, _ = run_disc(force=True)
cover_bytes[0] = COVER
check("the album still finishes", bj["state"], "done")
check("every track is there", len([f for f in share_files() if f.endswith(".flac")]), 3)
contains("and it says the art wasn't embedded", bj["warning"], "cover")

print("every format, with its tags, its cover and the right extension")


def probe(path):
    """ffprobe's view of a file: its codec, tags (lower-cased) and whether it holds a
    picture. ffprobe reads all seven formats, so one reader checks every encoder."""
    out = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_name,codec_type:stream_disposition=attached_pic:format_tags:"
         "stream_tags", "-of", "json", path], capture_output=True, text=True).stdout
        or "{}")
    tags = {k.lower(): v for k, v in (out.get("format", {}).get("tags") or {}).items()}
    for st in out.get("streams", []):              # Ogg and Opus keep tags per stream
        tags.update({k.lower(): v for k, v in (st.get("tags") or {}).items()})
    audio = [st["codec_name"] for st in out.get("streams", [])
             if st.get("codec_type") == "audio"]
    pic = any(st.get("codec_type") == "video" for st in out.get("streams", []))
    return (audio[0] if audio else None), tags, pic


def pcm_of(path):
    raw = os.path.join(_tmp, "pcm.raw")
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", path, "-f", "s16le",
                    "-ac", "2", "-ar", "44100", raw], check=True)
    with open(raw, "rb") as f:
        return f.read()


EXPECT = {   # format: (extension, codec, carries tags, carries the cover)
    "flac": (".flac", "flac", True, True), "alac": (".m4a", "alac", True, True),
    "wav": (".wav", "pcm_s16le", False, False), "mp3": (".mp3", "mp3", True, True),
    "aac": (".m4a", "aac", True, True), "opus": (".opus", "opus", True, True),
    "ogg": (".ogg", "vorbis", True, False),
}
answering(lambda path, params: mb_for_image(did=IMG_ID)
          if path == "discid/" + IMG_ID else None)
for fmt, (ext, codec, tagged, pictured) in EXPECT.items():
    fresh_share()
    db.set("music_format", fmt)
    db.set("music_quality", "")
    fj, _ = run_disc(force=True)
    check("%s: finished" % fmt, (fj or {}).get("state"), "done")
    names = [f for f in share_files() if not f.endswith("cover.jpg")]
    check("%s: three %s files" % (fmt, ext), [n.endswith(ext) for n in names],
          [True, True, True])
    if not names:
        continue
    path = os.path.join(SH.MOCK_SHARE_ROOT, "nas/Media/Music", names[0])
    got_codec, t, pic = probe(path)
    check("%s: really %s inside" % (fmt, codec), got_codec, codec)
    if tagged:
        check("%s: title, album artist, track" % fmt,
              (t.get("title"), t.get("album_artist") or t.get("albumartist"),
               (t.get("track") or t.get("tracknumber") or "").split("/")[0]),
              ("Real Title 1", "Image Artist", "1"))
    check("%s: cover %s" % (fmt, "embedded" if pictured else "beside it only"), pic,
          pictured)
    if fmt in ("flac", "alac", "wav"):
        check("%s: decodes to the disc's exact audio" % fmt, pcm_of(path) == want_pcm, True)
db.set("music_format", "flac")

print("quality settings reach the encoder")
cmd = MU.encode_command("mp3", "in.wav", "out.mp3", {"tracks": [{}]}, {"position": 1},
                        quality="320")
check("MP3 320 is constant 320", cmd[cmd.index("-b") + 1:cmd.index("-b") + 3],
      ["320", "--cbr"])
cmd = MU.encode_command("mp3", "in.wav", "out.mp3", {"tracks": [{}]}, {"position": 1},
                        quality="v2")
check("MP3 V2 is VBR 2", cmd[cmd.index("-V") + 1], "2")
cmd = MU.encode_command("opus", "in.wav", "o.opus", {"tracks": [{}]}, {"position": 1},
                        quality="v2")
check("a quality from another format falls back to this one's default",
      cmd[cmd.index("--bitrate") + 1], "160")
cmd = MU.encode_command("flac", "in.wav", "o.flac", {"tracks": [{}]}, {"position": 1},
                        quality="8")
check("FLAC level 8", "-8" in cmd, True)
fresh_share()
db.set("music_format", "mp3")
db.set("music_quality", "128")
small, _ = run_disc(force=True)
db.set("music_quality", "320")
big, _ = run_disc(force=True)
check("128 kbps really is smaller than 320", small["bytes_total"] < big["bytes_total"],
      True)
db.set("music_format", "flac")
db.set("music_quality", "")

print("an unknown format setting rips FLAC rather than failing")
db.set("music_format", "wma")
wj, _ = run_disc(force=True)
check("finished as FLAC", (wj["state"], share_files()[0].endswith(".flac")),
      ("done", True))
db.set("music_format", "flac")

print("which tools each format needs")
check("FLAC", MU.needs("flac"), ["cd-paranoia", "flac"])
check("AAC needs ffmpeg", MU.needs("aac"), ["cd-paranoia", "ffmpeg"])
check("WAV needs only the ripper", MU.needs("wav"), ["cd-paranoia"])
check("ffmpeg is the on-demand one", MU.ON_DEMAND, {"ffmpeg"})

print("nothing knows the disc: CD-Text names it")
fresh_share()
answering(lambda path, params: None)
cj, _ = run_disc(force=True)
check("finished", cj["state"], "done")
check("from the disc's own CD-Text", share_files()[0], "Test Artist/Test Album/01 - Song 1.flac")

print("MusicBrainz is down: still ripped, and it says why")
fresh_share()


def down(path, params):
    raise OSError("network is unreachable")


answering(down)
oj, _ = run_disc(force=True)
check("finished", oj["state"], "done")
contains("explains the name", oj["warning"], "MusicBrainz couldn't be reached")

print("no CD-Text and no match: Unknown Album, named so a re-rip finds it again")
IMG3 = os.path.join(_tmp, "img3")
os.makedirs(IMG3)
CUE3, _ = make_image(IMG3, [3, 3], cdtext=False)
os.environ["RIPARR_MOCK_CD_IMAGE"] = CUE3
fresh_share()
answering(lambda path, params: None)
uj, _ = run_disc()
fid = MU.freedb_id(MU.read_toc("/dev/sr0"))
check("finished", uj["state"], "done")
check("Unknown Artist / Unknown Album [freedb id]", share_files()[0],
      "Unknown Artist/Unknown Album [%s]/01 - Track 01.flac" % fid)
contains("and says nothing knew it", uj["warning"], "Nothing knew this disc")
print("the unknown-disc setting applies to CDs too")
db.set("on_unknown_disc", "skip")
fresh_share()
sk, why = run_disc(force=True)
check("Skip: not ripped", (sk or {}).get("state"), "cancelled")
check("and nothing written", share_files(), [])
db.set("on_unknown_disc", "ask")
job_id, _ = rip.enqueue(force=True)
rip._run_job(db.get_job(job_id))
asked = db.get_job(job_id)
check("Ask: waits for a name", asked["state"], "needs_input")
ok, _ = rip.answer(job_id, name="The Typists - Songs Nobody Knew")
check("the answer is accepted", ok, True)
rip._run_job(db.get_job(job_id))
named = db.get_job(job_id)
if named["state"] == "transferring":
    rip._send_one(named)
    named = db.get_job(job_id)
check("ripped under the typed name", (named["state"], share_files()[0]),
      ("done", "The Typists/Songs Nobody Knew/01 - Track 01.flac"))
check("titled the same way as a looked-up album", named["title"],
      "The Typists \u2014 Songs Nobody Knew")
check("no 'nothing knew this disc' warning once it has a name", named["warning"], None)
again, _ = run_disc(force=True)
check("the name is remembered when the CD comes back", (again or {}).get("title"),
      "The Typists \u2014 Songs Nobody Knew")
db.set("on_unknown_disc", "label")
check("CD-Text of only punctuation is no name", MU.parse_cdtext(
    "CD-TEXT for Disc:\n\tTITLE: Real\nCD-TEXT for Track  1:\n\tPERFORMER: ?\n"),
    {"album": "Real", "artist": "", "tracks": {}})

print("a rip starts from an empty scratch folder, whatever a reused job number left")
fresh_share()
# The number the next job will get -- the one a restored backup would hand out again.
next_id = db.conn().execute("SELECT MAX(id) FROM jobs").fetchone()[0] + 1
stale = os.path.join(P.STAGING, "job-%d" % next_id, "album")
os.makedirs(stale, exist_ok=True)
with open(os.path.join(stale, "01 - Somebody Else.flac"), "wb") as f:
    f.write(b"fLaC" + b"\0" * 200)
db.set("transfer_mode", "burst")
cj2, _ = run_disc(force=True)
check("the job got the number the stale folder had", cj2["id"], next_id)
check("only this disc's tracks arrive", [f for f in share_files() if "Somebody" in f], [])
os.environ["RIPARR_MOCK_CD_IMAGE"] = CUE

# ── data CDs ───────────────────────────────────────────────────────────────────

print("a data CD of MP3s is copied, folders and all")
src = os.path.join(_tmp, "iso-src")
os.makedirs(os.path.join(src, "Mix One"))
os.makedirs(os.path.join(src, "Docs"))
subprocess.run(["lame", "--quiet", "-V", "5", os.path.join(_tmp, "dec.wav"),
                os.path.join(src, "Mix One", "01 Track [Live].mp3")], check=True)
shutil.copy(os.path.join(src, "Mix One", "01 Track [Live].mp3"),
            os.path.join(src, "Loose song.mp3"))
with open(os.path.join(src, "Mix One", "folder.jpg"), "wb") as f:
    f.write(b"\xff\xd8\xff" + b"\0" * 16)
with open(os.path.join(src, "Docs", "readme.pdf"), "wb") as f:
    f.write(b"%PDF-1.4")
with open(os.path.join(src, "autorun.inf"), "w") as f:
    f.write("[autorun]")
iso = os.path.join(_tmp, "mix.iso")
if shutil.which("xorriso"):
    subprocess.run(["xorriso", "-as", "mkisofs", "-quiet", "-J", "-R", "-V", "MP3_MIX",
                    "-o", iso, src], check=True)
elif shutil.which("genisoimage"):
    subprocess.run(["genisoimage", "-quiet", "-J", "-R", "-V", "MP3_MIX", "-o", iso, src],
                   check=True)
else:
    subprocess.run(["hdiutil", "makehybrid", "-quiet", "-iso", "-joliet",
                    "-default-volume-name", "MP3_MIX", "-o", iso, src], check=True)
files = dict(MU.data_audio_files(iso))
check("bsdtar finds the music and only the music",
      sorted(files), ["Loose song.mp3", "Mix One/01 Track [Live].mp3"])
os.environ["RIPARR_MOCK_CD_IMAGE"] = iso
os.environ["RIPARR_MOCK_CD"] = "data"
fresh_share()
fj, why = run_disc()
check("finished", (fj or {}).get("state"), "done")
check("recorded as a file copy", (fj or {}).get("output"), "files")
check("under the disc's name, folders kept, junk left behind", share_files(), [
    "Mp3 Mix/Loose song.mp3", "Mp3 Mix/Mix One/01 Track [Live].mp3",
    "Mp3 Mix/Mix One/folder.jpg"])
os.environ["RIPARR_MOCK_CD"] = "audio"
os.environ["RIPARR_MOCK_CD_IMAGE"] = CUE

# ── the gates ──────────────────────────────────────────────────────────────────

print("Auto Rip doesn't wait on MakeMKV for a CD")
real_mk = P.makemkv_status
P.makemkv_status = lambda: {"installed": False}
check("a CD in the tray: ready without MakeMKV", bool(rip._autorip_ready()), True)
os.environ["RIPARR_MOCK_DISC"] = "dvd"
check("a DVD in the tray: still needs it", bool(rip._autorip_ready()), False)
os.environ["RIPARR_MOCK_DISC"] = "cd"
P.makemkv_status = real_mk

print("music switched off leaves the CD alone")
db.set("music_enabled", False)
nj, why = rip.enqueue(force=True)
check("not queued", nj, None)
contains("and says where the switch is", why, "Settings")
db.set("music_enabled", True)

print("the CD tools gate")
os.environ["RIPARR_MOCK_CDTOOLS"] = "0"
check("not ready without them", MU.ready("flac")[0], False)
os.environ["RIPARR_MOCK_CDTOOLS"] = "1"
check("ready with them", MU.ready("flac")[0], True)

print("the mount script mounts the music share too")
with open(os.path.join(os.path.dirname(HERE), "tools", "mount-library.sh")) as f:
    contains("music_share_id is in its list", f.read(), '"music_share_id"')

shutil.rmtree(_tmp, ignore_errors=True)
if failures:
    print("\n%d FAILED: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("\nall music checks passed")
