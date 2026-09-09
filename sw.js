// CACHE_VERSION is auto-rewritten by build.py to a hash of the rendered
// index.html on every build, which is what makes installed PWAs detect
// updates — the SW bytes change → registration.update() sees a new worker
// → install/activate runs → controllerchange fires → page reloads.
// Don't hand-edit this line; bumping it manually is fine but unnecessary.
const CACHE_VERSION = '74feb5448454';
const CACHE_NAME = `hilary-elliot-${CACHE_VERSION}`;

// Icon and manifest URLs carry ?v=${CACHE_VERSION} so iOS treats each new
// build as a fresh resource — without the query string the OS holds onto
// the home-screen icon it grabbed at install time even when the PNG bytes
// on disk change. The page in index.html requests the same versioned URLs,
// so the pre-cache keys here line up with what the page asks for.
const APP_SHELL = [
  '/',
  '/index.html',
  `/manifest.webmanifest?v=${CACHE_VERSION}`,
  `/icons/icon.svg?v=${CACHE_VERSION}`,
  `/icons/icon-192.png?v=${CACHE_VERSION}`,
  `/icons/icon-512.png?v=${CACHE_VERSION}`,
  `/icons/icon-maskable-512.png?v=${CACHE_VERSION}`,
  `/icons/apple-touch-icon.png?v=${CACHE_VERSION}`,
  '/images/icons/sneaker.png',
  '/images/icons/sandal.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  // Navigation: network-first, fall back to cached index.html for offline shell.
  // Cache writes go through event.waitUntil so the SW lifetime covers the body
  // drain — without it, the teed response keeps the navigation request marked
  // "pending" in the browser (spinner stays forever) until the cache stream
  // drains, and the SW can be killed mid-write.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)
        .then((res) => {
          event.waitUntil(
            caches.open(CACHE_NAME).then((c) => c.put('/index.html', res.clone()))
          );
          return res;
        })
        .catch(() => caches.match('/index.html'))
    );
    return;
  }

  // Same-origin static assets: cache-first.
  if (url.origin === self.location.origin) {
    event.respondWith(
      caches.match(req).then((cached) => {
        if (cached) return cached;
        return fetch(req).then((res) => {
          if (res.ok && res.type === 'basic') {
            event.waitUntil(
              caches.open(CACHE_NAME).then((c) => c.put(req, res.clone()))
            );
          }
          return res;
        });
      })
    );
    return;
  }

  // Cross-origin (fonts etc.): stale-while-revalidate. When we already have a
  // cached copy, return it immediately and let the background revalidation run
  // under event.waitUntil so a slow or hung font fetch doesn't leave a dangling
  // request that keeps the page's loading indicator spinning.
  event.respondWith(
    caches.match(req).then((cached) => {
      const network = fetch(req).then((res) => {
        if (res.ok) {
          event.waitUntil(
            caches.open(CACHE_NAME).then((c) => c.put(req, res.clone()))
          );
        }
        return res;
      });
      if (cached) {
        event.waitUntil(network.catch(() => {}));
        return cached;
      }
      return network.catch(() => cached);
    })
  );
});
