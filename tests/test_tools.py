import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import blocked_list
import calibrate
import merge_results

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


def failed(why, rules, **outcomes):
    return {"rules": rules, "passed": False, "why": why, **outcomes}


BOTH = {"name": "X", "category": "misc", "profile_url": "https://x/{username}", "known_user": "k", "found_regex": "a", "not_found_regex": "b"}
STATUS_ONLY = {"name": "X", "category": "misc", "profile_url": "https://x/{username}", "known_user": "k"}
REFUSED = {"fakes": {"f": ["unknown", "blocked (HTTP 403)", "chromium"]}}


def test_blocked_service_with_found_text_is_used_but_unverified():
    outcome, site = merge_results.decide([row(failed("fake: blocked (HTTP 403)", BOTH, **REFUSED))])
    assert outcome == "used from the list, untested"
    assert site["unverified"] and site["found_regex"] == "a" and not site.get("manual")


def test_blocked_service_without_found_text_stays_a_link():
    outcome, site = merge_results.decide([row(failed("fake: blocked (HTTP 403)", STATUS_ONLY, **REFUSED))])
    assert outcome == "link only" and site["manual"] and not site.get("unverified")


def test_failure_that_is_not_a_block_is_not_trusted():
    outcome, site = merge_results.decide([row(failed("accepts made-up names", BOTH))])
    assert outcome == "link only" and site["manual"]


def test_dead_service_is_dropped_even_with_found_text():
    gone = {"fakes": {"f": ["unknown", "connection failed (ConnectError)", ""]}}
    assert merge_results.decide([row(failed("fake: connection failed (ConnectError)", BOTH, **gone))]) == ("dead", None)


def test_blocked_dating_site_needs_found_text():
    dating = {**STATUS_ONLY, "public_page_only": True}
    outcome, site = merge_results.decide([row(failed("fake: blocked (HTTP 403)", dating, **REFUSED))])
    assert outcome == "dating, no public profiles" and site is None


def test_link_needs_somewhere_to_put_the_name():
    nowhere = {**STATUS_ONLY, "profile_url": "https://x/search.php", "manual": True}
    assert merge_results.decide([row(failed("needs POST or headers", nowhere))]) == ("no way to link to a name", None)
