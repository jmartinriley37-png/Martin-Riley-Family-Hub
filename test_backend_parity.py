"""Backend parity, failure-mode and query-budget tests for the HTTP/API layer. All data is synthetic.

The same scripted family scenario runs through the real request handlers on SQLite and on PostgreSQL and the
normalised responses must be identical. PostgreSQL parts need HUB_TEST_POSTGRES_URL (a disposable database; every
test gets a private schema) and are reported as skipped without it.
"""
import contextlib
import datetime
import io
import json
import os
import re
import tempfile
import threading
import unittest
from unittest import mock

import server
import test_server as ts
from persistence.errors import SchemaNotReady, StorageConflict, StorageError
from persistence.repository import Repository

PG_AVAILABLE = bool(ts.PG_URL)
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\+00:00|Z)?")


class Harness(unittest.TestCase):
    """Borrows the real-handler helpers from the main HTTP suite."""
    call, client, create = ts.FamilyPrivacyTests.call, ts.FamilyPrivacyTests.client, ts.FamilyPrivacyTests.create

    def runTest(self):
        pass


def normalise(value):
    text = TIMESTAMP.sub("TS", json.dumps(value, sort_keys=True))
    return json.loads(text)


def find(items, title):
    return next(item for item in items if item.get("title") == title or item.get("text") == title)


SECRETS = (
    "Vault secret", "ACCT-99887766", "Dad private journal", "Mom private journal", "Dad Me Only task",
    "Mom Me Only task", "Adults bills", "Adults only event", "FEE-LABEL-7391", "AMT-7391", "secret-coach-note",
    "Parents only budget note", "Adult dance budget",
)


def run_scenario():
    """Drive every feature area through the HTTP handlers. Returns a normalised transcript plus final state."""
    h = Harness()
    h.maxDiff = None
    dad, mom, kid = h.client("Dad"), h.client("Mom"), h.client("Daughter")
    today = server.local_today()
    day = lambda n: (today + datetime.timedelta(days=n)).isoformat()
    transcript = []

    def go(label, who, path, data=None):
        status, body = h.call(who, path, data)
        transcript.append([label, status, body if path != "state" else None])
        return status, body

    def make(label, actor, kind, **record):
        return go(label, actor, "action", dict(action="create", kind=kind, record=record))

    def state(who):
        return h.call(who, "state")[1]

    # authorization rejections come first so they are part of the transcript
    make("kid cannot create task", kid, "tasks", title="Nope", who="Daughter")
    make("kid cannot create event", kid, "events", title="Nope", date=day(1))
    make("kid cannot create adults visibility", kid, "posts", text="Nope", visibility="Adults")
    make("kid cannot give recognition", kid, "recognitions", recognitionType="Great Job")

    make("family chore", dad, "tasks", title="Family chore", who="Daughter", category="Home", priority="Normal",
         visibility="Family", dueDate=day(0), reminderOffsets=[0], ack=True)
    make("adults task", dad, "tasks", title="Adults bills", visibility="Adults", category="Bills", dueDate=day(2))
    make("assigned task", dad, "tasks", title="Assigned to Arielle", visibility="Assigned", who="Daughter", dueDate=day(1))
    make("dad me task", dad, "tasks", title="Dad Me Only task", visibility="Me")
    make("mom me task", mom, "tasks", title="Mom Me Only task", visibility="Me")
    make("weekly task", dad, "tasks", title="Weekly trash", who="Daughter", category="Home", dueDate=day(0),
         repeat="Weekly", reminderOffsets=[])
    make("family event", mom, "events", title="Family dinner", date=day(1), startTime="18:00", endTime="19:00")
    make("adults event", mom, "events", title="Adults only event", date=day(2), visibility="Adults")
    make("recurring event", dad, "events", title="Monthly meetup", date=day(3), repeat="Monthly")
    make("vault note", dad, "notes", title="Vault secret", space="Vault", category="Bills / Financial Notes",
         notes="ACCT-99887766 balance")
    make("dad me note", dad, "notes", title="Dad private journal", space="Me", category="Personal")
    make("mom me note", mom, "notes", title="Mom private journal", space="Me", category="Personal", reminderDate=day(0), reminderOffsets=[0])
    make("competition", dad, "dance", danceType="competition", title="Winter Classic", startDate=day(3), endDate=day(4),
         venue="Arena", schedulePending=True, notes="Parents only budget note",
         fees=[{"label": "FEE-LABEL-7391", "amount": "AMT-7391"}], financials={"total": "AMT-7391"},
         deadlines=[{"title": "Pay entry", "date": day(1)}])
    make("adult dance budget", dad, "dance", title="Adult dance budget", visibility="Adults", danceType="competition",
         fees=[{"label": "FEE-LABEL-7391"}])
    make("routine", dad, "dance", danceType="routine", title="Solo", routineType="Solo")
    make("costume", dad, "dance", danceType="costume", title="Costume")
    make("checklist", dad, "dance", danceType="checklist", title="Packing", checklistItems=["Shoes", "Tights"])
    make("kid cannot create dance", kid, "dance", danceType="routine", title="Nope")
    make("maddox activity", dad, "activities", memberId="Maddox", activityName="Baseball", activityType="Baseball",
         eventType="Practice", date=day(1), startTime="17:00", endTime="18:00", parentNotes="secret-coach-note",
         reminderOffsets=[1440])
    make("kid cannot create activity", kid, "activities", memberId="Maddox", activityName="x", activityType="x", date=day(1))
    make("important post", dad, "posts", text="Dentist Friday", important=True, ackRequired=True)
    make("shared list", dad, "lists", text="Groceries")
    make("kid request", kid, "requests", type="Permission", text="Can I go to the game?", date=day(2), time="18:30", description="Dad please")

    dad_state = state(dad)
    chore = find(dad_state["tasks"], "Family chore")
    post = find(dad_state["posts"], "Dentist Friday")
    shared = find(dad_state["lists"], "Groceries")
    request = find(dad_state["requests"], "Can I go to the game?")
    competition = find(dad_state["dance"], "Winter Classic")
    routine = find(dad_state["dance"], "Solo")
    checklist = find(dad_state["dance"], "Packing")
    weekly = sorted((t for t in dad_state["tasks"] if t["title"] == "Weekly trash"), key=lambda t: t["id"])

    go("kid done before ack", kid, "action", {"action": "done", "id": chore["id"]})
    go("kid ack", kid, "action", {"action": "ack", "id": chore["id"]})
    go("kid done", kid, "action", {"action": "done", "id": chore["id"]})
    go("dad reopen", dad, "action", {"action": "reopen", "id": chore["id"]})
    go("kid done again", kid, "action", {"action": "done", "id": chore["id"]})
    make("recognition", dad, "recognitions", recognitionType="Great Job", message="Thanks", sourceTaskId=chore["id"])
    recognition = find(state(kid)["recognitions"], "Great Job")
    go("mom cannot mark recognition seen", mom, "recognitions/action", {"id": recognition["id"], "action": "seen"})
    go("kid recognition seen", kid, "recognitions/action", {"id": recognition["id"], "action": "seen"})
    go("post ack", kid, "action", {"action": "ack", "id": post["id"]})
    go("post react", mom, "action", {"action": "react", "id": post["id"], "emoji": "👍"})
    go("post comment", mom, "action", {"action": "comment", "id": post["id"], "comment": "See you there"})
    go("post pin", dad, "action", {"action": "pin", "id": post["id"], "pinned": True})
    go("kid cannot pin", kid, "action", {"action": "pin", "id": post["id"], "pinned": True})
    go("list check", kid, "action", {"action": "check", "id": shared["id"]})
    go("request decision", dad, "action", {"action": "decision", "id": request["id"], "status": "Approved", "reply": "Yes", "addToCalendar": True})
    go("kid cannot decide", kid, "action", {"action": "decision", "id": request["id"], "status": "Denied"})
    go("checklist item", dad, "action", {"action": "checklist_item", "id": checklist["id"], "itemId": 1})
    go("deadline done", dad, "action", {"action": "deadline_done", "id": competition["id"], "deadlineId": "1"})
    go("edit routine", dad, "action", {"action": "edit", "id": routine["id"], "record": {"choreographer": "Coach A"}})
    go("edit competition", dad, "action", {"action": "edit", "id": competition["id"], "record": {"venue": "Main Arena", "endDate": day(5)}})
    go("kid cannot edit dance", kid, "action", {"action": "edit", "id": routine["id"], "record": {"notes": "x"}})
    go("edit weekly series", dad, "action", {"action": "edit", "id": weekly[1]["id"], "scope": "series", "record": {"title": "Weekly trash v2"}})
    go("edit weekly future", dad, "action", {"action": "edit", "id": weekly[3]["id"], "scope": "future", "record": {"who": "Dad"}})
    go("delete one occurrence", dad, "action", {"action": "delete", "id": weekly[2]["id"], "scope": "this"})
    go("kid cannot see dad me task", kid, "action", {"action": "delete", "id": find(dad_state["tasks"], "Dad Me Only task")["id"]})
    go("mom cannot touch dad me note", mom, "action", {"action": "edit", "id": find(dad_state["notes"], "Dad private journal")["id"], "record": {"notes": "x"}})

    # reminders and notification badges
    for who, name in ((dad, "dad"), (mom, "mom"), (kid, "kid")):
        current = state(who)
        transcript.append([f"{name} badge counts", 200, current["badgeCounts"]])
    kid_reminders = sorted(state(kid)["reminders"], key=lambda r: r["id"])
    if kid_reminders:
        go("kid snooze", kid, "reminders/action", {"id": kid_reminders[0]["id"], "action": "snooze", "minutes": 60})
    go("kid read category", kid, "reminders/action", {"action": "read_category", "category": "tasks"})
    go("bad reminder category", kid, "reminders/action", {"action": "read_category", "category": "nope"})
    dad_reminders = sorted(state(dad)["reminders"], key=lambda r: r["id"])
    go("dad dismiss reminder", dad, "reminders/action", {"id": dad_reminders[0]["id"], "action": "dismiss"})
    recap = next((r for r in state(mom)["reminders"] if r["type"] == "weekly_recap"), None)
    if recap:
        go("mom dismiss recap", mom, "reminders/action", {"id": recap["id"], "action": "dismiss"})
    go("dad cannot act on mom reminder", dad, "reminders/action", {"id": 987654, "action": "read"})

    # Dance archive, restore, season, delete
    go("archive routine", dad, "action", {"action": "archive", "id": routine["id"]})
    go("restore routine", dad, "action", {"action": "restore", "id": routine["id"]})
    go("kid cannot archive", kid, "action", {"action": "archive", "id": competition["id"]})
    go("archive competition", dad, "action", {"action": "archive", "id": competition["id"]})
    transcript.append(["kid state after archive", 200, [i["title"] for i in state(kid)["dance"]]])
    go("restore competition", dad, "action", {"action": "restore", "id": competition["id"]})
    go("kid cannot archive season", kid, "action", {"action": "archive_season"})
    go("archive season", dad, "action", {"action": "archive_season"})
    go("delete costume", dad, "action", {"action": "delete", "id": find(dad_state["dance"], "Costume")["id"]})
    go("delete competition", dad, "action", {"action": "delete", "id": competition["id"]})

    # Maddox is a managed profile, never an account
    go("maddox cannot sign in", {}, "login", {"name": "Maddox", "password": "testing-password"})
    with ts.raw() as db:
        facts = {
            "accounts": sorted(row["name"] for row in db.execute("SELECT name FROM users")),
            "sessions": sorted({row["name"] for row in db.execute("SELECT name FROM sessions")}),
            "maddox": [dict(row) for row in db.execute("SELECT member_id,member_type,account_name,avatar FROM family_members WHERE member_id='Maddox'")],
            "audit_actions": sorted({row["action"] for row in db.execute("SELECT action FROM audit")}),
        }
    states = {}
    for name, who in (("Dad", dad), ("Mom", mom), ("Daughter", kid)):
        current = state(who)
        current["reminders"] = sorted(current["reminders"], key=lambda r: (r["sourceKind"], r["sourceId"], r["type"], r["id"]))
        states[name] = current
    return {
        "transcript": normalise(transcript), "states": normalise(states), "facts": facts,
        "profiles": h.call({}, "profiles")[1], "raw_daughter": json.dumps(states["Daughter"]),
        "raw_dad": json.dumps(states["Dad"]), "raw_mom": json.dumps(states["Mom"]),
    }


def run_on(backend):
    with tempfile.TemporaryDirectory() as directory:
        cleanups, _ = ts.start_backend(backend, directory)
        try:
            server.initialize()
            ts.seed_accounts()
            return run_scenario()
        finally:
            for cleanup in reversed(cleanups):
                cleanup()


class ScenarioParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {"sqlite": run_on("sqlite")}
        if PG_AVAILABLE:
            cls.results["postgres"] = run_on("postgres")

    def backends(self):
        return list(self.results)

    def test_scenario_exercises_the_expected_features(self):
        sqlite = self.results["sqlite"]
        statuses = {entry[0]: entry[1] for entry in sqlite["transcript"]}
        for label in ("family chore", "kid done", "recognition", "kid recognition seen", "request decision", "archive season",
                      "delete competition", "edit weekly series", "kid snooze", "archive competition", "restore competition"):
            self.assertEqual(statuses[label], 200, label)
        for label in ("kid cannot create task", "kid cannot create event", "kid cannot give recognition", "kid cannot pin",
                      "kid cannot decide", "kid cannot archive", "kid cannot archive season", "kid cannot edit dance"):
            self.assertEqual(statuses[label], 403, label)
        for label in ("kid cannot see dad me task", "mom cannot touch dad me note"):
            self.assertEqual(statuses[label], 404, label)
        dad = sqlite["states"]["Dad"]
        for key in ("tasks", "events", "dance", "recognitions", "requests", "posts", "lists", "activities", "activity", "reminders"):
            self.assertTrue(dad[key], key)
        self.assertTrue(any(event.get("sourceDanceId") for event in dad["events"]) or "delete competition" in statuses)
        self.assertTrue({"done", "archive", "restore", "decision", "recurring_series_changed"} <= set(sqlite["facts"]["audit_actions"]))

    @unittest.skipUnless(PG_AVAILABLE, "Set HUB_TEST_POSTGRES_URL to compare with PostgreSQL")
    def test_transcripts_and_state_are_identical_on_both_backends(self):
        sqlite, postgres = self.results["sqlite"], self.results["postgres"]
        self.maxDiff = None
        self.assertEqual(sqlite["transcript"], postgres["transcript"])
        for viewer in ("Dad", "Mom", "Daughter"):
            self.assertEqual(sqlite["states"][viewer], postgres["states"][viewer], viewer)
        self.assertEqual(sqlite["profiles"], postgres["profiles"])
        self.assertEqual(sqlite["facts"], postgres["facts"])

    def test_arielle_never_receives_adult_private_or_financial_data(self):
        for backend in self.backends():
            daughter = self.results[backend]["raw_daughter"]
            for secret in SECRETS:
                self.assertNotIn(secret, daughter, f"{backend}: {secret}")
            for key in ('"fees"', '"financials"', '"parentNotes"'):
                self.assertNotIn(key, daughter, f"{backend}: {key}")
            self.assertEqual(self.results[backend]["states"]["Daughter"]["badgeCounts"].get("vault", 0), 0)

    def test_each_parents_private_data_is_invisible_to_the_other_parent(self):
        for backend in self.backends():
            result = self.results[backend]
            self.assertIn("Dad private journal", result["raw_dad"])
            self.assertIn("Dad Me Only task", result["raw_dad"])
            self.assertNotIn("Dad private journal", result["raw_mom"])
            self.assertNotIn("Dad Me Only task", result["raw_mom"])
            self.assertIn("Mom private journal", result["raw_mom"])
            self.assertIn("Mom Me Only task", result["raw_mom"])
            self.assertNotIn("Mom private journal", result["raw_dad"])
            self.assertNotIn("Mom Me Only task", result["raw_dad"])

    def test_adult_vault_and_financial_values_are_adult_only(self):
        for backend in self.backends():
            result = self.results[backend]
            for adult in ("raw_dad", "raw_mom"):
                self.assertIn("Vault secret", result[adult])
                self.assertIn("Adults only event", result[adult])

    def test_maddox_is_a_managed_profile_on_every_backend(self):
        for backend in self.backends():
            result = self.results[backend]
            self.assertEqual(result["facts"]["accounts"], ["Dad", "Daughter", "Mom"])
            self.assertEqual(set(result["facts"]["sessions"]) - {"Dad", "Mom", "Daughter"}, set())
            self.assertEqual(result["facts"]["maddox"], [{"member_id": "Maddox", "member_type": "managed_child", "account_name": None, "avatar": "⚾"}])
            self.assertNotIn("Maddox", result["profiles"]["profiles"])
            for viewer in ("Dad", "Mom", "Daughter"):
                member = result["states"][viewer]["familyMembers"]["Maddox"]
                self.assertFalse(member["hasAccount"])
                self.assertEqual(member["profileType"], "managed_child")
            status = {entry[0]: entry[1] for entry in result["transcript"]}["maddox cannot sign in"]
            self.assertNotEqual(status, 200)


class FailureModes:
    backend = None

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.cleanups, _ = ts.start_backend(cls.backend, cls.temp.name)
        server.initialize()
        ts.seed_accounts()
        cls.h = Harness()

    @classmethod
    def tearDownClass(cls):
        for cleanup in reversed(cls.cleanups):
            cleanup()
        cls.temp.cleanup()

    def setUp(self):
        ts.wipe()

    def counts(self):
        with ts.raw() as db:
            return [db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("records", "audit", "recurrence_series", "recurrence_occurrences", "reminders")]

    def test_failed_write_rolls_back_every_part_of_the_request(self):
        dad = self.h.client("Dad")
        before = self.counts()
        with mock.patch.object(server, "audit_record", side_effect=StorageError()), contextlib.redirect_stderr(io.StringIO()):
            status, body = self.h.call(dad, "action", dict(action="create", kind="tasks", record=dict(
                title="Never stored", who="Daughter", category="Home", dueDate=server.local_today().isoformat(), repeat="Weekly")))
        self.assertEqual(status, 503)
        self.assertEqual(body, {"error": StorageError.public_message})
        self.assertEqual(self.counts(), before)

    def test_duplicate_write_is_a_conflict_without_database_details(self):
        with server.repository() as repo:
            task = repo.create_record("tasks", {"title": "t"})
            repo.create_series(task, {}, "2030-01-01", "x", "x", "Dad")
            repo.add_occurrence(task, "2030-01-01", task, 0)
            with self.assertRaises(StorageConflict) as caught:
                repo.add_occurrence(task, "2030-01-01", task, 0)
            message = str(caught.exception).lower()
            for leak in ("recurrence", "unique", "constraint", "sqlite", "postgres", "insert"):
                self.assertNotIn(leak, message)
            repo.rollback()

    def test_concurrent_completion_is_applied_exactly_once(self):
        dad, kid = self.h.client("Dad"), self.h.client("Daughter")
        self.h.create(dad, "tasks", title="One winner", who="Daughter", category="Home", visibility="Family")
        task = find(self.h.call(dad, "state")[1]["tasks"], "One winner")
        results, errors = [], []

        def worker():
            try:
                results.append(self.h.call(dict(kid), "action", {"action": "done", "id": task["id"]})[0])
            except Exception as error:  # pragma: no cover - reported below
                errors.append(repr(error))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [200] + [403] * 7)
        stored = find(self.h.call(dad, "state")[1]["tasks"], "One winner")
        self.assertEqual(len(stored["completionHistory"]), 1)
        with ts.raw() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE record_id=? AND action='done'", (task["id"],)).fetchone()[0], 1)

    def test_concurrent_state_loads_do_not_duplicate_recurrence_or_reminders(self):
        dad = self.h.client("Dad")
        today = server.local_today().isoformat()
        self.h.create(dad, "tasks", title="Repeater", who="Daughter", category="Home", dueDate=today, repeat="Weekly", reminderOffsets=[0])
        with ts.raw() as db:
            series = db.execute("SELECT series_id FROM recurrence_series").fetchone()["series_id"]
            future = [row["task_id"] for row in db.execute("SELECT task_id FROM recurrence_occurrences WHERE sequence>0")]
            db.execute("DELETE FROM recurrence_occurrences WHERE sequence>0")
            for task_id in future:
                db.execute("DELETE FROM audit WHERE record_id=?", (task_id,))
                db.execute("DELETE FROM reminders WHERE source_id=?", (task_id,))
                db.execute("DELETE FROM records WHERE id=?", (task_id,))
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.h.call(dict(dad), "state")[0])) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results, [200] * 6)
        with ts.raw() as db:
            row = db.execute("SELECT COUNT(*) AS total, COUNT(DISTINCT occurrence_date) AS uniq FROM recurrence_occurrences WHERE series_id=?", (series,)).fetchone()
            self.assertEqual(row["total"], row["uniq"])
            self.assertEqual(row["total"], len(future) + 1)
            keys = db.execute("SELECT COUNT(*) AS total, COUNT(DISTINCT account || source_kind || source_id || reminder_key) AS uniq FROM reminders").fetchone()
            self.assertEqual(keys["total"], keys["uniq"])

    def test_malformed_requests_are_rejected_and_store_nothing(self):
        dad = self.h.client("Dad")
        before = self.counts()
        bad_payloads = (
            dict(action="create", kind="posts", record={"text": "nul \u0000 byte"}),
            dict(action="create", kind="posts", record={"text": "x", "comments": [float("nan")]}),
            dict(action="create", kind="posts", record={"text": "x" * 5000}),
            dict(action="create", kind="nonsense", record={"text": "x"}),
            dict(action="create", kind="tasks", record={"title": "t", "who": "Nobody"}),
            dict(action="done", id=10 ** 30),
            dict(action="done", id="1"),
            dict(action="delete", id=True),
            dict(action="create", kind="activities", record={"memberId": ["Maddox"], "activityName": "x", "activityType": "x", "date": "2030-01-01"}),
        )
        for payload in bad_payloads:
            status, body = self.h.call(dad, "action", payload)
            self.assertIn(status, {400, 404}, payload)
            self.assertIn("error", body)
        self.assertEqual(self.counts(), before)

    def test_authorization_rejections_write_nothing(self):
        dad, kid = self.h.client("Dad"), self.h.client("Daughter")
        self.h.create(dad, "notes", title="Vault entry", space="Vault", category="Other")
        self.h.create(dad, "tasks", title="Parent task", visibility="Adults")
        state = self.h.call(dad, "state")[1]
        vault, task = find(state["notes"], "Vault entry"), find(state["tasks"], "Parent task")
        before = self.counts()
        attempts = (
            dict(action="create", kind="tasks", record={"title": "x"}),
            dict(action="create", kind="notes", record={"title": "x", "space": "Vault", "category": "Other"}),
            dict(action="delete", id=vault["id"]),
            dict(action="edit", id=task["id"], record={"title": "hijack"}),
            dict(action="archive_season"),
            dict(action="create", kind="recognitions", record={"recognitionType": "Great Job"}),
        )
        for payload in attempts:
            self.assertIn(self.h.call(kid, "action", payload)[0], {403, 404, 400}, payload)
        self.assertEqual(self.h.call({}, "action", dict(action="archive_season"))[0], 401)
        self.assertEqual(self.counts(), before)


class SQLiteFailureModeTests(FailureModes, unittest.TestCase):
    backend = "sqlite"


@unittest.skipUnless(PG_AVAILABLE, "Set HUB_TEST_POSTGRES_URL to run PostgreSQL failure-mode tests")
class PostgresFailureModeTests(FailureModes, unittest.TestCase):
    backend = "postgres"


@unittest.skipUnless(PG_AVAILABLE, "Set HUB_TEST_POSTGRES_URL to run PostgreSQL failure-mode tests")
class PostgresConnectionFailureTests(unittest.TestCase):
    """Unavailable, misconfigured and unmigrated PostgreSQL: safe, generic and secret-free."""
    SECRET = "s3cr3t-pw-do-not-leak"

    def request(self, env, path="state", data=None, client=None):
        h = Harness()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(stderr):
            status, body = h.call(client or {}, path, data)
        return status, body, stderr.getvalue()

    def assert_safe(self, *outputs):
        text = json.dumps(outputs)
        for leak in (self.SECRET, "127.0.0.1", "hubuser", "hubdb", "postgresql://"):
            self.assertNotIn(leak, text)

    def test_unavailable_database_returns_generic_503(self):
        env = {"HUB_DB_BACKEND": "postgres", "HUB_DATABASE_URL": f"postgresql://hubuser:{self.SECRET}@127.0.0.1:1/hubdb"}
        for path, data in (("state", None), ("profiles", None), ("login", {"name": "Dad", "password": "testing-password"})):
            status, body, logged = self.request(env, path, data)
            self.assertEqual((status, body), (503, {"error": StorageError.public_message}), path)
            self.assert_safe(body, logged)
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(StorageError) as caught:
                server.initialize()
        self.assert_safe(str(caught.exception))

    def test_bad_configuration_returns_generic_503(self):
        for url in (None, "mysql://hubuser:%s@127.0.0.1/hubdb" % self.SECRET, "postgresql://hubuser:%s@[bad/hubdb" % self.SECRET,
                    "not a url %s" % self.SECRET):
            env = {"HUB_DB_BACKEND": "postgres"}
            if url:
                env["HUB_DATABASE_URL"] = url
            with mock.patch.dict(os.environ, env):
                if url is None:
                    os.environ.pop("HUB_DATABASE_URL", None)
                status, body, logged = self.request({}, "state")
            self.assertEqual((status, body), (503, {"error": StorageError.public_message}))
            self.assert_safe(body, logged)
        status, body, _ = self.request({"HUB_DB_BACKEND": "oracle"}, "profiles")
        self.assertEqual(status, 503)

    def test_unmigrated_database_is_refused_without_creating_any_schema(self):
        import psycopg
        with tempfile.TemporaryDirectory() as directory:
            cleanups, url = ts.start_backend("postgres", directory, migrate=False)
            try:
                with self.assertRaises(SchemaNotReady) as caught:
                    server.initialize()
                self.assertIn("0000", str(caught.exception))
                self.assertIn("0003", str(caught.exception))
                self.assertIn("python -m persistence migrate", str(caught.exception))
                self.assert_safe(str(caught.exception))
                status, body, _ = self.request({}, "state")
                self.assertEqual((status, body), (503, {"error": StorageError.public_message}))
                schema = url.rsplit("%3D", 1)[1]
                with psycopg.connect(ts.PG_URL) as conn:
                    tables = conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=%s", (schema,)).fetchone()[0]
                self.assertEqual(tables, 0)
            finally:
                for cleanup in reversed(cleanups):
                    cleanup()

    def test_partially_migrated_database_is_refused(self):
        import psycopg
        from persistence import migrator
        with tempfile.TemporaryDirectory() as directory:
            cleanups, url = ts.start_backend("postgres", directory, migrate=False)
            try:
                with psycopg.connect(url, autocommit=True) as conn:
                    migrator.migrate(conn, target=1)
                with self.assertRaises(SchemaNotReady) as caught:
                    server.initialize()
                self.assertIn("0001", str(caught.exception))
                self.assertIn("0003", str(caught.exception))
            finally:
                for cleanup in reversed(cleanups):
                    cleanup()


class QueryBudgetTests(unittest.TestCase):
    """Guards against N+1 behaviour. Set HUB_PERF_REPORT=1 to print per-request query counts and timings."""

    def measure(self, backend):
        import time
        with tempfile.TemporaryDirectory() as directory:
            cleanups, _ = ts.start_backend(backend, directory)
            try:
                server.initialize()
                ts.seed_accounts()
                h = Harness()
                dad, kid = h.client("Dad"), h.client("Daughter")
                today = (server.local_today() - datetime.timedelta(days=1)).isoformat()  # overdue, so reminders always generate
                counter = {"n": 0}
                original = Repository._rows

                def counting(self, sql, params=()):
                    counter["n"] += 1
                    return original(self, sql, params)

                def probe(label, who, path, data=None):
                    counter["n"] = 0
                    started = time.perf_counter()
                    status = h.call(who, path, data)[0]
                    elapsed = (time.perf_counter() - started) * 1000
                    return label, status, counter["n"], elapsed

                rows = []
                with mock.patch.object(Repository, "_rows", counting):
                    rows.append(probe("login", {}, "login", {"name": "Dad", "password": "testing-password"}))
                    sizes = {}
                    for batch in (10, 40):
                        for index in range(batch):
                            h.create(dad, "tasks", title=f"Task {batch}-{index}", who="Daughter", category="Home", dueDate=today, reminderOffsets=[0, 60])
                            h.create(dad, "events", title=f"Event {batch}-{index}", date=today, startTime="10:00")
                        probe("warm", dad, "state")
                        label, status, queries, elapsed = probe(f"state({batch * 2} records)", dad, "state")
                        sizes[batch] = queries
                        rows.append((label, status, queries, elapsed))
                        rows.append(probe(f"state again({batch * 2} records)", dad, "state"))
                    task = find(h.call(dad, "state")[1]["tasks"], "Task 10-0")
                    rows.append(probe("create task", dad, "action", dict(action="create", kind="tasks", record=dict(title="One more", who="Daughter", category="Home"))))
                    rows.append(probe("ack", kid, "action", {"action": "ack", "id": task["id"]}))
                    rows.append(probe("complete", kid, "action", {"action": "done", "id": task["id"]}))
                    rows.append(probe("edit", dad, "action", {"action": "edit", "id": task["id"], "record": {"description": "x"}}))
                    rows.append(probe("create competition", dad, "action", dict(action="create", kind="dance", record=dict(
                        danceType="competition", title="C", startDate=today, endDate=today, deadlines=[{"title": "d", "date": today}]))))
                    rows.append(probe("archive season", dad, "action", {"action": "archive_season"}))
                return rows, sizes
            finally:
                for cleanup in reversed(cleanups):
                    cleanup()

    def test_request_query_counts_stay_flat(self):
        for backend in ("sqlite", "postgres") if PG_AVAILABLE else ("sqlite",):
            rows, sizes = self.measure(backend)
            if os.environ.get("HUB_PERF_REPORT"):
                print(f"\n{backend}")
                for label, status, queries, elapsed in rows:
                    print(f"  {label:<28} status={status} queries={queries:<5} {elapsed:8.1f} ms")
            self.assertTrue(all(status == 200 for _, status, _, _ in rows))
            repeat = {label: queries for label, _, queries, _ in rows}
            # Once reminders exist, a repeat state load issues no per-record writes: its query count is a small constant.
            self.assertLessEqual(repeat["state again(20 records)"], 60, backend)
            self.assertLessEqual(repeat["state again(80 records)"], repeat["state again(20 records)"] + 5, backend)


if __name__ == "__main__":
    unittest.main()
