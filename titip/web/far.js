/* Titip from any network: ask the PC a question through the relay and wait for its answer.

   Both travel as AES-GCM ciphertext. The key came from the QR code on the PC (after the # of the
   address) and stays in this browser; the relay never has it. Used by the mail app (app.js) when
   this phone has scanned that code and "Mail from anywhere" is switched on in Titip on the PC.
   The code may open the mail app directly: it is kept here and taken out of the address bar
   before the app starts. On an iPhone the scan goes on to home.html, the page that can carry the
   key onto the Home Screen (an icon there has its own storage and would start without one). */
window.TitipFar = (function () {
  "use strict";
  var KEY = "titip.relay";
  var FIRST_LOOK_MS = 300, LOOK_EVERY_MS = 500, SLOW_AFTER_MS = 8000, SLOW_EVERY_MS = 1200;
  var FROZEN_MS = 15000;   // a gap this long between two looks: the phone had put this page to sleep
  var LOST_LOOKS = 4;      // looks in a row that did not reach the relay before giving up

  function fromBase64(text) {
    var plain = atob(text.replace(/-/g, "+").replace(/_/g, "/"));
    var bytes = new Uint8Array(plain.length);
    for (var i = 0; i < plain.length; i += 1) bytes[i] = plain.charCodeAt(i);
    return bytes;
  }
  function toBase64(bytes) {
    var plain = "";
    for (var i = 0; i < bytes.length; i += 0x8000) plain += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(plain).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function good(found) { return found && /^https:\/\//.test(found.u) && found.c && found.k ? found : null; }

  var onPages = /\.github\.io$/.test(location.hostname);   // the public page; home.html lives only there
  var ios = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  var standalone = navigator.standalone === true || window.matchMedia("(display-mode: standalone)").matches;
  var leaving = false;   // on the way to home.html: the app must not start

  // the code this page was opened with (#r=...), if any; a browser that cannot store keeps it until the page closes
  var scanned = (function () {
    var found = /^#r=([A-Za-z0-9_-]+)$/.exec(location.hash);
    if (!found) return null;
    var read = null;
    try { read = good(JSON.parse(new TextDecoder().decode(fromBase64(found[1])))); } catch (error) { read = null; }
    try { history.replaceState(null, "", location.pathname + location.search); } catch (error) { location.hash = ""; }  // keep the key out of the address bar
    if (read) { try { localStorage.setItem(KEY, JSON.stringify(read)); } catch (error) { /* private browsing */ } }
    if (read && onPages && ios && !standalone) {
      leaving = true;
      location.replace("home.html#r=" + found[1]);
    }
    return read;
  }());

  function ticket() {
    if (scanned) return scanned;
    try { return good(JSON.parse(localStorage.getItem(KEY) || "null")); } catch (error) { return null; }
  }

  function pause(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }

  function connect(found) {
    var keyPromise = crypto.subtle.importKey("raw", fromBase64(found.k), "AES-GCM", false, ["encrypt", "decrypt"]);
    var base = found.u + "/c/" + found.c;

    function seal(data) {
      var iv = crypto.getRandomValues(new Uint8Array(12));
      return keyPromise.then(function (key) {
        return crypto.subtle.encrypt({ name: "AES-GCM", iv: iv }, key, new TextEncoder().encode(JSON.stringify(data)));
      }).then(function (sealed) {
        var all = new Uint8Array(12 + sealed.byteLength);
        all.set(iv, 0);
        all.set(new Uint8Array(sealed), 12);
        return toBase64(all);
      });
    }
    function open(text) {
      var raw = fromBase64(text.trim());
      return keyPromise.then(function (key) {
        return crypto.subtle.decrypt({ name: "AES-GCM", iv: raw.slice(0, 12) }, key, raw.slice(12));
      }).then(function (plain) { return JSON.parse(new TextDecoder().decode(plain)); });
    }

    /* Looks for the answer until it is there or waitMs of looking has passed.
       A phone freezes a page that is not on screen. Time spent frozen is not time the PC was given,
       so only the time between two looks counts, and after a long gap one more look decides: the
       answer is there, or the question is too old ("asleep") and the app asks it again.
       A look that does not reach the relay (the network changed, the phone just woke) is tried again. */
    function waitFor(id, watch, waitMs) {
      var now = Date.now();
      var gap = now - watch.at;
      var frozen = gap > FROZEN_MS;
      watch.spent += Math.min(gap, SLOW_EVERY_MS * 2);
      watch.at = now;
      return fetch(base + "/answer/" + id, { cache: "no-store" }).then(function (reply) {
        watch.lost = 0;
        if (reply.status === 200) return reply.text().then(open);
        if (reply.status !== 204) throw new Error("relay");
        return null;
      }, function () {
        watch.lost += 1;
        if (watch.lost >= LOST_LOOKS) throw new Error("relay");
        return null;
      }).then(function (answer) {
        if (answer) return answer;
        if (frozen) throw new Error("asleep");
        if (watch.spent > waitMs) throw new Error("silent");
        return pause(watch.spent > SLOW_AFTER_MS ? SLOW_EVERY_MS : LOOK_EVERY_MS).then(function () { return waitFor(id, watch, waitMs); });
      });
    }

    /* Resolves with {status, data} exactly as the PC answered. Rejects with Error("silent") when no
       answer came in time, Error("asleep") when this page slept through the wait, Error("relay")
       when the relay could not be reached or refused. */
    function ask(route, params, waitMs) {
      var id = toBase64(crypto.getRandomValues(new Uint8Array(12)));
      var started = Date.now();
      return seal({ id: id, at: Math.floor(started / 1000), route: route, params: params || {} }).then(function (sealed) {
        return fetch(base + "/ask/" + id, { method: "POST", headers: { "Content-Type": "text/plain" }, body: sealed });
      }).then(function (reply) {
        if (!reply.ok) throw new Error(reply.status === 429 ? "silent" : "relay");
        return pause(FIRST_LOOK_MS);
      }, function () { throw new Error("relay"); }).then(function () {
        return waitFor(id, { at: Date.now(), spent: 0, lost: 0 }, waitMs);
      }).then(function (answer) {
        if (!answer || answer.id !== id) throw new Error("relay");  // not the answer to this question
        return { status: answer.status, data: answer.data || {} };
      });
    }

    return { ask: ask };
  }

  return { ticket: ticket, connect: connect, fromBase64: fromBase64, leaving: leaving, ios: ios, standalone: standalone };
}());
