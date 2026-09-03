// PrivacyFence's service worker -- tier 1 notifications only
// (docs/approval-list-ui-ux.md §4.1). Its one job is to host
// registration.showNotification() calls the page itself makes (see
// web_shell.py's own script) and to route a click on one of those back to
// an open /approvals tab -- there is no `push` handler here, because tier
// 2 (web push, VAPID, a subscription store) is org-mode/P7+ work, not
// this phase's. Nothing here fetches, caches, or intercepts requests --
// this is deliberately not an offline-support service worker.
//
// Served at the origin root (GET /sw.js, see web/routes_approvals.py) so
// its default scope covers the whole app -- a service worker's scope can
// never be wider than the path it's served from.

self.addEventListener("install", function (event) {
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then(function (clientList) {
        for (var i = 0; i < clientList.length; i++) {
          var client = clientList[i];
          if ("focus" in client) {
            client.focus();
            if ("navigate" in client) {
              client.navigate("/approvals");
            }
            return;
          }
        }
        if (self.clients.openWindow) {
          return self.clients.openWindow("/approvals");
        }
      })
  );
});
