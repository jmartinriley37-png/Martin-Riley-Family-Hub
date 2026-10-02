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
        CREATE TABLE IF NOT EXISTS users(name TEXT PRIMARY KEY, salt TEXT NOT NULL, hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, name TEXT REFERENCES users(name), expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS records(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, actor TEXT, record_id INTEGER REFERENCES records(id), action TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS attempts(address TEXT PRIMARY KEY, count INTEGER, reset INTEGER);
        """)

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

def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()

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
        if path == "/api/state":
            with connection() as c:
                name = self.identity(c)
                if not name:
                    return self.respond(401, {"error": "Please sign in."})
                state = {k: [] for k in KINDS}
                allowed = set()
                for row in c.execute("SELECT * FROM records"):
                    record = json.loads(row["body"])
                    record["id"] = row["id"]
                    if visible(record, name):
                        state[row["kind"]].append(record)
                        allowed.add(row["id"])
                state["activity"] = [f'{r["actor"]} {r["action"]} · {r["created"][:16]}'
                    for r in c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 500") if r["record_id"] in allowed][:50]
                done = [t for t in state["tasks"] if t["status"] == "done" and t["who"] == "Daughter"]
                days = {t.get("completedAt", "")[:10] for t in done}
                day = dt.datetime.now(dt.timezone.utc).date()
                if day.isoformat() not in days:
                    day -= dt.timedelta(days=1)
                streak = 0
                while day.isoformat() in days:
                    streak += 1
                    day -= dt.timedelta(days=1)
                state.update(viewer=name, points=len(done), streak=streak)
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
                if action == "create":
                    kind = payload.get("kind")
                    if kind not in KINDS:
                        raise ValueError("Unknown item type")
                    if kind == "tasks" and not adult(name):
                        return self.respond(403, {"error": "Only parents create tasks"})
                    r = payload.get("record", {})
                    if not isinstance(r, dict):
                        raise ValueError("Invalid item")
                    r = {key: r[key] for key in ("title", "text", "date", "who", "priority", "visibility", "type", "category", "ack") if key in r}
                    for key in ("title", "text", "date", "type", "category"):
                        if key in r and (not isinstance(r[key], str) or len(r[key]) > 4000):
                            raise ValueError("Invalid text")
                    if not (r.get("title", "").strip() or r.get("text", "").strip()):
                        raise ValueError("Please enter some text")
                    r.update(creator=name, by=name, createdAt=stamp())
                    r.setdefault("visibility", "Family")
                    if r["visibility"] not in VISIBILITY or r["visibility"] == "Adults" and not adult(name):
                        raise ValueError("Invalid visibility")
                    if r.get("who", "Everyone") not in {"Dad", "Mom", "Daughter", "Everyone"}:
                        raise ValueError("Invalid assignee")
                    if r["visibility"] == "Assigned" and r.get("who") not in {"Dad", "Mom", "Daughter"}:
                        raise ValueError("Choose one assigned person")
                    if kind == "tasks":
                        if r.get("priority") not in {"Normal", "Important", "High", "Urgent"}:
                            raise ValueError("Invalid priority")
                        r.update(status="open", ack=r["priority"] == "Urgent" or bool(r.get("ack")), acked=[], reason="")
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
                    row = c.execute("SELECT * FROM records WHERE id=?", (payload["id"],)).fetchone()
                    if not row or not visible(json.loads(row["body"]), name):
                        return self.respond(404, {"error": "Item unavailable"})
                    rid, kind, r = row["id"], row["kind"], json.loads(row["body"])
                    if action in {"ack", "done", "miss"}:
                        if kind != "tasks" or r["status"] != "open" or r["who"] not in {name, "Everyone"}:
                            return self.respond(403, {"error": "This task is not assigned to you"})
                        if action == "ack":
                            r["acked"] = list(set(r["acked"] + [name]))
                        elif action == "done":
                            if r["ack"] and name not in r["acked"]:
                                raise ValueError("Acknowledge this task first")
                            r.update(status="done", completedAt=stamp())
                        else:
                            reason = payload.get("reason", "")
                            if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
                                raise ValueError("Please give a reason")
                            r.update(status="missed", reason=reason)
                    elif action == "decision":
                        if kind != "requests" or not adult(name) or r["status"] != "Pending":
                            return self.respond(403, {"error": "Only parents can decide pending requests"})
                        if payload.get("status") not in {"Approved", "Denied"}:
                            raise ValueError("Invalid decision")
                        reply = payload.get("reply", "")
                        if not isinstance(reply, str) or len(reply) > 1000:
                            raise ValueError("Invalid reply")
                        r.update(status=payload["status"], reply=name + ": " + reply)
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
                    elif action == "delete":
                        if not editable(r, name):
                            return self.respond(403, {"error": "You cannot delete this item"})
                        c.execute("DELETE FROM audit WHERE record_id=?", (rid,))
                        c.execute("DELETE FROM records WHERE id=?", (rid,))
                        c.commit()
                        return self.respond(200, {"ok": True})
                    else:
                        raise ValueError("Unknown action")
                    c.execute("UPDATE records SET body=? WHERE id=?", (json.dumps(r), rid))
                c.execute("INSERT INTO audit(actor,record_id,action,created) VALUES(?,?,?,?)", (name, rid, action, stamp()))
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
            c.execute("INSERT INTO users VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET salt=excluded.salt, hash=excluded.hash", (args.user, salt, password_hash(password, salt)))
            c.execute("DELETE FROM sessions WHERE name=?", (args.user,))
        print("Account saved; previous sessions revoked.")
    else:
        ThreadingHTTPServer((os.environ.get("HUB_HOST", "127.0.0.1"), int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
