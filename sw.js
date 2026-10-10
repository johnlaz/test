/* BRVTY service worker.
   The version comes from the page: it registers sw.js?v=<APP_VERSION>, so the cache name
   (brvty-v<ver>) always matches the version shown in the app. Bump APP_VERSION in index.html only. */
const VERSION = new URL(self.location.href).searchParams.get('v') || 'dev';
const CACHE_NAME = 'brvty-v' + VERSION;
const SCOPE = self.registration.scope;
const u = p => new URL(p, SCOPE).href;
const OFFLINE_URL = u('offline.html');
const INDEX_URL = u('index.html');

// Every file listed here must exist; install fails loudly otherwise.
const PRECACHE = ['./', 'index.html', 'manifest.json', 'offline.html', 'icon-192.png', 'icon-512.png', 'icon-180.png'].map(u);

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE_NAME).then(c => c.addAll(PRECACHE)));
  // No skipWaiting here: the page shows an "update ready" banner and sends SKIP_WAITING when tapped.
});

self.addEventListener('message', e => {
  if (e.data && e.data.type === 'SKIP_WAITING') self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k.startsWith('brvty-') && k !== CACHE_NAME).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Google Fonts: stale-while-revalidate so type survives offline
  if (url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com') {
    e.respondWith(caches.open(CACHE_NAME).then(c => c.match(req).then(hit => {
      const net = fetch(req).then(r => { if (r && (r.ok || r.type === 'opaque')) c.put(req, r.clone()); return r; }).catch(() => hit);
      return hit || net;
    })));
    return;
  }

  // Everything else cross-origin (Gemini, Open Library, iTunes, pdf.js CDN) goes straight to the network
  if (url.origin !== self.location.origin) return;

  // Never cache big user files or ranged media requests
  if (/\.(folio|apk)$/i.test(url.pathname) || req.headers.has('range')) return;

  // Pages: network-first, fall back to the cached shell, then the offline page
  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req).then(r => {
        if (r && r.ok && !url.search) { const copy = r.clone(); caches.open(CACHE_NAME).then(c => c.put(INDEX_URL, copy)); }
        return r;
      }).catch(() => caches.match(INDEX_URL).then(r => r || caches.match(OFFLINE_URL)))
    );
    return;
  }

  // Static assets: cache-first with background refresh
  e.respondWith(caches.match(req).then(cached => {
    const net = fetch(req).then(r => {
      if (r && r.status === 200 && r.type === 'basic') { const copy = r.clone(); caches.open(CACHE_NAME).then(c => c.put(req, copy)); }
      return r;
    }).catch(() => cached);
    return cached || net;
  }));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: 'window' }).then(list => {
    for (const c of list) if ('focus' in c) return c.focus();
    if (self.clients.openWindow) return self.clients.openWindow(SCOPE);
  }));
});
