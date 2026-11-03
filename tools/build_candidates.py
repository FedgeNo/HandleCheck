#!/usr/bin/env python3
"""Merge site lists into candidates for tools/calibrate.py.

Usage: build_candidates.py WMN_JSON SHERLOCK_JSON MAIGRET_JSON OUTPUT_JSON

Nothing here is trusted: a candidate only reaches sites.json by passing
calibration. Hacking sites are left out, as are ones their lists mark as archived
or disabled. Adult sites are filed under the "adult" category. Dating sites are
only tried as a plain page load, with no API and no adjustments, so only openly
public profiles can pass, and that holds for a dating site that is also adult.
Rule sets for the same service share their test usernames.
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from handlecheck import paths

ROOT = Path(__file__).resolve().parent.parent
EXCLUDED_WMN_CATEGORIES = {"archived"}
ADULT_WMN_CATEGORY = "xx NSFW xx"
# Adult services the source lists don't tag as adult.
ADULT_HOSTS = {"fansly.com", "fetlife.com", "redgifs.com"}
DATING_HOSTS = {
    "badoo.com", "bumble.com", "coffeemeetsbagel.com", "eharmony.com", "feeld.co", "grindr.com",
    "happn.com", "hinge.co", "match.com", "okcupid.com", "pof.com", "plentyoffish.com",
    "tinder.com", "zoosk.com",
}
CATEGORY_MAP = {"coding": "dev", "tech": "dev", "video": "media", "music": "media", "images": "media"}
API_LIKE = re.compile(r"(^|[./])api[./]|/api/|\.json|\.xml|/v\d+/|graphql", re.IGNORECASE)
EXCLUDED_MAIGRET_TAGS = {"hacking"}
ADULT_MAIGRET_TAGS = {"porn", "webcam", "erotic"}
# Forum and hosting software with a documented public API for user lookups.
ENGINE_APIS = {
    "Discourse": ("/u/{username}.json", None, None),
    "Gitea": ("/api/v1/users/{username}", None, None),
    "GitLab": ("/api/v4/users?username={username}", '"username"', r"^\s*\[\s*\]\s*$"),
    "Flarum": ("/api/users/{username}?bySlug=true", None, None),
    "Mastodon": ("/api/v1/accounts/lookup?acct={username}", None, None),
    "MediaWiki": ("/w/api.php?action=query&list=users&format=json&ususers={username}", '"userid"', '"missing"'),
}
SUBDOMAIN_PATTERN = r"^[A-Za-z0-9-]{1,63}$"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def host_of(url: str) -> str:
    host = urlsplit(url.replace("{username}", "x")).hostname or ""
    return host.removeprefix("www.").lower()


def registrable(host: str) -> str:
    """The part of a subdomain-template host that identifies the service."""
    return ".".join(host.split(".")[-2:]) if host.startswith("x.") else host


def https(url: str) -> str:
    return "https://" + url[len("http://"):] if url.startswith("http://") else url


def literal(text: str) -> str:
    """Escape a literal string, keeping the name placeholder usable."""
    escaped = re.escape(text)
    for placeholder in (r"\{account\}", r"\{\}", r"\{username\}"):
        escaped = escaped.replace(placeholder, "{username}")
    return escaped


def valid_pattern(pattern: str | None) -> str | None:
    if not pattern:
        return None
    try:
        re.compile(pattern)
    except re.error:
        return None
    return pattern


def finish(entry: dict, likely: list[str]) -> dict:
    if entry["profile_url"].startswith("https://{username}") and "username_pattern" not in entry:
        entry["username_pattern"] = SUBDOMAIN_PATTERN
    names = [n for n in dict.fromkeys(likely) if NAME_RE.fullmatch(n)]
    entry["known_user"] = names[0] if names else "john"
    entry["likely"] = names
    return entry


def from_wmn(site: dict) -> dict:
    check = https(site["uri_check"]).replace("{account}", "{username}")
    pretty = https(site.get("uri_pretty", site["uri_check"])).replace("{account}", "{username}")
    category = CATEGORY_MAP.get(site["cat"], site["cat"])
    entry = {"name": site["name"], "category": category}
    # Needing a POST or special headers means it can't be loaded as a page.
    if site.get("post_body") or site.get("headers"):
        entry.update(profile_url=pretty, manual=True)
    elif check != pretty and (API_LIKE.search(check) or host_of(check) != host_of(pretty)):
        entry.update(profile_url=pretty, probe_url=check)
    else:
        entry["profile_url"] = check
    if not entry.get("manual"):
        if site.get("e_string"):
            entry["found_regex"] = literal(site["e_string"])
        if site.get("m_string"):
            entry["not_found_regex"] = literal(site["m_string"])
    return finish(entry, list(site.get("known", [])))


def from_sherlock(name: str, site: dict) -> dict:
    url = https(site["url"]).replace("{}", "{username}")
    entry = {"name": name, "category": "misc", "profile_url": url}
    if pattern := valid_pattern(site.get("regexCheck")):
        entry["username_pattern"] = pattern
    if site.get("request_payload") or site.get("headers") or site.get("request_method") not in (None, "GET"):
        entry["manual"] = True
    else:
        # A probe that is just another web page still goes through the browser.
        if site.get("urlProbe") and API_LIKE.search(site["urlProbe"]):
            entry["probe_url"] = https(site["urlProbe"]).replace("{}", "{username}")
        if site.get("errorType") == "message" and site.get("errorMsg"):
            messages = site["errorMsg"]
            messages = [messages] if isinstance(messages, str) else messages
            entry["not_found_regex"] = "|".join(literal(m) for m in messages)
    return finish(entry, [site.get("username_claimed", "")])


def merged_maigret(site: dict, engines: dict) -> dict:
    engine = engines.get(site.get("engine"), {}).get("site", {})
    merged = {**engine, **site}
    for key in ("url", "urlProbe"):
        if merged.get(key):
            merged[key] = (
                merged[key]
                .replace("{urlMain}", merged.get("urlMain", ""))
                .replace("{urlSubpath}", merged.get("urlSubpath", ""))
            )
    return merged


def from_maigret(name: str, site: dict) -> dict | None:
    if not site.get("url") or "{username}" not in site["url"]:
        return None
    url = https(site["url"])
    if not url.startswith("https://"):
        return None
    tags = [t for t in site.get("tags", []) if len(t) > 2]
    category = tags[0] if tags else "misc"
    entry = {"name": name, "category": CATEGORY_MAP.get(category, category), "profile_url": url}
    if pattern := valid_pattern(site.get("regexCheck")):
        entry["username_pattern"] = pattern
    needs_more = site.get("headers") or site.get("requestPayload") or site.get("activation")
    if needs_more or site.get("requestMethod") not in (None, "GET"):
        entry["manual"] = True
        return finish(entry, [site.get("usernameClaimed", "")])
    if site.get("urlProbe") and API_LIKE.search(site["urlProbe"]):
        entry["probe_url"] = https(site["urlProbe"])
    if site.get("checkType") == "message":
        if site.get("presenseStrs"):
            entry["found_regex"] = "|".join(literal(s) for s in site["presenseStrs"])
        if site.get("absenceStrs"):
            entry["not_found_regex"] = "|".join(literal(s) for s in site["absenceStrs"])
    return finish(entry, [site.get("usernameClaimed", "")])


def as_public_page_only(entry: dict) -> dict | None:
    """Reduce a rule set to a plain page load, or None if it can't be one."""
    if entry.get("manual") or entry.get("engine_api"):
        return None
    entry = {**entry, "category": "dating", "public_page_only": True}
    # Strings written for an API response won't appear on the page.
    if entry.pop("probe_url", None):
        entry.pop("found_regex", None)
        entry.pop("not_found_regex", None)
    return entry


def engine_api_rule(name: str, site: dict, page_rule: dict) -> dict | None:
    """A rule using the site's software API, tried ahead of loading the page."""
    api = ENGINE_APIS.get(site.get("engine"))
    if not api or page_rule.get("manual") or not site.get("urlMain"):
        return None
    path, found, missing = api
    base = https(site["urlMain"]).rstrip("/") + site.get("urlSubpath", "")
    entry = {**page_rule, "name": name, "probe_url": base + path, "engine_api": True}
    entry.pop("found_regex", None)
    entry.pop("not_found_regex", None)
    if found:
        entry["found_regex"] = found
    if missing:
        entry["not_found_regex"] = missing
    return entry


def main() -> None:
    wmn_path, sherlock_path, maigret_path, output = sys.argv[1:5]
    current = json.loads(paths.sites_file().read_text())
    seen = {registrable(host_of(s["profile_url"])) for s in current}

    wmn = json.loads(Path(wmn_path).read_text())["sites"]
    sherlock = json.loads(Path(sherlock_path).read_text())
    sherlock.pop("$schema", None)
    maigret = json.loads(Path(maigret_path).read_text())
    maigret_sites = {n: merged_maigret(s, maigret["engines"]) for n, s in maigret["sites"].items()}

    # Sites on these hosts stay out whichever list names them.
    excluded = {
        registrable(host_of(https(s["uri_check"]).replace("{account}", "{username}")))
        for s in wmn
        if s["cat"] in EXCLUDED_WMN_CATEGORIES
    }
    excluded |= {
        registrable(host_of(s.get("url") or s.get("urlMain") or ""))
        for s in maigret_sites.values()
        if EXCLUDED_MAIGRET_TAGS.intersection(s.get("tags", []))
    }
    excluded.discard("")
    adult = set(ADULT_HOSTS) | {
        registrable(host_of(https(s["uri_check"]).replace("{account}", "{username}")))
        for s in wmn
        if s["cat"] == ADULT_WMN_CATEGORY
    }
    adult |= {registrable(host_of(s["url"].replace("{}", "{username}"))) for s in sherlock.values() if s.get("isNSFW")}
    adult |= {
        registrable(host_of(s.get("url") or s.get("urlMain") or ""))
        for s in maigret_sites.values()
        if ADULT_MAIGRET_TAGS.intersection(s.get("tags", []))
    }
    dating = set(DATING_HOSTS)
    dating |= {
        registrable(host_of(https(s["uri_check"]).replace("{account}", "{username}")))
        for s in wmn
        if s["cat"] == "dating"
    }
    dating |= {
        registrable(host_of(s.get("url") or s.get("urlMain") or ""))
        for s in maigret_sites.values()
        if "dating" in s.get("tags", [])
    }
    dating -= excluded
    dating.discard("")

    groups: dict[str, list[dict]] = {}
    skipped = {"already supported": 0, "excluded": 0, "dating, needs more than a page load": 0}

    def add(entry: dict) -> None:
        key = registrable(host_of(entry["profile_url"]))
        if not key:
            return
        if key in excluded:
            skipped["excluded"] += 1
            return
        if key in dating:
            entry = as_public_page_only(entry)
            if entry is None:
                skipped["dating, needs more than a page load"] += 1
                return
        elif key in adult:
            entry = {**entry, "category": "adult"}
        if key in seen:
            skipped["already supported"] += 1
        else:
            groups.setdefault(key, []).append(entry)

    for site in wmn:
        if site["cat"] in EXCLUDED_WMN_CATEGORIES:
            skipped["excluded"] += 1
        else:
            add(from_wmn(site))
    for name, site in sherlock.items():
        add(from_sherlock(name, site))
    for name, site in maigret_sites.items():
        if site.get("disabled") or EXCLUDED_MAIGRET_TAGS.intersection(site.get("tags", [])):
            skipped["excluded"] += 1
        elif entry := from_maigret(name, site):
            if api_rule := engine_api_rule(name, site, entry):
                add(api_rule)
            add(entry)
    for name, category, url in json.loads((ROOT / "tools" / "extra_sites.json").read_text()):
        add(finish({"name": name, "category": category, "profile_url": url}, []))

    for rules in groups.values():
        rules.sort(key=lambda e: not e.get("probe_url"))
        shared = [name for entry in rules for name in entry["likely"]]
        for entry in rules:
            entry["likely"] = list(dict.fromkeys(entry["likely"] + shared))

    Path(output).write_text(json.dumps(groups, indent=1))
    print(f"{len(groups)} services, {sum(map(len, groups.values()))} rule sets; skipped {skipped}")


if __name__ == "__main__":
    main()
