"""Synthetic migration rehearsal: realistic SQLite family -> dry run -> PostgreSQL import -> semantic comparison ->
privacy, authorization and integrity acceptance through the real HTTP handlers on PostgreSQL.

Every value is synthetic; no real family database is read. Needs HUB_TEST_POSTGRES_URL (disposable database).
"""
import contextlib
import datetime
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import psycopg

import server
import test_server as ts
from persistence import cli, importer, migrator, runtime
from test_backend_parity import Harness, find

PG_URL = ts.PG_URL
DEST_VAR = "REHEARSAL_DEST_URL"

# Sentinels: each must be visible to exactly the people listed in VISIBLE_TO and nobody else.
VAULT = "SENTINEL-VAULT-ACCT-4471"
ADULTS_TASK = "SENTINEL-ADULTS-TASK"
ADULTS_EVENT = "SENTINEL-ADULTS-EVENT"
DAD_ME_TASK = "SENTINEL-DAD-ME-TASK"
DAD_ME_NOTE = "SENTINEL-DAD-ME-NOTE"
MOM_ME_TASK = "SENTINEL-MOM-ME-TASK"
MOM_ME_NOTE = "SENTINEL-MOM-ME-NOTE"
FEE = "SENTINEL-FEE-LABEL"
MONEY = "SENTINEL-MONEY-9183"
DANCE_NOTE = "SENTINEL-DANCE-NOTE-FOR-FAMILY"  # competition notes are part of the family-visible Dance record
ACTIVITY_PARENT_NOTE = "SENTINEL-ACTIVITY-PARENT-NOTE"
ADULT_DANCE_BUDGET = "SENTINEL-ADULT-DANCE-BUDGET"
DAD_ME_DANCE = "SENTINEL-DAD-ME-DANCE"
MOM_ME_DANCE = "SENTINEL-MOM-ME-DANCE"
FAMILY_OK = ("Family chore", "Family dinner", "Winter Classic", "Dentist Friday", "Groceries")

SECRET_TO_ARIELLE = (VAULT, ADULTS_TASK, ADULTS_EVENT, DAD_ME_TASK, DAD_ME_NOTE, MOM_ME_TASK, MOM_ME_NOTE, FEE, MONEY,
                     ACTIVITY_PARENT_NOTE, ADULT_DANCE_BUDGET, DAD_ME_DANCE, MOM_ME_DANCE)
VISIBLE_TO = {
    "Dad": {VAULT, ADULTS_TASK, ADULTS_EVENT, DAD_ME_TASK, DAD_ME_NOTE, FEE, MONEY, ACTIVITY_PARENT_NOTE, ADULT_DANCE_BUDGET, DAD_ME_DANCE},
    "Mom": {VAULT, ADULTS_TASK, ADULTS_EVENT, MOM_ME_TASK, MOM_ME_NOTE, FEE, MONEY, ACTIVITY_PARENT_NOTE, ADULT_DANCE_BUDGET, MOM_ME_DANCE},
    "Daughter": set(),
}
ALL_SENTINELS = set(SECRET_TO_ARIELLE)


def build_family(h):
    """Create the synthetic family purely through the application's own API."""
    server.initialize()  # a second start, as on a real server, so account profile rows exist
    dad, mom, kid = h.client("Dad"), h.client("Mom"), h.client("Daughter")
    today = server.local_today()
    day = lambda n: (today + datetime.timedelta(days=n)).isoformat()

    def make(actor, kind, **record):
        status, body = h.call(actor, "action", dict(action="create", kind=kind, record=record))
        assert status == 200, (kind, record.get("title"), status, body)

    def act(actor, **payload):
        status, body = h.call(actor, "action", payload)
        assert status == 200, (payload, status, body)

    def state(who):
        return h.call(who, "state")[1]

    # tasks
    make(dad, "tasks", title="Family chore", who="Daughter", category="Home", visibility="Family", dueDate=day(-1), reminderOffsets=[0, 60], ack=True)
    make(dad, "tasks", title=ADULTS_TASK, visibility="Adults", category="Bills", dueDate=day(2))
    make(dad, "tasks", title="Assigned to Arielle", visibility="Assigned", who="Daughter", dueDate=day(1))
    make(dad, "tasks", title=DAD_ME_TASK, visibility="Me")
    make(mom, "tasks", title=MOM_ME_TASK, visibility="Me")
    make(dad, "tasks", title="Weekly trash", who="Daughter", category="Home", dueDate=day(0), repeat="Weekly", reminderOffsets=[])
    make(mom, "tasks", title="Monthly filter", category="Home", dueDate=day(3), repeat="Monthly", reminderOffsets=[])
    make(dad, "tasks", title="Missed errand", who="Daughter", dueDate=day(-2), priority="Urgent")
    make(dad, "tasks", title="Task to remove", dueDate=day(5))
    make(dad, "tasks", title="Reassign me", who="Daughter", dueDate=day(2))
    st = state(dad)
    chore, missed = find(st["tasks"], "Family chore"), find(st["tasks"], "Missed errand")
    act(kid, action="ack", id=chore["id"])
    act(kid, action="done", id=chore["id"])
    act(dad, action="reopen", id=chore["id"])
    act(kid, action="done", id=chore["id"])
    act(kid, action="ack", id=missed["id"])
    act(kid, action="miss", id=missed["id"], reasonCode="Ran out of time", explanation="Synthetic reason")
    act(dad, action="delete", id=find(st["tasks"], "Task to remove")["id"])
    act(dad, action="edit", id=find(st["tasks"], "Reassign me")["id"], scope="this", record={"who": "Dad"})
    weekly = sorted((t for t in st["tasks"] if t["title"] == "Weekly trash"), key=lambda t: t["id"])
    act(dad, action="edit", id=weekly[2]["id"], scope="future", record={"description": "Use the blue bin"})
    act(dad, action="delete", id=weekly[1]["id"], scope="this")

    # calendar
    make(mom, "events", title="Family dinner", date=day(1), startTime="18:00", endTime="19:00", reminderOffsets=[0, 60])
    make(mom, "events", title=ADULTS_EVENT, date=day(2), visibility="Adults")
    make(dad, "events", title="Monthly meetup", date=day(3), repeat="Monthly")
    make(dad, "events", title="Cancelled picnic", date=day(4))
    act(dad, action="delete", id=find(state(dad)["events"], "Cancelled picnic")["id"])

    # private and vault notes
    make(dad, "notes", title="Vault entry", space="Vault", category="Bills / Financial Notes", notes=VAULT, reminderDate=day(0), reminderOffsets=[0])
    make(dad, "notes", title=DAD_ME_NOTE, space="Me", category="Personal", reminderDate=day(0), reminderOffsets=[0])
    make(mom, "notes", title=MOM_ME_NOTE, space="Me", category="Personal")

    # dance
    make(dad, "dance", danceType="competition", title="Winter Classic", startDate=day(10), endDate=day(12), venue="Synthetic Arena",
         schedulePending=True, notes=DANCE_NOTE, fees=[{"label": FEE, "amount": MONEY}], financials={"total": MONEY},
         deadlines=[{"title": "Entry due", "date": day(2)}, {"title": "Music due", "date": day(5)}])
    make(dad, "dance", danceType="competition", title="Spring Showcase", startDate=day(40), endDate=day(40), fees=[{"label": FEE}])
    make(dad, "dance", danceType="routine", title="Solo", routineType="Solo")
    make(dad, "dance", danceType="routine", title="Retired routine", routineType="Group")
    make(dad, "dance", danceType="costume", title="Sparkle costume", ordered=True)
    make(dad, "dance", danceType="costume", title="Old costume")
    make(dad, "dance", danceType="checklist", title="Packing list", checklistItems=["Shoes", "Tights", "Hairspray"])
    make(dad, "dance", danceType="schedule", title="Rehearsal block", date=day(6))
    dance = {item["title"]: item for item in state(dad)["dance"]}
    act(dad, action="edit", id=dance["Solo"]["id"], record={"competitionIds": [dance["Winter Classic"]["id"]]})
    act(dad, action="edit", id=dance["Sparkle costume"]["id"], record={"routineIds": [dance["Solo"]["id"]], "competitionId": dance["Winter Classic"]["id"]})
    act(dad, action="checklist_item", id=dance["Packing list"]["id"], itemId=1)
    act(dad, action="deadline_done", id=dance["Winter Classic"]["id"], deadlineId="1")
    act(dad, action="archive", id=dance["Retired routine"]["id"])
    act(dad, action="archive", id=dance["Spring Showcase"]["id"])
    act(dad, action="archive", id=dance["Old costume"]["id"])
    act(dad, action="restore", id=dance["Old costume"]["id"])
    act(dad, action="delete", id=dance["Old costume"]["id"])

    # Older private Dance budgets (the API now always makes structured Dance records family-visible, so these predate it).
    with ts.raw() as db:
        for creator, visibility, title in (("Dad", "Adults", ADULT_DANCE_BUDGET), ("Dad", "Me", DAD_ME_DANCE), ("Mom", "Me", MOM_ME_DANCE)):
            db.execute("INSERT INTO records(kind,body) VALUES(?,?)", ("dance", json.dumps({
                "title": title, "danceType": "competition", "visibility": visibility, "creator": creator, "by": creator,
                "fees": [{"label": FEE, "amount": MONEY}], "financials": {"total": MONEY}})))

    # managed child activity and its calendar mirror
    make(dad, "activities", memberId="Maddox", activityName="Baseball", activityType="Baseball", eventType="Practice", date=day(1),
         startTime="17:00", endTime="18:00", parentNotes=ACTIVITY_PARENT_NOTE, reminderOffsets=[1440])

    # board, lists, requests, recognition
    make(dad, "posts", text="Dentist Friday", important=True, ackRequired=True)
    make(mom, "posts", text="Movie night?")
    make(dad, "lists", text="Groceries")
    make(kid, "requests", type="Permission", text="Can I go to the game?", date=day(2), time="18:30", description="Synthetic request")
    make(kid, "requests", type="Purchase", text="New shoes?", description="Synthetic purchase request")
    make(kid, "requests", type="Question", text="Can we get a dog?")
    st = state(dad)
    post, grocery = find(st["posts"], "Dentist Friday"), find(st["lists"], "Groceries")
    act(kid, action="ack", id=post["id"])
    act(mom, action="react", id=post["id"], emoji="👍")
    act(mom, action="comment", id=post["id"], comment="See you there")
    act(dad, action="pin", id=post["id"], pinned=True)
    act(kid, action="check", id=grocery["id"])
    requests = {r["text"]: r for r in st["requests"]}
    act(dad, action="decision", id=requests["Can I go to the game?"]["id"], status="Approved", reply="Yes", addToCalendar=True)
    act(mom, action="decision", id=requests["New shoes?"]["id"], status="Denied", reply="Not now")
    act(dad, action="reply", id=requests["Can we get a dog?"]["id"], reply="Let's talk")
    make(dad, "recognitions", recognitionType="Great Job", message="Thanks for the chores", sourceTaskId=chore["id"])
    recognition = find(state(kid)["recognitions"], "Great Job")
    status, _ = h.call(kid, "recognitions/action", {"id": recognition["id"], "action": "seen"})
    assert status == 200

    # reminders, badges, recap: state loads generate them; then exercise actions
    for who in (dad, mom, kid):
        state(who)
    dad_reminders = sorted(state(dad)["reminders"], key=lambda r: r["id"])
    kid_reminders = sorted(state(kid)["reminders"], key=lambda r: r["id"])
    assert dad_reminders and kid_reminders
    h.call(dad, "reminders/action", {"id": dad_reminders[0]["id"], "action": "dismiss"})
    h.call(kid, "reminders/action", {"id": kid_reminders[0]["id"], "action": "snooze", "minutes": 60})
    h.call(kid, "reminders/action", {"action": "read_category", "category": "calendar"})


def snapshot_states(h):
    states = {}
    for name in ("Dad", "Mom", "Daughter"):
        current = h.call(h.client(name), "state")[1]
        current["reminders"] = sorted(current["reminders"], key=lambda r: r["id"])
        states[name] = json.loads(json.dumps(current, sort_keys=True))
    return states


def dump_repository():
    """Everything the application persists, read through the repository on whichever backend is active."""
    with server.repository() as repo:
        records = repo.list_records(include_deleted=True)
        series_ids = [r["id"] for r in records if repo.get_series(r["id"], active_only=False)]
        accounts = [u["name"] for u in repo.list_users()]
        return json.loads(json.dumps({
            "records": records,
            "audit": sorted(repo.list_audit(limit=10 ** 6), key=lambda a: a["id"]),
            "users": repo.list_users(),
            "family_members": repo.list_family_members(),
            "reminders": {account: repo.list_reminders(account) for account in accounts},
            "receipts": {account: repo.seen_recognitions(account) for account in accounts},
            "series": [repo.get_series(i, active_only=False) for i in series_ids],
            "occurrences": {str(i): repo.list_occurrences(i) for i in series_ids},
            "settings": {name: repo.get_setting(name) for name in ("audit_notifications_started_at", "audit_notification_last_id")},
        }, sort_keys=True, default=str))


@unittest.skipUnless(PG_URL, "Set HUB_TEST_POSTGRES_URL to run the migration rehearsal")
class SyntheticMigrationRehearsal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        # 1. Build the synthetic SQLite family through the real application.
        sqlite_cleanups, _ = ts.start_backend("sqlite", cls.temp.name)
        server.initialize()
        ts.seed_accounts()
        h = Harness()
        build_family(h)
        cls.source_states = snapshot_states(h)
        cls.source_dump = dump_repository()
        cls.source_path = server.DB
        for cleanup in reversed(sqlite_cleanups):
            cleanup()
        cls.source_digest = ts_digest(cls.source_path)
        # 2. Fresh, migrated-by-CLI PostgreSQL destination.
        cls.pg_cleanups, url = ts.start_backend("postgres", cls.temp.name, migrate=False)
        cls.url = url
        for cleanup in cls.pg_cleanups:  # run in reverse even if set-up fails part-way
            cls.addClassCleanup(cleanup)
        cls.env = mock.patch.dict(os.environ, {DEST_VAR: url})
        cls.env.start()
        cls.addClassCleanup(cls.env.stop)
        cls.run_cli_ok("migrate", "--destination-env", DEST_VAR)
        cls.dry = json.loads(cls.run_cli_ok("import-dry-run", "--source", cls.source_path, "--destination-env", DEST_VAR, "--json"))
        with psycopg.connect(url) as conn:
            cls.database = conn.execute("SELECT current_database()").fetchone()[0]
        cls.imported = json.loads(cls.run_cli_ok("import-execute", "--source", cls.source_path, "--destination-env", DEST_VAR,
                                                 "--confirm-write", "--confirm-database", cls.database, "--json"))
        server.initialize()
        cls.dest_dump = dump_repository()
        cls.dest_states = snapshot_states(Harness())

    @staticmethod
    def run_cli_ok(*args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(list(args))
        assert code == 0, (args[0], code)
        return out.getvalue()

    # --- the rehearsal itself
    def test_dry_run_is_clean_and_describes_the_family(self):
        self.assertTrue(self.dry["ready"], self.dry["errors"])
        self.assertFalse(self.dry["wrote_to_destination"])
        self.assertEqual(self.dry["errors"], [])
        self.assertEqual(self.dry["destination"]["database"], self.database)
        summary = self.dry["summary"]
        text = json.dumps(summary)
        for expected in ("dance", "tasks", "events", "notes", "recognitions", "activities", "lists", "posts", "requests"):
            self.assertIn(expected, text)

    def test_import_executed_and_validated(self):
        self.assertEqual((self.imported["wrote_to_destination"], self.imported["validation"]), (True, "passed"))

    def test_source_database_was_not_modified_by_the_rehearsal(self):
        self.assertEqual(ts_digest(self.source_path), self.source_digest)

    def test_every_persisted_table_matches_the_source_exactly(self):
        for key in self.source_dump:
            self.assertEqual(self.source_dump[key], self.dest_dump[key], key)

    def test_rehearsal_dataset_is_realistic(self):
        records = self.dest_dump["records"]
        kinds = {r["kind"] for r in records}
        self.assertEqual(kinds, {"tasks", "posts", "requests", "events", "lists", "dance", "notes", "recognitions", "activities"})
        self.assertTrue(any(r["deleted"] for r in records))
        self.assertTrue(any(r["kind"] == "dance" and r["body"].get("archived") for r in records))
        self.assertTrue(any(r["kind"] == "events" and r["body"].get("sourceDanceId") for r in records))
        self.assertTrue(any(r["kind"] == "events" and r["body"].get("sourceActivityId") for r in records))
        self.assertTrue(any(r["kind"] == "events" and r["body"].get("sourceRequestId") for r in records))
        self.assertTrue(self.dest_dump["series"] and self.dest_dump["occurrences"])
        self.assertTrue(any(self.dest_dump["receipts"].values()))
        for account in ("Dad", "Mom", "Daughter"):
            self.assertTrue(self.dest_dump["reminders"][account], account)
        self.assertGreater(len(self.dest_dump["audit"]), 50)

    # --- integrity
    def test_ids_kinds_visibility_creators_and_assignees_are_preserved(self):
        source = {r["id"]: r for r in self.source_dump["records"]}
        dest = {r["id"]: r for r in self.dest_dump["records"]}
        self.assertEqual(set(source), set(dest))
        for record_id, record in source.items():
            for field in ("visibility", "creator", "who", "space", "archived", "status", "seriesId", "sourceDanceId", "sourceActivityId", "sourceRequestId"):
                self.assertEqual(record["body"].get(field), dest[record_id]["body"].get(field), (record_id, field))
            self.assertEqual((record["kind"], record["deleted"]), (dest[record_id]["kind"], dest[record_id]["deleted"]))

    def test_mirrors_dance_links_and_recurrence_resolve_in_the_destination(self):
        records = {r["id"]: r for r in self.dest_dump["records"]}
        for record in records.values():
            body = record["body"]
            if record["kind"] == "events" and body.get("sourceDanceId") and not record["deleted"]:
                self.assertEqual(records[body["sourceDanceId"]]["kind"], "dance")
                self.assertFalse(records[body["sourceDanceId"]]["deleted"])
            if record["kind"] == "events" and body.get("sourceActivityId") and not record["deleted"]:
                self.assertEqual(records[body["sourceActivityId"]]["kind"], "activities")
            if record["kind"] == "dance":
                for field in ("competitionIds", "routineIds", "costumeIds"):
                    for linked in body.get(field, []):
                        self.assertEqual(records[linked]["kind"], "dance")
        for series_id, occurrences in self.dest_dump["occurrences"].items():
            self.assertIn(int(series_id), records)
            self.assertEqual(len({o["occurrence_date"] for o in occurrences}), len(occurrences))
            for occurrence in occurrences:
                self.assertIn(occurrence["task_id"], records)

    def test_managed_child_is_still_only_a_profile(self):
        accounts = {u["name"] for u in self.dest_dump["users"]}
        self.assertEqual(accounts, {"Dad", "Mom", "Daughter"})
        maddox = next(m for m in self.dest_dump["family_members"] if m["member_id"] == "Maddox")
        self.assertEqual((maddox["member_type"], maddox["account_name"]), ("managed_child", None))
        with psycopg.connect(self.url) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions WHERE name='Maddox'").fetchone()[0], 0)
        self.assertNotIn("Maddox", Harness().call({}, "profiles")[1]["profiles"])

    def test_competition_mirrors_stay_authoritative_and_follow_their_source(self):
        h = Harness()
        dad = h.client("Dad")
        state = h.call(dad, "state")[1]
        competition = find(state["dance"], "Winter Classic")
        mirrors = [e for e in state["events"] if e.get("sourceDanceId") == competition["id"]]
        self.assertEqual({m["sourceDanceKey"] for m in mirrors}, {"competition", "competition:end", "deadline:1", "deadline:2"})
        for mirror in mirrors:
            for action, extra in (("edit", {"record": {"title": "diverged"}}), ("delete", {})):
                status, body = h.call(dad, "action", {"action": action, "id": mirror["id"], **extra})
                self.assertEqual(status, 403, (action, body))
        new_start = (server.local_today() + datetime.timedelta(days=20)).isoformat()
        new_end = (server.local_today() + datetime.timedelta(days=21)).isoformat()
        self.assertEqual(h.call(dad, "action", {"action": "edit", "id": competition["id"], "record": {"startDate": new_start, "endDate": new_end}})[0], 200)
        after = h.call(dad, "state")[1]
        by_key = {e["sourceDanceKey"]: e for e in after["events"] if e.get("sourceDanceId") == competition["id"]}
        self.assertEqual((by_key["competition"]["date"], by_key["competition:end"]["date"]), (new_start, new_end))
        self.assertEqual(len([e for e in after["events"] if e.get("sourceDanceId") == competition["id"]]), 4)  # no duplicates

    def test_state_served_from_postgres_equals_state_served_from_the_sqlite_source(self):
        for name in ("Dad", "Mom", "Daughter"):
            self.assertEqual(self.source_states[name], self.dest_states[name], name)

    # --- privacy acceptance on PostgreSQL
    def serialized(self, name):
        return json.dumps(self.dest_states[name])

    def test_each_person_receives_exactly_their_authorized_sentinels(self):
        for name in ("Dad", "Mom", "Daughter"):
            text = self.serialized(name)
            for sentinel in ALL_SENTINELS:
                if sentinel in VISIBLE_TO[name]:
                    self.assertIn(sentinel, text, f"{name} should receive {sentinel}")
                else:
                    self.assertNotIn(sentinel, text, f"{name} must not receive {sentinel}")

    def test_dad_and_mom_receive_family_records_and_not_each_others_private_data(self):
        for name in ("Dad", "Mom"):
            titles = json.dumps(self.dest_states[name])
            for family in FAMILY_OK:
                self.assertIn(family, titles)
        self.assertNotIn(MOM_ME_TASK, self.serialized("Dad"))
        self.assertNotIn(MOM_ME_NOTE, self.serialized("Dad"))
        self.assertNotIn(DAD_ME_TASK, self.serialized("Mom"))
        self.assertNotIn(DAD_ME_NOTE, self.serialized("Mom"))

    def test_dance_notes_are_family_visible_but_money_is_not(self):
        for name in ("Dad", "Mom", "Daughter"):
            self.assertIn(DANCE_NOTE, self.serialized(name))

    def test_arielle_receives_only_family_and_assigned_records_without_parent_data(self):
        state = self.dest_states["Daughter"]
        titles = {t["title"] for t in state["tasks"]}
        self.assertIn("Family chore", titles)
        self.assertIn("Assigned to Arielle", titles)
        self.assertEqual({n["title"] for n in state["notes"]}, set())
        self.assertFalse(any(item.get("visibility") in {"Adults", "Me"} for kind in ("tasks", "events", "dance", "posts", "lists", "notes") for item in state[kind]))
        text = self.serialized("Daughter")
        for key in ('"fees"', '"financials"', '"parentNotes"'):
            self.assertNotIn(key, text)
        self.assertEqual(state["badgeCounts"].get("vault", 0), 0)
        self.assertEqual(state["badgeCounts"].get("private", 0), 0)
        for entry in state["activity"]:
            self.assertEqual(set(entry), {"actorName", "summary", "createdAt"})

    def test_unauthorized_writes_are_rejected_server_side_and_change_nothing(self):
        h = Harness()
        dad, mom, kid = h.client("Dad"), h.client("Mom"), h.client("Daughter")
        dad_state = h.call(dad, "state")[1]
        mom_state = h.call(mom, "state")[1]
        ids = {
            "family_task": find(dad_state["tasks"], "Family chore")["id"],
            "adults_task": find(dad_state["tasks"], ADULTS_TASK)["id"],
            "dad_me_task": find(dad_state["tasks"], DAD_ME_TASK)["id"],
            "mom_me_task": find(mom_state["tasks"], MOM_ME_TASK)["id"],
            "vault": find(dad_state["notes"], "Vault entry")["id"],
            "dad_note": find(dad_state["notes"], DAD_ME_NOTE)["id"],
            "mom_note": find(mom_state["notes"], MOM_ME_NOTE)["id"],
            "competition": find(dad_state["dance"], "Winter Classic")["id"],
            "routine": find(dad_state["dance"], "Solo")["id"],
            "request": find(dad_state["requests"], "Can we get a dog?")["id"],
            "post": find(dad_state["posts"], "Dentist Friday")["id"],
        }
        before = dump_repository()
        attempts = [
            (kid, {"action": "create", "kind": "tasks", "record": {"title": "x", "who": "Daughter"}}),
            (kid, {"action": "create", "kind": "events", "record": {"title": "x", "date": "2030-07-01"}}),
            (kid, {"action": "create", "kind": "dance", "record": {"danceType": "routine", "title": "x"}}),
            (kid, {"action": "create", "kind": "activities", "record": {"memberId": "Maddox", "activityName": "x", "activityType": "x", "date": "2030-07-01"}}),
            (kid, {"action": "create", "kind": "recognitions", "record": {"recognitionType": "Great Job"}}),
            (kid, {"action": "create", "kind": "posts", "record": {"text": "x", "important": True}}),
            (kid, {"action": "create", "kind": "notes", "record": {"title": "x", "space": "Vault", "category": "Other"}}),
            (kid, {"action": "create", "kind": "lists", "record": {"text": "x", "visibility": "Adults"}}),
            (kid, {"action": "edit", "id": ids["family_task"], "scope": "this", "record": {"title": "hijack"}}),
            (kid, {"action": "delete", "id": ids["family_task"]}),
            (kid, {"action": "reopen", "id": ids["family_task"]}),
            (kid, {"action": "edit", "id": ids["competition"], "record": {"notes": "x"}}),
            (kid, {"action": "archive", "id": ids["competition"]}),
            (kid, {"action": "restore", "id": ids["competition"]}),
            (kid, {"action": "delete", "id": ids["routine"]}),
            (kid, {"action": "archive_season"}),
            (kid, {"action": "decision", "id": ids["request"], "status": "Approved"}),
            (kid, {"action": "pin", "id": ids["post"], "pinned": False}),
            (kid, {"action": "delete", "id": ids["vault"]}),
            (kid, {"action": "edit", "id": ids["vault"], "record": {"notes": "x"}}),
            (kid, {"action": "edit", "id": ids["adults_task"], "scope": "this", "record": {"title": "x"}}),
            (kid, {"action": "done", "id": ids["adults_task"]}),
            (kid, {"action": "edit", "id": ids["dad_note"], "record": {"notes": "x"}}),
            (kid, {"action": "delete", "id": ids["mom_me_task"]}),
            (mom, {"action": "edit", "id": ids["dad_note"], "record": {"notes": "x"}}),
            (mom, {"action": "delete", "id": ids["dad_me_task"]}),
            (mom, {"action": "done", "id": ids["dad_me_task"], "manager": True}),
            (dad, {"action": "edit", "id": ids["mom_note"], "record": {"notes": "x"}}),
            (dad, {"action": "delete", "id": ids["mom_me_task"]}),
        ]
        for who, payload in attempts:
            status, body = h.call(who, "action", payload)
            self.assertIn(status, {400, 403, 404}, (payload, status, body))
        # Not signed in / forged requests never reach the data.
        self.assertEqual(h.call({}, "action", {"action": "archive_season"})[0], 401)
        self.assertEqual(h.call({"cookie": "hub_session=forged"}, "action", {"action": "archive_season"})[0], 401)
        self.assertEqual(h.call(kid, "action", {"action": "archive_season"}, headers=False)[0], 403)
        self.assertEqual(h.call({}, "state")[0], 401)
        # Reminder and recognition endpoints are scoped to their owner.
        dad_reminder = sorted(h.call(dad, "state")[1]["reminders"], key=lambda r: r["id"])[0]["id"]
        for who in (mom, kid):
            self.assertIn(h.call(who, "reminders/action", {"id": dad_reminder, "action": "dismiss"})[0], {404})
        recognition = find(h.call(kid, "state")[1]["recognitions"], "Great Job")["id"]
        self.assertEqual(h.call(dad, "recognitions/action", {"id": recognition, "action": "seen"})[0], 403)
        self.assertEqual(dump_repository(), before)  # every rejection left the database byte-for-byte alone

    def test_everyone_can_still_sign_in_on_postgres_and_maddox_cannot(self):
        h = Harness()
        for name in ("Dad", "Mom", "Daughter"):
            self.assertEqual(h.call({}, "login", {"name": name, "password": "testing-password"})[0], 200)
            self.assertEqual(h.call({}, "login", {"name": name, "password": "wrong-password"})[0], 401)
        self.assertNotEqual(h.call({}, "login", {"name": "Maddox", "password": "testing-password"})[0], 200)


@unittest.skipUnless(PG_URL, "Set HUB_TEST_POSTGRES_URL to run the migration rehearsal")
class ImportRefusals(unittest.TestCase):
    """Duplicate, non-empty, partial and invalid-source attempts against the CLI, on top of the importer unit tests."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        sqlite_cleanups, _ = ts.start_backend("sqlite", self.temp.name)
        server.initialize()
        ts.seed_accounts()
        build_family(Harness())
        self.source = server.DB
        for cleanup in reversed(sqlite_cleanups):
            cleanup()
        cleanups, self.url = ts.start_backend("postgres", self.temp.name, migrate=False)
        for cleanup in reversed(cleanups):
            self.addCleanup(cleanup)
        patch = mock.patch.dict(os.environ, {DEST_VAR: self.url})
        patch.start()
        self.addCleanup(patch.stop)
        with psycopg.connect(self.url, autocommit=True) as conn:
            self.database = conn.execute("SELECT current_database()").fetchone()[0]

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue() + err.getvalue()

    def count(self, table):
        with psycopg.connect(self.url) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def execute_args(self):
        return ("import-execute", "--source", self.source, "--destination-env", DEST_VAR, "--confirm-write", "--confirm-database", self.database)

    def test_status_and_dry_run_never_write_to_an_unmigrated_database(self):
        for args in (("status", "--destination-env", DEST_VAR), ("migrate", "--destination-env", DEST_VAR, "--dry-run"),
                     ("import-dry-run", "--source", self.source, "--destination-env", DEST_VAR)):
            self.assertEqual(self.cli(*args)[0], 0, args)
        with psycopg.connect(self.url) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=current_schema()").fetchone()[0], 0)

    def test_import_into_an_unmigrated_database_is_refused_without_creating_anything(self):
        code, _ = self.cli(*self.execute_args())
        self.assertEqual(code, 1)
        with psycopg.connect(self.url) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=current_schema()").fetchone()[0], 0)

    def test_partially_migrated_destination_is_refused(self):
        with psycopg.connect(self.url, autocommit=True) as conn:
            migrator.migrate(conn, target=1)
        self.assertEqual(self.cli(*self.execute_args())[0], 1)
        self.assertEqual(self.count("records"), 0)

    def test_second_import_and_non_empty_destination_are_refused(self):
        self.assertEqual(self.cli("migrate", "--destination-env", DEST_VAR)[0], 0)
        self.assertEqual(self.cli(*self.execute_args())[0], 0)
        records = self.count("records")
        code, output = self.cli(*self.execute_args())
        self.assertEqual(code, 1)
        self.assertIn("not empty", output)
        self.assertEqual(self.count("records"), records)

    def test_execute_needs_the_exact_database_name(self):
        self.assertEqual(self.cli("migrate", "--destination-env", DEST_VAR)[0], 0)
        for wrong in ("", "hub", self.database.upper()):
            args = list(self.execute_args())
            args[-1] = wrong
            self.assertEqual(self.cli(*args)[0], 1, wrong)
        self.assertEqual(self.count("records"), 0)

    def test_invalid_source_is_refused_before_any_write(self):
        self.assertEqual(self.cli("migrate", "--destination-env", DEST_VAR)[0], 0)
        import sqlite3
        broken = os.path.join(self.temp.name, "broken.sqlite3")
        with contextlib.closing(sqlite3.connect(self.source)) as src, contextlib.closing(sqlite3.connect(broken)) as dst:
            src.backup(dst)
            dst.execute("UPDATE records SET kind='mystery' WHERE id=1")
            dst.commit()
        args = list(self.execute_args())
        args[args.index("--source") + 1] = broken
        code, _ = self.cli(*args)
        self.assertEqual(code, 1)
        self.assertEqual(self.count("records"), 0)
        self.assertEqual(self.cli("import-dry-run", "--source", broken)[0], 2)
        self.assertEqual(self.cli("import-dry-run", "--source", os.path.join(self.temp.name, "missing.sqlite3"))[0], 1)

    def test_a_failure_after_partial_writes_rolls_the_whole_import_back(self):
        self.assertEqual(self.cli("migrate", "--destination-env", DEST_VAR)[0], 0)
        with mock.patch.object(importer, "compare", return_value=["forced mismatch"]):
            self.assertEqual(self.cli(*self.execute_args())[0], 1)
        for table in importer.TABLES:
            self.assertEqual(self.count(table), 0, table)

    def test_the_live_database_path_still_needs_an_explicit_flag(self):
        code, output = self.cli("import-dry-run", "--source", str(importer.DEFAULT_SQLITE_PATH))
        self.assertEqual(code, 1)
        self.assertIn("allow-live-source", output)


def ts_digest(path):
    import hashlib
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


if __name__ == "__main__":
    unittest.main()
