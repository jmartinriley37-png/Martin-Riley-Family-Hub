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
            finally:
                server.DB = previous_db

    def test_visibility_and_history_never_leak(self):
        dad, mom, daughter = [self.client(n) for n in ('Dad', 'Mom', 'Daughter')]
        for v, text in [('Family','shared'),('Adults','adult-secret'),('Me','dad-secret'),('Assigned','mom-secret')]:
            self.create(dad, 'notes', text=text, visibility=v, who='Mom')
        ds = self.call(daughter, 'state')[1]
        self.assertEqual([n['text'] for n in ds['notes']], ['shared'])
        self.assertEqual(len(ds['activity']), 1)
        self.assertIn('Jermaine created', ds['activity'][0]['summary'])
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

    def test_activity_does_not_leak_previous_private_task_values(self):
        dad, daughter = self.client('Dad'), self.client('Daughter')
        self.create(dad, 'tasks', title='Private appointment', description='Private calendar details',
                    who='Dad', priority='Normal', visibility='Me', category='Appointments')
        task = next(t for t in self.call(dad, 'state')[1]['tasks'] if t['title'] == 'Private appointment')
        self.assertNotIn('Private appointment', [t['title'] for t in self.call(daughter, 'state')[1]['tasks']])
        self.assertEqual(self.call(dad, 'action', {'action': 'edit', 'id': task['id'], 'record': {'visibility': 'Family'}})[0], 200)
        state = self.call(daughter, 'state')[1]
        self.assertIn('Private appointment', [t['title'] for t in state['tasks']])
        edit = next(e for e in state['activity'] if e['recordId'] == task['id'] and e['action'] == 'edit')
        self.assertEqual(edit['details'], {})
        self.assertNotIn('Private calendar details', json.dumps(state['activity']))

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

if __name__ == '__main__':
    unittest.main()
