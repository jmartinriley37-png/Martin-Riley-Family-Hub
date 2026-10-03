// Cache the public shell only. Family API responses never enter offline storage.
const CACHE='mrhub-server-v3';
const SHELL=['/','/index.html','/styles.css','/app.js','/manifest.json','/icon.svg'];
self.addEventListener('install',e=>{self.skipWaiting();e.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL)))});
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',e=>{
 const u=new URL(e.request.url);
 if(e.request.method!=='GET'||u.origin!==location.origin||u.pathname.startsWith('/api/'))return;
 if(!SHELL.includes(u.pathname))return;
 e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)));
});
