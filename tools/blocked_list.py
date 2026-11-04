#!/usr/bin/env python3
"""List services that probably failed because of where the test ran from.

Usage: blocked_list.py CANDIDATES_JSON RESULTS_JSONL OUTPUT_DIR

Safe to run while calibrate.py is still going. Writes, for each kind of evidence,
a candidates file that calibrate.py can test again from another connection:

  blocked    the service refused us: a 403, a bot challenge, a login redirect
  unreached  no clear answer: timeouts and resets, which a firewall can also cause
  silent     every likely name came back "not found", which may be real, or may
             be a service showing a datacenter an empty page
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

KINDS = ("blocked", "unreached", "silent")
BLOCKED_RE = re.compile(
    r"bot challenge|rate limited|blocked \(HTTP|redirected to .*(login|signin|sign-in|sign_in|auth|captcha"
    r"|challenge|checkpoint|blocked|denied|verify|consent|403|forbidden)",
    re.IGNORECASE,
)
UNREACHED_RE = re.compile(
    r"timed out|no response|CONNECTION_TIMED_OUT|CONNECTION_RESET|CONNECTION_CLOSED|TIMED_OUT"
    r"|NS_ERROR_NET|ConnectTimeout|ReadTimeout|ReadError|RemoteProtocolError|HTTP 52\d",
    re.IGNORECASE,
)


def reasons(attempt: dict) -> list[str]:
    outcomes = [*attempt.get("fakes", {}).values(), *attempt.get("likely", {}).values()]
    return [outcome[1] for outcome in outcomes if outcome[0] == "unknown"]


def classify(rows: list[dict]) -> str | None:
    """The strongest evidence of a block across every attempt, or None."""
    attempts = [attempt for row in rows for attempt in row["attempts"]]
    if any(attempt["passed"] for attempt in attempts):
        return None
    seen = [reason for attempt in attempts for reason in reasons(attempt)]
    if any(BLOCKED_RE.search(reason) for reason in seen):
        return "blocked"
    if any(UNREACHED_RE.search(reason) for reason in seen):
        return "unreached"
    if any(
        attempt.get("likely") and all(value[0] == "not_found" for value in attempt["likely"].values())
        for attempt in attempts
    ):
        return "silent"
    return None


def main() -> None:
    candidates = json.loads(Path(sys.argv[1]).read_text())
    output = Path(sys.argv[3])
    output.mkdir(parents=True, exist_ok=True)
    rows: dict[str, list[dict]] = {}
    for line in Path(sys.argv[2]).read_text().splitlines():
        row = json.loads(line)
        rows.setdefault(row["key"], []).append(row)

    groups: dict[str, dict] = {kind: {} for kind in KINDS}
    outcomes = Counter()
    for key, service in rows.items():
        kind = classify(service)
        if kind:
            groups[kind][key] = candidates[key]
        elif any(attempt["passed"] for row in service for attempt in row["attempts"]):
            kind = "passed"
        else:
            kind = "other failure"
        outcomes[kind] += 1
    for kind, group in groups.items():
        (output / f"{kind}.json").write_text(json.dumps(group, indent=1) + "\n")
    print(f"{len(rows)} of {len(candidates)} services tested:", dict(outcomes))


if __name__ == "__main__":
    main()
