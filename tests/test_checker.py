import asyncio
import re
import sys
from pathlib import Path

import httpx
import pytest

from handlecheck import cli, paths
from handlecheck import checker as checker_module
from handlecheck.checker import (
    Result,
    Site,
    Status,
    check_site,
    classify,
    load_sites,
    validate_username,
)

URL = "https://example.com/alice"


def site(**kwargs):
    defaults = dict(
        name="Example",
        category="dev",
        profile_url="https://example.com/{username}",
        probe_url="https://example.com/{username}",
        known_user="x",
    )
    return Site(**{**defaults, **kwargs})


def status_of(code=200, body="", final=URL, username="alice", **kwargs):
    return classify(site(**kwargs), username, code, final, URL, body)[0]


@pytest.mark.parametrize("code", [401, 403, 429, 500, 503, 999])
def test_blocks_and_errors_are_unknown(code):
    assert status_of(code) is Status.UNKNOWN


@pytest.mark.parametrize("code", [404, 410])
def test_missing_statuses(code):
    assert status_of(code) is Status.NOT_FOUND


def test_plain_200_is_found():
    assert status_of(200) is Status.FOUND


@pytest.mark.parametrize(
    "body",
    [
        "<title>Just a moment...</title>",
        "<html><title>Client Challenge</title></html>",
        "Enable JavaScript and cookies to continue",
        "<title>Reddit - Prove your humanity</title>",
    ],
)
def test_challenge_page_served_with_200_is_not_found_as_account(body):
    assert status_of(200, body) is Status.UNKNOWN


def test_not_found_regex_beats_200():
    assert status_of(200, "null", not_found_regex=r"^\s*null\s*$") is Status.NOT_FOUND


def test_found_regex_missing_is_unknown_not_found():
    assert status_of(200, "something else", found_regex="profile") is Status.UNKNOWN
    assert status_of(200, "a profile", found_regex="profile") is Status.FOUND


def test_regex_username_placeholder_is_escaped():
    kwargs = dict(found_regex=r"\({username}\) - Profile")
    assert classify(site(**kwargs), "a.b", 200, URL, URL, "(a.b) - Profile")[0] is Status.FOUND
    assert classify(site(**kwargs), "a.b", 200, URL, URL, "(aXb) - Profile")[0] is Status.UNKNOWN


@pytest.mark.parametrize(
    "final,username,expected",
    [
        ("https://example.com/login", "alice", Status.UNKNOWN),
        # A name that is merely a substring of the target must not pass.
        ("https://example.com/login", "log", Status.UNKNOWN),
        ("https://example.com/search?q=alice", "alice", Status.UNKNOWN),
        ("https://example.com/@Alice", "alice", Status.FOUND),
        ("https://example.com/cw/alice", "alice", Status.FOUND),
        ("https://alice.example.com/", "alice", Status.FOUND),
    ],
)
def test_redirect_guard(final, username, expected):
    assert status_of(200, final=final, username=username) is expected


def test_allow_redirect_opt_out():
    assert status_of(200, final="https://example.com/users/abc123", allow_redirect=True) is Status.FOUND


@pytest.mark.parametrize("name", ["alice", "a", "john.doe", "a_b-c", "A1"])
def test_valid_usernames(name):
    assert validate_username(name) is None


@pytest.mark.parametrize(
    "name", ["", "..", "../etc", "a/b", "a b", "a?x=1", "a#b", "evil.com/", "-rf", "a" * 65, "a\n"]
)
def test_unsafe_usernames_rejected(name):
    assert validate_username(name) is not None


def run_check(handler, target, username="alice"):
    async def go():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            return await check_site(client, None, target, username)

    return asyncio.run(go())


def test_retries_a_failing_gateway_once(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(503 if len(calls) == 1 else 200)

    assert run_check(handler, site()).status is Status.FOUND
    assert len(calls) == 2


def test_timeouts_end_as_unknown():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    result = run_check(handler, site())
    assert result.status is Status.UNKNOWN
    assert result.reason == "timed out"


def test_probe_url_is_requested_but_profile_url_is_reported():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200)

    target = site(probe_url="https://api.example.com/u/{username}")
    result = run_check(handler, target)
    assert seen == ["https://api.example.com/u/alice"]
    assert result.url == "https://example.com/alice"


def test_manual_and_invalid_sites_send_nothing():
    def handler(request):
        raise AssertionError("no request should be sent")

    assert run_check(handler, site(manual=True)).status is Status.MANUAL
    assert run_check(handler, site(username_pattern="^[0-9]+$")).status is Status.INVALID


def test_bundled_sites_are_well_formed():
    sites = load_sites()
    assert len({s.name for s in sites}) == len(sites)
    for s in sites:
        assert s.profile_url.startswith("https://"), s.name
        assert "{username}" in s.profile_url, s.name
        # Links never use their known user, and the lists don't always give one that fits.
        assert s.manual or s.accepts(s.known_user), s.name
        re.compile(s.username_pattern)
        for regex in (s.found_regex, s.not_found_regex):
            if regex:
                re.compile(regex.replace("{username}", "x"))
        # A dot in a subdomain name would address another host level.
        if s.profile_url.startswith("https://{username}"):
            assert not s.accepts("a.b"), s.name


def test_unknown_site_filter_is_an_error(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["alice", "--sites", "nosuchsite"])
    assert exc.value.code == 2


def test_invalid_username_is_an_error_before_any_request(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["../etc"])
    assert exc.value.code == 2


def test_browser_site_without_a_browser_is_unknown_and_sends_nothing():
    def handler(request):
        raise AssertionError("must not fall back to plain HTTP")

    result = run_check(handler, site(probe_url=None))
    assert result.status is Status.UNKNOWN
    assert "no browser" in result.reason


@pytest.mark.parametrize("name", ["login", "Settings"])
def test_reserved_names_are_not_loaded_as_profiles(name):
    def handler(request):
        raise AssertionError("no request should be sent")

    result = run_check(handler, site(probe_url=None), username=name)
    assert result.status is Status.UNKNOWN


def test_reserved_names_are_still_asked_of_apis():
    assert run_check(lambda r: httpx.Response(200), site(), username="login").status is Status.FOUND


def test_report_links_everything_without_an_answer():
    import io

    results = [
        Result(site(name="Yes"), "alice", Status.FOUND, "https://yes.example/alice"),
        Result(site(name="No"), "alice", Status.NOT_FOUND, "https://no.example/alice"),
        Result(site(name="Blocked"), "alice", Status.UNKNOWN, "https://blocked.example/alice", "rate limited"),
        Result(site(name="Hand", manual=True), "alice", Status.MANUAL, "https://hand.example/alice", "cannot be checked reliably"),
    ]
    out = io.StringIO()
    cli.print_report("alice", results, False, out)
    report = out.getvalue()
    section = report.split("Check these yourself (2):")[1]
    assert "https://blocked.example/alice  (rate limited)" in section
    assert "https://hand.example/alice" in section
    assert "https://no.example/alice" not in report
    assert "1 found, 1 not found, 2 to check yourself" in report


FAST_SITES = [
    Site("HasApi", "dev", "https://a.example/{username}", "x", probe_url="https://a.example/api/{username}"),
    Site("PageOnly", "dev", "https://b.example/{username}", "x"),
    Site("ByHand", "dev", "https://c.example/{username}", "x", manual=True),
]


def test_fast_lists_only_services_with_an_api(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_sites", lambda: FAST_SITES)
    assert cli.main(["--list", "--fast"]) == 0
    listed = capsys.readouterr().out
    assert "HasApi" in listed
    assert "PageOnly" not in listed and "ByHand" not in listed


def test_fast_opens_no_browser_and_reports_what_it_skipped(monkeypatch, capsys):
    async def api_answer(self, site, username):
        return checker_module.Result(site, username, Status.NOT_FOUND, site.url_for(username))

    async def no_browser(candidate):
        raise AssertionError("--fast started a browser")

    monkeypatch.setattr(cli, "load_sites", lambda: FAST_SITES)
    monkeypatch.setattr(checker_module.Checker, "check_api", api_answer)
    monkeypatch.setattr(checker_module.browser_module, "start_browser", no_browser)
    assert cli.main(["alice", "--fast", "--all"]) == 0
    out = capsys.readouterr().out
    assert "HasApi" in out
    assert "2 services skipped by --fast" in out


def test_fast_with_nothing_left_to_check_is_an_error(monkeypatch):
    monkeypatch.setattr(cli, "load_sites", lambda: FAST_SITES)
    with pytest.raises(SystemExit):
        cli.main(["alice", "--fast", "-s", "PageOnly"])


def test_sites_come_from_xdg_data_home_first(monkeypatch, tmp_path):
    home, shared = tmp_path / "home", tmp_path / "shared"
    (shared / "handlecheck").mkdir(parents=True)
    (shared / "handlecheck" / "sites.json").write_text("[]")
    monkeypatch.setenv("XDG_DATA_HOME", str(home))
    monkeypatch.setenv("XDG_DATA_DIRS", str(shared))
    assert paths.sites_file() == shared / "handlecheck" / "sites.json"
    (home / "handlecheck").mkdir(parents=True)
    (home / "handlecheck" / "sites.json").write_text("[]")
    assert paths.sites_file() == home / "handlecheck" / "sites.json"


def test_sites_fall_back_to_the_bundled_list(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path))
    assert paths.sites_file() == paths.BUNDLED_SITES


def test_relative_xdg_paths_are_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", "relative")
    monkeypatch.setenv("XDG_DATA_DIRS", "relative:/abs")
    assert paths.data_home() == tmp_path / ".local" / "share" / "handlecheck"
    assert paths.data_dirs() == [Path("/abs/handlecheck")]


def test_forum_redirect_to_name_with_id_counts_as_found():
    assert status_of(200, final="https://example.com/members/alice.777/") is Status.FOUND
    assert status_of(200, final="https://example.com/members/alice-777/") is Status.FOUND


def test_name_with_other_text_still_does_not_count():
    assert status_of(200, final="https://example.com/members/alice.html") is Status.UNKNOWN
    assert status_of(200, final="https://example.com/alice.777.x/") is Status.UNKNOWN


def test_documented_redirect_means_missing():
    assert status_of(200, final="https://example.com/", redirect_means_missing=True) is Status.NOT_FOUND
    assert status_of(200, final="https://example.com/") is Status.UNKNOWN


def test_login_redirect_is_never_taken_as_missing():
    for path in ("/login", "/users/sign_in", "/captcha", "/account/signin"):
        final = f"https://example.com{path}"
        assert status_of(200, final=final, redirect_means_missing=True) is Status.UNKNOWN


@pytest.mark.parametrize(
    "url,could",
    [
        ("https://x.example/{username}", True),
        ("https://x.example/@{username}", True),
        ("https://{username}.example.com/", True),
        ("https://x.example/index/8-0-{username}", False),
        ("https://x.example/members/?username={username}", False),
    ],
)
def test_reserved_names_only_apply_where_they_could_be_a_page(url, could):
    rule = Site(name="X", category="misc", profile_url=url, known_user="k", username_pattern=r"^[a-z]+$")
    result = checker_module._precheck(rule, "admin")
    assert (result is not None and result.reason == checker_module.REASON_RESERVED) is could


def test_untested_rule_can_report_found_but_not_missing():
    assert status_of(200, body="profile", found_regex="profile", unverified=True) is Status.FOUND
    assert status_of(404, unverified=True) is Status.UNKNOWN
    assert status_of(200, body="gone", not_found_regex="gone", unverified=True) is Status.UNKNOWN
    assert status_of(200, body="nothing here", found_regex="profile", unverified=True) is Status.UNKNOWN


def test_untested_rule_is_not_retried_after_a_clear_page():
    rule = site(unverified=True)
    answer = checker_module.Result(rule, "a", Status.UNKNOWN, URL, checker_module.REASON_UNVERIFIED)
    assert not checker_module.worth_retrying(answer)


def test_report_marks_untested_rules(capsys):
    rule = site(unverified=True)
    hit = checker_module.Result(rule, "a", Status.FOUND, URL)
    cli.print_report("a", [hit], False, sys.stdout)
    assert "(untested rule)" in capsys.readouterr().out
    assert cli._as_dict(hit)["verified"] is False
