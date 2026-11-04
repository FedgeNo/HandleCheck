#!/usr/bin/env python3
"""Add calibrated services to a site list.

Usage: merge_results.py RESULTS_JSONL... [--output SITES_JSON]

Later results files replace earlier ones for the services they retested. The
output defaults to sites.json in your XDG data directory.

Passing services are checked automatically. A service that seems to have been
blocked from where calibration ran, but whose source list says what a real
profile contains, is used as is and marked unverified: a page can't pass for a
profile by accident, and "no account" from it stays a link. Other failures become
links to check by hand, except dead sites, and dating sites, which are only wanted
when a profile is openly public. Services added by hand stay as they are.
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from blocked_list import classify
from handlecheck import paths
from handlecheck.checker import DEAD_RE as DEAD

RULE_FIELDS = (
    "name", "category", "profile_url", "probe_url", "known_user", "username_pattern",
    "found_regex", "not_found_regex", "public_page_only", "redirect_means_missing",
)


def to_site(rules: dict, known_user: str | None = None, manual: bool = False) -> dict:
    site = {k: rules[k] for k in RULE_FIELDS if k in rules}
    if known_user:
        site["known_user"] = known_user
    if manual:
        site["manual"] = True
        for field in ("probe_url", "found_regex", "not_found_regex", "redirect_means_missing"):
            site.pop(field, None)
    return site


def trusted_rules(attempts: list[dict]) -> dict | None:
    """The best rule that says what a real profile contains, if there is one."""
    usable = [a["rules"] for a in attempts if a["rules"].get("found_regex") and not a["rules"].get("manual")]
    return max(usable, key=lambda rules: bool(rules.get("not_found_regex")), default=None)


def decide(rows: list[dict]) -> tuple[str, dict | None]:
    """What to do with one service: a description for the tally, and its entry."""
    attempts = [a for row in rows for a in row["attempts"]]
    passed = next((a for a in attempts if a["passed"]), None)
    first = attempts[0]["rules"]
    if passed:
        return "checked automatically", to_site(passed["rules"], passed["known_user"])
    if all(DEAD.search(a["why"]) or DEAD.search(json.dumps(a.get("fakes", ""))) for a in attempts):
        return ("dating, no public profiles" if first.get("public_page_only") else "dead"), None
    if classify(rows) and (rules := trusted_rules(attempts)):
        return "used from the list, untested", {**to_site(rules), "unverified": True}
    if first.get("public_page_only"):
        return "dating, no public profiles", None
    # A link is only useful if it can lead to this name's page.
    linkable = next((a["rules"] for a in attempts if "{username}" in a["rules"]["profile_url"]), None)
    if linkable is None:
        return "no way to link to a name", None
    return "link only", to_site(linkable, manual=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", nargs="+")
    parser.add_argument("--output", type=Path, default=paths.data_home() / paths.SITES_NAME)
    args = parser.parse_args()

    current = json.loads(paths.sites_file().read_text())
    original = [s for s in current if not s.get("calibrated")]
    names = {s["name"].lower() for s in original}
    added = []
    tally = Counter()

    # A service can have a row per browser, and any of them may have passed.
    by_key: dict[str, list[dict]] = {}
    for path in args.results:
        fresh: dict[str, list[dict]] = {}
        for line in Path(path).read_text().splitlines():
            row = json.loads(line)
            fresh.setdefault(row["key"], []).append(row)
        by_key.update(fresh)

    for key, rows in by_key.items():
        outcome, site = decide(rows)
        tally[outcome] += 1
        if site is None:
            continue
        site["calibrated"] = True
        if site["name"].lower() in names:
            site["name"] = f"{site['name']} ({key})"
        names.add(site["name"].lower())
        added.append(site)

    sites = sorted(original + added, key=lambda s: (s["category"], s["name"].lower()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("[\n" + ",\n".join("  " + json.dumps(s) for s in sites) + "\n]\n")
    print(f"{len(sites)} services in total, written to {args.output}:", dict(tally))


if __name__ == "__main__":
    main()
