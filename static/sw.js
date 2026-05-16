// ExamGuard Service Worker v2.0
// Implements cache-first for static assets, network-first for pages,
// and a full offline fallback page.

const CACHE_NAME = 'examguard-v2';
const OFFLINE_URL = '/offline.html';

// Static assets to pre-cache on install
const PRECACHE_ASSETS = [
  '/',
  '/offline.html',
  '/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  // Google Fonts (cached on first fetch via the fetch handler below)
];

// ── Install: pre-cache critical assets ───────────────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      return cache.addAll(PRECACHE_ASSETS);
    }).then(() => {
      // Activate immediately without waiting for old tabs to close
      return self.skipWaiting();
    })
  );
});

// ── Activate: clean up old caches ────────────────────────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => {
      return Promise.all(
        keys
          .filter(key => key !== CACHE_NAME)
          .map(key => caches.delete(key))
      );
    }).then(() => clients.claim())
  );
});

// ── Fetch: routing strategy ───────────────────────────────────────────────────
self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);

  // Only handle GET requests
  if (request.method !== 'GET') return;

  // Skip cross-origin requests (except Google Fonts)
  const isGoogleFont = url.hostname === 'fonts.googleapis.com' ||
                       url.hostname === 'fonts.gstatic.com';
  if (url.origin !== self.location.origin && !isGoogleFont) return;

  // ── API calls: network-only, never cache ─────────────────────────────────
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/socket.io/')) {
    event.respondWith(
      fetch(request).catch(() => {
        return new Response(
          JSON.stringify({ error: 'You are offline. Please check your connection.' }),
          { status: 503, headers: { 'Content-Type': 'application/json' } }
        );
      })
    );
    return;
  }

  // ── Static assets: cache-first ────────────────────────────────────────────
  // Icons, fonts, JS, CSS — serve from cache, update in background
  const isStaticAsset = url.pathname.startsWith('/static/') || isGoogleFont;
  if (isStaticAsset) {
    event.respondWith(
      caches.match(request).then(cached => {
        // Return cached version immediately, fetch update in background
        const networkFetch = fetch(request).then(response => {
          if (response && response.status === 200) {
            const responseClone = response.clone();
            caches.open(CACHE_NAME).then(cache => cache.put(request, responseClone));
          }
          return response;
        });
        return cached || networkFetch;
      })
    );
    return;
  }

  // ── HTML pages: network-first, fall back to cache, then offline page ──────
  event.respondWith(
    fetch(request)
      .then(response => {
        // Cache a copy of successful page responses
        if (response && response.status === 200) {
          const responseClone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(request, responseClone));
        }
        return response;
      })
      .catch(() => {
        // Network failed — try cache
        return caches.match(request).then(cached => {
          if (cached) return cached;
          // Nothing in cache — show offline page
          return caches.match(OFFLINE_URL).then(offlinePage => {
            return offlinePage || new Response(
              '<h1>You are offline</h1><p>Please reconnect to use ExamGuard.</p>',
              { headers: { 'Content-Type': 'text/html' } }
            );
          });
        });
      })
  );
});

// ── Push Notifications ────────────────────────────────────────────────────────
self.addEventListener('push', event => {
  let data = { title: 'ExamGuard', body: 'New notification', icon: '/static/icons/icon-192.png' };

  if (event.data) {
    try {
      data = { ...data, ...event.data.json() };
    } catch {
      data.body = event.data.text();
    }
  }

  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: data.icon || '/static/icons/icon-192.png',
      badge: '/static/icons/icon-192.png',
      tag: data.tag || 'examguard-notification',
      data: data.url || '/',
      vibrate: data.requireInteraction ? [300, 100, 300, 100, 300] : [200, 100, 200],
      requireInteraction: data.requireInteraction || false,
    })
  );
});

// ── Notification click: open the relevant page ────────────────────────────────
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const targetUrl = event.notification.data || '/';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
      // If app is already open, focus it and navigate
      for (const client of windowClients) {
        if (client.url === targetUrl && 'focus' in client) {
          return client.focus();
        }
      }
      // Otherwise open a new window
      if (clients.openWindow) {
        return clients.openWindow(targetUrl);
      }
    })
  );
});

// ── Background sync (future use) ──────────────────────────────────────────────
self.addEventListener('sync', event => {
  if (event.tag === 'sync-violations') {
    event.waitUntil(syncPendingViolations());
  }
});

async function syncPendingViolations() {
  // Placeholder for future: sync queued offline actions when connectivity resumes
  console.log('[ExamGuard SW] Background sync triggered');
}