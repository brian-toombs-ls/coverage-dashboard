#!/usr/bin/env python3
"""
Coverage collector — fetches unit test coverage from GitHub Actions artifacts
and appends results to coverage-history.csv. Falls back to the last known
value when artifacts have expired (HTTP 410).
"""

import csv
import http.client
import io
import json
import os
import re
import ssl
import sys
import zipfile
from datetime import date
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

REPOSITORIES = [
    "LegalSifter/api-documents",
    "LegalSifter/api-tickets",
    "LegalSifter/api-settings",
    "LegalSifter/automator",
    "LegalSifter/go-mail",
    "LegalSifter/ms-auth",
    "LegalSifter/ms-billing",
    "LegalSifter/ms-chat",
    "LegalSifter/ms-playbook",
    "LegalSifter/ms-profile",
    "LegalSifter/ms-project",
    "LegalSifter/ms-search",
    "LegalSifter/ms-sign",
    "LegalSifter/ms-storage",
    "LegalSifter/ms-share",
    "LegalSifter/go-microservices",
    "LegalSifter/workflows",
    "LegalSifter/ls-review-fe",
    "LegalSifter/control-bff",
    "LegalSifter/ReviewPro-GoogleDocsAddIn",
    "LegalSifter/reviewpro-onlyoffice",
]

# Repos that produce coverage-summary.json (Istanbul/Vitest json-summary reporter)
# Parsed as JSON rather than HTML — accurate and unambiguous.
ISTANBUL_REPOS = {
    "LegalSifter/ls-review-fe",
    "LegalSifter/control-bff",
    "LegalSifter/ReviewPro-GoogleDocsAddIn",
    "LegalSifter/reviewpro-onlyoffice",
}

# Repos that split coverage across several artifacts in one run. Every listed
# artifact must be present or the run is skipped — half a codebase is not a
# coverage number. Totals are combined line-weighted, not averaged, so a small
# surface cannot outweigh a large one.
MULTI_ARTIFACT_REPOS = {
    "LegalSifter/ReviewPro-GoogleDocsAddIn": ("server-coverage", "client-coverage"),
}

JACOCO_REPOS = {
    "LegalSifter/ms-auth",
    "LegalSifter/ms-profile",
    "LegalSifter/ms-project",
    "LegalSifter/ms-storage",
}

GITHUB_API = "https://api.github.com"
CSV_PATH = os.path.join(os.path.dirname(__file__), "coverage-history.csv")
CSV_COLUMNS = ["date", "repo", "coverage_pct", "source", "repo_pushed_at", "measured_on", "measured_branch"]


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def github_get(path, token, binary=False):
    url = path if path.startswith("http") else f"{GITHUB_API}{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    req = Request(url, headers=headers)
    with urlopen(req, context=ssl_context) as r:
        return r.read() if binary else json.loads(r.read())


def fetch_repo_meta(repo, token):
    """Return (last push date as YYYY-MM-DD, default branch). Either may be empty."""
    try:
        data = github_get(f"/repos/{repo}", token)
        pushed = data.get("pushed_at", "") or ""
        return pushed[:10], data.get("default_branch", "") or ""
    except Exception:
        return "", ""


def download_artifact(url, token):
    """Download a GitHub artifact zip, following the redirect to blob storage."""
    parsed = urlparse(url)
    conn = http.client.HTTPSConnection(parsed.netloc, context=ssl_context)
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    conn.request("GET", path, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "coverage-collector",
    })
    resp = conn.getresponse()

    if resp.status in (301, 302, 303, 307, 308):
        redirect = resp.getheader("Location")
        conn.close()
        rp = urlparse(redirect)
        rc = http.client.HTTPSConnection(rp.netloc, context=ssl_context)
        rc.request("GET", rp.path + (f"?{rp.query}" if rp.query else ""), headers={"User-Agent": "coverage-collector"})
        rr = rc.getresponse()
        if rr.status != 200:
            raise Exception(f"redirect failed: {rr.status}")
        data = rr.read()
        rc.close()
        return data

    if resp.status == 200:
        data = resp.read()
        conn.close()
        return data

    raise Exception(f"status {resp.status}")


# ---------------------------------------------------------------------------
# Coverage extraction
# ---------------------------------------------------------------------------

def extract_istanbul_json(json_bytes):
    """Parse coverage-summary.json produced by Istanbul json-summary reporter.
    Returns lines.pct from the 'total' key — unambiguous, no HTML scraping."""
    try:
        data = json.loads(json_bytes)
        total = data.get("total", {})
        return float(total["lines"]["pct"])
    except Exception:
        return None


def istanbul_line_counts(json_bytes):
    """Return (covered, total) line counts from an Istanbul coverage-summary.json.
    Raw counts rather than a percentage so several reports can be combined."""
    try:
        lines = json.loads(json_bytes)["total"]["lines"]
        return int(lines["covered"]), int(lines["total"])
    except Exception:
        return None


def extract_go_profile(text):
    """Statement coverage from a Go `coverage.out` profile.

    Each body line is `file:startLine.col,endLine.col numStatements hitCount`.
    This is the same arithmetic `go tool cover -func` reports as "total", and
    it is read in preference to the HTML report: `go tool cover -html` output
    carries only per-file percentages in a dropdown, with no total anywhere.
    """
    covered = total = 0
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 3 or ":" not in parts[0]:
            continue
        try:
            statements, hits = int(parts[1]), int(parts[2])
        except ValueError:
            continue
        total += statements
        if hits > 0:
            covered += statements
    return round(100.0 * covered / total, 2) if total else None


def extract_gocov(html):
    patterns = [
        r'<div\s+id=["\']totalcov["\'][^>]*>\s*(\d+(?:\.\d+)?)\s*%\s*</div>',
        r'Report\s+Total.*?(\d+(?:\.\d+)?)\s*%',
        r'Total[^<]*?(\d+(?:\.\d+)?)\s*%',
        r'>(\d+(?:\.\d+)?)\s*%<',
    ]
    for p in patterns:
        m = re.search(p, html, re.IGNORECASE | re.DOTALL)
        if m:
            return float(m.group(1))
    return None


def extract_jacoco(html):
    patterns = [
        r'<tfoot>.*?<tr>.*?Total.*?<td[^>]*class=["\']ctr2["\'][^>]*>(\d+(?:\.\d+)?)\s*%',
        r'>Total<.*?(\d+(?:\.\d+)?)\s*%',
        r'<td[^>]*class=["\']ctr2["\'][^>]*>(\d+(?:\.\d+)?)\s*%</td>(?!.*<td[^>]*class=["\']ctr2["\'])',
    ]
    for p in patterns:
        m = re.search(p, html, re.IGNORECASE | re.DOTALL)
        if m:
            return float(m.group(1))
    return None


def coverage_from_zip(zip_bytes, repo):
    is_jacoco = repo in JACOCO_REPOS
    is_istanbul = repo in ISTANBUL_REPOS
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()

        # Istanbul repos: parse coverage-summary.json directly — no HTML scraping needed
        if is_istanbul:
            for name in names:
                if name.endswith("coverage-summary.json"):
                    pct = extract_istanbul_json(zf.read(name))
                    if pct is not None:
                        return pct

        if is_jacoco:
            jacoco_paths = [
                "build/reports/jacoco/test/html/index.html",
                "reports/jacoco/test/html/index.html",
                "jacoco/test/html/index.html",
            ]
            for jp in jacoco_paths:
                for name in names:
                    if name.endswith(jp) or name == jp:
                        pct = extract_jacoco(zf.read(name).decode("utf-8", errors="ignore"))
                        if pct is not None:
                            return pct
            for name in names:
                if "jacoco" in name.lower() and name.endswith("index.html"):
                    pct = extract_jacoco(zf.read(name).decode("utf-8", errors="ignore"))
                    if pct is not None:
                        return pct

        # Go coverage profile: exact counts, and present whether or not the
        # run also produced a parseable HTML report.
        if not is_jacoco and not is_istanbul:
            for name in names:
                if name.endswith(".out") and "coverage" in name.lower():
                    pct = extract_go_profile(zf.read(name).decode("utf-8", errors="ignore"))
                    if pct is not None:
                        return pct

        for name in names:
            if "coverage" in name.lower() and name.endswith(".html"):
                pct = extract_gocov(zf.read(name).decode("utf-8", errors="ignore"))
                if pct is not None:
                    return pct

        for name in names:
            if name.endswith(".html"):
                html = zf.read(name).decode("utf-8", errors="ignore")
                pct = (extract_jacoco(html) if is_jacoco else None) or extract_gocov(html)
                if pct is not None:
                    return pct

    return None


def combined_coverage_from_run(repo, run_id, token, artifact_names):
    """Line-weighted coverage across a run's several coverage artifacts.
    Returns None unless every named artifact is present and parses."""
    try:
        art_data = github_get(f"/repos/{repo}/actions/runs/{run_id}/artifacts", token)
    except Exception:
        return None

    by_name = {a.get("name"): a for a in art_data.get("artifacts", [])}
    covered = total = 0

    for name in artifact_names:
        artifact = by_name.get(name)
        if artifact is None:
            return None

        try:
            zip_bytes = download_artifact(artifact["archive_download_url"], token)
        except Exception:
            return None

        counts = None
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for entry in zf.namelist():
                if entry.endswith("coverage-summary.json"):
                    counts = istanbul_line_counts(zf.read(entry))
                    if counts:
                        break
        if not counts:
            return None

        covered += counts[0]
        total += counts[1]

    return round(100.0 * covered / total, 2) if total else None


# Artifacts whose names match the fallback patterns but hold no coverage data.
# "GoSec Report" matched on "report" and was downloaded on every ms-billing
# collection; it parsed to nothing, but a template that rendered any percentage
# would have been recorded as coverage.
NON_COVERAGE_ARTIFACTS = ("gosec", "sigrid", "playwright", "e2e", "dockerbuild")


def find_coverage_artifact(artifacts):
    def usable(a):
        name = a.get("name", "").lower()
        return not any(bad in name for bad in NON_COVERAGE_ARTIFACTS)

    candidates = [a for a in artifacts if usable(a)]

    for a in candidates:
        name = a.get("name", "").lower()
        if "unit" in name and ("test" in name or "report" in name or "coverage" in name):
            return a
        if "coverage" in name:
            return a
    for a in candidates:
        name = a.get("name", "").lower()
        if "test" in name or "report" in name:
            return a
    return None


# ---------------------------------------------------------------------------
# Per-repo fetching (walks back through runs until a live artifact is found)
# ---------------------------------------------------------------------------

def fetch_coverage(repo, token, branch=""):
    """Collect coverage from the newest run carrying an artifact.

    The default branch is tried first, then any branch. A feature branch runs
    the same suite, so its number is a real measurement — just of unmerged
    code, which reads high when the branch is what added the tests. A same-day
    branch run still beats a three-week-old default-branch one, so the branch
    is recorded rather than the value discarded.
    """
    if branch:
        found = _coverage_from_runs(repo, token, branch)
        if found is not None:
            return found + (branch,)

    found = _coverage_from_runs(repo, token, "")
    return (found + ("any",)) if found is not None else None


def _coverage_from_runs(repo, token, branch):
    multi = MULTI_ARTIFACT_REPOS.get(repo)
    branch_q = f"&branch={branch}" if branch else ""

    page = 1
    while page <= 10:
        try:
            data = github_get(f"/repos/{repo}/actions/runs?per_page=10&page={page}{branch_q}", token)
        except Exception as e:
            print(f"  API error: {e}")
            return None

        runs = data.get("workflow_runs", [])
        if not runs:
            break

        # status=success is deliberately absent: passing it returns runs out of
        # chronological order, so the newest run holding an artifact can sit
        # beyond the ten pages walked here and never be seen. Unfiltered runs
        # come back newest-first, so success is checked per run instead.
        for run in runs:
            if run.get("conclusion") != "success":
                continue

            measured_on = (run.get("created_at") or "")[:10]

            if multi:
                pct = combined_coverage_from_run(repo, run["id"], token, multi)
                if pct is not None:
                    return pct, measured_on
                continue

            try:
                art_data = github_get(f"/repos/{repo}/actions/runs/{run['id']}/artifacts", token)
            except Exception:
                continue

            artifacts = art_data.get("artifacts", [])
            if not artifacts:
                continue

            artifact = find_coverage_artifact(artifacts)
            if not artifact:
                continue

            try:
                zip_bytes = download_artifact(artifact["archive_download_url"], token)
                pct = coverage_from_zip(zip_bytes, repo)
                if pct is not None:
                    return pct, measured_on
            except Exception as e:
                if "410" in str(e) or "status 410" in str(e):
                    continue
                print(f"  download error: {e}")
                continue

        page += 1

    return None


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

SOURCE_RANK = {"fresh": 0, "carried_forward": 1, "stale": 2, "unavailable": 3, "": 4}


def load_csv(csv_path):
    """Load all rows and deduplicate: one row per (date, repo), best source wins."""
    if not os.path.exists(csv_path):
        return {}

    # key: (date, repo) → best row seen so far
    best = {}
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            row.setdefault("repo_pushed_at", "")  # back-fill missing columns
            row.setdefault("measured_on", "")
            row.setdefault("measured_branch", "")
            key = (row["date"], row["repo"])
            existing = best.get(key)
            if existing is None:
                best[key] = row
            else:
                if SOURCE_RANK.get(row["source"], 9) < SOURCE_RANK.get(existing["source"], 9):
                    best[key] = row
    return best


def write_csv(csv_path, rows_by_key):
    """Write all rows sorted by date then repo, replacing the file."""
    sorted_rows = [v for _, v in sorted(rows_by_key.items())]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(sorted_rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("Error: GITHUB_TOKEN not set")
        sys.exit(1)

    today = date.today().isoformat()

    # Load and deduplicate existing history
    rows_by_key = load_csv(CSV_PATH)

    # Derive last known values, keeping the date each was actually measured so
    # a carried-forward value can be judged against later commits.
    # Anchored to the last row that came from a real artifact. A carried-forward
    # row's own date is the day it was copied, not the day it was measured, so
    # using it would keep resetting the clock and the value could never age.
    last_known = {}
    for (d, repo), row in sorted(rows_by_key.items()):
        if row.get("coverage_pct") in ("", None):
            continue
        if row.get("source") == "fresh":
            last_known[repo] = (float(row["coverage_pct"]), row.get("measured_on", "") or d,
                                row.get("measured_branch", ""))
        elif repo not in last_known:
            # No fresh row on record; carry the value with whatever measurement
            # date it has, and treat a missing one as unknown rather than today.
            last_known[repo] = (float(row["coverage_pct"]), row.get("measured_on", ""),
                                row.get("measured_branch", ""))

    # Which repos already have a fresh entry for today — skip them on re-run
    already_fresh = {
        repo for (d, repo), row in rows_by_key.items()
        if d == today and row.get("source") == "fresh"
    }

    new_count = 0
    for repo in REPOSITORIES:
        short = repo.split("/")[-1]

        if repo in already_fresh:
            print(f"  {short}... skipped (already fresh today)")
            continue

        print(f"  {short}... ", end="", flush=True)
        pushed_at, default_branch = fetch_repo_meta(repo, token)
        result = fetch_coverage(repo, token, default_branch)

        if result is not None:
            pct, measured_on, measured_branch = result
            source = "fresh"
            note = "" if measured_branch == default_branch else f" [{measured_branch}]"
            print(f"{pct:.1f}%{note}")
        elif repo in last_known:
            pct, measured_on, measured_branch = last_known[repo]
            # A value measured after the last push still describes the current
            # code: nothing has landed that could have moved it. Once a push
            # lands the value describes code that no longer exists, so it is
            # carried but marked, not presented as a measurement.
            if measured_on and pushed_at and pushed_at > measured_on:
                source = "stale"
                print(f"{pct:.1f}% (stale — pushed {pushed_at}, measured {measured_on})")
            else:
                source = "carried_forward"
                print(f"{pct:.1f}% (carried forward, measured {measured_on or 'unknown'})")
        else:
            pct, measured_on, measured_branch = None, "", ""
            source = "unavailable"
            print("N/A")

        rows_by_key[(today, repo)] = {
            "date": today,
            "repo": repo,
            "coverage_pct": f"{pct:.2f}" if pct is not None else "",
            "source": source,
            "repo_pushed_at": pushed_at,
            "measured_on": measured_on,
            "measured_branch": measured_branch,
        }
        new_count += 1

    write_csv(CSV_PATH, rows_by_key)
    print(f"\nWrote {len(rows_by_key)} total rows ({new_count} updated today)")


if __name__ == "__main__":
    main()
