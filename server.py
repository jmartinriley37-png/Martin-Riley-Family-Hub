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
KINDS = {"tasks", "posts", "requests", "events", "lists", "dance", "notes"}
VISIBILITY = {"Family", "Adults", "Assigned", "Me"}
ACCOUNT_ROLES = {"Dad": "ADMIN", "Mom": "ADMIN", "Daughter": "CHILD"}
DEFAULT_DISPLAY_NAMES = {"Dad": "Jermaine", "Mom": "Stephanie", "Daughter": "Arielle"}
TASK_CATEGORIES = {"Home", "Dance", "School", "Bills", "Shopping", "Appointments", "Work", "Other"}
TASK_REPEATS = {"One Time", "Daily", "Weekdays", "Weekly", "Monthly", "Custom"}
TASK_PRIORITIES = {"Normal", "Important", "High Priority", "Urgent"}
TASK_FIELDS = ("title", "description", "who", "priority", "visibility", "category", "dueDate", "dueTime", "repeat", "ack", "customIntervalDays", "reminderOffsets")
TASK_MISS_REASONS = {"Ran out of time", "Waiting on someone/something", "Rescheduled", "No longer needed", "Other"}
EVENT_CATEGORIES = {"Family", "Appointments", "School", "Dance", "Work", "Birthdays", "Travel", "Competitions", "Other"}
REMINDER_OFFSETS = {0, 15, 30, 60, 120, 1440}
RECURRENCE_HORIZON_DAYS = 90
RECURRENCE_MAX_OCCURRENCES = 120
STATIC = {"index.html", "styles.css", "app.js", "manifest.json", "sw.js", "icon.svg", "icon-192.png", "icon-512.png"}

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

def reminder_state(c, account, state):
    sources = {("tasks", item["id"]): item for item in state["tasks"]}
    sources.update({("events", item["id"]): item for item in state["events"]})
    result = []
    now = dt.datetime.now(dt.timezone.utc)
    for row in c.execute("SELECT * FROM reminders WHERE account=? ORDER BY due_at DESC LIMIT 200", (account,)):
        source = sources.get((row["source_kind"], row["source_id"]))
        if not source or row["source_kind"] == "tasks" and source.get("status") in {"done", "missed"} and not row["dismissed_at"]:
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
            "snoozed": snoozed, "snoozedUntil": snoozed_until,
            "title": source.get("title", source.get("text", "")), "description": source.get("description", ""),
            "who": source.get("who", ""), "priority": source.get("priority", ""),
            "category": source.get("category", ""), "visibility": source.get("visibility", "Family"),
            "date": source.get("date", source.get("dueDate", "")),
            "time": source.get("startTime", source.get("dueTime", "")),
        })
    return result

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

def activity_entry(row, snapshot, profiles, viewer):
    details = json.loads(row["details"] or "{}")
    before = details.get("before")
    if before and (not visible(before, viewer) or viewer == "Daughter" and before.get("visibility") != "Family"):
        details = {}
    actor = profiles.get(row["actor"], {}).get("displayName", row["actor"])
    title = snapshot.get("title") or snapshot.get("text") or "an item"
    action = row["action"]
    item_type = "event" if "allDay" in snapshot or "startTime" in snapshot or snapshot.get("seriesKind") == "events" else "task"
    if action == "create":
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
                        state[row["kind"]].append(record)
                        allowed.add(row["id"])
                profiles = profile_data(c)
                activity = []
                for event in c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 500"):
                    snapshot = json.loads(event["snapshot"] or "{}")
                    if not snapshot and event["record_id"]:
                        record_row = c.execute("SELECT body FROM records WHERE id=?", (event["record_id"],)).fetchone()
                        if record_row:
                            snapshot = json.loads(record_row["body"])
                    if not snapshot or not visible(snapshot, name):
                        continue
                    activity.append(activity_entry(event, snapshot, profiles, name))
                    if len(activity) == 50:
                        break
                state["activity"] = activity
                completions = []
                for task in state["tasks"]:
                    if task.get("category") != "Home":
                        continue
                    history = task.get("completionHistory", [])
                    if not history and task.get("status") == "done" and task.get("completedAt"):
                        history = [{"completedAt": task["completedAt"], "completedBy": task.get("completedBy")}]
                    completions.extend(event for event in history if event.get("completedBy") == "Daughter")
                days = {event.get("completedAt", "")[:10] for event in completions}
                day = dt.datetime.now(dt.timezone.utc).date()
                if day.isoformat() not in days:
                    day -= dt.timedelta(days=1)
                streak = 0
                while day.isoformat() in days:
                    streak += 1
                    day -= dt.timedelta(days=1)
                state.update(viewer=name, points=len(completions), streak=streak, profiles=profiles)
                state["timezone"] = HUB_TIMEZONE
                generate_reminders(c, profiles)
                state["reminders"] = reminder_state(c, name, state)
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
                    reminder_id = payload.get("id")
                    if type(reminder_id) is not int:
                        raise ValueError("Invalid reminder ID")
                    reminder = c.execute("SELECT * FROM reminders WHERE id=? AND account=?", (reminder_id, name)).fetchone()
                    if not reminder:
                        return self.respond(404, {"error": "Reminder unavailable"})
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
                    r = {key: r[key] for key in ("title", "text", "description", "date", "startTime", "endTime", "allDay", "location", "people", "customIntervalDays", "reminderOffsets", "dueDate", "dueTime", "who", "priority", "visibility", "type", "category", "repeat", "ack") if key in r}
                    for key in ("title", "text", "description", "date", "startTime", "endTime", "location", "dueDate", "dueTime", "type", "category", "repeat"):
                        if key in r and (not isinstance(r[key], str) or len(r[key]) > 4000):
                            raise ValueError("Invalid text")
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
                    if kind == "requests":
                        r.update(status="Pending", reply="", visibility="Family")
                    if kind == "posts":
                        r["reacts"] = {}
                        r["reactors"] = {}
                    if kind in {"lists", "dance"}:
                        r["checked"] = False
                    cursor = c.execute("INSERT INTO records(kind,body) VALUES(?,?)", (kind, json.dumps(r)))
                    rid = cursor.lastrowid
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
                        if series and scope == "this":
                            audit_record(c, name, rid, "recurrence_occurrence_deleted", {**r, "id": rid}, {"scope": scope})
                        c.commit()
                        return self.respond(200, {"ok": True})
                    if action == "edit":
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
                    elif action == "decision":
                        if kind != "requests" or not adult(name) or r["status"] != "Pending":
                            return self.respond(403, {"error": "Only parents can decide pending requests"})
                        if payload.get("status") not in {"Approved", "Denied"}:
                            raise ValueError("Invalid decision")
                        reply = payload.get("reply", "")
                        if not isinstance(reply, str) or len(reply) > 1000:
                            raise ValueError("Invalid reply")
                        r.update(status=payload["status"], reply=reply, replyBy=name)
                    elif action == "react":
                        if kind != "posts" or payload.get("emoji") not in {"❤️", "👍", "😂", "🎉"}:
                            raise ValueError("Invalid reaction")
                        emoji = payload["emoji"]
                        people = set(r["reactors"].get(emoji, []))
                        people.symmetric_difference_update({name})
                        r["reactors"][emoji] = sorted(people)
                        r["reacts"][emoji] = len(people)
                    elif action == "check":
                        if kind not in {"lists", "dance"}:
                            raise ValueError("Cannot check this item")
                        r["checked"] = not r["checked"]
                    else:
                        if action not in {"edit", "recurring_occurrence_edited", "recurring_series_changed", "reassigned", "reopen"}:
                            raise ValueError("Unknown action")
                    if kind == "tasks" and action not in {"edit", "recurring_occurrence_edited", "recurring_series_changed", "reassigned", "reopen"}:
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
