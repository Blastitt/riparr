#!/usr/bin/env python3
"""A whole season disc, end to end, through the real pipeline.

`tv-detect.test.py` proves the reasoning. This proves the plumbing: that a season disc
identified as one job produces *many* files, in the right folders, with the right names,
and that the film path did not change shape on the way past.

It runs the actual `_identify`, `_rip_season` and `_transfer_season` against the mock
drive and the mock share, with a throwaway database. Nothing here is stubbed except the
hardware, which is the same thing every other mock in this repository means.

Worth knowing what this catches that unit tests could not: the pipeline was written for
one job to produce one file, and that assumption is spread across five stages and a
dozen job columns. Ripping a season with it is the only way to find the places that
still believe it -- `local_path` holding a directory, `bytes_total` meaning the sum of
six things, staging cleanup running after the first episode and deleting the other five.
That last one was real, and `direct_mode_keeps_the_rest_of_the_season` is why.

Run: python3 server/tv-pipeline.test.py
"""
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_tmp = tempfile.mkdtemp(prefix="riparr-tv-test-")
os.environ["RIPARR_DB"] = os.path.join(_tmp, "test.db")
os.environ["RIPARR_MOCK_CONTENT"] = "tv"
os.environ["RIPARR_APPLIANCE"] = "0"
os.environ["RIPARR_MOCK_FAST"] = "1"   # skip the mock rip's deliberate pacing

from riparr import db, rip, shares as SH  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s: got %r, wanted %r" % (name, got, want))
        failures.append(name)


def contains(name, haystack, needle):
    check(name, needle in haystack, True)


def share_files():
    """Every file under the mock share, share-relative and sorted."""
    out = []
    for root, _dirs, files in os.walk(SH.MOCK_SHARE_ROOT):
        for f in files:
            out.append(os.path.relpath(os.path.join(root, f), SH.MOCK_SHARE_ROOT))
    return sorted(out)


def fresh_share():
    shutil.rmtree(SH.MOCK_SHARE_ROOT, ignore_errors=True)
    for row in db.list_shares():
        db.delete_share(row["id"])
    return db.add_share("Media", "nas", "Media", None, None, make_default=True)


def run(job):
    """Identify and run one job to completion."""
    os.environ["RIPARR_MOCK_LABEL"] = job.get("disc_label") or ""
    s = rip._settings()
    job = rip._identify(job, s)
    if job is None:
        return None                     # parked on a question
    base = rip._rip_season(job, s, threading.Event())
    transport, folder, base = rip._transfer_season(job, s, base, threading.Event())
    rip._finish_season(job, s, transport, folder, base)
    return db.get_job(job["id"])


def run_all(job):
    """As `run`, but the way the worker does it: a season held for its naming stops
    after the rip, with the disc out, instead of going on to the library."""
    os.environ["RIPARR_MOCK_LABEL"] = job.get("disc_label") or ""
    s = rip._settings()
    job = rip._identify(job, s)
    if job is None:
        return None
    base = rip._rip_season(job, s, threading.Event())
    if job["_plan"].get("hold"):
        rip._hold_after_rip(job)
        return db.get_job(job["id"])
    transport, folder, base = rip._transfer_season(job, s, base, threading.Event())
    rip._finish_season(job, s, transport, folder, base)
    return db.get_job(job["id"])


def send(job_id):
    """What the sender thread does with a released job."""
    j = db.get_job(job_id)
    check("released to the sender", j["state"], "transferring")
    rip._send_one(j)
    return db.get_job(job_id)


def new_job(label):
    """A queued job for a disc with this volume label, in the tray.

    The label goes on the mock drive as well as the job, because `_identify_season`
    reads the drive first -- that is where a real label comes from, and pointing the
    test at the job field instead would have exercised a path no disc ever takes.
    """
    os.environ["RIPARR_MOCK_LABEL"] = label
    job_id = db.create_job(state="queued", disc_label=label, fingerprint="fp-" + label)
    return db.get_job(job_id)


print("settings and schema came up")
db.init()
db.set("setup_complete", True)
db.set("tv_metadata", False)          # no network in a test; names come from numbers
db.set("on_season_disc", "auto")      # exercise the unattended path first
db.set("transfer_mode", "burst")      # stage on the card, then send
db.set("verify_mode", "quick")
check("tv columns exist", "episode_plan" in db.get_job(db.create_job(state="queued")), True)

# ── the unattended path ──────────────────────────────────────────────────────

print("a season disc rips as a season, unattended")
share = fresh_share()
job = run(new_job("THE_EXPANSE_S02_D01"))
check("the job finished", job["state"], "done")
check("it is a TV job", job["kind"], "tv")
check("season came off the label", job["season"], 2)
plan = db.episode_plan(job)
check("six episodes planned", len(plan["episodes"]), 6)
check("every one of them landed",
      [e["state"] for e in plan["episodes"]], ["done"] * 6)

files = share_files()
check("six files in the library, not one", len(files), 6)
check("named and foldered for Plex and Jellyfin", files,
      ["nas/Media/TV/The Expanse/Season 02/The Expanse - S02E%02d.mkv" % n
       for n in range(1, 7)])

print("the episodes are in the disc's order, not MakeMKV's")
check("title 3 is episode 1, as the play-all says",
      [(e["title_index"], e["episode"]) for e in plan["episodes"]],
      [(3, 1), (1, 2), (5, 3), (0, 4), (4, 5), (2, 6)])

print("the next disc of the same season continues the numbering")
job2 = run(new_job("THE_EXPANSE_S02_D02"))
plan2 = db.episode_plan(job2)
check("disc two starts at episode 7", plan2["episodes"][0]["episode"], 7)
check("and runs to twelve", plan2["episodes"][-1]["episode_last"], 12)
check("twelve files in the library now", len(share_files()), 12)

# ── "unsure": stop once per season, not once per disc ────────────────────────

print("the default does not stop on a disc it can read and name")
# The order comes off the disc, and with no episode names on the files there is no
# broadcast-order question to ask. The first disc used to stop here regardless, which
# parked every unattended box set on its first disc.
fresh_share()
db.set("on_season_disc", "unsure")
d1 = run_all(new_job("SOME_SHOW_S05_D01"))
check("first disc rips straight through", d1["state"], "done")
d2 = run_all(new_job("SOME_SHOW_S05_D02"))
check("and so does the second", d2["state"], "done")
check("carrying on from seven", db.episode_plan(d2)["episodes"][0]["episode"], 7)

# Names on, through stand-ins for TVmaze. What settles broadcast order is TVmaze's
# DVD-release list: none (an empty answer) means disc order is aired order.
_real = (rip.tv.search_series, rip.tv.episodes, rip.tv.dvd_order)
rip.tv.search_series = lambda name, limit=6: [{"id": 77, "name": "Named Show",
                                               "year": "2004", "score": 1.0}]
rip.tv.episodes = lambda sid, include_specials=True: [
    {"season": 1, "number": n, "name": "Episode %d" % n, "runtime": 42, "special": False}
    for n in range(1, 13)]
db.set("tv_metadata", True)

print("a show TVmaze says has no other order rips without asking")
fresh_share()
rip.tv.dvd_order = lambda sid: {}
n1 = run_all(new_job("NAMED_SHOW_S01_D01"))
check("done, nobody asked", n1["state"], "done")
check("named from TVmaze", share_files()[0],
      "nas/Media/TV/Named Show (2004)/Season 01/Named Show - S01E01 - Episode 1.mkv")

print("when TVmaze can't be asked, it rips anyway and holds only the naming")
fresh_share()
rip.tv.dvd_order = lambda sid: None
db.conn().execute("DELETE FROM jobs WHERE title='Named Show'")
db.conn().execute("DELETE FROM discs")   # a fresh copy of the set, never seen before
db.conn().commit()
h = run_all(new_job("NAMED_SHOW_S01_D01"))
check("read, and waiting to be named", (h["state"], bool(h["local_path"])),
      ("needs_input", True))
contains("saying so", h["question"], "Ripped and ejected")
check("which does not hold the drive", (db.drive_busy() or {}).get("id") != h["id"], True)
check("nothing in the library yet", share_files(), [])
ok, msg = rip.answer(h["id"])
check("an empty answer accepts it", ok, True)
h = send(h["id"])
check("and it is filed", (h["state"], len(share_files())), ("done", 6))

print("a show released in production order is named from the DVD list")
# Firefly's shape: DVD position 1 is the episode the guides call E11.
fresh_share()
db.conn().execute("DELETE FROM jobs WHERE title='Named Show'")
db.conn().execute("DELETE FROM discs")   # a fresh copy of the set, never seen before
db.conn().commit()
order = [11, 1, 2, 6, 7, 3, 4, 5, 8, 9, 10, 12]
rip.tv.dvd_order = lambda sid: {(1, i + 1): {"season": 1, "number": a,
                                             "name": "Episode %d" % a}
                                for i, a in enumerate(order)}
f1 = run_all(new_job("NAMED_SHOW_S01_D01"))
check("rips without asking", f1["state"], "done")
fp = db.episode_plan(f1)
check("positions are disc order", [e["position"] for e in fp["episodes"]],
      [1, 2, 3, 4, 5, 6])
check("file numbers are the broadcast ones", [e["episode"] for e in fp["episodes"]],
      [11, 1, 2, 6, 7, 3])
contains("and the user is told", fp["warnings"][0], "TVmaze's DVD order")
check("the first file is S01E11 with its own name",
      "nas/Media/TV/Named Show (2004)/Season 01/Named Show - S01E11 - Episode 11.mkv"
      in share_files(), True)
f2 = run_all(new_job("NAMED_SHOW_S01_D02"))
check("disc two carries on in disc order: positions 7-12",
      [e["position"] for e in db.episode_plan(f2)["episodes"]][::5], [7, 12])
check("named 4, 5, 8, 9, 10, 12",
      [e["episode"] for e in db.episode_plan(f2)["episodes"]], [4, 5, 8, 9, 10, 12])
check("twelve distinct files", len(set(share_files())), 12)

rip.tv.search_series, rip.tv.episodes, rip.tv.dvd_order = _real
db.set("tv_metadata", False)
db.set("on_season_disc", "auto")

# ── discs put in out of order ───────────────────────────────────────────────

def finish(job_id):
    j = db.get_job(job_id)
    j["_device"], j["_plan"] = "/dev/sr0", db.episode_plan(j)
    b = rip._rip_season(j, rip._settings(), threading.Event())
    tr, fo, b = rip._transfer_season(j, rip._settings(), b, threading.Event())
    rip._finish_season(j, rip._settings(), tr, fo, b)
    return db.get_job(job_id)


print("disc 3 before disc 2 does not push disc 2 to the end")
# The box set is open on the sofa and disc 2 is in the other player. Numbering used to
# be "after the highest episode so far", so disc 2 became E13-E18 behind disc 3.
fresh_share()
db.set("on_season_disc", "auto")
o1 = run(new_job("OUT_OF_ORDER_S01_D01"))
check("disc one is 1-6", [db.episode_plan(o1)["episodes"][i]["episode"] for i in (0, -1)],
      [1, 6])
o3 = run_all(new_job("OUT_OF_ORDER_S01_D03"))
check("disc three, with disc two missing, is read and held, even under auto",
      (o3["state"], bool(o3["local_path"])), ("needs_input", True))
p3 = db.episode_plan(o3)
check("proposing 13 from the size of disc one", p3["episodes"][0]["episode"], 13)
contains("and saying it is a guess", p3["warnings"][0], "can't know where")
check("without holding the drive for disc two",
      (db.drive_busy() or {}).get("id") != o3["id"], True)
o2 = run_all(new_job("OUT_OF_ORDER_S01_D02"))
check("disc two does not ask -- disc one says where it starts", o2["state"], "done")
check("and it is 7-12, not 19-24",
      [db.episode_plan(o2)["episodes"][i]["episode"] for i in (0, -1)], [7, 12])
o3 = send(o3["id"])
check("disc three then files itself, nobody asked", o3["state"], "done")
check("as 13-18", [db.episode_plan(o3)["episodes"][i]["episode"] for i in (0, -1)],
      [13, 18])
check("eighteen files", len(share_files()), 18)

print("disc 2 after disc 3 alone counts back from disc 3")
fresh_share()
b3 = run_all(new_job("COUNT_BACK_S02_D03"))
ok, _ = rip.answer(b3["id"], season=2, first_episode=13)
check("a human can settle a held disc's numbering", ok, True)
send(b3["id"])
b2 = run(new_job("COUNT_BACK_S02_D02"))
check("ends right before disc three", db.episode_plan(b2)["episodes"][-1]["episode"], 12)
check("so starts at 7", db.episode_plan(b2)["episodes"][0]["episode"], 7)

# ── extras ──────────────────────────────────────────────────────────────────

print("a ticked extra lands in Featurettes, beside the seasons")
fresh_share()
db.set("auto_tv", "show")
xj = new_job("EXTRAS_SHOW_S01_D01")
rip._identify(xj, rip._settings())
xplan = db.episode_plan(db.get_job(xj["id"]))
check("only the featurette is offered -- not the duplicate playlists",
      [x["title_index"] for x in xplan["extras"]], [20])
check("and it starts unticked", xplan["extras"][0]["include"], False)
ok, _ = rip.answer(xj["id"], season=1, first_episode=1,
                   extras={"20": "Behind the Scenes"})
check("accepted", ok, True)
xj = finish(xj["id"])
check("the job finished", xj["state"], "done")
files = share_files()
check("six episodes and one extra", len(files), 7)
check("the extra is in the show's Featurettes folder",
      "nas/Media/TV/Extras Show/Featurettes/Behind the Scenes.mkv" in files, True)
xj2 = new_job("EXTRAS_SHOW_S01_D02")
rip._identify(xj2, rip._settings())
check("unnamed extras are named for their season",
      db.episode_plan(db.get_job(xj2["id"]))["extras"][0]["name"], "Season 1 Extra 1")
rip.answer(xj2["id"], extras={"20": "Behind the Scenes"})
finish(xj2["id"])
check("a second disc's extra with the same name sits beside the first, not over it",
      sorted(f for f in share_files() if "Featurettes" in f),
      ["nas/Media/TV/Extras Show/Featurettes/Behind the Scenes (2).mkv",
       "nas/Media/TV/Extras Show/Featurettes/Behind the Scenes.mkv"])
check("and it did not move the next disc's numbering",
      db.next_episode(None, 1, series_name="Extras Show"), 13)
db.set("auto_tv", "rip")
db.set("on_season_disc", "auto")

# ── the film path is unchanged ───────────────────────────────────────────────

print("a film disc still rips as a film")
os.environ["RIPARR_MOCK_CONTENT"] = "movie"
fresh_share()
mjob = new_job("BLADE_RUNNER")
s = rip._settings()
mjob = rip._identify(mjob, s)
check("not taken by the season path", mjob is not None, True)
check("still a movie", mjob.get("kind") or "movie", "movie")
path = rip._rip(mjob, s, threading.Event())
transport, name, path = rip._transfer(mjob, s, path, threading.Event())
rip._finish(mjob, s, transport, name, path)
check("one file, in the movie folder", share_files(),
      ["nas/Media/Movies/Blade Runner/Blade Runner.mkv"])
os.environ["RIPARR_MOCK_CONTENT"] = "tv"

# ── the question, and the answer ─────────────────────────────────────────────

print("an unlabelled season disc is ripped, then asks for its season")
fresh_share()
db.set("on_season_disc", "unsure")
ask_job = run_all(new_job("DISC_1"))   # no season anywhere
check("read, ejected, and waiting for a human",
      (ask_job["state"], bool(ask_job["local_path"])), ("needs_input", True))
contains("and says why", ask_job["question"], "nothing on the disc says which season")
proposed = db.episode_plan(ask_job)
check("with a plan already drawn up", len(proposed["episodes"]), 6)
check("no season on it yet", proposed["season"], None)

print("the answer is applied, and remembered")
ok, msg = rip.answer(ask_job["id"], season=3, first_episode=4, name="Twin Peaks")
check("accepted", ok, True)
answered = db.get_job(ask_job["id"])
check("straight to the sender, not back to the drive", answered["state"], "transferring")
apl = db.episode_plan(answered)
check("season set", apl["season"], 3)
check("numbering starts where asked",
      [e["episode"] for e in apl["episodes"]], [4, 5, 6, 7, 8, 9])
disc = db.get_disc("fp-DISC_1")
check("and the disc remembers it, so it never asks twice",
      (disc["season"], disc["first_episode"]), (3, 4))

send(answered["id"])
check("filed under the season the human gave it", share_files(),
      ["nas/Media/TV/Twin Peaks/Season 03/Twin Peaks - S03E%02d.mkv" % n
       for n in range(4, 10)])

print("a folder name typed over a matched series is used")
fresh_share()
db.set("auto_tv", "show")
rj = new_job("RENAMED_S01_D01")
rip._identify(rj, rip._settings())
rplan = db.episode_plan(db.get_job(rj["id"]))
rip.answer(rj["id"], name=rplan["series"])
check("an untouched box changes nothing", db.episode_plan(db.get_job(rj["id"]))["series"],
      rplan["series"])
db.update_job(rj["id"], state="needs_input")
rip.answer(rj["id"], name="Renamed By Hand")
check("an edited one is the folder", db.get_job(rj["id"])["title"], "Renamed By Hand")
db.set("auto_tv", "rip")
db.set("on_season_disc", "unsure")

print("unticking an episode renumbers the rest")
fresh_share()
j = new_job("SOMETHING_S01_D01")
rip._identify(j, rip._settings())
j = db.get_job(j["id"])
if j["state"] != "needs_input":
    db.update_job(j["id"], state="needs_input")
plan_before = db.episode_plan(db.get_job(j["id"]))
keep = [e["title_index"] for e in plan_before["episodes"]][2:]     # drop the first two
ok, _ = rip.answer(j["id"], season=1, first_episode=1, include=keep)
after = db.episode_plan(db.get_job(j["id"]))
kept = [e for e in after["episodes"] if e.get("include", True)]
check("four episodes kept", len(kept), 4)
check("numbered from one, with no gap",
      [e["episode"] for e in kept], [1, 2, 3, 4])
check("the dropped ones are marked skipped",
      [e["state"] for e in after["episodes"] if not e.get("include", True)],
      ["skipped", "skipped"])

# ── direct mode ──────────────────────────────────────────────────────────────

print("direct mode keeps the rest of the season")
# The bug this guards: `_place_directly` tidies the whole job directory when it is
# done, which on a season disc deletes every episode still waiting to be sent. The
# first episode landed and the other five vanished between stages.
fresh_share()
db.set("transfer_mode", "direct")
db.set("on_season_disc", "auto")
dj = run(new_job("DIRECT_SHOW_S01_D01"))
check("finished", dj["state"], "done")
check("all six survived the transfer", len(share_files()), 6)
db.set("transfer_mode", "burst")

print("MakeMKV's own message is what a failure says")
# Found on Samurai Jack season 2: the NAS stalled 800 MB into episode 12, and the job
# failed with "Saving to MKV file" -- the phase label, not the reason.
real = ("Error 'Internal error - Operation result is incorrect (178)' occurred while "
        "writing data to '/srv/library/RiparrDumps/.riparr-incoming/job-2/ep-12/x.mkv' "
        "at offset '838860800'")
check("a write failure is the library's", rip._makemkv_failure(real)[0], "write")
contains("and says so", rip._makemkv_failure(real)[1], "Couldn't write to your library")
check("routine commentary is not a failure",
      rip._makemkv_failure("Title #00821.mpls has length of 66 seconds which is less "
                           "than minimum title length"), None)

_real_mock_rip = rip._mock_rip_episode


def failing(title, times, where):
    """The mock rip, failing `times` times on one title the way MakeMKV does."""
    left = {"n": times}

    def rip_episode(job, out_dir, cancel_ev, row, progress, first_byte):
        if int(row["title_index"]) == title and left["n"] > 0:
            left["n"] -= 1
            err = rip.RipFailed("Couldn't write to your library: it stopped accepting "
                                "data partway through." if where == "write" else
                                "The drive couldn't read part of the disc.")
            err.where = where
            raise err
        return _real_mock_rip(job, out_dir, cancel_ev, row, progress, first_byte)
    return rip_episode


print("a share that stalls once costs one retry, onto the card")
fresh_share()
db.set("transfer_mode", "direct")
rip._mock_rip_episode = failing(0, 1, "write")
st = run(new_job("STALL_SHOW_S01_D01"))
check("the season still finished", st["state"], "done")
check("all six filed", len(share_files()), 6)
retried = [e for e in db.episode_plan(st)["episodes"] if e["title_index"] == 0][0]
check("the retried episode was read onto the card",
      retried["path"].startswith(rip.STAGING), True)

print("an episode that can't be read loses that episode, not the season")
fresh_share()
rip._mock_rip_episode = failing(5, 99, "read")
bad = run(new_job("SCRATCHED_S01_D01"))
check("the job says something needs doing", bad["state"], "failed")
check("five episodes filed anyway", len(share_files()), 5)
contains("naming the missing one", bad["error"], "S01E03 couldn't be ripped")
contains("and what to do", bad["error"], "Put the disc back in")
check("the disc is not recorded as ripped, so it can go back in",
      rip._already_have(db.get_disc("fp-SCRATCHED_S01_D01")), False)

print("putting it back in reads only what's missing")
rip._mock_rip_episode = _real_mock_rip
read = []


def counting(job, out_dir, cancel_ev, row, progress, first_byte):
    read.append(int(row["title_index"]))
    return _real_mock_rip(job, out_dir, cancel_ev, row, progress, first_byte)


rip._mock_rip_episode = counting
again = run(new_job("SCRATCHED_S01_D01"))
check("done", again["state"], "done")
check("only the missing episode was read", read, [5])
check("all six in the library, under the first attempt's numbers", share_files(),
      ["nas/Media/TV/Scratched/Season 01/Scratched - S01E%02d.mkv" % n
       for n in range(1, 7)])
rip._mock_rip_episode = _real_mock_rip
db.set("transfer_mode", "burst")

print("a box that dies mid-season keeps what it read, and carries on")
# 2026-10-12: the Wi-Fi chip froze the kernel seven episodes into a disc. Recovery on
# the next boot used to delete those seven.
fresh_share()
db.set("transfer_mode", "direct")
cut = new_job("POWER_CUT_S01_D01")
os.environ["RIPARR_MOCK_LABEL"] = "POWER_CUT_S01_D01"
cj = rip._identify(cut, rip._settings())
stop_after = {"n": 0}


def dies_after_three(job, out_dir, cancel_ev, row, progress, first_byte):
    stop_after["n"] += 1
    if stop_after["n"] > 3:
        raise KeyboardInterrupt          # the power goes
    return _real_mock_rip(job, out_dir, cancel_ev, row, progress, first_byte)


rip._mock_rip_episode = dies_after_three
try:
    rip._rip_season(cj, rip._settings(), threading.Event())
except KeyboardInterrupt:
    pass
rip._mock_rip_episode = _real_mock_rip
rip.recover()
dead = db.get_job(cj["id"])
check("marked failed", dead["state"], "failed")
contains("saying what it kept", dead["error"], "The 3 episodes it had read are kept")
read.clear()
rip._mock_rip_episode = counting
back = run(new_job("POWER_CUT_S01_D01"))
rip._mock_rip_episode = _real_mock_rip
check("the disc going back in finishes it", back["state"], "done")
check("reading only the three it hadn't", len(read), 3)
check("six episodes filed", len([f for f in share_files() if "Power Cut" in f]), 6)
db.set("transfer_mode", "burst")

# ── interrupted, then resumed ────────────────────────────────────────────────

print("a season interrupted mid-transfer resumes where it stopped")
# What a reboot leaves behind: some episodes in the library, some still on the card,
# and a plan that says which is which. The bug this guards is that a season job's
# `local_path` is a *directory*, and `_send_one` used to hand it to the single-file
# transfer -- which takes the size of a directory entry and uploads that.
fresh_share()
db.set("transfer_mode", "burst")
rj = run(new_job("INTERRUPTED_S04_D01"))
check("all six landed first time", len(share_files()), 6)

rplan = db.episode_plan(rj)
for e in rplan["episodes"][3:]:                 # pretend the last three never went
    os.remove(os.path.join(SH.MOCK_SHARE_ROOT, "nas", "Media", e["remote_name"]))
    e["state"] = "ripped"
    e.pop("remote_name", None)
db.update_job(rj["id"], episode_plan=rplan, state="transferring",
              finished_at=None, error=None)
check("three files left in the library", len(share_files()), 3)

rip._send_one(db.get_job(rj["id"]))
resumed = db.get_job(rj["id"])
check("the job finished on the second pass", resumed["state"], "done")
check("and all six are there again", len(share_files()), 6)
check("every row marked done",
      [e["state"] for e in db.episode_plan(resumed)["episodes"]], ["done"] * 6)

print("re-verification checks every episode, not a directory")
ok, msg = rip.reverify(rj["id"], mode="quick")
check("accepted", ok, True)
contains("and says how many", msg, "6 episodes")
for _ in range(80):
    if db.get_job(rj["id"])["state"] in ("done", "failed"):
        break
    time.sleep(0.05)
check("and they all pass", db.get_job(rj["id"])["state"], "done")

# ── specials ─────────────────────────────────────────────────────────────────

print("season zero goes where specials go")
fresh_share()
sj = new_job("SPECIALS_S00_D01")
rip._identify(sj, rip._settings())
sj = db.get_job(sj["id"])
if sj["state"] == "needs_input":
    rip.answer(sj["id"], season=0, first_episode=1)
    sj = db.get_job(sj["id"])
splan = db.episode_plan(sj)
check("rendered into Season 00",
      "/Season 00/" in rip._episode_name(sj, rip._settings(), splan,
                                         splan["episodes"][0]), True)
db.set("tv_specials_folder", "Specials")
check("or into Specials when that is the setting",
      "/Specials/" in rip._episode_name(sj, rip._settings(), splan,
                                        splan["episodes"][0]), True)

print("")
shutil.rmtree(_tmp, ignore_errors=True)
shutil.rmtree(SH.MOCK_SHARE_ROOT, ignore_errors=True)
if failures:
    print("%d FAILED: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all good")
