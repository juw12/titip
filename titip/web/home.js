/* Titip on the iPhone Home Screen.

   An iPhone gives a Home Screen icon its own storage: what Safari kept (the key from the QR code)
   is not there, so an icon made from the mail app opens with no key. The one thing an icon does
   keep is the address of the page it was made from, and for a page without a manifest that is the
   whole address, the part after the # included. That part never leaves the phone.

   So this page is what goes on the Home Screen. In the browser it puts the key back into its own
   address for five minutes and shows the three taps. Opened from the icon it takes the key out of
   the address, keeps it in the icon's storage and goes on to the mail app or the Autopilot page. */
(function () {
  "use strict";
  var KEY = "titip.relay";
  var FORGOT = "titip.forgot";   // the key of an icon you unlinked: its address still carries it
  var READY_MS = 300000;         // how long the address may hold the key while you add the icon

  var $ = function (id) { return document.getElementById(id); };
  var store = (function () {
    try { localStorage.setItem("titip.test", "1"); localStorage.removeItem("titip.test"); return localStorage; }
    catch (error) { var memory = {}; return { getItem: function (k) { return memory[k] || null; }, setItem: function (k, v) { memory[k] = v; }, removeItem: function (k) { delete memory[k]; } }; }
  }());
  var dark = window.matchMedia("(prefers-color-scheme: dark)");
  document.documentElement.setAttribute("data-theme", store.getItem("titip.theme") || (dark.matches ? "dark" : "light"));

  var ios = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  var standalone = navigator.standalone === true || window.matchMedia("(display-mode: standalone)").matches;
  var back = /[?&]from=auto\b/.test(location.search) ? "auto.html" : "./";
  var here = location.pathname + location.search;

  function good(found) { return found && /^https:\/\//.test(found.u) && found.c && found.k ? found : null; }
  function unpack(text) {
    try {
      var plain = atob(text.replace(/-/g, "+").replace(/_/g, "/"));
      var bytes = new Uint8Array(plain.length);
      for (var i = 0; i < plain.length; i += 1) bytes[i] = plain.charCodeAt(i);
      return good(JSON.parse(new TextDecoder().decode(bytes)));
    } catch (error) { return null; }
  }
  function pack(ticket) {
    var bytes = new TextEncoder().encode(JSON.stringify(ticket)), plain = "";
    for (var i = 0; i < bytes.length; i += 1) plain += String.fromCharCode(bytes[i]);
    return btoa(plain).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function kept(name) {
    try { return good(JSON.parse(store.getItem(name) || "null")); } catch (error) { return null; }
  }
  function strip() {
    try { history.replaceState(null, "", here); } catch (error) { /* the page is on its way out */ }
  }
  function leave(where) { strip(); location.replace(where); }

  var found = /^#r=([A-Za-z0-9_-]+)$/.exec(location.hash);
  var arrived = found ? unpack(found[1]) : null;

  // ------------------------------------------------------------ opened from the icon
  if (standalone) {
    var forgot = kept(FORGOT);
    if (arrived && !(forgot && forgot.c === arrived.c)) {
      store.setItem(KEY, JSON.stringify(arrived));
      store.removeItem(FORGOT);
      leave(back);
    } else {
      leave(!arrived && kept(KEY) ? back : "auto.html");   // no key, or one you unlinked: that page says so
    }
    return;
  }

  // ------------------------------------------------------------ opened in the browser
  if (arrived) { store.setItem(KEY, JSON.stringify(arrived)); store.removeItem(FORGOT); }
  var ticket = arrived || kept(KEY);
  $("opening").hidden = true;
  if (!ticket) { strip(); $("none").hidden = false; return; }
  if (!ios) { leave(back); return; }   // elsewhere a home screen icon shares the browser's storage: nothing to carry

  var readyAt = Date.now(), done = false;
  function finish() {
    if (done) return;
    done = true;
    leave(back);
  }
  try { history.replaceState(null, "", here + "#r=" + (found ? found[1] : pack(ticket))); } catch (error) { /* the steps still show */ }
  $("ready").hidden = false;
  $("skip").onclick = finish;
  setTimeout(finish, READY_MS);
  document.addEventListener("visibilitychange", function () {   // timers sleep while the phone is locked
    if (!document.hidden && Date.now() - readyAt >= READY_MS) finish();
  });
  window.addEventListener("pagehide", strip);
  window.addEventListener("pageshow", function (event) { if (event.persisted) finish(); });
}());
