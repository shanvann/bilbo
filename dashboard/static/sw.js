// BILBO PWA service worker.
// Strategy: cache the static shell so the app launches offline; always
// hit the network first for /api/* (fall back to cache only if offline).
//
// Cloudflare Access service token is injected by Flask at serve-time
// (see /sw.js route in dashboard/app.py). The SW adds these headers to
// every same-origin fetch so the installed PWA keeps working when the
// user's Access SSO cookie has expired.

const CACHE_VERSION = 'bilbo-shell-v2';
const CF_ACCESS_CLIENT_ID = '__CF_ACCESS_CLIENT_ID__';
const CF_ACCESS_CLIENT_SECRET = '__CF_ACCESS_CLIENT_SECRET__';

const SHELL_ASSETS = [
  '/',
  '/static/style.css',
  '/static/app.js',
  '/static/manifest.webmanifest',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/apple-touch-icon.png',
];

// Build a Request that carries the Access service-token headers.
// `redirect: 'follow'` is the default but explicit here so we get the
// final origin response, not the SSO redirect, when there's no token
// match. `res.redirected === true` is then the signal that auth failed.
function authedRequest(req) {
  const headers = new Headers(req.headers);
  if (CF_ACCESS_CLIENT_ID && CF_ACCESS_CLIENT_SECRET) {
    headers.set('CF-Access-Client-Id', CF_ACCESS_CLIENT_ID);
    headers.set('CF-Access-Client-Secret', CF_ACCESS_CLIENT_SECRET);
  }
  // The fetch handler upstream filters to same-origin requests only, so
  // 'same-origin' mode is correct for both navigations and subresources.
  // 'navigate' mode can't be set on a constructed Request, so we coerce.
  return new Request(req.url, {
    method: req.method,
    headers,
    body: req.method === 'GET' || req.method === 'HEAD' ? undefined : req.body,
    mode: 'same-origin',
    credentials: req.credentials,
    redirect: 'follow',
  });
}

// Don't cache responses that came back as an SSO redirect — caching a
// redirect-to-cloudflareaccess.com would poison /api/status forever.
function cacheable(res) {
  return res && res.ok && !res.redirected && res.type === 'basic';
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION)
      .then((cache) => Promise.all(
        SHELL_ASSETS.map((url) =>
          fetch(authedRequest(new Request(url)))
            .then((res) => cacheable(res) ? cache.put(url, res) : null)
            .catch(() => null)
        )
      ))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;  // let cross-origin pass through

  // API + frame data: network-first, cache fallback only if offline.
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      fetch(authedRequest(req))
        .then((res) => {
          if (cacheable(res)) {
            const copy = res.clone();
            caches.open(CACHE_VERSION).then((c) => c.put(req, copy));
          }
          return res;
        })
        .catch(() => caches.match(req))
    );
    return;
  }

  // Shell assets: cache-first, fall back to network.
  event.respondWith(
    caches.match(req).then((cached) => cached || fetch(authedRequest(req)).then((res) => {
      if (cacheable(res)) {
        const copy = res.clone();
        caches.open(CACHE_VERSION).then((c) => c.put(req, copy));
      }
      return res;
    }).catch(() => {
      // Last-resort fallback for navigations: serve cached index.
      if (req.mode === 'navigate') return caches.match('/');
    }))
  );
});
