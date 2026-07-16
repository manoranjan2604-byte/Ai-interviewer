/* =========================================================
   Aperture — AI Interview Console — Service Worker
   Version: 1.0
========================================================= */

const CACHE_NAME = "aperture-v1";

const STATIC_FILES = [
  "/",
  "/manifest.json",
  "/offline.html",
  "/static/style.css",
  "/static/script.js",
  "/public/icon-192.png",
  "/public/icon-512.png"
];

/* ============================
   INSTALL
============================= */

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_FILES))
  );
  self.skipWaiting();
});

/* ============================
   ACTIVATE
============================= */

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) {
            return caches.delete(key);
          }
        })
      )
    )
  );
  self.clients.claim();
});

/* ============================
   FETCH
============================= */

self.addEventListener("fetch", (event) => {
  const request = event.request;

  if (request.method !== "GET") return;

  // Never cache/interfere with API calls or the audio/interview
  // websocket traffic -- an interview session is live, real-time
  // state and must always hit the network.
  const url = new URL(request.url);
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws/")) {
    return;
  }

  /* ---------- HTML Pages ----------
     Network First
  --------------------------------*/

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(request, copy);
          });
          return response;
        })
        .catch(() => {
          return caches.match(request).then((cached) => {
            return cached || caches.match("/offline.html");
          });
        })
    );
    return;
  }

  /* ---------- Static Assets ----------
     Cache First
  --------------------------------*/

  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) {
        return cached;
      }

      return fetch(request).then((response) => {
        if (!response || response.status !== 200 || response.type !== "basic") {
          return response;
        }

        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => {
          cache.put(request, copy);
        });
        return response;
      });
    })
  );
});
