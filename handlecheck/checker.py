"""Core logic: load site definitions, send probes, classify the responses.

A service with a public API (a probe_url) is asked directly. Everything else
is loaded in a headless browser.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import sys
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Iterable
from urllib.parse import quote, urlsplit

import httpx
from websockets.exceptions import WebSocketException

from . import __version__
from . import browser as browser_module
from . import paths
from .browser import Browser, BrowserError, NavigationTimeout, Tab

USER_AGENT = f"handlecheck/{__version__} (username availability checker)"
MAX_BODY_BYTES = 2_000_000
RETRY_STATUSES = {502, 503, 504}
# How long to let a browser page run its scripts before believing it.
SETTLE_MIN = 1.0
SETTLE_MAX = 5.0
REASON_CHALLENGE = "bot challenge"
REASON_UNRECOGNISED = "unrecognised page"
REASON_RESERVED = "name is also a page on this site"
REASON_NO_TAB = "browser connection closed"
# The site is gone or unreachable, which no other browser will change.
DEAD_RE = re.compile(
    r"connection failed|UNKNOWN_HOST|NAME_NOT_RESOLVED|CONNECTION_REFUSED|CONNECTION_TIMED_OUT"
    r"|ADDRESS_UNREACHABLE|CERT_|SSL_|NS_ERROR_CONNECTION|NS_ERROR_NET|HTTP (52\d|530)",
    re.IGNORECASE,
)
DEFAULT_TABS = 5
RESTART_AFTER_TABS = 30
# New tabs wait while less than this is free, then give up rather than risk the machine.
MIN_AVAILABLE_MB = 700
MEMORY_WAIT_SECONDS = 120
# A page that may still turn into something conclusive if given time.
STILL_LOADING = {REASON_CHALLENGE, REASON_UNRECOGNISED}

# Path-based sites serve their own pages at these names, which look just like
# profiles. APIs do not have the problem, so they are still asked.
RESERVED_NAMES = frozenset(
    """about account accounts admin api blog careers community contact developers
    docs download explore features feed help home jobs login logout messages new
    notifications press pricing privacy profile register search security settings
    signin signup status support terms trending user users watch""".split()
)

# Every username must pass this before it goes anywhere near a URL. Sites can
# be stricter through their own username_pattern.
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Sites using a {username}.example.com URL must set a pattern without dots, or
# a name could address a different host level than intended.
DEFAULT_SITE_PATTERN = r"^[A-Za-z0-9._-]+$"

# Bot-challenge pages are often served with a 200, so they need spotting by
# content. Deliberately narrow: Cloudflare also injects scripts into normal
# pages, and those must not count as a block.
LOGIN_PATH_RE = re.compile(r"log-?in|sign-?in|sign_in|auth|captcha|challenge|checkpoint|verify", re.IGNORECASE)
CHALLENGE_RE = re.compile(
    r"<title>\s*(just a moment|attention required)"
    r"|cf-browser-verification"
    r"|<title>\s*client challenge"
    r"|<title>[^<]*prove your humanity"
    r"|enable javascript and cookies to continue",
    re.IGNORECASE,
)


class Status(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"
    # The username breaks this service's naming rules, so no account can exist.
    INVALID = "invalid"
    # The service blocks automated lookups, so nothing was sent to it.
    MANUAL = "manual"


@dataclass(frozen=True)
class Site:
    name: str
    category: str
    profile_url: str
    known_user: str
    probe_url: str | None = None
    username_pattern: str = DEFAULT_SITE_PATTERN
    found_regex: str | None = None
    not_found_regex: str | None = None
    # The service legitimately redirects to a URL without the name in it.
    allow_redirect: bool = False
    # The service sends missing accounts elsewhere, as its source list documents.
    redirect_means_missing: bool = False
    manual: bool = False
    # Plain page load only: no API, no other browser, no adjustments. For sites
    # such as dating services, where only what's openly public should be read.
    public_page_only: bool = False
    # Added by tools/merge_results.py, which replaces these on each run.
    calibrated: bool = False

    @property
    def uses_browser(self) -> bool:
        return self.probe_url is None and not self.manual

    def accepts(self, username: str) -> bool:
        return re.fullmatch(self.username_pattern, username) is not None

    def url_for(self, username: str) -> str:
        return self.profile_url.replace("{username}", quote(username, safe=""))

    def probe_for(self, username: str) -> str:
        template = self.probe_url or self.profile_url
        return template.replace("{username}", quote(username, safe=""))


@dataclass(frozen=True)
class Result:
    site: Site
    username: str
    status: Status
    url: str
    reason: str = ""
    # The browser that produced this result, if one was used.
    via: str = ""


def validate_username(username: str) -> str | None:
    """Return an error message, or None if the username is safe to probe with."""
    if not USERNAME_RE.fullmatch(username):
        return (
            f"{username!r} is not a valid username: use 1-64 letters, digits, "
            "dots, underscores or hyphens, starting with a letter or digit"
        )
    return None


def load_sites(path: Path | None = None) -> list[Site]:
    with open(path or paths.sites_file(), encoding="utf-8") as f:
        return [Site(**entry) for entry in json.load(f)]


def _pattern(regex: str, username: str) -> re.Pattern[str]:
    return re.compile(regex.replace("{username}", re.escape(username)), re.IGNORECASE)


def _url_names_user(url: str, username: str) -> bool:
    """True if the name is a whole path segment or host label of the URL.

    Substring matching is not enough: the name "log" must not pass on a
    redirect to /login, and query strings prove nothing (/search?q=name).
    Forum software often adds a numeric ID, as in /members/john.777/.
    """
    parts = urlsplit(url)
    pieces = [s.lstrip("@~") for s in parts.path.split("/")] + (parts.hostname or "").split(".")
    named = re.compile(re.escape(username) + r"(?:[.-]\d+)?", re.IGNORECASE)
    return any(named.fullmatch(piece) for piece in pieces)


def _name_could_be_a_page(site: Site) -> bool:
    """True if the name fills a whole path segment or host label of the URL.

    Only then can a name like "admin" land on one of the site's own pages.
    /index/8-0-admin and /members/?username=admin can't.
    """
    placeholder = "handlecheckname"
    parts = urlsplit(site.profile_url.replace("{username}", placeholder))
    pieces = [s.lstrip("@~") for s in parts.path.split("/")] + (parts.hostname or "").split(".")
    return placeholder in pieces


def classify(
    site: Site, username: str, status_code: int, final_url: str, requested_url: str, body: str
) -> tuple[Status, str]:
    """Decide what a response means. Anything ambiguous is UNKNOWN, never FOUND."""
    if CHALLENGE_RE.search(body):
        return Status.UNKNOWN, REASON_CHALLENGE
    if status_code == 429:
        return Status.UNKNOWN, "rate limited"
    if status_code in (401, 403, 407, 451, 999):
        return Status.UNKNOWN, f"blocked (HTTP {status_code})"
    if status_code >= 500:
        return Status.UNKNOWN, f"server error (HTTP {status_code})"
    if status_code in (404, 410):
        return Status.NOT_FOUND, ""
    if site.not_found_regex and _pattern(site.not_found_regex, username).search(body):
        return Status.NOT_FOUND, ""
    if not 200 <= status_code < 300:
        return Status.UNKNOWN, f"unexpected HTTP {status_code}"

    # Being bounced to a login or home page says nothing about the account.
    if (
        final_url != requested_url
        and not site.allow_redirect
        and not _url_names_user(final_url, username)
    ):
        # A login or challenge page is never the documented "no such account".
        if site.redirect_means_missing and not LOGIN_PATH_RE.search(urlsplit(final_url).path):
            return Status.NOT_FOUND, ""
        return Status.UNKNOWN, f"redirected to {urlsplit(final_url).path or '/'}"

    if site.found_regex and not _pattern(site.found_regex, username).search(body):
        return Status.UNKNOWN, REASON_UNRECOGNISED
    return Status.FOUND, ""


async def _fetch(client: httpx.AsyncClient, url: str) -> tuple[int, str, str]:
    """GET a URL, reading at most MAX_BODY_BYTES of the body."""
    async with client.stream("GET", url) as response:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size >= MAX_BODY_BYTES:
                break
        body = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
        return response.status_code, str(response.url), body


async def _check_api(client: httpx.AsyncClient, site: Site, username: str, retries: int) -> Result:
    url = site.url_for(username)
    probe = site.probe_for(username)
    reason = ""
    for attempt in range(retries + 1):
        if attempt:
            await asyncio.sleep(1)
        try:
            status_code, final_url, body = await _fetch(client, probe)
        except httpx.TimeoutException:
            reason = "timed out"
            continue
        except httpx.TooManyRedirects:
            return Result(site, username, Status.UNKNOWN, url, "redirect loop")
        except httpx.HTTPError as exc:
            reason = f"connection failed ({type(exc).__name__})"
            continue
        status, reason = classify(site, username, status_code, final_url, probe, body)
        if status_code not in RETRY_STATUSES:
            return Result(site, username, status, url, reason)
    return Result(site, username, Status.UNKNOWN, url, reason)


async def _check_browser(tab: Tab, site: Site, username: str, timeout: float) -> Result:
    url = site.url_for(username)
    try:
        await tab.goto(url, timeout, unmodified=site.public_page_only)
    except NavigationTimeout:
        return Result(site, username, Status.UNKNOWN, url, "timed out")
    except BrowserError as exc:
        return Result(site, username, Status.UNKNOWN, url, f"page failed to load ({exc})")

    loop = asyncio.get_running_loop()
    loaded = loop.time()
    deadline = loaded + SETTLE_MAX
    while True:
        try:
            final_url, body = await tab.snapshot()
        except BrowserError:
            # The page is mid-navigation, which a client-side redirect does.
            if loop.time() >= deadline:
                return Result(site, username, Status.UNKNOWN, url, "could not read page")
        else:
            if tab.status is None:
                status, reason = Status.UNKNOWN, "no response"
            else:
                status, reason = classify(site, username, tab.status, final_url, url, body)
            # With nothing on the page to confirm it, a plain "found" only means
            # the page hasn't redirected yet, so give it a moment to do so.
            unconfirmed = (
                status is Status.FOUND
                and not site.found_regex
                and loop.time() < loaded + SETTLE_MIN
            )
            if not unconfirmed and (reason not in STILL_LOADING or loop.time() >= deadline):
                return Result(site, username, status, url, reason)
        await asyncio.sleep(0.25)


def worth_retrying(result: Result) -> bool:
    """True if another browser might get an answer this one couldn't."""
    return (
        result.status is Status.UNKNOWN
        and not result.site.public_page_only
        and not DEAD_RE.search(result.reason)
    )


def _precheck(site: Site, username: str) -> Result | None:
    """The answer, if it can be given without contacting the site."""
    url = site.url_for(username)
    if not site.accepts(username):
        return Result(site, username, Status.INVALID, url, "name not allowed on this service")
    if site.manual:
        return Result(site, username, Status.MANUAL, url, "cannot be checked reliably")
    if site.uses_browser and username.lower() in RESERVED_NAMES and _name_could_be_a_page(site):
        return Result(site, username, Status.UNKNOWN, url, REASON_RESERVED)
    return None


async def check_site(
    client: httpx.AsyncClient | None,
    browser: Browser | None,
    site: Site,
    username: str,
    timeout: float = 20.0,
    retries: int = 1,
    browser_problem: str = "no browser available",
) -> Result:
    """Check one site on its own. Checker is the way to check many."""
    if answer := _precheck(site, username):
        return answer
    if not site.uses_browser:
        return await _check_api(client, site, username, retries)
    url = site.url_for(username)
    if browser is None:
        return Result(site, username, Status.UNKNOWN, url, browser_problem)
    try:
        tab = await browser.new_tab()
    except BrowserError as exc:
        return Result(site, username, Status.UNKNOWN, url, f"page failed to load ({exc})")
    try:
        return await _check_browser(tab, site, username, timeout)
    finally:
        await tab.close()


def _memory_available_mb() -> int | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


async def _wait_for_memory() -> bool:
    """Hold a new tab back while the machine is short of memory."""
    for _ in range(MEMORY_WAIT_SECONDS // 2):
        available = _memory_available_mb()
        if available is None or available >= MIN_AVAILABLE_MB:
            return True
        await asyncio.sleep(2)
    return False


# Does the work for one site in its private tab, and says whether the site
# should be tried again in the next browser. The tab is None if none could open.
SiteWork = Callable[[Site, "Tab | None", str], Awaitable[bool]]


class Checker:
    """Runs checks with APIs in parallel and browsers strictly one at a time.

    A browser works through the sites in runs of RESTART_AFTER_TABS, with one
    private tab per site, and restarts between runs. Sites it can't answer are
    saved for the next browser, which only starts once this one has finished.
    """

    def __init__(
        self,
        sites: Iterable[Site],
        timeout: float = 20.0,
        concurrency: int = 20,
        tabs: int = DEFAULT_TABS,
        browser: str = "auto",
    ):
        self.sites = list(sites)
        self.timeout = timeout
        self.tabs = tabs
        self.families = browser_module.find_browsers(browser)
        self.browser_choice = browser
        self._failures: list[str] = []
        self._api_limit = asyncio.Semaphore(concurrency)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            limits=httpx.Limits(max_connections=concurrency),
        )

    async def __aenter__(self) -> "Checker":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self._client.aclose()

    @property
    def browser_problem(self) -> str:
        if self._failures:
            detail = "; ".join(self._failures)
        elif self.browser_choice == "auto":
            detail = "Chromium, Chrome or Firefox is not installed"
        else:
            detail = f"{self.browser_choice} is not installed"
        return f"no browser available ({detail})"

    async def check_api(self, site: Site, username: str) -> Result:
        if answer := _precheck(site, username):
            return answer
        async with self._api_limit:
            return await _check_api(self._client, site, username, retries=1)

    async def check_in_tab(self, site: Site, username: str, tab: Tab | None, label: str) -> Result:
        """Check a name using this site's tab, or its API if it has one."""
        if not site.uses_browser or site.manual:
            return await self.check_api(site, username)
        if answer := _precheck(site, username):
            return answer
        url = site.url_for(username)
        if tab is None:
            return Result(site, username, Status.UNKNOWN, url, REASON_NO_TAB)
        return replace(await _check_browser(tab, site, username, self.timeout), via=label)

    async def run_on_browser(
        self, candidate, sites: list[Site], work: SiteWork
    ) -> tuple[list[Site], list[Site]]:
        """Run sites on one browser. Returns those to retest and those not reached."""
        retests: list[Site] = []
        limit = asyncio.Semaphore(self.tabs)

        async def one(browser: Browser, site: Site) -> None:
            async with limit:
                tab = None
                if await _wait_for_memory():
                    try:
                        tab = await browser.new_tab()
                    except BrowserError:
                        pass
                try:
                    if await work(site, tab, browser.label):
                        retests.append(site)
                except Exception as exc:
                    # One odd page must never end the run for every other site.
                    print(f"handlecheck: {site.name}: {exc!r}", file=sys.stderr)
                    retests.append(site)
                finally:
                    if tab:
                        await tab.close()

        for start in range(0, len(sites), RESTART_AFTER_TABS):
            batch = sites[start : start + RESTART_AFTER_TABS]
            try:
                browser = await browser_module.start_browser(candidate)
            except (BrowserError, OSError, WebSocketException) as exc:
                self._failures.append(f"{candidate.label}: {exc}")
                return retests, sites[start:]
            try:
                await asyncio.gather(*(one(browser, site) for site in batch))
            finally:
                await browser.close()
        return retests, []

    async def run_in_browsers(self, sites: list[Site], work: SiteWork) -> list[Site]:
        """Try each browser in turn. Returns the sites none of them settled.

        A retest only helps in a different engine, so once Chromium has run,
        Chrome is passed over. It still stands in if Chromium won't start.
        """
        pending = list(sites)
        engines_run = set()
        for candidate in self.families:
            if not pending:
                break
            if candidate.engine in engines_run:
                continue
            retests, unreached = await self.run_on_browser(candidate, pending, work)
            if len(unreached) < len(pending):
                engines_run.add(candidate.engine)
            pending = unreached + retests
        return pending

    async def check_all(
        self, username: str, on_result: Callable[[Result], None] | None = None
    ) -> list[Result]:
        results: dict[str, Result] = {}
        unsettled: dict[str, Result] = {}

        def settle(result: Result) -> None:
            results[result.site.name] = result
            if on_result:
                on_result(result)

        async def api(site: Site) -> None:
            settle(await self.check_api(site, username))

        in_browser = []
        api_sites = []
        for site in self.sites:
            if answer := _precheck(site, username):
                settle(answer)
            elif site.uses_browser:
                in_browser.append(site)
            else:
                api_sites.append(site)
        await asyncio.gather(*(api(site) for site in api_sites))

        async def work(site: Site, tab: Tab | None, label: str) -> bool:
            result = await self.check_in_tab(site, username, tab, label)
            if worth_retrying(result):
                unsettled[site.name] = result
                return True
            settle(result)
            return False

        for site in await self.run_in_browsers(in_browser, work):
            settle(
                unsettled.get(site.name)
                or Result(site, username, Status.UNKNOWN, site.url_for(username), self.browser_problem)
            )
        return [results[site.name] for site in self.sites]

    async def verify(self) -> list["Verification"]:
        """Probe each site with a real account and a made-up one.

        A site only works if it reports the first as found and the second as
        not found. Sites change, so this is how to tell when one has rotted.
        """
        random_name = "zq" + secrets.token_hex(6)
        results: dict[str, Verification] = {}
        sites = [s for s in self.sites if not s.manual]

        async def both(site: Site, tab: Tab | None, label: str) -> Verification:
            known = await self.check_in_tab(site, site.known_user, tab, label)
            random = await self.check_in_tab(site, random_name, tab, label)
            return Verification(site, known, random)

        async def api(site: Site) -> None:
            results[site.name] = await both(site, None, "")

        await asyncio.gather(*(api(s) for s in sites if not s.uses_browser))

        async def work(site: Site, tab: Tab | None, label: str) -> bool:
            outcome = await both(site, tab, label)
            results[site.name] = outcome
            return worth_retrying(outcome.known) or worth_retrying(outcome.random)

        in_browser = [s for s in sites if s.uses_browser]
        leftovers = await self.run_in_browsers(in_browser, work)
        for site in leftovers:
            if site.name not in results:
                unknown = Result(site, "", Status.UNKNOWN, site.profile_url, self.browser_problem)
                results[site.name] = Verification(site, unknown, unknown)
        return [results[site.name] for site in sites]


@dataclass(frozen=True)
class Verification:
    site: Site
    known: Result
    random: Result

    @property
    def ok(self) -> bool:
        return self.known.status is Status.FOUND and self.random.status is Status.NOT_FOUND
