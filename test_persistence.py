"""Persistence, migration and importer tests. All data is synthetic; no real family database is read.

PostgreSQL integration tests run only when HUB_TEST_POSTGRES_URL points at a disposable database. Without it they are
reported as skipped, never as passed.
"""
import contextlib
import copy
import hashlib
import io
import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import server
from persistence import cli, config, importer, migrator
from persistence.repository import PostgresRepository, SQLiteRepository

PG_URL_VAR = "HUB_TEST_POSTGRES_URL"
PG_URL = os.environ.get(PG_URL_VAR, "")
PG_SKIP = "Set HUB_TEST_POSTGRES_URL to a disposable PostgreSQL database to run this test"
try:
    import psycopg
    import psycopg.rows
except ImportError:  # pragma: no cover
    psycopg = None
    PG_URL = ""


def seed(repo):
    """Populate any repository with the same synthetic family. Returns label -> record id."""
    for name, display in (("Dad", "Parent One"), ("Mom", "Parent Two"), ("Daughter", "Child One")):
        repo.add_user(name, "00" * 16, "ab" * 32, display)
        repo.upsert_family_member(name, display, "account", name)
    repo.upsert_family_member("Maddox", "Maddox", "managed_child", None, ["Dad", "Mom"], "baseball")
    ids = {}

    def add(label, kind, **body):
        body.setdefault("creator", "Dad")
        ids[label] = repo.create_record(kind, body)
        return ids[label]

    add("task_family", "tasks", title="Family task", visibility="Family", who="Everyone", dueDate="2030-01-10", status="open")
    add("task_adults", "tasks", title="Adults task", visibility="Adults", dueDate="2030-01-11", status="open")
    add("task_assigned", "tasks", title="Assigned task", visibility="Assigned", who="Daughter", dueDate="2030-01-12", status="open")
    add("task_me", "tasks", title="Mom only task", visibility="Me", creator="Mom", status="open")
    add("note_vault", "notes", title="Vault note", space="Vault", visibility="Family")
    add("note_me", "notes", title="Dad personal note", space="Me", visibility="Me")
    add("task_gone", "tasks", title="Removed task", visibility="Family", status="open")
    repo.soft_delete_record(ids["task_gone"], {"title": "Removed task", "visibility": "Family", "creator": "Dad", "deletedAt": "2030-01-01T00:00:00+00:00", "deletedBy": "Dad"})
    add("comp", "dance", title="Spring Showcase", danceType="competition", visibility="Family", startDate="2030-03-01", endDate="2030-03-02",
        fees=[{"label": "Entry", "amount": "75"}], financials={"paid": False}, deadlines=[{"id": "fee", "title": "Fee due", "date": "2030-02-01"}])
    add("routine", "dance", title="Solo", danceType="routine", visibility="Family", competitionIds=[ids["comp"]])
    add("costume", "dance", title="Costume", danceType="costume", visibility="Family", routineIds=[ids["routine"]], neededBy="2030-02-20")
    add("checklist", "dance", title="Packing", danceType="checklist", visibility="Family", competitionId=ids["comp"],
        routineIds=[ids["routine"]], costumeId=ids["costume"], checklistItems=[{"text": "Shoes", "checked": False}])
    add("schedule", "dance", title="Old rehearsal", danceType="schedule", visibility="Family", date="2029-12-01", archived=True,
        archivedAt="2030-01-05T00:00:00+00:00", archivedBy="Dad")
    repo.update_record_body(ids["comp"], {**repo.get_record(ids["comp"])["body"], "routineIds": [ids["routine"]], "costumeIds": [ids["costume"]]})
    add("mirror_start", "events", title="Spring Showcase", date="2030-03-01", visibility="Family", sourceDanceId=ids["comp"], sourceDanceKey="start")
    add("mirror_end", "events", title="Spring Showcase ends", date="2030-03-02", visibility="Family", sourceDanceId=ids["comp"], sourceDanceKey="end")
    add("activity", "activities", title="Practice", memberId="Maddox", visibility="Family", parentNotes="parents only", date="2030-01-20")
    add("activity_mirror", "events", title="Maddox practice", date="2030-01-20", visibility="Family", sourceActivityId=ids["activity"])
    add("series", "tasks", title="Weekly chore", visibility="Family", dueDate="2030-01-06", repeat="Weekly", status="open")
    add("series_next", "tasks", title="Weekly chore", visibility="Family", dueDate="2030-01-13", status="open")
    repo.create_series(ids["series"], {"title": "Weekly chore", "repeat": "Weekly"}, "2030-01-06", "2030-01-01T00:00:00+00:00", "2030-01-01T00:00:00+00:00", "Dad")
    repo.add_occurrence(ids["series"], "2030-01-06", ids["series"], 0)
    repo.add_occurrence(ids["series"], "2030-01-13", ids["series_next"], 1)
    add("recognition", "recognitions", title="Great Job", who="Daughter", visibility="Assigned")
    repo.mark_recognition_seen(ids["recognition"], "Daughter", "2030-01-02T00:00:00+00:00")
    repo.upsert_reminder("Dad", "tasks", ids["task_family"], "due", "task_due", "2030-01-10T09:00:00+00:00", "2030-01-01T00:00:00+00:00")
    repo.upsert_reminder("Daughter", "events", ids["mirror_start"], "soon", "event_upcoming", "2030-03-01", "2030-01-01T00:00:00+00:00")
    repo.upsert_reminder("Mom", "tasks", ids["task_gone"], "due", "task_due", "2030-01-10", "2030-01-01T00:00:00+00:00")
    repo.upsert_reminder("Mom", "recap", 1, "weekly", "weekly_recap", "2030-01-07", "2030-01-01T00:00:00+00:00")
    repo.mark_reminder_read(repo.list_reminders("Daughter")[0]["id"], "Daughter", "2030-01-03T00:00:00+00:00")
    repo.dismiss_reminders_for_source("tasks", ids["task_gone"], "2030-01-04T00:00:00+00:00")
    repo.set_setting("audit_notification_last_id", "3")
    repo.add_audit("Dad", ids["comp"], "create", "2030-01-01T00:00:00+00:00", {"title": "Spring Showcase", "visibility": "Family"})
    repo.add_audit("Dad", ids["schedule"], "archive", "2030-01-05T00:00:00+00:00", {"title": "Old rehearsal", "danceType": "schedule"}, {"by": "Dad"})
    repo.add_audit("Dad", ids["task_gone"], "delete", "2030-01-01T00:00:01+00:00", {"title": "Removed task"})
    repo.commit()
    return ids


@contextlib.contextmanager
def sqlite_source(mutate=None):
    """Create a synthetic SQLite database file; `mutate(conn, ids)` may corrupt it on purpose."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "source.sqlite3"
        previous = server.DB
        server.DB = str(path)
        try:
            server.initialize()
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA foreign_keys=ON")
            ids = seed(SQLiteRepository(conn))
            if mutate:
                conn.execute("PRAGMA foreign_keys=OFF")
                mutate(conn, ids)
                conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
        finally:
            server.DB = previous
        yield path, ids


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RepositoryContract:
    """Behaviour every repository implementation must share. Subclasses provide self.repo."""

    def test_create_read_update(self):
        first = self.repo.create_record("tasks", {"title": "One", "creator": "Dad"})
        second = self.repo.create_record("notes", {"title": "Two"})
        self.assertGreater(second, first)
        self.assertEqual(self.repo.get_record(first)["body"]["title"], "One")
        self.repo.update_record_body(first, {"title": "One!", "creator": "Dad"})
        self.assertEqual(self.repo.get_record(first)["body"]["title"], "One!")
        self.assertEqual([r["id"] for r in self.repo.list_records("notes")], [second])
        self.assertIsNone(self.repo.get_record(999999))

    def test_soft_delete_hides_but_preserves(self):
        record_id = self.repo.create_record("tasks", {"title": "Gone"})
        self.repo.soft_delete_record(record_id, {"title": "Gone", "deletedBy": "Dad"})
        self.assertIsNone(self.repo.get_record(record_id))
        stored = self.repo.get_record(record_id, include_deleted=True)
        self.assertTrue(stored["deleted"])
        self.assertEqual(stored["body"]["deletedBy"], "Dad")
        self.assertNotIn(record_id, [r["id"] for r in self.repo.list_records()])
        self.assertIn(record_id, [r["id"] for r in self.repo.list_records(include_deleted=True)])

    def test_archive_and_restore_state_round_trips(self):
        record_id = self.repo.create_record("dance", {"danceType": "routine", "title": "R"})
        body = self.repo.get_record(record_id)["body"]
        self.repo.update_record_body(record_id, {**body, "archived": True, "archivedBy": "Dad"})
        self.assertTrue(self.repo.get_record(record_id)["body"]["archived"])
        self.repo.update_record_body(record_id, {**body, "archived": False, "restoredBy": "Mom"})
        restored = self.repo.get_record(record_id)["body"]
        self.assertFalse(restored["archived"])
        self.assertEqual(restored["restoredBy"], "Mom")

    def test_relationships_and_generated_mirrors(self):
        comp = self.repo.create_record("dance", {"danceType": "competition", "title": "C"})
        routine = self.repo.create_record("dance", {"danceType": "routine", "competitionIds": [comp]})
        mirror = self.repo.create_record("events", {"title": "C", "sourceDanceId": comp})
        self.repo.create_record("events", {"title": "Unrelated"})
        self.assertEqual([r["id"] for r in self.repo.find_by_source("sourceDanceId", comp)], [mirror])
        self.assertEqual(self.repo.get_record(routine)["body"]["competitionIds"], [comp])
        self.repo.soft_delete_record(mirror)
        self.assertEqual(self.repo.find_by_source("sourceDanceId", comp), [])
        self.assertEqual(len(self.repo.find_by_source("sourceDanceId", comp, include_deleted=True)), 1)
        with self.assertRaises(ValueError):
            self.repo.find_by_source("title", "x")

    def test_reminders_read_and_dismiss_are_scoped(self):
        source, other = self.repo.create_record("tasks", {"title": "S"}), self.repo.create_record("tasks", {"title": "O"})
        self.repo.add_user("Dad", "00", "11", "A")
        self.repo.add_user("Mom", "00", "11", "B")
        self.assertTrue(self.repo.upsert_reminder("Dad", "tasks", source, "k", "task_due", "2030-01-01", "2030-01-01"))
        self.assertFalse(self.repo.upsert_reminder("Dad", "tasks", source, "k", "task_due", "2030-01-01", "2030-01-01"))
        self.repo.upsert_reminder("Dad", "tasks", other, "k", "task_due", "2030-01-01", "2030-01-01")
        self.repo.upsert_reminder("Mom", "tasks", source, "k", "task_due", "2030-01-01", "2030-01-01")
        dad_first = self.repo.list_reminders("Dad")[0]
        self.repo.mark_reminder_read(dad_first["id"], "Mom", "2030-01-02")
        self.assertIsNone(self.repo.list_reminders("Dad")[0]["read_at"])
        self.repo.mark_reminder_read(dad_first["id"], "Dad", "2030-01-02")
        self.assertEqual(self.repo.list_reminders("Dad")[0]["read_at"], "2030-01-02")
        self.repo.dismiss_reminders_for_source("tasks", source, "2030-01-03")
        dismissed = {(r["account"], r["source_id"]): r["dismissed_at"] for a in ("Dad", "Mom") for r in self.repo.list_reminders(a)}
        self.assertEqual(dismissed[("Dad", source)], "2030-01-03")
        self.assertEqual(dismissed[("Mom", source)], "2030-01-03")
        self.assertIsNone(dismissed[("Dad", other)])

    def test_recurrence_series_and_occurrences(self):
        first, second = self.repo.create_record("tasks", {"title": "T"}), self.repo.create_record("tasks", {"title": "T"})
        self.repo.create_series(first, {"repeat": "Weekly"}, "2030-01-01", "a", "b", "Dad")
        self.repo.add_occurrence(first, "2030-01-08", second, 1)
        self.repo.add_occurrence(first, "2030-01-01", first, 0)
        self.assertEqual([(o["sequence"], o["task_id"]) for o in self.repo.list_occurrences(first)], [(0, first), (1, second)])

    def test_recognition_receipt_keeps_first_seen_time(self):
        self.repo.add_user("Daughter", "00", "11", "C")
        recognition = self.repo.create_record("recognitions", {"title": "Great Job"})
        self.assertIsNone(self.repo.recognition_seen_at(recognition, "Daughter"))
        self.repo.mark_recognition_seen(recognition, "Daughter", "2030-01-01")
        self.repo.mark_recognition_seen(recognition, "Daughter", "2030-02-02")
        self.assertEqual(self.repo.recognition_seen_at(recognition, "Daughter"), "2030-01-01")

    def test_notification_settings_upsert(self):
        self.assertIsNone(self.repo.get_setting("missing"))
        self.repo.set_setting("audit_notification_last_id", "1")
        self.repo.set_setting("audit_notification_last_id", "9")
        self.assertEqual(self.repo.get_setting("audit_notification_last_id"), "9")

    def test_audit_history_round_trips_newest_first(self):
        record = self.repo.create_record("dance", {"danceType": "routine"})
        self.repo.add_audit("Dad", record, "archive", "2030-01-01", {"title": "R"}, {"by": "Dad"})
        last = self.repo.add_audit("Mom", record, "restore", "2030-01-02", {"title": "R"})
        history = self.repo.list_audit(record_id=record)
        self.assertEqual([entry["action"] for entry in history], ["restore", "archive"])
        self.assertEqual(history[0]["id"], last)
        self.assertEqual(history[1]["details"], {"by": "Dad"})
        self.assertEqual(history[1]["snapshot"], {"title": "R"})

    def test_managed_child_is_a_profile_not_a_user(self):
        self.repo.add_user("Dad", "00", "11", "A")
        self.repo.upsert_family_member("Dad", "A", "account", "Dad")
        self.repo.upsert_family_member("Maddox", "Maddox", "managed_child", None, ["Dad"], "x")
        members = {m["member_id"]: m for m in self.repo.list_family_members()}
        self.assertIsNone(members["Maddox"]["account_name"])
        self.assertEqual(members["Maddox"]["managed_by"], ["Dad"])
        self.assertIsNone(self.repo.get_user("Maddox"))
        self.assertNotIn("hash", self.repo.get_user("Dad"))

    def test_sessions_and_login_attempts(self):
        self.repo.add_user("Dad", "00", "11", "A")
        self.repo.create_session("tok", "Dad", 200)
        self.assertEqual(self.repo.session_user("tok", 100), "Dad")
        self.assertIsNone(self.repo.session_user("tok", 300))
        self.assertIsNone(self.repo.session_user("other", 100))
        self.repo.delete_expired_sessions(300)
        self.assertIsNone(self.repo.session_user("tok", 100))
        self.repo.save_attempt("addr", 1, 900)
        self.repo.save_attempt("addr", 2, 950)
        self.assertEqual(self.repo.get_attempt("addr"), {"address": "addr", "count": 2, "reset": 950})
        self.repo.clear_attempt("addr")
        self.assertIsNone(self.repo.get_attempt("addr"))
        self.repo.save_user_credentials("Dad", "22", "33", "ignored")
        self.assertEqual(self.repo.get_user_credentials("Dad"), {"name": "Dad", "salt": "22", "hash": "33"})
        self.assertEqual(self.repo.get_user("Dad")["display_name"], "A")
        self.repo.create_session("again", "Dad", 500)
        self.repo.delete_sessions_for("Dad")
        self.assertIsNone(self.repo.session_user("again", 100))

    def test_audit_feed_joins_records_in_one_query(self):
        alive, gone = self.repo.create_record("tasks", {"title": "A"}), self.repo.create_record("tasks", {"title": "G"})
        self.repo.add_audit("Dad", alive, "create", "2030-01-01T00:00:00+00:00", {"title": "A"})
        self.repo.add_audit("Dad", gone, "create", "2030-01-01T00:00:00.5+00:00", {})
        self.repo.add_audit("Dad", None, "note", "2030-01-02T00:00:00+00:00", {"title": "orphan"})
        self.repo.soft_delete_record(gone)
        feed = self.repo.list_audit_feed(10)
        self.assertEqual([entry["action"] for entry in feed], ["note", "create", "create"])
        self.assertIsNone(feed[0]["record_kind"])
        self.assertEqual((feed[1]["record_kind"], feed[1]["record_body"]), ("tasks", {"title": "G"}))
        self.assertEqual(self.repo.max_audit_id(), feed[0]["id"])
        # Deleted records are excluded, and timestamps compare as plain text (".5" sorts after "+").
        since = self.repo.list_audit_since(0, feed[0]["id"], "2030-01-01T00:00:00+00:00")
        self.assertEqual([(entry["record_id"], entry["record_kind"]) for entry in since], [(alive, "tasks")])
        self.assertEqual(self.repo.list_audit_since(0, feed[0]["id"], "2030-01-01T00:00:00.1+00:00"), [])

    def test_reminder_listing_order_keys_and_bulk_updates(self):
        for name in ("Dad", "Mom"):
            self.repo.add_user(name, "00", "11", name)
        task = self.repo.create_record("tasks", {"title": "T"})
        for key, due, kind in (("a", "2030-01-01T00:00:00+00:00", "task_due"), ("b", "2030-01-01T00:00:00.5+00:00", "overdue"),
                               ("c", "2029-12-31T23:59:59+00:00", "task_due")):
            self.repo.upsert_reminder("Dad", "tasks", task, key, kind, due, due)
        self.repo.upsert_reminder("Mom", "tasks", task, "a", "task_due", "2030-01-01", "2030-01-01")
        self.assertEqual([r["reminder_key"] for r in self.repo.recent_reminders("Dad", 10)], ["b", "a", "c"])
        self.assertEqual(len(self.repo.recent_reminders("Dad", 2)), 2)
        self.assertEqual(self.repo.reminder_keys(), {("Dad", "tasks", task, "a"), ("Dad", "tasks", task, "b"),
                                                     ("Dad", "tasks", task, "c"), ("Mom", "tasks", task, "a")})
        self.repo.mark_reminders_read("Dad", ("task_due",), "T1")
        reads = {r["reminder_key"]: r["read_at"] for r in self.repo.list_reminders("Dad")}
        self.assertEqual(reads, {"a": "T1", "b": None, "c": "T1"})
        self.assertIsNone(self.repo.list_reminders("Mom")[0]["read_at"])
        first = self.repo.recent_reminders("Dad", 1)[0]
        self.repo.snooze_reminder(first["id"], "Dad", "T9", "T2")
        self.assertEqual((self.repo.get_reminder(first["id"], "Dad")["snoozed_until"], self.repo.get_reminder(first["id"], "Dad")["read_at"]), ("T9", "T2"))
        self.assertIsNone(self.repo.get_reminder(first["id"], "Mom"))
        self.repo.dismiss_reminder(first["id"], "Dad", "T3")
        self.assertEqual(self.repo.get_reminder(first["id"], "Dad")["dismissed_at"], "T3")

    def test_series_queries_and_updates(self):
        first, second, third = (self.repo.create_record("tasks", {"title": str(i)}) for i in range(3))
        self.repo.create_series(first, {"repeat": "Weekly"}, "2030-01-01", "a", "b", "Dad")
        self.repo.add_occurrence(first, "2030-01-01", first, 0)
        self.repo.add_occurrence_if_absent(first, "2030-01-08", second, 1)
        self.repo.add_occurrence_if_absent(first, "2030-01-08", third, 2)
        self.assertEqual(self.repo.occurrence_dates_by_series(), {first: {"2030-01-01", "2030-01-08"}})
        self.assertEqual(self.repo.get_occurrence(first, second), {"occurrence_date": "2030-01-08", "sequence": 1})
        self.assertEqual(self.repo.get_occurrence_by_date(first, "2030-01-08"), {"task_id": second, "sequence": 1})
        self.repo.soft_delete_record(second)
        members = self.repo.list_series_members(first)
        self.assertEqual([(m["sequence"], m["deleted"], m["body"]["title"]) for m in members], [(0, False, "0"), (1, True, "1")])
        self.assertEqual([m["sequence"] for m in self.repo.list_series_members(first, from_sequence=1)], [1])
        self.repo.set_occurrence_date(second, "2030-01-09")
        self.assertEqual(self.repo.get_occurrence(first, second)["occurrence_date"], "2030-01-09")
        self.repo.remove_occurrences([second])
        self.repo.remove_occurrences([])
        self.assertEqual(self.repo.get_occurrence(first, second), None)
        self.repo.update_series(first, {"repeat": "Daily"}, "2030-02-01", 3, True, "c")
        series = self.repo.get_series(first)
        self.assertEqual((series["rule"], series["start_date"], series["anchor_sequence"], series["active"]), ({"repeat": "Daily"}, "2030-02-01", 3, True))
        self.assertEqual([s["series_id"] for s in self.repo.list_active_series()], [first])
        self.repo.deactivate_series(first, "d")
        self.assertIsNone(self.repo.get_series(first))
        self.assertFalse(self.repo.get_series(first, active_only=False)["active"])
        self.assertEqual(self.repo.list_active_series(), [])

    def test_kind_filters_and_member_lookup(self):
        self.repo.create_record("tasks", {"title": "t"})
        self.repo.create_record("notes", {"title": "n"})
        self.repo.create_record("posts", {"title": "p"})
        self.assertEqual(sorted(r["kind"] for r in self.repo.list_records(kinds=("tasks", "notes"))), ["notes", "tasks"])
        self.repo.ensure_family_member("Maddox", "Maddox", "managed_child", None, ["Dad"], "x")
        self.repo.ensure_family_member("Maddox", "Changed", "managed_child", None, [], "y")
        self.assertEqual(self.repo.get_family_member("Maddox"), {"member_id": "Maddox", "display_name": "Maddox", "member_type": "managed_child"})
        self.assertIsNone(self.repo.get_family_member("Nobody"))
        self.repo.set_setting_if_absent("k", "1")
        self.repo.set_setting_if_absent("k", "2")
        self.assertEqual(self.repo.get_setting("k"), "1")


class AuthorizationParity:
    """The server's visibility rules must give identical answers for data loaded through any repository."""

    EXPECTED_VISIBLE = {
        "Dad": {"task_family", "task_adults", "note_vault", "note_me", "comp", "routine", "costume", "checklist", "schedule",
                "mirror_start", "mirror_end", "activity", "activity_mirror", "series", "series_next"},
        "Mom": {"task_family", "task_adults", "task_me", "note_vault", "comp", "routine", "costume", "checklist", "schedule",
                "mirror_start", "mirror_end", "activity", "activity_mirror", "series", "series_next"},
        "Daughter": {"task_family", "task_assigned", "comp", "routine", "costume", "checklist", "schedule",
                     "mirror_start", "mirror_end", "activity", "activity_mirror", "series", "series_next", "recognition"},
    }

    def test_visibility_rules_match_expectations(self):
        ids = seed(self.repo)
        by_label = {value: key for key, value in ids.items()}
        for account, expected in self.EXPECTED_VISIBLE.items():
            seen = {by_label[r["id"]] for r in self.repo.list_records() if server.visible(r["body"], account)}
            # Recognition is Assigned to Daughter; Dad/Mom do not see it through visible().
            self.assertEqual(seen, expected, account)

    def test_dance_fees_and_parent_notes_stay_hidden_from_arielle(self):
        ids = seed(self.repo)
        comp = self.repo.get_record(ids["comp"])["body"]
        self.assertNotIn("fees", server.dance_record_for_viewer(comp, "Daughter"))
        self.assertNotIn("financials", server.dance_record_for_viewer(comp, "Daughter"))
        self.assertIn("fees", server.dance_record_for_viewer(comp, "Mom"))
        activity = self.repo.get_record(ids["activity"])["body"]
        self.assertNotIn("parentNotes", server.family_activity_for_viewer(activity, "Daughter"))
        self.assertEqual(server.family_activity_for_viewer(activity, "Dad")["parentNotes"], "parents only")

    def test_only_admin_accounts_are_adults(self):
        self.assertEqual({name for name in ("Dad", "Mom", "Daughter") if server.adult(name)}, {"Dad", "Mom"})
        self.assertFalse(server.adult("Maddox"))


class SQLiteRepositoryMixin:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        previous = server.DB
        server.DB = str(Path(self.temp.name) / "repo.sqlite3")
        self.addCleanup(setattr, server, "DB", previous)
        server.initialize()
        conn = sqlite3.connect(server.DB)
        conn.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(conn.close)
        self.repo = SQLiteRepository(conn)


class SQLiteRepositoryTests(SQLiteRepositoryMixin, RepositoryContract, unittest.TestCase):
    pass


class SQLiteAuthorizationParityTests(SQLiteRepositoryMixin, AuthorizationParity, unittest.TestCase):
    pass


class ConfigTests(unittest.TestCase):
    def test_default_backend_is_sqlite_and_invalid_values_are_rejected(self):
        self.assertEqual(config.backend({}), "sqlite")
        self.assertEqual(config.backend({"HUB_DB_BACKEND": "Postgres"}), "postgres")
        with self.assertRaises(config.ConfigError):
            config.backend({"HUB_DB_BACKEND": "mysql"})

    def test_errors_and_redaction_never_include_credentials(self):
        secret = "pa55-word-xyz"
        url = f"postgresql://someone:{secret}@db.example.test:5432/hub?sslmode=require"
        self.assertNotIn(secret, config.redact(url))
        self.assertNotIn("someone", config.redact(url))
        with self.assertRaises(config.ConfigError) as caught:
            config.postgres_url("HUB_DATABASE_URL", {"HUB_DATABASE_URL": f"mysql://u:{secret}@h/db"})
        self.assertNotIn(secret, str(caught.exception))
        with self.assertRaises(config.ConfigError):
            config.postgres_url("HUB_DATABASE_URL", {})

    def test_server_reports_missing_postgres_configuration_without_secrets(self):
        with mock.patch.dict(os.environ, {"HUB_DB_BACKEND": "postgres"}):
            os.environ.pop("HUB_DATABASE_URL", None)
            with self.assertRaises(config.ConfigError) as caught:
                server.repository()
            self.assertIn("HUB_DATABASE_URL", str(caught.exception))


class MigrationFileTests(unittest.TestCase):
    def write(self, directory, files):
        for name, sql in files.items():
            (Path(directory) / name).write_text(sql)

    def test_shipped_migrations_are_ordered_and_contiguous(self):
        found = migrator.discover()
        self.assertGreaterEqual(len(found), 2)
        self.assertEqual([m.version for m in found], list(range(1, len(found) + 1)))
        self.assertEqual(found[0].name, "baseline")

    def test_gap_duplicate_and_bad_names_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write(directory, {"0001_a.sql": "SELECT 1;", "0003_c.sql": "SELECT 1;"})
            with self.assertRaises(migrator.MigrationError):
                migrator.discover(directory)
        with tempfile.TemporaryDirectory() as directory:
            self.write(directory, {"0001_a.sql": "SELECT 1;", "0001_b.sql": "SELECT 1;"})
            with self.assertRaises(migrator.MigrationError):
                migrator.discover(directory)
        with tempfile.TemporaryDirectory() as directory:
            self.write(directory, {"one.sql": "SELECT 1;"})
            with self.assertRaises(migrator.MigrationError):
                migrator.discover(directory)

    def test_checksum_is_stable_and_ignores_line_endings(self):
        self.assertEqual(migrator.Migration(1, "a", "SELECT 1;\r\n").checksum, migrator.Migration(1, "a", "SELECT 1;\n").checksum)
        self.assertNotEqual(migrator.Migration(1, "a", "SELECT 1;").checksum, migrator.Migration(1, "a", "SELECT 2;").checksum)

    def test_baseline_covers_every_current_table(self):
        sql = "".join(m.sql for m in migrator.discover())
        for table in ("users", "sessions", "family_members", "records", "audit", "attempts", "recurrence_series",
                      "recurrence_occurrences", "reminders", "recognition_receipts", "notification_settings"):
            self.assertIn(f"CREATE TABLE {table} ", sql)


class ImporterDryRunTests(unittest.TestCase):
    def dry(self, mutate=None):
        with sqlite_source(mutate) as (path, ids):
            with importer.snapshot_sqlite(path) as conn:
                return importer.dry_run(conn), ids

    def codes(self, report, level="errors"):
        return {item["code"] for item in report[level]}

    def test_clean_synthetic_source_is_ready_with_useful_counts(self):
        report, ids = self.dry()
        self.assertTrue(report["ready"], report["errors"])
        self.assertFalse(report["wrote_to_destination"])
        summary = report["summary"]
        self.assertEqual(summary["counts"]["users"], 3)
        self.assertEqual(summary["counts"]["family_members"], 4)
        self.assertEqual(summary["counts"]["records"], len(ids))
        self.assertEqual(summary["records_by_kind"]["dance"], {"total": 5, "deleted": 0})
        self.assertEqual(summary["records_by_kind"]["tasks"]["deleted"], 1)
        self.assertEqual(summary["dance_archived"], 1)
        self.assertEqual(summary["calendar_mirrors"], 3)
        self.assertEqual(summary["counts"]["recognition_receipts"], 1)
        self.assertEqual(summary["counts"]["recurrence_occurrences"], 2)
        self.assertEqual(report["warnings"], [])

    def test_source_file_is_never_modified(self):
        with sqlite_source() as (path, _):
            before = file_digest(path)
            with importer.snapshot_sqlite(path) as conn:
                importer.dry_run(conn)
            self.assertEqual(file_digest(path), before)
            self.assertFalse(Path(str(path) + "-wal").exists() and Path(str(path) + "-wal").stat().st_size > 0)

    def test_checksums_are_deterministic(self):
        with sqlite_source() as (path, _):
            with importer.snapshot_sqlite(path) as conn:
                first = importer.dry_run(conn)["summary"]["checksums"]
                second = importer.dry_run(conn)["summary"]["checksums"]
        self.assertEqual(first, second)

    def test_malformed_json_body_is_reported(self):
        report, _ = self.dry(lambda c, ids: c.execute("UPDATE records SET body='{not json' WHERE id=?", (ids["task_family"],)))
        self.assertFalse(report["ready"])
        self.assertIn("malformed_json", self.codes(report))
        self.assertIn("body_not_object", self.codes(report))

    def test_non_object_body_and_unknown_kind_are_reported(self):
        def corrupt(c, ids):
            c.execute("UPDATE records SET body='[1,2]' WHERE id=?", (ids["task_adults"],))
            c.execute("UPDATE records SET kind='mystery' WHERE id=?", (ids["task_assigned"],))
        report, _ = self.dry(corrupt)
        self.assertLessEqual({"body_not_object", "unknown_kind"}, self.codes(report))

    def test_orphans_are_detected_not_discarded(self):
        def corrupt(c, ids):
            c.execute("UPDATE audit SET record_id=987654 WHERE id=1")
            c.execute("UPDATE reminders SET account='Nobody' WHERE id=1")
            c.execute("UPDATE reminders SET source_id=987654 WHERE id=2")
            c.execute("UPDATE recognition_receipts SET recognition_id=987654")
            c.execute("UPDATE recurrence_occurrences SET task_id=987654 WHERE sequence=1")
            c.execute("DELETE FROM recurrence_series")
        report, _ = self.dry(corrupt)
        self.assertLessEqual({"orphan_audit_record", "orphan_reminder_account", "orphan_reminder_source", "orphan_receipt_recognition",
                              "orphan_occurrence_series", "orphan_occurrence_record"}, self.codes(report))
        self.assertEqual(report["summary"]["counts"]["reminders"], 4)

    def test_generated_calendar_events_must_have_exactly_one_valid_source(self):
        def corrupt(c, ids):
            c.execute("UPDATE records SET body=json_set(body,'$.sourceDanceId',987654) WHERE id=?", (ids["mirror_start"],))
            c.execute("UPDATE records SET body=json_set(body,'$.sourceDanceId',?) WHERE id=?", (ids["task_family"], ids["mirror_end"]))
            c.execute("UPDATE records SET body=json_set(body,'$.sourceDanceId',?) WHERE id=?", (ids["comp"], ids["activity_mirror"]))
        report, _ = self.dry(corrupt)
        self.assertLessEqual({"mirror_source_missing", "mirror_source_wrong_kind", "mirror_multiple_sources"}, self.codes(report))

    def test_mirror_of_deleted_or_unarchived_source_warns(self):
        def drift(c, ids):
            c.execute("UPDATE records SET deleted=1 WHERE id=?", (ids["comp"],))
            c.execute("UPDATE records SET body=json_set(body,'$.archived',json('true')) WHERE id=?", (ids["activity"],))
        report, _ = self.dry(drift)
        self.assertIn("mirror_source_deleted", self.codes(report, "warnings"))
        report, _ = self.dry(lambda c, ids: c.execute("UPDATE records SET body=json_set(body,'$.archived',json('true')) WHERE id=?", (ids["comp"],)))
        self.assertIn("mirror_archive_mismatch", self.codes(report, "warnings"))

    def test_dance_relationship_errors(self):
        def corrupt(c, ids):
            c.execute("UPDATE records SET body=json_set(body,'$.routineIds',json('[987654]')) WHERE id=?", (ids["comp"],))
            c.execute("UPDATE records SET body=json_set(body,'$.costumeId',?) WHERE id=?", (ids["comp"], ids["checklist"]))
        report, _ = self.dry(corrupt)
        self.assertLessEqual({"dance_ref_missing", "dance_ref_wrong_type"}, self.codes(report))

    def test_maddox_must_not_have_a_login(self):
        def corrupt(c, ids):
            c.execute("INSERT INTO users(name,salt,hash,display_name) VALUES('Maddox','00','11','Maddox')")
        report, _ = self.dry(corrupt)
        self.assertIn("managed_child_has_user", self.codes(report))

    def test_nul_characters_are_rejected(self):
        report, _ = self.dry(lambda c, ids: c.execute("UPDATE records SET body=json_set(body,'$.title',char(0)||'x') WHERE id=?", (ids["task_family"],)))
        self.assertIn("unsupported_nul", self.codes(report))

    def test_missing_source_tables_are_reported(self):
        report, _ = self.dry(lambda c, ids: (c.execute("DROP TABLE recognition_receipts")))
        self.assertIn("missing_table", self.codes(report))

    def test_compare_flags_every_kind_of_difference(self):
        with sqlite_source() as (path, ids):
            with importer.snapshot_sqlite(path) as conn:
                source, _ = importer.load_sqlite(conn)
        self.assertEqual(importer.compare(source, copy.deepcopy(source)), [])
        missing = copy.deepcopy(source)
        missing["records"] = [r for r in missing["records"] if r["id"] != ids["task_family"]]
        self.assertTrue(any("count" in p or "missing" in p for p in importer.compare(source, missing)))
        deleted = copy.deepcopy(source)
        next(r for r in deleted["records"] if r["id"] == ids["task_family"])["deleted"] = True
        self.assertTrue(any("status differs" in p for p in importer.compare(source, deleted)))
        changed = copy.deepcopy(source)
        next(r for r in changed["records"] if r["id"] == ids["task_family"])["body"]["title"] = "tampered"
        self.assertIn("records: content checksum differs", importer.compare(source, changed))
        orphaned = copy.deepcopy(source)
        orphaned["audit"][0]["record_id"] = 987654
        self.assertTrue(any("destination integrity" in p for p in importer.compare(source, orphaned)))


class CommandLineSafetyTests(unittest.TestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_default_is_dry_run_and_needs_no_destination(self):
        with sqlite_source() as (path, _):
            code, out, _ = self.run_cli("import", "--source", str(path), "--json")
        self.assertEqual(code, 0)
        self.assertIn('"wrote_to_destination": false', out)

    def test_dry_run_reports_failure_with_exit_code_two(self):
        with sqlite_source(lambda c, ids: c.execute("UPDATE records SET kind='mystery' WHERE id=1")) as (path, _):
            self.assertEqual(self.run_cli("import", "--source", str(path))[0], 2)

    def test_execute_requires_confirmation_and_destination(self):
        with sqlite_source() as (path, _):
            self.assertEqual(self.run_cli("import", "--source", str(path), "--execute")[0], 1)
            self.assertEqual(self.run_cli("import", "--source", str(path), "--execute", "--confirm-write")[0], 1)

    def test_live_database_path_is_refused_without_explicit_flag(self):
        code, _, err = self.run_cli("import", "--source", str(config.DEFAULT_SQLITE_PATH))
        self.assertEqual(code, 1)
        self.assertIn("allow-live-source", err)

    def test_source_is_required_and_must_exist(self):
        with self.assertRaises(SystemExit):
            self.run_cli("import")
        self.assertEqual(self.run_cli("import", "--source", "/nonexistent/path.sqlite3")[0], 1)

    def test_connection_errors_do_not_leak_the_url(self):
        secret = "leakcheck-pw"
        with mock.patch.dict(os.environ, {"TEST_BAD_URL": f"postgresql://user:{secret}@127.0.0.1:1/none"}):
            code, out, err = self.run_cli("status", "--destination-env", "TEST_BAD_URL")
        self.assertEqual(code, 1)
        self.assertNotIn(secret, out + err)


@unittest.skipUnless(PG_URL, PG_SKIP)
class PostgresCase(unittest.TestCase):
    """Each test gets a private schema so tests never touch each other or any other data."""

    def setUp(self):
        self.schema = "t_" + uuid.uuid4().hex[:12]
        admin = psycopg.connect(PG_URL, autocommit=True)
        admin.execute(f"CREATE SCHEMA {self.schema}")
        admin.close()
        self.url = PG_URL + ("&" if "?" in PG_URL else "?") + f"options=-csearch_path%3D{self.schema}"
        self.conn = psycopg.connect(self.url, autocommit=True)
        self.addCleanup(self.drop_schema)

    def drop_schema(self):
        self.conn.close()
        with psycopg.connect(PG_URL, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA {self.schema} CASCADE")

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n" if self.conn.row_factory is not psycopg.rows.tuple_row else 0]


class PostgresMigrationTests(PostgresCase):
    def test_versioning_and_idempotence(self):
        self.assertEqual(migrator.status(self.conn)["current"], 0)
        latest = migrator.discover()[-1].version
        self.assertEqual(migrator.migrate(self.conn), list(range(1, latest + 1)))
        self.assertEqual(migrator.migrate(self.conn), [])
        state = migrator.status(self.conn)
        self.assertEqual((state["current"], state["latest"], state["pending"]), (latest, latest, []))
        self.assertEqual(migrator.current_version(self.conn), latest)

    def test_target_version_stops_early(self):
        self.assertEqual(migrator.migrate(self.conn, target=1), [1])
        self.assertEqual(migrator.status(self.conn)["pending"], [2])

    def test_modified_applied_migration_is_detected(self):
        migrator.migrate(self.conn)
        self.conn.execute("UPDATE schema_migrations SET checksum='tampered' WHERE version=1")
        with self.assertRaises(migrator.MigrationError):
            migrator.migrate(self.conn)

    def test_database_ahead_of_code_is_detected(self):
        migrator.migrate(self.conn)
        self.conn.execute("INSERT INTO schema_migrations(version,name,checksum) VALUES(99,'future','x')")
        with self.assertRaises(migrator.MigrationError):
            migrator.migrate(self.conn)

    def test_failed_migration_rolls_back_and_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "0001_ok.sql").write_text("CREATE TABLE ok_table(id INTEGER);")
            (Path(directory) / "0002_bad.sql").write_text("CREATE TABLE half_table(id INTEGER); SELECT * FROM does_not_exist;")
            with self.assertRaises(psycopg.Error):
                migrator.migrate(self.conn, directory)
            self.assertEqual(migrator.current_version(self.conn), 1)
            self.assertIsNone(self.conn.execute("SELECT to_regclass('half_table')").fetchone()[0])

    def test_schema_constraints_protect_the_data_model(self):
        migrator.migrate(self.conn)
        run = self.conn.execute
        with self.assertRaises(psycopg.errors.CheckViolation):
            run("INSERT INTO records(kind,body) VALUES('mystery','{}')")
        with self.assertRaises(psycopg.errors.CheckViolation):
            run("INSERT INTO records(kind,body) VALUES('tasks','[]')")
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            run("INSERT INTO audit(actor,record_id,action,created) VALUES('Dad',987654,'create','x')")
        with self.assertRaises(psycopg.errors.CheckViolation):
            run("INSERT INTO family_members(member_id,display_name,member_type,account_name) VALUES('Maddox','Maddox','account',NULL)")
        run("INSERT INTO users(name,salt,hash) VALUES('Dad','0','0')")
        with self.assertRaises(psycopg.errors.CheckViolation):
            run("INSERT INTO family_members(member_id,display_name,member_type,account_name) VALUES('Maddox','Maddox','managed_child','Dad')")
        run("INSERT INTO family_members(member_id,display_name,member_type,managed_by) VALUES('Maddox','Maddox','managed_child','[\"Dad\"]')")
        self.assertEqual(self.count("users"), 1)

    def test_indexes_exist_for_core_query_paths(self):
        migrator.migrate(self.conn)
        names = {row[0] for row in self.conn.execute("SELECT indexname FROM pg_indexes WHERE schemaname=%s", (self.schema,))}
        for expected in ("idx_records_kind_active", "idx_records_source_dance", "idx_records_dance_archived", "idx_reminders_open",
                         "idx_audit_record", "idx_records_visibility"):
            self.assertIn(expected, names)

    def test_cli_status_and_migrate_use_env_var_without_printing_it(self):
        with mock.patch.dict(os.environ, {"TEST_DEST_URL": self.url}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["migrate", "--destination-env", "TEST_DEST_URL", "--json"]), 0)
            self.assertIn('"pending": []', out.getvalue())
            self.assertNotIn(self.url, out.getvalue())


class PostgresRepositoryTests(PostgresCase, RepositoryContract):
    def setUp(self):
        super().setUp()
        migrator.migrate(self.conn)
        self.repo = PostgresRepository(self.conn)


class PostgresAuthorizationParityTests(PostgresCase, AuthorizationParity):
    def setUp(self):
        super().setUp()
        migrator.migrate(self.conn)
        self.repo = PostgresRepository(self.conn)


class PostgresImportTests(PostgresCase):
    def setUp(self):
        super().setUp()
        migrator.migrate(self.conn)

    def test_dry_run_with_destination_writes_nothing(self):
        with sqlite_source() as (path, _):
            with importer.snapshot_sqlite(path) as source:
                report = importer.dry_run(source)
        self.assertTrue(report["ready"])
        self.assertEqual(importer.destination_report(self.conn)["non_empty_tables"], [])
        self.assertEqual(self.count("records"), 0)

    def test_import_preserves_ids_state_and_relationships(self):
        with sqlite_source() as (path, ids):
            with importer.snapshot_sqlite(path) as source:
                expected, _ = importer.load_sqlite(source)
                result = importer.execute_import(source, self.conn)
        self.assertEqual(result["validation"], "passed")
        self.assertEqual(importer.compare(expected, importer.load_postgres(self.conn)), [])
        repo = PostgresRepository(self.conn)
        self.assertEqual({r["id"] for r in repo.list_records(include_deleted=True)}, set(ids.values()))
        self.assertTrue(repo.get_record(ids["task_gone"], include_deleted=True)["deleted"])
        self.assertTrue(repo.get_record(ids["schedule"])["body"]["archived"])
        self.assertEqual({r["id"] for r in repo.find_by_source("sourceDanceId", ids["comp"])}, {ids["mirror_start"], ids["mirror_end"]})
        self.assertEqual(repo.recognition_seen_at(ids["recognition"], "Daughter"), "2030-01-02T00:00:00+00:00")
        self.assertEqual(repo.get_setting("audit_notification_last_id"), "3")
        self.assertEqual([o["task_id"] for o in repo.list_occurrences(ids["series"])], [ids["series"], ids["series_next"]])
        self.assertTrue(any(r["read_at"] for r in repo.list_reminders("Daughter")))
        self.assertEqual(self.count("sessions"), 0)
        self.assertIsNone(repo.get_user("Maddox"))
        self.assertEqual({m["member_id"] for m in repo.list_family_members()}, {"Dad", "Mom", "Daughter", "Maddox"})
        new_id = repo.create_record("tasks", {"title": "after import"})
        self.assertGreater(new_id, max(ids.values()))
        self.assertGreater(repo.add_audit("Dad", new_id, "create", "x"), 3)

    def test_imported_data_is_filtered_by_the_same_authorization_rules(self):
        with sqlite_source() as (path, ids):
            with importer.snapshot_sqlite(path) as source:
                importer.execute_import(source, self.conn)
        repo, by_label = PostgresRepository(self.conn), {v: k for k, v in ids.items()}
        for account, expected in AuthorizationParity.EXPECTED_VISIBLE.items():
            seen = {by_label[r["id"]] for r in repo.list_records() if server.visible(r["body"], account)}
            self.assertEqual(seen, expected, account)

    def test_invalid_source_is_refused_before_any_write(self):
        with sqlite_source(lambda c, ids: c.execute("UPDATE reminders SET account='Nobody' WHERE id=1")) as (path, _):
            with importer.snapshot_sqlite(path) as source:
                with self.assertRaises(importer.ImportError_):
                    importer.execute_import(source, self.conn)
        self.assertEqual(self.count("users"), 0)

    def test_non_empty_destination_is_refused(self):
        with sqlite_source() as (path, _):
            with importer.snapshot_sqlite(path) as source:
                importer.execute_import(source, self.conn)
                with self.assertRaises(importer.ImportError_):
                    importer.execute_import(source, self.conn)

    def test_unmigrated_destination_is_refused(self):
        self.conn.execute("DELETE FROM schema_migrations WHERE version=2")
        with sqlite_source() as (path, _):
            with importer.snapshot_sqlite(path) as source:
                with self.assertRaises(importer.ImportError_):
                    importer.execute_import(source, self.conn)

    def test_validation_mismatch_rolls_the_whole_import_back(self):
        with sqlite_source() as (path, _):
            with importer.snapshot_sqlite(path) as source:
                with mock.patch.object(importer, "compare", return_value=["forced mismatch"]):
                    with self.assertRaises(importer.ImportError_):
                        importer.execute_import(source, self.conn)
        for table in importer.TABLES:
            self.assertEqual(self.count(table), 0, table)

    def test_cli_execute_requires_confirmation_then_imports(self):
        with sqlite_source() as (path, _), mock.patch.dict(os.environ, {"TEST_DEST_URL": self.url}):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["import", "--source", str(path), "--destination-env", "TEST_DEST_URL", "--execute"]), 1)
            self.assertEqual(self.count("records"), 0)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["import", "--source", str(path), "--destination-env", "TEST_DEST_URL", "--execute", "--confirm-write"]), 0)
            self.assertGreater(self.count("records"), 0)


if __name__ == "__main__":
    unittest.main()
