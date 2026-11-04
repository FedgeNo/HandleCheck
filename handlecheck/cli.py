"""Command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys

from . import __version__, update
from .checker import (
    DEFAULT_TABS,
    Checker,
    Result,
    Site,
    Status,
    load_sites,
    validate_username,
)


def _colour(enabled: bool):
    def paint(code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if enabled else text

    return paint


def _use_colour(stream) -> bool:
    return stream.isatty() and "NO_COLOR" not in os.environ


def print_report(username: str, results: list[Result], show_all: bool, stream, skipped: int = 0) -> None:
    paint = _colour(_use_colour(stream))
    groups = {status: [] for status in Status}
    for result in sorted(results, key=lambda r: r.site.name.lower()):
        groups[result.status].append(result)

    found, missing = groups[Status.FOUND], groups[Status.NOT_FOUND]
    invalid = groups[Status.INVALID]
    by_hand = sorted(groups[Status.UNKNOWN] + groups[Status.MANUAL], key=lambda r: r.site.name.lower())
    width = max((len(r.site.name) for r in results), default=0)

    print(paint("1", f"\n{username}"), file=stream)
    if found:
        print(paint("32;1", f"\n  Found on {len(found)}:"), file=stream)
        for r in found:
            note = paint("2", "  (untested rule)") if r.site.unverified else ""
            print(f"    {r.site.name:<{width}}  {r.url}{note}", file=stream)
    else:
        print("\n  Not found on any service that could be checked.", file=stream)

    if by_hand:
        print(paint("33;1", f"\n  Check these yourself ({len(by_hand)}):"), file=stream)
        for r in by_hand:
            note = paint("2", f"  ({r.reason})") if r.status is Status.UNKNOWN else ""
            print(f"    {r.site.name:<{width}}  {r.url}{note}", file=stream)

    if show_all:
        if missing:
            print(paint("2", f"\n  Not found on {len(missing)}:"), file=stream)
            for r in missing:
                print(f"    {r.site.name}", file=stream)
        if invalid:
            print(paint("2", f"\n  Name not allowed on {len(invalid)}:"), file=stream)
            for r in invalid:
                print(f"    {r.site.name}", file=stream)

    print(
        f"\n  {len(found)} found, {len(missing)} not found, "
        f"{len(by_hand)} to check yourself, {len(invalid)} name not allowed",
        file=stream,
    )
    if skipped:
        print(paint("2", f"  {skipped} services skipped by --fast because they have no API"), file=stream)
    if found:
        print(
            paint("2", "  A match means the name is taken, not that it is the same person."),
            file=stream,
        )


def _as_dict(result: Result) -> dict:
    return {
        "site": result.site.name,
        "category": result.site.category,
        "status": result.status.value,
        "method": "api" if result.site.probe_url else "browser",
        "browser": result.via or None,
        "url": result.url,
        "reason": result.reason,
        "verified": not result.site.unverified,
    }


def _select_sites(sites: list[Site], names: str | None, parser) -> list[Site]:
    if not names:
        return sites
    wanted = {name.strip().lower() for name in names.split(",") if name.strip()}
    available = {site.name.lower() for site in sites} | {site.category.lower() for site in sites}
    unknown = sorted(wanted - available)
    if unknown:
        parser.error(f"unknown service or category: {', '.join(unknown)}")
    return [s for s in sites if s.name.lower() in wanted or s.category.lower() in wanted]


def _has_api(site: Site) -> bool:
    return not site.uses_browser and not site.manual


def _make_checker(sites: list[Site], args) -> Checker:
    return Checker(sites, args.timeout, args.concurrency, args.tabs, args.browser)


def _announce_browser(checker: Checker) -> None:
    if not any(site.uses_browser for site in checker.sites):
        return
    if checker.families:
        first = checker.families[0]
        retest = next((c for c in checker.families if c.engine != first.engine), None)
        plan = f"{first.label}, with retests in {retest.label}" if retest else first.label
        print(f"Using {plan} (headless, one at a time)", file=sys.stderr)
    else:
        print(f"warning: {checker.browser_problem}; page checks will be skipped", file=sys.stderr)


def run_verify(sites: list[Site], args) -> int:
    async def go():
        async with _make_checker(sites, args) as checker:
            _announce_browser(checker)
            return await checker.verify()

    try:
        results = asyncio.run(go())
    except KeyboardInterrupt:
        return 130
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "site": v.site.name,
                        "ok": v.ok,
                        "browser": v.known.via or None,
                        "known_user": v.known.status.value,
                        "known_reason": v.known.reason,
                        "random_user": v.random.status.value,
                        "random_reason": v.random.reason,
                    }
                    for v in results
                ],
                indent=2,
            )
        )
    else:
        paint = _colour(_use_colour(sys.stdout))
        width = max((len(v.site.name) for v in results), default=0)
        for v in sorted(results, key=lambda v: (v.ok, v.site.name.lower())):
            mark = paint("32", "ok  ") if v.ok else paint("31", "FAIL")
            detail = f"  via {v.known.via}" if v.known.via else ""
            if not v.ok:
                detail = (
                    f"  real account={v.known.status.value} {v.known.reason}".rstrip()
                    + f"; random name={v.random.status.value} {v.random.reason}".rstrip()
                )
            print(f"{mark} {v.site.name:<{width}}{detail}")
        passed = sum(v.ok for v in results)
        print(f"\n{passed}/{len(results)} services working")
    return 0 if all(v.ok for v in results) else 1


def _interrupt(_signum, _frame) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    # Browsers run in their own process group, so nothing else stops them. Treat
    # a kill like Ctrl-C so the normal cleanup closes the browser and its profile.
    signal.signal(signal.SIGTERM, _interrupt)
    parser = argparse.ArgumentParser(
        prog="handlecheck", description="Check which online services a username exists on."
    )
    parser.add_argument("usernames", nargs="*", metavar="username", help="username(s) to look up")
    parser.add_argument("-a", "--all", action="store_true", help="also list services without a match")
    parser.add_argument("-s", "--sites", help="comma-separated services or categories to check")
    parser.add_argument("--list", action="store_true", help="list supported services and exit")
    parser.add_argument("--json", action="store_true", help="print results as JSON")
    parser.add_argument(
        "--browser",
        choices=["auto", "chromium", "chrome", "firefox"],
        default="auto",
        help="browser for sites without an API (default: auto, tries them in the order listed)",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="seconds per page or request (default: 15)")
    parser.add_argument("--concurrency", type=int, default=20, help="parallel API requests (default: 20)")
    parser.add_argument("--tabs", type=int, default=DEFAULT_TABS, help=f"parallel browser tabs (default: {DEFAULT_TABS})")
    parser.add_argument(
        "--fast", action="store_true", help="only check services with an API, without opening a browser"
    )
    parser.add_argument(
        "--verify", action="store_true", help="test that each service's detection still works"
    )
    parser.add_argument("--update", action="store_true", help="update to the latest version and exit")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    # Before anything else, so that a broken site list can still be updated away.
    if args.update:
        if args.usernames:
            parser.error("--update doesn't take usernames")
        try:
            return update.update()
        except KeyboardInterrupt:
            return 130

    if args.timeout <= 0:
        parser.error("--timeout must be greater than 0")
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    if args.tabs < 1:
        parser.error("--tabs must be at least 1")

    sites = _select_sites(load_sites(), args.sites, parser)
    skipped = 0
    if args.fast:
        selected = len(sites)
        sites = [site for site in sites if _has_api(site)]
        skipped = selected - len(sites)
        if not sites:
            parser.error("none of the selected services have an API; drop --fast to check them")

    if args.list:
        for site in sorted(sites, key=lambda s: (s.category, s.name.lower())):
            note = "  (check by hand)" if site.manual else ""
            print(f"{site.category:<10} {site.name}{note}")
        return 0
    if args.verify:
        return run_verify(sites, args)

    usernames = args.usernames
    if not usernames:
        try:
            entered = input("Username: ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return 130
        usernames = [entered]

    for username in usernames:
        error = validate_username(username)
        if error:
            parser.error(error)

    async def check_everyone() -> dict[str, list[Result]]:
        everything = {}
        async with _make_checker(sites, args) as checker:
            _announce_browser(checker)
            for username in usernames:
                done = 0

                def progress(_result: Result) -> None:
                    nonlocal done
                    done += 1
                    if sys.stderr.isatty():
                        print(f"\rChecking {username}: {done}/{len(sites)}", end="", file=sys.stderr, flush=True)

                results = await checker.check_all(username, progress)
                if sys.stderr.isatty():
                    print("\r\033[K", end="", file=sys.stderr)
                everything[username] = results
                if not args.json:
                    print_report(username, results, args.all, sys.stdout, skipped)
        return everything

    try:
        everything = asyncio.run(check_everyone())
    except KeyboardInterrupt:
        return 130
    if args.json:
        print(json.dumps({u: [_as_dict(r) for r in rs] for u, rs in everything.items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
