#!/usr/bin/env python3
"""Browser refresh regressions. Run with Playwright installed: python server/ui-refresh.test.py."""
import functools
import argparse
import http.server
import json
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def translate_path(self, path):
        if path.startswith("/static/"):
            path = "/server" + path
        return super().translate_path(path)


def retention_checks():
    root = Path(__file__).resolve().parent.parent
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as manager:
            browser = manager.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page()
            page.route("**/api/**", lambda route: route.fulfill(
                content_type="application/json", body=json.dumps(
                    {"has_users": False, "complete": False, "version": "test"})))
            page.goto("http://127.0.0.1:%d/server/static/index.html" % server.server_port)
            page.wait_for_selector("#wz-body")
            page.evaluate("""async () => {
              views.queue = async () => '<select id="draft-mode"><option>one</option><option>two</option></select><input id="draft-name">';
              await route();
              window.originalSelect = document.querySelector('#draft-mode');
              window.originalInput = document.querySelector('#draft-name');
              originalSelect.value = 'two'; originalInput.value = 'unsaved draft';
              views.queue = async () => { const error = new Error('connection refused'); error.transient = true; throw error; };
              await route();
            }""")
            assert page.evaluate("document.querySelector('#draft-mode') === originalSelect")
            assert page.evaluate("document.querySelector('#draft-name') === originalInput")
            assert page.locator("#draft-mode").input_value() == "two"
            assert page.locator("#draft-name").input_value() == "unsaved draft"
            page.evaluate("""() => {
              polling.pause();
                            document.querySelector('#wizard').classList.add('hidden');
                            document.querySelector('#shell').classList.remove('hidden');
              const content = document.querySelector('#content');
              patchMarkup(content, '<div id="job-1" class="job needs"><input id="title" value="old"></div><div id="job-2" class="job"><span>10%</span></div><details id="details"><summary>Details</summary><p>old</p></details>');
              window.titleNode = document.querySelector('#title');
              titleNode.value = 'draft'; titleNode.dispatchEvent(new Event('input', {bubbles:true}));
              titleNode.focus(); titleNode.setSelectionRange(2, 4);
              document.querySelector('#details').open = true;
              patchMarkup(content, '<div id="job-1" class="job"><b>Now ripping</b></div><div id="job-2" class="job"><span>40%</span></div><div id="job-3" class="job">new</div><details id="details"><summary>Details</summary><p>new</p></details>');
            }""")
            assert page.evaluate("document.querySelector('#title') === titleNode && document.activeElement === titleNode")
            assert page.locator("#title").input_value() == "draft"
            assert page.evaluate("titleNode.selectionStart === 2 && titleNode.selectionEnd === 4")
            assert page.locator("#job-2").inner_text() == "40%"
            assert page.locator("#job-3").count() == 1
            assert page.evaluate("document.querySelector('#details').open")
            assert page.locator("#job-1").get_attribute("data-ui-pending") == "true"
            page.evaluate("""() => {
              dirtyFields.delete(titleNode); titleNode.blur();
              patchMarkup(document.querySelector('#content'), '<div id="job-1" class="job"><b>Now ripping</b></div><div id="job-2" class="job"><span>40%</span></div><div id="job-3" class="job">new</div><details id="details"><summary>Details</summary><p>new</p></details>');
              if (document.querySelector('#title')) throw new Error('saved job must transition');
              patchMarkup(document.querySelector('#content'), '<label id="changed-form"><input id="changed-draft" value="old"></label><div id="live-progress">10%</div>');
              const changedDraft = document.querySelector('#changed-draft');
              changedDraft.value = 'protected'; changedDraft.dispatchEvent(new Event('input', {bubbles:true}));
              patchMarkup(document.querySelector('#content'), '<div id="live-progress">20%</div>');
              if (document.querySelector('#changed-draft') !== changedDraft || changedDraft.value !== 'protected') throw new Error('container removal lost draft');
              if (document.querySelector('#live-progress').textContent !== '20%') throw new Error('unrelated progress stopped');
              dirtyFields.delete(changedDraft);
              patchMarkup(document.querySelector('#content'), '<p>different node type</p><span>next node</span>');
              switchView('settings/test', '<input id="settings-draft" value="server">');
              window.settingsDraft = document.querySelector('#settings-draft');
              settingsDraft.value = 'kept'; settingsDraft.dispatchEvent(new Event('input', {bubbles:true}));
              switchView('history', '<p>History</p>');
              switchView('settings/test');
            }""")
            assert page.evaluate("document.querySelector('#settings-draft') === settingsDraft")
            assert page.locator("#settings-draft").input_value() == "kept"
            page.evaluate("resetPrivateViews()")
            assert page.locator("#content input").count() == 0
            assert page.evaluate("cachedViews.size === 0")
            browser.close()
            print("PASS transient retention, keyed progress, deferred edits, caret/details, navigation and private-cache cleanup")
    finally:
        server.shutdown()
        server.server_close()


def network_checks():
        root = Path(__file__).resolve().parent.parent
        server = http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0), functools.partial(Handler, directory=str(root)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
                with sync_playwright() as manager:
                        browser = manager.chromium.launch(channel="msedge", headless=True)
                        page = browser.new_page()
                        page.route("**/api/**", lambda route: route.fulfill(
                                content_type="application/json", body=json.dumps(
                                        {"has_users": False, "complete": False, "version": "test"})))
                        page.goto("http://127.0.0.1:%d/server/static/index.html" % server.server_port)
                        page.wait_for_selector("#wz-body")
                        result = page.evaluate("""async () => {
                            polling.pause(); polling.topics.clear(); polling.schedule = () => {};
                            const assert = (test, message) => { if (!test) throw new Error(message); };
                            const settle = async () => { for (let count=0; count<40; count++) await Promise.resolve(); };
                            let now = 1000000, offline = false, mutationCount = 0, readCount = 0;
                            Date.now = () => now;
                            document.querySelector('#gate').classList.add('hidden');
                            document.querySelector('#wizard').classList.add('hidden');
                            document.querySelector('#shell').classList.remove('hidden');
                            state.status = {makemkv:{installed:true}, wifi:{connected:true}, storage:{}, drives:[]};
                            state.settings = {};
                            const markup = '<button id="backend-command">Command</button><select id="mode"><option>one</option><option>two</option></select><input id="draft"><details id="open"><summary>Details</summary></details>';
                            const response = (data, status=200, headers={}) => ({status, ok:status<400, headers:new Headers({'Content-Type':'application/json', ...headers}), json:async () => data});
                            window.fetch = async (path, options) => {
                                readCount++;
                                if (options.method !== 'GET') mutationCount++;
                                if (offline) throw new TypeError('Failed to fetch');
                                return response(path.includes('ready') ? {ok:true, epoch:'first'} : {markup});
                            };
                            views.queue = async () => (await api.get('/api/test/view')).markup;
                            await route(); polling.paused = false;
                            const draft = document.querySelector('#draft'), mode = document.querySelector('#mode');
                            draft.value = 'retained'; draft.dispatchEvent(new Event('input', {bubbles:true}));
                            mode.value = 'two'; mode.dispatchEvent(new Event('change', {bubbles:true}));
                            document.querySelector('#open').open = true;
                            offline = true;
                            try { await api.post('/api/test/mutation', {}); } catch (error) {}
                            assert(connection.status === 'reconnecting', 'first failure status');
                            assert(document.querySelector('#backend-command').disabled, 'commands disabled');
                            assert(!draft.disabled && !mode.disabled, 'draft fields stay editable');
                            for (let count=0; count<2; count++) {
                                now += 10000; polling.topics.get('live').next = now;
                                polling.tick(); polling.tick(); await settle();
                            }
                            assert(connection.status === 'disconnected', 'three failed attempts');
                            assert(connection.delay() >= 20000, 'recovery backoff');
                            connection.failures = 9;
                            assert(connection.delay() >= 30000 && connection.delay() < 30250, 'backoff cap');
                            assert(!document.querySelector('#connection-banner').hidden, 'warning banner');
                            now += 1000; connection.draw();
                            assert(document.querySelector('#connection-age').textContent.includes('ago'), 'last-data age');
                            offline = false; state.serverEpoch = 'old';
                            polling.topics.get('live').next = now;
                            polling.tick(); await settle();
                            assert(connection.status === 'connected', 'resync enables connection');
                            assert(!document.querySelector('#backend-command').disabled, 'commands restored');
                            assert(document.querySelector('#draft') === draft && draft.value === 'retained', 'same draft node');
                            assert(mode.value === 'two' && document.querySelector('#open').open, 'mode and details retained');
                            assert(mutationCount === 1, 'mutation must never replay');

                            let tagged = 0;
                            window.fetch = async (path, options) => {
                                tagged++;
                                return tagged === 1 ? response({epoch:'first', topics:{queue:{progress:10}}, sources:{queue:{sampled_at:1}}}, 200, {ETag:'"stable"'})
                                    : response(null, 304, {'X-Riparr-Sources':JSON.stringify({queue:{sampled_at:2}})});
                            };
                            await api.get('/api/tagged', {etag:true});
                            const unchanged = await api.get('/api/tagged', {etag:true});
                            assert(unchanged.topics.queue.progress === 10 && unchanged.sources.queue.sampled_at === 2, '304 metadata');
                            const observer = new MutationObserver(() => {});
                            observer.observe(document.querySelector('#content'), {subtree:true, childList:true, attributes:true, characterData:true});
                            patchMarkup(document.querySelector('#content'), markup);
                            assert(observer.takeRecords().length === 0, 'unchanged markup must be a no-op'); observer.disconnect();

                            let resolveRead, slowReads=0;
                            window.fetch = (path, options) => {
                                slowReads++;
                                return new Promise((resolve, reject) => {
                                    resolveRead = () => resolve(response({ok:true}));
                                    options.signal.addEventListener('abort', () => reject(new DOMException('cancelled','AbortError')));
                                });
                            };
                            const first = api.get('/api/slow'), second = api.get('/api/slow');
                            assert(first === second && slowReads === 1, 'deduplicate slow reads');
                            api.cancelReads();
                            try { await first; } catch(error) { assert(error.cancelled && !error.transient, 'intentional cancellation'); }
                            assert(connection.status === 'connected', 'cancellation is not disconnection');

                            let hidden = true;
                            Object.defineProperty(document, 'hidden', {configurable:true, get:() => hidden});
                            document.dispatchEvent(new Event('visibilitychange'));
                            const beforeHidden = slowReads; polling.tick(); await settle();
                            assert(polling.paused && slowReads === beforeHidden, 'hidden tab does not poll');
                            hidden = false; document.dispatchEvent(new Event('visibilitychange'));
                            assert(!polling.paused && state.resync, 'visible tab resyncs');
                            delete document.hidden;

                            window.fetch = async () => { throw new TypeError('optional telemetry'); };
                            try { await api.get('/api/optional', {optional:true}); } catch(error) {}
                            assert(connection.status === 'connected', 'optional source does not mark server offline');
                            window.fetch = async () => response({}, 401);
                            try { await api.get('/api/protected'); } catch(error) { assert(error.status === 401, 'auth classification'); }
                            assert(polling.paused && cachedViews.size === 0 && api.cache.size === 0, 'auth clears protected caches');
                            assert(document.querySelector('#content').childElementCount === 0, 'auth clears drafts');
                            assert(connection.status === 'connected', 'auth error is not a transport failure');
                            return {mutationCount, tagged, slowReads};
                        }""")
                        assert result["mutationCount"] == 1
                        browser.close()
                        print("PASS adaptive recovery, no command replay, ETag/no-op, dedup/cancellation, hidden-tab and authentication handling")
        finally:
                server.shutdown()
                server.server_close()


def runtime_checks(url, container=None):
    with sync_playwright() as manager:
        browser = manager.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        client = context.request
        setup = client.get(url + "/api/setup/state").json()
        assert not setup["has_users"], "Runtime test requires a fresh disposable instance"
        assert client.post(url + "/api/setup/user", data={
            "username": "refresh-test", "password": "refresh-test-only"}).ok
        assert client.post(url + "/api/library/directory", data={}).ok
        assert client.post(url + "/api/setup/complete", data={}).ok
        page = context.new_page()
        errors, requests = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append(request.url.split("/api/")[-1])
                if "/api/" in request.url else None)
        page.goto(url)
        page.wait_for_selector("#ar-route", timeout=30000)
        page.wait_for_function("connection.status === 'connected' && state.liveFresh")
        page.evaluate("""() => {
          window.runtimePolls = 0;
          const fetch = window.fetch;
          window.fetch = (...args) => {
            if (String(args[0]).includes('/api/ui/live?scopes=base,drives,queue')) window.runtimePolls++;
            return fetch(...args);
          };
          window.routeNode = document.querySelector('#ar-route');
          routeNode.focus();
        }""")
        requests.clear()
        started = time.monotonic()
        page.wait_for_function("window.runtimePolls >= 3", timeout=35000)
        elapsed = time.monotonic() - started
        assert elapsed >= 17, "Idle queue must not poll every few seconds"
        assert not any(path.split("?")[0] in ("status", "autorip", "queue") for path in requests), requests
        assert page.evaluate("document.querySelector('#ar-route') === routeNode && document.activeElement === routeNode")
        page.screenshot(path=str(Path(tempfile.gettempdir()) / "riparr-refresh-connected.png"))
        page.evaluate("location.hash = '#/settings/ripping'")
        page.wait_for_selector('[data-set="min_title_seconds"]')
        field = page.locator('[data-set="min_title_seconds"]')
        field.fill("401")
        page.evaluate("window.runtimeDraft = document.querySelector('[data-set=\"min_title_seconds\"]')")
        epoch = client.get(url + "/api/ui/ready").json()["epoch"]
        if container:
            assert container.startswith("riparr-refresh-browser-"), "Only stop this test's disposable container"
            subprocess.run(["docker", "stop", "--time", "5", container], check=True, capture_output=True)
        else:
            page.route("**/api/**", lambda route: route.abort("connectionrefused"))
        page.evaluate("api.get('/api/ui/ready').catch(() => {})")
        page.wait_for_function("connection.status !== 'connected'")
        assert page.locator("#save-settings").is_disabled()
        assert not field.is_disabled() and field.input_value() == "401"
        for width, height, label in ((1280, 900, "desktop"), (390, 844, "mobile")):
            page.set_viewport_size({"width": width, "height": height})
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_function("innerWidth > 800 || document.querySelector('#sidebar').getBoundingClientRect().right <= 1")
            banner = page.locator("#connection-banner").bounding_box()
            heading = page.locator("#content .page-head").bounding_box()
            assert heading["y"] >= banner["y"] + banner["height"], (label, heading, banner)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), label
            page.screenshot(path=str(Path(tempfile.gettempdir()) / ("riparr-refresh-" + label + ".png")))
        if container:
            subprocess.run(["docker", "start", container], check=True, capture_output=True)
        else:
            page.unroute("**/api/**")
        page.locator("#connection-retry").click()
        page.wait_for_function("connection.status === 'connected'", timeout=45000)
        assert page.evaluate("document.querySelector('[data-set=\"min_title_seconds\"]') === runtimeDraft")
        assert field.input_value() == "401" and not page.locator("#save-settings").is_disabled()
        if container:
            assert client.get(url + "/api/ui/ready").json()["epoch"] != epoch
        assert not errors, errors
        browser.close()
        print("PASS real runtime: 3 idle snapshots in %.1fs, no broad polling, same control/focus, desktop/mobile and restart recovery" % elapsed)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="Fresh disposable runtime URL with a marked library")
    parser.add_argument("--restart-container", help="Disposable riparr-refresh-browser-* container")
    args = parser.parse_args()
    retention_checks()
    network_checks()
    if args.url:
        runtime_checks(args.url.rstrip("/"), args.restart_container)


if __name__ == "__main__":
        main()