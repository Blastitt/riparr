#!/usr/bin/env python3
"""Auto Rip, one kind of disc at a time: films, seasons and data discs, end to end.

Each kind has its own "Rip it / Show me first / Leave it alone", films and seasons
have their extras, and anything that is none of the three is saved as an ISO image.
Run against the mock drive and mock share, like the other pipeline tests; music's
half of this lives in music-pipeline.test.py, which has the real CD tools.

Run: python3 server/autorip-kinds.test.py
"""
import os
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_tmp = tempfile.mkdtemp(prefix="riparr-kinds-test-")
os.environ["RIPARR_DB"] = os.path.join(_tmp, "test.db")
os.environ["RIPARR_APPLIANCE"] = "0"
os.environ["RIPARR_MOCK_FAST"] = "1"
os.environ["RIPARR_MOCK_CONTENT"] = "movie"
# Direct mode needs the library mounted; this is where the mock share lives.
os.environ["RIPARR_LIBRARY_MOUNT"] = "/tmp/riparr-mock-share/nas/Media"

from riparr import db, platform as P, rip, shares as SH  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s: got %r, wanted %r" % (name, got, want))
        failures.append(name)


def contains(name, haystack, needle):
    check(name, needle in (haystack or ""), True)


def share_files():
    out = []
    for root, dirs, files in os.walk(SH.MOCK_SHARE_ROOT):
        dirs[:] = [d for d in dirs if d != ".riparr-incoming"]
        for f in files:
            out.append(os.path.relpath(os.path.join(root, f), SH.MOCK_SHARE_ROOT))
    return sorted(out)


def fresh_share():
    shutil.rmtree(SH.MOCK_SHARE_ROOT, ignore_errors=True)
    os.makedirs(os.environ["RIPARR_LIBRARY_MOUNT"], exist_ok=True)
    for row in db.list_shares():
        db.delete_share(row["id"])
    db.conn().execute("DELETE FROM discs")
    db.conn().commit()
    return db.add_share("Media", "nas", "Media", None, None, make_default=True)


ejects = []
P.eject = lambda *a, **k: ejects.append(1) or {"ok": True}


def new_job(label, manual=False):
    os.environ["RIPARR_MOCK_LABEL"] = label
    job_id = db.create_job(state="queued", disc_label=label, fingerprint="fp-" + label,
                           manual=1 if manual else 0)
    return db.get_job(job_id)


def run(job):
    """Identify and run a job the way the worker does; None if it stopped to ask."""
    rip._run_job(job)
    j = db.get_job(job["id"])
    if j["state"] == "transferring":
        rip._send_one(j)
        j = db.get_job(job["id"])
    return j


db.init()
db.set("setup_complete", True)
db.set("tv_metadata", False)
db.set("transfer_mode", "burst")
db.set("verify_mode", "quick")

# ── films ─────────────────────────────────────────────────────────────────────

print("a film Auto Rip is set to leave alone is ejected, and says why")
fresh_share()
db.set("auto_movie", "ignore")
n = len(ejects)
left = run(new_job("THE_MATRIX"))
check("not ripped", left["state"], "cancelled")
contains("History says why", left["error"], "Auto Rip is set to ignore films")
check("and the disc came out", len(ejects) > n, True)
check("nothing in the library", share_files(), [])

print("pressing Rip rips it anyway")
pressed = run(new_job("THE_MATRIX", manual=True))
check("a person asking overrides Leave it alone", pressed["state"], "done")

print("Show me first stops before the rip, then rips once answered")
fresh_share()
db.set("auto_movie", "show")
shown = run(new_job("THE_MATRIX"))
check("waiting for a look", shown["state"], "needs_input")
contains("asking for one", shown["question"], "Check the name and the title")
check("nothing ripped yet", share_files(), [])
ok, _ = rip.answer(shown["id"], name="The Matrix (1999)", title_index=0)
check("answered", ok, True)
answered = run(db.get_job(shown["id"]))
check("and then it rips, without asking again", answered["state"], "done")
check("under the answered name", share_files(),
      ["nas/Media/Movies/The Matrix (1999)/The Matrix (1999).mkv"])
db.set("auto_movie", "rip")

print("film extras: off by default")
fresh_share()
plain = run(new_job("THE_MATRIX"))
check("just the film", share_files(), ["nas/Media/Movies/The Matrix/The Matrix.mkv"])

print("film extras: all of them, beside the film")
for mode in ("burst", "direct"):
    fresh_share()
    db.set("transfer_mode", mode)
    db.set("movie_extras", "all")
    xf = run(new_job("THE_MATRIX"))
    check("%s: done" % mode, xf["state"], "done")
    check("%s: the trailer is in the film's Featurettes folder, the menu loop isn't"
          % mode, share_files(),
          ["nas/Media/Movies/The Matrix/Featurettes/Trailer.mkv",
           "nas/Media/Movies/The Matrix/The Matrix.mkv"])
db.set("transfer_mode", "burst")
db.set("movie_extras", "none")

print("choosing a film's extras")


def t(i, secs, segs, name=""):
    return {"index": i, "seconds": secs, "bytes": secs * 1000, "segments": segs,
            "name": name, "source": "%05d.mpls" % i}


MAIN = t(0, 7200, "1,2,3", "Film")
TITLES = [MAIN,
          t(1, 7190, "1,2,3"),          # a decoy: the film again
          t(2, 7400, "1,2,3,9"),        # the extended cut
          t(3, 600, "20", "Making of"),
          t(4, 600, "20"),              # the same featurette, a second playlist
          t(5, 300, "21", "Deleted scenes"),
          t(6, 900, "20,21"),           # a play-all of the featurettes
          t(7, 1200, "2"),              # a scene of the film re-cut
          t(8, 60, "30")]               # a menu loop
picked = rip.film_extras(TITLES, MAIN, 120)
check("decoys, cuts, re-cuts, play-alls, duplicates and loops are left out",
      [x["index"] for x in picked], [3, 5])
check("named from the disc where it has names", [x["name"] for x in picked],
      ["Making of", "Deleted scenes"])
check("capped", len(rip.film_extras(
    [MAIN] + [t(10 + i, 200 + i, str(100 + i)) for i in range(60)], MAIN, 120)),
      rip.FILM_EXTRAS_MAX)

# ── seasons ───────────────────────────────────────────────────────────────────

os.environ["RIPARR_MOCK_CONTENT"] = "tv"

print("a season Auto Rip is set to leave alone is ejected")
fresh_share()
db.set("auto_tv", "ignore")
tl = run(new_job("SOME_SHOW_S01_D01"))
check("not ripped", (tl["state"], tl["kind"]), ("cancelled", "tv"))
contains("History says why", tl["error"], "ignore TV seasons")

print("Show me first stops before the rip")
db.set("auto_tv", "show")
ts = run(new_job("SOME_SHOW_S01_D02"))
check("waiting, before anything was read", (ts["state"], ts["local_path"]),
      ("needs_input", None))
db.set("auto_tv", "rip")

print("TV extras: all of them")
fresh_share()
db.set("tv_extras", "all")
db.set("transfer_mode", "direct")      # six episodes don't fit the mock card
tx = run(new_job("EXTRAS_SHOW_S02_D01"))
db.set("transfer_mode", "burst")
check("done", tx["state"], "done")
check("the featurette went in with the episodes",
      "nas/Media/TV/Extras Show/Featurettes/Season 2 Extra 1.mkv" in share_files(), True)
db.set("tv_extras", "none")

print("the old \"always show me the season plan\" carries across")
db.set("on_season_disc", "ask")
db.conn().execute("DELETE FROM settings WHERE key='auto_tv'")
db.conn().commit()
db.init()
check("as Show me first for TV", (db.get("auto_tv"), db.get("on_season_disc")),
      ("show", "unsure"))
db.set("auto_tv", "rip")

# ── data discs ────────────────────────────────────────────────────────────────

os.environ["RIPARR_MOCK_CONTENT"] = "data"
os.environ["RIPARR_MOCK_DISC"] = "dvd"

print("a disc that is none of the others is saved as an ISO image")
for mode in ("burst", "direct"):
    fresh_share()
    db.set("transfer_mode", mode)
    dj = run(new_job("AGE_OF_EMPIRES_II"))
    check("%s: done" % mode, dj["state"], "done")
    check("%s: as a data job" % mode, (dj["kind"], dj["output"]), ("data", "iso"))
    check("%s: one image in the Data folder" % mode, share_files(),
          ["nas/Media/Data/Age Of Empires Ii.iso"])
    contains("%s: and told so" % mode, dj["dest_path"], "Data/Age Of Empires Ii.iso")
db.set("transfer_mode", "burst")

print("two data discs with the same label are not the same disc")
d1 = dict(P.optical_drives()[0])
d2 = dict(d1, size_bytes=d1["size_bytes"] // 2)
check("their size tells them apart", rip.fingerprint(d1) != rip.fingerprint(d2), True)

print("data discs follow their own Auto Rip row")
fresh_share()
db.set("auto_data", "ignore")
dl = run(new_job("DRIVERS"))
check("left alone", dl["state"], "cancelled")
contains("saying so", dl["error"], "ignore data discs")
db.set("auto_data", "show")
ds = run(new_job("DRIVERS"))
check("Show me first waits", ds["state"], "needs_input")
contains("and says what it will do", ds["question"], "ISO image")
rip.answer(ds["id"], name="Printer Drivers 2004")
ds = run(db.get_job(ds["id"]))
check("named as answered", share_files(), ["nas/Media/Data/Printer Drivers 2004.iso"])
db.set("auto_data", "rip")

print("an unreadable stretch is zero-filled and counted, not fatal")


class Flaky:
    """A disc whose third sector will not read."""
    def __init__(self, data):
        self.data, self.pos = data, 0

    def seek(self, pos):
        self.pos = pos

    def read(self, n):
        bad = 2 * rip.SECTOR
        if self.pos <= bad < self.pos + n:
            raise OSError(5, "Input/output error")
        out = self.data[self.pos:self.pos + n]
        self.pos += len(out)
        return out


disc = bytes(range(256)) * (rip.SECTOR * 4 // 256)
bad = [0]
got = rip._read_sectors(Flaky(disc), 0, len(disc), bad)
check("one sector counted", bad[0], 1)
check("the rest read", (got[:2 * rip.SECTOR], got[3 * rip.SECTOR:]),
      (disc[:2 * rip.SECTOR], disc[3 * rip.SECTOR:]))
check("and the bad one is zeros", got[2 * rip.SECTOR:3 * rip.SECTOR],
      b"\0" * rip.SECTOR)

print("")
if failures:
    print("%d FAILED: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all good")
