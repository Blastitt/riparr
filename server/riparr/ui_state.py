"""Bounded, shared read-only snapshots for browser refreshes."""
import copy
import hashlib
import json
import threading
import time
import uuid


class SnapshotCache:
    def __init__(self):
        self.epoch = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.entries = {}
        self.closed = False

    def register(self, name, interval, collector=None):
        with self.lock:
            self.entries[name] = {"interval": interval, "collector": collector,
                                  "data": None, "json": "null", "version": "0",
                                  "sampled_at": None, "attempted": float("-inf"),
                                  "refreshing": False, "error": None}

    def publish(self, name, data):
        with self.lock:
            entry = self.entries.get(name)
            if entry is None or self.closed:
                return
            if entry["data"] != data or entry["sampled_at"] is None:
                encoded = json.dumps(data, sort_keys=True, separators=(",", ":"))
                entry.update(data=json.loads(encoded), json=encoded,
                             version=hashlib.sha256(encoded.encode()).hexdigest()[:16])
                if name in ("drives", "base", "estimates", "telemetry") and "queue" in self.entries:
                    self.entries["queue"]["attempted"] = float("-inf")
            entry.update(sampled_at=time.time(), error=None)

    def peek(self, name):
        with self.lock:
            return copy.deepcopy(self.entries.get(name, {}).get("data"))

    def invalidate(self, *names):
        with self.lock:
            for name in names:
                if name in self.entries:
                    self.entries[name]["attempted"] = float("-inf")

    def request(self, names):
        pending = []
        now = time.monotonic()
        with self.lock:
            for name in names:
                entry = self.entries[name]
                if (not self.closed and entry["collector"] and not entry["refreshing"]
                        and now - entry["attempted"] >= entry["interval"]):
                    entry.update(refreshing=True, attempted=now)
                    pending.append((name, entry["collector"]))
        for name, collector in pending:
            threading.Thread(target=self._refresh, args=(name, collector),
                             name="riparr-ui-" + name, daemon=True).start()

    def _refresh(self, name, collector):
        try:
            self.publish(name, collector())
        except Exception as error:
            with self.lock:
                self.entries[name]["error"] = str(error)
        finally:
            with self.lock:
                self.entries[name]["refreshing"] = False

    def packet(self, names):
        with self.lock:
            versions, fields, sources = [], [], {}
            for name in names:
                entry = self.entries[name]
                versions.append(name + ":" + entry["version"])
                fields.append(json.dumps(name) + ":" + entry["json"])
                sampled = entry["sampled_at"]
                sources[name] = {"sampled_at": sampled, "error": entry["error"],
                                 "stale": sampled is None or bool(entry["error"]) or
                                 time.time() - sampled > entry["interval"] * 2}
        tag = '"' + hashlib.sha256((self.epoch + "|" + "|".join(versions)).encode()).hexdigest()[:24] + '"'
        body = ('{"epoch":' + json.dumps(self.epoch) + ',"topics":{' + ",".join(fields)
                + '},"sources":' + json.dumps(sources, separators=(",", ":")) + '}')
        return tag, body, sources

    def close(self):
        with self.lock:
            self.closed = True


cache = SnapshotCache()
cache.register("drives", 10)


def publish_drives(drives):
    cache.publish("drives", drives)