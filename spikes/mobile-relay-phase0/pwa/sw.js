// Minimal service worker: exists so the PWA is installable ("Add to Home
// Screen" on iOS requires one) and to mark where real-time wake-up would
// plug in later.
//
// Phase 0 deliberately wakes via long-polling (app.js), not Web Push --
// issue #55 explicitly allows this as a fallback ("the phone polls a
// self-hosted endpoint on an interval instead of real-time push"), and
// implementing real VAPID subscription + the relay signing/sending push
// messages is meaningful additional scope with nothing in Phase 0's own
// definition asking for it. This handler is the seam a later phase fills in:
// the relay would sign an opaque, content-free payload naming only the
// mailbox ID (per the architecture's "one unavoidable exception"), and this
// handler would show a notification and let app.js take it from there --
// never carrying request content through the push message itself.

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  // Not wired up in Phase 0 (see file header) -- present so the extension
  // point exists and is discoverable, not because anything sends a push
  // message yet.
  event.waitUntil(
    self.registration.showNotification("PrivacyFence relay spike", {
      body: "A pending approval may be waiting -- open the app to check.",
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(self.clients.openWindow("./index.html"));
});
