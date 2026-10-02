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
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
DB = os.environ.get("HUB_DB", str(ROOT / "data" / "hub.sqlite3"))
KINDS = {"tasks", "posts", "requests", "events", "lists", "dance", "notes"}
VISIBILITY = {"Family", "Adults", "Assigned", "Me"}
ACCOUNT_ROLES = {"Dad": "ADMIN", "Mom": "ADMIN", "Daughter": "CHILD"}
DEFAULT_DISPLAY_NAMES = {"Dad": "Jermaine", "Mom": "Stephanie", "Daughter": "Arielle"}
TASK_CATEGORIES = {"Home", "Dance", "School", "Bills", "Shopping", "Appointments", "Work", "Other"}
TASK_REPEATS = {"One Time", "Daily", "Weekdays", "Weekly", "Monthly", "Custom"}
TASK_PRIORITIES = {"Normal", "Important", "High Priority", "Urgent"}
TASK_FIELDS = ("title", "description", "who", "priority", "visibility", "category", "dueDate", "dueTime", "repeat", "ack")
TASK_MISS_REASONS = {"Ran out of time", "Waiting on someone/something", "Rescheduled", "No longer needed", "Other"}
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
        for account, display_name in DEFAULT_DISPLAY_NAMES.items():
            c.execute("UPDATE users SET display_name=? WHERE name=? AND display_name=''", (display_name, account))

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
    if action == "create":
        summary = f"📝 {actor} created \"{title}\""
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
                    if name == "Daughter" and snapshot.get("visibility") != "Family":
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
                if self.path != "/api/action":
                    return self.respond(404, {"error": "Not found"})
                c.execute("BEGIN IMMEDIATE")
                action = payload.get("action")
                event_details = {}
                if action == "create":
                    kind = payload.get("kind")
                    if kind not in KINDS:
                        raise ValueError("Unknown item type")
                    if kind == "tasks" and not adult(name):
                        return self.respond(403, {"error": "Only parents create tasks"})
                    r = payload.get("record", {})
                    if not isinstance(r, dict):
                        raise ValueError("Invalid item")
                    r = {key: r[key] for key in ("title", "text", "description", "date", "dueDate", "dueTime", "who", "priority", "visibility", "type", "category", "repeat", "ack") if key in r}
                    for key in ("title", "text", "description", "date", "dueDate", "dueTime", "type", "category", "repeat"):
                        if key in r and (not isinstance(r[key], str) or len(r[key]) > 4000):
                            raise ValueError("Invalid text")
                    if not (r.get("title", "").strip() or r.get("text", "").strip()):
                        raise ValueError("Please enter some text")
                    created_at = stamp()
                    r.update(creator=name, by=name, createdAt=created_at, updatedAt=created_at)
                    r.setdefault("visibility", "Family")
                    if r["visibility"] not in VISIBILITY or r["visibility"] == "Adults" and not adult(name):
                        raise ValueError("Invalid visibility")
                    if r.get("who", "Everyone") not in {"Dad", "Mom", "Daughter", "Everyone", ""}:
                        raise ValueError("Invalid assignee")
                    if r["visibility"] == "Assigned" and r.get("who") not in {"Dad", "Mom", "Daughter"}:
                        raise ValueError("Choose one assigned person")
                    if kind == "tasks":
                        r.update(task_fields(r, name))
                        r.update(status="open", acked=[], acknowledgements=[], reason="", completionHistory=[], notCompletedHistory=[])
                    if kind == "requests":
                        r.update(status="Pending", reply="", visibility="Family")
                    if kind == "posts":
                        r["reacts"] = {}
                        r["reactors"] = {}
                    if kind in {"lists", "dance"}:
                        r["checked"] = False
                    cursor = c.execute("INSERT INTO records(kind,body) VALUES(?,?)", (kind, json.dumps(r)))
                    rid = cursor.lastrowid
                else:
                    if type(payload.get("id")) is not int:
                        raise ValueError("Invalid item ID")
                    row = c.execute("SELECT * FROM records WHERE id=? AND deleted=0", (payload["id"],)).fetchone()
                    if not row or not visible(json.loads(row["body"]), name):
                        return self.respond(404, {"error": "Item unavailable"})
                    rid, kind, r = row["id"], row["kind"], json.loads(row["body"])
                    previous = dict(r)
                    if action == "delete":
                        if kind == "tasks" and not adult(name):
                            return self.respond(403, {"error": "Only parents can delete tasks"})
                        if not editable(r, name):
                            return self.respond(403, {"error": "You cannot delete this item"})
                        deleted_at = stamp()
                        r.update(deletedAt=deleted_at, deletedBy=name, updatedAt=deleted_at, updatedBy=name)
                        c.execute("UPDATE records SET body=?, deleted=1 WHERE id=?", (json.dumps(r), rid))
                        audit_record(c, name, rid, "delete", {**r, "id": rid})
                        c.commit()
                        return self.respond(200, {"ok": True})
                    if action == "edit":
                        if kind != "tasks" or not adult(name):
                            return self.respond(403, {"error": "Only parents can edit tasks"})
                        if not editable(r, name):
                            return self.respond(403, {"error": "You cannot edit this item"})
                        changes = payload.get("record")
                        if not isinstance(changes, dict) or set(changes) - set(TASK_FIELDS):
                            raise ValueError("Invalid task changes")
                        updated = task_fields(changes, name, r)
                        event_details = {"before": {}, "after": {}}
                        for field in TASK_FIELDS:
                            if updated.get(field) != r.get(field):
                                event_details["before"][field] = r.get(field)
                                event_details["after"][field] = updated.get(field)
                        action = "edit"
                        r.update(updated, updatedAt=stamp(), updatedBy=name)
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
                        if action not in {"edit", "reassigned", "reopen"}:
                            raise ValueError("Unknown action")
                    if kind == "tasks" and action not in {"edit", "reassigned", "reopen"}:
                        r["updatedAt"] = stamp()
                        r["updatedBy"] = name
                    c.execute("UPDATE records SET body=? WHERE id=?", (json.dumps(r), rid))
                snapshot = {**r, "id": rid}
                audit_record(c, name, rid, action, snapshot, event_details)
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
