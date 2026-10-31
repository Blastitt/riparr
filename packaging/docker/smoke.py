#!/usr/bin/env python3
"""Exercise a fresh disposable Docker instance. Uses test-only account credentials."""
import argparse
import http.cookiejar
import json
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="URL of a fresh disposable Riparr container")
    args = parser.parse_args()
    client = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        query = urllib.request.Request(args.url.rstrip("/") + path, data=data,
                                       headers={"Content-Type": "application/json"})
        with client.open(query, timeout=30) as response:
            return json.load(response)

    setup = request("/api/setup/state")
    assert not setup["has_users"], "Use a fresh disposable instance, not existing user data"
    assert setup["deployment"]["container"]
    assert setup["makemkv"]["installed"] and setup["makemkv"]["eula_accepted"]
    request("/api/setup/user", {"username": "docker-smoke", "password": "docker-test-only"})
    assert request("/api/library/directory", {})["verified"]
    state = request("/api/status")
    assert state["deployment"]["library_backend"] == "directory"
    assert not state["system"]["mock"]
    assert state["library"]["mounted"]
    assert request("/api/system/components")["ok"]
    assert request("/api/backup/tools")["ready"]
    assert request("/api/music/tools")["ready"]
    assert not request("/api/update/install", {})["ok"]
    for path, body in (("/api/wifi/scan", {}), ("/api/shares/remount", {}),
                       ("/api/system/power", {"action": "reboot"})):
        try:
            request(path, body)
        except urllib.error.HTTPError as error:
            assert error.code in (400, 409, 503), (path, error.code)
        else:
            raise AssertionError("A container attempted host management: " + path)
    request("/api/setup/complete", {})
    assert request("/api/setup/state")["complete"]
    print("PASS: real Docker setup, prebuilt tools, directory storage and host/update boundaries")


if __name__ == "__main__":
    main()