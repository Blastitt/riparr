#!/usr/bin/env python3
"""Real season discs, from real drives, through the real parser and planner.

`tv-detect.test.py` uses a mock disc built from forum folklore. These are the scans of
actual discs, taken with `makemkvcon -r --cache=1 --minlength=0 info disc:0` on the
reference box and saved under fixtures/discs, next to TVmaze's episode list for the show
as it was on the day. Each one encodes what the right answer turned out to be, checked
against the disc, so a change to the planner that breaks a disc people own fails here.

Add a disc: save its scan and its TVmaze list, then write down what it must produce.

Run: python3 server/tv-realdisc.test.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("RIPARR_DB", os.path.join(tempfile.mkdtemp(), "t.db"))
os.environ["RIPARR_APPLIANCE"] = "0"

from riparr import rip, tv  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "discs")
# Riparr scans with MakeMKV's default minimum title length; the fixtures were taken
# with none, so they hold the menu loops too. Apply the floor the box would have.
MAKEMKV_MIN = 120
failures = []


def check(name, got, want):
    if got == want:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s: got %r, wanted %r" % (name, got, want))
        failures.append(name)


def load(scan, tvmaze):
    titles = rip.parse_info(open(os.path.join(FIX, scan), encoding="utf-8").read())
    titles = [t for t in titles if t["seconds"] >= MAKEMKV_MIN]
    raw = json.load(open(os.path.join(FIX, tvmaze), encoding="utf-8"))
    eps = [{"season": e["season"], "number": e["number"], "name": e.get("name") or "",
            "runtime": e.get("runtime") or 0,
            "special": (e.get("type") or "regular") != "regular"}
           for e in raw if e.get("season") is not None and e.get("number") is not None]
    return titles, eps


# ── Samurai Jack, The Complete First Season (Blu-ray, 2026-10-12) ────────────

print("Samurai Jack season 1: one disc, the whole season")
titles, eps = load("samurai-jack-s1-bluray.info.txt", "tvmaze-1603-samurai-jack.json")
check("the parser reads the segment map MakeMKV really prints",
      tv.parse_segments(next(t for t in titles if t["index"] == 15)["segments"])[:3],
      [2609, 2610, 2611])
found = tv.analyse(titles, MAKEMKV_MIN)
check("read as a season", found is not None, True)
check("thirteen episodes", len(found["episodes"]), 13)
check("the five-hour play-all is found", found["play_all"]["index"], 15)
check("and orders the disc, so it is trusted",
      (found["order_source"], found["confidence"]), ("segments", "high"))
check("no duplicates invented", found["duplicates"], [])
check("no double-length episodes invented", found["doubles"], [])
check("the 5:40 and 7:10 extras are extras",
      sorted(t["index"] for t in found["extras"]), [4, 19])
# The trap this disc sets: episode 7 is 00100.mpls, because MakeMKV dropped 00806 as
# identical to it. Sorting by playlist name would have filed it first.
check("00100.mpls is episode 7, where the play-all puts it, not episode 1",
      [t["index"] for t in found["episodes"]],
      [0, 1, 2, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14])
check("ordering by source name alone would have got this disc wrong",
      tv._order_by_source(found["episodes"])[0]["source"], "00100.mpls")

label = "Samurai Jack Season 1"
check("the label says season 1 and no disc number", tv.season_from_label(label), (1, None))
check("and names the show", tv.series_name_from_label(label), "Samurai Jack")

plan = tv.build_plan(found, season=1, first_episode=1,
                     series={"id": 1603, "name": "Samurai Jack", "year": "2001"},
                     episode_list=eps)
check("named in order",
      [(e["episode"], e["episode_title"]) for e in plan["episodes"]][:3] + [
          (plan["episodes"][6]["episode"], plan["episodes"][6]["episode_title"]),
          (plan["episodes"][-1]["episode"], plan["episodes"][-1]["episode_title"])],
      [(1, "The Beginning (1)"), (2, "The Samurai Called Jack (2)"),
       (3, "The First Fight (3)"), (7, "Jack and the Three Blind Archers"),
       (13, "Aku's Fairy Tales")])
check("nothing to warn about but the play-all",
      len(tv.plan_warnings(plan, found, eps)), 1)


# ── Samurai Jack, The Complete Second Season (Blu-ray, 2026-10-12) ───────────

print("")
print("Samurai Jack season 2: the same shape, plus a 21-minute title outside the play-all")
titles, eps = load("samurai-jack-s2-bluray.info.txt", "tvmaze-1603-samurai-jack.json")
found = tv.analyse(titles, MAKEMKV_MIN)
check("thirteen episodes, not fourteen", len(found["episodes"]), 13)
check("the bare 02630.m2ts is offered as an extra, not numbered",
      (found["outside_play_all"], sorted(t["index"] for t in found["extras"])),
      ([21], [4, 19, 21]))
check("so the play-all still orders the disc",
      (found["play_all"]["index"], found["order_source"]), (15, "segments"))
# The same trap as season 1, one disc on: 00101.mpls is episode 12. With title 21
# counted the segment order failed and playlist numbering filed it as E01.
check("00101.mpls is episode 12",
      [t["index"] for t in found["episodes"]],
      [0, 1, 2, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14])
check("the label says season 2", tv.season_from_label("Samurai Jack Season 2"), (2, None))
plan = tv.build_plan(found, season=2, first_episode=1,
                     series={"id": 1603, "name": "Samurai Jack", "year": "2001"},
                     episode_list=eps)
check("named in order",
      [(e["episode"], e["episode_title"]) for e in plan["episodes"]][:2] + [
          (plan["episodes"][11]["episode"], plan["episodes"][11]["episode_title"])],
      [(1, "Jack Learns to Jump Good"), (2, "Jack Tales"), (12, "Jack and the Spartans")])
warnings = tv.plan_warnings(plan, found, eps)
check("the user is told about the left-out title",
      any("Title 21 is episode-length (21 min)" in w for w in warnings), True)


# ── Samurai Jack, The Complete Third Season (Blu-ray, 2026-10-12) ────────────

print("")
print("Samurai Jack season 3: the plain one -- everything where it should be")
titles, eps = load("samurai-jack-s3-bluray.info.txt", "tvmaze-1603-samurai-jack.json")
found = tv.analyse(titles, MAKEMKV_MIN)
check("thirteen episodes in playlist order, confirmed by the play-all",
      ([t["index"] for t in found["episodes"]], found["order_source"]),
      ([0, 1, 2, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14], "segments"))
check("three extras, the 4:56 one included",
      sorted(t["index"] for t in found["extras"]), [4, 19, 21])
check("nothing left outside the play-all", found["outside_play_all"], [])
plan = tv.build_plan(found, season=3, first_episode=1,
                     series={"id": 1603, "name": "Samurai Jack", "year": "2001"},
                     episode_list=eps)
check("named in order",
      [plan["episodes"][i]["episode_title"] for i in (0, 10, 12)],
      ["Chicken Jack", "The Birth of Evil (1)", "Jack and the Labyrinth"])

print("")
if failures:
    print("%d FAILED: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all good")
