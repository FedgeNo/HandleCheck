#!/usr/bin/env python3
"""Test candidate services with likely usernames and two made-up ones.

Usage: calibrate.py CANDIDATES_JSON RESULTS_JSONL [--tabs N] [--workers N]

A rule set passes if it rejects both fakes and finds at least one likely name.
Results are appended to RESULTS_JSONL as they finish, and a restart resumes from
what is there. APIs run first, then each browser in turn on what is left.
"""

import argparse
import asyncio
import json
import random
import re
import secrets
import signal
import time
from pathlib import Path

from handlecheck.checker import DEAD_RE, Checker, Site, Status

GENERIC_NAMES = ["john", "alex", "mike"]
SITE_FIELDS = set(Site.__dataclass_fields__)
BROKEN_SESSION = "browser connection closed"


def make_site(entry: dict) -> Site:
    return Site(**{k: v for k, v in entry.items() if k in SITE_FIELDS})


def likely_names(entry: dict) -> list[str]:
    own = re.sub(r"[^a-z0-9]", "", entry["name"].lower())
    names = entry.get("likely", [])[:4] + ([own] if own else []) + GENERIC_NAMES
    return list(dict.fromkeys(names))


async def test_rules(check, entry: dict) -> dict:
    site = make_site(entry)
    outcome = {"rules": entry, "passed": False, "fakes": {}, "likely": {}, "why": ""}
    fakes = ["zq" + secrets.token_hex(6), "quietmarble" + secrets.token_hex(2).translate(str.maketrans("0123456789", "ghjkmnpqrs"))]

    for fake in fakes:
        # The likely names are the costly part, so rule out soft 404s first.
        result = await check(site, fake)
        outcome["fakes"][fake] = [result.status.value, result.reason, result.via]
        if result.status is Status.INVALID:
            outcome["why"] = "fake name not allowed by pattern"
            return outcome
        if result.status is not Status.NOT_FOUND:
            word = "accepts made-up names" if result.status is Status.FOUND else f"fake: {result.reason}"
            outcome["why"] = word
            return outcome

    found = []
    for name in likely_names(entry):
        result = await check(site, name)
        outcome["likely"][name] = [result.status.value, result.reason, result.via]
        if result.status is Status.FOUND:
            # The fakes have already been rejected, so one real hit settles it.
            found.append(name)
            break
    if found:
        outcome["passed"] = True
        outcome["known_user"] = found[0]
    else:
        outcome["why"] = "no likely name was found"
    return outcome


def is_api_only(rules: list[dict]) -> bool:
    return all(e.get("manual") or e.get("probe_url") for e in rules)


def api_rules(rules: list[dict]) -> list[dict]:
    return [e for e in rules if e.get("probe_url") or e.get("manual")]


def page_rules(rules: list[dict]) -> list[dict]:
    return [e for e in rules if not e.get("probe_url") and not e.get("manual")]


def worth_retesting(attempts: list[dict]) -> bool:
    """True if another browser might get an answer this one couldn't."""
    if any(a["passed"] for a in attempts):
        return False
    for attempt in attempts:
        if attempt["rules"].get("public_page_only") or attempt["rules"].get("manual"):
            continue
        if DEAD_RE.search(attempt["why"]):
            continue
        if attempt["why"].startswith("fake:"):
            return True
        if any(v[0] == "unknown" for v in attempt.get("likely", {}).values()):
            return True
    return False


async def attempt_rules(rules: list[dict], check) -> list[dict]:
    attempts = []
    for entry in rules:
        if entry.get("manual"):
            attempts.append({"rules": entry, "passed": False, "why": "needs POST or headers"})
            continue
        outcome = await test_rules(check, entry)
        attempts.append(outcome)
        if outcome["passed"]:
            break
    return attempts


async def run(candidates: dict, results_path: Path, tabs: int, workers: int) -> None:
    rows: dict[str, list[dict]] = {}
    if results_path.exists():
        for line in results_path.read_text().splitlines():
            row = json.loads(line)
            rows.setdefault(row["key"], []).append(row)

    def record(key: str, family: str, attempts: list[dict]) -> None:
        row = {"key": key, "family": family, "attempts": attempts}
        rows.setdefault(key, []).append(row)
        with results_path.open("a") as f:
            f.write(json.dumps(row) + "\n")

    usable = [make_site(entry) for rules in candidates.values() for entry in rules]
    started = time.monotonic()

    async with Checker(usable, timeout=15, tabs=tabs, concurrency=workers) as checker:
        # Phase 1: anything with an API, with no browser open.
        api_todo = [(k, v) for k, v in candidates.items() if api_rules(v) and k not in rows]
        print(f"API: {len(api_todo)} services", flush=True)
        limit = asyncio.Semaphore(workers)

        async def by_api(key: str, rules: list[dict]) -> None:
            async with limit:
                record(key, "api", await attempt_rules(api_rules(rules), checker.check_api))

        await asyncio.gather(*(by_api(k, v) for k, v in api_todo))
        print(f"API done after {(time.monotonic() - started) / 60:.1f} min", flush=True)

        # Phase 2: one browser after another, each only retesting what the last
        # couldn't settle, and every list kept so a restart resumes where it was.
        first_rules = {key: page_rules(rules) for key, rules in candidates.items() if page_rules(rules)}
        site_keys = {make_site(rules[0]).name: key for key, rules in first_rules.items()}
        order = list(first_rules)
        random.Random(1).shuffle(order)
        engines_run = set()
        for candidate in checker.families:
            if candidate.engine in engines_run:
                continue
            engines_run.add(candidate.engine)
            todo = []
            for key in order:
                earlier = [r for r in rows.get(key, []) if r["family"] != "api"]
                if any(any(a["passed"] for a in r["attempts"]) for r in rows.get(key, [])):
                    continue
                if any(r["family"] == candidate.family for r in earlier):
                    continue
                if not earlier or worth_retesting(earlier[-1]["attempts"]):
                    todo.append(key)
            print(f"{candidate.label}: {len(todo)} services", flush=True)
            sites = [make_site(first_rules[k][0]) for k in todo]

            async def work(site: Site, tab, label: str) -> bool:
                key = site_keys[site.name]
                if tab is None:
                    return False  # Left unrecorded, so the next start retries it.

                async def check(rule_site: Site, name: str):
                    return await checker.check_in_tab(rule_site, name, tab, label)

                attempts = await attempt_rules(first_rules[key], check)
                if BROKEN_SESSION not in json.dumps(attempts):
                    record(key, candidate.family, attempts)
                return False

            batch_start = time.monotonic()
            await checker.run_on_browser(candidate, sites, work)
            print(f"{candidate.label} done in {(time.monotonic() - batch_start) / 60:.1f} min", flush=True)
    print("finished", flush=True)


def _interrupt(_signum, _frame) -> None:
    raise KeyboardInterrupt


def main() -> None:
    # Browsers run in their own process group, so nothing else would stop them.
    signal.signal(signal.SIGTERM, _interrupt)
    parser = argparse.ArgumentParser()
    parser.add_argument("candidates")
    parser.add_argument("results")
    parser.add_argument("--tabs", type=int, default=5)
    parser.add_argument("--workers", type=int, default=48)
    args = parser.parse_args()
    try:
        asyncio.run(run(json.loads(Path(args.candidates).read_text()), Path(args.results), args.tabs, args.workers))
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
