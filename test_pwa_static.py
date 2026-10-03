"""Static PWA checks: installability assets exist, the service worker never touches API or health routes."""
import json
import re
import unittest
from pathlib import Path

import server

ROOT = Path(__file__).parent


class PwaStaticTests(unittest.TestCase):
    def test_manifest_is_installable(self):
        manifest = json.loads((ROOT / "manifest.json").read_text())
        self.assertEqual((manifest["start_url"], manifest["scope"], manifest["display"]), ("/", "/", "standalone"))
        sizes = {icon["sizes"] for icon in manifest["icons"]}
        self.assertTrue({"192x192", "512x512"} <= sizes)
        for icon in manifest["icons"]:
            self.assertTrue((ROOT / icon["src"].lstrip("/")).is_file(), icon["src"])
            self.assertIn(icon["src"].lstrip("/"), server.STATIC)
        html = (ROOT / "index.html").read_text()
        self.assertIn('rel="manifest" href="/manifest.json"', html)
        self.assertIn("serviceWorker", html)

    def test_service_worker_caches_only_the_public_shell(self):
        worker = (ROOT / "sw.js").read_text()
        shell = re.search(r"const SHELL=\[(.*?)\]", worker).group(1)
        for path in re.findall(r"'/([^']*)'", shell):
            name = path or "index.html"
            self.assertIn(name, server.STATIC, name)
            self.assertTrue((ROOT / name).is_file(), name)
        self.assertIn("pathname.startsWith('/api/')", worker)       # API responses are never cached
        self.assertIn("method!=='GET'", worker)
        self.assertIn("SHELL.includes(u.pathname)", worker)          # /healthz and /readyz fall through to the network
        for forbidden in ("/healthz", "/readyz"):
            self.assertNotIn(forbidden, worker)
        self.assertNotIn("api", shell)

    def test_service_worker_cache_is_versioned_and_old_caches_are_removed(self):
        worker = (ROOT / "sw.js").read_text()
        self.assertRegex(worker, r"const CACHE='mrhub-server-v\d+'")
        self.assertIn("keys.filter(k=>k!==CACHE).map(k=>caches.delete(k))", worker)
        self.assertIn("fetch(e.request).catch(()=>caches.match(e.request))", worker)  # network first, cache only as an offline fallback

    def test_client_sends_the_csrf_header_and_same_origin_requests_only(self):
        app = (ROOT / "app.js").read_text()
        self.assertIn("fetch('/api/'+path", app)
        self.assertIn("'X-Hub-Request':'1'", app)


if __name__ == "__main__":
    unittest.main()
