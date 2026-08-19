# Treasury Technology Job Scraper

A Python pipeline that scrapes treasury-technology job postings from Google Jobs (via SerpApi) and maintains them in a local SQLite database with automatic deduplication, first-seen / last-seen tracking, and a pipeline-status layer that survives re-runs.

Built to track a focused set of roles — Kyriba Support Account Manager, Treasury Management Systems, Treasury IT, and Deloitte Treasury practice positions — but the search terms are trivially configurable for any target role.

---

## Why it exists

Job boards surface the same posting repeatedly, drop listings without notice, and give you no memory of what you've already reviewed. This tool turns a recurring manual search into a queryable dataset: run it on a schedule, and it tells you what's *new*, what's *still live*, and what's *disappeared* — while preserving your own annotations (applied, reviewing, priority, notes) across every run.

## Architecture

```
SerpApi (Google Jobs)  ──►  normalize + SHA1 dedup key  ──►  SQLite UPSERT  ──►  queryable pipeline
```

The storage layer is the heart of the design. Three decisions drive it:

**Stable deduplication.** SerpApi's own `job_id` is not stable across runs, so it can't serve as a primary key. Instead, each posting is keyed on a SHA1 hash of normalized *title + company + city*. Re-running the scraper never creates duplicate rows — the same posting seen across ten weekly runs stays a single record.

**First-seen / last-seen / times-seen.** Every record tracks when it first appeared, when it was last seen, and how many runs have surfaced it. A posting that keeps reappearing for weeks is a signal (hard-to-fill role, or a perpetual req); a posting whose `last_seen` goes stale has likely been filled or pulled.

**Annotations survive re-runs.** The `ON CONFLICT` upsert refreshes `last_seen`, `times_seen`, and salary — but deliberately never touches `status`, `priority`, `notes`, or `first_seen`. You can mark a role "applied" and re-scrape without losing your work. This is the behavior a spreadsheet made painful.

## From Google Sheets to SQLite

The original implementation ([`archive/job_scraper_sheets.py`](archive/job_scraper_sheets.py)) wrote results to a Google Sheet. That worked, but it couldn't deduplicate cleanly, had no concept of a posting's lifecycle, and put manual annotations at risk on every refresh.

Migrating the storage layer to SQLite resolved all three: natural dedup via a primary key, lifecycle tracking via timestamp columns, and durable pipeline state via a non-overwritten status column — plus real SQL for filtering instead of manual sorting. The Sheets version is preserved in `archive/` as a record of that migration.

## Project structure

```
job-scraper/
├── treasury_jobs_sqlite.py     # current — SerpApi → SQLite
├── archive/
│   └── job_scraper_sheets.py   # original — SerpApi → Google Sheets
└── .gitignore
```

## Setup

Requires Python 3.9+ and a [SerpApi](https://serpapi.com) key.

```bash
pip install requests
```

Provide your SerpApi key via environment variable (never hardcoded):

```powershell
# PowerShell — current session
$env:SERPAPI_KEY = "your_key_here"

# or persist for your user account (survives new terminals)
[Environment]::SetEnvironmentVariable("SERPAPI_KEY", "your_key_here", "User")
```

## Usage

```bash
python treasury_jobs_sqlite.py
```

Each run scrapes the configured searches, upserts results into `treasury_jobs.db`, logs a per-search summary (found / new / seen-again), and prints untriaged postings. Edit the `SEARCHES` list near the top of the script to change target roles or locations.

## Querying the data

The database is standard SQLite — open it in [DB Browser for SQLite](https://sqlitebrowser.org/) for a spreadsheet-style view, or query directly:

```sql
-- Roles that vanished (likely filled or pulled)
SELECT title, company, last_seen FROM jobs
WHERE last_seen < datetime('now','-10 days') AND status = 'new';

-- Companies hiring treasury tech most consistently
SELECT company, COUNT(*) n, MAX(last_seen) FROM jobs
GROUP BY company ORDER BY n DESC LIMIT 20;

-- Kyriba-specific roles still live in the last week
SELECT title, company, location, apply_url FROM jobs
WHERE (title LIKE '%Kyriba%' OR description LIKE '%Kyriba%')
  AND last_seen > datetime('now','-7 days');
```

## Schema

| Column | Purpose |
|---|---|
| `job_key` | SHA1 dedup key (title + company + city) — primary key |
| `title`, `company`, `location`, `via` | Core posting fields |
| `posted_at`, `schedule`, `salary` | Detected extensions from the source |
| `apply_url` | Direct application link |
| `first_seen`, `last_seen`, `times_seen` | Lifecycle tracking |
| `status`, `priority`, `notes` | User-owned pipeline state (never overwritten) |

A companion `runs` table logs each scrape (timestamp, search, counts) for auditing over time.

## Notes on security

API keys and OAuth credentials are read from the environment or ignored files — never committed. The `.gitignore` excludes `.env`, `credentials.json`, `token.json`, and all `*.db` files so that scraped data and secrets stay local to the machine.
