"""
Google Calendar -> Notion "Calendar" sync.
Runs on GitHub Actions (free). No servers, no subscription.

Reads Google Calendar "secret iCal" URLs, expands recurring events inside a
rolling window, and upserts them into a Notion database. Rows you add by hand
(no UID) are never touched. Any property you remove from the database (e.g.
"Calendar" or "Type") is simply skipped, so a schema change never breaks it.

Required environment variables (set as GitHub repository secrets):
  NOTION_TOKEN  - your Notion internal integration secret
  NOTION_DB_ID  - the Calendar database id
  ICS_URLS      - comma-separated sources, each "Label|https://...ics"
"""

import os
import sys
import datetime as dt
import requests
import icalendar
import recurring_ical_events

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DB_ID = os.environ["NOTION_DB_ID"]
ICS_URLS = os.environ["ICS_URLS"]

API = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# Rolling window: keep 1 week of history + 4 weeks ahead.
TODAY = dt.date.today()
WINDOW_START = TODAY - dt.timedelta(days=7)
WINDOW_END = TODAY + dt.timedelta(days=28)

VALID_CAL_LABELS = {"Personal", "ArabSoc"}


def parse_sources(raw):
    sources = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "|" in part:
            label, url = part.split("|", 1)
        else:
            label, url = "Personal", part
        sources.append((label.strip(), url.strip()))
    return sources


def iso(value):
    return value.isoformat()


def db_property_names():
    """Property names that currently exist on the database, so we never send a
    property the user has since deleted (which would 400 the whole request)."""
    r = requests.get(f"{API}/databases/{DB_ID}", headers=HEADERS)
    r.raise_for_status()
    return set(r.json().get("properties", {}).keys())


def fetch_events():
    events = {}
    for label, url in parse_sources(ICS_URLS):
        try:
            resp = requests.get(url, timeout=45)
            resp.raise_for_status()
            cal = icalendar.Calendar.from_ical(resp.text)
            for ev in recurring_ical_events.of(cal).between(WINDOW_START, WINDOW_END):
                start = ev.get("DTSTART").dt
                uid = str(ev.get("UID", ""))
                key = f"{uid}|{iso(start)}"
                events[key] = {
                    "summary": str(ev.get("SUMMARY") or "(no title)"),
                    "start": iso(start),
                    "location": str(ev.get("LOCATION") or ""),
                    "label": label,
                }
        except Exception as exc:
            print(f"[warn] {label}: {exc}", file=sys.stderr)
    return events


def notion_existing():
    rows, cursor = {}, None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        r = requests.post(f"{API}/databases/{DB_ID}/query", headers=HEADERS, json=payload)
        r.raise_for_status()
        data = r.json()
        for page in data["results"]:
            uid_rt = page["properties"].get("UID", {}).get("rich_text", [])
            uid = uid_rt[0]["plain_text"] if uid_rt else None
            if uid:
                rows[uid] = page["id"]
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    return rows


def props_for(key, ev, valid):
    props = {
        "Session": {"title": [{"text": {"content": ev["summary"][:2000]}}]},
        "When": {"date": {"start": ev["start"]}},
        "Location": {"rich_text": ([{"text": {"content": ev["location"][:2000]}}] if ev["location"] else [])},
        "UID": {"rich_text": [{"text": {"content": key[:2000]}}]},
    }
    if ev["label"] in VALID_CAL_LABELS:
        props["Calendar"] = {"select": {"name": ev["label"]}}
    return {k: v for k, v in props.items() if k in valid}


def main():
    valid = db_property_names()
    events = fetch_events()
    existing = notion_existing()
    seen = set()

    for key, ev in events.items():
        seen.add(key)
        body = {"properties": props_for(key, ev, valid)}
        if key in existing:
            requests.patch(f"{API}/pages/{existing[key]}", headers=HEADERS, json=body).raise_for_status()
        else:
            body["parent"] = {"database_id": DB_ID}
            requests.post(f"{API}/pages", headers=HEADERS, json=body).raise_for_status()

    for key, page_id in existing.items():
        if key not in seen:
            requests.patch(f"{API}/pages/{page_id}", headers=HEADERS, json={"archived": True}).raise_for_status()

    print(f"Synced {len(events)} events; cleaned {len(existing) - len(seen & set(existing))} stale rows.")


if __name__ == "__main__":
    main()
