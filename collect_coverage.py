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
]

# Repos that produce coverage-summary.json (Istanbul/Vitest json-summary reporter)
# Parsed as JSON rather than HTML — accurate and unambiguous.
ISTANBUL_REPOS = {
    "LegalSifter/ls-review-fe",
    "LegalSifter/control-bff",
}

JACOCO_REPOS = {
    "LegalSifter/ms-auth",
    "LegalSifter/ms-profile",
    "LegalSifter/ms-project",
    "LegalSifter/ms-storage",
}

GITHUB_API = "https://api.github.com"
CSV_PATH = os.path.join(os.path.dirname(__file__), "coverage-history.csv")
CSV_COLUMNS = ["date", "repo", "coverage_pct", "source", "repo_pushed_at"]


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


def fetch_repo_pushed_at(repo, token):
    """Return the date the repo was last pushed to (YYYY-MM-DD), or empty string."""
    try:
        data = github_get(f"/repos/{repo}", token)
        pushed = data.get("pushed_at", "")
        return pushed[:10] if pushed else ""
    except Exception:
        return ""


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


def find_coverage_artifact(artifacts):
    for a in artifacts:
        name = a.get("name", "").lower()
        if "unit" in name and ("test" in name or "report" in name or "coverage" in name):
            return a
        if "coverage" in name:
            return a
    for a in artifacts:
        name = a.get("name", "").lower()
        if "test" in name or "report" in name:
            return a
    return None


# ---------------------------------------------------------------------------
# Per-repo fetching (walks back through runs until a live artifact is found)
# ---------------------------------------------------------------------------

def fetch_coverage(repo, token):
    page = 1
    while page <= 10:
        try:
            data = github_get(f"/repos/{repo}/actions/runs?status=success&per_page=10&page={page}", token)
        except Exception as e:
            print(f"  API error: {e}")
            return None

        runs = data.get("workflow_runs", [])
        if not runs:
            break

        for run in runs:
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
                    return pct
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

SOURCE_RANK = {"fresh": 0, "carried_forward": 1, "unavailable": 2, "": 3}


def load_csv(csv_path):
    """Load all rows and deduplicate: one row per (date, repo), best source wins."""
    if not os.path.exists(csv_path):
        return {}

    # key: (date, repo) → best row seen so far
    best = {}
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            row.setdefault("repo_pushed_at", "")  # back-fill missing column
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

    # Derive last known values from historical fresh/carried_forward entries
    last_known = {}
    for (d, repo), row in rows_by_key.items():
        if row.get("coverage_pct") not in ("", None):
            last_known[repo] = float(row["coverage_pct"])

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
        pct = fetch_coverage(repo, token)

        if pct is not None:
            source = "fresh"
            print(f"{pct:.1f}%")
        elif repo in last_known:
            pct = last_known[repo]
            source = "carried_forward"
            print(f"{pct:.1f}% (carried forward)")
        else:
            source = "unavailable"
            print("N/A")

        pushed_at = fetch_repo_pushed_at(repo, token)
        rows_by_key[(today, repo)] = {
            "date": today,
            "repo": repo,
            "coverage_pct": f"{pct:.2f}" if pct is not None else "",
            "source": source,
            "repo_pushed_at": pushed_at,
        }
        new_count += 1

    write_csv(CSV_PATH, rows_by_key)
    print(f"\nWrote {len(rows_by_key)} total rows ({new_count} updated today)")


if __name__ == "__main__":
    main()
