import http.cookiejar
import json
import sqlite3
import threading
import tempfile
import io
from email.message import Message
from http.cookies import SimpleCookie
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
import server

class FamilyPrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        server.DB = str(Path(cls.temp.name) / 'test.sqlite3')
        server.initialize()
        with server.connection() as c:
            for name in ('Dad', 'Mom', 'Daughter'):
                salt = '01' * 16
                c.execute('INSERT INTO users(name,salt,hash) VALUES(?,?,?)', (name, salt, server.password_hash('testing-password', salt)))


    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        with server.connection() as c:
            c.execute("DELETE FROM audit")
            c.execute("DELETE FROM reminders")
            c.execute("DELETE FROM recognition_receipts")
            c.execute("UPDATE notification_settings SET value='0' WHERE name='audit_notification_last_id'")
            c.execute("DELETE FROM recurrence_occurrences")
            c.execute("DELETE FROM recurrence_series")
            c.execute("DELETE FROM records")
            c.execute("DELETE FROM sessions")
            c.execute("DELETE FROM attempts")

    def client(self, name):
        client = {}
        self.assertEqual(self.call(client, 'login', dict(name=name, password='testing-password'))[0], 200)
        return client

    def call(self, client, path, data=None, headers=True):
        # Execute the real HTTP handlers without sockets (works in restricted sandboxes).
        h = object.__new__(server.Handler)
        h.path = '/api/' + path
        h.client_address = ('test-client', 0)
        h.headers = Message()
        raw = json.dumps(data).encode() if data is not None else b''
        h.headers['Content-Length'] = str(len(raw))
        if headers:
            h.headers['X-Hub-Request'] = '1'
        if client.get('cookie'):
            h.headers['Cookie'] = client['cookie']
        h.rfile = io.BytesIO(raw)
        h.wfile = io.BytesIO()
        response = {}
        h.send_response = lambda status: response.update(status=status)
        def header(key, value):
            if key == 'Set-Cookie':
                cookie = SimpleCookie(value)
                client['cookie'] = 'hub_session=' + cookie['hub_session'].value
        h.send_header = header
        h.end_headers = lambda: None
        if data is None:
            h.do_GET()
        else:
            h.do_POST()
        return response['status'], json.loads(h.wfile.getvalue())

    def create(self, client, kind, **record):
        self.assertEqual(self.call(client, 'action', dict(action='create', kind=kind, record=record))[0], 200)

    def test_existing_account_database_gets_display_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            old_db = Path(directory) / 'pre-profile.sqlite3'
            with sqlite3.connect(old_db) as c:
                c.execute('CREATE TABLE users(name TEXT PRIMARY KEY, salt TEXT NOT NULL, hash TEXT NOT NULL)')
                c.executemany('INSERT INTO users VALUES(?,?,?)', [(name, 'salt', 'hash') for name in ('Dad', 'Mom', 'Daughter')])
                c.execute('CREATE TABLE records(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, body TEXT NOT NULL)')
                c.execute('CREATE TABLE audit(id INTEGER PRIMARY KEY, actor TEXT, record_id INTEGER REFERENCES records(id), action TEXT, created TEXT)')
                c.execute('INSERT INTO records(kind,body) VALUES(?,?)', ('tasks', json.dumps({'title':'Legacy chore','visibility':'Family','who':'Daughter'})))
                c.execute('INSERT INTO audit(actor,record_id,action,created) VALUES(?,?,?,?)', ('Dad', 1, 'create', '2026-10-01T00:00:00+00:00'))
            previous_db = server.DB
            server.DB = str(old_db)
            try:
                server.initialize()
                with server.connection() as c:
                    self.assertEqual(server.profile_data(c), {
                        'Dad': {'displayName': 'Jermaine', 'role': 'ADMIN'},
                        'Mom': {'displayName': 'Stephanie', 'role': 'ADMIN'},
                        'Daughter': {'displayName': 'Arielle', 'role': 'CHILD'},
                    })
                    self.assertEqual(c.execute('SELECT COUNT(*) FROM records').fetchone()[0], 1)
                    self.assertEqual(c.execute('SELECT COUNT(*) FROM audit').fetchone()[0], 1)
                    self.assertEqual(c.execute('SELECT snapshot FROM audit').fetchone()['snapshot'], '{}')
                    self.assertIn('deleted', {row['name'] for row in c.execute('PRAGMA table_info(records)')})
                    self.assertIn('notification_settings', {row['name'] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")})
                    self.assertEqual(c.execute("SELECT COUNT(*) FROM notification_settings WHERE name='audit_notifications_started_at'").fetchone()[0], 1)
                    self.assertEqual(c.execute("SELECT value FROM notification_settings WHERE name='audit_notification_last_id'").fetchone()[0], '1')
                server.initialize()
                with server.connection() as c:
                    self.assertEqual(c.execute("SELECT COUNT(*) FROM notification_settings WHERE name='audit_notifications_started_at'").fetchone()[0], 1)
                    self.assertEqual(c.execute("SELECT value FROM notification_settings WHERE name='audit_notification_last_id'").fetchone()[0], '1')
            finally:
                server.DB = previous_db

    def test_recurrence_dates_handle_weekdays_month_ends_and_leap_years(self):
        self.assertEqual(server.recurrence_date(Path('2026-10-01').name and __import__('datetime').date(2026, 10, 1), 'Weekly', 2), __import__('datetime').date(2026, 10, 15))
        self.assertEqual(server.recurrence_date(__import__('datetime').date(2026, 10, 2), 'Weekdays', 1), __import__('datetime').date(2026, 10, 5))
        self.assertEqual(server.recurrence_date(__import__('datetime').date(2026, 1, 31), 'Monthly', 1), __import__('datetime').date(2026, 2, 28))
        self.assertEqual(server.recurrence_date(__import__('datetime').date(2026, 1, 31), 'Monthly', 2), __import__('datetime').date(2026, 3, 31))
        self.assertEqual(server.recurrence_date(__import__('datetime').date(2024, 1, 31), 'Monthly', 1), __import__('datetime').date(2024, 2, 29))

    def test_recurring_tasks_generate_bounded_independent_occurrences(self):
        dad, daughter = self.client('Dad'), self.client('Daughter')
        start = server.local_today().isoformat()
        self.create(dad, 'tasks', title='Thursday trash', description='Use outside bin', who='Daughter',
                    priority='Normal', visibility='Family', category='Home', dueDate=start, repeat='Weekly',
                    reminderOffsets=[])
        first_state = self.call(daughter, 'state')[1]
        occurrences = [t for t in first_state['tasks'] if t['title'] == 'Thursday trash']
        self.assertGreaterEqual(len(occurrences), 12)
        self.assertLessEqual(len(occurrences), server.RECURRENCE_MAX_OCCURRENCES + 1)
        self.assertEqual(occurrences[0]['occurrenceNumber'], 0)
        self.assertTrue(all(t['status'] == 'open' for t in occurrences))
        count = len(occurrences)
        self.assertEqual(len([t for t in self.call(daughter, 'state')[1]['tasks'] if t['title'] == 'Thursday trash']), count)
        first_id = occurrences[0]['id']
        self.assertEqual(self.call(daughter, 'action', {'action': 'done', 'id': first_id})[0], 200)
        after = [t for t in self.call(daughter, 'state')[1]['tasks'] if t['title'] == 'Thursday trash']
        self.assertEqual(next(t for t in after if t['id'] == first_id)['status'], 'done')
        self.assertTrue(any(t['status'] == 'open' and t['id'] != first_id for t in after))

    def test_recurring_edit_scopes_preserve_completed_and_other_occurrences(self):
        dad, daughter = self.client('Dad'), self.client('Daughter')
        start = (server.local_today() + __import__('datetime').timedelta(days=1)).isoformat()
        self.create(dad, 'tasks', title='Trash rotation', who='Daughter', priority='Normal', visibility='Family',
                    category='Home', dueDate=start, repeat='Weekly', reminderOffsets=[])
        occurrences = sorted((t for t in self.call(daughter, 'state')[1]['tasks'] if t['title'] == 'Trash rotation'), key=lambda t: t['occurrenceNumber'])
        self.assertGreaterEqual(len(occurrences), 12)
        first, second, third = occurrences[:3]
        self.assertEqual(self.call(daughter, 'action', {'action': 'done', 'id': first['id']})[0], 200)

        future = {'action': 'edit', 'id': second['id'], 'scope': 'future', 'record': {'title': 'Trash after move'}}
        self.assertEqual(self.call(dad, 'action', future)[0], 200)
        after_future = self.call(daughter, 'state')[1]['tasks']
        self.assertEqual(next(t for t in after_future if t['id'] == first['id'])['title'], 'Trash rotation')
        self.assertEqual(next(t for t in after_future if t['id'] == second['id'])['title'], 'Trash after move')
        self.assertEqual(next(t for t in after_future if t['id'] == third['id'])['title'], 'Trash after move')

        single = {'action': 'edit', 'id': second['id'], 'scope': 'this', 'record': {'description': 'Only this week'}}
        self.assertEqual(self.call(dad, 'action', single)[0], 200)
        after_single = self.call(daughter, 'state')[1]['tasks']
        self.assertEqual(next(t for t in after_single if t['id'] == second['id'])['description'], 'Only this week')
        self.assertNotEqual(next(t for t in after_single if t['id'] == third['id']).get('description'), 'Only this week')

        whole = {'action': 'edit', 'id': second['id'], 'scope': 'series', 'record': {'title': 'Whole series name'}}
        self.assertEqual(self.call(dad, 'action', whole)[0], 200)
        after_series = self.call(daughter, 'state')[1]['tasks']
        self.assertEqual(next(t for t in after_series if t['id'] == first['id'])['title'], 'Trash rotation')
        self.assertEqual(next(t for t in after_series if t['id'] == second['id'])['title'], 'Whole series name')
        self.assertEqual(next(t for t in after_series if t['id'] == third['id'])['title'], 'Whole series name')

    def test_calendar_event_visibility_and_profile_privacy(self):
        dad, mom, daughter = [self.client(n) for n in ('Dad', 'Mom', 'Daughter')]
        event = {'title': 'Arielle dance practice', 'description': 'Studio A', 'date': server.local_today().isoformat(),
                 'startTime': '17:00', 'endTime': '18:00', 'allDay': False, 'location': 'Studio',
                 'category': 'Dance', 'who': 'Daughter', 'people': ['Daughter'], 'visibility': 'Family',
                 'repeat': 'One Time', 'reminderOffsets': []}
        self.create(dad, 'events', **event)
        self.create(dad, 'events', title='Adult appointment', date=server.local_today().isoformat(),
                    category='Appointments', visibility='Adults', who='Dad', allDay=True, reminderOffsets=[])
        self.create(dad, 'events', title='Jermaine private', date=server.local_today().isoformat(),
                    category='Work', visibility='Me', who='Dad', allDay=True, reminderOffsets=[])
        self.create(mom, 'events', title='Stephanie private', date=server.local_today().isoformat(),
                    category='Appointments', visibility='Me', who='Mom', allDay=True, reminderOffsets=[])
        self.assertIn('Arielle dance practice', [e['title'] for e in self.call(daughter, 'state')[1]['events']])
        daughter_titles = [e['title'] for e in self.call(daughter, 'state')[1]['events']]
        self.assertNotIn('Adult appointment', daughter_titles)
        self.assertNotIn('Jermaine private', daughter_titles)
        self.assertNotIn('Stephanie private', daughter_titles)
        self.assertNotIn('Jermaine private', [e['title'] for e in self.call(mom, 'state')[1]['events']])
        self.assertNotIn('Stephanie private', [e['title'] for e in self.call(dad, 'state')[1]['events']])

    def test_daughter_receives_assigned_reminders_without_other_private_leaks(self):
        dad, daughter = self.client('Dad'), self.client('Daughter')
        due_date = server.local_today().isoformat()
        self.create(dad, 'tasks', title='Arielle assigned chore', who='Daughter', priority='Normal',
                    visibility='Assigned', category='Home', dueDate=due_date, dueTime='00:01', reminderOffsets=[0])
        self.create(dad, 'tasks', title='Dad private chore', who='Dad', priority='Normal',
                    visibility='Me', category='Home', dueDate=due_date, dueTime='00:01', reminderOffsets=[0])
        state = self.call(daughter, 'state')[1]
        self.assertIn('Arielle assigned chore', [t['title'] for t in state['tasks']])
        self.assertNotIn('Dad private chore', [t['title'] for t in state['tasks']])
        self.assertTrue(any(r['title'] == 'Arielle assigned chore' for r in state['reminders']))
        self.assertNotIn('Dad private chore', [r['title'] for r in state['reminders']])

    def test_reminder_generation_is_deduplicated_and_actions_are_private(self):
        dad, mom, daughter = [self.client(n) for n in ('Dad', 'Mom', 'Daughter')]
        due_date = server.local_today().isoformat()
        self.create(dad, 'tasks', title='Urgent private chore', who='Daughter', priority='Urgent',
                    visibility='Family', category='Home', dueDate=due_date, dueTime='00:01', reminderOffsets=[0])
        daughter_state = self.call(daughter, 'state')[1]
        reminders = [r for r in daughter_state['reminders'] if r['title'] == 'Urgent private chore']
        self.assertTrue(any(r['type'] == 'urgent_ack' for r in reminders))
        self.assertTrue(any(r['type'] == 'overdue' for r in reminders))
        self.assertEqual(self.call(daughter, 'state')[1]['reminders'] and len([r for r in self.call(daughter, 'state')[1]['reminders'] if r['title'] == 'Urgent private chore']), len(reminders))
        reminder = next(r for r in reminders if r['type'] == 'urgent_ack')
        self.assertEqual(self.call(mom, 'reminders/action', {'id': reminder['id'], 'action': 'dismiss'})[0], 404)
        self.assertEqual(self.call(daughter, 'reminders/action', {'id': reminder['id'], 'action': 'snooze', 'minutes': 30})[0], 200)
        snoozed = next(r for r in self.call(daughter, 'state')[1]['reminders'] if r['id'] == reminder['id'])
        self.assertTrue(snoozed['snoozed'])
        self.assertEqual(self.call(daughter, 'reminders/action', {'id': reminder['id'], 'action': 'dismiss'})[0], 200)
        dismissed = next(r for r in self.call(daughter, 'state')[1]['reminders'] if r['id'] == reminder['id'])
        self.assertTrue(dismissed['dismissed'])

    def test_badge_counts_are_per_user_filtered_deduplicated_and_actionable(self):
        dad, mom, daughter = [self.client(name) for name in ('Dad', 'Mom', 'Daughter')]
        task_date = (server.local_today() + __import__('datetime').timedelta(days=4)).isoformat()
        self.create(dad, 'tasks', title='Arielle urgent chore', who='Daughter', priority='Normal', ack=True,
                    visibility='Family', category='Home', dueDate=task_date, reminderOffsets=[])
        self.create(dad, 'tasks', title='Dad private badge task', who='Dad', priority='Urgent', ack=True,
                    visibility='Me', category='Bills', dueDate=task_date, reminderOffsets=[])
        self.create(mom, 'tasks', title='Mom private badge task', who='Mom', priority='Urgent', ack=True,
                    visibility='Me', category='Bills', dueDate=task_date, reminderOffsets=[])
        self.create(daughter, 'requests', title='Ride request', text='Can I get a ride?', type='Ride')
        request = next(item for item in self.call(dad, 'state')[1]['requests'] if item['title'] == 'Ride request')
        self.assertGreaterEqual(self.call(dad, 'state')[1]['badgeCounts']['requests'], 1)
        self.assertGreaterEqual(self.call(mom, 'state')[1]['badgeCounts']['requests'], 1)
        dad_before_reply = self.call(dad, 'state')[1]
        dad_request_notice = next(item for item in dad_before_reply['reminders'] if item['type'] == 'request_update' and item['sourceId'] == request['id'])
        self.assertEqual(self.call(dad, 'reminders/action', {'id': dad_request_notice['id'], 'action': 'read'})[0], 200)
        self.assertGreaterEqual(self.call(dad, 'state')[1]['badgeCounts']['requests'], 1)
        self.assertEqual(self.call(dad, 'action', {'action': 'reply', 'id': request['id'], 'reply': 'Which time?'})[0], 200)
        self.assertGreaterEqual(self.call(daughter, 'state')[1]['badgeCounts']['requests'], 1)

        self.create(dad, 'posts', text='Please acknowledge recital time', important=True, ackRequired=True)
        future_deadline = (server.local_today() + __import__('datetime').timedelta(days=20)).isoformat()
        self.create(dad, 'dance', title='Badge Test Competition', danceType='competition', startDate=task_date,
                deadlines=[{'id': 'payment', 'title': 'Dance payment due', 'date': future_deadline}])
        self.create(dad, 'events', title='Visible parent event', date=task_date, category='Family', allDay=True)
        self.create(mom, 'lists', text='Packing socks', visibility='Family')
        self.create(dad, 'recognitions', recognitionType='Nice Work', message='Great effort!')
        daughter_state = self.call(daughter, 'state')[1]
        self.assertGreaterEqual(daughter_state['badgeCounts']['tasks'], 1)
        self.assertGreaterEqual(daughter_state['badgeCounts']['board'], 1)
        self.assertGreaterEqual(daughter_state['badgeCounts']['dance'], 1)
        self.assertEqual(daughter_state['badgeCounts']['dance'], 1)
        competition_attention = next(item for item in daughter_state['danceAttention'] if item['title'] == 'Badge Test Competition')
        self.assertTrue(any('Performance schedule pending' in reason for reason in competition_attention['reasons']))
        self.assertTrue(any('Dance payment due' in reason for reason in competition_attention['reasons']))
        self.assertGreaterEqual(daughter_state['badgeCounts']['calendar'], 1)
        self.assertGreaterEqual(daughter_state['badgeCounts']['lists'], 1)
        self.assertGreaterEqual(daughter_state['badgeCounts']['recognition'], 1)
        self.assertEqual(daughter_state['badgeCounts']['recap'], 1)
        daughter_tasks = json.dumps(daughter_state['tasks'])
        self.assertNotIn('Dad private badge task', daughter_tasks)
        self.assertNotIn('Mom private badge task', daughter_tasks)
        self.assertNotIn('Dad private badge task', json.dumps(daughter_state['reminders']))
        self.assertNotIn('Mom private badge task', json.dumps(daughter_state['reminders']))

        second_daughter_session = self.client('Daughter')
        self.assertEqual(self.call(second_daughter_session, 'state')[1]['badgeCounts'], daughter_state['badgeCounts'])
        with server.connection() as db:
            notification_rows_before = db.execute("SELECT COUNT(*) FROM reminders WHERE account='Daughter' AND reminder_key LIKE 'notice:%'").fetchone()[0]
            audit_cursor_before = int(db.execute("SELECT value FROM notification_settings WHERE name='audit_notification_last_id'").fetchone()['value'])
        self.call(second_daughter_session, 'state')
        with server.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM reminders WHERE account='Daughter' AND reminder_key LIKE 'notice:%'").fetchone()[0], notification_rows_before)
            self.assertEqual(int(db.execute("SELECT value FROM notification_settings WHERE name='audit_notification_last_id'").fetchone()['value']), audit_cursor_before)
        request_notice = next(item for item in daughter_state['reminders'] if item['type'] == 'request_update' and item['sourceId'] == request['id'])
        self.assertEqual(self.call(mom, 'reminders/action', {'id': request_notice['id'], 'action': 'read'})[0], 404)
        self.assertEqual(self.call(mom, 'reminders/action', {'action': 'read_category', 'category': 'requests'})[0], 200)
        self.assertGreaterEqual(self.call(daughter, 'state')[1]['badgeCounts']['requests'], 1)
        self.assertEqual(self.call(second_daughter_session, 'reminders/action', {'action': 'read_category', 'category': 'requests'})[0], 200)
        self.assertEqual(self.call(daughter, 'state')[1]['badgeCounts']['requests'], 0)
        self.assertEqual(self.call(dad, 'action', {'action': 'decision', 'id': request['id'], 'status': 'Approved'})[0], 200)
        self.assertEqual(self.call(dad, 'state')[1]['badgeCounts']['requests'], 0)
        post = next(item for item in daughter_state['posts'] if item['text'] == 'Please acknowledge recital time')
        board_notice = next(item for item in daughter_state['reminders'] if item['type'] == 'board_update' and item['sourceId'] == post['id'])
        self.assertEqual(self.call(daughter, 'reminders/action', {'id': board_notice['id'], 'action': 'read'})[0], 200)
        self.assertGreaterEqual(self.call(daughter, 'state')[1]['badgeCounts']['board'], 1)
        self.assertEqual(self.call(daughter, 'action', {'action': 'ack', 'id': post['id']})[0], 200)
        self.assertEqual(self.call(daughter, 'state')[1]['badgeCounts']['board'], 0)
        task = next(item for item in daughter_state['tasks'] if item['title'] == 'Arielle urgent chore')
        task_notices = [item for item in daughter_state['reminders'] if item['sourceKind'] == 'tasks' and item['sourceId'] == task['id'] and item['type'] in {'task_assigned', 'task_acknowledgement'}]
        self.assertTrue(task_notices)
        self.assertEqual(self.call(daughter, 'reminders/action', {'action': 'read_category', 'category': 'tasks'})[0], 200)
        self.assertGreaterEqual(self.call(daughter, 'state')[1]['badgeCounts']['tasks'], 1)
        self.assertEqual(self.call(daughter, 'action', {'action': 'ack', 'id': task['id']})[0], 200)
        self.assertEqual(self.call(daughter, 'state')[1]['badgeCounts']['tasks'], 0)

        recap_reminder = next(item for item in daughter_state['reminders'] if item['type'] == 'weekly_recap')
        self.assertEqual(self.call(daughter, 'reminders/action', {'id': recap_reminder['id'], 'action': 'read'})[0], 200)
        self.assertEqual(self.call(daughter, 'state')[1]['badgeCounts']['recap'], 0)
        self.assertGreaterEqual(self.call(dad, 'state')[1]['badgeCounts']['recap'], 1)

    def test_visibility_and_history_never_leak(self):
        dad, mom, daughter = [self.client(n) for n in ('Dad', 'Mom', 'Daughter')]
        for v, text in [('Family','shared'),('Adults','adult-secret'),('Me','dad-secret'),('Assigned','mom-secret')]:
            self.create(dad, 'notes', text=text, visibility=v, who='Mom')
        ds = self.call(daughter, 'state')[1]
        self.assertEqual([n['text'] for n in ds['notes']], ['shared'])
        self.assertEqual(len(ds['activity']), 1)
        self.assertIn('Jermaine added a family note', ds['activity'][0]['summary'])
        ms = self.call(mom, 'state')[1]
        self.assertEqual({n['text'] for n in ms['notes']}, {'shared','adult-secret','mom-secret'})
        secret = next(n for n in self.call(dad,'state')[1]['notes'] if n['text']=='dad-secret')
        self.assertEqual(self.call(mom,'action',dict(action='delete',id=secret['id']))[0],404)
        self.assertEqual(self.call(daughter,'action',dict(action='delete',id=secret['id']))[0],404)

    def test_task_acknowledgement_and_authorization(self):
        dad, daughter = self.client('Dad'), self.client('Daughter')
        self.create(dad,'tasks',title='Urgent test',who='Daughter',priority='Urgent',visibility='Family',ack=False)
        task = self.call(daughter,'state')[1]['tasks'][-1]
        self.assertTrue(task['ack'])
        self.assertEqual(self.call(dad,'action',dict(action='done',id=task['id']))[0],403)
        self.assertEqual(self.call(daughter,'action',dict(action='done',id=task['id']))[0],400)
        self.assertEqual(self.call(daughter,'action',dict(action='ack',id=task['id']))[0],200)
        self.assertEqual(self.call(daughter,'action',dict(action='done',id=task['id']))[0],200)
        daughter_state = self.call(daughter, 'state')[1]
        self.assertEqual(next(item for item in daughter_state['tasks'] if item['id'] == task['id'])['status'], 'done')
        self.assertEqual(daughter_state['badgeCounts']['tasks'], 0)
        self.assertFalse(any(item['type'] == 'urgent_ack' and item['sourceId'] == task['id'] for item in daughter_state['reminders']))
        self.assertEqual(self.call(dad,'state')[1]['tasks'][-1]['status'],'done')
        self.assertEqual(self.call(daughter,'action',dict(action='create',kind='tasks',record={'title':'no'}))[0],403)

    def test_family_task_fields_sync_without_leaking_adult_tasks(self):
        dad, mom, daughter = [self.client(n) for n in ('Dad', 'Mom', 'Daughter')]
        profiles = self.call({}, 'profiles')[1]['profiles']
        self.assertEqual(profiles['Dad'], {'displayName': 'Jermaine', 'role': 'ADMIN'})
        self.assertEqual(profiles['Mom'], {'displayName': 'Stephanie', 'role': 'ADMIN'})
        self.assertEqual(profiles['Daughter'], {'displayName': 'Arielle', 'role': 'CHILD'})
        self.create(dad, 'tasks', title='Pack dance bag', description='Shoes and water bottle',
                    who='Daughter', priority='Important', visibility='Family', ack=False,
                    dueDate='2026-10-03', dueTime='17:30', category='Dance', repeat='Weekly')
        for client in (dad, mom, daughter):
            state = self.call(client, 'state')[1]
            task = next(t for t in state['tasks'] if t['title'] == 'Pack dance bag')
            self.assertEqual(task['who'], 'Daughter')
            self.assertEqual(task['description'], 'Shoes and water bottle')
            self.assertEqual(task['dueDate'], '2026-10-03')
            self.assertEqual(task['dueTime'], '17:30')
            self.assertEqual(task['category'], 'Dance')
            self.assertEqual(task['repeat'], 'Weekly')
            self.assertFalse(task['ack'])
            self.assertEqual(state['profiles']['Dad']['displayName'], 'Jermaine')
            self.assertEqual(state['profiles']['Mom']['displayName'], 'Stephanie')
            self.assertEqual(state['profiles']['Daughter']['displayName'], 'Arielle')

        self.create(dad, 'tasks', title='Parent budget review', who='Dad', priority='Normal', visibility='Adults')
        daughter_tasks = self.call(daughter, 'state')[1]['tasks']
        self.assertNotIn('Parent budget review', [t['title'] for t in daughter_tasks])
        self.assertEqual(self.call(dad, 'state')[1]['profiles']['Dad']['role'], 'ADMIN')
        self.assertEqual(self.call(daughter, 'state')[1]['profiles']['Daughter']['role'], 'CHILD')

    def test_unassigned_family_task_and_urgent_acknowledgement(self):
        dad, daughter = self.client('Dad'), self.client('Daughter')
        self.create(dad, 'tasks', title='Unassigned chore', who='', priority='Urgent', visibility='Family', ack=False)
        task = next(t for t in self.call(daughter, 'state')[1]['tasks'] if t['title'] == 'Unassigned chore')
        self.assertEqual(task['who'], '')
        self.assertTrue(task['ack'])
        self.assertEqual(self.call(daughter, 'action', {'action': 'ack', 'id': task['id']})[0], 403)
        self.assertEqual(self.call(dad, 'action', {'action': 'ack', 'id': task['id']})[0], 200)
        self.assertEqual(self.call(dad, 'action', {'action': 'done', 'id': task['id'], 'manager': True})[0], 200)

    def test_task_lifecycle_reassignment_reopen_and_delete_audit(self):
        dad, mom, daughter = [self.client(n) for n in ('Dad', 'Mom', 'Daughter')]
        self.create(dad, 'tasks', title='Take out trash', description='Use the blue bin',
                    who='Daughter', priority='Normal', visibility='Family', category='Home')
        task = next(t for t in self.call(daughter, 'state')[1]['tasks'] if t['title'] == 'Take out trash')
        task_id = task['id']

        self.assertEqual(self.call(daughter, 'action', {'action': 'done', 'id': task_id})[0], 200)
        completed = next(t for t in self.call(dad, 'state')[1]['tasks'] if t['id'] == task_id)
        self.assertEqual(completed['completedBy'], 'Daughter')
        self.assertTrue(completed['completedAt'])
        self.assertEqual(completed['completionHistory'][0]['completedBy'], 'Daughter')
        self.assertEqual(self.call(daughter, 'state')[1]['points'], 1)
        self.assertIn('✅ Arielle completed "Take out trash"', [e['summary'] for e in self.call(mom, 'state')[1]['activity']])

        self.assertEqual(self.call(dad, 'action', {'action': 'reopen', 'id': task_id})[0], 200)
        self.assertEqual(next(t for t in self.call(daughter, 'state')[1]['tasks'] if t['id'] == task_id)['status'], 'open')
        self.assertIn('reopen', [e['action'] for e in self.call(dad, 'state')[1]['activity']])

        changes = {'who': 'Dad', 'dueDate': '2026-10-04', 'dueTime': '12:02'}
        self.assertEqual(self.call(mom, 'action', {'action': 'edit', 'id': task_id, 'record': changes})[0], 200)
        updated = next(t for t in self.call(dad, 'state')[1]['tasks'] if t['id'] == task_id)
        self.assertEqual(updated['who'], 'Dad')
        self.assertEqual(updated['updatedBy'], 'Mom')
        self.assertIn('reassigned', [e['action'] for e in self.call(dad, 'state')[1]['activity']])
        self.assertIn('due_date', [e['action'] for e in self.call(dad, 'state')[1]['activity']])
        self.assertEqual(self.call(daughter, 'action', {'action': 'edit', 'id': task_id, 'record': {'title': 'Changed'}})[0], 403)

        self.assertEqual(self.call(mom, 'action', {'action': 'delete', 'id': task_id})[0], 200)
        self.assertNotIn(task_id, [t['id'] for t in self.call(dad, 'state')[1]['tasks']])
        deleted_events = [e for e in self.call(dad, 'state')[1]['activity'] if e['recordId'] == task_id]
        self.assertEqual([e['action'] for e in deleted_events][0], 'delete')
        with server.connection() as c:
            self.assertEqual(c.execute('SELECT deleted FROM records WHERE id=?', (task_id,)).fetchone()['deleted'], 1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM audit WHERE record_id=?', (task_id,)).fetchone()[0], 7)

    def test_me_only_task_edit_delete_is_owner_only(self):
        dad, mom, daughter = [self.client(n) for n in ('Dad', 'Mom', 'Daughter')]
        self.create(dad, 'tasks', title='Dad private task', who='Dad', priority='Normal', visibility='Me', category='Bills')
        dad_task = next(t for t in self.call(dad, 'state')[1]['tasks'] if t['title'] == 'Dad private task')
        for client in (mom, daughter):
            self.assertNotIn('Dad private task', [t['title'] for t in self.call(client, 'state')[1]['tasks']])
            self.assertNotIn('Dad private task', [e['title'] for e in self.call(client, 'state')[1]['activity']])
            self.assertEqual(self.call(client, 'action', {'action': 'edit', 'id': dad_task['id'], 'record': {'title': 'Leaked'}})[0], 404)
            self.assertEqual(self.call(client, 'action', {'action': 'delete', 'id': dad_task['id']})[0], 404)

        self.create(mom, 'tasks', title='Mom private task', who='Mom', priority='Normal', visibility='Me', category='Bills')
        mom_task = next(t for t in self.call(mom, 'state')[1]['tasks'] if t['title'] == 'Mom private task')
        for client in (dad, daughter):
            self.assertNotIn('Mom private task', [t['title'] for t in self.call(client, 'state')[1]['tasks']])
            self.assertNotIn('Mom private task', [e['title'] for e in self.call(client, 'state')[1]['activity']])
            self.assertEqual(self.call(client, 'action', {'action': 'edit', 'id': mom_task['id'], 'record': {'title': 'Leaked'}})[0], 404)
            self.assertEqual(self.call(client, 'action', {'action': 'delete', 'id': mom_task['id']})[0], 404)

    def test_adult_vault_notes_are_shared_only_with_adults_server_side(self):
        dad, mom, daughter = [self.client(name) for name in ('Dad', 'Mom', 'Daughter')]
        reminder_date = (server.local_today() - __import__('datetime').timedelta(days=2)).isoformat()
        status, response = self.call(dad, 'action', {'action': 'create', 'kind': 'notes', 'record': {
            'space': 'Vault', 'visibility': 'Family', 'title': 'Vault emergency contacts',
            'category': 'Emergency Information', 'notes': 'Family doctor phone ending 1234.',
            'date': server.local_today().isoformat(), 'reminderDate': reminder_date, 'reminderOffsets': [0],
        }})
        self.assertEqual(status, 200, response)
        vault = next(item for item in self.call(dad, 'state')[1]['notes'] if item.get('title') == 'Vault emergency contacts')
        self.assertEqual((vault['space'], vault['visibility'], vault['category']), ('Vault', 'Adults', 'Emergency Information'))
        for parent in (dad, mom):
            state = self.call(parent, 'state')[1]
            parent_vault = next(item for item in state['notes'] if item['id'] == vault['id'])
            self.assertEqual(parent_vault['notes'], 'Family doctor phone ending 1234.')
            self.assertTrue(any(item['sourceKind'] == 'notes' and item['sourceId'] == vault['id'] for item in state['reminders']))
            self.assertEqual(state['badgeCounts']['vault'], 1)
        daughter_state = self.call(daughter, 'state')[1]
        for secret in ('Vault emergency contacts', 'Family doctor phone ending 1234.', 'Emergency Information', '1234'):
            self.assertNotIn(secret, json.dumps(daughter_state))
        self.assertFalse(any(item['sourceKind'] == 'notes' for item in daughter_state['reminders']))
        self.assertEqual(daughter_state['badgeCounts'].get('vault', 0), 0)
        self.assertNotIn('Vault emergency contacts', json.dumps(daughter_state['activity']))
        self.assertEqual(self.call(daughter, 'action', {'action': 'create', 'kind': 'notes', 'record': {
            'space': 'Vault', 'visibility': 'Family', 'title': 'Arielle forged vault entry',
        }})[0], 403)
        self.assertEqual(self.call(daughter, 'action', {'action': 'edit', 'id': vault['id'], 'record': {'title': 'Leaked'}})[0], 404)
        self.assertEqual(self.call(daughter, 'action', {'action': 'delete', 'id': vault['id']})[0], 404)
        self.assertEqual(self.call(mom, 'action', {'action': 'edit', 'id': vault['id'], 'record': {'notes': 'Updated by Stephanie'}})[0], 200)
        with server.connection() as db:
            cursor = db.execute('INSERT INTO records(kind,body) VALUES(?,?)', ('notes', json.dumps({
                'space': 'Vault', 'visibility': 'Family', 'title': 'Malformed legacy private vault note',
                'category': 'Other', 'notes': 'Must remain hidden regardless of visibility.' , 'creator': 'Dad',
            })))
            malformed_id = cursor.lastrowid
        malformed_daughter_state = self.call(daughter, 'state')[1]
        self.assertNotIn('Malformed legacy private vault note', json.dumps(malformed_daughter_state))
        self.assertEqual(malformed_daughter_state['badgeCounts'].get('vault', 0), 0)
        self.assertEqual(self.call(daughter, 'action', {'action': 'edit', 'id': malformed_id, 'record': {'notes': 'leak'}})[0], 404)
        self.create(dad, 'notes', space='Me', visibility='Family', title='Dad counts only his own private reminder',
                category='Personal', notes='Owner-only', reminderDate=reminder_date, reminderOffsets=[0])
        dad_state = self.call(dad, 'state')[1]
        mom_state = self.call(mom, 'state')[1]
        self.assertEqual(dad_state['badgeCounts']['private'], 1)
        self.assertEqual(mom_state['badgeCounts']['private'], 0)

    def test_me_only_notes_cannot_be_read_modified_or_inferred_by_other_accounts(self):
        dad, mom, daughter = [self.client(name) for name in ('Dad', 'Mom', 'Daughter')]
        daughter_reminders_before = self.call(daughter, 'state')[1]['badgeCounts']['reminders']
        dad_reminder = (server.local_today() - __import__('datetime').timedelta(days=2)).isoformat()
        mom_reminder = (server.local_today() - __import__('datetime').timedelta(days=3)).isoformat()
        dad_status, dad_response = self.call(dad, 'action', {'action': 'create', 'kind': 'notes', 'record': {
            'space': 'Me', 'visibility': 'Family', 'title': 'Jermaine private work note',
            'category': 'Work', 'notes': 'Jermaine private contents.', 'date': server.local_today().isoformat(),
            'reminderDate': dad_reminder, 'reminderOffsets': [0],
        }})
        mom_status, mom_response = self.call(mom, 'action', {'action': 'create', 'kind': 'notes', 'record': {
            'space': 'Me', 'visibility': 'Family', 'title': 'Stephanie personal appointment',
            'category': 'Appointment', 'notes': 'Stephanie private contents.', 'date': server.local_today().isoformat(),
            'reminderDate': mom_reminder, 'reminderOffsets': [0],
        }})
        self.assertEqual((dad_status, mom_status), (200, 200), (dad_response, mom_response))
        dad_state = self.call(dad, 'state')[1]
        mom_state = self.call(mom, 'state')[1]
        dad_note = next(item for item in dad_state['notes'] if item.get('title') == 'Jermaine private work note')
        mom_note = next(item for item in mom_state['notes'] if item.get('title') == 'Stephanie personal appointment')
        self.assertEqual((dad_note['space'], dad_note['visibility']), ('Me', 'Me'))
        self.assertEqual((mom_note['space'], mom_note['visibility']), ('Me', 'Me'))
        self.assertEqual(dad_state['badgeCounts']['private'], 1)
        self.assertEqual(mom_state['badgeCounts']['private'], 1)
        self.assertTrue(any(item['sourceKind'] == 'notes' and item['sourceId'] == dad_note['id'] for item in dad_state['reminders']))
        self.assertTrue(any(item['sourceKind'] == 'notes' and item['sourceId'] == mom_note['id'] for item in mom_state['reminders']))
        old_dad_reminder = next(item for item in dad_state['reminders'] if item['sourceKind'] == 'notes' and item['sourceId'] == dad_note['id'])
        self.assertEqual(self.call(dad, 'action', {'action': 'edit', 'id': dad_note['id'], 'record': {'notes': 'Jermaine updated his own note'}})[0], 200)
        self.assertEqual(self.call(mom, 'action', {'action': 'edit', 'id': mom_note['id'], 'record': {'notes': 'Stephanie updated her own note'}})[0], 200)
        self.assertEqual(self.call(dad, 'action', {'action': 'edit', 'id': mom_note['id'], 'record': {'title': 'Stolen'}})[0], 404)
        self.assertEqual(self.call(dad, 'action', {'action': 'delete', 'id': mom_note['id']})[0], 404)
        self.assertEqual(self.call(mom, 'action', {'action': 'edit', 'id': dad_note['id'], 'record': {'title': 'Stolen'}})[0], 404)
        self.assertEqual(self.call(mom, 'action', {'action': 'delete', 'id': dad_note['id']})[0], 404)
        new_reminder_date = (server.local_today() + __import__('datetime').timedelta(days=2)).isoformat()
        self.assertEqual(self.call(dad, 'action', {'action': 'edit', 'id': dad_note['id'], 'record': {'reminderDate': new_reminder_date}})[0], 200)
        dad_state_after_reminder_edit = self.call(dad, 'state')[1]
        self.assertTrue(next(item for item in dad_state_after_reminder_edit['reminders'] if item['id'] == old_dad_reminder['id'])['dismissed'])
        self.assertEqual(sum(item['sourceKind'] == 'notes' and item['sourceId'] == dad_note['id'] and not item['dismissed'] for item in dad_state_after_reminder_edit['reminders']), 0)
        mom_state = self.call(mom, 'state')[1]
        daughter_state = self.call(daughter, 'state')[1]
        for private_text in ('Jermaine private work note', 'Jermaine private contents.'):
            self.assertNotIn(private_text, json.dumps(mom_state))
        self.assertEqual(mom_state['badgeCounts']['private'], 1)
        for private_text in ('Jermaine private work note', 'Jermaine private contents.', 'Stephanie personal appointment', 'Stephanie private contents.'):
            self.assertNotIn(private_text, json.dumps(daughter_state))
        self.assertEqual(daughter_state['badgeCounts']['private'], 0)
        self.assertEqual(daughter_state['badgeCounts']['reminders'], daughter_reminders_before)
        self.assertIn('Jermaine private work note', json.dumps(self.call(dad, 'state')[1]))
        self.assertIn('Stephanie personal appointment', json.dumps(mom_state))
        daughter_status, daughter_response = self.call(daughter, 'action', {'action': 'create', 'kind': 'notes', 'record': {
            'space': 'Me', 'visibility': 'Family', 'title': 'Arielle own private note', 'category': 'Idea',
            'notes': 'Only Arielle can see this.', 'reminderDate': dad_reminder, 'reminderOffsets': [0],
        }})
        self.assertEqual(daughter_status, 200, daughter_response)
        daughter_state = self.call(daughter, 'state')[1]
        self.assertIn('Arielle own private note', json.dumps(daughter_state))
        self.assertEqual(daughter_state['badgeCounts']['private'], 1)
        for viewer in (dad, mom):
            self.assertNotIn('Arielle own private note', json.dumps(self.call(viewer, 'state')[1]))

    def test_maddox_is_managed_profile_without_account_and_activity_uses_family_calendar(self):
        dad, mom, daughter = [self.client(name) for name in ('Dad', 'Mom', 'Daughter')]
        dad_state = self.call(dad, 'state')[1]
        self.assertEqual(dad_state['familyMembers']['Maddox'], {
            'memberId': 'Maddox', 'displayName': 'Maddox', 'profileType': 'managed_child',
            'role': 'MANAGED CHILD PROFILE', 'hasAccount': False, 'managedBy': ['Dad', 'Mom'], 'avatar': '⚾',
        })
        self.assertNotIn('Maddox', dad_state['profiles'])
        with server.connection() as db:
            self.assertIsNone(db.execute("SELECT name FROM users WHERE name='Maddox'").fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions s JOIN users u ON u.name=s.name WHERE u.name='Maddox'").fetchone()[0], 0)
            db.execute("INSERT INTO family_members(member_id,display_name,member_type,account_name,managed_by,avatar) VALUES(?,?,?,NULL,?,?)",
                       ('ManagedChildTwo', 'Future Child', 'managed_child', json.dumps(['Dad', 'Mom']), '🏀'))
        self.assertNotEqual(self.call({}, 'login', {'name': 'Maddox', 'password': 'testing-password'})[0], 200)

        activity_date = (server.local_today() + __import__('datetime').timedelta(days=1)).isoformat()
        status, response = self.call(dad, 'action', {'action': 'create', 'kind': 'activities', 'record': {
            'memberId': 'Maddox', 'activityName': 'Baseball', 'activityType': 'Baseball',
            'organization': 'Little League', 'season': 'Fall 2026', 'eventType': 'Practice',
            'date': activity_date, 'startTime': '17:30', 'endTime': '18:30',
            'location': 'North Field', 'equipmentNotes': 'Bring glove, cleats and water bottle',
            'parentNotes': 'Coach asks players to arrive early', 'reminderOffsets': [1440],
        }})
        self.assertEqual(status, 200, response)
        second_status, second_response = self.call(dad, 'action', {'action': 'create', 'kind': 'activities', 'record': {
            'memberId': 'ManagedChildTwo', 'activityName': 'Camp', 'activityType': 'Camp',
            'eventType': 'Camp', 'date': activity_date, 'location': 'Community Center',
        }})
        self.assertEqual(second_status, 200, second_response)
        activity = next(item for item in self.call(dad, 'state')[1]['activities'] if item['activityName'] == 'Baseball')
        self.assertEqual(activity['memberId'], 'Maddox')
        self.assertFalse(self.call(dad, 'state')[1]['familyMembers']['ManagedChildTwo']['hasAccount'])
        self.assertTrue(any(item['memberId'] == 'ManagedChildTwo' for item in self.call(dad, 'state')[1]['activities']))
        self.assertEqual(self.call(daughter, 'action', {'action': 'create', 'kind': 'activities', 'record': {
            'memberId': 'Maddox', 'activityName': 'Unauthorized', 'activityType': 'Camp', 'date': activity_date,
        }})[0], 403)
        for client in (dad, mom, daughter):
            state = self.call(client, 'state')[1]
            linked = [event for event in state['events'] if event.get('sourceActivityId') == activity['id']]
            self.assertEqual(len(linked), 1)
            self.assertEqual((linked[0]['date'], linked[0]['startTime'], linked[0]['location']), (activity_date, '17:30', 'North Field'))
            self.assertIn('Maddox', linked[0]['people'])
            self.assertIn('Bring glove, cleats and water bottle', linked[0]['description'])
            self.assertNotIn('Coach asks players to arrive early', linked[0]['description'])
            self.assertNotIn('Maddox', state['profiles'])
        self.assertIn('Coach asks players to arrive early', json.dumps(self.call(dad, 'state')[1]['activities']))
        self.assertNotIn('Coach asks players to arrive early', json.dumps(self.call(daughter, 'state')[1]['activities']))
        daughter_state = self.call(daughter, 'state')[1]
        self.assertTrue(any(reminder['sourceKind'] == 'events' and reminder['sourceId'] == next(event['id'] for event in daughter_state['events'] if event.get('sourceActivityId') == activity['id']) for reminder in daughter_state['reminders']))

        self.create(dad, 'tasks', title='Pack Maddox glove', who='Maddox', visibility='Family', category='Home', ack=True, priority='Urgent')
        parent_task = next(task for task in self.call(dad, 'state')[1]['tasks'] if task['title'] == 'Pack Maddox glove')
        self.assertEqual(parent_task['who'], 'Maddox')
        self.assertFalse(parent_task['ack'])
        self.assertFalse(parent_task['chore'])
        self.assertTrue(any(task['id'] == parent_task['id'] for task in self.call(daughter, 'state')[1]['tasks']))
        self.assertEqual(self.call(daughter, 'action', {'action': 'ack', 'id': parent_task['id']})[0], 403)
        self.assertEqual(self.call(daughter, 'action', {'action': 'done', 'id': parent_task['id']})[0], 403)
        self.assertEqual(self.call({}, 'action', {'action': 'done', 'id': parent_task['id']})[0], 401)
        self.assertEqual(self.call(dad, 'action', {'action': 'done', 'id': parent_task['id'], 'manager': True})[0], 200)

        later_date = (server.local_today() + __import__('datetime').timedelta(days=3)).isoformat()
        self.assertEqual(self.call(mom, 'action', {'action': 'edit', 'id': activity['id'], 'record': {
            'date': later_date, 'location': 'South Field', 'parentNotes': 'Arielle must not see parent-only arrival details',
        }})[0], 200)
        updated_events = [event for event in self.call(daughter, 'state')[1]['events'] if event.get('sourceActivityId') == activity['id']]
        self.assertEqual(len(updated_events), 1)
        self.assertEqual((updated_events[0]['date'], updated_events[0]['location']), (later_date, 'South Field'))
        daughter_state = self.call(daughter, 'state')[1]
        self.assertNotIn('Arielle must not see parent-only arrival details', json.dumps(daughter_state))
        self.assertEqual(self.call(dad, 'action', {'action': 'delete', 'id': activity['id']})[0], 200)
        self.assertFalse(any(event.get('sourceActivityId') == activity['id'] for event in self.call(daughter, 'state')[1]['events']))
        self.assertNotIn('Maddox', self.call(daughter, 'profiles')[1]['profiles'])

    def test_adult_vault_is_shared_by_parents_and_absent_from_daughter_state(self):
        dad, mom, daughter = [self.client(name) for name in ('Dad', 'Mom', 'Daughter')]
        today = server.local_today().isoformat()
        response_status, response = self.call(dad, 'action', {'action': 'create', 'kind': 'notes', 'record': {
            'space': 'Vault', 'visibility': 'Family', 'title': 'Emergency contact notes',
            'category': 'Emergency Information', 'notes': 'Call the family doctor.',
            'date': today, 'reminderDate': today, 'reminderOffsets': [0],
        }})
        self.assertEqual(response_status, 200, response)
        vault = next(item for item in self.call(dad, 'state')[1]['notes'] if item.get('title') == 'Emergency contact notes')
        self.assertEqual((vault['space'], vault['visibility'], vault['category']), ('Vault', 'Adults', 'Emergency Information'))
        for parent in (dad, mom):
            self.assertIn('Emergency contact notes', [item.get('title') for item in self.call(parent, 'state')[1]['notes']])
            self.assertTrue(any(item['sourceKind'] == 'notes' and item['sourceId'] == vault['id'] for item in self.call(parent, 'state')[1]['reminders']))

        daughter_state = self.call(daughter, 'state')[1]
        payload = json.dumps(daughter_state)
        for private_value in ('Emergency contact notes', 'Call the family doctor.', 'Emergency Information'):
            self.assertNotIn(private_value, payload)
        self.assertFalse(any(item['sourceKind'] == 'notes' for item in daughter_state['reminders']))
        self.assertEqual(self.call(daughter, 'action', {'action': 'create', 'kind': 'notes', 'record': {
            'space': 'Vault', 'visibility': 'Family', 'title': 'Forged Vault note', 'notes': 'No access',
        }})[0], 403)
        self.assertEqual(self.call(daughter, 'action', {'action': 'edit', 'id': vault['id'], 'record': {'title': 'Leaked'}})[0], 404)
        self.assertEqual(self.call(daughter, 'action', {'action': 'delete', 'id': vault['id']})[0], 404)

    def test_me_only_notes_are_creator_only_for_reads_writes_badges_and_history(self):
        dad, mom, daughter = [self.client(name) for name in ('Dad', 'Mom', 'Daughter')]
        dad_status, dad_response = self.call(dad, 'action', {'action': 'create', 'kind': 'notes', 'record': {
            'space': 'Me', 'visibility': 'Family', 'title': 'Jermaine private appointment',
            'category': 'Appointment', 'notes': 'Private doctor visit.',
        }})
        mom_status, mom_response = self.call(mom, 'action', {'action': 'create', 'kind': 'notes', 'record': {
            'space': 'Me', 'visibility': 'Family', 'title': 'Stephanie private work note',
            'category': 'Work', 'notes': 'Private work details.',
        }})
        self.assertEqual((dad_status, mom_status), (200, 200), (dad_response, mom_response))
        dad_note = next(item for item in self.call(dad, 'state')[1]['notes'] if item.get('title') == 'Jermaine private appointment')
        mom_note = next(item for item in self.call(mom, 'state')[1]['notes'] if item.get('title') == 'Stephanie private work note')
        self.assertEqual((dad_note['space'], dad_note['visibility']), ('Me', 'Me'))
        self.assertEqual(self.call(dad, 'action', {'action': 'edit', 'id': dad_note['id'], 'record': {'notes': 'Updated by owner'}})[0], 200)
        self.assertEqual(self.call(mom, 'action', {'action': 'edit', 'id': dad_note['id'], 'record': {'title': 'Stolen'}})[0], 404)
        self.assertEqual(self.call(daughter, 'action', {'action': 'delete', 'id': dad_note['id']})[0], 404)
        self.assertEqual(self.call(dad, 'action', {'action': 'edit', 'id': mom_note['id'], 'record': {'title': 'Stolen'}})[0], 404)
        self.assertEqual(self.call(dad, 'action', {'action': 'delete', 'id': mom_note['id']})[0], 404)

        dad_payload = json.dumps(self.call(dad, 'state')[1])
        mom_payload = json.dumps(self.call(mom, 'state')[1])
        daughter_payload = json.dumps(self.call(daughter, 'state')[1])
        self.assertIn('Jermaine private appointment', dad_payload)
        self.assertNotIn('Stephanie private work note', dad_payload)
        self.assertIn('Stephanie private work note', mom_payload)
        self.assertNotIn('Jermaine private appointment', mom_payload)
        for private_text in ('Jermaine private appointment', 'Private doctor visit.', 'Stephanie private work note', 'Private work details.'):
            self.assertNotIn(private_text, daughter_payload)

    def test_activity_does_not_leak_previous_private_task_values(self):
        dad, daughter = self.client('Dad'), self.client('Daughter')
        self.create(dad, 'tasks', title='Private appointment', description='Private calendar details',
                    who='Dad', priority='Normal', visibility='Me', category='Appointments')
        task = next(t for t in self.call(dad, 'state')[1]['tasks'] if t['title'] == 'Private appointment')
        self.assertNotIn('Private appointment', [t['title'] for t in self.call(daughter, 'state')[1]['tasks']])
        self.assertEqual(self.call(dad, 'action', {'action': 'edit', 'id': task['id'], 'record': {'visibility': 'Family'}})[0], 200)
        state = self.call(daughter, 'state')[1]
        self.assertIn('Private appointment', [t['title'] for t in state['tasks']])
        edit = next(e for e in state['activity'] if 'edited "Private appointment"' in e['summary'])
        self.assertNotIn('recordId', edit)
        self.assertNotIn('action', edit)
        self.assertNotIn('details', edit)
        self.assertNotIn('Private calendar details', json.dumps(state['activity']))

    def test_family_activity_hides_audit_identifiers_and_friendly_labels_internal_actions(self):
        dad, daughter = self.client('Dad'), self.client('Daughter')
        self.create(dad, 'dance', title='Solo Routine', danceType='routine')
        routine = next(item for item in self.call(dad, 'state')[1]['dance'] if item['title'] == 'Solo Routine')
        with server.connection() as db:
            server.audit_record(db, 'Dad', routine['id'], 'competition_unlinked',
                                {**routine, 'id': routine['id']},
                                {'competitionId': 987654, 'before': {'parentNotes': 'Never show this private audit detail'}})
        child_state = self.call(daughter, 'state')[1]
        activity = next(item for item in child_state['activity'] if 'dance links' in item['summary'])
        for internal_field in ('id', 'recordId', 'actor', 'action', 'details'):
            self.assertNotIn(internal_field, activity)
        for internal_value in ('competition_unlinked', 'competitionId', '987654', 'Never show this private audit detail'):
            self.assertNotIn(internal_value, json.dumps(child_state['activity']))
        parent_state = self.call(dad, 'state')[1]
        parent_summary = next(item['summary'] for item in parent_state['activity'] if 'dance links' in item['summary'])
        self.assertNotIn('competition_unlinked', parent_summary)

    def test_acknowledgements_are_individual_and_not_completed_reasons_are_audited(self):
        dad, mom, daughter = [self.client(n) for n in ('Dad', 'Mom', 'Daughter')]
        self.create(dad, 'tasks', title='Family homework reminder', who='Everyone', priority='Normal',
                    visibility='Family', category='School', ack=True)
        shared = next(t for t in self.call(daughter, 'state')[1]['tasks'] if t['title'] == 'Family homework reminder')
        for person, client in (('Daughter', daughter), ('Dad', dad), ('Mom', mom)):
            self.assertEqual(self.call(client, 'action', {'action': 'ack', 'id': shared['id']})[0], 200)
        task = next(t for t in self.call(dad, 'state')[1]['tasks'] if t['id'] == shared['id'])
        self.assertEqual({x['person'] for x in task['acknowledgements']}, {'Dad', 'Mom', 'Daughter'})
        self.assertTrue(all(x['acknowledgedAt'] for x in task['acknowledgements']))
        self.assertEqual(self.call(daughter, 'action', {'action': 'done', 'id': shared['id']})[0], 200)
        self.assertEqual(self.call(daughter, 'state')[1]['points'], 0)

        self.create(dad, 'tasks', title='Take out recycling', who='Daughter', priority='Normal',
                    visibility='Family', category='Home')
        chore = next(t for t in self.call(daughter, 'state')[1]['tasks'] if t['title'] == 'Take out recycling')
        result = {'action': 'miss', 'id': chore['id'], 'reasonCode': 'Ran out of time', 'explanation': 'School ran late'}
        self.assertEqual(self.call(daughter, 'action', result)[0], 200)
        missed = next(t for t in self.call(dad, 'state')[1]['tasks'] if t['id'] == chore['id'])
        self.assertEqual(missed['reasonBy'], 'Daughter')
        self.assertEqual(missed['reasonCode'], 'Ran out of time')
        self.assertEqual(missed['reasonExplanation'], 'School ran late')
        self.assertTrue(missed['reasonAt'])
        self.assertIn('Take out recycling', [e['title'] for e in self.call(dad, 'state')[1]['activity']])
        self.create(dad, 'tasks', title='Tidy toy shelf', who='Daughter', priority='Normal', visibility='Family', category='Home')
        optional = next(t for t in self.call(daughter, 'state')[1]['tasks'] if t['title'] == 'Tidy toy shelf')
        self.assertEqual(self.call(daughter, 'action', {'action': 'miss', 'id': optional['id'], 'reasonCode': 'Other'})[0], 200)

    def test_urgent_requires_assignee_ack_before_manager_completion(self):
        dad, daughter = self.client('Dad'), self.client('Daughter')
        self.create(dad, 'tasks', title='Urgent family chore', who='Daughter', priority='Urgent',
                    visibility='Family', category='Home', ack=False)
        urgent = next(t for t in self.call(daughter, 'state')[1]['tasks'] if t['title'] == 'Urgent family chore')
        self.assertTrue(urgent['ack'])
        self.assertEqual(self.call(dad, 'action', {'action': 'done', 'id': urgent['id'], 'manager': True})[0], 400)
        self.assertEqual(self.call(daughter, 'action', {'action': 'ack', 'id': urgent['id']})[0], 200)
        self.assertEqual(self.call(dad, 'action', {'action': 'done', 'id': urgent['id'], 'manager': True})[0], 200)
        completed = next(t for t in self.call(daughter, 'state')[1]['tasks'] if t['id'] == urgent['id'])
        self.assertEqual(completed['completedBy'], 'Dad')
        self.assertEqual(completed['completionHistory'][0]['completedBy'], 'Dad')

    def test_family_task_appears_through_real_http_api(self):
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{httpd.server_port}/api/'
        clients = {}
        try:
            for name in ('Dad', 'Mom', 'Daughter'):
                jar = http.cookiejar.CookieJar()
                client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
                login = urllib.request.Request(base + 'login', json.dumps({'name': name, 'password': 'testing-password'}).encode(), {'Content-Type': 'application/json', 'X-Hub-Request': '1'}, method='POST')
                with client.open(login) as response:
                    self.assertEqual(response.status, 200)
                clients[name] = client

            action = urllib.request.Request(base + 'action', json.dumps({
                'action': 'create', 'kind': 'tasks', 'record': {
                    'title': 'Dance bag ready', 'who': 'Daughter', 'priority': 'Normal',
                    'visibility': 'Family', 'description': 'Shoes and water', 'category': 'Home',
                },
            }).encode(), {'Content-Type': 'application/json', 'X-Hub-Request': '1'}, method='POST')
            with clients['Dad'].open(action) as response:
                self.assertEqual(response.status, 200)

            for name, client in clients.items():
                with client.open(base + 'state') as response:
                    state = json.load(response)
                task = next(t for t in state['tasks'] if t['title'] == 'Dance bag ready')
                self.assertEqual(task['who'], 'Daughter')
                self.assertEqual(task['description'], 'Shoes and water')
                self.assertEqual(state['viewer'], name)
                if name == 'Daughter':
                    task_id = task['id']

            def action_as(name, payload):
                request = urllib.request.Request(base + 'action', json.dumps(payload).encode(),
                    {'Content-Type': 'application/json', 'X-Hub-Request': '1'}, method='POST')
                with clients[name].open(request) as response:
                    self.assertEqual(response.status, 200)

            action_as('Daughter', {'action': 'done', 'id': task_id})
            for name in ('Dad', 'Mom'):
                with clients[name].open(base + 'state') as response:
                    state = json.load(response)
                self.assertIn('✅ Arielle completed "Dance bag ready"', [event['summary'] for event in state['activity']])

            action_as('Dad', {'action': 'reopen', 'id': task_id})
            action_as('Mom', {'action': 'edit', 'id': task_id, 'record': {'who': 'Dad'}})
            with clients['Dad'].open(base + 'state') as response:
                dad_state = json.load(response)
            reopened = next(t for t in dad_state['tasks'] if t['id'] == task_id)
            self.assertEqual(reopened['status'], 'open')
            self.assertEqual(reopened['who'], 'Dad')
            action_as('Dad', {'action': 'delete', 'id': task_id})
            with clients['Daughter'].open(base + 'state') as response:
                daughter_state = json.load(response)
            self.assertNotIn(task_id, [t['id'] for t in daughter_state['tasks']])
            with clients['Dad'].open(base + 'state') as response:
                dad_after_delete = json.load(response)
            self.assertIn('🗑️ Jermaine deleted "Dance bag ready"', [event['summary'] for event in dad_after_delete['activity']])
            with server.connection() as db:
                self.assertEqual(db.execute('SELECT deleted FROM records WHERE id=?', (task_id,)).fetchone()['deleted'], 1)
                self.assertGreaterEqual(db.execute('SELECT COUNT(*) FROM audit WHERE record_id=?', (task_id,)).fetchone()[0], 5)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()

    def test_requests_session_and_csrf(self):
        daughter, dad = self.client('Daughter'), self.client('Dad')
        self.create(daughter,'requests',text='Ride please',type='Ride')
        r = self.call(dad,'state')[1]['requests'][-1]
        decision=dict(action='decision',id=r['id'],status='Approved',reply='Yes')
        self.assertEqual(self.call(daughter,'action',decision)[0],403)
        self.assertEqual(self.call(dad,'action',decision,headers=False)[0],403)
        self.assertEqual(self.call(dad,'action',decision)[0],200)
        self.assertEqual(self.call(daughter,'state')[1]['requests'][-1]['status'],'Approved')
        self.assertEqual(self.call(daughter,'logout',{})[0],200)
        self.assertEqual(self.call(daughter,'state')[0],401)

    def test_ask_parents_request_flow_and_calendar_conversion(self):
        daughter, mom, dad = [self.client(n) for n in ('Daughter', 'Mom', 'Dad')]
        self.create(daughter, 'requests', title='Friends house', text='Can I go to my friend’s house?',
                    type='Permission', date='2026-10-12', time='18:30',
                    description='Her parent will bring me home after dinner.')
        request = next(r for r in self.call(dad, 'state')[1]['requests'] if r['title'] == 'Friends house')
        self.assertEqual(request['status'], 'Pending')
        self.assertEqual((request['type'], request['date'], request['time'], request['description']),
                         ('Permission', '2026-10-12', '18:30', 'Her parent will bring me home after dinner.'))
        for parent in (dad, mom):
            parent_request = next(r for r in self.call(parent, 'state')[1]['requests'] if r['id'] == request['id'])
            self.assertEqual(parent_request['description'], request['description'])
        with server.connection() as db:
            stored_request = json.loads(db.execute('SELECT body FROM records WHERE id=?', (request['id'],)).fetchone()['body'])
        self.assertEqual(stored_request['date'], '2026-10-12')
        self.assertEqual(stored_request['time'], '18:30')
        self.assertEqual(stored_request['description'], 'Her parent will bring me home after dinner.')
        self.assertFalse(any(event.get('sourceRequestId') == request['id'] for event in self.call(daughter, 'state')[1]['events']))
        parent_activity = self.call(dad, 'state')[1]['activity']
        daughter_activity = self.call(daughter, 'state')[1]['activity']
        self.assertIn('🙋 Arielle sent a Permission request "Friends house"', [entry['summary'] for entry in parent_activity])
        self.assertIn('🙋 Arielle sent a Permission request "Friends house"', [entry['summary'] for entry in daughter_activity])
        self.assertNotIn('created task "Friends house"', [entry['summary'] for entry in parent_activity])
        self.assertEqual(self.call(daughter, 'action', {'action': 'decision', 'id': request['id'], 'status': 'Approved', 'reply': 'Yes, after practice', 'addToCalendar': True})[0], 403)
        self.assertEqual(self.call(dad, 'action', {'action': 'reply', 'id': request['id'], 'reply': 'Which friend’s house?'})[0], 200)
        after_question = next(r for r in self.call(daughter, 'state')[1]['requests'] if r['id'] == request['id'])
        self.assertEqual(after_question['status'], 'Pending')
        self.assertEqual(after_question['responses'][-1]['by'], 'Dad')
        self.assertEqual(after_question['responses'][-1]['reply'], 'Which friend’s house?')
        self.assertTrue(after_question['responses'][-1]['createdAt'])
        self.assertEqual(next(r for r in self.call(mom, 'state')[1]['requests'] if r['id'] == request['id'])['responses'][-1]['reply'], 'Which friend’s house?')
        self.assertIn('💬 Jermaine replied to Arielle’s request "Friends house"', [entry['summary'] for entry in self.call(daughter, 'state')[1]['activity']])
        self.assertEqual(self.call(daughter, 'action', {'action': 'reply', 'id': request['id'], 'reply': 'Maya’s house'})[0], 200)
        self.assertEqual(self.call(dad, 'action', {'action': 'decision', 'id': request['id'], 'status': 'Approved', 'reply': 'Yes, after practice', 'addToCalendar': True})[0], 200)
        updated = next(r for r in self.call(daughter, 'state')[1]['requests'] if r['id'] == request['id'])
        self.assertEqual(updated['status'], 'Approved')
        self.assertIn('Yes, after practice', updated['reply'])
        self.assertEqual(updated['replyBy'], 'Dad')
        self.assertEqual(updated['responses'][-1]['reply'], 'Yes, after practice')
        approved_activity = [entry['summary'] for entry in self.call(daughter, 'state')[1]['activity']]
        self.assertIn('✅ Jermaine approved Arielle’s request "Friends house"', approved_activity)
        events = [event for event in self.call(daughter, 'state')[1]['events'] if event.get('sourceRequestId') == request['id']]
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]['date'], events[0]['startTime'], events[0]['description']),
                         ('2026-10-12', '18:30', 'Her parent will bring me home after dinner.'))
        self.assertEqual(self.call(dad, 'action', {'action': 'decision', 'id': request['id'], 'status': 'Approved', 'reply': 'Already approved', 'addToCalendar': True})[0], 200)
        self.assertEqual(sum(1 for e in self.call(daughter, 'state')[1]['events'] if e.get('sourceRequestId') == request['id']), 1)

        self.create(daughter, 'requests', title='Extra screen time', text='May I stay up later?', type='Permission',
                date='2026-10-15', time='20:00')
        second = next(r for r in self.call(mom, 'state')[1]['requests'] if r['title'] == 'Extra screen time')
        self.assertEqual(self.call(mom, 'action', {'action': 'decision', 'id': second['id'], 'status': 'Approved', 'reply': 'Only on Friday.'})[0], 200)
        self.assertFalse(any(event.get('sourceRequestId') == second['id'] for event in self.call(daughter, 'state')[1]['events']))
        self.create(daughter, 'requests', title='Sleepover', text='Can Maya sleep over?', type='Sleepover/Friend')
        denied = next(r for r in self.call(dad, 'state')[1]['requests'] if r['title'] == 'Sleepover')
        self.assertEqual(self.call(mom, 'action', {'action': 'decision', 'id': denied['id'], 'status': 'Denied', 'reply': 'Not this weekend, please ask again next week.'})[0], 200)
        denied_request = next(r for r in self.call(daughter, 'state')[1]['requests'] if r['id'] == denied['id'])
        self.assertEqual((denied_request['status'], denied_request['replyBy']), ('Denied', 'Mom'))
        self.assertEqual(denied_request['reply'], 'Not this weekend, please ask again next week.')
        self.assertIn('❌ Stephanie denied Arielle’s request "Sleepover"', [entry['summary'] for entry in self.call(daughter, 'state')[1]['activity']])

    def test_board_reactions_comments_pins_and_important_acknowledgements(self):
        dad, mom, daughter = [self.client(n) for n in ('Dad', 'Mom', 'Daughter')]
        self.assertEqual(self.call(daughter, 'action', {
            'action': 'create', 'kind': 'posts', 'record': {'text': 'Child announcement', 'important': True, 'ackRequired': True},
        })[0], 403)
        self.create(dad, 'posts', text='Dance bag needs to be packed tonight', pinned=False, important=True,
                    ackRequired=True, visibility='Me')
        post = next(p for p in self.call(daughter, 'state')[1]['posts'] if p['text'] == 'Dance bag needs to be packed tonight')
        self.assertEqual(post['visibility'], 'Family')
        self.assertIn(post['id'], [p['id'] for p in self.call(mom, 'state')[1]['posts']])
        self.assertEqual(self.call(daughter, 'action', {'action': 'react', 'id': post['id'], 'emoji': '❤️'})[0], 200)
        self.assertEqual(self.call(mom, 'action', {'action': 'comment', 'id': post['id'], 'comment': 'Thanks for the reminder'})[0], 200)
        self.assertEqual(self.call(daughter, 'action', {'action': 'pin', 'id': post['id'], 'pinned': True})[0], 403)
        self.assertEqual(self.call(dad, 'action', {'action': 'pin', 'id': post['id'], 'pinned': True})[0], 200)
        self.assertEqual(self.call(daughter, 'action', {'action': 'ack', 'id': post['id']})[0], 200)
        state = self.call(mom, 'state')[1]
        updated = next(p for p in state['posts'] if p['id'] == post['id'])
        self.assertTrue(updated['pinned'])
        self.assertTrue(updated['important'])
        self.assertIn('Daughter', updated['acknowledgedBy'])
        self.assertTrue(any(c['text'] == 'Thanks for the reminder' for c in updated['comments']))
        self.assertEqual(self.call(daughter, 'action', {'action': 'delete_comment', 'id': post['id'], 'commentId': next(c['id'] for c in updated['comments'])})[0], 403)

    def test_dance_competition_schedule_privacy_and_calendar_sync(self):
        dad, daughter = self.client('Dad'), self.client('Daughter')
        start = (server.local_today() + __import__('datetime').timedelta(days=12)).isoformat()
        end = (server.local_today() + __import__('datetime').timedelta(days=13)).isoformat()
        self.create(dad, 'dance', title='Harbor Classic', danceType='competition', startDate=start,
                    endDate=end, venue='Civic Hall', address='10 Main Street', schedulePending=True,
                    fees=[{'label': 'Entry fee', 'amount': 125}])
        parent_state = self.call(dad, 'state')[1]
        competition = next(item for item in parent_state['dance'] if item.get('danceType') == 'competition')
        self.assertTrue(competition['schedulePending'])
        self.assertEqual(competition['fees'][0]['amount'], 125)
        initial_events = [event for event in parent_state['events'] if event.get('sourceDanceId') == competition['id']]
        self.assertEqual({event['sourceDanceKey']: event['date'] for event in initial_events}, {
            'competition': start, 'competition:end': end,
        })

        child_state = self.call(daughter, 'state')[1]
        child_competition = next(item for item in child_state['dance'] if item['id'] == competition['id'])
        self.assertNotIn('fees', child_competition)
        self.assertNotIn('125', json.dumps(child_state))

        updated_date = (server.local_today() + __import__('datetime').timedelta(days=14)).isoformat()
        updated_end_date = (server.local_today() + __import__('datetime').timedelta(days=15)).isoformat()
        status, _ = self.call(dad, 'action', {'action': 'edit', 'id': competition['id'], 'record': {
            'startDate': updated_date, 'endDate': updated_end_date, 'schedulePending': False, 'performanceSchedule': 'Solo 10:15 AM',
        }})
        self.assertEqual(status, 200, _)
        updated_state = self.call(dad, 'state')[1]
        updated = next(item for item in updated_state['dance'] if item['id'] == competition['id'])
        self.assertEqual((updated['startDate'], updated['endDate']), (updated_date, updated_end_date))
        self.assertFalse(updated['schedulePending'])
        self.assertEqual(updated['performanceSchedule'], 'Solo 10:15 AM')
        linked_events = [event for event in updated_state['events'] if event.get('sourceDanceId') == competition['id']]
        self.assertEqual({event['sourceDanceKey']: event['date'] for event in linked_events}, {
            'competition': updated_date, 'competition:end': updated_end_date,
        })
        with server.connection() as db:
            db.execute("INSERT INTO reminders(account,source_kind,source_id,reminder_key,reminder_type,due_at,created_at) VALUES(?,?,?,?,?,?,?)",
                       ('Dad', 'events', linked_events[0]['id'], 'old-date-reminder', 'event_upcoming', updated_date, server.stamp()))
        third_date = (server.local_today() + __import__('datetime').timedelta(days=16)).isoformat()
        third_end_date = (server.local_today() + __import__('datetime').timedelta(days=17)).isoformat()
        status, response = self.call(dad, 'action', {'action': 'edit', 'id': competition['id'], 'record': {
            'startDate': third_date, 'endDate': third_end_date, 'arrivalTime': '08:15', 'callTime': '09:00',
            'venue': 'New Civic Hall', 'address': '25 New Street', 'schedulePending': True,
            'performanceSchedule': '',
        }})
        self.assertEqual(status, 200, response)
        repeated_state = self.call(dad, 'state')[1]
        repeated = next(item for item in repeated_state['dance'] if item['id'] == competition['id'])
        self.assertEqual((repeated['startDate'], repeated['endDate']), (third_date, third_end_date))
        self.assertEqual((repeated['arrivalTime'], repeated['callTime'], repeated['venue'], repeated['address']),
                         ('08:15', '09:00', 'New Civic Hall', '25 New Street'))
        self.assertTrue(repeated['schedulePending'])
        self.assertEqual(len([event for event in repeated_state['events'] if event.get('sourceDanceId') == competition['id']]), 2)
        self.assertEqual({event['sourceDanceKey']: event['date'] for event in repeated_state['events'] if event.get('sourceDanceId') == competition['id']}, {
            'competition': third_date, 'competition:end': third_end_date,
        })
        with server.connection() as db:
            stale_reminder = db.execute("SELECT dismissed_at FROM reminders WHERE account='Dad' AND reminder_key='old-date-reminder'").fetchone()
            self.assertTrue(stale_reminder['dismissed_at'])
        self.assertEqual(next(item for item in self.call(daughter, 'state')[1]['dance'] if item['id'] == competition['id'])['startDate'], third_date)
        self.assertEqual(self.call(dad, 'action', {'action': 'edit', 'id': linked_events[0]['id'], 'record': {'date': updated_date}})[0], 403)
        self.assertEqual(self.call(dad, 'action', {'action': 'delete', 'id': linked_events[0]['id']})[0], 403)

    def test_dance_routines_costumes_and_checklists(self):
        dad, daughter = self.client('Dad'), self.client('Daughter')
        competition_date = (server.local_today() + __import__('datetime').timedelta(days=20)).isoformat()
        self.create(dad, 'dance', title='Spring Showcase', danceType='competition', startDate=competition_date)
        competition = next(item for item in self.call(dad, 'state')[1]['dance'] if item.get('title') == 'Spring Showcase')
        self.create(dad, 'dance', title='Moonlight', danceType='routine', routineType='Solo',
                    choreographer='Ms. Lee', competitionIds=[competition['id']], scheduleInformation='Studio 2, Thursdays')
        routine = next(item for item in self.call(dad, 'state')[1]['dance'] if item.get('title') == 'Moonlight')
        self.create(dad, 'dance', title='Moonlight costume', danceType='costume', routineIds=[routine['id']],
                    ordered=True, received=False, alterationsNeeded=True, alterationsCompleted=False,
                    accessories='Hairpiece', shoes='Tan jazz shoes', tights='Pink tights', neededBy=competition_date)
        self.create(dad, 'dance', title='Competition Packing', danceType='checklist', competitionId=competition['id'],
                    checklistItems=['Costume', 'Shoes', 'Tights', 'Water'])

        daughter_dance = self.call(daughter, 'state')[1]['dance']
        visible_routine = next(item for item in daughter_dance if item.get('title') == 'Moonlight')
        visible_costume = next(item for item in daughter_dance if item.get('danceType') == 'costume')
        checklist = next(item for item in daughter_dance if item.get('danceType') == 'checklist')
        self.assertEqual(visible_routine['competitionIds'], [competition['id']])
        self.assertEqual(visible_costume['routineIds'], [routine['id']])
        self.assertTrue(visible_costume['alterationsNeeded'])
        self.assertEqual([item['text'] for item in checklist['checklistItems']], ['Costume', 'Shoes', 'Tights', 'Water'])
        self.assertEqual(self.call(daughter, 'action', {'action': 'checklist_item', 'id': checklist['id'], 'itemId': 1})[0], 200)
        updated_checklist = next(item for item in self.call(dad, 'state')[1]['dance'] if item['id'] == checklist['id'])
        self.assertTrue(updated_checklist['checklistItems'][0]['checked'])
        self.assertEqual(self.call(daughter, 'action', {'action': 'edit', 'id': routine['id'], 'record': {'title': 'Hijacked'}})[0], 403)

    def test_competition_deadlines_sync_without_duplicates_and_validate_associations(self):
        dad, daughter = self.client('Dad'), self.client('Daughter')
        start = (server.local_today() + __import__('datetime').timedelta(days=10)).isoformat()
        deadline_date = (server.local_today() + __import__('datetime').timedelta(days=5)).isoformat()
        self.create(dad, 'dance', title='Autumn Classic', danceType='competition', startDate=start,
                    endDate=start, venue='Civic Hall', schedulePending=True,
                    deadlines=[{'id': 'registration', 'title': 'Registration due', 'date': deadline_date}],
                    fees=[{'label': 'Entry fee', 'amount': 125}])
        competition = next(item for item in self.call(dad, 'state')[1]['dance'] if item.get('title') == 'Autumn Classic')
        primary_id = competition['id']
        daughter_state = self.call(daughter, 'state')[1]
        generated = [event for event in daughter_state['events'] if event.get('sourceDanceId') == primary_id]
        self.assertEqual(len(generated), 2)
        self.assertEqual({event['sourceDanceKey'] for event in generated}, {'competition', 'deadline:registration'})
        self.assertNotIn('fees', next(item for item in daughter_state['dance'] if item['id'] == primary_id))
        self.assertNotIn('125', json.dumps(daughter_state))

        updated_deadline = (server.local_today() + __import__('datetime').timedelta(days=6)).isoformat()
        status, response = self.call(dad, 'action', {'action': 'edit', 'id': primary_id, 'record': {
            'deadlines': [{'id': 'registration', 'title': 'Updated registration deadline', 'date': updated_deadline}],
            'schedulePending': False, 'performanceSchedule': 'Solo 10:15 AM',
        }})
        self.assertEqual(status, 200, response)
        updated_events = [event for event in self.call(daughter, 'state')[1]['events'] if event.get('sourceDanceId') == primary_id]
        self.assertEqual(len(updated_events), 2)
        linked_deadline = next(event for event in updated_events if event.get('sourceDanceKey') == 'deadline:registration')
        self.assertEqual(linked_deadline['date'], updated_deadline)
        self.assertIn('Updated registration deadline', linked_deadline['title'])

        routine_status, routine = self.call(dad, 'action', {'action': 'create', 'kind': 'dance', 'record': {
            'title': 'Autumn Solo', 'danceType': 'routine', 'routineType': 'Solo', 'competitionIds': [primary_id],
        }})
        self.assertEqual(routine_status, 200, routine)
        routine_id = next(item['id'] for item in self.call(dad, 'state')[1]['dance'] if item.get('title') == 'Autumn Solo')
        status, response = self.call(dad, 'action', {'action': 'edit', 'id': primary_id, 'record': {'routineIds': [routine_id]}})
        self.assertEqual(status, 200, response)
        self.assertIn(routine_id, next(item for item in self.call(dad, 'state')[1]['dance'] if item['id'] == primary_id)['routineIds'])
        invalid, response = self.call(dad, 'action', {'action': 'create', 'kind': 'dance', 'record': {
            'title': 'Broken Routine', 'danceType': 'routine', 'competitionIds': [999999],
        }})
        self.assertEqual(invalid, 400, response)

    def test_competition_deadline_completion_resolves_dance_badge_and_linked_reminder(self):
        dad, daughter = self.client('Dad'), self.client('Daughter')
        start = (server.local_today() + __import__('datetime').timedelta(days=15)).isoformat()
        deadline_date = (server.local_today() + __import__('datetime').timedelta(days=2)).isoformat()
        self.create(dad, 'dance', title='Deadline Test Comp', danceType='competition', startDate=start,
                    schedulePending=False, deadlines=[{'id': 'payment', 'title': 'Payment due', 'date': deadline_date}],
                    reminderOffsets=[0])
        competition = next(item for item in self.call(daughter, 'state')[1]['dance'] if item.get('title') == 'Deadline Test Comp')
        daughter_state = self.call(daughter, 'state')[1]
        attention = next(item for item in daughter_state['danceAttention'] if item['id'] == competition['id'])
        self.assertIn('Payment due', ' '.join(attention['reasons']))
        self.assertEqual(daughter_state['badgeCounts']['dance'], 1)
        dance_notices = [item for item in daughter_state['reminders'] if item['notificationCategory'] == 'dance' and item['sourceId'] == competition['id'] and not item['read']]
        for notice in dance_notices:
            self.assertEqual(self.call(daughter, 'reminders/action', {'id': notice['id'], 'action': 'read'})[0], 200)
        self.assertEqual(self.call(daughter, 'state')[1]['badgeCounts']['dance'], 1)
        linked_event = next(event for event in daughter_state['events'] if event.get('sourceDanceId') == competition['id'] and event.get('sourceDanceKey') == 'deadline:payment')
        with server.connection() as db:
            db.execute("INSERT INTO reminders(account,source_kind,source_id,reminder_key,reminder_type,due_at,created_at) VALUES(?,?,?,?,?,?,?)",
                       ('Daughter', 'events', linked_event['id'], 'dance-deadline-test', 'event_upcoming', deadline_date, server.stamp()))

        self.assertEqual(self.call(daughter, 'action', {'action': 'deadline_done', 'id': competition['id'], 'deadlineId': 'payment'})[0], 200)
        completed_state = self.call(daughter, 'state')[1]
        self.assertFalse(any(item['id'] == competition['id'] for item in completed_state['danceAttention']))
        self.assertEqual(completed_state['badgeCounts']['dance'], 0)
        completed = next(item for item in completed_state['dance'] if item['id'] == competition['id'])
        self.assertTrue(completed['deadlines'][0]['completed'])
        self.assertIn('completed a competition deadline', ' '.join(item['summary'] for item in completed_state['activity']))
        with server.connection() as db:
            reminder = db.execute("SELECT dismissed_at FROM reminders WHERE reminder_key='dance-deadline-test'").fetchone()
            self.assertTrue(reminder['dismissed_at'])
            before = db.execute("SELECT COUNT(*) FROM audit WHERE record_id=? AND action='deadline_completed'", (competition['id'],)).fetchone()[0]
        self.assertEqual(self.call(daughter, 'action', {'action': 'deadline_done', 'id': competition['id'], 'deadlineId': 'payment'})[0], 200)
        with server.connection() as db:
            after = db.execute("SELECT COUNT(*) FROM audit WHERE record_id=? AND action='deadline_completed'", (competition['id'],)).fetchone()[0]
        self.assertEqual(after, before)

    def test_dance_me_only_and_adult_records_are_filtered_server_side(self):
        dad, mom, daughter = [self.client(name) for name in ('Dad', 'Mom', 'Daughter')]
        with server.connection() as db:
            for creator, visibility, title in (
                ('Dad', 'Me', 'Jermaine private dance budget'),
                ('Mom', 'Me', 'Stephanie private dance budget'),
                ('Dad', 'Adults', 'Adult dance budget'),
            ):
                db.execute('INSERT INTO records(kind,body) VALUES(?,?)', ('dance', json.dumps({
                    'title': title, 'danceType': 'competition', 'visibility': visibility, 'creator': creator,
                    'fees': [{'label': 'Entry fee', 'amount': 125}], 'financials': {'total': 125},
                })))
        daughter_state = self.call(daughter, 'state')[1]
        self.assertFalse(any('budget' in item.get('title', '') for item in daughter_state['dance']))
        self.assertNotIn('125', json.dumps(daughter_state))
        self.assertNotIn('Jermaine private dance budget', json.dumps(self.call(mom, 'state')[1]))
        self.assertNotIn('Stephanie private dance budget', json.dumps(self.call(dad, 'state')[1]))

    def test_chore_streak_excludes_unrelated_tasks_and_achievements_are_stable(self):
        daughter = self.client('Daughter')
        today = server.local_today()
        dates = [today - __import__('datetime').timedelta(days=offset) for offset in range(6, -1, -1)]
        with server.connection() as db:
            for index in range(25):
                day = dates[index] if index < 6 else dates[6]
                completed_at = __import__('datetime').datetime.combine(day, __import__('datetime').time(12), server.family_timezone()).isoformat()
                task = {'title': f'Chore {index}', 'category': 'Home', 'who': 'Daughter', 'creator': 'Dad',
                        'visibility': 'Family', 'chore': True, 'status': 'done',
                        'completedBy': 'Daughter', 'completedAt': completed_at,
                        'completionHistory': [{'completedBy': 'Daughter', 'completedAt': completed_at}]}
                db.execute('INSERT INTO records(kind,body) VALUES(?,?)', ('tasks', json.dumps(task)))
            unrelated = {'title': 'Homework', 'category': 'School', 'who': 'Daughter', 'creator': 'Dad',
                         'visibility': 'Family', 'chore': True, 'status': 'done', 'completedBy': 'Daughter',
                         'completedAt': __import__('datetime').datetime.combine(today, __import__('datetime').time(13), server.family_timezone()).isoformat(),
                         'completionHistory': [{'completedBy': 'Daughter', 'completedAt': __import__('datetime').datetime.combine(today, __import__('datetime').time(13), server.family_timezone()).isoformat()}]}
            db.execute('INSERT INTO records(kind,body) VALUES(?,?)', ('tasks', json.dumps(unrelated)))
            private = dict(unrelated, title='Private dance chore', category='Home', chore=True,
                           visibility='Adults', completionHistory=[{'completedBy': 'Daughter', 'completedAt': __import__('datetime').datetime.combine(today, __import__('datetime').time(14), server.family_timezone()).isoformat()}])
            db.execute('INSERT INTO records(kind,body) VALUES(?,?)', ('tasks', json.dumps(private)))

        state = self.call(daughter, 'state')[1]
        self.assertEqual(state['points'], 25)
        self.assertEqual(state['streak'], 7)
        self.assertFalse(next(task for task in state['tasks'] if task['title'] == 'Homework')['chore'])
        self.assertEqual({achievement['id'] for achievement in state['achievements'] if achievement['earned']},
                         {'streak_7', 'chores_25', 'perfect_week'})
        second_read = self.call(daughter, 'state')[1]
        self.assertEqual(state['achievements'], second_read['achievements'])
        self.assertEqual(len({achievement['id'] for achievement in state['achievements']}), len(state['achievements']))
        self.assertNotIn('Private dance chore', [task['title'] for task in state['tasks']])

    def test_only_parents_can_issue_family_recognition(self):
        dad, daughter = self.client('Dad'), self.client('Daughter')
        self.assertEqual(self.call(daughter, 'action', {'action': 'create', 'kind': 'recognitions', 'record': {
            'recognitionType': 'Proud of You', 'message': 'You worked hard today.'}})[0], 403)
        self.create(dad, 'recognitions', recognitionType='Proud of You', message='You worked hard today.')
        state = self.call(daughter, 'state')[1]
        self.assertEqual(state['recognitions'][0]['recognitionType'], 'Proud of You')
        self.assertIn('🌟 Jermaine recognized Arielle: Proud of You', [entry['summary'] for entry in state['activity']])
        self.create(dad, 'tasks', title='Pack dance bag', who='Daughter', category='Home', visibility='Family')
        task = next(item for item in self.call(daughter, 'state')[1]['tasks'] if item['title'] == 'Pack dance bag')
        self.assertEqual(self.call(dad, 'action', {'action': 'create', 'kind': 'recognitions', 'record': {
            'recognitionType': 'Great Job', 'sourceTaskId': task['id'],
        }})[0], 400)
        self.assertEqual(self.call(daughter, 'action', {'action': 'done', 'id': task['id']})[0], 200)
        self.assertEqual(self.call(dad, 'action', {'action': 'create', 'kind': 'recognitions', 'record': {
            'recognitionType': 'Great Job', 'sourceTaskId': task['id'],
        }})[0], 200)

    def test_competition_delete_removes_only_generated_calendar_data_and_unlinks_associations(self):
        dad, mom, daughter = [self.client(name) for name in ('Dad', 'Mom', 'Daughter')]
        date = (server.local_today() + __import__('datetime').timedelta(days=30)).isoformat()
        self.create(dad, 'events', title='Unrelated family dinner', date=date, category='Family', allDay=True)
        self.create(dad, 'tasks', title='Unrelated family task', who='Everyone', category='Home', visibility='Family')
        self.create(dad, 'dance', title='Delete Me Classic', danceType='competition', startDate=date,
                    endDate=date, deadlines=[{'id': 'entry', 'title': 'Entry due', 'date': date}],
                    fees=[{'label': 'Entry fee', 'amount': 125}])
        state = self.call(dad, 'state')[1]
        competition = next(item for item in state['dance'] if item.get('title') == 'Delete Me Classic')
        competition_events = [event for event in state['events'] if event.get('sourceDanceId') == competition['id']]
        self.assertEqual(len(competition_events), 2)
        with server.connection() as db:
            for event in competition_events:
                db.execute("INSERT INTO reminders(account,source_kind,source_id,reminder_key,reminder_type,due_at,created_at) VALUES(?,?,?,?,?,?,?)",
                           ('Daughter', 'events', event['id'], f"delete-test:{event['id']}", 'event_upcoming', date, server.stamp()))
        self.create(dad, 'dance', title='Independent Routine', danceType='routine', competitionIds=[competition['id']])
        routine = next(item for item in self.call(dad, 'state')[1]['dance'] if item.get('title') == 'Independent Routine')
        self.create(dad, 'dance', title='Unrelated Routine', danceType='routine')
        unrelated_routine = next(item for item in self.call(dad, 'state')[1]['dance'] if item.get('title') == 'Unrelated Routine')
        self.create(dad, 'dance', title='Independent Costume', danceType='costume', routineIds=[routine['id']])
        costume = next(item for item in self.call(dad, 'state')[1]['dance'] if item.get('title') == 'Independent Costume')
        self.create(dad, 'dance', title='Independent Checklist', danceType='checklist', competitionId=competition['id'], checklistItems=['Shoes'])
        checklist = next(item for item in self.call(dad, 'state')[1]['dance'] if item.get('title') == 'Independent Checklist')
        self.assertEqual(self.call(daughter, 'action', {'action': 'delete', 'id': competition['id']})[0], 403)

        self.assertEqual(self.call(dad, 'action', {'action': 'delete', 'id': competition['id']})[0], 200)
        parent_state = self.call(dad, 'state')[1]
        daughter_state = self.call(daughter, 'state')[1]
        for state_after in (parent_state, daughter_state):
            self.assertNotIn(competition['id'], [item['id'] for item in state_after['dance']])
            self.assertFalse(any(event.get('sourceDanceId') == competition['id'] for event in state_after['events']))
            self.assertIn('Unrelated family dinner', [event['title'] for event in state_after['events']])
            self.assertIn('Unrelated family task', [item['title'] for item in state_after['tasks']])
            self.assertNotIn('Delete Me Classic', json.dumps(state_after['reminders']))
        remaining_dance = {item['id']: item for item in daughter_state['dance']}
        self.assertEqual(remaining_dance[routine['id']]['competitionIds'], [])
        self.assertIn(unrelated_routine['id'], remaining_dance)
        self.assertEqual(remaining_dance[checklist['id']]['competitionId'], None)
        self.assertIn(costume['id'], remaining_dance)
        self.assertIn('🗑️ Jermaine deleted "Delete Me Classic"', [event['summary'] for event in daughter_state['activity']])
        with server.connection() as db:
            self.assertEqual(db.execute('SELECT deleted FROM records WHERE id=?', (competition['id'],)).fetchone()['deleted'], 1)
            self.assertTrue(all(db.execute('SELECT dismissed_at FROM reminders WHERE reminder_key=?', (f"delete-test:{event['id']}",)).fetchone()['dismissed_at'] for event in competition_events))

    def test_recognition_seen_receipts_are_daughter_only_and_persist_across_login(self):
        dad, mom, daughter = [self.client(name) for name in ('Dad', 'Mom', 'Daughter')]
        self.create(dad, 'recognitions', recognitionType='Great Job', message='You did it!')
        first = next(item for item in self.call(daughter, 'state')[1]['recognitions'] if item['recognitionType'] == 'Great Job')
        self.assertFalse(first['seenAt'])
        self.assertEqual(self.call(mom, 'recognitions/action', {'action': 'seen', 'id': first['id']})[0], 403)

        self.create(mom, 'recognitions', recognitionType='Proud of You', message='Keep shining!')
        unseen = [item for item in self.call(daughter, 'state')[1]['recognitions'] if not item['seenAt']]
        self.assertEqual(len(unseen), 2)
        self.assertEqual(self.call(daughter, 'recognitions/action', {'action': 'seen', 'id': first['id']})[0], 200)
        refreshed = self.call(daughter, 'state')[1]['recognitions']
        self.assertTrue(next(item for item in refreshed if item['id'] == first['id'])['seenAt'])
        self.assertEqual(sum(not item['seenAt'] for item in refreshed), 1)
        self.assertEqual(self.call(dad, 'recognitions/action', {'action': 'seen', 'id': unseen[1]['id']})[0], 403)

        self.assertEqual(self.call(daughter, 'logout', {})[0], 200)
        self.create(dad, 'recognitions', recognitionType='Nice Work', message='Welcome back!')
        daughter = self.client('Daughter')
        after_login = self.call(daughter, 'state')[1]['recognitions']
        self.assertTrue(any(item['recognitionType'] == 'Nice Work' and not item['seenAt'] for item in after_login))
        self.assertEqual(self.call(daughter, 'recognitions/action', {'action': 'seen', 'id': first['id']})[0], 200)
        self.assertTrue(next(item for item in self.call(daughter, 'state')[1]['recognitions'] if item['id'] == first['id'])['seenAt'])

if __name__ == '__main__':
    unittest.main()
