/* Autopilot page: what the PC did with new Gmail, what waits for you, and the switch.

   Three back ends, one page:
   - pc:    the Titip server on your own network (paired phone, or the page on the PC itself)
   - relay: from any network. State and orders pass through a relay as AES-GCM ciphertext; the key
            comes from the QR code (after the # of the address) and stays in this browser.
   - demo:  invented mail, for the public page.
   Mail text is only ever put on the page with textContent.

   An iPhone Home Screen icon has its own storage. A scan on an iPhone therefore goes on to
   home.html, which carries the key into the icon; an icon that has no key says so here. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var KEY = "titip.relay";
  var FORGOT = "titip.forgot";   // the key of a Home Screen icon you unlinked; its address still carries it
  var LOCAL_EVERY = 5000, RELAY_EVERY = 12000;

  var store = (function () {
    try { localStorage.setItem("titip.test", "1"); localStorage.removeItem("titip.test"); return localStorage; }
    catch (error) { var memory = {}; return { getItem: function (k) { return memory[k] || null; }, setItem: function (k, v) { memory[k] = v; }, removeItem: function (k) { delete memory[k]; } }; }
  }());

  var dark = window.matchMedia("(prefers-color-scheme: dark)");
  document.documentElement.setAttribute("data-theme", store.getItem("titip.theme") || (dark.matches ? "dark" : "light"));

  // ------------------------------------------------------------ small helpers
  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function ago(seconds) {
    if (seconds < 50) return "just now";
    if (seconds < 3600) return Math.round(seconds / 60) + " min ago";
    if (seconds < 86400) return Math.round(seconds / 3600) + " h ago";
    return Math.round(seconds / 86400) + " days ago";
  }
  function clock(at) {
    var when = new Date(at * 1000), today = new Date();
    var time = when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return when.toDateString() === today.toDateString() ? time : when.toLocaleDateString([], { day: "numeric", month: "short" }) + ", " + time;
  }
  var toastTimer = 0;
  function toast(text) {
    $("toastText").textContent = text;
    $("toast").hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { $("toast").hidden = true; }, 6000);
  }
  function fromBase64(text) {
    var plain = atob(text.replace(/-/g, "+").replace(/_/g, "/"));
    var bytes = new Uint8Array(plain.length);
    for (var i = 0; i < plain.length; i += 1) bytes[i] = plain.charCodeAt(i);
    return bytes;
  }
  function toBase64(bytes) {
    var plain = "";
    for (var i = 0; i < bytes.length; i += 1) plain += String.fromCharCode(bytes[i]);
    return btoa(plain).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  // ------------------------------------------------------------ back end: your PC
  var pc = {
    name: "pc",
    every: LOCAL_EVERY,
    read: function () {
      return fetch("api/auto", { headers: { "X-Titip": "1" }, cache: "no-store" }).then(function (reply) {
        if (!reply.ok) throw new Error(reply.status === 401 ? "This phone is no longer paired." : "Your PC did not answer.");
        return reply.json();
      });
    },
    order: function (order) {
      return fetch("api/auto", {
        method: "POST", headers: { "X-Titip": "1", "Content-Type": "application/json" }, body: JSON.stringify(order)
      }).then(function (reply) {
        return reply.json().then(function (data) {
          if (!reply.ok) throw new Error(data.error || "Your PC did not take that.");
          return data;
        });
      });
    }
  };

  // ------------------------------------------------------------ back end: the relay (any network)
  function relayFrom(ticket) {
    var keyPromise = crypto.subtle.importKey("raw", fromBase64(ticket.k), "AES-GCM", false, ["encrypt", "decrypt"]);
    var base = ticket.u + "/c/" + ticket.c;
    function open(text) {
      var raw = fromBase64(text.trim());
      return keyPromise.then(function (key) {
        return crypto.subtle.decrypt({ name: "AES-GCM", iv: raw.slice(0, 12) }, key, raw.slice(12));
      }).then(function (plain) { return JSON.parse(new TextDecoder().decode(plain)); });
    }
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
    return {
      name: "relay",
      every: RELAY_EVERY,
      read: function () {
        return fetch(base + "/state", { cache: "no-store" }).then(function (reply) {
          if (reply.status === 404) throw new Error("Your PC has not sent anything yet. Is Titip running there, and is this the newest code?");
          if (!reply.ok) throw new Error("The relay did not answer.");
          return reply.text();
        }).then(open).then(function (state) { state.far = true; return state; });
      },
      order: function (order) {
        var id = toBase64(crypto.getRandomValues(new Uint8Array(9)));
        return seal(Object.assign({ nonce: id, at: Math.floor(Date.now() / 1000), name: order.command }, order)).then(function (sealed) {
          return fetch(base + "/orders", { method: "POST", headers: { "Content-Type": "text/plain" }, body: sealed });
        }).then(function (reply) {
          if (!reply.ok) throw new Error(reply.status === 429 ? "Your PC has not picked up the last requests yet. Is it on?" : "The relay did not take that.");
          return null;  // the PC picks it up within a minute; the next state shows the result
        });
      }
    };
  }

  // ------------------------------------------------------------ back end: the demo
  var demoState = (function () {
    var now = Math.floor(Date.now() / 1000);
    return {
      available: true, on: true, mode: "auto", busy: "", lastCheck: now - 180, nextCheck: now + 420, now: now,
      mailboxes: ["nadia@mail.example"], sentToday: 1, dailyLimit: 8, demo: true,
      held: [{
        id: "d1", at: now - 1500, mailbox: "nadia@mail.example", sender: "Harbor Print Co.", address: "billing@harborprint.example",
        subject: "Invoice 2291 is due", summary: "They ask you to pay invoice 2291 by bank transfer this week.",
        reason: "The mail asks for money or payment details.",
        draft: "Hello,\n\nThank you for the reminder. I will check invoice 2291 against my order and come back to you.\n\nNadia"
      }],
      events: [
        { id: "e4", at: now - 180, kind: "left", text: "No answer needed: a weekly newsletter.", mailbox: "nadia@mail.example", who: "Field Notes Weekly", subject: "Issue 48: paper sizes" },
        { id: "e3", at: now - 1500, kind: "held", text: "The mail asks for money or payment details.", mailbox: "nadia@mail.example", who: "Harbor Print Co.", subject: "Invoice 2291 is due" },
        { id: "e2", at: now - 5400, kind: "sent", text: "Hi Tomas,\n\nThanks for asking. The template is free to use; the source is in the repository linked from the project page.\n\nNadia", mailbox: "nadia@mail.example", who: "Tomas Lind", subject: "Can I use your template?" },
        { id: "e1", at: now - 9000, kind: "switch", text: "Autopilot is on.", mailbox: "", who: "", subject: "" }
      ]
    };
  }());
  var demo = {
    name: "demo",
    every: 60000,
    read: function () { demoState.now = Math.floor(Date.now() / 1000); return Promise.resolve(JSON.parse(JSON.stringify(demoState))); },
    order: function (order) {
      var now = Math.floor(Date.now() / 1000);
      var note = function (kind, text, item) {
        demoState.events.unshift({ id: "e" + now + kind, at: now, kind: kind, text: text, mailbox: item ? item.mailbox : "", who: item ? item.sender : "", subject: item ? item.subject : "" });
      };
      var item = demoState.held.filter(function (held) { return held.id === order.id; })[0];
      if (order.command === "switch") { demoState.on = !!order.on; note("switch", order.on ? "Autopilot is on." : "Autopilot is paused. Nothing is read or sent."); }
      if (order.command === "mode") demoState.mode = order.mode;
      if (order.command === "check") demoState.lastCheck = now;
      if (item && order.command === "send") { note("sent", order.text, item); demoState.sentToday += 1; }
      if (item && order.command === "drop") note("dropped", "You chose not to answer.", item);
      if (item) demoState.held = demoState.held.filter(function (held) { return held !== item; });
      return demo.read();
    }
  };

  // ------------------------------------------------------------ which back end
  function ticketFromAddress() {
    var found = /[#&]r=([A-Za-z0-9_-]+)/.exec(location.hash);
    if (!found) return null;
    try {
      var ticket = JSON.parse(new TextDecoder().decode(fromBase64(found[1])));
      if (!/^https:\/\//.test(ticket.u) || !ticket.c || !ticket.k) return null;
      store.setItem(KEY, JSON.stringify(ticket));
      store.removeItem(FORGOT);
      history.replaceState(null, "", location.pathname + location.search);  // keep the key out of the address bar
      if (hosted && ios && !standalone) {   // an iPhone browser: the page that can put the key on the Home Screen
        leaving = true;
        location.replace("home.html?from=auto#r=" + found[1]);
      }
      return ticket;
    } catch (error) { return null; }
  }
  function storedTicket(name) {
    try { return JSON.parse(store.getItem(name || KEY) || "null"); } catch (error) { return null; }
  }

  var hosted = /\.github\.io$/.test(location.hostname) || location.protocol === "file:";
  var forcedDemo = /[?&]demo\b/.test(location.search);
  var ios = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  var standalone = navigator.standalone === true || window.matchMedia("(display-mode: standalone)").matches;
  var leaving = false;
  var ticket = ticketFromAddress() || storedTicket();  // the mail app next door keeps the same code (far.js)
  var noKey = hosted && ios && standalone && !ticket && !forcedDemo;   // a Home Screen icon that brought no key along
  var api = forcedDemo ? demo : ticket ? relayFrom(ticket) : hosted ? demo : pc;

  // ------------------------------------------------------------ drawing
  var state = null, drafts = {}, pending = {}, lastGood = 0;
  var hope = null;  // relay only: the switch you just pressed, shown until the PC's next state arrives
  var KINDS = { sent: "Answered", held: "Kept for you", left: "Left alone", dropped: "Not answered", error: "Problem", "switch": "You changed" };

  function headline(s) {
    var boxes = (s.mailboxes || []).join(" and ");
    var dot = $("dot");
    if (!s.available) {
      dot.className = "dot off";
      $("headline").textContent = "Autopilot cannot run on this PC";
      $("line").textContent = "It needs Windows, Google Chrome signed in to Gmail, and the Python packages pywin32 and comtypes.";
      return;
    }
    dot.className = "dot " + (s.on ? "on" : "off");
    $("headline").textContent = s.on ? "On" : "Paused";
    var seen = s.lastCheck ? "Looked " + ago(s.now - s.lastCheck) + ". " : "";
    var next = s.on && s.nextCheck ? "Next look " + (s.nextCheck > s.now + 50 ? "in " + Math.round((s.nextCheck - s.now) / 60) + " min. " : "soon. ") : "";
    $("line").textContent = (s.on ? "Watching " + boxes + ". " + seen + next : "Nothing is read or sent. " + seen) +
      "Answered today: " + s.sentToday + " of " + s.dailyLimit + ".";
  }

  function heldItem(item) {
    var row = make("li", "letter");
    row.appendChild(make("p", "who", (item.sender || item.address) + " \u00b7 " + clock(item.at)));
    row.appendChild(make("h3", "", item.subject || "(no subject)"));
    if (item.summary) row.appendChild(make("p", "", item.summary));
    row.appendChild(make("p", "why", item.reason));
    if (item.error) row.appendChild(make("p", "error", item.error));
    var label = make("label", "sr", "Your answer to " + (item.sender || item.address));
    var box = make("textarea");
    box.id = "draft-" + item.id;
    label.htmlFor = box.id;
    box.rows = 6;
    box.value = drafts[item.id] !== undefined ? drafts[item.id] : item.draft || "";
    box.placeholder = "Write your answer";
    box.oninput = function () { drafts[item.id] = box.value; };
    row.appendChild(label);
    row.appendChild(box);
    var buttons = make("div", "rowline");
    var send = make("button", "primary", "Send to " + (item.sender || item.address).split(" ")[0]);
    var drop = make("button", "", "Don't answer");
    var waiting = item.sending || pending[item.id];
    send.type = drop.type = "button";
    send.disabled = drop.disabled = box.readOnly = !!waiting;
    if (waiting) send.textContent = "Sending from your PC";
    send.onclick = function () {
      if (!box.value.trim()) { toast("Write an answer first."); box.focus(); return; }
      pending[item.id] = true;
      order({ command: "send", id: item.id, text: box.value }, "Your PC is sending it. It shows under What it did when it has left.");
    };
    drop.onclick = function () {
      pending[item.id] = true;
      order({ command: "drop", id: item.id }, "Left unanswered. The mail stays in Gmail.");
    };
    buttons.appendChild(send);
    buttons.appendChild(drop);
    row.appendChild(buttons);
    row.appendChild(make("p", "soft", "Goes to " + item.address + " from " + item.mailbox + "."));
    return row;
  }

  function feedItem(event) {
    var row = make("li", "entry " + event.kind);
    var head = make("p", "head");
    head.appendChild(make("span", "tag", KINDS[event.kind] || event.kind));
    head.appendChild(make("time", "", clock(event.at)));
    row.appendChild(head);
    if (event.who || event.subject) row.appendChild(make("p", "about", [event.who, event.subject].filter(Boolean).join(" \u00b7 ")));
    row.appendChild(make(event.kind === "sent" ? "blockquote" : "p", event.kind === "sent" ? "" : "what", event.text));
    return row;
  }

  function draw(s) {
    if (hope && (Date.now() > hope.until || (s.published || 0) > hope.published)) hope = null;
    if (hope) s = Object.assign({}, s, hope.change);
    state = s;
    lastGood = Date.now();
    var heldIds = {};
    (s.held || []).forEach(function (item) { heldIds[item.id] = true; });
    Object.keys(pending).forEach(function (id) { if (!heldIds[id]) delete pending[id]; });
    Object.keys(drafts).forEach(function (id) { if (!heldIds[id]) delete drafts[id]; });

    headline(s);
    $("trouble").hidden = true;
    $("demoNote").hidden = !s.demo;
    $("busy").hidden = !s.busy;
    $("busy").textContent = s.busy ? s.busy + " \u2026" : "";
    $("controls").hidden = $("modes").hidden = !s.available;
    $("switch").textContent = s.on ? "Pause" : "Switch on";
    $("switch").className = s.on ? "" : "primary";
    $("check").disabled = !s.on || !!s.busy;
    Array.prototype.forEach.call(document.querySelectorAll('input[name="mode"]'), function (input) {
      input.checked = input.value === s.mode;
    });
    $("limit").textContent = s.dailyLimit;

    var typing = document.activeElement && document.activeElement.tagName === "TEXTAREA";
    if (!typing) {  // never redraw under the thumb that is writing an answer
      var list = $("held");
      list.textContent = "";
      (s.held || []).forEach(function (item) { list.appendChild(heldItem(item)); });
    }
    $("heldSection").hidden = !s.available;
    $("heldNone").hidden = !!(s.held || []).length;
    $("heldCount").hidden = !(s.held || []).length;
    $("heldCount").textContent = (s.held || []).length;
    document.title = ((s.held || []).length ? "(" + s.held.length + ") " : "") + "Autopilot \u00b7 Titip";

    var feed = $("feed");
    feed.textContent = "";
    (s.events || []).forEach(function (event) { feed.appendChild(feedItem(event)); });
    $("feedSection").hidden = !s.available;
    $("feedNone").hidden = !!(s.events || []).length;
    $("rules").hidden = false;

    var far = $("far");
    far.hidden = !s.far;
    if (s.far) {
      far.textContent = "You are reading this through the relay. Your PC last wrote " + ago(Math.floor(Date.now() / 1000) - (s.published || s.now)) +
        ". What you press here reaches it within about a minute. The relay only ever holds ciphertext.";
    }
    $("forget").hidden = api.name !== "relay";
    $("home").hidden = !(api.name === "relay" && hosted && ios && !standalone);
    if (api.name === "relay") $("mailTab").hidden = !s.mail;  // Outlook mail passes the relay only while its switch on the PC is on
  }

  function trouble(error) {
    $("trouble").hidden = false;
    $("trouble").textContent = (error && error.message ? error.message : "No connection.") +
      (state ? " Showing what was known " + ago(Math.floor((Date.now() - lastGood) / 1000)) + "." : "");
    if (!state) { $("dot").className = "dot off"; $("headline").textContent = "Can't reach your PC"; $("line").textContent = ""; }
  }

  function refresh() {
    return api.read().then(draw).catch(trouble);
  }

  function order(what, said) {
    api.order(what).then(function (next) {
      if (said) toast(api.name === "relay" ? said.replace("Your PC is sending it.", "Your PC picks it up within a minute.") : said);
      if (next) draw(next);
      else if (state) {
        var change = what.command === "switch" ? { on: !!what.on } : what.command === "mode" ? { mode: what.mode } : null;
        if (change) hope = { until: Date.now() + 120000, published: state.published || 0, change: Object.assign(hope ? hope.change : {}, change) };
        draw(state);
      }
    }).catch(function (error) {
      if (what.id) delete pending[what.id];
      toast(error.message || "That did not work.");
      if (state) draw(state);
    });
  }

  // ------------------------------------------------------------ wiring
  $("switch").onclick = function () {
    if (!state) return;
    order({ command: "switch", on: !state.on }, state.on ? "Paused. Nothing is read or sent until you switch it on." : "On. It looks at your Gmail now.");
  };
  $("check").onclick = function () { order({ command: "check" }, "Looking now."); };
  Array.prototype.forEach.call(document.querySelectorAll('input[name="mode"]'), function (input) {
    input.onchange = function () { order({ command: "mode", mode: input.value }, input.value === "auto" ? "Safe answers go out on their own." : "Every answer waits for you."); };
  });
  var icon = ios && standalone;
  if (icon) $("forget").textContent = "Unlink this icon";
  $("forget").onclick = function () {
    if (!window.confirm(icon
      ? "Unlink this icon? It stops showing your mail. Delete it from the Home Screen afterwards: it still carries the key."
      : "Unlink this phone? You will need to scan the code on your PC again.")) return;
    if (icon) store.setItem(FORGOT, JSON.stringify(ticket));   // or the icon's own address would link it again at the next start
    store.removeItem(KEY);
    // what the mail app kept on this phone to open fast: the last lists and the names with their addresses
    ["titip.list.inbox", "titip.list.sent", "titip.list.drafts", "titip.people", "titip.me"].forEach(function (name) { store.removeItem(name); });
    location.reload();
  };

  // a Home Screen icon without a key: say so, instead of a demo that looks like broken mail
  function showNoKey() {
    var forgot = storedTicket(FORGOT);
    document.querySelector(".tabs").hidden = true;   // nothing to switch between
    $("dot").className = "dot off";
    $("headline").textContent = forgot ? "You unlinked this icon" : "This icon has no key to your PC";
    $("line").textContent = forgot
      ? "It shows nothing of your mail any more. To remove it for good, delete the icon from the Home Screen. It still carries the key, so you can also link it again."
      : "A Home Screen icon keeps its own storage, apart from the browser it was made in. Delete this icon. Then scan the code Titip shows on your PC with the camera: the page that opens shows how to make an icon that works.";
    $("noKey").hidden = false;
    $("relink").hidden = !forgot;
    $("relink").onclick = function () {
      store.setItem(KEY, JSON.stringify(forgot));
      store.removeItem(FORGOT);
      location.reload();
    };
  }

  if (noKey) showNoKey();
  else if (!leaving) {
    if (api.name === "relay") $("mailTab").hidden = true;  // until the PC says "Mail from anywhere" is on
    refresh();
    setInterval(function () { if (!document.hidden) refresh(); }, api.every);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) refresh(); });
  }
}());
