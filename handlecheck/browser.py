"""Drive an installed browser, headless, over its native debugging protocol.

Chromium and Chrome speak the DevTools Protocol; Firefox speaks WebDriver BiDi.
Both are used directly because they report the HTTP status of a navigation,
which the classifier depends on, and neither needs a separate driver binary.
"""

from __future__ import annotations

import asyncio
import atexit
import ctypes
import ctypes.util
import fcntl
import glob
import json
import os
import pwd
import re
import shutil
import signal
import tempfile
from dataclasses import dataclass

import websockets

# Tried in this order. Each family lists the names its binary goes by.
BROWSERS = (
    ("chromium", "Chromium", ("chromium", "chromium-browser")),
    ("chrome", "Google Chrome", ("google-chrome-stable", "google-chrome", "chrome")),
    ("firefox", "Firefox", ("firefox",)),
)
# Browsers never run as root. Root runs them as this user, made by install.sh.
BROWSER_USER = "handlecheck-browser"
STARTUP_TIMEOUT = 30.0
CALL_TIMEOUT = 15.0
MAX_HTML_CHARS = 2_000_000
# Not JSON: pages can change how it behaves, as Prototype.js does for arrays.
# A URL never contains a raw newline, so the first one ends it.
SNAPSHOT_JS = (
    "location.href + '\\n' + "
    f"(document.documentElement?.outerHTML ?? '').slice(0, {MAX_HTML_CHARS})"
)
LOCK_TIMEOUT = 600.0
# Never needed to tell whether an account exists, and slow to fetch and run.
BLOCKED_RESOURCES = [
    "*.woff", "*.woff2", "*.ttf", "*.otf", "*.eot",
    "*.mp4", "*.webm", "*.m3u8", "*.mp3", "*.ogg",
    "*google-analytics.com*", "*googletagmanager.com*", "*googlesyndication.com*",
    "*doubleclick.net*", "*adservice.google.*", "*amazon-adsystem.com*",
    "*connect.facebook.net*", "*hotjar.com*", "*scorecardresearch.com*", "*mc.yandex.ru*",
]
_WS_URL_RE = re.compile(rb"ws://[^\s]+")
_PR_SET_PDEATHSIG = 1
_LIBC = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_LIVE: set["Browser"] = set()


def _stop_all() -> None:
    for browser in list(_LIVE):
        browser.force_stop()


atexit.register(_stop_all)


class BrowserError(Exception):
    pass


class NavigationTimeout(BrowserError):
    pass


class _Rpc:
    """JSON-RPC over a websocket. Close enough to the same on both protocols."""

    def __init__(self, ws, on_event):
        self._ws = ws
        self._on_event = on_event
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                message = json.loads(raw)
                future = self._pending.pop(message.get("id"), None)
                if future is None:
                    if "method" in message:
                        self._on_event(message)
                elif future.done():
                    pass
                elif "error" in message:
                    error = message["error"]
                    detail = error.get("message") if isinstance(error, dict) else message.get("message")
                    future.set_exception(BrowserError(detail or str(error)))
                else:
                    future.set_result(message.get("result", {}))
        except websockets.ConnectionClosed:
            pass
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(BrowserError("browser connection closed"))
            self._pending.clear()

    async def call(self, method: str, params: dict | None = None, session: str | None = None, timeout: float = CALL_TIMEOUT):
        self._next_id += 1
        message = {"id": self._next_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        future = asyncio.get_running_loop().create_future()
        self._pending[self._next_id] = future
        try:
            await self._ws.send(json.dumps(message))
        except websockets.ConnectionClosed as exc:
            self._pending.pop(self._next_id, None)
            raise BrowserError("browser connection closed") from exc
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise NavigationTimeout(f"{method} timed out") from exc

    async def close(self) -> None:
        self._reader.cancel()
        await asyncio.gather(self._reader, return_exceptions=True)
        await self._ws.close()


def _parse_snapshot(value: object) -> tuple[str, str]:
    if not isinstance(value, str) or "\n" not in value:
        raise BrowserError("could not read page")
    final_url, body = value.split("\n", 1)
    return final_url, body


class Tab:
    """A private tab for one site. `status` is its main document's HTTP status."""

    status: int | None = None

    async def goto(self, url: str, timeout: float, unmodified: bool = False) -> None:
        """Load a page. `unmodified` skips every adjustment to how the browser appears."""
        raise NotImplementedError

    async def snapshot(self) -> tuple[str, str]:
        """Return the page's current (url, rendered html)."""
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class Browser:
    label = "browser"

    def __init__(self, process, profile, rpc=None):
        self._process = process
        self._profile = profile
        self._rpc = rpc
        self._lock_fd: int | None = None
        _LIVE.add(self)

    async def new_tab(self) -> Tab:
        raise NotImplementedError

    async def close(self) -> None:
        try:
            if self._rpc:
                await self._rpc.close()
            await _stop_process(self._process)
        finally:
            self.force_stop()

    def force_stop(self) -> None:
        """Make certain nothing of this browser is left. Safe to call repeatedly."""
        if self not in _LIVE:
            return
        _LIVE.discard(self)
        try:
            os.killpg(self._process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        _kill_processes_using(self._profile.name)
        drain = getattr(self._process, "_drain_task", None)
        if drain:
            try:
                drain.cancel()
            except RuntimeError:
                pass
        self._profile.cleanup()
        _release_lock(self._lock_fd)
        self._lock_fd = None


class _CdpTab(Tab):
    def __init__(self, browser: "CdpBrowser", context: str, target: str, session: str):
        self._browser = browser
        self._context = context
        self._target = target
        self._session = session
        self._frame: str | None = None
        self._loaded = asyncio.Event()
        self._navigating = False

    def handle(self, method: str, params: dict) -> None:
        if method == "Network.responseReceived":
            if params.get("type") == "Document" and params.get("frameId") == self._frame:
                self.status = params["response"]["status"]
        elif method == "Page.domContentEventFired" and self._navigating:
            # The answer is in the document, so don't wait for everything else.
            self._loaded.set()

    async def goto(self, url: str, timeout: float, unmodified: bool = False) -> None:
        rpc = self._browser._rpc
        if self._frame is None:
            await rpc.call("Page.enable", session=self._session)
            await rpc.call("Network.enable", session=self._session)
            if not unmodified:
                agent = await self._browser.plain_user_agent()
                await rpc.call("Network.setUserAgentOverride", {"userAgent": agent}, self._session)
                await rpc.call("Network.setBlockedURLs", {"urls": BLOCKED_RESOURCES}, self._session)
            tree = await rpc.call("Page.getFrameTree", session=self._session)
            self._frame = tree["frameTree"]["frame"]["id"]
        else:
            # The last page may still be loading, since its answer came early.
            await rpc.call("Page.stopLoading", session=self._session)
        self.status = None
        self._loaded.clear()
        self._navigating = True
        result = await rpc.call("Page.navigate", {"url": url}, self._session, timeout)
        error = result.get("errorText")
        if error == "net::ERR_HTTP_RESPONSE_CODE_FAILURE" and self.status is not None:
            # An error status with an empty body. The status is the answer.
            return
        if error:
            raise BrowserError(error)
        try:
            await asyncio.wait_for(self._loaded.wait(), timeout)
        except asyncio.TimeoutError:
            # Pages with endless ad or tracker requests still have a usable
            # document once the main response has arrived.
            if self.status is None:
                raise NavigationTimeout(f"timed out loading {url}") from None

    async def snapshot(self) -> tuple[str, str]:
        result = await self._browser._rpc.call(
            "Runtime.evaluate", {"expression": SNAPSHOT_JS, "returnByValue": True}, self._session
        )
        if "exceptionDetails" in result:
            raise BrowserError("could not read page")
        return _parse_snapshot(result["result"].get("value"))

    async def close(self) -> None:
        self._browser._tabs.pop(self._session, None)
        rpc = self._browser._rpc
        try:
            await rpc.call("Target.closeTarget", {"targetId": self._target})
            await rpc.call("Target.disposeBrowserContext", {"browserContextId": self._context})
        except BrowserError:
            pass


class CdpBrowser(Browser):
    def __init__(self, process, profile, label):
        super().__init__(process, profile)
        self.label = label
        self._tabs: dict[str, _CdpTab] = {}
        self._user_agent: str | None = None

    async def plain_user_agent(self) -> str:
        """The browser's own user agent, without the headless marker."""
        if self._user_agent is None:
            version = await self._rpc.call("Browser.getVersion")
            self._user_agent = version["userAgent"].replace("HeadlessChrome", "Chrome")
        return self._user_agent

    def _on_event(self, message: dict) -> None:
        tab = self._tabs.get(message.get("sessionId"))
        if tab:
            tab.handle(message["method"], message.get("params", {}))

    async def new_tab(self) -> Tab:
        created = await self._rpc.call("Target.createBrowserContext", {"disposeOnDetach": True})
        context = created["browserContextId"]
        # A page must not be able to save files on this machine.
        await self._rpc.call(
            "Browser.setDownloadBehavior", {"behavior": "deny", "browserContextId": context}
        )
        target = await self._rpc.call(
            "Target.createTarget", {"url": "about:blank", "browserContextId": context}
        )
        attached = await self._rpc.call(
            "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}
        )
        tab = _CdpTab(self, context, target["targetId"], attached["sessionId"])
        self._tabs[attached["sessionId"]] = tab
        return tab


class _BidiTab(Tab):
    def __init__(self, browser: "BidiBrowser", user_context: str, context: str):
        self._browser = browser
        self._user_context = user_context
        self._context = context
        self._url: str | None = None
        self._timeout = CALL_TIMEOUT
        self._reloaded = False

    def handle(self, params: dict) -> None:
        # Sub-resources have no navigation id. A redirect chain reports each
        # hop in order, so the last one is the final document.
        if params.get("navigation"):
            self.status = params["response"]["status"]

    async def goto(self, url: str, timeout: float, unmodified: bool = False) -> None:
        # Firefox's user agent has no headless marker, so there's nothing to adjust.
        self._url, self._timeout, self._reloaded = url, timeout, False
        await self._navigate(url, timeout)

    async def _navigate(self, url: str, timeout: float) -> None:
        self.status = None
        for attempt in (1, 2):
            try:
                await self._browser._rpc.call(
                    "browsingContext.navigate",
                    {"context": self._context, "url": url, "wait": "interactive"},
                    timeout=timeout,
                )
                return
            except NavigationTimeout:
                if self.status is None:
                    raise
                return
            except BrowserError as exc:
                # The last page may still be loading, since its answer came early,
                # and Firefox reports that as this load being aborted.
                if attempt == 2 or "NS_BINDING_ABORTED" not in str(exc):
                    raise

    async def snapshot(self) -> tuple[str, str]:
        try:
            result = await self._evaluate_snapshot()
        except BrowserError as exc:
            # A new tab's first load can leave it stuck in Firefox's parent process,
            # where scripts can't run. Waiting doesn't help, but loading again does.
            if "parent process" not in str(exc) or self._reloaded or not self._url:
                raise
            self._reloaded = True
            await self._navigate(self._url, self._timeout)
            result = await self._evaluate_snapshot()
        if result.get("type") != "success":
            raise BrowserError("could not read page")
        return _parse_snapshot(result["result"].get("value"))

    async def _evaluate_snapshot(self) -> dict:
        return await self._browser._rpc.call(
            "script.evaluate",
            {
                "expression": SNAPSHOT_JS,
                "target": {"context": self._context},
                "awaitPromise": False,
            },
        )

    async def close(self) -> None:
        self._browser._tabs.pop(self._context, None)
        rpc = self._browser._rpc
        try:
            await rpc.call("browsingContext.close", {"context": self._context})
            await rpc.call("browser.removeUserContext", {"userContext": self._user_context})
        except BrowserError:
            pass


class BidiBrowser(Browser):
    label = "Firefox"

    def __init__(self, process, profile):
        super().__init__(process, profile)
        self._tabs: dict[str, _BidiTab] = {}

    def _on_event(self, message: dict) -> None:
        if message["method"] == "network.responseCompleted":
            tab = self._tabs.get(message["params"].get("context"))
            if tab:
                tab.handle(message["params"])

    async def new_tab(self) -> Tab:
        created = await self._rpc.call("browser.createUserContext")
        user_context = created["userContext"]
        tab_created = await self._rpc.call(
            "browsingContext.create", {"type": "tab", "userContext": user_context}
        )
        tab = _BidiTab(self, user_context, tab_created["context"])
        self._tabs[tab_created["context"]] = tab
        return tab


def _kill_processes_using(profile: str) -> None:
    """Kill anything still naming this run's profile, however it detached."""
    needle = profile.encode()
    me = os.getpid()
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            with open(f"/proc/{entry.name}/cmdline", "rb") as f:
                if needle in f.read():
                    os.kill(int(entry.name), signal.SIGKILL)
        except OSError:
            pass


async def _stop_process(process) -> None:
    if process.returncode is not None:
        return
    # Browsers fork helpers, so signal the whole process group.
    try:
        os.killpg(process.pid, signal.SIGTERM)
        await asyncio.wait_for(process.wait(), 5)
    except asyncio.TimeoutError:
        os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
    except ProcessLookupError:
        pass


def _browser_account() -> pwd.struct_passwd | None:
    """The account to run browsers under, or None to stay as the current user."""
    if os.geteuid() != 0:
        return None
    try:
        return pwd.getpwnam(BROWSER_USER)
    except KeyError:
        raise BrowserError(f"won't run a browser as root and user {BROWSER_USER} is missing; run install.sh") from None


def _give_to_account(profile: str, account: pwd.struct_passwd) -> None:
    os.chown(profile, account.pw_uid, account.pw_gid)
    for folder, subfolders, files in os.walk(profile):
        for name in subfolders + files:
            os.chown(os.path.join(folder, name), account.pw_uid, account.pw_gid)


def _account_options(account: pwd.struct_passwd | None) -> dict:
    """Process options that switch to the account, leaving root's groups and environment behind."""
    if account is None:
        return {}
    env = {
        "HOME": account.pw_dir,
        "USER": account.pw_name,
        "LOGNAME": account.pw_name,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    return {"user": account.pw_uid, "group": account.pw_gid, "extra_groups": [], "cwd": account.pw_dir, "env": env}


def _child_setup() -> None:
    """Runs in the browser process before it starts, after any change of user."""
    # Keep the desktop responsive whatever the browser is doing.
    os.nice(10)
    # Die with handlecheck, even if it is killed in a way it can't clean up from.
    if _LIBC is not None:
        _LIBC.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)


async def _acquire_lock() -> int:
    """Wait until no other handlecheck has a browser open."""
    path = os.path.join(tempfile.gettempdir(), f"handlecheck.{os.getuid()}.lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = asyncio.get_running_loop().time() + LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if asyncio.get_running_loop().time() >= deadline:
                os.close(fd)
                raise BrowserError("another handlecheck kept its browser open too long") from None
            await asyncio.sleep(1)


def _remove_stale_profiles() -> None:
    """Clear profiles from a run that was killed. Holding the lock proves they're unused."""
    account = _browser_account()
    owners = {os.getuid(), account.pw_uid} if account else {os.getuid()}
    pattern = os.path.join(tempfile.gettempdir(), "handlecheck-*")
    for path in glob.glob(pattern):
        try:
            # Another user's browser has its own lock and may still be using it.
            if os.stat(path).st_uid in owners:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _release_lock(fd: int | None) -> None:
    if fd is not None:
        os.close(fd)


async def _spawn(argv: list[str], profile: str) -> tuple[asyncio.subprocess.Process, str]:
    """Start a browser and wait for it to announce its debugging websocket."""
    account = _browser_account()
    if account:
        _give_to_account(profile, account)
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
        preexec_fn=_child_setup,
        **_account_options(account),
    )

    async def read_url() -> str:
        while line := await process.stdout.readline():
            if (b"DevTools listening" in line or b"BiDi listening" in line) and (
                match := _WS_URL_RE.search(line)
            ):
                return match.group().decode()
        raise BrowserError(f"exited with code {await process.wait()} before starting")

    try:
        url = await asyncio.wait_for(read_url(), STARTUP_TIMEOUT)
    except asyncio.TimeoutError:
        await _stop_process(process)
        raise BrowserError("did not start in time") from None
    except (BrowserError, ValueError):
        await _stop_process(process)
        raise

    async def drain() -> None:
        # Keep the pipe empty or the browser blocks once it fills.
        while await process.stdout.read(65536):
            pass

    process._drain_task = asyncio.create_task(drain())
    return process, url


async def _connect(url: str):
    return await websockets.connect(url, max_size=None, proxy=None, open_timeout=STARTUP_TIMEOUT)


async def _launch_chromium(binary: str, label: str) -> Browser:
    profile = tempfile.TemporaryDirectory(prefix="handlecheck-", ignore_cleanup_errors=True)
    argv = [
        binary,
        "--headless=new",
        "--remote-debugging-port=0",
        f"--user-data-dir={profile.name}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-sync",
        "--disable-background-networking",
        # Tabs opened by the debugger count as background tabs otherwise,
        # and would have their scripts throttled.
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--blink-settings=imagesEnabled=false",
        "--disk-cache-size=1",
        "--renderer-process-limit=4",
        "--disable-gpu",
        "--mute-audio",
        "about:blank",
    ]
    try:
        process, url = await _spawn(argv, profile.name)
    except BaseException:
        profile.cleanup()
        raise
    browser = CdpBrowser(process, profile, label)
    try:
        browser._rpc = _Rpc(await _connect(url), browser._on_event)
    except BaseException:
        await browser.close()
        raise
    return browser


FIREFOX_PREFS = {
    "permissions.default.image": 2,
    "browser.display.use_document_fonts": 0,
    "privacy.trackingprotection.enabled": True,
    "dom.ipc.processCount": 2,
    "browser.cache.disk.enable": False,
    "browser.download.folderList": 2,
    "browser.download.useDownloadDir": True,
    "browser.download.always_ask_before_handling_new_types": False,
    "media.autoplay.default": 5,
    "app.update.auto": False,
    "browser.shell.checkDefaultBrowser": False,
    "browser.aboutwelcome.enabled": False,
    "datareporting.policy.dataSubmissionEnabled": False,
    "toolkit.telemetry.enabled": False,
}


async def _launch_firefox(binary: str) -> Browser:
    profile = tempfile.TemporaryDirectory(prefix="handlecheck-", ignore_cleanup_errors=True)
    # Firefox has no "deny", so anything a page saves stays in the throwaway profile.
    prefs = {**FIREFOX_PREFS, "browser.download.dir": profile.name}
    with open(os.path.join(profile.name, "user.js"), "w", encoding="utf-8") as f:
        for key, value in prefs.items():
            f.write(f"user_pref({json.dumps(key)}, {json.dumps(value)});\n")
    argv = [binary, "--headless", "--no-remote", "--profile", profile.name, "--remote-debugging-port", "0"]
    try:
        process, url = await _spawn(argv, profile.name)
    except BaseException:
        profile.cleanup()
        raise
    browser = BidiBrowser(process, profile)
    try:
        ws = await _connect(url.rstrip("/") + "/session")
        browser._rpc = _Rpc(ws, browser._on_event)
        await browser._rpc.call("session.new", {"capabilities": {}}, timeout=STARTUP_TIMEOUT)
        await browser._rpc.call("session.subscribe", {"events": ["network.responseCompleted"]})
    except BaseException:
        await browser.close()
        raise
    return browser


@dataclass(frozen=True)
class _Candidate:
    family: str
    label: str
    binary: str

    @property
    def engine(self) -> str:
        """Chromium and Chrome are the same engine, so sites treat them alike."""
        return "blink" if self.family in ("chromium", "chrome") else self.family


def find_browsers(choice: str = "auto") -> list[_Candidate]:
    """Installed browsers in preference order, one per family."""
    found = []
    for family, label, names in BROWSERS:
        if choice not in ("auto", family):
            continue
        binary = next(filter(None, map(shutil.which, names)), None)
        if binary:
            found.append(_Candidate(family, label, binary))
    return found


async def start_browser(candidate: _Candidate) -> Browser:
    """Start this browser, once no other handlecheck has one open."""
    fd = await _acquire_lock()
    try:
        _remove_stale_profiles()
        if candidate.family == "firefox":
            browser = await _launch_firefox(candidate.binary)
        else:
            browser = await _launch_chromium(candidate.binary, candidate.label)
    except BaseException:
        _release_lock(fd)
        raise
    browser._lock_fd = fd
    return browser


async def launch_browser(choice: str = "auto") -> Browser:
    """Start the first browser that works, falling through on failure."""
    candidates = find_browsers(choice)
    if not candidates:
        wanted = "Chromium, Chrome or Firefox" if choice == "auto" else choice
        raise BrowserError(f"{wanted} is not installed")
    failures = []
    for candidate in candidates:
        try:
            return await start_browser(candidate)
        except (BrowserError, OSError, websockets.WebSocketException) as exc:
            failures.append(f"{candidate.label}: {exc}")
    raise BrowserError("; ".join(failures))
