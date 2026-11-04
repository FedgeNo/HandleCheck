# handlecheck

Enter a username and see which online services it exists on.

Services with a public API are asked directly. Everything else is loaded in a
headless browser: Chromium if installed, otherwise Chrome, otherwise Firefox.
If a browser can't get a clear answer from a site, for example because it gets
blocked, that site is tried again in the next browser. `--browser` limits
checks to one browser.

## Install

```sh
./install.sh
```

| What | Where |
|---|---|
| Code, the working repository | `~/.local/lib/handlecheck/source` |
| Command | `~/.local/bin/handlecheck` |
| Data | `$XDG_DATA_HOME/handlecheck`, by default `~/.local/share/handlecheck` |

To upgrade, run `handlecheck --update`. A git clone is pulled, and it stops without
changing anything if you have local changes or your history has diverged. Any other
install is replaced with a fresh download from GitHub, so edits to its code are lost.

A site list you calibrate is saved in the data directory, where it overrides the one
in the code. Updates don't touch it, so it won't pick up new sites until you rebuild
or remove it.

Run as root, the installer also creates a locked `handlecheck-browser` account,
and handlecheck starts every browser under it. It won't start a browser as root if
that account is missing. Other users can't switch accounts, so their browsers run
as themselves.

## Use

```sh
handlecheck                     # prompts for a username
handlecheck alice bob           # one or more names
handlecheck alice --all         # also list services with no match
handlecheck alice -s gaming,github
handlecheck alice --fast        # services with an API only, no browser
handlecheck alice --browser firefox
handlecheck alice --json
handlecheck --list              # supported services
handlecheck --verify            # test that detection still works
handlecheck --update            # update to the latest version
```

## Reading the results

- **Found**: the service confirmed an account with that name. This means the
  name is taken, not that it belongs to the person you have in mind.
- **Not found**: the service said no such account exists.
- **Check these yourself**: a link to open, because there's no reliable answer.
  Either every browser hit a rate limit, bot challenge, login wall, timeout or
  unexpected page (the reason is shown), or the service is marked `manual` and
  gets no request. These never count as found or not found.
- **Name not allowed**: the name breaks that service's naming rules.
- **(untested rule)**: this match comes from a source list's rules, which couldn't
  be tested because the site blocked the test. It's probably right, but less
  certain than the others. The same rules can't report "no account", so that
  shows as a link instead.

Names like `login` that are also pages on a site are listed under "check these
yourself" on browser-checked services, because the page would look like a
profile. Those links open the site's own page.

## How browsers are used

- Only one browser ever runs, even across separate runs of handlecheck.
- Each site gets its own private tab, closed when finished, with up to 5 open at
  once (`--tabs`).
- The browser restarts after every 30 tabs.
- Sites the browser couldn't answer are saved and retested after it has finished,
  in a browser with a different engine, using the same 30-tab restarts. Chrome is
  the same engine as Chromium, so it only runs if Chromium is missing or won't
  start. Sites that are unreachable aren't retested.
- Browsers run at low priority, new tabs wait if under 700 MB of memory is free,
  and browsers die with handlecheck, even if it is killed with `kill -9`.

## Dating sites

Dating sites are only included if a logged-out browser can see the profile. They
get one plain page load: no API, no retest and no adjustments to the browser.

## Keeping it working

Sites change, so run `handlecheck --verify` now and then. For each service it
looks up a real account and a random name, and passes only if the first is found
and the second is not. A failure on a service that is rate limiting you can be
temporary.

Services live in `handlecheck/sites.json`:

| Field | Meaning |
| --- | --- |
| `profile_url` | Page to show the user |
| `probe_url` | Public API or feed to ask. Without one, a browser loads `profile_url` |
| `known_user` | A real account, used by `--verify` |
| `username_pattern` | Names the service allows. Must exclude dots for subdomain URLs |
| `found_regex` | Must match before the page counts as a real profile |
| `not_found_regex` | Matching means no such account |
| `allow_redirect` | The service redirects to a URL without the name in it |
| `manual` | Send nothing, and link to it for checking by hand |
| `public_page_only` | One plain page load, as for dating sites |
| `redirect_means_missing` | The service sends missing accounts elsewhere. Login and challenge pages still count as inconclusive |
| `calibrated` | Added by `tools/merge_results.py`, which replaces these on each run |
| `unverified` | Rules from a source list that couldn't be tested. They can report a match, but "no account" becomes a link |

Add a `found_regex` for any site that returns 200 for everything.

Where a service has a documented public route (an API, oEmbed or RSS feed) it is
used instead of a page. Browsers run with a throwaway profile, signed in to
nothing. Outside dating sites, Chromium reports its normal browser name rather
than `HeadlessChrome`, and fonts, video and common trackers aren't loaded. There
is no CAPTCHA solving, no proxying and no logging in. If a site still challenges
the browser, it goes under "check these yourself".

## Adding more services

`tools/` builds and tests a much larger list from three open-source projects:

```sh
cd ~/.local/lib/handlecheck/source
WORK=${XDG_DATA_HOME:-$HOME/.local/share}/handlecheck
.venv/bin/python tools/build_candidates.py sources/whatsmyname/wmn-data.json sources/sherlock/data.json sources/maigret/data.json "$WORK/candidates.json"
.venv/bin/python tools/calibrate.py "$WORK/candidates.json" "$WORK/results.jsonl"   # resumes if restarted
.venv/bin/python tools/merge_results.py "$WORK/results.jsonl"   # or several, newest last
```

Each site is tested with two made-up names and up to several likely ones: the
sample accounts from all three lists, its own name, and common first names. It
has to reject both fakes and find a likely name. If a site seems to have blocked
the test, and its source list says what a real profile contains, those rules are
used as they are and marked `unverified`. Other failures become links, and dead
sites and dating sites without public profiles are dropped. Hacking sites are
left out. Adult sites are checked like any other, under the `adult` category, so
`-s adult` selects them. An adult site that is also a dating site follows the
dating rules.

The lists are in [sources/](sources/README.md), with their licenses and the
exact upstream versions: [WhatsMyName](https://github.com/WebBreacher/WhatsMyName)
(CC BY-SA 4.0), [Sherlock](https://github.com/sherlock-project/sherlock) (MIT) and
[Maigret](https://github.com/soxoj/maigret) (MIT). A site list built from WhatsMyName
stays under CC BY-SA 4.0.

## Tests

```sh
.venv/bin/python -m pytest
```

The browser tests drive each installed browser against a local server.

## License

The code is MIT, see [LICENSE](LICENSE). The lists in [sources/](sources/README.md)
keep their own licenses. `handlecheck/sites.json` is built from those lists, including
WhatsMyName, so it is under [CC BY-SA 4.0](sources/whatsmyname/LICENSE.md).
