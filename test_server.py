import http.cookiejar
import json
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
                c.execute('INSERT INTO users VALUES(?,?,?)', (name, salt, server.password_hash('testing-password', salt)))


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

    def test_visibility_and_history_never_leak(self):
        dad, mom, daughter = [self.client(n) for n in ('Dad', 'Mom', 'Daughter')]
        for v, text in [('Family','shared'),('Adults','adult-secret'),('Me','dad-secret'),('Assigned','mom-secret')]:
            self.create(dad, 'notes', text=text, visibility=v, who='Mom')
        ds = self.call(daughter, 'state')[1]
        self.assertEqual([n['text'] for n in ds['notes']], ['shared'])
        self.assertEqual(len(ds['activity']), 1)
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
