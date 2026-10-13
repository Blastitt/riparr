"""Wikidata: a second witness for what a season disc is.

TVmaze is the first and only source of episode names, and on 2026-10-13 it was shown
to be one source too few: it holds two shows called exactly "Avatar: The Last
Airbender" (the 2005 cartoon and the 2024 remake) at the same search score, and taking
the first filed the cartoon under the remake. Wikidata is the independent check:
keyless and free like TVmaze, maintained by different people, and it records each
show's TVmaze id beside its own facts -- when it started, how many seasons and episodes,
how many episodes each season has, and (for most well-known shows) every episode's
title and position.

So it can say two things TVmaze cannot say about itself:

* **Which show.** Given TVmaze's candidates, Wikidata says which of them exist as the
  show they claim to be, and how many seasons each has -- a disc of "Book Three" is not
  the two-season remake.
* **Which episode is which.** Its episode titles, compared with TVmaze's, either agree
  with the numbering Riparr is about to use or show it is shifted.

It names things its own way (Samurai Jack's episodes are "Episode I", "Episode II"...,
TVmaze's are "The Beginning (1)"), so the comparison is about agreement where both have
something to say, never about whether the names are identical -- see tv.cross_check.

Never raises and never blocks: a witness that cannot be reached is reported as absent,
and the rip carries on with TVmaze alone. Its facts can be wrong (it lists Avatar's
episode length as three minutes), so nothing here overrules the disc.
"""
import json
import logging
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger("riparr.Wikidata")

SPARQL = "https://query.wikidata.org/sparql"
UA = "Riparr/1 (https://github.com/jackharvest/riparr; disc-ripping appliance)"
TIMEOUT = 20
CACHE_TTL = 24 * 3600
TV_SEASON = "Q3464665"      # "television series season"
TV_EPISODE = "Q21191270"    # "television series episode"

_lock = threading.Lock()
_cache = {}


def _query(q):
    """Run a SPARQL query; the bindings, or None if Wikidata could not be asked."""
    with _lock:
        hit = _cache.get(q)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
    url = "%s?%s" % (SPARQL, urllib.parse.urlencode({"query": q, "format": "json"}))
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/sparql-results+json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            rows = json.loads(r.read().decode("utf-8", "replace"))["results"]["bindings"]
    except Exception as e:
        log.info("Wikidata query failed: %s", e)
        return None
    with _lock:
        _cache[q] = (time.time(), rows)
    return rows


def _v(row, key):
    return (row.get(key) or {}).get("value")


def _int(text):
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def series_facts(tvmaze_ids):
    """{tvmaze id: facts} for the shows Wikidata knows by these TVmaze ids, or None if
    Wikidata could not be asked.

    facts: {"qid", "label", "year", "seasons", "episodes", "season_episodes": {n: count}}
    """
    ids = sorted({str(int(i)) for i in tvmaze_ids or [] if i})
    if not ids:
        return {}
    values = " ".join('"%s"' % i for i in ids)
    rows = _query("""
SELECT ?tvmaze ?item ?itemLabel ?start ?eps ?seasons ?n ?neps WHERE {
  VALUES ?tvmaze { %s }
  ?item wdt:P8600 ?tvmaze.
  OPTIONAL { ?item wdt:P580 ?start }
  OPTIONAL { ?item wdt:P1113 ?eps }
  OPTIONAL { ?item wdt:P2437 ?seasons }
  OPTIONAL { ?season p:P179 ?st. ?st ps:P179 ?item; pq:P1545 ?n.
             ?season wdt:P31 wd:%s. OPTIONAL { ?season wdt:P1113 ?neps } }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}""" % (values, TV_SEASON))
    if rows is None:
        return None
    return parse_series_facts(rows)


def parse_series_facts(rows):
    """The SPARQL bindings of `series_facts` as its return value. Its own function so a
    saved answer can be replayed in a test."""
    out = {}
    for row in rows:
        tvm = _int(_v(row, "tvmaze"))
        if tvm is None:
            continue
        f = out.setdefault(tvm, {"qid": (_v(row, "item") or "").rsplit("/", 1)[-1],
                                 "label": _v(row, "itemLabel") or "", "year": None,
                                 "seasons": None, "episodes": None,
                                 "season_episodes": {}})
        start = _v(row, "start") or ""
        if start[:4].isdigit():
            y = int(start[:4])
            f["year"] = y if f["year"] is None else min(f["year"], y)
        for key, field in (("eps", "episodes"), ("seasons", "seasons")):
            n = _int(_v(row, key))
            if n is not None:
                f[field] = max(f[field] or 0, n)
        n, neps = _int(_v(row, "n")), _int(_v(row, "neps"))
        if n is not None and neps is not None:
            f["season_episodes"][n] = max(f["season_episodes"].get(n, 0), neps)
    return out


API = "https://www.wikidata.org/w/api.php"


def _api(params):
    key = json.dumps(params, sort_keys=True)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
    url = "%s?%s" % (API, urllib.parse.urlencode(dict(params, format="json")))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            out = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        log.info("Wikidata lookup failed: %s", e)
        return None
    with _lock:
        _cache[key] = (time.time(), out)
    return out


def search_series(name):
    """The shows Wikidata finds by this name, independently of TVmaze: a list of
    {"qid", "label", "tvmaze": [ids]}, only items that carry a TVmaze id -- or None if
    Wikidata could not be asked.

    This is the half of the second witness that does not start from TVmaze's answer.
    "Samurai Jack" finds one show, and it is TVmaze 1603; "The Office" finds four, and
    the disc has to say which; a name that finds TVmaze's pick nowhere is a dispute.
    """
    q = (name or "").strip()
    if not q:
        return []
    found = _api({"action": "wbsearchentities", "search": q, "language": "en",
                  "type": "item", "limit": 10})
    if found is None:
        return None
    ids = [x["id"] for x in found.get("search") or []]
    if not ids:
        return []
    ents = _api({"action": "wbgetentities", "ids": "|".join(ids),
                 "props": "claims|labels", "languages": "en"})
    if ents is None:
        return None
    return parse_search(ids, ents)


def parse_search(ids, ents):
    out = []
    for qid in ids:
        e = (ents.get("entities") or {}).get(qid) or {}
        tvm = []
        for c in (e.get("claims") or {}).get("P8600") or []:
            v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
            if v and str(v).isdigit():
                tvm.append(int(v))
        if tvm:
            out.append({"qid": qid, "tvmaze": tvm,
                        "label": ((e.get("labels") or {}).get("en") or {}).get("value", "")})
    return out


def episode_titles(qid, season):
    """{episode number: title} for one season of the show with this Wikidata id, or
    None if Wikidata could not be asked. Empty when it has no episode items."""
    if not qid or season is None:
        return {}
    rows = _query("""
SELECT ?en ?epLabel WHERE {
  ?season p:P179 ?st. ?st ps:P179 wd:%s; pq:P1545 "%d".
  ?season wdt:P31 wd:%s.
  ?ep wdt:P31 wd:%s; p:P4908 ?es. ?es ps:P4908 ?season; pq:P1545 ?en.
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}""" % (qid, int(season), TV_SEASON, TV_EPISODE))
    if rows is None:
        return None
    return parse_episode_titles(rows)


def parse_episode_titles(rows):
    out = {}
    for row in rows:
        n, title = _int(_v(row, "en")), _v(row, "epLabel") or ""
        # An unlabelled item comes back labelled with its own id ("Q12345").
        if n is not None and title and not (title[0] == "Q" and title[1:].isdigit()):
            out[n] = title
    return out
