/* Market Stretch Terminal service worker.
   Bump VERSION with every release; it must match the version stamp in index.html. */
const VERSION = '1.0.0';
const CACHE = 'mst-' + VERSION;
const SHELL = ['./', 'index.html', 'manifest.json', 'icon-192.png', 'icon-512.png', 'apple-touch-icon.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k.startsWith('mst-') && k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function networkFirst(req, fallbackKey) {
  return fetch(req)
    .then(res => {
      if (res && res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(fallbackKey || req, copy)); }
      return res;
    })
    .catch(() => caches.match(fallbackKey || req).then(hit => hit || Response.error()));
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;            // never touch third-party APIs (FMP etc.)

  if (url.pathname.endsWith('/market.json')) {           // data: always try fresh first
    e.respondWith(networkFirst(new Request(req.url, { cache: 'no-store' }), url.origin + url.pathname));
    return;
  }
  if (req.mode === 'navigate' || req.destination === 'document') {   // HTML: network-first, offline fallback
    e.respondWith(
      fetch(req).then(res => {
        if (res && res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put('index.html', copy)); }
        return res;
      }).catch(() => caches.match('index.html').then(hit => hit || caches.match('./')))
    );
    return;
  }
  e.respondWith(                                          // assets: cache-first
    caches.match(req).then(hit => hit || fetch(req).then(res => {
      if (res && res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
      return res;
    }))
  );
});
