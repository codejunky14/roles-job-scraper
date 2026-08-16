"""
Job Scraper: Google Jobs → Google Sheets
-----------------------------------------
Searches Google Jobs via SerpApi and writes results to a Google Sheet.
New jobs are appended; duplicates (by job ID) are skipped automatically.

SETUP INSTRUCTIONS:
1. pip install serpapi google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client
2. Replace SERPAPI_KEY with your SerpApi key (regenerate at serpapi.com if exposed)
3. Set up Google Sheets API:
   a. Go to https://console.cloud.google.com/
   b. Create a project → Enable "Google Sheets API" and "Google Drive API"
   c. Create credentials → OAuth 2.0 Client ID → Desktop App
   d. Download the JSON file and rename it to credentials.json in this folder
4. Create a Google Sheet and paste its ID into SPREADSHEET_ID below
   (the ID is in the URL: docs.google.com/spreadsheets/d/<ID>/edit)
5. Share the Google Sheet with your Google account (joel.montes10@gmail.com)
6. Run: python job_scraper.py
"""

import os
import json
import time
from datetime import datetime
import requests  # built-in, no install needed
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ─── CONFIGURATION ────────────────────────────────────────────────────────────

SERPAPI_KEY = os.getenv("SERPAPI_KEY")

SPREADSHEET_ID = "1rBD0T5t5bdMpW24d7wRp3kWv02GBUSi5jao8prFs8wg"
SHEET_NAME = "Sheet1"  # Default Google Sheets tab name

# Job search queries — tailored to your background
SEARCH_QUERIES = [
    "Kyriba Consultant",
    "Treasury Management Systems Los Angeles",
    "Treasury IT Applications Analyst",
    "Kyriba Implementation Consultant",
    "Treasury Technology Lead",
    "Treasury Systems Manager",
    "Treasury analyst",
    "Deloitte Treasury Consultant",
]

LOCATION = "Los Angeles, California"  # Filter by location (leave "" for remote/any)

# ─── GOOGLE SHEETS SETUP ──────────────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TOKEN_FILE = "token.json"
CREDENTIALS_FILE = "credentials.json"

HEADERS = [
    "Job ID", "Title", "Company", "Location", "Date Posted",
    "Salary", "Job Type", "Description (Preview)", "Apply Link",
    "Search Query", "Date Added"
]


def get_sheets_service():
    """Authenticate and return Google Sheets API service."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())
    return build("sheets", "v4", credentials=creds)


def ensure_header_row(service):
    """Make sure the sheet has a header row. If empty, add one."""
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A1:K1"
    ).execute()
    values = result.get("values", [])
    if not values:
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{SHEET_NAME}!A1",
            valueInputOption="RAW",
            body={"values": [HEADERS]}
        ).execute()
        print("✅ Header row added to sheet.")


def get_existing_job_ids(service):
    """Pull all existing Job IDs from column A to avoid duplicates."""
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A2:A"
    ).execute()
    values = result.get("values", [])
    return {row[0] for row in values if row}


def append_jobs(service, rows):
    """Append new job rows to the sheet."""
    if not rows:
        return
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows}
    ).execute()


# ─── JOB SCRAPING ─────────────────────────────────────────────────────────────

def scrape_google_jobs(query, location):
    """Search Google Jobs via SerpApi and return list of job dicts."""
    params = {
        "engine": "google_jobs",
        "q": query,
        "api_key": SERPAPI_KEY,
        "hl": "en",
        "gl": "us",
    }
    if location:
        params["location"] = location

    response = requests.get("https://serpapi.com/search", params=params)
    response.raise_for_status()
    results = response.json()
    return results.get("jobs_results", [])


def parse_job(job, query):
    """Extract relevant fields from a SerpApi job result."""
    job_id = job.get("job_id", "")
    title = job.get("title", "")
    company = job.get("company_name", "")
    location = job.get("location", "")
    description = job.get("description", "")[:300].replace("\n", " ")  # Preview only

    # Date posted
    detected_extensions = job.get("detected_extensions", {})
    date_posted = detected_extensions.get("posted_at", "")
    salary = detected_extensions.get("salary", "")
    job_type = detected_extensions.get("schedule_type", "")

    # Apply link — first available option
    apply_link = ""
    apply_options = job.get("apply_options", [])
    if apply_options:
        apply_link = apply_options[0].get("link", "")

    date_added = datetime.now().strftime("%Y-%m-%d %H:%M")

    return [
        job_id, title, company, location, date_posted,
        salary, job_type, description, apply_link,
        query, date_added
    ]


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def update_last_refreshed(service, total_new):
    """Write a Last Updated timestamp to columns M-N in the sheet."""
    timestamp = datetime.now().strftime("%Y-%m-%d %I:%M %p")
    summary = [
        ["Last Refreshed:", timestamp],
        ["New Jobs Added:", total_new],
    ]
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!M1:N2",
        valueInputOption="USER_ENTERED",
        body={"values": summary}
    ).execute()
    print(f"🕒 Last Updated timestamp written: {timestamp}")


def main():
    print("🔄 Connecting to Google Sheets...")
    service = get_sheets_service()
    ensure_header_row(service)
    existing_ids = get_existing_job_ids(service)
    print(f"📋 Found {len(existing_ids)} existing jobs in sheet.")

    total_new = 0

    for query in SEARCH_QUERIES:
        print(f"\n🔍 Searching: '{query}'...")
        try:
            jobs = scrape_google_jobs(query, LOCATION)
            print(f"   Found {len(jobs)} results.")

            new_rows = []
            for job in jobs:
                job_id = job.get("job_id", "")
                if job_id and job_id in existing_ids:
                    continue  # Skip duplicate
                row = parse_job(job, query)
                new_rows.append(row)
                existing_ids.add(job_id)  # Track in memory too

            if new_rows:
                append_jobs(service, new_rows)
                print(f"   ✅ Added {len(new_rows)} new jobs.")
                total_new += len(new_rows)
            else:
                print("   ℹ️  No new jobs found.")

            time.sleep(2)  # Be polite to the API

        except Exception as e:
            print(f"   ❌ Error on query '{query}': {e}")

    update_last_refreshed(service, total_new)
    print(f"\n🎉 Done! {total_new} new jobs added to your Google Sheet.")
    print(f"   View at: https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit")


if __name__ == "__main__":
    main()
