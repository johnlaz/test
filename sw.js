// Transitional worker (safe to delete after a few months).
// ARMORY now lives in /app/. Installs created before the move still have a worker registered at
// this path; this file replaces it, clears only the OLD caches (names like "armory-v3.2"),
// then unregisters itself. The new app worker (/app/sw.js) caches as "armory-vX.Y.Z" and is left alone.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => /^armory-v\d+(\.\d+)?$/.test(k)).map(k => caches.delete(k)));
    await self.registration.unregister();
  })());
});
