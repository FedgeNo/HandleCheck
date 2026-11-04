import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import blocked_list
import calibrate

from handlecheck.checker import Result, Site, Status


def outcome(status, reason=""):
    return [status, reason, "chromium"]


def row(*attempts):
    return {"key": "k", "family": "chromium", "attempts": list(attempts)}


def attempt(passed=False, fakes=None, likely=None):
    return {"rules": {}, "passed": passed, "why": "", "fakes": fakes or {}, "likely": likely or {}}


def test_a_pass_anywhere_is_not_blocked():
    blocked = attempt(fakes={"f": outcome("unknown", "blocked (HTTP 403)")})
    assert blocked_list.classify([row(blocked), row(attempt(passed=True))]) is None


def test_refusals_count_as_blocked():
    for reason in ("blocked (HTTP 403)", "bot challenge", "rate limited", "redirected to /login"):
        assert blocked_list.classify([row(attempt(fakes={"f": outcome("unknown", reason)}))]) == "blocked"


def test_strongest_evidence_wins_across_browsers():
    slow = attempt(likely={"a": outcome("unknown", "timed out")})
    refused = attempt(fakes={"f": outcome("unknown", "bot challenge")})
    assert blocked_list.classify([row(slow), row(refused)]) == "blocked"
    assert blocked_list.classify([row(slow)]) == "unreached"


def test_all_likely_names_missing_is_silent():
    assert blocked_list.classify([row(attempt(likely={"a": outcome("not_found"), "b": outcome("not_found")}))]) == "silent"


def test_other_failures_are_not_blamed_on_the_network():
    assert blocked_list.classify([row(attempt())]) is None  # e.g. accepts made-up names
    gone = attempt(fakes={"f": outcome("unknown", "page failed to load (net::ERR_NAME_NOT_RESOLVED)")})
    assert blocked_list.classify([row(gone)]) is None


SITE = Site(name="x", category="c", profile_url="https://x/{}", known_user="u")


def result(status):
    return Result(SITE, "n", status, "https://x/n", "")


def run_guarded(network, answers):
    """network: successive probe answers. answers: successive check results."""
    network, answers = iter(network), iter(answers)
    calls = []
    calibrate.NETWORK_CACHE_SECONDS = 0

    async def probe():
        return next(network)

    async def check(site, name):
        answer = next(answers)
        calls.append(answer)
        return result(answer)

    async def go():
        return await calibrate.NetworkGate(probe, retry_seconds=0).guard(check)(SITE, "n")

    return asyncio.run(go()), calls


def test_inconclusive_result_during_outage_is_retried():
    final, calls = run_guarded([True, False, False, True, True], [Status.UNKNOWN, Status.NOT_FOUND])
    assert final.status is Status.NOT_FOUND
    assert calls == [Status.UNKNOWN, Status.NOT_FOUND]


def test_checks_wait_for_the_network():
    final, calls = run_guarded([False, False, True], [Status.FOUND])
    assert final.status is Status.FOUND and calls == [Status.FOUND]


def test_real_block_with_network_up_is_kept():
    final, calls = run_guarded([True, True], [Status.UNKNOWN])
    assert final.status is Status.UNKNOWN and len(calls) == 1
