"""Martin-Riley Family Hub: shared database and private, authenticated API."""
import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from calendar import monthrange
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).parent
DB = os.environ.get("HUB_DB", str(ROOT / "data" / "hub.sqlite3"))
HUB_TIMEZONE = os.environ.get("HUB_TIMEZONE", "UTC")
KINDS = {"tasks", "posts", "requests", "events", "lists", "dance", "notes", "recognitions"}
VISIBILITY = {"Family", "Adults", "Assigned", "Me"}
ACCOUNT_ROLES = {"Dad": "ADMIN", "Mom": "ADMIN", "Daughter": "CHILD"}
DEFAULT_DISPLAY_NAMES = {"Dad": "Jermaine", "Mom": "Stephanie", "Daughter": "Arielle"}
TASK_CATEGORIES = {"Home", "Dance", "School", "Bills", "Shopping", "Appointments", "Work", "Other"}
TASK_REPEATS = {"One Time", "Daily", "Weekdays", "Weekly", "Monthly", "Custom"}
TASK_PRIORITIES = {"Normal", "Important", "High Priority", "Urgent"}
TASK_FIELDS = ("title", "description", "who", "priority", "visibility", "category", "dueDate", "dueTime", "repeat", "ack", "customIntervalDays", "reminderOffsets")
TASK_FIELDS += ("chore",)
RECOGNITION_TYPES = {"Great Job", "Nice Work", "Streak Saver", "Above & Beyond", "Proud of You", "Thank You"}
TASK_MISS_REASONS = {"Ran out of time", "Waiting on someone/something", "Rescheduled", "No longer needed", "Other"}
EVENT_CATEGORIES = {"Family", "Appointments", "School", "Dance", "Work", "Birthdays", "Travel", "Competitions", "Other"}
REQUEST_TYPES = {"Permission", "Purchase", "Ride", "Sleepover/Friend", "Schedule Change", "Chore/Task", "Question", "Other"}
REMINDER_OFFSETS = {0, 15, 30, 60, 120, 1440}
RECURRENCE_HORIZON_DAYS = 90
RECURRENCE_MAX_OCCURRENCES = 120
STATIC = {"index.html", "styles.css", "app.js", "manifest.json", "sw.js", "icon.svg", "icon-192.png", "icon-512.png"}
DANCE_FIELDS = (
    "danceType", "routineType", "choreographer", "instructor", "competitionIds", "costumeId",
    "routineIds", "costumeIds", "checklistType",
    "scheduleInformation", "startDate", "endDate", "venue", "address", "hotel", "travel",
    "arrivalTime", "callTime", "performanceSchedule", "schedulePending", "results", "awards",
    "ordered", "received", "alterationsNeeded", "alterationsCompleted", "accessories", "shoes", "deadlines",
    "tights", "neededBy", "checklistItems", "competitionId", "fees", "financials", "notes",
)

def connection():
    Path(DB).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def initialize():
    with connection() as c:
        c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users(name TEXT PRIMARY KEY, salt TEXT NOT NULL, hash TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, name TEXT REFERENCES users(name), expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS records(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, body TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, actor TEXT, record_id INTEGER REFERENCES records(id), action TEXT, created TEXT, snapshot TEXT NOT NULL DEFAULT '{}', details TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS attempts(address TEXT PRIMARY KEY, count INTEGER, reset INTEGER);
        CREATE TABLE IF NOT EXISTS recurrence_series(series_id INTEGER PRIMARY KEY REFERENCES records(id), active INTEGER NOT NULL DEFAULT 1, rule TEXT NOT NULL, start_date TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT '', anchor_sequence INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS recurrence_occurrences(series_id INTEGER NOT NULL REFERENCES recurrence_series(series_id), occurrence_date TEXT NOT NULL, task_id INTEGER NOT NULL UNIQUE REFERENCES records(id), sequence INTEGER NOT NULL, PRIMARY KEY(series_id, occurrence_date));
        CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL REFERENCES users(name), source_kind TEXT NOT NULL, source_id INTEGER NOT NULL, reminder_key TEXT NOT NULL, reminder_type TEXT NOT NULL, due_at TEXT NOT NULL, created_at TEXT NOT NULL, read_at TEXT, dismissed_at TEXT, snoozed_until TEXT, UNIQUE(account, source_kind, source_id, reminder_key));
        CREATE TABLE IF NOT EXISTS recognition_receipts(recognition_id INTEGER NOT NULL REFERENCES records(id), recipient TEXT NOT NULL REFERENCES users(name), seen_at TEXT NOT NULL, PRIMARY KEY(recognition_id, recipient));
        CREATE TABLE IF NOT EXISTS notification_settings(name TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        columns = {row["name"] for row in c.execute("PRAGMA table_info(users)")}
        if "display_name" not in columns:
            c.execute("ALTER TABLE users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
        record_columns = {row["name"] for row in c.execute("PRAGMA table_info(records)")}
        if "deleted" not in record_columns:
            c.execute("ALTER TABLE records ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
        audit_columns = {row["name"] for row in c.execute("PRAGMA table_info(audit)")}
        if "snapshot" not in audit_columns:
            c.execute("ALTER TABLE audit ADD COLUMN snapshot TEXT NOT NULL DEFAULT '{}'")
        if "details" not in audit_columns:
            c.execute("ALTER TABLE audit ADD COLUMN details TEXT NOT NULL DEFAULT '{}'")
        series_columns = {row["name"] for row in c.execute("PRAGMA table_info(recurrence_series)")}
        if "created_by" not in series_columns:
            c.execute("ALTER TABLE recurrence_series ADD COLUMN created_by TEXT NOT NULL DEFAULT ''")
        if "anchor_sequence" not in series_columns:
            c.execute("ALTER TABLE recurrence_series ADD COLUMN anchor_sequence INTEGER NOT NULL DEFAULT 0")
        c.execute("INSERT OR IGNORE INTO notification_settings(name,value) VALUES('audit_notifications_started_at',?)", (stamp(),))
        latest_audit_id = c.execute("SELECT COALESCE(MAX(id),0) FROM audit").fetchone()[0]
        c.execute("INSERT OR IGNORE INTO notification_settings(name,value) VALUES('audit_notification_last_id',?)", (str(latest_audit_id),))
        for account, display_name in DEFAULT_DISPLAY_NAMES.items():
            c.execute("UPDATE users SET display_name=? WHERE name=? AND display_name=''", (display_name, account))
        for row in c.execute("SELECT id,kind,body FROM records WHERE deleted=0 AND kind IN ('tasks','events')").fetchall():
            record = json.loads(row["body"])
            repeat = record.get("repeat", "One Time")
            start_date = record.get("dueDate") if row["kind"] == "tasks" else record.get("date", "")[:10]
            if repeat == "One Time" or not start_date or c.execute("SELECT 1 FROM recurrence_series WHERE series_id=?", (row["id"],)).fetchone():
                continue
            record.update(seriesId=row["id"], occurrenceDate=start_date, occurrenceNumber=0)
            rule = dict(record, seriesKind=row["kind"])
            c.execute("UPDATE records SET body=? WHERE id=?", (json.dumps(record), row["id"]))
            created_at = record.get("createdAt", stamp())
            c.execute("INSERT INTO recurrence_series(series_id,rule,start_date,created_at,updated_at,created_by) VALUES(?,?,?,?,?,?)",
                      (row["id"], json.dumps(rule), start_date, created_at, record.get("updatedAt", created_at), record.get("creator", "")))
            c.execute("INSERT OR IGNORE INTO recurrence_occurrences(series_id,occurrence_date,task_id,sequence) VALUES(?,?,?,0)",
                      (row["id"], start_date, row["id"]))

def profile_data(c):
    return {
        row["name"]: {
            "displayName": row["display_name"] or DEFAULT_DISPLAY_NAMES.get(row["name"], row["name"]),
            "role": ACCOUNT_ROLES.get(row["name"], "CHILD"),
        }
        for row in c.execute("SELECT name, display_name FROM users")
    }

def normalize_dance_record(source, actor, existing=None):
    record = dict(existing or {})
    for key in DANCE_FIELDS + ("title", "text", "category", "date", "time", "checked", "visibility", "who"):
        if key in source:
            record[key] = source[key]
    record.setdefault("visibility", "Family")
    record.setdefault("category", "Other")
    record.setdefault("notes", "")
    dance_type = record.get("danceType")
    if dance_type is None:
        return record
    if dance_type not in {"competition", "routine", "costume", "checklist", "schedule"}:
        raise ValueError("Invalid dance item type")
    if not adult(actor):
        raise PermissionError("Only parents can manage dance details")
    title = record.get("title", record.get("text", ""))
    if not isinstance(title, str) or not title.strip() or len(title) > 4000:
        raise ValueError("Please enter a dance item name")
    record["title"] = title.strip()
    for field in ("notes", "choreographer", "instructor", "scheduleInformation", "venue", "address", "hotel", "travel", "arrivalTime", "callTime", "performanceSchedule", "results", "awards", "accessories", "shoes", "tights"):
        value = record.get(field, "")
        if not isinstance(value, str) or len(value) > 4000:
            raise ValueError("Invalid dance item details")
        record[field] = value
    record["visibility"] = "Family"
    record["who"] = "Daughter"
    if dance_type == "routine" and record.get("routineType", "Solo") not in {"Solo", "Trio", "Group"}:
        raise ValueError("Choose Solo, Trio, or Group")
    if dance_type == "checklist":
        record.setdefault("checklistType", "Competition Packing")
        if record["checklistType"] not in {"Competition Packing", "Practice Bag", "Costume Checklist", "Travel Checklist", "Custom"}:
            raise ValueError("Choose a valid checklist type")
    if dance_type == "competition" and not isinstance(record.get("schedulePending", True), bool):
        raise ValueError("Invalid schedule status")
    for field in ("startDate", "endDate", "neededBy", "date"):
        value = record.get(field, "")
        if value:
            try:
                dt.date.fromisoformat(value)
            except (ValueError, TypeError) as error:
                raise ValueError("Choose a valid dance date") from error
    if dance_type == "competition":
        record.setdefault("schedulePending", True)
        if record.get("startDate") and record.get("endDate") and record["endDate"] < record["startDate"]:
            raise ValueError("Competition end date must be on or after its start date")
    for field in ("ordered", "received", "alterationsNeeded", "alterationsCompleted", "checked"):
        value = record.get(field, False)
        if not isinstance(value, bool):
            raise ValueError("Invalid dance status")
        record[field] = value
    for field in ("competitionIds", "routineIds", "costumeIds"):
        values = record.get(field, [])
        if not isinstance(values, list) or len(values) > 100 or any(type(value) is not int for value in values):
            raise ValueError("Invalid dance associations")
        record[field] = sorted(set(values))
    for field in ("competitionId", "costumeId"):
        value = record.get(field)
        if value is not None and type(value) is not int:
            raise ValueError("Invalid dance association")
    items = record.get("checklistItems", [])
    if not isinstance(items, list) or len(items) > 100:
        raise ValueError("Invalid checklist")
    normalized_items = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            item = {"text": item, "checked": False}
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip() or len(item["text"]) > 500 or not isinstance(item.get("checked", False), bool):
            raise ValueError("Invalid checklist item")
        normalized_items.append({"id": index + 1, "text": item["text"].strip(), "checked": item.get("checked", False)})
    record["checklistItems"] = normalized_items
    fees = record.get("fees", [])
    if not isinstance(fees, list) or len(fees) > 100:
        raise ValueError("Invalid dance fees")
    for fee in fees:
        if not isinstance(fee, dict) or not isinstance(fee.get("label", ""), str) or len(fee.get("label", "")) > 300:
            raise ValueError("Invalid dance fee")
    record["fees"] = fees
    deadlines = record.get("deadlines", [])
    if not isinstance(deadlines, list) or len(deadlines) > 100:
        raise ValueError("Invalid competition deadlines")
    normalized_deadlines = []
    for index, deadline in enumerate(deadlines):
        if not isinstance(deadline, dict):
            raise ValueError("Invalid competition deadline")
        title, date = deadline.get("title", ""), deadline.get("date", "")
        if not isinstance(title, str) or not title.strip() or len(title) > 300:
            raise ValueError("Enter a deadline name")
        try:
            dt.date.fromisoformat(date)
        except (ValueError, TypeError) as error:
            raise ValueError("Choose a valid deadline date") from error
        deadline_id = deadline.get("id", index + 1)
        if not isinstance(deadline_id, (str, int)) or isinstance(deadline_id, bool):
            raise ValueError("Invalid deadline ID")
        completed = deadline.get("completed", False)
        if not isinstance(completed, bool):
            raise ValueError("Invalid competition deadline status")
        normalized_deadlines.append({"id": str(deadline_id), "title": title.strip(), "date": date, "completed": completed})
    record["deadlines"] = normalized_deadlines
    return record

def validate_dance_associations(c, record, actor):
    expected = {
        "competitionIds": "competition", "routineIds": "routine", "costumeIds": "costume",
        "costumeId": "costume", "competitionId": "competition",
    }
    for field, dance_type in expected.items():
        value = record.get(field, [] if field.endswith("Ids") else None)
        ids = value if isinstance(value, list) else [value]
        for record_id in ids:
            if record_id is None:
                continue
            row = c.execute("SELECT kind,body,deleted FROM records WHERE id=?", (record_id,)).fetchone()
            if not row or row["kind"] != "dance" or row["deleted"]:
                raise ValueError("A linked dance item is unavailable")
            linked = json.loads(row["body"])
            if not visible(linked, actor):
                raise ValueError("A linked dance item is unavailable")
            if linked.get("danceType") != dance_type:
                raise ValueError("A linked dance item has the wrong type")

def dance_record_for_viewer(record, viewer):
    result = dict(record)
    if viewer == "Daughter":
        result.pop("fees", None)
        result.pop("financials", None)
    return result

def sync_competition_calendar(c, actor, dance_id, competition):
    if competition.get("danceType") != "competition":
        return
    linked = {}
    duplicate_ids = []
    for row in c.execute("SELECT id,body FROM records WHERE kind='events' AND deleted=0"):
        event = json.loads(row["body"])
        if event.get("sourceDanceId") == dance_id:
            key = event.get("sourceDanceKey", "competition")
            if key in linked:
                duplicate_ids.append(row["id"])
            else:
                linked[key] = (row["id"], event)

    desired = {}
    if competition.get("startDate"):
        span = competition["startDate"]
        if competition.get("endDate") and competition["endDate"] != span:
            span += " to " + competition["endDate"]
        desired["competition"] = {
            "title": competition["title"],
            "description": "Dance competition" + (" · Schedule Pending" if competition.get("schedulePending", True) else "") + " · " + span,
            "date": competition["startDate"], "startTime": competition.get("arrivalTime", ""), "endTime": "",
            "allDay": not bool(competition.get("arrivalTime")), "location": competition.get("venue", ""),
            "who": "Daughter", "people": ["Daughter"], "category": "Competitions",
        }
        if competition.get("endDate") and competition["endDate"] != competition["startDate"]:
            desired["competition:end"] = {
                "title": competition["title"] + " · Final day",
                "description": "Final day of dance competition · " + span,
                "date": competition["endDate"], "startTime": "", "endTime": "", "allDay": True,
                "location": competition.get("venue", ""), "who": "Daughter", "people": ["Daughter"],
                "category": "Competitions",
            }
    for deadline in competition.get("deadlines", []):
        key = "deadline:" + deadline["id"]
        desired[key] = {
            "title": deadline["title"] + " · " + competition["title"],
            "description": "Competition deadline", "date": deadline["date"], "startTime": "", "endTime": "",
            "allDay": True, "location": competition.get("venue", ""), "who": "Daughter",
            "people": ["Daughter"], "category": "Competitions",
        }

    for key, values in desired.items():
        event = {
            **values, "visibility": "Family", "repeat": "One Time", "reminderOffsets": [],
            "sourceDanceId": dance_id, "sourceDanceKey": key, "creator": actor, "by": actor,
            "createdAt": stamp(), "updatedAt": stamp(),
        }
        event = event_fields(event, actor)
        event.update(sourceDanceId=dance_id, sourceDanceKey=key, creator=actor, by=actor,
                 createdAt=event.get("createdAt", stamp()), updatedAt=stamp())
        previous = linked.pop(key, None)
        if previous:
            event["createdAt"] = previous[1].get("createdAt", event["createdAt"])
            event_id = previous[0]
            if any(previous[1].get(field) != event.get(field) for field in ("date", "startTime", "endTime", "reminderOffsets")):
                c.execute("UPDATE reminders SET dismissed_at=COALESCE(dismissed_at,?) WHERE source_kind='events' AND source_id=?",
                          (stamp(), event_id))
            c.execute("UPDATE records SET body=? WHERE id=?", (json.dumps(event), event_id))
        else:
            cursor = c.execute("INSERT INTO records(kind,body) VALUES('events',?)", (json.dumps(event),))
            event_id = cursor.lastrowid
            audit_record(c, actor, event_id, "create", {**event, "id": event_id})

    for record_id, _event in linked.values():
        c.execute("UPDATE records SET deleted=1 WHERE id=?", (record_id,))
    for record_id in duplicate_ids:
        c.execute("UPDATE records SET deleted=1 WHERE id=?", (record_id,))

def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()

def adult(name):
    return name in {"Dad", "Mom"}

def visible(record, name):
    v = record.get("visibility", "Family")
    return (v == "Family" or v == "Adults" and adult(name)
            or v == "Assigned" and record.get("who") == name
            or v == "Me" and record.get("creator") == name)

def editable(record, name):
    return record.get("creator") == name or adult(name) and record.get("visibility") != "Me"

def task_fields(source, actor, existing=None):
    task = dict(existing or {})
    for key in TASK_FIELDS:
        if key in source:
            task[key] = source[key]
    task.setdefault("description", "")
    task.setdefault("who", "Everyone")
    task.setdefault("priority", "Normal")
    task.setdefault("visibility", "Family")
    task.setdefault("category", "Other")
    task.setdefault("dueDate", "")
    task.setdefault("dueTime", "")
    task.setdefault("repeat", "One Time")
    task.setdefault("customIntervalDays", 1)
    task.setdefault("reminderOffsets", [0] if task.get("dueDate") else [])
    task.setdefault("ack", False)
    task.setdefault("chore", task["category"] == "Home" and task["who"] in {"Daughter", "Everyone"})
    if task.get("priority") == "High":
        task["priority"] = "High Priority"
    if not isinstance(task["title"], str) or not task["title"].strip() or len(task["title"]) > 4000:
        raise ValueError("Please enter a task name")
    if not isinstance(task["description"], str) or len(task["description"]) > 4000:
        raise ValueError("Invalid task description")
    if task["who"] not in {"Dad", "Mom", "Daughter", "Everyone", ""}:
        raise ValueError("Invalid assignee")
    if task["visibility"] not in VISIBILITY or task["visibility"] == "Adults" and not adult(actor):
        raise ValueError("Invalid visibility")
    if task["visibility"] == "Assigned" and task["who"] not in {"Dad", "Mom", "Daughter"}:
        raise ValueError("Choose one assigned person")
    if task["priority"] not in TASK_PRIORITIES:
        raise ValueError("Invalid priority")
    if task["category"] not in TASK_CATEGORIES or task["repeat"] not in TASK_REPEATS:
        raise ValueError("Invalid task category or repeat schedule")
    if task["repeat"] != "One Time" and not task["dueDate"]:
        raise ValueError("Recurring tasks need a due date")
    if type(task["customIntervalDays"]) is not int or not 1 <= task["customIntervalDays"] <= 366:
        raise ValueError("Custom recurrence interval must be between 1 and 366 days")
    if not isinstance(task["reminderOffsets"], list) or any(type(offset) is not int or offset not in REMINDER_OFFSETS for offset in task["reminderOffsets"]):
        raise ValueError("Invalid task reminder settings")
    if not isinstance(task["ack"], bool):
        raise ValueError("Invalid acknowledgement setting")
    if not isinstance(task["chore"], bool):
        raise ValueError("Invalid chore setting")
    if task["chore"] and (task["category"] != "Home" or task["who"] not in {"Daughter", "Everyone"}):
        task["chore"] = False
    for key, pattern in (("dueDate", "%Y-%m-%d"), ("dueTime", "%H:%M")):
        if not isinstance(task[key], str) or len(task[key]) > 32:
            raise ValueError("Invalid task due date or time")
        if task[key]:
            try:
                dt.datetime.strptime(task[key], pattern)
            except ValueError as error:
                raise ValueError("Invalid task due date or time") from error
    task["ack"] = task["priority"] == "Urgent" or task["ack"]
    return task

def qualifies_as_chore(task):
    return (task.get("category") == "Home" and task.get("who") in {"Daughter", "Everyone"}
            and task.get("chore", True) is True)

def reminder_offsets(source):
    offsets = source.get("reminderOffsets", [0])
    if not isinstance(offsets, list) or any(type(offset) is not int or offset not in REMINDER_OFFSETS for offset in offsets):
        raise ValueError("Invalid reminder settings")
    return sorted(set(offsets))

def event_fields(source, actor, existing=None):
    event = dict(existing or {})
    allowed = ("title", "description", "category", "date", "startTime", "endTime", "allDay", "location", "who", "people", "visibility", "repeat", "customIntervalDays", "reminderOffsets")
    for key in allowed:
        if key in source:
            event[key] = source[key]
    if "date" not in event and isinstance(event.get("startDate"), str):
        event["date"] = event["startDate"]
    legacy_date = event.get("date", "")
    if isinstance(legacy_date, str) and len(legacy_date) > 10 and "T" in legacy_date:
        event["date"] = legacy_date[:10]
        event.setdefault("startTime", legacy_date[11:16])
    event.setdefault("description", "")
    event.setdefault("category", "Family")
    event.setdefault("startTime", "")
    event.setdefault("endTime", "")
    event.setdefault("allDay", not bool(event["startTime"]))
    event.setdefault("location", "")
    event.setdefault("who", "Everyone")
    event.setdefault("people", [event["who"]] if event["who"] in ACCOUNT_ROLES else [])
    event.setdefault("visibility", "Family")
    event.setdefault("repeat", "One Time")
    event.setdefault("customIntervalDays", 1)
    event["reminderOffsets"] = reminder_offsets(event)
    event["timezone"] = HUB_TIMEZONE
    for key in ("title", "description", "location"):
        if not isinstance(event.get(key, ""), str) or len(event.get(key, "")) > 4000:
            raise ValueError("Invalid event text")
    if not event.get("title", "").strip():
        raise ValueError("Please enter an event name")
    try:
        dt.date.fromisoformat(event["date"])
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError("Choose a valid event date") from error
    if not isinstance(event["allDay"], bool):
        raise ValueError("Invalid all-day setting")
    for field in ("startTime", "endTime"):
        if event[field]:
            try:
                dt.datetime.strptime(event[field], "%H:%M")
            except (ValueError, TypeError) as error:
                raise ValueError("Choose a valid event time") from error
    if event["startTime"] and event["endTime"] and event["endTime"] <= event["startTime"]:
        raise ValueError("Event end must be after its start")
    if event["category"] not in EVENT_CATEGORIES:
        raise ValueError("Invalid event category")
    if event["visibility"] not in VISIBILITY or event["visibility"] == "Adults" and not adult(actor):
        raise ValueError("Invalid event visibility")
    if event["who"] not in {"Dad", "Mom", "Daughter", "Everyone", ""}:
        raise ValueError("Invalid involved family member")
    if event["visibility"] == "Assigned" and event["who"] not in ACCOUNT_ROLES:
        raise ValueError("Assigned events need a family member")
    if not isinstance(event["people"], list) or any(person not in ACCOUNT_ROLES for person in event["people"]):
        raise ValueError("Invalid event participants")
    if event["repeat"] not in TASK_REPEATS:
        raise ValueError("Invalid event recurrence")
    if event["repeat"] != "One Time" and not event["allDay"] and not event["startTime"]:
        raise ValueError("Recurring timed events need a start time")
    if event["repeat"] != "One Time" and not 1 <= event["customIntervalDays"] <= 366:
        raise ValueError("Custom recurrence interval must be between 1 and 366 days")
    return event

def family_timezone():
    try:
        return ZoneInfo(HUB_TIMEZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")

def local_today():
    return dt.datetime.now(family_timezone()).date()

def recurrence_date(start_date, repeat, sequence, interval=1):
    if repeat == "Daily":
        return start_date + dt.timedelta(days=sequence)
    if repeat == "Weekdays":
        result = start_date
        count = 0
        direction = 1 if sequence >= 0 else -1
        while count < abs(sequence):
            result += dt.timedelta(days=direction)
            if result.weekday() < 5:
                count += 1
        return result
    if repeat == "Weekly":
        return start_date + dt.timedelta(days=7 * sequence)
    if repeat == "Custom":
        return start_date + dt.timedelta(days=interval * sequence)
    if repeat == "Monthly":
        month_index = start_date.year * 12 + start_date.month - 1 + sequence
        year, month_zero = divmod(month_index, 12)
        month = month_zero + 1
        return dt.date(year, month, min(start_date.day, monthrange(year, month)[1]))
    return None

def ensure_recurrence_occurrences(c, now=None):
    today = (now or dt.datetime.now(family_timezone())).date()
    horizon = today + dt.timedelta(days=RECURRENCE_HORIZON_DAYS)
    for series in c.execute("SELECT * FROM recurrence_series WHERE active=1").fetchall():
        try:
            rule = json.loads(series["rule"])
            start = dt.date.fromisoformat(series["start_date"])
        except (ValueError, TypeError):
            continue
        repeat = rule.get("repeat", "One Time")
        series_kind = rule.get("seriesKind", "tasks")
        if repeat == "One Time":
            continue
        interval = rule.get("customIntervalDays", 1)
        anchor_sequence = series["anchor_sequence"]
        for offset in range(1, RECURRENCE_MAX_OCCURRENCES + 1):
            sequence = anchor_sequence + offset
            occurrence_date = recurrence_date(start, repeat, offset, interval)
            if not occurrence_date or occurrence_date > horizon:
                break
            if c.execute("SELECT 1 FROM recurrence_occurrences WHERE series_id=? AND occurrence_date=?", (series["series_id"], occurrence_date.isoformat())).fetchone():
                continue
            created_at = stamp()
            occurrence = dict(rule)
            occurrence.update(
                seriesId=series["series_id"], occurrenceDate=occurrence_date.isoformat(),
                occurrenceNumber=sequence, createdAt=created_at,
                updatedAt=created_at, creator=rule.get("creator", series["created_by"]),
                by=rule.get("creator", series["created_by"]),
            )
            occurrence["date" if series_kind == "events" else "dueDate"] = occurrence_date.isoformat()
            if series_kind == "tasks":
                occurrence.update(status="open", acked=[], acknowledgements=[], reason="",
                                  completionHistory=[], notCompletedHistory=[])
            cursor = c.execute("INSERT INTO records(kind,body) VALUES(?,?)", (series_kind, json.dumps(occurrence)))
            task_id = cursor.lastrowid
            c.execute("INSERT INTO recurrence_occurrences(series_id,occurrence_date,task_id,sequence) VALUES(?,?,?,?)",
                      (series["series_id"], occurrence_date.isoformat(), task_id, sequence))
            audit_record(c, rule.get("creator", series["created_by"]), task_id, "recurrence_occurrence_created",
                         {**occurrence, "id": task_id}, {"seriesId": series["series_id"], "sequence": sequence})

def item_reminder_times(record, kind, zone):
    date_value = record.get("date") if kind == "events" else record.get("dueDate")
    if not date_value:
        return None, None
    try:
        day = dt.date.fromisoformat(date_value[:10])
    except (ValueError, TypeError):
        return None, None
    time_value = record.get("startTime", "") if kind == "events" else record.get("dueTime", "")
    reminder_time = dt.time(9, 0) if not time_value or record.get("allDay") else dt.time.fromisoformat(time_value)
    due_time = dt.time(23, 59) if kind == "tasks" and not time_value else reminder_time
    reminder_at = dt.datetime.combine(day, reminder_time, zone)
    due_at = dt.datetime.combine(day, due_time, zone)
    return reminder_at, due_at

def reminder_recipients(record, profiles):
    if record.get("visibility", "Family") == "Me":
        return [record.get("creator")]
    if record.get("visibility") == "Adults":
        return [name for name in profiles if adult(name)]
    if record.get("visibility") == "Assigned":
        return [record.get("who")] if record.get("who") in profiles else []
    return list(profiles)

def generate_reminders(c, profiles, now=None):
    zone = family_timezone()
    current = now or dt.datetime.now(zone)
    for row in c.execute("SELECT id,kind,body FROM records WHERE deleted=0 AND kind IN ('tasks','events')").fetchall():
        record = json.loads(row["body"])
        kind = row["kind"]
        reminder_at, due_at = item_reminder_times(record, kind, zone)
        if not reminder_at:
            continue
        offsets = record.get("reminderOffsets", [0])
        for account in reminder_recipients(record, profiles):
            if account not in profiles:
                continue
            if not visible(record, account):
                continue
            if kind == "tasks" and record.get("status") in {"done", "missed"}:
                continue
            for offset in offsets if isinstance(offsets, list) else []:
                if type(offset) is not int or offset not in REMINDER_OFFSETS:
                    continue
                trigger = reminder_at - dt.timedelta(minutes=offset)
                if trigger <= current:
                    c.execute("INSERT OR IGNORE INTO reminders(account,source_kind,source_id,reminder_key,reminder_type,due_at,created_at) VALUES(?,?,?,?,?,?,?)",
                              (account, kind, row["id"], f"offset:{offset}:{reminder_at.isoformat()}:{record.get('updatedAt','')}", "event_upcoming" if kind == "events" else "task_due", trigger.isoformat(), stamp()))
            if kind == "tasks" and record.get("status") == "open":
                acked = set(record.get("acked", []))
                assignee = record.get("who")
                requires_ack = bool(record.get("ack")) and (account == assignee or assignee == "Everyone" or not assignee and account == record.get("creator"))
                if record.get("priority") == "Urgent" and requires_ack and account not in acked:
                    escalation = int(current.timestamp() // 3600)
                    c.execute("INSERT OR IGNORE INTO reminders(account,source_kind,source_id,reminder_key,reminder_type,due_at,created_at) VALUES(?,?,?,?,?,?,?)",
                              (account, kind, row["id"], f"urgent-ack:{escalation}", "urgent_ack", current.isoformat(), stamp()))
                if due_at <= current:
                    c.execute("INSERT OR IGNORE INTO reminders(account,source_kind,source_id,reminder_key,reminder_type,due_at,created_at) VALUES(?,?,?,?,?,?,?)",
                              (account, kind, row["id"], f"overdue:{record.get('updatedAt','')}", "overdue", due_at.isoformat(), stamp()))

def audit_notification_types(kind, action, record, actor, account):
    if actor == account or not visible(record, account):
        return []
    if kind == "tasks":
        result = []
        assignee = record.get("who")
        assigned = assignee == account or assignee == "Everyone"
        if action in {"create", "reassigned"} and assigned:
            result.append("task_assigned")
        if action in {"create", "reassigned", "edit"} and record.get("ack") and assigned and account not in record.get("acked", []):
            result.append("task_acknowledgement")
        if action in {"done", "miss", "reopen"} and adult(account):
            result.append("task_update")
        return result
    if kind == "requests" and action in {"create", "decision", "request_reply"}:
        return ["request_update"]
    if kind == "posts" and action in {"create", "comment", "pin", "ack"}:
        return ["board_update"]
    if kind == "dance" and action in {"create", "edit", "check", "checklist_item", "competition_unlinked", "deadline_completed"}:
        return ["dance_update"]
    if kind == "events" and action in {"create", "edit"} and not record.get("sourceDanceId"):
        return ["calendar_update"]
    if kind == "lists" and action in {"create", "check"}:
        return ["list_update"]
    return []

def generate_notification_reminders(c, profiles):
    setting = c.execute("SELECT value FROM notification_settings WHERE name='audit_notifications_started_at'").fetchone()
    started_at = setting["value"] if setting else stamp()
    cursor_setting = c.execute("SELECT value FROM notification_settings WHERE name='audit_notification_last_id'").fetchone()
    last_id = int(cursor_setting["value"]) if cursor_setting else 0
    high_water = c.execute("SELECT COALESCE(MAX(id),0) FROM audit").fetchone()[0]
    audits = c.execute("""
        SELECT a.id,a.actor,a.record_id,a.action,a.created,a.snapshot,r.kind,r.body,r.deleted
        FROM audit a JOIN records r ON r.id=a.record_id
        WHERE a.id>? AND a.id<=? AND a.created>=? AND r.deleted=0
        ORDER BY a.id
    """, (last_id, high_water, started_at)).fetchall()
    for audit in audits:
        current = json.loads(audit["body"])
        snapshot = json.loads(audit["snapshot"] or "{}")
        if not snapshot:
            snapshot = current
        for account in profiles:
            for notification_type in audit_notification_types(audit["kind"], audit["action"], snapshot, audit["actor"], account):
                if not visible(current, account):
                    continue
                key = f"notice:{audit['id']}:{notification_type}"
                c.execute("INSERT OR IGNORE INTO reminders(account,source_kind,source_id,reminder_key,reminder_type,due_at,created_at) VALUES(?,?,?,?,?,?,?)",
                          (account, audit["kind"], audit["record_id"], key, notification_type, audit["created"], audit["created"]))
    c.execute("UPDATE notification_settings SET value=? WHERE name='audit_notification_last_id'", (str(high_water),))

    today = local_today()
    week_start = today - dt.timedelta(days=today.weekday())
    week_key = week_start.isoformat()
    week_id = int(week_start.strftime("%Y%m%d"))
    week_due = dt.datetime.combine(week_start, dt.time.min, family_timezone()).astimezone(dt.timezone.utc).isoformat()
    for account in profiles:
        c.execute("INSERT OR IGNORE INTO reminders(account,source_kind,source_id,reminder_key,reminder_type,due_at,created_at) VALUES(?,?,?,?,?,?,?)",
                  (account, "recap", week_id, f"weekly-recap:{week_key}", "weekly_recap", week_due, stamp()))

def reminder_category(reminder_type):
    return {
        "task_due": "tasks", "priority": "tasks", "overdue": "tasks", "urgent_ack": "tasks",
        "task_assigned": "tasks", "task_acknowledgement": "tasks", "task_update": "tasks",
        "request_update": "requests", "board_update": "board", "dance_update": "dance",
        "calendar_update": "calendar", "list_update": "lists", "weekly_recap": "recap",
    }.get(reminder_type, "reminders")

def reminder_state(c, account, state):
    sources = {(kind, item["id"]): item for kind in KINDS for item in state[kind]}
    result = []
    now = dt.datetime.now(dt.timezone.utc)
    for row in c.execute("SELECT * FROM reminders WHERE account=? ORDER BY due_at DESC LIMIT 200", (account,)):
        source = sources.get((row["source_kind"], row["source_id"]))
        if row["source_kind"] == "recap":
            source = {"title": "Weekly Recap", "category": "Recap", "visibility": "Family"}
        if not source or row["source_kind"] == "tasks" and source.get("status") in {"done", "missed"} and not row["dismissed_at"]:
            continue
        if row["reminder_type"] in {"task_acknowledgement", "urgent_ack"} and account in source.get("acked", []):
            continue
        snoozed_until = row["snoozed_until"]
        snoozed = False
        if snoozed_until:
            try:
                snoozed = dt.datetime.fromisoformat(snoozed_until) > now
            except ValueError:
                pass
        result.append({
            "id": row["id"], "sourceKind": row["source_kind"], "sourceId": row["source_id"],
            "type": row["reminder_type"], "dueAt": row["due_at"], "createdAt": row["created_at"],
            "read": bool(row["read_at"]), "dismissed": bool(row["dismissed_at"]),
            "notificationCategory": reminder_category(row["reminder_type"]),
            "snoozed": snoozed, "snoozedUntil": snoozed_until,
            "title": source.get("title", source.get("text", "")), "description": source.get("description", ""),
            "who": source.get("who", ""), "priority": source.get("priority", ""),
            "category": source.get("category", ""), "visibility": source.get("visibility", "Family"),
            "date": source.get("date", source.get("dueDate", "")),
            "time": source.get("startTime", source.get("dueTime", "")),
        })
    return result

def dance_attention_items(account, state):
    attention = {}
    today = local_today()
    for item in state["dance"]:
        reasons = []
        actionable_deadlines = []
        target = "Competitions"
        if item.get("danceType") == "competition":
            if item.get("schedulePending"):
                reasons.append("Performance schedule pending")
            for deadline in item.get("deadlines", []):
                if deadline.get("completed"):
                    continue
                try:
                    days_until = (dt.date.fromisoformat(deadline["date"]) - today).days
                except (ValueError, TypeError, KeyError):
                    continue
                status = "overdue" if days_until < 0 else "due today" if days_until == 0 else f"due in {days_until} days"
                reasons.append(f"{deadline['title']} {status}")
                actionable_deadlines.append({"id": deadline["id"], "title": deadline["title"], "date": deadline["date"]})
        elif item.get("danceType") == "costume":
            target = "Costumes"
            if not item.get("ordered"):
                reasons.append("Not ordered")
            elif not item.get("received"):
                reasons.append("Ordered, not received")
            if item.get("alterationsNeeded") and not item.get("alterationsCompleted"):
                reasons.append("Alterations needed")
        elif item.get("danceType") == "checklist":
            target = "Dance Checklists"
            remaining = sum(not check.get("checked") for check in item.get("checklistItems", []))
            if remaining:
                reasons.append(f"{remaining} packing item{'s' if remaining != 1 else ''} unchecked")
        if reasons:
            attention[item["id"]] = {"id": item["id"], "title": item.get("title", "Dance item"), "danceType": item.get("danceType"), "reasons": reasons, "deadlines": actionable_deadlines, "target": target}
    for reminder in state["reminders"]:
        if reminder["notificationCategory"] != "dance" or reminder["read"] or reminder["dismissed"] or reminder["sourceKind"] != "dance":
            continue
        source_id = reminder["sourceId"]
        if source_id not in attention:
            item = next((dance for dance in state["dance"] if dance["id"] == source_id), None)
            if item:
                attention[source_id] = {"id": source_id, "title": item.get("title", "Dance item"), "danceType": item.get("danceType"), "reasons": [], "deadlines": [], "target": {"competition": "Competitions", "costume": "Costumes", "checklist": "Dance Checklists", "routine": "Routines", "schedule": "Schedule"}.get(item.get("danceType"), "Dance Home")}
        if source_id in attention and "New update to review" not in attention[source_id]["reasons"]:
            attention[source_id]["reasons"].append("New update to review")
    return sorted(attention.values(), key=lambda item: (item["target"], item["title"].casefold()))

def notification_badge_counts(account, state):
    sources = {"tasks": set(), "requests": set(), "dance": set(), "board": set(), "calendar": set(), "lists": set(), "recap": set(), "reminders": set()}
    task_by_id = {task["id"]: task for task in state["tasks"]}
    for item in state["reminders"]:
        if item["dismissed"]:
            continue
        kind, record_id, notification_type = item["sourceKind"], item["sourceId"], item["type"]
        entity = (kind, record_id)
        if not item["read"]:
            sources["reminders"].add(entity)
            category = item["notificationCategory"]
            if category in sources:
                sources[category].add(entity)
        if notification_type == "task_acknowledgement":
            task = task_by_id.get(record_id)
            if task and task.get("status") == "open" and account not in task.get("acked", []):
                sources["tasks"].add(entity)
                sources["reminders"].add(entity)
        if notification_type == "urgent_ack":
            task = task_by_id.get(record_id)
            if task and task.get("status") == "open" and account not in task.get("acked", []):
                sources["tasks"].add(entity)
                sources["reminders"].add(entity)
    for task in state["tasks"]:
        if task.get("status") != "open":
            continue
        assigned = task.get("who") in {account, "Everyone"} or not task.get("who") and task.get("creator") == account
        if assigned and task.get("ack") and account not in task.get("acked", []):
            entity = ("tasks", task["id"])
            sources["tasks"].add(entity)
            sources["reminders"].add(entity)
    if adult(account):
        for request in state["requests"]:
            if request.get("status") == "Pending":
                sources["requests"].add(("requests", request["id"]))
    for post in state["posts"]:
        if post.get("important") and post.get("ackRequired") and account not in post.get("acknowledgedBy", []):
            sources["board"].add(("posts", post["id"]))
    sources["dance"].update(("dance", item["id"]) for item in state.get("danceAttention", []))
    if account == "Daughter":
        sources["recognition"] = {("recognitions", item["id"]) for item in state["recognitions"] if not item.get("seenAt")}
    else:
        sources["recognition"] = set()
    return {name: min(len(items), 10) for name, items in sources.items()}

def edit_record_scope(c, actor, record_id, kind, current, changes, scope):
    if kind not in {"tasks", "events"} or not adult(actor) or not editable(current, actor):
        raise PermissionError("You cannot edit this item")
    series_id = current.get("seriesId")
    series = c.execute("SELECT * FROM recurrence_series WHERE series_id=? AND active=1", (series_id,)).fetchone() if series_id else None
    if not series:
        if scope not in {None, "this"}:
            raise ValueError("This item is not part of an active recurring series")
        scope = "this"
    elif scope not in {"this", "future", "series"}:
        raise ValueError("Choose this occurrence, this and future, or entire series")

    allowed = set(TASK_FIELDS if kind == "tasks" else ("title", "description", "category", "date", "startTime", "endTime", "allDay", "location", "who", "people", "visibility", "repeat", "customIntervalDays", "reminderOffsets"))
    if not isinstance(changes, dict) or set(changes) - allowed:
        raise ValueError("Invalid item changes")
    validate = task_fields if kind == "tasks" else event_fields
    if scope == "this":
        updated = validate(changes, actor, current)
        details = {"before": {}, "after": {}}
        for field in allowed:
            if updated.get(field) != current.get(field):
                details["before"][field] = current.get(field)
                details["after"][field] = updated.get(field)
        if kind == "tasks":
            updated.update(updatedAt=stamp(), updatedBy=actor)
        else:
            updated.update(updatedAt=stamp(), updatedBy=actor)
        c.execute("UPDATE records SET body=? WHERE id=?", (json.dumps(updated), record_id))
        if series and (("dueDate" if kind == "tasks" else "date") in details["after"]):
            date_value = updated.get("dueDate" if kind == "tasks" else "date")
            c.execute("UPDATE recurrence_occurrences SET occurrence_date=? WHERE task_id=?", (date_value, record_id))
        if {"dueDate", "dueTime", "date", "startTime", "endTime", "reminderOffsets"} & set(details["after"]):
            c.execute("UPDATE reminders SET dismissed_at=COALESCE(dismissed_at,?) WHERE source_kind=? AND source_id=?", (updated["updatedAt"], kind, record_id))
        return updated, [(record_id, "edit", details)]

    current_occurrence = c.execute("SELECT occurrence_date,sequence FROM recurrence_occurrences WHERE series_id=? AND task_id=?", (series_id, record_id)).fetchone()
    if not current_occurrence:
        raise ValueError("Recurring occurrence index is unavailable")
    sequence = current_occurrence["sequence"]
    series_rule = json.loads(series["rule"])
    normalized_rule = validate(changes, actor, series_rule)
    date_field = "dueDate" if kind == "tasks" else "date"
    current_date = dt.date.fromisoformat(current_occurrence["occurrence_date"])
    proposed_current_date = dt.date.fromisoformat(changes.get(date_field, current_occurrence["occurrence_date"])[:10])
    if scope == "future":
        anchor = proposed_current_date
        anchor_sequence = sequence
    else:
        series_start = dt.date.fromisoformat(series["start_date"])
        original_expected = recurrence_date(series_start, series_rule.get("repeat", "One Time"), sequence - series["anchor_sequence"], series_rule.get("customIntervalDays", 1)) or current_date
        anchor = series_start + (proposed_current_date - original_expected if date_field in changes else dt.timedelta())
        anchor_sequence = series["anchor_sequence"]

    occurrences = []
    for occurrence in c.execute("SELECT o.task_id,o.occurrence_date,o.sequence,r.body,r.deleted FROM recurrence_occurrences o JOIN records r ON r.id=o.task_id WHERE o.series_id=? ORDER BY o.sequence", (series_id,)).fetchall():
        if scope == "future" and occurrence["sequence"] < sequence:
            continue
        record = json.loads(occurrence["body"])
        if occurrence["deleted"] or record.get("status") in {"done", "missed"}:
            continue
        normalized = validate(changes, actor, record)
        occurrences.append((occurrence, record, normalized))
    if not occurrences:
        raise ValueError("No open occurrences can be changed")

    repeat = normalized_rule.get("repeat", "One Time")
    interval = normalized_rule.get("customIntervalDays", 1)
    scheduled = {}
    if repeat != "One Time":
        for occurrence, _, _ in occurrences:
            offset = occurrence["sequence"] - anchor_sequence
            new_date = recurrence_date(anchor, repeat, offset, interval)
            if not new_date:
                continue
            if new_date.isoformat() in scheduled.values():
                raise ValueError("This schedule would create duplicate occurrences")
            scheduled[occurrence["task_id"]] = new_date.isoformat()
        target_ids = set(scheduled)
        for new_date in scheduled.values():
            conflict = c.execute("SELECT task_id FROM recurrence_occurrences WHERE series_id=? AND occurrence_date=?", (series_id, new_date)).fetchone()
            if conflict and conflict["task_id"] not in target_ids:
                raise ValueError("This schedule conflicts with an existing occurrence")
    else:
        for occurrence, _, normalized in occurrences:
            scheduled[occurrence["task_id"]] = normalized.get(date_field, occurrence["occurrence_date"])

    updated_current = current
    audit_events = []
    for occurrence, original, updated in occurrences:
        if occurrence["task_id"] in scheduled:
            updated[date_field] = scheduled[occurrence["task_id"]]
            updated["occurrenceDate"] = scheduled[occurrence["task_id"]]
        updated_at = stamp()
        updated.update(updatedAt=updated_at, updatedBy=actor)
        details = {"before": {}, "after": {}}
        for field in allowed:
            if updated.get(field) != original.get(field):
                details["before"][field] = original.get(field)
                details["after"][field] = updated.get(field)
        c.execute("UPDATE records SET body=? WHERE id=?", (json.dumps(updated), occurrence["task_id"]))
        audit_events.append((occurrence["task_id"], "edit", details))
        if "who" in details["after"]:
            audit_events.append((occurrence["task_id"], "reassigned", details))
        if {"dueDate", "dueTime", "date", "startTime", "endTime"} & set(details["after"]):
            audit_events.append((occurrence["task_id"], "due_date", details))
        if {"dueDate", "dueTime", "date", "startTime", "endTime", "reminderOffsets"} & set(details["after"]):
            c.execute("UPDATE reminders SET dismissed_at=COALESCE(dismissed_at,?) WHERE source_kind=? AND source_id=?", (updated_at, kind, occurrence["task_id"]))
        if occurrence["task_id"] == record_id:
            updated_current = updated

    c.execute("DELETE FROM recurrence_occurrences WHERE task_id IN (%s)" % ",".join("?" for _ in scheduled), tuple(scheduled))
    for occurrence, _, _ in occurrences:
        task_id = occurrence["task_id"]
        if task_id in scheduled:
            c.execute("INSERT INTO recurrence_occurrences(series_id,occurrence_date,task_id,sequence) VALUES(?,?,?,?)",
                      (series_id, scheduled[task_id], task_id, occurrence["sequence"]))

    rule_date = anchor.isoformat()
    normalized_rule[date_field] = rule_date
    normalized_rule["seriesKind"] = kind
    normalized_rule["seriesId"] = series_id
    c.execute("UPDATE recurrence_series SET rule=?,start_date=?,anchor_sequence=?,active=?,updated_at=? WHERE series_id=?",
              (json.dumps(normalized_rule), rule_date, anchor_sequence, 0 if repeat == "One Time" else 1, stamp(), series_id))
    return updated_current, audit_events

def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def audit_record(c, actor, record_id, action, snapshot, details=None, created=None):
    c.execute(
        "INSERT INTO audit(actor,record_id,action,created,snapshot,details) VALUES(?,?,?,?,?,?)",
        (actor, record_id, action, created or stamp(), json.dumps(snapshot), json.dumps(details or {})),
    )

def activity_entry(row, snapshot, profiles, viewer, record_kind=""):
    details = json.loads(row["details"] or "{}")
    before = details.get("before")
    if before and (not visible(before, viewer) or viewer == "Daughter" and before.get("visibility") != "Family"):
        details = {}
    if viewer == "Daughter" and record_kind == "dance":
        for revision in (details.get("before"), details.get("after")):
            if isinstance(revision, dict):
                revision.pop("fees", None)
                revision.pop("financials", None)
    actor = profiles.get(row["actor"], {}).get("displayName", row["actor"])
    title = snapshot.get("title") or snapshot.get("text") or "an item"
    action = row["action"]
    item_type = "event" if "allDay" in snapshot or "startTime" in snapshot or snapshot.get("seriesKind") == "events" else "task"
    if record_kind == "requests" and action == "create":
        request_type = snapshot.get("type") or "Other"
        summary = f'🙋 {actor} sent a {request_type} request "{title}"'
    elif record_kind == "requests" and action == "decision" and snapshot.get("status") == "Approved":
        requester = profiles.get(snapshot.get("creator"), {}).get("displayName", snapshot.get("creator", "a family member"))
        summary = f'✅ {actor} approved {requester}\u2019s request "{title}"'
    elif record_kind == "requests" and action == "decision" and snapshot.get("status") == "Denied":
        requester = profiles.get(snapshot.get("creator"), {}).get("displayName", snapshot.get("creator", "a family member"))
        summary = f'❌ {actor} denied {requester}\u2019s request "{title}"'
    elif record_kind == "requests" and action == "request_reply":
        requester = profiles.get(snapshot.get("creator"), {}).get("displayName", snapshot.get("creator", "a family member"))
        summary = f'💬 {actor} replied to {requester}\u2019s request "{title}"'
    elif record_kind == "recognitions" and action == "create":
        recipient = profiles.get(snapshot.get("who"), {}).get("displayName", "a family member")
        summary = f'🌟 {actor} recognized {recipient}: {snapshot.get("recognitionType", "Nice Work")}'
    elif record_kind == "dance" and action == "deadline_completed":
        summary = f'✅ {actor} completed a competition deadline for "{title}"'
    elif action == "create":
        summary = f"📝 {actor} created {item_type} \"{title}\""
    elif action == "recurring_series_created":
        summary = f"🔁 {actor} created a recurring {item_type} series for \"{title}\""
    elif action == "recurring_series_changed":
        summary = f"🔁 {actor} changed the recurring series for \"{title}\""
    elif action == "recurring_occurrence_edited":
        summary = f"📝 {actor} edited an occurrence of \"{title}\""
    elif action == "recurrence_occurrence_created":
        summary = f"🔁 Created occurrence of \"{title}\""
    elif action == "recurrence_occurrence_deleted":
        summary = f"🗑️ {actor} deleted this occurrence of \"{title}\""
    elif action == "reminder_dismissed":
        summary = f"🔕 {actor} dismissed a reminder for \"{title}\""
    elif action == "reminder_snoozed":
        summary = f"⏰ {actor} snoozed a reminder for \"{title}\""
    elif action == "due_date":
        summary = f"📅 {actor} changed the due date for \"{title}\""
    elif action == "edit":
        summary = f"📝 {actor} edited \"{title}\""
    elif action == "reassigned":
        target = details.get("after", {}).get("who", "")
        target_name = "Everyone" if target == "Everyone" else profiles.get(target, {}).get("displayName", "Unassigned") if target else "Unassigned"
        summary = f"🔄 {actor} reassigned \"{title}\" to {target_name}"
    elif action == "ack":
        summary = f"👀 {actor} acknowledged \"{title}\""
    elif action == "done":
        summary = f"✅ {actor} completed \"{title}\""
    elif action == "miss":
        reason = snapshot.get("reason", "")
        summary = f"❌ {actor} marked \"{title}\" not completed"
        if reason:
            summary += f" · {reason}"
    elif action == "reopen":
        summary = f"↩️ {actor} reopened \"{title}\""
    elif action == "delete":
        summary = f"🗑️ {actor} deleted \"{title}\""
    else:
        summary = f"{actor} {action} \"{title}\""
    return {
        "id": row["id"], "recordId": row["record_id"], "actor": row["actor"],
        "actorName": actor, "action": action, "title": title, "summary": summary,
        "createdAt": row["created"], "details": details,
    }

def chore_completion_metrics(c):
    completions = []
    for row in c.execute("SELECT body FROM records WHERE kind='tasks' AND deleted=0"):
        task = json.loads(row["body"])
        if not qualifies_as_chore(task) or not visible(task, "Daughter"):
            continue
        history = task.get("completionHistory", [])
        if not history and task.get("status") == "done" and task.get("completedAt"):
            history = [{"completedAt": task["completedAt"], "completedBy": task.get("completedBy")}]
        for entry in history:
            if entry.get("completedBy") != "Daughter" or not entry.get("completedAt"):
                continue
            try:
                completed = dt.datetime.fromisoformat(entry["completedAt"])
                if completed.tzinfo is None:
                    completed = completed.replace(tzinfo=dt.timezone.utc)
                completions.append(completed.astimezone(family_timezone()).date())
            except (ValueError, TypeError):
                continue
    days = set(completions)
    today = local_today()
    streak_day = today if today in days else today - dt.timedelta(days=1)
    streak = 0
    while streak_day in days:
        streak += 1
        streak_day -= dt.timedelta(days=1)
    perfect_week = all(today - dt.timedelta(days=offset) in days for offset in range(7))
    achievements = [
        {"id": "streak_7", "label": "7-day streak", "earned": streak >= 7},
        {"id": "chores_25", "label": "25 completed chores", "earned": len(completions) >= 25},
        {"id": "perfect_week", "label": "Perfect Week", "earned": perfect_week},
    ]
    return {"points": len(completions), "streak": streak, "achievements": achievements}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Do not log credentials, cookies or private family content.

    def respond(self, status, data, cookie=None):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def identity(self, c):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        token = cookie.get("hub_session")
        if not token:
            return None
        hashed = hashlib.sha256(token.value.encode()).hexdigest()
        row = c.execute("SELECT name FROM sessions WHERE token=? AND expires>?", (hashed, time.time())).fetchone()
        return row["name"] if row else None

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/profiles":
            with connection() as c:
                return self.respond(200, {"profiles": profile_data(c)})
        if path == "/api/state":
            with connection() as c:
                name = self.identity(c)
                if not name:
                    return self.respond(401, {"error": "Please sign in."})
                ensure_recurrence_occurrences(c)
                state = {k: [] for k in KINDS}
                allowed = set()
                for row in c.execute("SELECT * FROM records WHERE deleted=0"):
                    record = json.loads(row["body"])
                    record["id"] = row["id"]
                    if visible(record, name):
                        if row["kind"] == "dance":
                            record = dance_record_for_viewer(record, name)
                        if row["kind"] == "tasks":
                            record["chore"] = qualifies_as_chore(record)
                        if row["kind"] == "recognitions" and name == "Daughter":
                            receipt = c.execute("SELECT seen_at FROM recognition_receipts WHERE recognition_id=? AND recipient=?", (row["id"], name)).fetchone()
                            record["seenAt"] = receipt["seen_at"] if receipt else ""
                        state[row["kind"]].append(record)
                        allowed.add(row["id"])
                profiles = profile_data(c)
                activity = []
                for event in c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 500"):
                    snapshot = json.loads(event["snapshot"] or "{}")
                    record_row = c.execute("SELECT kind,body FROM records WHERE id=?", (event["record_id"],)).fetchone() if event["record_id"] else None
                    if not snapshot and event["record_id"]:
                        if record_row:
                            snapshot = json.loads(record_row["body"])
                    if not snapshot or not visible(snapshot, name):
                        continue
                    activity.append(activity_entry(event, snapshot, profiles, name, record_row["kind"] if record_row else ""))
                    if len(activity) == 50:
                        break
                state["activity"] = activity
                state.update(viewer=name, profiles=profiles, **chore_completion_metrics(c))
                state["timezone"] = HUB_TIMEZONE
                generate_reminders(c, profiles)
                generate_notification_reminders(c, profiles)
                state["reminders"] = reminder_state(c, name, state)
                state["danceAttention"] = dance_attention_items(name, state)
                state["badgeCounts"] = notification_badge_counts(name, state)
                return self.respond(200, state)
        filename = "index.html" if path == "/" else path.lstrip("/")
        if filename not in STATIC:
            return self.respond(404, {"error": "Not found"})
        import mimetypes
        body = (ROOT / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(filename)[0] or "text/plain")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # Custom header forces cross-origin callers to preflight; no CORS is enabled.
        if self.headers.get("X-Hub-Request") != "1":
            return self.respond(403, {"error": "Invalid request origin"})
        expected = os.environ.get("HUB_ORIGIN")
        if expected and self.headers.get("Origin") != expected:
            return self.respond(403, {"error": "Invalid request origin"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 32768:
                raise ValueError("Request is too large or empty")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Expected an object")
            with connection() as c:
                name = self.identity(c)
                if self.path == "/api/login":
                    if not isinstance(payload.get("name"), str) or payload["name"] not in {"Dad", "Mom", "Daughter"} or not isinstance(payload.get("password"), str) or len(payload["password"]) > 1024:
                        raise ValueError("Invalid sign-in details")
                    address = self.client_address[0]
                    now = int(time.time())
                    attempt = c.execute("SELECT * FROM attempts WHERE address=?", (address,)).fetchone()
                    if attempt and attempt["reset"] > now and attempt["count"] >= 10:
                        return self.respond(429, {"error": "Too many attempts. Try again in 15 minutes."})
                    user = c.execute("SELECT * FROM users WHERE name=?", (payload.get("name"),)).fetchone()
                    salt = user["salt"] if user else "00" * 16
                    valid = hmac.compare_digest(password_hash(str(payload.get("password", "")), salt), user["hash"] if user else "00" * 64)
                    if not user or not valid:
                        count = attempt["count"] + 1 if attempt and attempt["reset"] > now else 1
                        reset = attempt["reset"] if attempt and attempt["reset"] > now else now + 900
                        c.execute("INSERT OR REPLACE INTO attempts VALUES(?,?,?)", (address, count, reset))
                        c.commit()
                        return self.respond(401, {"error": "Incorrect name or password"})
                    c.execute("DELETE FROM attempts WHERE address=?", (address,))
                    c.execute("DELETE FROM sessions WHERE expires<?", (now,))
                    token = secrets.token_urlsafe(32)
                    c.execute("INSERT INTO sessions VALUES(?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), user["name"], now + 604800))
                    secure = "; Secure" if os.environ.get("HUB_SECURE_COOKIE") == "1" else ""
                    c.commit()
                    return self.respond(200, {"ok": True}, f"hub_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=604800{secure}")
                if not name:
                    return self.respond(401, {"error": "Please sign in."})
                if self.path == "/api/logout":
                    cookie = SimpleCookie(self.headers.get("Cookie", ""))
                    c.execute("DELETE FROM sessions WHERE token=?", (hashlib.sha256(cookie["hub_session"].value.encode()).hexdigest(),))
                    c.commit()
                    return self.respond(200, {"ok": True}, "hub_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
                if self.path == "/api/reminders/action":
                    if payload.get("action") == "read_category":
                        category = payload.get("category")
                        category_types = {
                            "tasks": ("task_due", "priority", "overdue", "urgent_ack", "task_assigned", "task_acknowledgement", "task_update"),
                            "requests": ("request_update",), "board": ("board_update",),
                            "dance": ("dance_update", "dance_upcoming"),
                            "calendar": ("calendar_update", "event_upcoming"), "lists": ("list_update",),
                            "recap": ("weekly_recap",),
                            "reminders": ("task_due", "priority", "overdue", "urgent_ack", "task_assigned", "task_acknowledgement", "task_update", "request_update", "board_update", "dance_update", "dance_upcoming", "calendar_update", "event_upcoming", "list_update", "weekly_recap"),
                        }
                        types = category_types.get(category)
                        if not types:
                            raise ValueError("Invalid reminder category")
                        placeholders = ",".join("?" for _ in types)
                        c.execute(f"UPDATE reminders SET read_at=COALESCE(read_at,?) WHERE account=? AND dismissed_at IS NULL AND read_at IS NULL AND reminder_type IN ({placeholders})",
                                  (stamp(), name, *types))
                        c.commit()
                        return self.respond(200, {"ok": True})
                    reminder_id = payload.get("id")
                    if type(reminder_id) is not int:
                        raise ValueError("Invalid reminder ID")
                    reminder = c.execute("SELECT * FROM reminders WHERE id=? AND account=?", (reminder_id, name)).fetchone()
                    if not reminder:
                        return self.respond(404, {"error": "Reminder unavailable"})
                    if reminder["source_kind"] == "recap":
                        if payload.get("action") not in {"read", "dismiss"}:
                            raise ValueError("Unknown recap reminder action")
                        now = stamp()
                        c.execute("UPDATE reminders SET read_at=COALESCE(read_at,?),dismissed_at=CASE WHEN ?='dismiss' THEN COALESCE(dismissed_at,?) ELSE dismissed_at END WHERE id=? AND account=?",
                                  (now, payload["action"], now, reminder_id, name))
                        c.commit()
                        return self.respond(200, {"ok": True})
                    record_row = c.execute("SELECT * FROM records WHERE id=? AND kind=? AND deleted=0", (reminder["source_id"], reminder["source_kind"])).fetchone()
                    if not record_row:
                        return self.respond(404, {"error": "Reminder source unavailable"})
                    source = json.loads(record_row["body"])
                    if not visible(source, name):
                        return self.respond(404, {"error": "Reminder unavailable"})
                    action = payload.get("action")
                    now = stamp()
                    if action == "read":
                        c.execute("UPDATE reminders SET read_at=COALESCE(read_at,?) WHERE id=? AND account=?", (now, reminder_id, name))
                    elif action == "dismiss":
                        c.execute("UPDATE reminders SET read_at=COALESCE(read_at,?),dismissed_at=COALESCE(dismissed_at,?) WHERE id=? AND account=?", (now, now, reminder_id, name))
                        audit_record(c, name, reminder["source_id"], "reminder_dismissed", {**source, "id": reminder["source_id"]}, {"reminderType": reminder["reminder_type"]})
                    elif action == "snooze":
                        minutes = payload.get("minutes")
                        if minutes not in {15, 30, 60, 120, 1440}:
                            raise ValueError("Choose a supported snooze interval")
                        until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).isoformat()
                        c.execute("UPDATE reminders SET read_at=COALESCE(read_at,?),snoozed_until=? WHERE id=? AND account=?", (now, until, reminder_id, name))
                        audit_record(c, name, reminder["source_id"], "reminder_snoozed", {**source, "id": reminder["source_id"]}, {"reminderType": reminder["reminder_type"], "snoozedUntil": until})
                    else:
                        raise ValueError("Unknown reminder action")
                    c.commit()
                    return self.respond(200, {"ok": True})
                if self.path == "/api/recognitions/action":
                    if name != "Daughter":
                        return self.respond(403, {"error": "Only Arielle can mark her recognition as seen"})
                    recognition_id = payload.get("id")
                    if type(recognition_id) is not int or payload.get("action") != "seen":
                        raise ValueError("Invalid recognition action")
                    row = c.execute("SELECT body FROM records WHERE id=? AND kind='recognitions' AND deleted=0", (recognition_id,)).fetchone()
                    if not row:
                        return self.respond(404, {"error": "Recognition unavailable"})
                    recognition = json.loads(row["body"])
                    if recognition.get("who") != name or not visible(recognition, name):
                        return self.respond(404, {"error": "Recognition unavailable"})
                    c.execute("INSERT OR IGNORE INTO recognition_receipts(recognition_id,recipient,seen_at) VALUES(?,?,?)",
                              (recognition_id, name, stamp()))
                    c.commit()
                    return self.respond(200, {"ok": True})
                if self.path != "/api/action":
                    return self.respond(404, {"error": "Not found"})
                c.execute("BEGIN IMMEDIATE")
                action = payload.get("action")
                event_details = {}
                extra_audit_events = []
                recurring_series_created = False
                if action == "create":
                    kind = payload.get("kind")
                    if kind not in KINDS:
                        raise ValueError("Unknown item type")
                    if kind == "tasks" and not adult(name):
                        return self.respond(403, {"error": "Only parents create tasks"})
                    r = payload.get("record", {})
                    if not isinstance(r, dict):
                        raise ValueError("Invalid item")
                    r = {key: r[key] for key in ("title", "text", "description", "date", "startDate", "endDate", "neededBy", "startTime", "endTime", "allDay", "location", "people", "customIntervalDays", "reminderOffsets", "dueDate", "dueTime", "who", "priority", "visibility", "type", "category", "repeat", "ack", "chore", "status", "reply", "replyBy", "time", "important", "pinned", "ackRequired", "comments", "acknowledgedBy", "responses", "history", "recognitionType", "message", "sourceTaskId") + (DANCE_FIELDS if kind == "dance" else ()) if key in r}
                    for key in ("title", "text", "description", "date", "startTime", "endTime", "location", "dueDate", "dueTime", "type", "category", "repeat", "status", "reply", "replyBy", "time"):
                        if key in r and (not isinstance(r[key], str) or len(r[key]) > 4000):
                            raise ValueError("Invalid text")
                    if kind == "recognitions":
                        r.setdefault("title", r.get("recognitionType", "Recognition"))
                        r.setdefault("text", r.get("message", ""))
                    if not (r.get("title", "").strip() or r.get("text", "").strip()):
                        raise ValueError("Please enter some text")
                    created_at = stamp()
                    r.update(creator=name, by=name, createdAt=created_at, updatedAt=created_at)
                    r.setdefault("visibility", "Family")
                    if r["visibility"] not in VISIBILITY or r["visibility"] == "Adults" and not adult(name):
                        raise ValueError("Invalid visibility")
                    if kind == "events" and not adult(name):
                        return self.respond(403, {"error": "Only parents can create family events"})
                    if r.get("who", "Everyone") not in {"Dad", "Mom", "Daughter", "Everyone", ""}:
                        raise ValueError("Invalid assignee")
                    if r["visibility"] == "Assigned" and r.get("who") not in {"Dad", "Mom", "Daughter"}:
                        raise ValueError("Choose one assigned person")
                    if kind == "tasks":
                        r.update(task_fields(r, name))
                        r.update(status="open", acked=[], acknowledgements=[], reason="", completionHistory=[], notCompletedHistory=[])
                    if kind == "events":
                        r.update(event_fields(r, name))
                        if r["repeat"] != "One Time":
                            r["seriesKind"] = "events"
                    if kind == "dance" and r.get("danceType"):
                        if not adult(name):
                            return self.respond(403, {"error": "Only parents can manage dance details"})
                        r.update(normalize_dance_record(r, name))
                        validate_dance_associations(c, r, name)
                    if kind == "requests":
                        r.setdefault("type", "Other")
                        r.setdefault("title", r.get("text", "").strip() or "Request")
                        r.setdefault("date", "")
                        r.setdefault("time", "")
                        r.setdefault("description", "")
                        if r["type"] not in REQUEST_TYPES:
                            raise ValueError("Choose a valid request type")
                        if not isinstance(r.get("text"), str) or not r["text"].strip():
                            raise ValueError("What would you like to ask?")
                        if not isinstance(r["description"], str) or len(r["description"]) > 4000:
                            raise ValueError("Invalid request details")
                        if r["date"]:
                            try:
                                dt.date.fromisoformat(r["date"])
                            except (ValueError, TypeError) as error:
                                raise ValueError("Choose a valid request date") from error
                        if r["time"]:
                            try:
                                dt.time.fromisoformat(r["time"])
                            except (ValueError, TypeError) as error:
                                raise ValueError("Choose a valid request time") from error
                        r.update(status="Pending", reply="", replyBy="", responses=[], visibility="Family")
                    if kind == "posts":
                        r["visibility"] = "Family"
                        if (r.get("important") or r.get("ackRequired")) and not adult(name):
                            return self.respond(403, {"error": "Only parents can create important announcements"})
                        if r.get("ackRequired") and not r.get("important"):
                            raise ValueError("Acknowledgement is only available for important announcements")
                        r.setdefault("comments", [])
                        r.setdefault("reacts", {})
                        r.setdefault("reactors", {})
                        r.setdefault("pinned", False)
                        r.setdefault("important", False)
                        r.setdefault("ackRequired", False)
                        r.setdefault("acknowledgedBy", [])
                    if kind == "recognitions":
                        if not adult(name):
                            return self.respond(403, {"error": "Only parents can give recognition"})
                        recognition_type = r.get("recognitionType")
                        if recognition_type not in RECOGNITION_TYPES:
                            raise ValueError("Choose a recognition type")
                        message = r.get("message", "")
                        if not isinstance(message, str) or len(message) > 500:
                            raise ValueError("Recognition note must be at most 500 characters")
                        source_task_id = r.get("sourceTaskId")
                        if source_task_id is not None:
                            if type(source_task_id) is not int:
                                raise ValueError("Invalid recognition task")
                            source = c.execute("SELECT body,deleted FROM records WHERE id=? AND kind='tasks'", (source_task_id,)).fetchone()
                            if not source or source["deleted"]:
                                raise ValueError("Completed task unavailable")
                            source_task = json.loads(source["body"])
                            if not visible(source_task, name) or source_task.get("status") != "done" or source_task.get("completedBy") != "Daughter":
                                raise ValueError("Recognition must be linked to Arielle's completed work")
                        r.update(title=recognition_type, text=message, who="Daughter", visibility="Family")
                    if kind in {"lists", "dance"}:
                        r["checked"] = False
                    cursor = c.execute("INSERT INTO records(kind,body) VALUES(?,?)", (kind, json.dumps(r)))
                    rid = cursor.lastrowid
                    if kind == "dance" and r.get("danceType") == "competition":
                        sync_competition_calendar(c, name, rid, r)
                    if kind in {"tasks", "events"} and r.get("repeat") != "One Time":
                        start_date = r.get("dueDate") if kind == "tasks" else r.get("date")
                        if not start_date:
                            raise ValueError("Recurring items need a date")
                        r.update(seriesId=rid, occurrenceDate=start_date, occurrenceNumber=0)
                        series_rule = dict(r)
                        if kind == "tasks":
                            series_rule["seriesKind"] = "tasks"
                        c.execute("UPDATE records SET body=? WHERE id=?", (json.dumps(r), rid))
                        c.execute("INSERT INTO recurrence_series(series_id,rule,start_date,created_at,updated_at,created_by) VALUES(?,?,?,?,?,?)",
                                  (rid, json.dumps(series_rule), start_date, created_at, created_at, name))
                        c.execute("INSERT INTO recurrence_occurrences(series_id,occurrence_date,task_id,sequence) VALUES(?,?,?,0)",
                                  (rid, start_date, rid))
                        recurring_series_created = True
                        ensure_recurrence_occurrences(c)
                else:
                    if type(payload.get("id")) is not int:
                        raise ValueError("Invalid item ID")
                    row = c.execute("SELECT * FROM records WHERE id=? AND deleted=0", (payload["id"],)).fetchone()
                    if not row or not visible(json.loads(row["body"]), name):
                        return self.respond(404, {"error": "Item unavailable"})
                    rid, kind, r = row["id"], row["kind"], json.loads(row["body"])
                    previous = dict(r)
                    if kind == "events" and r.get("sourceDanceId") and action in {"edit", "delete"}:
                        return self.respond(403, {"error": "Edit or delete the source competition to keep its calendar dates in sync"})
                    if action == "delete":
                        if kind in {"tasks", "events"} and not adult(name):
                            return self.respond(403, {"error": "Only parents can delete tasks and events"})
                        if not editable(r, name):
                            return self.respond(403, {"error": "You cannot delete this item"})
                        deleted_at = stamp()
                        series_id = r.get("seriesId")
                        scope = payload.get("scope", "this")
                        series = c.execute("SELECT * FROM recurrence_series WHERE series_id=? AND active=1", (series_id,)).fetchone() if series_id else None
                        if series and scope not in {"this", "future", "series"}:
                            raise ValueError("Choose this occurrence, this and future, or entire series")
                        targets = [(rid, r)]
                        if series and scope != "this":
                            occurrence = c.execute("SELECT sequence FROM recurrence_occurrences WHERE series_id=? AND task_id=?", (series_id, rid)).fetchone()
                            if not occurrence:
                                raise ValueError("Recurring occurrence index is unavailable")
                            if scope == "future":
                                rows = c.execute("SELECT r.id,r.body FROM recurrence_occurrences o JOIN records r ON r.id=o.task_id WHERE o.series_id=? AND o.sequence>=? AND r.deleted=0", (series_id, occurrence["sequence"])).fetchall()
                            else:
                                rows = c.execute("SELECT r.id,r.body FROM recurrence_occurrences o JOIN records r ON r.id=o.task_id WHERE o.series_id=? AND r.deleted=0", (series_id,)).fetchall()
                            targets = [(item["id"], json.loads(item["body"])) for item in rows if json.loads(item["body"]).get("status") not in {"done", "missed"}]
                            c.execute("UPDATE recurrence_series SET active=0,updated_at=? WHERE series_id=?", (deleted_at, series_id))
                        for target_id, target in targets:
                            target.update(deletedAt=deleted_at, deletedBy=name, updatedAt=deleted_at, updatedBy=name)
                            c.execute("UPDATE records SET body=?, deleted=1 WHERE id=?", (json.dumps(target), target_id))
                            c.execute("UPDATE reminders SET dismissed_at=COALESCE(dismissed_at,?) WHERE source_kind=? AND source_id=?", (deleted_at, kind, target_id))
                            audit_record(c, name, target_id, "delete", {**target, "id": target_id}, {"scope": scope})
                            if kind == "dance" and target.get("danceType") == "competition":
                                linked_events = c.execute("SELECT id,body FROM records WHERE kind='events' AND deleted=0").fetchall()
                                for linked_event in linked_events:
                                    event = json.loads(linked_event["body"])
                                    if event.get("sourceDanceId") == target_id:
                                        event.update(deletedAt=deleted_at, deletedBy=name, updatedAt=deleted_at, updatedBy=name)
                                        c.execute("UPDATE records SET body=?,deleted=1 WHERE id=?", (json.dumps(event), linked_event["id"]))
                                        c.execute("UPDATE reminders SET dismissed_at=COALESCE(dismissed_at,?) WHERE source_kind='events' AND source_id=?", (deleted_at, linked_event["id"]))
                                        audit_record(c, name, linked_event["id"], "delete", {**event, "id": linked_event["id"]}, {"sourceCompetitionId": target_id})
                                for linked_row in c.execute("SELECT id,body FROM records WHERE kind='dance' AND deleted=0 AND id<>?", (target_id,)).fetchall():
                                    linked_record = json.loads(linked_row["body"])
                                    before = {}
                                    competition_ids = linked_record.get("competitionIds")
                                    if isinstance(competition_ids, list) and target_id in competition_ids:
                                        before["competitionIds"] = competition_ids
                                        linked_record["competitionIds"] = [item for item in competition_ids if item != target_id]
                                    if linked_record.get("competitionId") == target_id:
                                        before["competitionId"] = target_id
                                        linked_record["competitionId"] = None
                                    if before:
                                        linked_record.update(updatedAt=deleted_at, updatedBy=name)
                                        c.execute("UPDATE records SET body=? WHERE id=?", (json.dumps(linked_record), linked_row["id"]))
                                        audit_record(c, name, linked_row["id"], "competition_unlinked", {**linked_record, "id": linked_row["id"]}, {"before": before, "competitionId": target_id})
                        if series and scope == "this":
                            audit_record(c, name, rid, "recurrence_occurrence_deleted", {**r, "id": rid}, {"scope": scope})
                        c.commit()
                        return self.respond(200, {"ok": True})
                    if action == "edit":
                        if kind == "dance":
                            if not adult(name) or not r.get("danceType"):
                                return self.respond(403, {"error": "Only parents can edit structured dance details"})
                            changes = payload.get("record")
                            if not isinstance(changes, dict) or set(changes) - set(DANCE_FIELDS) - {"title", "text", "category", "date", "time", "checked", "visibility", "who"}:
                                raise ValueError("Invalid dance item changes")
                            updated = normalize_dance_record(changes, name, r)
                            validate_dance_associations(c, updated, name)
                            event_details = {"before": {}, "after": {}}
                            for field in set(changes):
                                if r.get(field) != updated.get(field):
                                    event_details["before"][field] = r.get(field)
                                    event_details["after"][field] = updated.get(field)
                            updated.update(updatedAt=stamp(), updatedBy=name)
                            c.execute("UPDATE records SET body=? WHERE id=?", (json.dumps(updated), rid))
                            r = updated
                            if r.get("danceType") == "competition":
                                sync_competition_calendar(c, name, rid, r)
                        else:
                            if kind not in {"tasks", "events"} or not adult(name):
                                return self.respond(403, {"error": "Only parents can edit tasks and events"})
                            if not editable(r, name):
                                return self.respond(403, {"error": "You cannot edit this item"})
                            changes = payload.get("record")
                            scope = payload.get("scope", "this")
                            r, extra_audit_events = edit_record_scope(c, name, rid, kind, r, changes, scope)
                            event_details = next((details for target_id, event_action, details in extra_audit_events if target_id == rid and event_action == "edit"), {})
                            if r.get("seriesId"):
                                action = "recurring_series_changed" if scope in {"future", "series"} else "recurring_occurrence_edited"
                            else:
                                action = "edit"
                    if action in {"ack", "done", "miss"}:
                        if kind == "posts":
                            if not r.get("important") or not r.get("ackRequired"):
                                return self.respond(403, {"error": "This announcement does not require acknowledgement"})
                            people = set(r.get("acknowledgedBy", []))
                            people.add(name)
                            r["acknowledgedBy"] = sorted(people)
                            r["acknowledgedAt"] = stamp()
                            event_details = {"person": name, "acknowledgedAt": r["acknowledgedAt"]}
                        else:
                            if kind != "tasks" or r["status"] != "open":
                                return self.respond(403, {"error": "This task is not open"})
                            manager = adult(name) and payload.get("manager") is True
                            assigned = r.get("who") in {name, "Everyone"}
                            may_acknowledge = assigned or not r.get("who") and r.get("creator") == name
                            if action == "ack" and not may_acknowledge or action != "ack" and not (assigned or manager):
                                return self.respond(403, {"error": "This task is not assigned to you"})
                            if action == "ack":
                                r.setdefault("acked", [])
                                r.setdefault("acknowledgements", [])
                                if name not in r["acked"]:
                                    acknowledged_at = stamp()
                                    r["acked"].append(name)
                                    r["acknowledgements"].append({"person": name, "acknowledgedAt": acknowledged_at})
                                    event_details = {"person": name, "acknowledgedAt": acknowledged_at}
                            elif action == "done":
                                r.setdefault("acked", [])
                                required_ack = r.get("who") if r.get("who") in ACCOUNT_ROLES else name
                                if r.get("ack") and required_ack not in r["acked"]:
                                    raise ValueError("Acknowledge this task first")
                                completed_at = stamp()
                                r.setdefault("completionHistory", [])
                                r["completionHistory"].append({"completedBy": name, "completedAt": completed_at})
                                r.update(status="done", completedAt=completed_at, completedBy=name, updatedAt=completed_at, updatedBy=name)
                                event_details = {"completedBy": name, "completedAt": completed_at}
                            else:
                                reason_code = payload.get("reasonCode", "Other")
                                explanation = payload.get("explanation", payload.get("reason", ""))
                                if reason_code not in TASK_MISS_REASONS or not isinstance(explanation, str) or len(explanation) > 1000:
                                    raise ValueError("Choose a reason and provide at most 1000 characters of explanation")
                                missed_at = stamp()
                                reason = reason_code + (": " + explanation.strip() if explanation.strip() else "")
                                r.setdefault("notCompletedHistory", [])
                                entry = {"person": name, "reasonCode": reason_code, "explanation": explanation.strip(), "timestamp": missed_at}
                                r["notCompletedHistory"].append(entry)
                                r.update(status="missed", reason=reason, reasonCode=reason_code, reasonExplanation=explanation.strip(), reasonBy=name, reasonAt=missed_at, updatedAt=missed_at, updatedBy=name)
                                event_details = entry
                    elif action == "reopen":
                        if kind != "tasks" or not adult(name):
                            return self.respond(403, {"error": "Only parents can reopen tasks"})
                        if not editable(r, name):
                            return self.respond(403, {"error": "You cannot reopen this item"})
                        if r.get("status") not in {"done", "missed"}:
                            raise ValueError("Only completed or not-completed tasks can be reopened")
                        reopened_at = stamp()
                        r.update(status="open", reopenedAt=reopened_at, reopenedBy=name, updatedAt=reopened_at, updatedBy=name)
                        event_details = {"previousStatus": previous.get("status"), "status": "open"}
                    elif action == "deadline_done":
                        if kind != "dance" or r.get("danceType") != "competition":
                            raise ValueError("This is not a competition")
                        deadline_id = payload.get("deadlineId")
                        if not isinstance(deadline_id, (str, int)) or isinstance(deadline_id, bool):
                            raise ValueError("Invalid competition deadline")
                        deadline_id = str(deadline_id)
                        deadline = next((item for item in r.get("deadlines", []) if item.get("id") == deadline_id), None)
                        if not deadline:
                            return self.respond(404, {"error": "Competition deadline unavailable"})
                        if deadline.get("completed"):
                            c.commit()
                            return self.respond(200, {"ok": True})
                        deadline["completed"] = True
                        completed_at = stamp()
                        r.update(updatedAt=completed_at, updatedBy=name)
                        event_details = {"deadlineId": deadline_id, "completedAt": completed_at}
                        action = "deadline_completed"
                        for linked_event in c.execute("SELECT id,body FROM records WHERE kind='events' AND deleted=0").fetchall():
                            event = json.loads(linked_event["body"])
                            if event.get("sourceDanceId") == rid and event.get("sourceDanceKey") == "deadline:" + deadline_id:
                                c.execute("UPDATE reminders SET dismissed_at=COALESCE(dismissed_at,?) WHERE source_kind='events' AND source_id=?", (completed_at, linked_event["id"]))
                    elif action == "decision":
                        if kind != "requests" or not adult(name):
                            return self.respond(403, {"error": "Only parents can decide requests"})
                        status = payload.get("status")
                        if status not in {"Approved", "Denied"}:
                            raise ValueError("Invalid decision")
                        reply = payload.get("reply", "")
                        if not isinstance(reply, str) or len(reply) > 1000:
                            raise ValueError("Invalid reply")
                        r.setdefault("responses", [])
                        r["responses"].append({"by": name, "status": status, "reply": reply, "createdAt": stamp()})
                        r.update(status=status, reply=reply, replyBy=name, updatedAt=stamp(), updatedBy=name)
                        if status == "Approved" and payload.get("addToCalendar") is True:
                            event_date = r.get("date") or r.get("requestedDate") or r.get("dueDate")
                            if event_date:
                                event_title = r.get("title") or r.get("text") or "Request"
                                existing = [json.loads(row["body"]) for row in c.execute("SELECT body FROM records WHERE kind='events' AND deleted=0").fetchall()]
                                already = any(item.get("title") == event_title and item.get("date") == event_date and item.get("sourceRequestId") == rid for item in existing)
                                if not already:
                                    event = {
                                        "title": event_title,
                                        "description": r.get("description") or r.get("text") or "",
                                        "date": event_date,
                                        "startTime": r.get("time") or "",
                                        "endTime": "",
                                        "allDay": not bool(r.get("time")),
                                        "location": "",
                                        "who": "Everyone",
                                        "people": ["Dad", "Mom", "Daughter"],
                                        "visibility": "Family",
                                        "category": "Family",
                                        "repeat": "One Time",
                                        "customIntervalDays": 1,
                                        "reminderOffsets": [],
                                        "sourceRequestId": rid,
                                        "creator": name,
                                        "by": name,
                                        "createdAt": stamp(),
                                        "updatedAt": stamp(),
                                    }
                                    event = event_fields(event, name)
                                    event.update(creator=name, by=name, createdAt=stamp(), updatedAt=stamp(), sourceRequestId=rid)
                                    event_cursor = c.execute("INSERT INTO records(kind,body) VALUES(?,?)", ("events", json.dumps(event)))
                                    audit_record(c, name, event_cursor.lastrowid, "create", {**event, "id": event_cursor.lastrowid})
                    elif action == "reply":
                        if kind != "requests" or name != r.get("creator") and not adult(name):
                            return self.respond(403, {"error": "Only the requester or a parent can reply"})
                        reply = payload.get("reply", "")
                        if not isinstance(reply, str) or not reply.strip() or len(reply) > 1000:
                            raise ValueError("Write a reply of at most 1000 characters")
                        created_at = stamp()
                        r.setdefault("responses", []).append({"by": name, "status": "Message", "reply": reply.strip(), "createdAt": created_at})
                        r.update(reply=reply.strip(), replyBy=name, updatedAt=created_at, updatedBy=name)
                        action = "request_reply"
                    elif action == "react":
                        if kind != "posts" or payload.get("emoji") not in {"❤️", "👍", "😂", "🎉"}:
                            raise ValueError("Invalid reaction")
                        emoji = payload["emoji"]
                        r.setdefault("reactors", {})
                        r.setdefault("reacts", {})
                        people = set(r["reactors"].get(emoji, []))
                        people.symmetric_difference_update({name})
                        r["reactors"][emoji] = sorted(people)
                        r["reacts"][emoji] = len(people)
                    elif action == "comment":
                        if kind != "posts":
                            raise ValueError("This is not a board post")
                        text = payload.get("comment", "")
                        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                            raise ValueError("Write a short comment")
                        r.setdefault("comments", [])
                        r["comments"].append({"id": max((item.get("id", 0) for item in r["comments"]), default=0) + 1, "by": name, "text": text.strip(), "createdAt": stamp()})
                    elif action == "delete_comment":
                        if kind != "posts":
                            raise ValueError("This is not a board post")
                        comment_id = payload.get("commentId")
                        if type(comment_id) is not int:
                            raise ValueError("Invalid comment ID")
                        comments = r.get("comments", [])
                        match = next((item for item in comments if item.get("id") == comment_id), None)
                        if not match:
                            raise ValueError("Comment not found")
                        if match.get("by") != name and not adult(name):
                            return self.respond(403, {"error": "You cannot remove this comment"})
                        r["comments"] = [item for item in comments if item.get("id") != comment_id]
                    elif action == "pin":
                        if kind != "posts" or not adult(name):
                            return self.respond(403, {"error": "Only parents can pin posts"})
                        r["pinned"] = bool(payload.get("pinned", True))
                        r["pinnedBy"] = name if r["pinned"] else ""
                    elif action == "ack":
                        if kind != "posts":
                            raise ValueError("This is not a board post")
                        if not r.get("important") or not r.get("ackRequired"):
                            return self.respond(403, {"error": "This announcement does not require acknowledgement"})
                        people = set(r.get("acknowledgedBy", []))
                        people.add(name)
                        r["acknowledgedBy"] = sorted(people)
                        r["acknowledgedAt"] = stamp()
                    elif action == "check":
                        if kind not in {"lists", "dance"}:
                            raise ValueError("Cannot check this item")
                        if kind == "dance" and r.get("danceType"):
                            raise ValueError("Use checklist item controls for structured dance checklists")
                        r["checked"] = not r["checked"]
                    elif action == "checklist_item":
                        if kind != "dance" or r.get("danceType") != "checklist":
                            raise ValueError("This is not a dance checklist")
                        item_id = payload.get("itemId")
                        if type(item_id) is not int:
                            raise ValueError("Invalid checklist item")
                        item = next((item for item in r.get("checklistItems", []) if item.get("id") == item_id), None)
                        if not item:
                            raise ValueError("Checklist item unavailable")
                        item["checked"] = not item["checked"]
                    else:
                        if action not in {"edit", "recurring_occurrence_edited", "recurring_series_changed", "reassigned", "reopen"}:
                            raise ValueError("Unknown action")
                    if kind == "tasks" and action not in {"edit", "recurring_occurrence_edited", "recurring_series_changed", "reassigned", "reopen"}:
                        r["updatedAt"] = stamp()
                        r["updatedBy"] = name
                    if kind == "dance" and action == "checklist_item":
                        r["updatedAt"] = stamp()
                        r["updatedBy"] = name
                    c.execute("UPDATE records SET body=? WHERE id=?", (json.dumps(r), rid))
                snapshot = {**r, "id": rid}
                audit_record(c, name, rid, action, snapshot, event_details)
                for target_id, audit_action, details in extra_audit_events:
                    if target_id == rid and audit_action == "edit":
                        continue
                    target_row = c.execute("SELECT body FROM records WHERE id=?", (target_id,)).fetchone()
                    if target_row:
                        audit_record(c, name, target_id, audit_action, {**json.loads(target_row["body"]), "id": target_id}, details)
                if recurring_series_created:
                    audit_record(c, name, rid, "recurring_series_created", snapshot,
                                 {"repeat": r.get("repeat"), "startDate": r.get("occurrenceDate")})
                if action == "edit" and "who" in event_details.get("after", {}):
                    audit_record(c, name, rid, "reassigned", snapshot, event_details)
                if action == "edit" and {"dueDate", "dueTime"} & event_details.get("after", {}).keys():
                    audit_record(c, name, rid, "due_date", snapshot, event_details)
                c.commit()
                return self.respond(200, {"ok": True})
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            self.respond(400, {"error": str(error)})

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", choices=["Dad", "Mom", "Daughter"], help="Create/reset a family account interactively")
    args = parser.parse_args()
    initialize()
    if args.user:
        import getpass
        password = getpass.getpass("New password (at least 12 characters): ")
        if len(password) < 12:
            raise SystemExit("Use at least 12 characters")
        if password != getpass.getpass("Confirm password: "):
            raise SystemExit("Passwords do not match")
        salt = secrets.token_hex(16)
        with connection() as c:
            c.execute("INSERT INTO users(name,salt,hash,display_name) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET salt=excluded.salt, hash=excluded.hash", (args.user, salt, password_hash(password, salt), DEFAULT_DISPLAY_NAMES[args.user]))
            c.execute("DELETE FROM sessions WHERE name=?", (args.user,))
        print("Account saved; previous sessions revoked.")
    else:
        ThreadingHTTPServer((os.environ.get("HUB_HOST", "127.0.0.1"), int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
