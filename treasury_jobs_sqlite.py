"""
Treasury tech job scraper -> SQLite

Replaces the Google Sheets writer with a local SQLite database.
Key benefits over the sheet:
  - Natural dedupe: re-running never creates duplicate rows
  - first_seen / last_seen: tells you how long a posting has been live
  - status / notes columns survive re-runs, so your pipeline tracking is safe
  - Real SQL for filtering instead of manual sorting

Requires: pip install requests
(SerpAPI key in env var SERPAPI_KEY -- swap fetch_google_jobs() for whatever
 source you're already using; everything downstream is source-agnostic.)
"""

import os
import re
import sqlite3
import hashlib
from datetime import datetime, timezone

import requests

DB_PATH = os.getenv("JOBS_DB", "treasury_jobs.db")
SERPAPI_KEY = os.getenv("SERPAPI_KEY")

SEARCHES = [
    ("Kyriba Support Account Manager", "United States"),
    ("Kyriba consultant",              "United States"),
    ("Treasury Management System",     "United States"),
    ("Treasury IT analyst",            "United States"),
    ("Deloitte Treasury technology",   "United States"),
    ("Treasury Systems Manager",       "Los Angeles, CA"),
]


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_key      TEXT PRIMARY KEY,      -- stable hash of title+company+location
    title        TEXT NOT NULL,
    company      TEXT,
    location     TEXT,
    via          TEXT,                  -- job board the listing came through
    posted_at    TEXT,                  -- raw "3 days ago" style text from source
    schedule     TEXT,                  -- Full-time / Contract / etc.
    salary       TEXT,
    description  TEXT,
    apply_url    TEXT,
    search_term  TEXT,                  -- which query surfaced it
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    times_seen   INTEGER NOT NULL DEFAULT 1,
    -- your columns; never overwritten by a re-run
    status       TEXT NOT NULL DEFAULT 'new',   -- new/reviewing/applied/interviewing/closed
    priority     INTEGER,
    notes        TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status   ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_lastseen ON jobs(last_seen);
CREATE INDEX IF NOT EXISTS idx_jobs_company  ON jobs(company);

CREATE TABLE IF NOT EXISTS runs (
    run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at     TEXT NOT NULL,
    search     TEXT,
    found      INTEGER,
    inserted   INTEGER,
    updated    INTEGER
);
"""


def connect(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    return conn


# --------------------------------------------------------------------------
# Dedupe key
# --------------------------------------------------------------------------

def normalize(s):
    """Lowercase, strip punctuation/whitespace so trivial differences collapse."""
    if not s:
        return ""
    s = re.sub(r"[^a-z0-9 ]", "", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def make_key(title, company, location):
    """
    Google Jobs' own job_id is not stable across runs, so hash the fields that
    actually identify a posting. Location is truncated to city-level to avoid
    'Remote in Los Angeles, CA' vs 'Los Angeles, CA' splitting into two rows.
    """
    city = normalize(location).replace("remote in ", "")
    raw = f"{normalize(title)}|{normalize(company)}|{city}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Source: SerpAPI Google Jobs
# --------------------------------------------------------------------------

def fetch_google_jobs(query, location, pages=2):
    """Yields normalized dicts. Swap this out if you're using a different source."""
    for page in range(pages):
        params = {
            "engine": "google_jobs",
            "q": query,
            "location": location,
            "hl": "en",
            "api_key": SERPAPI_KEY,
        }
        if page:
            params["start"] = page * 10

        r = requests.get("https://serpapi.com/search", params=params, timeout=30)
        r.raise_for_status()
        results = r.json().get("jobs_results", [])
        if not results:
            break

        for j in results:
            detected = j.get("detected_extensions", {}) or {}
            apply_url = ""
            opts = j.get("apply_options") or []
            if opts:
                apply_url = opts[0].get("link", "")

            yield {
                "title":       j.get("title", ""),
                "company":     j.get("company_name", ""),
                "location":    j.get("location", ""),
                "via":         (j.get("via") or "").replace("via ", ""),
                "posted_at":   detected.get("posted_at", ""),
                "schedule":    detected.get("schedule_type", ""),
                "salary":      detected.get("salary", ""),
                "description": (j.get("description") or "")[:5000],
                "apply_url":   apply_url,
                "search_term": query,
            }


# --------------------------------------------------------------------------
# Upsert
# --------------------------------------------------------------------------

UPSERT = """
INSERT INTO jobs (
    job_key, title, company, location, via, posted_at, schedule, salary,
    description, apply_url, search_term, first_seen, last_seen, times_seen
) VALUES (
    :job_key, :title, :company, :location, :via, :posted_at, :schedule, :salary,
    :description, :apply_url, :search_term, :now, :now, 1
)
ON CONFLICT(job_key) DO UPDATE SET
    last_seen   = :now,
    times_seen  = times_seen + 1,
    salary      = COALESCE(NULLIF(excluded.salary, ''), jobs.salary),
    apply_url   = COALESCE(NULLIF(excluded.apply_url, ''), jobs.apply_url),
    posted_at   = excluded.posted_at
;
"""
# Note what is deliberately absent from the UPDATE clause: status, priority,
# notes, first_seen. Your annotations are never clobbered by a scrape.


def upsert_job(conn, job):
    job = dict(job)
    job["job_key"] = make_key(job["title"], job["company"], job["location"])
    job["now"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    before = conn.total_changes
    cur = conn.execute(UPSERT, job)
    # rowid is only meaningful for a fresh insert; compare times_seen instead
    row = conn.execute(
        "SELECT times_seen FROM jobs WHERE job_key = ?", (job["job_key"],)
    ).fetchone()
    return "inserted" if row["times_seen"] == 1 else "updated"


def run(searches=SEARCHES, db_path=DB_PATH):
    conn = connect(db_path)
    grand_new = 0

    for query, location in searches:
        found = ins = upd = 0
        try:
            for job in fetch_google_jobs(query, location):
                found += 1
                if upsert_job(conn, job) == "inserted":
                    ins += 1
                else:
                    upd += 1
        except requests.RequestException as e:
            print(f"  ! {query}: request failed -- {e}")
            continue

        conn.execute(
            "INSERT INTO runs (ran_at, search, found, inserted, updated) "
            "VALUES (?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"),
             f"{query} @ {location}", found, ins, upd),
        )
        conn.commit()
        grand_new += ins
        print(f"  {query:<38} found {found:>3}  new {ins:>3}  seen-again {upd:>3}")

    print(f"\n{grand_new} new postings added -> {db_path}")
    show_new(conn)
    conn.close()


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def show_new(conn, limit=25):
    rows = conn.execute("""
        SELECT title, company, location, salary, via
        FROM jobs
        WHERE status = 'new'
        ORDER BY first_seen DESC
        LIMIT ?
    """, (limit,)).fetchall()

    if not rows:
        print("\nNothing new since last run.")
        return

    print(f"\n--- Untriaged ({len(rows)}) ---")
    for r in rows:
        pay = f"  [{r['salary']}]" if r["salary"] else ""
        print(f"  {r['title'][:52]:<52} | {(r['company'] or '')[:24]:<24}"
              f" | {(r['location'] or '')[:20]:<20}{pay}")


def export_csv(conn, path="jobs_export.csv", where="status = 'new'"):
    """Still want a sheet? Export on demand rather than writing to one live."""
    import csv
    rows = conn.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY first_seen DESC")
    cols = [d[0] for d in rows.description]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    print(f"exported -> {path}")


if __name__ == "__main__":
    if not SERPAPI_KEY:
        raise SystemExit("Set SERPAPI_KEY in your environment first.")
    run()
