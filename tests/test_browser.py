"""Drives each installed browser for real against a local server."""

import asyncio
import glob
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from handlecheck import browser as browser_module
from handlecheck.browser import BrowserError, find_browsers, launch_browser
from handlecheck.checker import Checker, Site, Status, check_site


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path
        if path.startswith("/firefox-only/"):
            allowed = "Firefox" in self.headers.get("User-Agent", "")
            data = b"<title>Profile</title>" if allowed else b"<title>Denied</title>"
            self.send_response(200 if allowed else 403)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path.startswith("/redirect/"):
            self.send_response(302)
            self.send_header("Location", "/login-page")
            self.end_headers()
            return
        if path.startswith("/empty404/"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        pages = {
            "/alice": (200, "<title>Alice</title>"),
            "/login-page": (200, "<title>Log in</title>"),
            # What Prototype.js does, which makes JSON.stringify encode arrays twice.
            "/prototype/alice": (
                200,
                "<script>Array.prototype.toJSON=function(){return JSON.stringify(Array.from(this))}</script>"
                "<title>alice</title>",
            ),
            "/rendered/alice": (
                200,
                "<script>setTimeout(()=>{document.title='alice profile'},1500)</script>",
            ),
        }
        code, html = pages.get(path, (404, "<title>Gone</title>"))
        data = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def base_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture(params=["chromium", "chrome", "firefox"])
def family(request):
    if not find_browsers(request.param):
        pytest.skip(f"{request.param} is not installed")
    return request.param


def probe(family, base_url, path, username="alice", **kwargs):
    target = Site(
        name="Local",
        category="dev",
        profile_url=f"{base_url}{path}",
        known_user="alice",
        **kwargs,
    )

    async def go():
        browser = await launch_browser(family)
        try:
            return await check_site(None, browser, target, username, timeout=15)
        finally:
            await browser.close()

    return asyncio.run(go())


def test_existing_page(family, base_url):
    assert probe(family, base_url, "/{username}").status is Status.FOUND


def test_missing_page(family, base_url):
    assert probe(family, base_url, "/nobody/{username}").status is Status.NOT_FOUND


def test_error_status_with_empty_body(family, base_url):
    assert probe(family, base_url, "/empty404/{username}").status is Status.NOT_FOUND


def test_page_that_changes_json_can_still_be_read(family, base_url):
    assert probe(family, base_url, "/prototype/{username}").status is Status.FOUND


def test_redirect_away_is_unknown(family, base_url):
    result = probe(family, base_url, "/redirect/{username}")
    assert result.status is Status.UNKNOWN
    assert "redirected" in result.reason


def test_waits_for_scripts_to_render(family, base_url):
    result = probe(family, base_url, "/rendered/{username}", found_regex="{username} profile")
    assert result.status is Status.FOUND


def test_unreachable_host_is_unknown(family):
    result = probe(family, "http://127.0.0.1:9", "/{username}")
    assert result.status is Status.UNKNOWN


def test_no_profile_left_behind(family, base_url):
    pattern = f"{tempfile.gettempdir()}/handlecheck-*"
    before = set(glob.glob(pattern))
    probe(family, base_url, "/{username}")
    assert set(glob.glob(pattern)) == before


def test_preference_order(monkeypatch):
    installed = {"chromium-browser", "google-chrome", "firefox"}
    monkeypatch.setattr(browser_module.shutil, "which", lambda n: f"/bin/{n}" if n in installed else None)
    assert [c.family for c in find_browsers()] == ["chromium", "chrome", "firefox"]

    installed.discard("chromium-browser")
    assert [c.family for c in find_browsers()] == ["chrome", "firefox"]

    installed.discard("google-chrome")
    assert [c.family for c in find_browsers()] == ["firefox"]

    installed.clear()
    assert find_browsers() == []


def test_falls_through_to_next_browser_when_one_will_not_start(monkeypatch):
    installed = {"chromium", "firefox"}
    monkeypatch.setattr(browser_module.shutil, "which", lambda n: f"/bin/{n}" if n in installed else None)

    async def broken(binary, label):
        raise BrowserError("crashed")

    class Started:
        _lock_fd = None

    async def fake_firefox(binary):
        return Started()

    monkeypatch.setattr(browser_module, "_launch_chromium", broken)
    monkeypatch.setattr(browser_module, "_launch_firefox", fake_firefox)
    started = asyncio.run(launch_browser())
    assert isinstance(started, Started)
    browser_module._release_lock(started._lock_fd)


def test_no_browser_installed_raises(monkeypatch):
    monkeypatch.setattr(browser_module.shutil, "which", lambda n: None)
    with pytest.raises(BrowserError):
        asyncio.run(launch_browser())


def local_site(base_url, path):
    return Site(name="Local", category="dev", profile_url=f"{base_url}{path}", known_user="alice")


def run_checker(site, choice="auto", username="alice"):
    async def go():
        async with Checker([site], timeout=15, browser=choice) as checker:
            return (await checker.check_all(username))[0]

    return asyncio.run(go())


def test_blocked_site_is_retried_through_to_firefox(base_url):
    if not find_browsers("firefox") or len(find_browsers()) < 2:
        pytest.skip("needs Firefox and another browser")
    result = run_checker(local_site(base_url, "/firefox-only/{username}"))
    assert result.status is Status.FOUND
    assert result.via == "Firefox"


def test_named_browser_is_not_retried_elsewhere(base_url):
    chromium_family = next((c.family for c in find_browsers() if c.family != "firefox"), None)
    if not chromium_family:
        pytest.skip("needs Chromium or Chrome")
    result = run_checker(local_site(base_url, "/firefox-only/{username}"), chromium_family)
    assert result.status is Status.UNKNOWN
    assert "403" in result.reason


def test_tabs_are_private_from_each_other(family, base_url):
    async def go():
        browser = await launch_browser(family)
        try:
            first = await browser.new_tab()
            await first.goto(f"{base_url}/alice", 15)
            await first.snapshot()
            await evaluate(first, "document.cookie = 'seen=1'; localStorage.setItem('seen', '1')")
            assert "seen=1" in await evaluate(first, "document.cookie")
            second = await browser.new_tab()
            await second.goto(f"{base_url}/alice", 15)
            await second.snapshot()
            cookies = await evaluate(second, "document.cookie")
            stored = await evaluate(second, "String(localStorage.getItem('seen'))")
            await first.close()
            await second.close()
            return cookies, stored
        finally:
            await browser.close()

    assert asyncio.run(go()) == ("", "null")


async def evaluate(tab, expression):
    browser = tab._browser
    if browser.label == "Firefox":
        result = await browser._rpc.call(
            "script.evaluate",
            {"expression": expression, "target": {"context": tab._context}, "awaitPromise": False},
        )
        return result["result"].get("value", "")
    result = await browser._rpc.call(
        "Runtime.evaluate", {"expression": expression, "returnByValue": True}, tab._session
    )
    return result["result"].get("value", "")


class FakeTab:
    def __init__(self, browser):
        self.browser = browser
        self.status = None

    async def goto(self, url, timeout, unmodified=False):
        self.browser.loads.append(url)
        self.browser.unmodified = unmodified
        self.status = self.browser.status_for(url)

    async def snapshot(self):
        return self.browser.last_url(), ""

    async def close(self):
        self.browser.open_tabs -= 1


class FakeBrowser:
    """Records what the runner does, standing in for a real browser."""

    def __init__(self, label, status, world):
        self.label = label
        self.status = status
        self.world = world
        self.loads = []
        self.open_tabs = 0
        self.unmodified = None

    def status_for(self, url):
        return self.status(url) if callable(self.status) else self.status

    def last_url(self):
        return self.loads[-1]

    async def new_tab(self):
        self.open_tabs += 1
        self.world["peak_tabs"] = max(self.world["peak_tabs"], self.open_tabs)
        await asyncio.sleep(0)
        return FakeTab(self)

    async def close(self):
        assert self.open_tabs == 0, "tabs left open at restart"
        self.world["running"] -= 1
        self.world["order"].append(("stop", self.label))


def fake_world(monkeypatch, statuses):
    """Install browsers A, B... whose responses are given by `statuses`."""
    world = {"running": 0, "peak_running": 0, "peak_tabs": 0, "order": [], "starts": {}, "instances": []}
    monkeypatch.setattr("handlecheck.checker.SETTLE_MIN", 0)
    monkeypatch.setattr(
        browser_module,
        "find_browsers",
        lambda choice: [browser_module._Candidate(n, n, n) for n in statuses],
    )

    async def start(candidate):
        world["running"] += 1
        world["peak_running"] = max(world["peak_running"], world["running"])
        world["starts"][candidate.label] = world["starts"].get(candidate.label, 0) + 1
        world["order"].append(("start", candidate.label))
        browser = FakeBrowser(candidate.label, statuses[candidate.label], world)
        world["instances"].append(browser)
        return browser

    monkeypatch.setattr(browser_module, "start_browser", start)
    return world


def fake_sites(count, **fields):
    return [
        Site(name=f"S{i}", category="dev", profile_url=f"https://s{i}.example/{{username}}", known_user="x", **fields)
        for i in range(count)
    ]


def run_all(sites, username="alice", **kwargs):
    async def go():
        async with Checker(sites, timeout=5, **kwargs) as checker:
            return await checker.check_all(username)

    return asyncio.run(go())


def test_error_on_one_site_does_not_stop_the_others(monkeypatch, capsys):
    statuses = {"A": 200}
    fake_world(monkeypatch, statuses)
    real = Checker.check_in_tab

    async def explode_on_one(self, site, username, tab, label):
        if site.name == "S1":
            raise ValueError("odd page")
        return await real(self, site, username, tab, label)

    monkeypatch.setattr(Checker, "check_in_tab", explode_on_one)
    results = {r.site.name: r.status for r in run_all(fake_sites(3))}
    assert results["S1"] is Status.UNKNOWN
    assert results["S0"] is Status.FOUND and results["S2"] is Status.FOUND
    assert "S1" in capsys.readouterr().err


def test_browser_restarts_every_30_tabs_and_never_overlaps(monkeypatch):
    world = fake_world(monkeypatch, {"A": 200})
    results = run_all(fake_sites(65))
    assert [r.status for r in results] == [Status.FOUND] * 65
    assert world["starts"] == {"A": 3}
    assert world["peak_running"] == 1
    assert world["peak_tabs"] <= 5
    assert [len(b.loads) for b in world["instances"]] == [30, 30, 5]


def test_retests_wait_until_first_browser_has_finished_then_restart_too(monkeypatch):
    # The first browser is blocked from everything, the second answers.
    world = fake_world(monkeypatch, {"A": 403, "B": 200})
    results = run_all(fake_sites(35))
    assert all(r.status is Status.FOUND and r.via == "B" for r in results)
    assert world["peak_running"] == 1
    assert world["starts"] == {"A": 2, "B": 2}
    assert world["order"] == [
        ("start", "A"), ("stop", "A"), ("start", "A"), ("stop", "A"),
        ("start", "B"), ("stop", "B"), ("start", "B"), ("stop", "B"),
    ]


def test_only_unsettled_sites_are_retested(monkeypatch):
    def first(url):
        return 403 if "s1." in url else 404

    world = fake_world(monkeypatch, {"A": first, "B": 200})
    results = run_all(fake_sites(3))
    assert [r.status for r in results] == [Status.NOT_FOUND, Status.FOUND, Status.NOT_FOUND]
    assert world["instances"][1].loads == ["https://s1.example/alice"]


def test_retests_skip_a_browser_with_the_same_engine(monkeypatch):
    world = fake_world(monkeypatch, {"chromium": 403, "chrome": 200, "firefox": 200})
    result = run_all(fake_sites(1))[0]
    assert result.via == "firefox"
    assert "chrome" not in world["starts"]


def test_chrome_stands_in_when_chromium_will_not_start(monkeypatch):
    world = fake_world(monkeypatch, {"chromium": 200, "chrome": 200, "firefox": 200})
    real_start = browser_module.start_browser

    async def start(candidate):
        if candidate.family == "chromium":
            raise BrowserError("crashed")
        return await real_start(candidate)

    monkeypatch.setattr(browser_module, "start_browser", start)
    assert run_all(fake_sites(1))[0].via == "chrome"


def test_dead_sites_are_not_retested(monkeypatch):
    class DeadTab(FakeTab):
        async def goto(self, url, timeout, unmodified=False):
            self.browser.loads.append(url)
            raise BrowserError("net::ERR_NAME_NOT_RESOLVED")

    world = fake_world(monkeypatch, {"A": 200, "B": 200})
    monkeypatch.setattr(sys.modules[__name__], "FakeTab", DeadTab)
    result = run_all(fake_sites(1))[0]
    assert result.status is Status.UNKNOWN
    assert "B" not in world["starts"]


def test_killed_runs_profile_is_cleared_by_the_next_start(family, base_url):
    stale = tempfile.mkdtemp(prefix="handlecheck-")
    probe(family, base_url, "/{username}")
    assert not os.path.exists(stale)


def test_clear_answers_never_start_the_next_browser(monkeypatch):
    world = fake_world(monkeypatch, {"A": 404, "B": 200})
    results = run_all(fake_sites(2))
    assert all(r.status is Status.NOT_FOUND for r in results)
    assert "B" not in world["starts"]


def test_all_browsers_blocked_stays_unknown(monkeypatch):
    world = fake_world(monkeypatch, {"A": 403, "B": 429, "C": 403})
    result = run_all(fake_sites(1))[0]
    assert result.status is Status.UNKNOWN
    assert world["starts"] == {"A": 1, "B": 1, "C": 1}


def test_reserved_name_starts_no_browser(monkeypatch):
    world = fake_world(monkeypatch, {"A": 200})
    result = run_all(fake_sites(1), username="login")[0]
    assert result.status is Status.UNKNOWN
    assert world["starts"] == {}


def test_public_page_only_sites_get_one_plain_attempt(monkeypatch):
    world = fake_world(monkeypatch, {"A": 403, "B": 200})
    result = run_all(fake_sites(1, public_page_only=True))[0]
    assert result.status is Status.UNKNOWN
    assert world["instances"][0].unmodified is True
    assert "B" not in world["starts"]


def test_other_sites_may_be_adjusted(monkeypatch):
    world = fake_world(monkeypatch, {"A": 200})
    run_all(fake_sites(1))
    assert world["instances"][0].unmodified is False


def test_browser_that_will_not_start_passes_its_sites_on(monkeypatch):
    world = fake_world(monkeypatch, {"A": 200, "B": 200})
    real_start = browser_module.start_browser

    async def start(candidate):
        if candidate.label == "A":
            raise BrowserError("crashed")
        return await real_start(candidate)

    monkeypatch.setattr(browser_module, "start_browser", start)
    results = run_all(fake_sites(2))
    assert all(r.via == "B" for r in results)


def test_no_browser_means_unknown_with_a_reason(monkeypatch):
    fake_world(monkeypatch, {})
    result = run_all(fake_sites(1))[0]
    assert result.status is Status.UNKNOWN
    assert "no browser" in result.reason


def test_new_tabs_wait_for_memory_then_give_up(monkeypatch):
    world = fake_world(monkeypatch, {"A": 200})
    monkeypatch.setattr("handlecheck.checker._memory_available_mb", lambda: 10)
    monkeypatch.setattr("handlecheck.checker.MEMORY_WAIT_SECONDS", 0)
    result = run_all(fake_sites(1), browser="chromium")[0]
    assert result.status is Status.UNKNOWN
    assert world["instances"][0].loads == []


def test_user_agent_override_applies_only_when_allowed(base_url):
    family = next((c.family for c in find_browsers() if c.family != "firefox"), None)
    if not family:
        pytest.skip("needs Chromium or Chrome")

    async def agent(unmodified):
        browser = await launch_browser(family)
        try:
            tab = await browser.new_tab()
            await tab.goto(f"{base_url}/alice", 15, unmodified=unmodified)
            value = await evaluate(tab, "navigator.userAgent")
            await tab.close()
            return value
        finally:
            await browser.close()

    assert "HeadlessChrome" not in asyncio.run(agent(False))
    assert "HeadlessChrome" in asyncio.run(agent(True))


def test_second_browser_waits_for_the_first_to_close(family):
    async def go():
        first = await launch_browser(family)
        waiter = asyncio.create_task(launch_browser(family))
        await asyncio.sleep(2)
        blocked = not waiter.done()
        await first.close()
        second = await waiter
        await second.close()
        return blocked

    assert asyncio.run(go())


def test_root_refuses_to_start_a_browser_without_the_account(monkeypatch):
    monkeypatch.setattr(browser_module.os, "geteuid", lambda: 0)

    def missing(_name):
        raise KeyError(_name)

    monkeypatch.setattr(browser_module.pwd, "getpwnam", missing)
    with pytest.raises(browser_module.BrowserError, match="run install.sh"):
        browser_module._browser_account()


def test_other_users_keep_their_own_account(monkeypatch):
    monkeypatch.setattr(browser_module.os, "geteuid", lambda: 1000)
    assert browser_module._browser_account() is None
    assert browser_module._account_options(None) == {}


@pytest.mark.skipif(os.geteuid() != 0, reason="only root can switch accounts")
def test_browser_runs_as_the_browser_account(family):
    async def go():
        candidate = browser_module.find_browsers(family)[0]
        browser = await browser_module.start_browser(candidate)
        try:
            with open(f"/proc/{browser._process.pid}/status") as f:
                uid_line = next(line for line in f if line.startswith("Uid:"))
            return int(uid_line.split()[1]), os.stat(browser._profile.name).st_uid
        finally:
            await browser.close()

    expected = browser_module.pwd.getpwnam(browser_module.BROWSER_USER).pw_uid
    assert asyncio.run(go()) == (expected, expected)
