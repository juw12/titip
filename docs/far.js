/* Titip from any network: ask the PC a question through the relay and wait for its answer.

   Both travel as AES-GCM ciphertext. The key came from the QR code on the PC (after the # of the
   address) and stays in this browser; the relay never has it. Used by the mail app (app.js) when
   this phone has scanned that code and "Mail from anywhere" is switched on in Titip on the PC.
   The code may open the mail app directly: it is kept here and taken out of the address bar
   before the app starts. */
window.TitipFar = (function () {
  "use strict";
  var KEY = "titip.relay";
  var FIRST_LOOK_MS = 500, LOOK_EVERY_MS = 700, SLOW_AFTER_MS = 12000, SLOW_EVERY_MS = 1500;

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

  // the code this page was opened with (#r=...), if any; a browser that cannot store keeps it until the page closes
  var scanned = (function () {
    var found = /^#r=([A-Za-z0-9_-]+)$/.exec(location.hash);
    if (!found) return null;
    var read = null;
    try { read = good(JSON.parse(new TextDecoder().decode(fromBase64(found[1])))); } catch (error) { read = null; }
    try { history.replaceState(null, "", location.pathname + location.search); } catch (error) { location.hash = ""; }  // keep the key out of the address bar
    if (read) { try { localStorage.setItem(KEY, JSON.stringify(read)); } catch (error) { /* private browsing */ } }
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

    function waitFor(id, started, waitMs) {
      return fetch(base + "/answer/" + id, { cache: "no-store" }).then(function (reply) {
        if (reply.status === 200) return reply.text().then(open);
        if (reply.status !== 204) throw new Error("relay");
        var spent = Date.now() - started;
        if (spent > waitMs) throw new Error("silent");
        return pause(spent > SLOW_AFTER_MS ? SLOW_EVERY_MS : LOOK_EVERY_MS).then(function () { return waitFor(id, started, waitMs); });
      });
    }

    /* Resolves with {status, data} exactly as the PC answered. Rejects with Error("silent") when no
       answer came in time, Error("relay") when the relay could not be reached or refused. */
    function ask(route, params, waitMs) {
      var id = toBase64(crypto.getRandomValues(new Uint8Array(12)));
      var started = Date.now();
      return seal({ id: id, at: Math.floor(started / 1000), route: route, params: params || {} }).then(function (sealed) {
        return fetch(base + "/ask/" + id, { method: "POST", headers: { "Content-Type": "text/plain" }, body: sealed });
      }).then(function (reply) {
        if (!reply.ok) throw new Error(reply.status === 429 ? "silent" : "relay");
        return pause(FIRST_LOOK_MS);
      }, function () { throw new Error("relay"); }).then(function () {
        return waitFor(id, started, waitMs);
      }).then(function (answer) {
        if (!answer || answer.id !== id) throw new Error("relay");  // not the answer to this question
        return { status: answer.status, data: answer.data || {} };
      });
    }

    return { ask: ask };
  }

  return { ticket: ticket, connect: connect, fromBase64: fromBase64 };
}());
