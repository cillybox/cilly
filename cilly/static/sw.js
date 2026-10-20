// cilly service worker. It only shows notifications (posted by the open panel page) and
// brings the panel up when you tap one. No caching, no fetch handling, no push service.
"use strict";
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const id = e.notification.data && /^[0-9a-f]{8}$/.test(e.notification.data.agentId || "") ? e.notification.data.agentId : null;
  e.waitUntil((async () => {
    for (const c of await self.clients.matchAll({ type: "window", includeUncontrolled: true })) {
      if (new URL(c.url).origin === self.location.origin) {
        await c.focus();
        if (id) c.postMessage({ open: id });
        return;
      }
    }
    await self.clients.openWindow(id ? "/#a=" + id : "/");
  })());
});
