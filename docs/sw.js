/* Titip's service worker. It is here for one thing: to show a notice from your PC while the page is closed.
   It keeps no copy of any page and answers no request, so the app loads exactly as it does without it.
   What arrives is sealed by your PC for this phone (see titip/push.py): title, text, and where a tap leads. */
"use strict";

self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (event) { event.waitUntil(self.clients.claim()); });

self.addEventListener("push", function (event) {
  var notice = {};
  try { notice = event.data ? event.data.json() : {}; } catch (error) { notice = {}; }
  var title = String(notice.title || "Titip"), body = String(notice.body || ""), url = String(notice.url || "");
  // a push always shows something: a phone takes the right to be told away from a page that stays silent
  var shown = self.registration.showNotification(title, {
    body: body, tag: notice.tag ? String(notice.tag) : undefined, icon: "icon-192.png", badge: "icon-192.png", data: { url: url }
  });
  // and a Titip that is open says it too, in its own line, and looks for the mail
  var told = self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (open) {
    open.forEach(function (page) { page.postMessage({ titip: "notice", title: title, body: body, url: url }); });
  });
  event.waitUntil(Promise.all([shown, told]));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || "";
  if (url.charAt(0) !== "#") url = "";   // only a place inside Titip, never another address
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (open) {
    for (var index = 0; index < open.length; index += 1) {
      if ("focus" in open[index]) {
        open[index].postMessage({ titip: "open", url: url });
        return open[index].focus();
      }
    }
    return self.clients.openWindow(self.registration.scope + url);
  }));
});
