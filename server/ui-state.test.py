#!/usr/bin/env python3
"""UI snapshot concurrency and freshness checks: python server/ui-state.test.py."""
import json
import asyncio
import os
import tempfile
import threading
from unittest.mock import patch

from riparr.ui_state import SnapshotCache


def backend_checks():
    if os.name == "nt":
        print("SKIP Linux endpoint checks on Windows (fcntl required)")
        return
    with tempfile.TemporaryDirectory(prefix="riparr-ui-test-") as root:
        os.environ["RIPARR_DB"] = os.path.join(root, "test.db")
        os.environ["RIPARR_APPLIANCE"] = "0"
        from riparr import db, main, ui_state as UI
        from starlette.requests import Request
        db.init()
        with patch.object(UI.SnapshotCache, "request"):
            main._configure_ui_cache()
        cache = UI.cache
        drives = [{"id": "drive", "present": False, "reads_dvd": True, "reads_bluray": False}]
        cache.publish("drives", drives)
        cache.publish("base", {"makemkv": {"installed": True}, "storage": {
            "mode": "burst", "phrase": "Room available"}, "library": {}, "key_registered": True})
        cache.publish("telemetry", {"clock": {"plausible": True, "synced": True}})
        cache.publish("estimates", {"default": {"seconds": 100, "samples": 2, "stages": {}}})
        with patch.object(main.P, "optical_drives", side_effect=AssertionError("optical probe")), \
                patch.object(main.P, "_run", side_effect=AssertionError("subprocess")), \
                patch.object(main.P, "storage_status", side_effect=AssertionError("storage probe")), \
                patch.object(main.P, "library_status", side_effect=AssertionError("library probe")), \
                patch.object(db, "typical_job_seconds", side_effect=AssertionError("history")), \
                patch.object(db, "typical_stage_seconds", side_effect=AssertionError("stage history")):
            snapshot = main._ui_queue()
            assert snapshot["queue"]["typical_seconds"] == 100
            cache.publish("queue", snapshot)
            def request(query="scopes=base,drives,queue", tag=None):
                return Request({"type": "http", "method": "GET", "path": "/api/ui/live",
                                "query_string": query.encode(), "headers":
                                [(b"if-none-match", tag.encode())] if tag else []})
            with patch.object(cache, "request") as refresh:
                response = main.ui_live(request(), user="test")
                assert response.status_code == 200
                refresh.assert_called_once()
                tag = response.headers["etag"]
                response = main.ui_live(request(tag=tag), user="test")
                assert response.status_code == 304 and not response.body
                assert "queue" in json.loads(response.headers["x-riparr-sources"])
                try:
                    main.ui_live(request("scopes=not-allowed"), user="test")
                except main.HTTPException as error:
                    assert error.status_code == 400
                else:
                    raise AssertionError("Unknown scope was accepted")
            with patch.object(db, "has_users", return_value=True):
                try:
                    main.require_user(request())
                except main.HTTPException as error:
                    assert error.status_code == 401
                else:
                    raise AssertionError("Protected snapshot did not require authentication")
            assert asyncio.run(main.ui_ready())["ok"]
        cache.close()
        print("PASS live projection has no hardware/history probes; scoped ETag/metadata, auth and readiness")


def main():
    cache = SnapshotCache()
    started, release = threading.Event(), threading.Event()
    calls = []
    def collect():
        calls.append(True)
        started.set()
        assert release.wait(2)
        return {"progress": 10}
    cache.register("queue", 5, collect)
    cache.request(["queue"])
    assert started.wait(2)
    for _ in range(20):
        cache.request(["queue"])
        assert json.loads(cache.packet(["queue"])[1])["topics"]["queue"] is None
    assert len(calls) == 1
    worker = next(thread for thread in threading.enumerate() if thread.name == "riparr-ui-queue")
    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert cache.peek("queue") == {"progress": 10}
    tag = cache.packet(["queue"])[0]
    cache.publish("queue", {"progress": 10})
    assert cache.packet(["queue"])[0] == tag
    copy = cache.peek("queue")
    copy["progress"] = 99
    assert cache.peek("queue")["progress"] == 10
    cache._refresh("queue", lambda: (_ for _ in ()).throw(OSError("unavailable")))
    _, body, sources = cache.packet(["queue"])
    assert json.loads(body)["topics"]["queue"]["progress"] == 10
    assert sources["queue"]["stale"] and sources["queue"]["error"] == "unavailable"
    cache.publish("queue", {"progress": 20})
    assert cache.packet(["queue"])[0] != tag
    cache.close()
    print("PASS shared single-flight snapshots, stable versions, immutable reads and stale-data retention")
    backend_checks()


if __name__ == "__main__":
    main()