#!/usr/bin/env python3
"""Add calibrated services to sites.json in your XDG data directory.

Usage: merge_results.py RESULTS_JSONL

Passing services are checked automatically. Failures become links to check by
hand, except dead sites, and dating sites, which are only wanted when a profile
is openly public. Services added by hand stay as they are.
"""

import json
import sys
from collections import Counter
from pathlib import Path

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


def main() -> None:
    current = json.loads(paths.sites_file().read_text())
    original = [s for s in current if not s.get("calibrated")]
    names = {s["name"].lower() for s in original}
    added = []
    tally = Counter()

    # A service can have a row per browser, and any of them may have passed.
    by_key: dict[str, list[dict]] = {}
    for line in Path(sys.argv[1]).read_text().splitlines():
        row = json.loads(line)
        by_key.setdefault(row["key"], []).append(row)

    for key, rows in by_key.items():
        attempts = [a for row in rows for a in row["attempts"]]
        passed = next((a for a in attempts if a["passed"]), None)
        first = attempts[0]["rules"]
        if passed:
            site = to_site(passed["rules"], passed["known_user"])
            tally["checked automatically"] += 1
        elif first.get("public_page_only"):
            tally["dating, no public profiles"] += 1
            continue
        elif all(DEAD.search(a["why"]) or DEAD.search(json.dumps(a.get("fakes", ""))) for a in attempts):
            tally["dead"] += 1
            continue
        else:
            site = to_site(first, manual=True)
            tally["link only"] += 1
        site["calibrated"] = True
        if site["name"].lower() in names:
            site["name"] = f"{site['name']} ({key})"
        names.add(site["name"].lower())
        added.append(site)

    sites = sorted(original + added, key=lambda s: (s["category"], s["name"].lower()))
    target = paths.data_home() / paths.SITES_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("[\n" + ",\n".join("  " + json.dumps(s) for s in sites) + "\n]\n")
    print(f"{len(sites)} services in total, written to {target}:", dict(tally))


if __name__ == "__main__":
    main()
