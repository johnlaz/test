// ARMORY service worker.
// The cache name follows the app version: index.html registers "sw.js?v=<APP_VERSION>",
// so bumping APP_VERSION in the app is the only thing needed to roll a new cache.
const VERSION = new URL(self.location.href).searchParams.get('v') || 'dev';
const CACHE = 'armory-v' + VERSION;

// Every file listed here must exist — install fails (and the old worker stays) if one is missing.
const SHELL = [
  './',
  './index.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png',
  './vendor/xlsx.full.min.js',
  './demo/riptide.jpg',
  './demo/pathfinder.jpg',
  './demo/sandstorm.jpg',
  './demo/icebreaker.jpg',
  './demo/dracarys.jpg',
  './demo/paladin.jpg',
  './demo/ddm4isr.jpg'
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k.startsWith('armory-') && k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;           // Groq API, anything cross-origin: straight to network

  // Pages: network first so a new release always wins; cached shell when offline.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)
        .then(res => {
          if (res && res.status === 200) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
          return res;
        })
        .catch(() => caches.match(req, { ignoreSearch: true }).then(r => r || caches.match('./index.html')))
    );
    return;
  }

  // Assets: cache first, fill the cache on first use.
  event.respondWith(
    caches.match(req).then(hit => hit || fetch(req).then(res => {
      if (res && res.status === 200 && res.type === 'basic') { const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
      return res;
    }))
  );
});
