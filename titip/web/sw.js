/* Titip's service worker. Two things:
   1. It shows a notice from your PC while the page is closed. What arrives is sealed by your PC for this
      phone (see titip/push.py): title, text, and where a tap leads.
   2. It keeps a copy of the app's own files (the page, its script, its looks: nothing of your mail), so the
      Home Screen icon opens without a network too and shows the list the phone kept. The network always
      comes first: a copy is only used when the phone cannot get the file. No request to your PC or to the
      relay ever goes through here. */
"use strict";

var SHELL = "titip-shell-1";
// the app's own files, fetched once when the worker is put in, so the icon opens without a network from then on
var OWN = ["./", "styles.css", "app.js", "far.js", "demo.json", "manifest.webmanifest", "icon-192.png", "icon.svg",
  "geist.woff2", "geist-mono.woff2", "instrument-serif.woff2", "home.html", "home.js", "auto.html", "auto.js"];

self.addEventListener("install", function (event) {
  self.skipWaiting();
  event.waitUntil(caches.open(SHELL).then(function (cache) {
    return Promise.all(OWN.map(function (name) {
      var place = new URL(name, self.registration.scope).href;
      // one that cannot be fetched now is kept the next time the page loads it
      return fetch(place, { cache: "no-cache" }).then(function (reply) { if (reply.ok) return cache.put(place, reply); }).catch(function () { });
    }));
  }));
});
self.addEventListener("activate", function (event) {
  event.waitUntil(caches.keys().then(function (names) {
    return Promise.all(names.filter(function (name) { return name.indexOf("titip-shell-") === 0 && name !== SHELL; })
      .map(function (name) { return caches.delete(name); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener("fetch", function (event) {
  var request = event.request, url = new URL(request.url);
  var page = request.mode === "navigate";
  // only the app's own files: not another site (the relay), not the PC's answers, not a question with a "?" of its own
  if (request.method !== "GET" || url.origin !== self.location.origin || url.pathname.indexOf("/api/") >= 0 || (url.search && !page)) return;
  var kept = url.origin + url.pathname;   // a page is kept under its place, without what follows the "?"
  event.respondWith(fetch(request).then(function (reply) {
    if (reply && reply.ok && reply.type === "basic") {
      var copy = reply.clone();
      event.waitUntil(caches.open(SHELL).then(function (cache) { return cache.put(kept, copy); }));
    }
    return reply;
  }).catch(function (error) {
    return caches.open(SHELL).then(function (cache) { return cache.match(kept); }).then(function (copy) {
      if (copy) return copy;
      throw error;
    });
  }));
});

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
