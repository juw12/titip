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
  var LOST_LOOKS = 12;     // looks in a row that did not reach the relay before giving up
  var LOST_AGAIN_MS = 1000;
  var HANG_MS = 10000;     // a request with no reply for this long is dropped and made again
  var LEAVE_FOR_MS = 8000; // a question that could not be left at the relay is offered again for this long
  var LEAVE_AGAIN_MS = 700;
  var CHECK_MS = 6000;     // each step of check() gets this long
  var PC_QUIET_S = 45 * 60;   // the PC writes to the relay every 30 minutes at least

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

  /* fetch that gives up. A phone that wakes up can hand a page a connection that is already dead:
     the request then hangs for a minute before it fails. Dropped after ms, it is made again on a
     fresh connection. Only the wait for the first byte is timed; a long answer may take its time. */
  function reach(url, options, ms) {
    var settings = options || {};
    var stop = typeof AbortController === "function" ? new AbortController() : null;
    var timer = stop ? setTimeout(function () { stop.abort(); }, ms) : 0;
    if (stop) settings.signal = stop.signal;
    return fetch(url, settings).then(function (reply) { clearTimeout(timer); return reply; },
      function (error) { clearTimeout(timer); throw error; });
  }

  /* What went wrong, for ask(): "silent", "asleep" or "relay". A "relay" one says why:
     unreachable (no reply at all), refused (the relay answered with status), unreadable (an answer
     this phone's key does not open). */
  function trouble(kind, why, status) {
    var error = new Error(kind);
    error.why = why || "";
    error.status = status || 0;
    return error;
  }

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
      return reach(base + "/answer/" + id, { cache: "no-store" }, HANG_MS).then(function (reply) {
        watch.lost = 0;
        if (reply.status === 200) {
          return reply.text().then(function (text) {
            return open(text).catch(function () { throw trouble("relay", "unreadable"); });
          }, function () { throw trouble("relay", "unreachable"); });   // the answer broke off on its way
        }
        if (reply.status !== 204) throw trouble("relay", "refused", reply.status);
        return null;
      }, function () {
        watch.lost += 1;
        if (watch.lost >= LOST_LOOKS) throw trouble("relay", "unreachable");
        return null;
      }).then(function (answer) {
        if (answer) return answer;
        if (frozen) throw trouble("asleep");
        if (watch.spent > waitMs) throw trouble("silent");
        var wait = watch.lost ? LOST_AGAIN_MS : watch.spent > SLOW_AFTER_MS ? SLOW_EVERY_MS : LOOK_EVERY_MS;
        return pause(wait).then(function () { return waitFor(id, watch, waitMs); });
      });
    }

    /* Leaves the sealed question at the relay. A try that got no reply is made again for a few
       seconds: the PC carries out each id once, so leaving the same question twice is harmless. */
    function leave(id, sealed, since) {
      return reach(base + "/ask/" + id, { method: "POST", headers: { "Content-Type": "text/plain" }, body: sealed }, HANG_MS).then(function (reply) {
        if (reply.status === 429) throw trouble("silent");
        if (!reply.ok) throw trouble("relay", "refused", reply.status);
      }, function () {
        if (navigator.onLine === false || Date.now() - since > LEAVE_FOR_MS) throw trouble("relay", "unreachable");
        return pause(LEAVE_AGAIN_MS).then(function () { return leave(id, sealed, since); });
      });
    }

    /* Resolves with {status, data} exactly as the PC answered. Rejects with Error("silent") when no
       answer came in time, Error("asleep") when this page slept through the wait, Error("relay")
       when the relay could not be reached, refused, or handed over something this phone cannot open
       (error.why says which). */
    function ask(route, params, waitMs) {
      var id = toBase64(crypto.getRandomValues(new Uint8Array(12)));
      var started = Date.now();
      return seal({ id: id, at: Math.floor(started / 1000), route: route, params: params || {} }).then(function (sealed) {
        return leave(id, sealed, started);
      }).then(function () {
        return pause(FIRST_LOOK_MS);
      }).then(function () {
        return waitFor(id, { at: Date.now(), spent: 0, lost: 0 }, waitMs);
      }).then(function (answer) {
        if (!answer || answer.id !== id) throw trouble("relay", "unreadable");  // not the answer to this question
        return { status: answer.status, data: answer.data || {} };
      });
    }

    /* Finds out what stands between this phone and the PC, without asking the PC anything: the
       relay keeps the PC's last sealed state, so one read tells whether the relay can be reached
       from this network, whether it knows this phone's code, whether the key opens what the PC
       wrote, and when the PC last wrote. Resolves with {is, ...}, never rejects:
         offline      this phone says it has no connection
         nonet        neither the relay nor the site this page came from answer
         blocked      that site answers, the relay does not: this network does not let the relay through
         refused      the relay answered with an error (status)
         unknown      the relay has nothing under this phone's code
         key          the relay has something, and this phone's key does not open it
         quiet        the PC has not written for a long time (at: seconds since 1970)
         mailoff      the PC is there, and "Mail from anywhere" is switched off on it
         fine         the relay and the PC are there (at) */
    function check() {
      if (navigator.onLine === false) return Promise.resolve({ is: "offline" });
      return reach(base + "/state", { cache: "no-store" }, CHECK_MS).then(function (reply) {
        if (reply.status === 404) return { is: "unknown" };
        if (reply.status !== 200) return { is: "refused", status: reply.status };
        return reply.text().then(open).then(function (state) {
          var at = Number(state.published || state.now) || 0;
          if (at && Date.now() / 1000 - at > PC_QUIET_S) return { is: "quiet", at: at };
          return { is: state.mail === false ? "mailoff" : "fine", at: at };
        }, function () { return { is: "key" }; });
      }, function () {
        // is it the relay that this network does not let through, or is there no internet at all?
        return reach("icon.svg?" + Date.now(), { cache: "no-store" }, CHECK_MS).then(function (reply) {
          return { is: reply.ok ? "blocked" : "nonet" };
        }, function () { return { is: "nonet" }; });
      });
    }

    return { ask: ask, check: check, host: found.u.replace(/^https:\/\//, "").split("/")[0] };
  }

  return { ticket: ticket, connect: connect, fromBase64: fromBase64, leaving: leaving, ios: ios, standalone: standalone };
}());
