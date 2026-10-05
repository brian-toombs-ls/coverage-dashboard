# Coverage Dashboard — Ideation & Planning

## Repos to Add

### Added
- `LegalSifter/ReviewPro-GoogleDocsAddIn` — **done**. `ci.yml` runs on push to `develop` plus a 06:00 UTC nightly cron, so coverage regenerates daily on the default branch. Publishes two Istanbul artifacts, `server-coverage` (Jest) and `client-coverage` (Vitest); the collector combines them line-weighted via `MULTI_ARTIFACT_REPOS`. Currently 90.46% (server 2516/2725, client 3071/3451).
- `LegalSifter/reviewpro-onlyoffice` — **collector side done, blocked on CI**. Listed in `REPOSITORIES`/`ISTANBUL_REPOS` and reports `unavailable` until a coverage artifact exists. Shared `ci-actions-ls/node.yml@main` runs `yarn ci`, which has no `--coverage`. Needs `.github/workflows/coverage.yml` (drafted, modeled on `ls-review-fe/coverage.yml`). Locally the suite is 145 files / 1629 tests at **82.4% lines** (9678/11744).

### Ready to add now (Istanbul json-summary — no collector changes needed)
- `LegalSifter/CLX-ClientApp` — Jest with `--coverageReporters=json-summary`, uploads via `legalsifter/qa-publish-artifacts@v1`. Add to `REPOSITORIES` and `ISTANBUL_REPOS`.

### Needs new parser (Python coverage.xml)
- `LegalSifter/ls-pipeline_refactored-python` — uses `coverage run` + `coverage xml`. Would need a `coverage.xml` parser added to the collector alongside the existing HTML parsers.

### Unconfirmed — likely use shared CI, worth checking
- `LegalSifter/control-fe`
- `LegalSifter/api-notifications`
- `LegalSifter/ak-ml-data-py`

### Blocked on shared CI
- `LegalSifter/ms-project` — Java/JaCoCo. Reports `unavailable` and never has: no run in its
  history uploads any artifact, because shared `ci_actions/.github/workflows/java.yml@develop`
  has no report-upload step (the Go workflow in that same repo does). Fixing it needs a PR
  against `ci_actions` — shared infra touching every Java service — then a `workflow_dispatch`
  here. Weigh that against the repo being dormant since 2025-12-09; if it is decommissioned,
  drop it from `REPOSITORIES` instead.

### No test infrastructure (skip)
- `LegalSifter/ReviewPro-iOS` — Swift, no coverage artifacts
- `LegalSifter/reviewpro-libreoffice`
- `LegalSifter/lsift`
- `LegalSifter/ls-ocr`

---

## Pending PRs (coverage-related)
- **go-microservices #230** — **CLOSED unmerged.** Its PR-branch artifact (`retention-days: 30`) has expired, so go-microservices no longer reports; the dashboard's 83.3% is an August value being carried forward. Note the Test workflow triggers only on `pull_request`/`workflow_dispatch`, so even merging #230 would never produce a default-branch measurement — it needs a `push: develop` trigger too.
- **reviewpro-onlyoffice** — needs a PR adding `.github/workflows/coverage.yml`. Not yet opened.
- **control-bff #473** — open
- **ls-review-fe #1092** — open

---

## Collector Improvements

### Reliability
- **go-microservices**: collector walks all successful runs (not just the Test workflow) — 5 workflows × N PRs means the artifact run can end up on page 2+. Works now but could add workflow name filtering to go straight to Test runs.
- Consider filtering `workflow_runs` by `workflow_id` for repos where we know the exact workflow name, to avoid scanning Lint/Build/Security/Sigrid runs with no artifacts.

### New formats
- Go `coverage.out` profile parser — **done**. `api-settings`, `automator` and `go-mail` publish
  a `Unit Test Reports` artifact whose HTML is raw `go tool cover -html` output: per-file
  percentages in a dropdown, no total anywhere, so `extract_gocov` found nothing and all three
  recorded `unavailable`. The artifacts also carry `coverage.out`, which is read instead and
  summed to exact statement coverage. Repos whose artifact has a gocov-html report
  (`ms-billing`, `api-documents`, `ms-chat`) carry no `coverage.out` and are unaffected.
- Python `coverage.xml` parser for `ls-pipeline_refactored-python` — parse `<coverage line-rate="0.83">` attribute, multiply by 100.

---

## Dashboard UI Ideas

### Done
- Grafana-inspired flat dark theme with light mode toggle (persisted in localStorage)
- Service pod grid replacing separate "Needs Attention" + detail table
- Sparklines with tight Y-axis zoom (sub-1% fluctuations visible)
- 30-day delta indicator (↑↓→) with fallback to earliest available data
- Status color only on top border + faded meter bar; everything else neutral
- Coverage meter 0–100 with 80% target tick mark
- Source tag (fresh / carried forward / unavailable)
- Push recency chip on pod
- Coverage Changes section (biggest 30-day movers)

### Ideas to explore
- **Target line configurability** — currently hardcoded at 80%. Could make per-repo targets configurable (some teams have different thresholds).
- **Trend summary callout** — "3 services improved this week, 1 declined" as a header stat.
- **Email/Slack digest** — weekly summary of coverage movement, auto-sent from the cron job.
- **Per-repo drill-down** — clicking a pod could show a full historical chart for that service.
- **Badge generation** — SVG badges per service that could be embedded in repo READMEs.
- **Alert on regression** — collector could post a Slack message when any service drops more than 2% in a single run.

---

## Data Notes
- CSV columns: `date, repo, coverage_pct, source, repo_pushed_at`
- Sources: `fresh` (live artifact), `carried_forward` (last known), `unavailable` (no data ever)
- Deduplication: one row per (date, repo), best source wins on re-run
- Retention: artifacts expire after 30 days — collector walks back up to 100 runs to find a live one
