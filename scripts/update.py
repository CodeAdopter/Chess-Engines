"""Bring engines.json, engines.txt and ccrl.json up to date.

One run does everything, with as few requests as it can:

1. Adds the engines listed in new_engines.txt, one "Name owner/repo" per line, and takes their
   lines out. A line that can't be added stays, with the reason after it.
2. Refreshes every GitHub engine: repository_stats, repository_archived, and repository_available,
   which is false once the repository is gone. A renamed repository has its repository and URLs
   moved to the new name. A blank language or description is filled from GitHub; hand-written
   fields are kept.
3. Rebuilds ccrl.json from the CCRL 40/15 and Blitz rating lists (open-source engines on one CPU).
   The two list pages are fetched at most once a week, as agreed with the CCRL team. Between fetches the parsed lists are kept in
   .cache/ccrl_lists.json, so an engine added mid-week is matched without asking CCRL again.

GitHub cost: one GraphQL query per 40 engines, plus one REST call for each engine pushed since the
last run (to count contributors). The token comes from GITHUB_TOKEN, or from `gh auth token`.
The last line on stdout sums up what changed, for a commit message.

usage:
  python scripts/update.py                    update everything
  python scripts/update.py --dry-run          report what would change, write nothing
  python scripts/update.py --no-ccrl          leave ccrl.json alone
  python scripts/update.py --ccrl-pages A B   read saved 40/15 and Blitz pages instead of fetching

Only the Python standard library is required.
"""

import argparse
import datetime
import html
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENGINES = ROOT / "engines.json"
NAMES = ROOT / "engines.txt"
NEW = ROOT / "new_engines.txt"
CCRL = ROOT / "ccrl.json"
CCRL_CACHE = ROOT / ".cache" / "ccrl_lists.json"
API = "https://api.github.com"
BATCH = 40
AGENT = "Chess-Engines registry (https://github.com/CodeAdopter/Chess-Engines)"
# more repositories than this missing in one run is taken as a GitHub fault, and nothing is written
MISSING_MOST = 10

CCRL_HOME = "https://computerchess.org.uk/ccrl/"
CCRL_LISTS = [("40/15", "CCRL 40/15", "4040/"), ("blitz", "CCRL Blitz", "404/")]
# the text list of open-source engines on one CPU, best version of each: the registry's kind of
# engine, all rated on the same hardware, so their ratings compare
CCRL_QUERY = "cgi/compare_engines.cgi?print=Rating+list+%28text%29&class=Open+source+single-CPU+engines&only_best_in_class=1"
CCRL_EVERY = 7

NEW_HEADER = """\
# Engines to add to engines.json, one per line: the name, then the GitHub repository.
#   Stockfish official-stockfish/Stockfish
#   Leela Chess Zero https://github.com/LeelaChessZero/lc0
# python scripts/update.py adds them and takes their lines out. A line it can't add stays, with the reason after it.
"""

REPO_FIELDS = """
  nameWithOwner
  description
  primaryLanguage { name }
  stargazerCount
  forkCount
  createdAt
  pushedAt
  isArchived
  releases { totalCount }
  defaultBranchRef {
    target {
      ... on Commit {
        all: history(first: 0) { totalCount }
        recent: history(first: 0, since: "%s") { totalCount }
      }
    }
  }
"""
# only asked for engines being added: what a new entry's license and documentation links need
NEW_FIELDS = """
  licenseInfo { name spdxId }
  root: object(expression: "HEAD:") { ... on Tree { entries { name type } } }
"""
REPO_RE = re.compile(r"^(?:https?://github\.com/)?([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$")


def log(text):
    print(text, file=sys.stderr)


def token():
    t = os.environ.get("GITHUB_TOKEN", "").strip()
    if t:
        return t
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        sys.exit("No token: set GITHUB_TOKEN or log in with gh.")


def request(url, tok=None, body=None, tries=3):
    """The parsed JSON and headers of a GitHub call, tried again after a server fault or a timeout."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/vnd.github+json", "User-Agent": AGENT}
    if tok:
        headers["Authorization"] = "Bearer " + tok
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=60) as r:
                raw = r.read()
                return (json.loads(raw) if raw else None), r.headers
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == tries - 1:
                raise
        time.sleep(10 * (attempt + 1))


def graphql_batch(repos, tok, since, extra=""):
    """A node per repository, or None when GitHub can't find it or won't show it. An error that isn't
    about one repository stops the run."""
    parts = []
    for i, (owner, name) in enumerate(repos):
        parts.append("r%d: repository(owner: %s, name: %s) {%s%s}" % (i, json.dumps(owner), json.dumps(name), REPO_FIELDS % since, extra))
    body, _ = request(API + "/graphql", tok, {"query": "query {\n" + "\n".join(parts) + "\n}"})
    data = body.get("data") or {}
    not_found = set()
    for err in body.get("errors") or []:
        if err.get("type") in ("NOT_FOUND", "FORBIDDEN") and err.get("path"):
            not_found.add(err["path"][0])
        else:
            sys.exit("GitHub: " + err.get("message", str(err)))
    nodes = []
    for i in range(len(repos)):
        node = data.get("r%d" % i)
        if node is None and "r%d" % i not in not_found:
            sys.exit("GitHub gave nothing for %s/%s" % repos[i])
        nodes.append(node)
    return nodes


def graphql_all(repos, tok, since, extra=""):
    nodes = []
    for start in range(0, len(repos), BATCH):
        nodes += graphql_batch(repos[start:start + BATCH], tok, since, extra)
        if len(repos) > BATCH:
            log("GitHub: %d of %d" % (min(start + BATCH, len(repos)), len(repos)))
    return nodes


def contributors(repo, tok):
    """The number of contributors with GitHub accounts, read from the last page link at one per page."""
    try:
        body, headers = request("%s/repos/%s/contributors?per_page=1" % (API, repo), tok)
    except urllib.error.HTTPError:
        return None
    m = re.search(r'[?&]page=(\d+)>; rel="last"', headers.get("Link", ""))
    if m:
        return int(m.group(1))
    return len(body) if isinstance(body, list) else 0


def day(stamp):
    return stamp[:10] if stamp else None


def stats_of(node, contributor_count, today):
    target = (node.get("defaultBranchRef") or {}).get("target") or {}
    return {
        "checked_at": today,
        "stars": node["stargazerCount"],
        "forks": node["forkCount"],
        "commits": (target.get("all") or {}).get("totalCount", 0),
        "commits_last_year": (target.get("recent") or {}).get("totalCount", 0),
        "contributors": contributor_count,
        "releases": node["releases"]["totalCount"],
        "created_at": day(node.get("createdAt")),
        "pushed_at": day(node.get("pushedAt")),
    }


def put(entry, key, value, after):
    """The entry with key set to value, placed right after `after` when the key is new."""
    if key in entry:
        entry[key] = value
        return entry
    out = {}
    for k, v in entry.items():
        out[k] = v
        if k == after:
            out[key] = value
    out.setdefault(key, value)
    return out


def slug(name):
    return re.sub(r"[^0-9a-z]+", "-", name.casefold()).strip("-")


def squash(text):
    return re.sub(r"[^0-9a-z]", "", text.casefold())


def read_text(path):
    """The text of a file and its newline, \\r\\n when it has one."""
    if not path.exists():
        return "", "\n"
    raw = path.read_bytes().decode("utf-8")
    return raw.replace("\r\n", "\n"), "\r\n" if "\r\n" in raw else "\n"


def write_text(path, text, newline):
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))


def write_json(path, value, newline):
    write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n", newline)


# --- new_engines.txt ---------------------------------------------------------------------------

def read_new(text):
    """The engine lines of new_engines.txt as dicts with the name, the owner and repository, and the
    problem found while reading, if any."""
    wanted = []
    for line in text.split("\n"):
        line = line.split(" # ", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        words = line.split()
        m = REPO_RE.match(words[-1])
        name = " ".join(words[:-1])
        item = {"name": name, "given": words[-1], "repo": None, "problem": None}
        if not m:
            item["problem"] = "the last word should be the repository, as owner/name or a github.com link"
        else:
            item["repo"] = m.group(1) + "/" + m.group(2)
            item["name"] = name or m.group(2)
        wanted.append(item)
    return wanted


def read_new_file():
    text, newline = read_text(NEW)
    return text or NEW_HEADER, newline


def new_file_text(left):
    lines = ["%s %s  # not added: %s" % (item["name"], item["given"], item["problem"]) for item in left]
    return NEW_HEADER + "".join(line + "\n" for line in lines)


def new_entry(name, node, stats):
    """A catalog entry for an engine being added, or a reason it can't be."""
    repo = node["nameWithOwner"]
    url = "https://github.com/" + repo
    files = [e["name"] for e in ((node.get("root") or {}).get("entries") or []) if e.get("type") == "blob"]

    def first(pattern, prefer):
        hits = [f for f in files if re.fullmatch(pattern, f, re.I)]
        return prefer if prefer in hits else (sorted(hits)[0] if hits else None)

    readme = first(r"readme(\.\w+)?", "README.md")
    license_file = first(r"(licen[cs]e|copying)(\.\w+)?", "LICENSE")
    info = node.get("licenseInfo")
    if info and info.get("spdxId") != "NOASSERTION":
        license_name = info["name"]
    elif license_file:
        license_name = "Other (see the license file)"
    else:
        return None, "no license found in the repository"
    blob = url + "/blob/HEAD/"
    return {
        "id": slug(name),
        "name": name,
        "description": (node.get("description") or "").strip() or None,
        "language": (node.get("primaryLanguage") or {}).get("name"),
        "repository_provider": "github",
        "repository": repo,
        "repository_url": url,
        "repository_archived": node["isArchived"],
        "repository_available": True,
        "repository_stats": stats,
        "documentation_url": blob + urllib.parse.quote(readme) if readme else url,
        "license": {"name": license_name, "url": blob + urllib.parse.quote(license_file) if license_file else url},
    }, None


# --- GitHub refresh ---------------------------------------------------------------------------

def moved(url, old, new):
    """A github.com link into the old repository, pointed at the new one."""
    if not url:
        return url
    prefix = "https://github.com/" + old
    if url.casefold() == prefix.casefold() or url.casefold().startswith(prefix.casefold() + "/"):
        return "https://github.com/" + new + url[len(prefix):]
    return url


def refresh(entry, node, contributor_count, today):
    """The entry brought up to date from its GitHub node, and the name it was renamed from, if any."""
    old, new = entry["repository"], node["nameWithOwner"]
    renamed = old if new != old else None
    if renamed:
        entry["repository"] = new
        for key in ("repository_url", "documentation_url"):
            entry[key] = moved(entry.get(key), old, new)
        if isinstance(entry.get("license"), dict):
            entry["license"]["url"] = moved(entry["license"].get("url"), old, new)
    if entry.get("language") is None and node.get("primaryLanguage"):
        entry["language"] = node["primaryLanguage"]["name"]
    if not entry.get("description") and node.get("description"):
        entry["description"] = node["description"].strip()
    entry["repository_archived"] = node["isArchived"]
    entry = put(entry, "repository_available", True, "repository_archived")
    entry = put(entry, "repository_stats", stats_of(node, contributor_count, today), "repository_available")
    return entry, renamed


def without_dates(value):
    """The catalog with the days it was checked taken out, to tell a real change from a re-check."""
    if isinstance(value, dict):
        return {k: without_dates(v) for k, v in value.items() if k not in ("checked_at", "updated_at")}
    if isinstance(value, list):
        return [without_dates(v) for v in value]
    return value


# --- CCRL ----------------------------------------------------------------------------------------

LIST_ROW_RE = re.compile(r"^\s*(\d+)(?:-\d+)?\s+(.+?)\s+(\d{3,4})\s+\+(\d+)\s+-(\d+)\s+[\d.]+%\s+\S+\s+(\d+)\s*$")
COMPUTED_RE = re.compile(r"Computed on (\w+ \d{1,2}, \d{4})")


def parse_ccrl(page):
    """The rows of a CCRL text rating list, in rank order, and the day it was computed."""
    text = re.sub(r"<br\s*/?>", "\n", page, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).replace("\xa0", " ")
    rows = []
    for line in text.split("\n"):
        m = LIST_ROW_RE.match(line)
        if m:
            rows.append({"version": m.group(2), "rank": int(m.group(1)), "rating": int(m.group(3)),
                         "margin": max(int(m.group(4)), int(m.group(5))), "games": int(m.group(6))})
    if len(rows) < 100:
        raise ValueError("found %d rows, so the list layout may have changed" % len(rows))
    m = COMPUTED_RE.search(text)
    try:
        list_date = datetime.datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat() if m else None
    except ValueError:
        list_date = None
    return {"list_date": list_date, "rows": rows}


def fetch_page(url):
    req = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


def ccrl_lists(old, pages, today):
    """The parsed CCRL lists, from the cache while the last fetch is under a week old, else fetched
    again; and whether they were fetched (or read from pages) now."""
    cache = {}
    if CCRL_CACHE.exists():
        cache = json.loads(CCRL_CACHE.read_text(encoding="utf-8"))
    last = [((old.get("lists") or {}).get(key) or {}).get("fetched_at") for key, _, _ in CCRL_LISTS]
    due = pages or any(
        not stamp or (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(stamp)).days >= CCRL_EVERY
        for stamp in last)
    if not due and all(key in cache for key, _, _ in CCRL_LISTS):
        return cache, False
    if not due:
        log("CCRL: no saved lists and the last fetch was under %d days ago, so ccrl.json is kept" % CCRL_EVERY)
        return None, False
    fresh = {}
    for i, (key, title, path) in enumerate(CCRL_LISTS):
        if pages:
            text = pathlib.Path(pages[i]).read_text(encoding="utf-8", errors="replace")
        else:
            text = fetch_page(CCRL_HOME + path + CCRL_QUERY)
        parsed = parse_ccrl(text)
        parsed["fetched_at"] = today
        fresh[key] = parsed
        log("CCRL: %s, %d engines, computed %s" % (title, len(parsed["rows"]), parsed["list_date"]))
    return fresh, True


def ccrl_catalog(lists, engines):
    """ccrl.json: the CCRL ratings of the catalog's engines. A CCRL row belongs to the engine whose
    name, id or alias equals the longest run of its leading words, so "Rodent IV 0.3" is Rodent IV."""
    owner = {}
    for e in engines:
        names = ([e["ccrl_name"]] if e["ccrl_name"] else []) if "ccrl_name" in e else [e["name"], e["id"], *e.get("aliases", [])]
        for n in names:
            owner.setdefault(squash(n), e["id"])
    found = {}
    for key, _, path in CCRL_LISTS:
        for row in lists[key]["rows"]:
            words = row["version"].split()
            for n in range(len(words), 0, -1):
                hit = owner.get(squash(" ".join(words[:n])))
                if hit:
                    break
            if not hit or key in found.get(hit, {}):
                continue
            found.setdefault(hit, {})[key] = dict(row, ccrl_name=" ".join(words[:n]),
                url=CCRL_HOME + path + "cgi/engine_details.cgi?print=Details&each_game=0&eng=" + urllib.parse.quote(row["version"]))
    rated = []
    for e in engines:
        ratings = found.get(e["id"])
        if ratings:
            first = next(iter(ratings.values()))
            rated.append({"id": e["id"], "name": e["name"], "ccrl_name": first["ccrl_name"], "ratings": {
                key: {k: r[k] for k in ("rating", "margin", "rank", "games", "version", "url")} for key, r in ratings.items()}})
    return {
        "schema_version": 1,
        "label": "CCRL Rating",
        "description": "Ratings from the Computer Chess Rating Lists (CCRL) for the engines in engines.json, used with "
                       "the permission of the CCRL team. Show them labelled \"CCRL Rating\", with a link to "
                       "https://computerchess.org.uk/ccrl/.",
        "source": {"name": "Computer Chess Rating Lists (CCRL)", "url": CCRL_HOME},
        "lists": {key: {"name": title, "url": CCRL_HOME + path, "selection": "Open source single-CPU engines, best versions only",
                        "computed_at": lists[key]["list_date"], "fetched_at": lists[key]["fetched_at"],
                        "engines_listed": len(lists[key]["rows"])}
                  for key, title, path in CCRL_LISTS},
        "metadata_notes": {
            "selection": "Every rating comes from the list of open-source engines on one CPU, best version of each "
                         "engine only, so ratings from the same list compare with each other.",
            "matching": "A CCRL row belongs to the engine whose name, id or alias equals the longest run of its leading "
                        "words, ignoring case and punctuation. An engine's ccrl_name in engines.json, when present, is "
                        "used instead; null means the engine has no CCRL rating.",
            "ratings": "Per list: the rating, the larger of its two error margins, the rank within the selection, games "
                       "played, the version CCRL rated, and its CCRL details page.",
        },
        "engines": rated,
    }


# --- the run -------------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    ap.add_argument("--no-ccrl", action="store_true", help="leave ccrl.json alone")
    ap.add_argument("--ccrl-pages", nargs=2, metavar=("PAGE_40_15", "PAGE_BLITZ"), help="read saved list pages instead of fetching")
    args = ap.parse_args()

    text, newline = read_text(ENGINES)
    catalog = json.loads(text)
    before = without_dates(catalog)
    engines = catalog["engines"]
    tok = token()
    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.date().isoformat()
    since = (now - datetime.timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # every listed GitHub repository, in batches
    github = [i for i, e in enumerate(engines) if e.get("repository_provider") == "github"]
    nodes = graphql_all([tuple(engines[i]["repository"].split("/", 1)) for i in github], tok, since)
    missing, renamed, gone = [], [], []
    for i, node in zip(github, nodes):
        e = engines[i]
        if node is None:
            missing.append(e["repository"])
            if e.get("repository_available") is not False:
                gone.append(e["name"])
            engines[i] = put(e, "repository_available", False, "repository_archived")
            continue
        old = e.get("repository_stats") or {}
        fresh_push = day(node.get("pushedAt")) != old.get("pushed_at") or "contributors" not in old
        count = contributors(node["nameWithOwner"], tok) if fresh_push else old["contributors"]
        engines[i], was = refresh(e, node, count, today)
        if was:
            renamed.append((was, node["nameWithOwner"]))
            log("renamed: %s -> %s" % (was, node["nameWithOwner"]))
    for repo in missing:
        log("missing: " + repo)
    if len(gone) > MISSING_MOST:
        sys.exit("%d repositories went missing at once, which looks like a GitHub fault, so nothing was written" % len(gone))

    # the engines new_engines.txt asks for
    new_text, new_newline = read_new_file()
    wanted = read_new(new_text)
    known_repos = {e["repository"].casefold(): e for e in engines}
    known_names = {e["name"].casefold(): e for e in engines}
    known_ids = {e["id"] for e in engines}
    asking = []
    for item in wanted:
        if item["problem"]:
            continue
        if item["repo"].casefold() in known_repos:
            log("already listed: %s as %s" % (item["repo"], known_repos[item["repo"].casefold()]["name"]))
            item["done"] = True
        elif item["name"].casefold() in known_names:
            item["problem"] = "the name is taken by %s" % known_names[item["name"].casefold()]["repository"]
        elif not slug(item["name"]) or slug(item["name"]) in known_ids:
            item["problem"] = "the id %r is taken" % slug(item["name"])
        else:
            asking.append(item)
    added = []
    if asking:
        found = graphql_all([tuple(item["repo"].split("/", 1)) for item in asking], tok, since, NEW_FIELDS)
        for item, node in zip(asking, found):
            if node is None:
                item["problem"] = "GitHub can't find the repository"
                continue
            if node["nameWithOwner"].casefold() in known_repos:
                log("already listed: %s as %s" % (item["repo"], known_repos[node["nameWithOwner"].casefold()]["name"]))
                item["done"] = True
                continue
            entry, problem = new_entry(item["name"], node, stats_of(node, contributors(node["nameWithOwner"], tok), today))
            if problem:
                item["problem"] = problem
                continue
            engines.append(entry)
            known_repos[entry["repository"].casefold()] = entry
            known_names[entry["name"].casefold()] = entry
            known_ids.add(entry["id"])
            added.append(entry["name"])
            item["done"] = True
            log("added: %s (%s)" % (entry["name"], entry["repository"]))
    left = [item for item in wanted if not item.get("done")]
    for item in left:
        log("not added: %s %s: %s" % (item["name"], item["given"], item["problem"]))
    engines.sort(key=lambda e: e["id"])

    catalog["metadata_notes"]["repository_stats"] = (
        "Public GitHub numbers as of checked_at, refreshed by scripts/update.py: stars, forks, commits on the "
        "default branch (all time and in the 365 days before checked_at), contributors with GitHub accounts (null "
        "when GitHub would not count them), releases, and the days the repository was created and last pushed. "
        "Absent for repositories not on GitHub. Kept from the last check when the repository is unavailable."
    )
    catalog["metadata_notes"]["repository_available"] = (
        "Whether GitHub still had the repository when it was last checked. A renamed repository is followed to its "
        "new name; false means it was deleted or made private. Absent for repositories not on GitHub."
    )
    catalog["metadata_notes"]["ccrl"] = "CCRL ratings of these engines are kept in ccrl.json, matched by name."
    changed = without_dates(catalog) != before
    if changed:
        catalog["updated_at"] = today

    # CCRL, from the week's lists
    ccrl_note = None
    if not args.no_ccrl:
        old_ccrl = json.loads(CCRL.read_text(encoding="utf-8")) if CCRL.exists() else {}
        try:
            lists, fetched = ccrl_lists(old_ccrl, args.ccrl_pages, today)
        except (OSError, ValueError, urllib.error.URLError) as e:
            log("CCRL: not updated: %s" % e)
            lists, fetched = None, False
        if lists:
            ccrl = ccrl_catalog(lists, engines)
            if ccrl != old_ccrl:
                if fetched:
                    ccrl_note = "CCRL lists of %s" % ", ".join(sorted({lists[k]["list_date"] or today for k, _, _ in CCRL_LISTS}))
                else:
                    ccrl_note = "update CCRL matches"
                log("CCRL: %d engines rated" % len(ccrl["engines"]))
                if not args.dry_run:
                    write_json(CCRL, ccrl, newline)
            if fetched and not args.dry_run:
                CCRL_CACHE.parent.mkdir(exist_ok=True)
                CCRL_CACHE.write_text(json.dumps(lists), encoding="utf-8")

    new_changed = bool(wanted) and new_file_text(left) != new_text
    if not args.dry_run:
        if changed:
            write_json(ENGINES, catalog, newline)
            names_newline = read_text(NAMES)[1]
            write_text(NAMES, "".join(n + "\n" for n in sorted({e["name"] for e in engines}, key=str.casefold)), names_newline)
        if new_changed:
            write_text(NEW, new_file_text(left), new_newline)

    parts = []
    if added:
        parts.append("add " + ", ".join(added))
    if renamed:
        parts.append("follow %d renamed repositor%s" % (len(renamed), "y" if len(renamed) == 1 else "ies"))
    if gone:
        parts.append("mark %s unavailable" % ", ".join(gone))
    if changed and not (added or renamed or gone):
        parts.append("refresh engine stats")
    if ccrl_note:
        parts.append(ccrl_note)
    if new_changed and left:
        parts.append("note why %d engine%s in new_engines.txt can't be added" % (len(left), "" if len(left) == 1 else "s"))
    summary = "; ".join(parts) or "nothing changed"
    log("%d engines checked, %d missing, %d added, %d left in new_engines.txt%s" % (
        len(github), len(missing), len(added), len(left), " (dry run)" if args.dry_run else ""))
    print(summary[0].upper() + summary[1:])


if __name__ == "__main__":
    main()
