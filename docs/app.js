/* Titip phone app. No framework, no build step. Mail text only ever reaches the page through
   textContent or a script-less sandboxed frame, never innerHTML. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var VERSION = "0.10.2";   // shown under the list: a Home Screen icon can keep running an old page for days
  var LOADED = Date.now();
  var FOLDERS = { inbox: "Inbox", sent: "Sent", drafts: "Drafts", outbox: "Outbox", deleted: "Deleted", junk: "Junk", calendar: "Calendar" };
  var TO_FOLDERS = { sent: true, drafts: true, outbox: true };   // listed by who the mail goes to
  var KEPT_FOLDERS = { inbox: true, sent: true, drafts: true };   // their first page is kept on this phone
  var PAGE = 40;
  var CAL_DAYS = 14, CAL_FURTHEST = 182;   // the calendar comes two weeks at a time, half a year at most
  var MAX_FILES = 20;
  var UNDO_SECONDS = 3;
  var REFRESH_MS = 60000;
  var HOSTED = /\.github\.io$/.test(location.hostname);   // the public page: no Titip server behind it
  var FORCED_DEMO = /[?&]demo\b/.test(location.search);
  var ticket = FORCED_DEMO || !window.TitipFar ? null : window.TitipFar.ticket();   // this phone scanned the code for any network
  if (window.TitipFar && window.TitipFar.leaving) return;   // an iPhone that just scanned: home.html takes over
  if (HOSTED && !ticket && !FORCED_DEMO && window.TitipFar && window.TitipFar.ios && window.TitipFar.standalone) {
    location.replace("auto.html");   // a Home Screen icon without a key: that page says what to do, instead of a demo that looks broken
    return;
  }

  // at: when the rows on screen were read from the PC. stale: the last try to read them failed.
  var view = { folder: "inbox", id: "", rows: [], more: false, query: "", deep: false, loading: false, item: null, formatted: true, at: 0, stale: false, days: 0, from: "" };
  var own = { folders: [], categories: [], asked: 0, known: false };   // your own folders and Outlook's categories, as the PC last listed them
  var signature = { "new": "", reply: "" };   // the text Outlook signs with, put under a mail when it is started
  var planned = null;     // the calendar entry being made or changed: {id, scope, occurrence}
  var shown = { which: "", name: "" };   // the calendar on screen when it is not your own
  var pcVersion = "";     // what Titip on the PC says it is; through the relay this page can be the newer one for a while
  var draft = null;       // what the composer is writing: {mode, id, title, note, exact}
  var attached = [];      // files picked for it: {file, name, size}. They live in this page only
  var carried = [];       // files of the original that go along (forward, draft): {index, name, size, off}
  var written = null;     // what was being written, files included, for as long as this page is open
  var pending = null;     // something waiting out its undo window: {run, timer}
  var store = safeStorage();
  var opened = openedHere();   // ids of mail read on this phone; Outlook keeps its own unread flag

  // ------------------------------------------------------------ storage that cannot throw
  function safeStorage() {
    var memory = {};
    try {
      localStorage.setItem("titip.test", "1");
      localStorage.removeItem("titip.test");
      return {
        get: function (key) { return localStorage.getItem(key); },
        set: function (key, value) { localStorage.setItem(key, value); },
        drop: function (key) { localStorage.removeItem(key); }
      };
    } catch (error) {
      return {
        get: function (key) { return memory[key] || null; },
        set: function (key, value) { memory[key] = value; },
        drop: function (key) { delete memory[key]; }
      };
    }
  }

  function openedHere() {
    var ids = [];
    try { ids = JSON.parse(store.get("titip.opened") || "[]"); } catch (error) { ids = []; }
    return Array.isArray(ids) ? ids : [];
  }
  function markOpened(id) {
    if (opened.indexOf(id) >= 0) return;
    opened.push(id);
    if (opened.length > 300) opened = opened.slice(-300);
    store.set("titip.opened", JSON.stringify(opened));
  }

  // ------------------------------------------------------------ the back ends: your PC, your PC through the relay, or the demo
  var BUSY_WAIT = 300000;  // a read keeps asking this long while Outlook on the PC is busy
  var BUSY_AGAIN = 1000;  // the PC keeps the question in Outlook's queue, so asking again costs nothing
  var SAVED_AGAIN = 5000;  // a list shown from the PC's copy is asked for again this soon
  var BUSY_TEXT = "Outlook on your PC is busy. Still trying\u2026";
  function request(path, body, since) {
    var started = since || Date.now();
    var options = { credentials: "same-origin", headers: { "X-Titip": "1" } };
    if (body) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
      options.keepalive = options.body.length < 60000;   // a browser lets only a small request outlive its page
    }
    return fetch(path, options).then(function (reply) {
      if (reply.status === 401) {
        // unpaired on the PC: what this phone kept of the mail goes too
        ["titip.list.inbox", "titip.list.sent", "titip.list.drafts", "titip.people", "titip.me"].forEach(store.drop);
        location.replace("/pair");
        throw new Error("This phone is not paired any more.");
      }
      return reply.json().then(function (data) {
        // only reading is asked again; a send is never repeated, so it can't go out twice
        if (reply.status === 503 && data.busy && !body && Date.now() - started < BUSY_WAIT) {
          toast(BUSY_TEXT);
          return new Promise(function (resolve) { setTimeout(resolve, BUSY_AGAIN); }).then(function () {
            return request(path, null, started);
          });
        }
        if (!reply.ok) throw new Error(data.error || "Something went wrong on the PC.");
        if (since && $("toastText").textContent === BUSY_TEXT) hideToast();
        return data;
      });
    }, function () {
      throw new Error("Can't reach your PC. Check that it is on, awake and on the same network.");
    });
  }

  var pc = {
    state: function () { return request("/api/state"); },
    list: function (folder, offset, query, deep) {
      return request("/api/mail?folder=" + folder + "&limit=" + PAGE + "&offset=" + offset + "&q=" + encodeURIComponent(query) + (deep ? "&deep=1" : ""));
    },
    get: function (id) { return request("/api/item?id=" + encodeURIComponent(id)); },
    send: function (payload) { return request("/api/send", payload); },
    people: function () { return request("/api/people"); },
    lookup: function (typed) { return request("/api/people?q=" + encodeURIComponent(typed)); },
    fileUrl: function (id, index) { return "/api/attachment?id=" + encodeURIComponent(id) + "&index=" + index; },
    calendar: function (offset, days, which) { return request("/api/calendar?offset=" + offset + "&days=" + days + (which ? "&which=" + encodeURIComponent(which) : "")); },
    calendars: function () { return request("/api/calendars"); },
    rules: function () { return request("/api/rules"); },
    arrange: function (order) { return request("/api/arrange", order); },
    event: function (id) { return request("/api/event?id=" + encodeURIComponent(id)); },
    act: function (order) { return request("/api/act", order); },
    plan: function (order) { return request("/api/plan", order); },
    folders: function () { return request("/api/folders"); },
    signature: function () { return request("/api/signature"); },
    upload: function (piece) { return request("/api/upload", piece); },
    PIECE: 384000, LANES: 1, FILES_BYTES: 15000000   // a file goes over in pieces this large, one after the other
  };

  var demoData = null;
  function demoLoad() {
    if (demoData) return Promise.resolve(demoData);
    return fetch("demo.json").then(function (reply) { return reply.json(); }).then(function (data) {
      var now = Date.now();
      data.mail.forEach(function (mail, index) {
        mail.id = "demo." + index;
        mail.time = new Date(now - mail.minutesAgo * 60000).toISOString();
        mail.attachments = mail.attachments || [];
        mail.cc = mail.cc || "";
        mail.bcc = mail.bcc || "";
      });
      var midnight = new Date();
      midnight.setHours(0, 0, 0, 0);
      var every = (data.events || []).map(function (event, index) { event.id = "demo.e" + index; return event; });
      (data.calendars || []).forEach(function (one, number) {
        one.events.forEach(function (event, index) { event.id = "demo.c" + number + "." + index; event.other = true; every.push(event); });
      });
      every.forEach(function (event) {
        var at = (event.at || "00:00").split(":");
        var begins = new Date(midnight.getFullYear(), midnight.getMonth(), midnight.getDate() + event.inDays, Number(at[0]), Number(at[1]));
        var ends = event.allDay
          ? new Date(begins.getFullYear(), begins.getMonth(), begins.getDate() + (event.days || 1))
          : new Date(begins.getTime() + (event.minutes || 60) * 60000);
        var organizer = event.organizer || data.me.name;
        event.start = begins.toISOString();
        event.end = ends.toISOString();
        event.day = dayOf(begins);
        event.lastDay = dayOf(event.allDay ? new Date(ends.getTime() - 60000) : ends);
        event.allDay = !!event.allDay;
        event.location = event.location || "";
        event.organizer = organizer;
        event.meeting = !!event.required;
        event.canceled = false;
        event.invited = !event.other && organizer !== data.me.name;
        event.mine = !event.other && organizer === data.me.name;
        event.reminder = event.reminder === undefined ? (event.allDay ? null : 15) : event.reminder;
        event.response = event.response || (event.required ? "organizer" : "");
        event.recurring = !!event.recurring;
        event.required = event.required || "";
        event.optional = event.optional || "";
        event.body = event.body || "";
      });
      demoData = data;
      return data;
    });
  }
  // "Priya Nair; Mateo Silva" as people, each with the invented address when there is one
  function demoPeople(data, names) {
    var known = (data.people || []).concat(data.book || [], [data.me]);
    return String(names || "").split(";").map(function (name) { return name.trim(); }).filter(Boolean).map(function (name) {
      var typed = name.indexOf("@") >= 0;   // a mail written here is kept with the addresses it was handed
      var found = known.filter(function (person) { return typed ? person.address.toLowerCase() === name.toLowerCase() : person.name === name; })[0];
      return { name: found ? found.name : name, address: found ? found.address : typed ? name : "" };
    });
  }
  // who Reply and Reply all go to, as titip/mailbox.py works it out on the PC
  function demoPlans(data, mail) {
    var mine = data.me.address.toLowerCase();
    var sender = { name: mail.from, address: mail.fromAddress || "" };
    var fromMe = sender.address.toLowerCase() === mine;
    function others(people, taken) {
      return people.filter(function (person) {
        var key = (person.address || person.name).toLowerCase();
        if (taken[key] || person.address.toLowerCase() === mine) return false;
        taken[key] = true;
        return true;
      });
    }
    var to = demoPeople(data, mail.to), taken = {};
    var first = fromMe ? others(to, {}) : [sender];
    if (!first.length) first = [sender];
    first.forEach(function (person) { taken[(person.address || person.name).toLowerCase()] = true; });
    return {
      reply: { to: first, cc: [] },
      replyAll: { to: first.concat(others(to, taken)), cc: others(demoPeople(data, mail.cc), taken) }
    };
  }
  function demoEvent(event) {
    var row = {};
    Object.keys(event).forEach(function (name) {
      if (["inDays", "at", "minutes", "days", "required", "optional", "body"].indexOf(name) < 0) row[name] = event[name];
    });
    return row;
  }
  var demoFiles = {};   // files "attached" in the demo: only their names and sizes
  var demo = {
    state: function () {
      return demoLoad().then(function (data) {
        var counts = { inbox: 0, sent: 0, drafts: 0, outbox: 0 };
        data.mail.forEach(function (mail) { if (mail.folder in counts) counts[mail.folder] += 1; });
        return { ok: true, demo: true, account: data.me.address, name: data.me.name, counts: counts };
      });
    },
    list: function (folder, offset, query, deep) {
      return demoLoad().then(function (data) {
        var needle = query.toLowerCase().trim();
        var rows = data.mail.filter(function (mail) {
          return mail.folder === folder &&
            (!needle || (mail.subject + " " + mail.from + " " + mail.to + (deep ? " " + mail.body : "")).toLowerCase().indexOf(needle) >= 0);
        }).sort(function (a, b) { return a.time < b.time ? 1 : -1; });
        return {
          folder: folder,
          more: rows.length > offset + PAGE,
          mail: rows.slice(offset, offset + PAGE).map(function (mail) {
            return {
              id: mail.id, subject: mail.subject, from: mail.from, to: mail.to, time: mail.time, unread: !!mail.unread,
              kind: mail.invite === undefined ? "mail" : "meeting", flagged: !!mail.flagged, important: !!mail.important,
              attachments: mail.attachments.length > 0, preview: mail.body.replace(/\s+/g, " ").slice(0, 180)
            };
          })
        };
      });
    },
    get: function (id) {
      return demoLoad().then(function (data) {
        var mail = data.mail.filter(function (candidate) { return candidate.id === id; })[0];
        if (!mail) throw new Error("That message was moved or deleted. Refresh the list.");
        var copy = JSON.parse(JSON.stringify(mail));
        copy.draft = mail.folder === "drafts" || mail.folder === "outbox";
        copy.html = mail.html || "";
        copy.people = { to: demoPeople(data, mail.to), cc: demoPeople(data, mail.cc), bcc: demoPeople(data, mail.bcc) };
        copy.answer = demoPlans(data, mail);
        copy.kind = mail.invite === undefined ? "mail" : "meeting";
        copy.flagged = !!mail.flagged;
        copy.important = !!mail.important;
        copy.categories = (mail.categories || []).slice();
        copy.invite = mail.invite === undefined ? null : demoEvent(data.events[mail.invite]);
        copy.attachments = mail.attachments.map(function (file, index) {
          return { index: index + 1, name: file.name, size: file.size };
        });
        return copy;
      });
    },
    send: function (payload) {
      return demoLoad().then(function (data) {
        var source = data.mail.filter(function (candidate) { return candidate.id === payload.id; })[0];
        function shown(entries) { return [].concat(entries || []).join("; "); }
        var to = shown(payload.to);
        var subject = payload.subject || "";
        var files = [];
        if (payload.mode === "reply" || payload.mode === "replyAll") {
          if (payload.recipients !== "exact") to = to || source.from;
          subject = "RE: " + source.subject;
        }
        if (payload.mode === "forward") subject = "FW: " + source.subject;
        if (payload.mode === "forward" || payload.mode === "draft") {
          var gone = (payload.drop || []).map(function (file) { return file.index; });
          files = source.attachments.filter(function (file, index) { return gone.indexOf(index + 1) < 0; });
        }
        (payload.files || []).forEach(function (id) {
          if (demoFiles[id]) files.push(demoFiles[id]);
          delete demoFiles[id];
        });
        if (payload.mode === "draft") data.mail.splice(data.mail.indexOf(source), 1);
        data.mail.push({
          id: "demo.n" + Date.now(), folder: payload.action === "draft" ? "drafts" : "sent",
          from: data.me.name, fromAddress: data.me.address, to: to, cc: shown(payload.cc), bcc: shown(payload.bcc),
          subject: subject || "(no subject)", body: payload.body, time: new Date().toISOString(), attachments: files,
          important: payload.importance === "high", receipt: !!payload.receipt
        });
        return payload.action === "draft" ? { ok: true, saved: true } : { ok: true, sent: true };
      });
    },
    upload: function (piece) {
      var file = demoFiles[piece.upload] = demoFiles[piece.upload] || { name: piece.name, size: 0 };
      file.size += Math.floor(piece.data.replace(/=+$/, "").length * 3 / 4);
      return Promise.resolve({ ok: true });
    },
    calendars: function () {
      return demoLoad().then(function (data) {
        return { calendars: (data.calendars || []).map(function (one) { return { id: one.id, name: one.name, group: one.group || "" }; }) };
      });
    },
    rules: function () {
      return demoLoad().then(function (data) {
        return { rules: (data.rules || []).map(function (rule, order) { return { name: rule.name, on: !!rule.on, order: order + 1, sent: false }; }) };
      });
    },
    arrange: function (order) {
      return demoLoad().then(function (data) {
        data.folders = data.folders || [];
        data.categories = data.categories || [];
        data.rules = data.rules || [];
        var folder = data.folders.filter(function (one) { return one.id === (order.id || order.to); })[0];
        if (order.what === "folder") {
          var inside = order["in"] === "inbox" ? "Inbox" : (data.folders.filter(function (one) { return one.id === order["in"]; })[0] || { name: "" }).name;
          var made = { id: "f.de30000000." + ("0000000000000000" + (data.folders.length + 176).toString(16).toUpperCase()).slice(-16), name: order.name, "in": inside };
          data.folders.push(made);
          return { ok: true, id: made.id, name: made.name };
        }
        if (order.what === "rename") { folder.name = order.name; return { ok: true, name: order.name }; }
        if (order.what === "unfolder") {
          if (data.mail.some(function (mail) { return mail.folder === order.id; })) throw new Error("That folder is not empty. Only an empty folder is taken out from the phone.");
          data.folders.splice(data.folders.indexOf(folder), 1);
          return { ok: true, removed: true };
        }
        if (order.what === "category") { data.categories.push(order.name); return { ok: true, name: order.name }; }
        if (order.what === "rule") {
          data.rules.forEach(function (rule) { if (rule.name === order.name) rule.on = !!order.on; });
          return { ok: true, on: !!order.on };
        }
        var mail = data.mail.filter(function (candidate) { return candidate.id === order.id; })[0];
        var name = mail.from + " \u2192 " + (order.to === "junk" ? "Junk" : folder.name);
        data.rules.push({ name: name, on: true });
        return { ok: true, name: name };
      });
    },
    calendar: function (offset, days, which) {
      return demoLoad().then(function (data) {
        var first = new Date();
        first.setHours(0, 0, 0, 0);
        first.setDate(first.getDate() + offset);
        var after = new Date(first.getFullYear(), first.getMonth(), first.getDate() + days);
        var other = which ? (data.calendars || []).filter(function (one) { return one.id === which; })[0] : null;
        var events = (other ? other.events : data.events || []).filter(function (event) {
          return event.day < dayOf(after) && event.lastDay >= dayOf(first);
        }).sort(function (a, b) { return a.start < b.start ? -1 : 1; }).map(demoEvent);
        return { from: dayOf(first), days: days, offset: offset, events: events, more: false };
      });
    },
    event: function (id) {
      return demoLoad().then(function (data) {
        var every = (data.events || []).slice();
        (data.calendars || []).forEach(function (one) { every = every.concat(one.events); });
        var event = every.filter(function (candidate) { return candidate.id === id; })[0];
        if (!event) throw new Error("That calendar entry was moved or deleted. Refresh the calendar.");
        var copy = JSON.parse(JSON.stringify(event));
        copy.invitedPeople = demoPeople(data, event.required).filter(function (person) { return person.name !== data.me.name; });
        return copy;
      });
    },
    act: function (order) {
      return demoLoad().then(function (data) {
        var mail = data.mail.filter(function (candidate) { return candidate.id === order.id; })[0];
        if (order.what !== "respond" && !mail) throw new Error("That message was moved or deleted. Refresh the list.");
        if (order.what === "delete") { mail.folder = "deleted"; return { ok: true, deleted: true }; }
        if (order.what === "move") { mail.folder = order.to; return { ok: true, moved: true }; }
        if (order.what === "read" || order.what === "unread") { mail.unread = order.what === "unread"; return { ok: true, unread: mail.unread }; }
        if (order.what === "flag" || order.what === "unflag") { mail.flagged = order.what === "flag"; return { ok: true, flagged: mail.flagged }; }
        if (order.what === "categorize") { mail.categories = order.categories.slice(); return { ok: true, categories: mail.categories }; }
        var event = mail ? data.events[mail.invite] : (data.events || []).filter(function (candidate) { return candidate.id === order.id; })[0];
        if (!event) throw new Error("That is not an invitation.");
        event.response = { accept: "accepted", tentative: "tentative", decline: "declined" }[order.answer];
        return { ok: true, responded: order.answer, told: true };
      });
    },
    folders: function () {
      return demoLoad().then(function (data) {
        return { folders: (data.folders || []).map(function (folder) { return { id: folder.id, name: folder.name, "in": folder["in"], file: "", unread: 0 }; }),
          categories: (data.categories || []).slice(), cut: false };
      });
    },
    signature: function () { return demoLoad().then(function (data) { return data.signature || { "new": "", reply: "" }; }); },
    plan: function (order) {
      return demoLoad().then(function (data) {
        var rows = order.id ? data.events.filter(function (candidate) { return candidate.id === order.id; }) : [];
        if (order.id && !rows.length) throw new Error("That calendar entry was moved or deleted. Refresh the calendar.");
        if (rows.length && rows[0].recurring && order.scope === "one") {
          rows = rows.filter(function (one) { return one.start.slice(0, 16) === String(order.occurrence).slice(0, 16); });
          if (!rows.length) throw new Error("Outlook cannot find that one time of the series. Refresh the calendar and try again.");
        }
        if (order.what === "remove") {
          rows.forEach(function (one) { data.events.splice(data.events.indexOf(one), 1); });
          return { ok: true, removed: true, told: rows[0].meeting };
        }
        var first = order.allDay ? dayStart(order.day) : new Date(order.start);
        var after = order.allDay ? new Date(first.getFullYear(), first.getMonth(), first.getDate() + (order.days || 1)) : new Date(order.end);
        var people = order["with"] || [];
        function timed(event, shift) {
          var begins = new Date(first.getTime() + shift), ends = new Date(after.getTime() + shift);
          event.allDay = !!order.allDay;
          event.start = begins.toISOString();
          event.end = ends.toISOString();
          event.day = dayOf(begins);
          event.lastDay = dayOf(order.allDay ? new Date(ends.getTime() - 60000) : ends);
          return event;
        }
        function written(event) {
          event.subject = order.subject;
          event.location = order.location || "";
          event.body = order.body || "";
          if (order.reminder !== undefined) event.reminder = order.reminder === "none" ? null : Number(order.reminder);
          return event;
        }
        if (!rows.length) {
          var id = "demo.n" + Date.now();
          var step = order.repeat ? { day: 1, week: 7, month: 30, year: 365 }[order.repeat.every] * 86400000 : 0;
          var times = order.repeat ? Math.min(order.repeat.times || 12, 12) : 1;   // a year of them at most, in the demo
          for (var number = 0; number < times; number += 1) {
            data.events.push(timed(written({
              id: id, organizer: data.me.name, meeting: people.length > 0, canceled: false, invited: false, mine: true,
              response: people.length ? "organizer" : "", recurring: !!order.repeat, busy: "busy", required: people.join("; "), optional: "", reminder: 15
            }), step * number));
          }
          return { ok: true, saved: true, told: people.length > 0 };
        }
        var series = rows[0].recurring && order.scope === "series";
        rows.forEach(function (event) {
          written(event);
          if (!series) timed(event, 0);
          if (order.withExact && order.scope !== "one") {
            event.required = people.join("; ");
            event.meeting = people.length > 0;
            event.response = people.length ? "organizer" : "";
          }
        });
        return { ok: true, saved: true, told: rows[0].meeting };
      });
    },
    PIECE: 384000, LANES: 1, FILES_BYTES: 15000000,
    people: function () { return demoLoad().then(function (data) { return { people: data.people || [] }; }); },
    lookup: function (typed) {
      return demoLoad().then(function (data) {
        return { book: true, people: (data.book || []).filter(function (person) { return person.name.toLowerCase().indexOf(typed) === 0; }) };
      });
    },
    fileUrl: function () { return ""; }
  };
  // ---- your PC from any network: every request is a sealed question the PC answers through the relay
  var FAR_READ_WAIT = 45000, FAR_SEND_WAIT = 90000;
  var FAR_TRIES = 3;        // a read that was lost on the way (phone asleep, the relay hiccuped) is asked again
  var FAR_AGAIN = 800;
  var FAR_SILENT = "Your PC did not answer. Check that it is on, that Titip is running, and that \u201cMail from anywhere\u201d is switched on there.";
  var relay = ticket ? window.TitipFar.connect(ticket) : null;

  // Why a request through the relay failed, in words that say what to do. relay.check() looks at
  // what stands between this phone and the PC; one look serves every request that failed with it.
  var FAR_CHECK_KEEP = 3000;
  var farChecked = null, farCheckedAt = 0;
  function farCheck() {
    if (!farChecked || Date.now() - farCheckedAt > FAR_CHECK_KEEP) {
      farCheckedAt = Date.now();
      farChecked = relay.check();
    }
    return farChecked;
  }
  function farWhy(found, error) {
    var again = window.TitipFar.ios && window.TitipFar.standalone
      ? " On your PC, show the code again and scan it, then put Titip on the Home Screen again and remove this icon."
      : " On your PC, show the code again and scan it.";
    switch (found.is) {
      case "offline": return "This phone is offline.";
      case "nonet": return "This phone has no internet connection right now.";
      case "blocked": return "This network does not let the phone reach the relay (" + relay.host + "). Switch between Wi-Fi and mobile data.";
      case "refused": return "The relay answered with an error (" + found.status + "). Try again in a minute.";
      case "unknown": return "The relay does not know the code this phone has." + again;
      case "key": return "This phone has an older code than your PC." + again;
      case "quiet": return "Your PC was last heard from " + whenText(found.at * 1000) + ". Check that it is on and that Titip is running.";
      case "mailoff": return "\u201cMail from anywhere\u201d is switched off in Titip on your PC.";
    }
    // the relay and the PC are both there now
    if (error.message === "silent") return FAR_SILENT;
    if (error.why === "refused") return "The relay answered with an error (" + error.status + "). Try again in a minute.";
    if (error.why === "unreadable") return "The answer from your PC could not be opened on this phone." + again;
    return "The connection dropped on the way. Try again.";
  }
  function farRequest(route, params, sending, since, tries) {
    var started = since || Date.now();
    return relay.ask(route, params, sending ? FAR_SEND_WAIT : FAR_READ_WAIT).then(function (answer) {
      if (answer.status === 503 && answer.data.busy && !sending && Date.now() - started < BUSY_WAIT) {
        toast(BUSY_TEXT);
        return new Promise(function (resolve) { setTimeout(resolve, 3000); }).then(function () {
          return farRequest(route, params, false, started, tries);
        });
      }
      if (answer.status !== 200) throw new Error(answer.data.error || "Something went wrong on the PC.");
      if (since && $("toastText").textContent === BUSY_TEXT) hideToast();
      return answer.data;
    }, function (error) {
      // a send is never repeated by itself: the PC may have sent it and only the answer got lost
      if (sending) {
        var unsure = new Error(route === "send" ? "Your PC did not confirm it. Look in Sent before you send it again."
          : "Your PC did not confirm it. Look in Outlook before you try it again.");
        unsure.unsure = true;
        throw unsure;
      }
      // reading twice harms nothing: the phone slept through the wait, or the relay hiccuped.
      // (A relay that gave no reply at all was already tried for seconds in far.js.)
      if ((error.message === "asleep" || error.why === "refused") && (tries || 1) < FAR_TRIES) {
        return new Promise(function (resolve) { setTimeout(resolve, FAR_AGAIN); }).then(function () {
          return farRequest(route, params, false, started, (tries || 1) + 1);
        });
      }
      return farCheck().then(function (found) { throw new Error(farWhy(found, error)); });
    });
  }
  var far = {
    state: function () { return farRequest("state", {}); },
    list: function (folder, offset, query, deep) { return farRequest("mail", { folder: folder, limit: PAGE, offset: offset, q: query, deep: deep ? "1" : "" }); },
    get: function (id) { return farRequest("item", { id: id }); },
    send: function (payload) { return farRequest("send", payload, true); },
    people: function () { return farRequest("people", {}); },
    lookup: function (typed) { return farRequest("people", { q: typed }); },
    fileUrl: function () { return ""; },
    file: function (id, index) { return farRequest("attachment", { id: id, index: index }); },
    calendar: function (offset, days, which) { return farRequest("calendar", { offset: offset, days: days, which: which || "" }); },
    calendars: function () { return farRequest("calendars", {}); },
    rules: function () { return farRequest("rules", {}); },
    arrange: function (order) { return farRequest("arrange", order, true); },
    event: function (id) { return farRequest("event", { id: id }); },
    act: function (order) { return farRequest("act", order, order.what !== "read"); },   // like a send: never made twice (marking as read may be)
    plan: function (order) { return farRequest("plan", order, true); },
    folders: function () { return farRequest("folders", {}); },
    signature: function () { return farRequest("signature", {}); },
    upload: function (piece) { return farRequest("upload", piece); },   // the same piece twice does no harm
    PIECE: 96000, LANES: 3, FILES_BYTES: 5000000   // a question through the relay holds 200,000 characters once sealed
  };

  var DEMO = FORCED_DEMO || (HOSTED && !ticket);
  var api = DEMO ? demo : ticket ? far : pc;

  // The public page is updated before the PC has moved to the same version, and a PC can stay behind.
  // What 0.9.0 added is then said to be on its way, instead of asked of a PC that does not know the question.
  var NEEDS = "0.9.0", NEEDS_MORE = "0.10.0";   // what came with 0.10.0 asks for that one
  function pcBehind(needs) {
    if (DEMO || !pcVersion) return false;
    var has = pcVersion.split("."), wants = (needs || NEEDS).split(".");
    for (var i = 0; i < wants.length; i += 1) {
      var one = Number(has[i]) || 0, other = Number(wants[i]);
      if (one !== other) return one < other;
    }
    return false;
  }
  function behindText(needs) {
    return "Titip on your PC is version " + pcVersion + " and this needs " + (needs || NEEDS) + ". It updates itself, usually within the hour.";
  }

  // ------------------------------------------------------------ small helpers
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function shortTime(iso) {
    if (!iso) return "";
    var date = new Date(iso);
    var now = new Date();
    var days = Math.floor((new Date(now.getFullYear(), now.getMonth(), now.getDate()) -
      new Date(date.getFullYear(), date.getMonth(), date.getDate())) / 86400000);
    if (days <= 0) return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    if (days < 7) return date.toLocaleDateString([], { weekday: "short" });
    return date.toLocaleDateString([], { day: "numeric", month: "short" });
  }

  function clockTime(iso) {
    return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
  }

  function longTime(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleString([], { weekday: "short", day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit" });
  }

  // "2026-11-20" for a moment, on this phone's clock; and back to that day's midnight
  function dayOf(date) {
    function two(number) { return (number < 10 ? "0" : "") + number; }
    return date.getFullYear() + "-" + two(date.getMonth() + 1) + "-" + two(date.getDate());
  }
  function dayStart(day) {
    var parts = String(day).split("-");
    return new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
  }
  function clock(iso) { return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }
  function dayName(date) { return date.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }); }

  function icon(shape, className, label) {
    var drawn = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    drawn.setAttribute("viewBox", "0 0 24 24");
    drawn.setAttribute("class", className);
    if (label) { drawn.setAttribute("role", "img"); drawn.setAttribute("aria-label", label); }
    else drawn.setAttribute("aria-hidden", "true");
    var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", shape);
    drawn.appendChild(path);
    return drawn;
  }
  var CLIP = "M20 11l-8.5 8.5a5 5 0 0 1-7-7L13 4a3.5 3.5 0 0 1 5 5l-8.5 8.5a2 2 0 0 1-3-3L14 7";
  var DATES = "M4 6h16v14H4zM4 10h16M8 3v4M16 3v4";
  var FLAG = "M6 21V4M6 4h11l-2 4 2 4H6";

  // the name of a folder: Outlook's own ones, or one of yours as the PC listed it
  function folderName(folder) {
    if (FOLDERS[folder]) return FOLDERS[folder];
    var found = own.folders.filter(function (one) { return one.id === folder; })[0];
    return found ? found.name : "Folder";
  }
  function isOwnFolder(folder) { return /^f\.[0-9a-f]{10}\.[0-9A-Fa-f]{16,}$/.test(folder); }

  function fileSize(bytes) {
    if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + " MB";
    return Math.max(1, Math.round(bytes / 1024)) + " KB";
  }

  // the round badge with someone's initials, as Outlook draws it: the same person always gets the same colour
  function initials(name) {
    var words = String(name || "").replace(/[<(\[].*$/, "").replace(/["']/g, "").trim().split(/[\s,;._@-]+/).filter(Boolean);
    if (!words.length) return "?";
    var first = words[0].charAt(0);
    var last = words.length > 1 ? words[words.length - 1].charAt(0) : "";
    return (first + last).toUpperCase();
  }
  function paint(badge, name, extra) {
    var text = String(name || "");
    var sum = 0;
    for (var i = 0; i < text.length; i += 1) sum = (sum * 31 + text.charCodeAt(i)) % 9973;
    badge.className = "face c" + (sum % 8) + (extra ? " " + extra : "");
    badge.textContent = initials(text);
    badge.setAttribute("aria-hidden", "true");
    return badge;
  }
  function face(name, extra) { return paint(el("span"), name, extra); }

  // whose mailbox this is, top left as in Outlook; remembered so it is there before the PC answers
  function showMe(name) {
    if (!name) return;
    paint($("me"), name, "me");
    $("me").hidden = false;
    if (!DEMO) store.set("titip.me", name);
  }

  // the headings Outlook puts between mail of different age
  function dayGroup(iso) {
    if (!iso) return "Older";
    var date = new Date(iso);
    var now = new Date();
    var days = Math.floor((new Date(now.getFullYear(), now.getMonth(), now.getDate()) -
      new Date(date.getFullYear(), date.getMonth(), date.getDate())) / 86400000);
    if (days <= 0) return "Today";
    if (days === 1) return "Yesterday";
    if (days < 7) return "Last 7 days";
    if (date.getFullYear() === now.getFullYear() && date.getMonth() === now.getMonth()) return "This month";
    return "Older";
  }

  // "today 14:02" style, for the note under a list that could not be refreshed
  function whenText(at) {
    var date = new Date(at);
    var clock = date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    return date.toDateString() === new Date().toDateString() ? clock
      : date.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }) + ", " + clock;
  }

  var toastTimer = 0;
  function toast(text, actionLabel, action, stay) {
    clearTimeout(toastTimer);
    $("toastText").textContent = text;
    var button = $("toastAction");
    button.hidden = !actionLabel;
    button.textContent = actionLabel || "";
    button.onclick = action || null;
    $("toast").hidden = false;
    if (!stay) toastTimer = setTimeout(hideToast, 5000);
  }
  function hideToast() { clearTimeout(toastTimer); $("toast").hidden = true; }

  // ------------------------------------------------------------ list
  function showState(title, detail, retry) {
    var box = $("state");
    box.textContent = "";
    if (!title) return;
    box.appendChild(el("strong", "", title));
    if (detail) box.appendChild(document.createTextNode(detail));
    if (retry) {
      box.appendChild(el("br"));
      var button = el("button", "", "Try again");
      button.type = "button";
      button.onclick = function () { load(false); };
      box.appendChild(button);
    }
  }

  function skeleton() {
    var list = $("rows");
    list.textContent = "";
    for (var i = 0; i < 6; i += 1) {
      var item = el("li", "skeleton");
      item.setAttribute("aria-hidden", "true");
      item.appendChild(el("i")); item.appendChild(el("i")); item.appendChild(el("i"));
      list.appendChild(item);
    }
  }

  function renderRows() {
    if (view.folder === "calendar") { renderEvents(); return; }
    var list = $("rows");
    list.textContent = "";
    var group = "";
    view.rows.forEach(function (mail) {
      var heading = view.query ? "" : dayGroup(mail.time);   // search results come by mail file, not by day
      if (heading && heading !== group) {
        group = heading;
        list.appendChild(el("li", "day", heading));
      }
      var item = el("li");
      var unread = (view.folder === "inbox" || isOwnFolder(view.folder)) && mail.unread && opened.indexOf(mail.id) < 0;
      var row = el("button", "row" + (unread ? " unread" : ""));
      row.type = "button";
      if (mail.id === view.id) row.setAttribute("aria-current", "true");
      var who = TO_FOLDERS[view.folder] ? (mail.to || "(no recipient yet)") : mail.from;
      row.appendChild(face(who));
      var name = el("span", "who");
      if (view.folder === "drafts") name.appendChild(el("span", "tagDraft", "[Draft] "));
      name.appendChild(document.createTextNode(who));
      row.appendChild(name);
      row.appendChild(el("span", "time", shortTime(mail.time)));
      var subject = el("span", "subject");
      if (unread) subject.appendChild(el("span", "sr", "Unread. "));
      if (mail.important) subject.appendChild(el("span", "bang", "! "));
      if (mail.flagged) subject.appendChild(icon(FLAG, "clip flag", "Flagged"));
      if (mail.kind === "meeting") subject.appendChild(icon(DATES, "clip", "Meeting"));
      if (mail.attachments) subject.appendChild(icon(CLIP, "clip", "Has attachments"));
      subject.appendChild(document.createTextNode(mail.subject));
      row.appendChild(subject);
      row.appendChild(el("span", "preview", mail.preview));
      row.onclick = function () { location.hash = "#/" + view.folder + "/" + mail.id; };
      item.appendChild(row);
      list.appendChild(item);
    });
    $("more").hidden = !view.more;
    if (!view.rows.length) {
      if (view.query) showState("No match", "Nothing in " + folderName(view.folder) + " matches \u201c" + view.query + "\u201d" + (view.deep ? ", not in the text of the mail either." : "."));
      else if (view.folder === "drafts") showState("No drafts", "Mail you save from the phone waits here, and in Outlook's Drafts.");
      else if (view.folder === "outbox") showState("Nothing is waiting", "Mail that Outlook has not sent yet shows here until it leaves.");
      else if (view.folder === "deleted") showState("Nothing deleted", "Mail you delete lands here. Outlook empties it by its own rules.");
      else if (view.folder === "junk") showState("No junk", "Mail that Outlook takes for junk lands here.");
      else showState("Nothing here yet", "New mail shows up on its own once a minute.");
    } else {
      showState("");
    }
  }

  // The phone keeps the first page of each folder (sender, subject, first lines), so the list is on
  // screen the moment the app opens and stays there when the PC cannot be reached. Unlinking clears it.
  function keptList(folder) {
    var kept = null;
    try { kept = JSON.parse(store.get("titip.list." + folder) || "null"); } catch (error) { kept = null; }
    return kept && Array.isArray(kept.mail) && kept.mail.length ? kept : null;
  }
  function keepList(folder, page) {
    if (DEMO || !KEPT_FOLDERS[folder]) return;
    try { store.set("titip.list." + folder, JSON.stringify({ at: Date.now(), mail: page.mail, more: !!page.more })); }
    catch (error) { /* storage full: the list is simply asked for again next time */ }
  }

  // only what was asked for first is refreshed without a tap: older mail or later days would be folded away
  function firstPage() { return view.folder === "calendar" ? view.days <= CAL_DAYS : view.rows.length <= PAGE; }

  var STALE_AGAIN = 15000;   // a list that could not be refreshed is asked for again this soon, without a tap
  var staleTimer = 0;
  function retryStale() {
    clearTimeout(staleTimer);
    if (!view.stale || view.query) return;
    if (document.hidden || !$("composer").hidden) { staleTimer = setTimeout(retryStale, STALE_AGAIN); return; }
    load(false, true);
  }

  // ------------------------------------------------------------ calendar: the days ahead, two weeks at a time
  function eventDay(event) {   // the day it is listed under: where it starts, or the first day shown when it began before
    var day = event.allDay ? event.day : dayOf(new Date(event.start));
    return day < view.from ? view.from : day;
  }
  function eventKey(event) { return event.id + "@" + event.start; }   // every time a repeating meeting takes place has the same id
  function eventTimes(event) {
    if (event.allDay) return ["All day", ""];
    return [clock(event.start), clock(event.end)];
  }
  var SAID = { none: "Invited", tentative: "Tentative", declined: "Declined" };

  function renderEvents() {
    var list = $("rows");
    list.textContent = "";
    var today = dayOf(new Date());
    var tomorrow = dayOf(new Date(Date.now() + 86400000));
    var group = "";
    view.rows.forEach(function (event) {
      var day = eventDay(event);
      if (day !== group) {
        group = day;
        var dated = dayName(dayStart(day));
        list.appendChild(el("li", "day", day === today ? "Today \u00b7 " + dated : day === tomorrow ? "Tomorrow \u00b7 " + dated : dated));
      }
      var item = el("li");
      var open = event.invited && event.response === "none" && !event.canceled;   // an invitation nobody answered yet
      var row = el("button", "row event" + (open ? " unread" : "") + (event.canceled || event.response === "declined" ? " off" : ""));
      row.type = "button";
      if (eventKey(event) === view.id) row.setAttribute("aria-current", "true");
      var times = eventTimes(event);
      var when = el("span", "hours");
      when.appendChild(el("span", "", times[0]));
      if (times[1]) when.appendChild(el("span", "until", times[1]));
      row.appendChild(when);
      var name = el("span", "who");
      if (open) name.appendChild(el("span", "sr", "Not answered yet. "));
      name.appendChild(document.createTextNode(event.subject));
      row.appendChild(name);
      row.appendChild(el("span", "time", event.canceled ? "Canceled" : event.invited ? SAID[event.response] || "" : ""));
      var where = [event.location, event.invited ? event.organizer : ""].filter(Boolean).join(" \u00b7 ");
      if (event.recurring) where = (where ? where + " \u00b7 " : "") + "Repeats";
      row.appendChild(el("span", "preview", where));
      row.onclick = function () { location.hash = "#/calendar/" + eventKey(event); };
      item.appendChild(row);
      list.appendChild(item);
    });
    $("more").hidden = !view.more;
    if (!view.rows.length) showState("Nothing planned", "No meeting or appointment in the next " + view.days + " days.");
    else showState("");
  }

  function loadCalendar(append, silent) {
    if (view.loading && (append || silent)) return Promise.resolve();
    loads += 1;
    var mine = loads;
    view.loading = true;
    var offset = append ? view.days : 0;
    if (!append && !silent) {
      showState("");
      showSaved({});
      if (!view.rows.length) { skeleton(); $("more").hidden = true; }
    }
    if (!silent) $("refresh").classList.add("busy");
    return api.calendar(offset, CAL_DAYS, shown.which).then(function (page) {
      if (mine !== loads || view.folder !== "calendar") return;
      view.rows = append ? view.rows.concat(page.events) : page.events;
      view.days = offset + CAL_DAYS;
      if (!append) { view.from = page.from; view.at = Date.now(); view.stale = false; clearTimeout(staleTimer); }
      view.more = view.days < CAL_FURTHEST;
      renderEvents();
      if (!append) showSaved(page, "calendar");
    }).catch(function (error) {
      if (mine !== loads) return;
      if (append || (silent && view.rows.length)) { if (append) toast(error.message); return; }
      $("rows").textContent = "";
      showState("Can't show your calendar", error.message, true);
    }).then(function () {
      if (mine !== loads) return;
      view.loading = false;
      $("refresh").classList.remove("busy");
    });
  }

  var loads = 0;   // only the newest list asked for is shown: a tap on another folder never waits for the one before
  function load(append, silent) {
    if (!KEPT_FOLDERS[view.folder] && pcBehind()) {
      $("rows").textContent = "";
      $("more").hidden = true;
      showState("Not yet", behindText());
      return Promise.resolve();
    }
    if (view.folder === "calendar") return loadCalendar(append, silent);
    if (view.loading && (append || silent)) return Promise.resolve();
    loads += 1;
    var mine = loads;
    view.loading = true;
    var folder = view.folder;
    var query = view.query;
    var offset = append ? view.rows.length : 0;
    if (!append && !silent) {
      showState("");
      showSaved({});
      if (!query && !view.rows.length) {
        var kept = keptList(folder);
        if (kept) { view.rows = kept.mail; view.more = false; view.at = kept.at; renderRows(); }
      }
      // rows already on screen stay there while the fresh list is on its way; the arrows turn meanwhile
      if (query || !view.rows.length) { skeleton(); $("more").hidden = true; }
    }
    if (!silent) $("refresh").classList.add("busy");
    if (query && !append) showState("Searching\u2026", view.deep ? "Looking through the text of every mail. This can take a while." : "Looking through every mail file. This can take a few seconds.");
    return api.list(folder, offset, query, !!query && view.deep).then(function (page) {
      if (mine !== loads) return;
      view.rows = append ? view.rows.concat(page.mail) : page.mail;
      view.more = page.more;
      if (!append) {
        view.stale = false;
        clearTimeout(staleTimer);
        if (!query) { view.at = Date.now(); keepList(folder, page); }
      }
      renderRows();
      if (!append) showSaved(page);
      if (!append && !query) learnPeople();
    }).catch(function (error) {
      if (mine !== loads) return;
      if (append) { toast(error.message); return; }
      if (silent && !view.stale) return;   // a quiet refresh that failed: the next one tries again
      if (!silent) {
        if (!query && view.rows.length) {
          // the list from before stays readable; say that it is old, and why
          var note = $("savedNote");
          note.textContent = error.message + " This is the list from " + whenText(view.at) + ".";
          note.hidden = false;
        } else {
          $("rows").textContent = "";
          showState("Can't show your mail", error.message, true);
        }
      }
      view.stale = !query;
      clearTimeout(staleTimer);
      if (view.stale) staleTimer = setTimeout(retryStale, STALE_AGAIN);
    }).then(function () {
      if (mine !== loads) return;
      view.loading = false;
      $("refresh").classList.remove("busy");
    });
  }

  // Outlook was busy, so the PC sent the list it had: say from when, and ask again quietly
  var savedTimer = 0;
  function showSaved(page, what) {
    clearTimeout(savedTimer);
    var note = $("savedNote");
    note.hidden = !page.saved;
    if (!page.saved) return;
    note.textContent = what === "calendar" ? "Outlook on your PC is busy. This is the calendar as it was a moment ago."
      : "Outlook on your PC is busy. This is the list from " + clockTime(page.savedAt) + ".";
    savedTimer = setTimeout(function () {
      if (document.hidden || view.query || !firstPage() || !$("composer").hidden) return;
      load(false, true);
    }, SAVED_AGAIN);
  }

  function refreshCounts() {
    api.state().then(function (state) {
      var was = pcBehind();
      pcVersion = String(state.version || "");
      if (was !== pcBehind() && !KEPT_FOLDERS[view.folder] && !view.id) { view.rows = []; load(false); }
      showMe(state.name || state.account);
      $("demoNote").hidden = !state.demo;
      var count = $("draftCount");
      count.hidden = !state.counts.drafts;
      count.textContent = state.counts.drafts;
      showOutbox(state.counts.outbox || 0);
      if (!signature.asked && !pcBehind()) {
        signature.asked = true;
        api.signature().then(function (got) {
          signature["new"] = String(got["new"] || "");
          signature.reply = String(got.reply || "");
        }).catch(function () { signature.asked = false; });
      }
    }).catch(function () { /* the list shows the real error */ });
  }

  // a mail in the Outbox has not left yet: said over every list but the Outbox's own
  var waiting = 0;
  function showOutbox(count) {
    if (count !== undefined) waiting = count;
    $("outboxCount").hidden = !waiting;
    $("outboxCount").textContent = waiting;
    $("outboxText").textContent = waiting === 1 ? "1 mail is waiting in the Outbox and has not left yet."
      : waiting + " mails are waiting in the Outbox and have not left yet.";
    $("outboxNote").hidden = !waiting || view.folder === "outbox";
  }

  // ------------------------------------------------------------ reader
  function showBody() {
    var item = view.item;
    var box = $("body");
    box.textContent = "";
    var hasHtml = !!item.html;
    $("viewRow").hidden = !hasHtml;
    $("viewToggle").textContent = view.formatted ? "Show as plain text" : "Show formatted";
    if (hasHtml && view.formatted) {
      var frame = document.createElement("iframe");
      frame.title = "Message";
      // no allow-scripts: nothing in the mail can run. same-origin only lets us measure its height.
      frame.setAttribute("sandbox", "allow-same-origin allow-popups allow-popups-to-escape-sandbox");
      frame.srcdoc = '<base target="_blank"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; ' +
        'style-src \'unsafe-inline\'; img-src data:">' +
        '<style>body{margin:12px;font:15px/1.5 system-ui,sans-serif;color:#1b1a17;background:#fff;overflow-wrap:anywhere}' +
        'img{max-width:100%;height:auto}table{border-collapse:collapse;overflow-wrap:normal;word-break:normal}' +
        'td,th{padding:4px 10px 4px 0;text-align:left;vertical-align:top}</style>' + item.html;
      frame.onload = function () {
        try { frame.style.height = Math.min(frame.contentDocument.documentElement.scrollHeight + 24, 20000) + "px"; }
        catch (error) { frame.style.height = "70vh"; }
      };
      box.appendChild(frame);
    } else {
      box.appendChild(el("pre", "", item.body || "(this message has no text)"));
    }
    if (view.folder === "outbox") {
      box.insertBefore(el("p", "note", "Waiting in Outlook's Outbox on your PC. It leaves when Outlook can send it. Delete stops it."), box.firstChild);
    }
  }

  // ---- a meeting: when and where, what you answered, and the three answers
  function meetingWhen(event) {
    var first = event.allDay ? dayStart(event.day) : new Date(event.start);
    var last = event.allDay ? dayStart(event.lastDay) : new Date(event.end);
    var text = dayName(first);
    if (event.allDay) text += dayOf(first) === dayOf(last) ? ", all day" : " to " + dayName(last);
    else text += ", " + clock(event.start) + " to " + (dayOf(first) === dayOf(last) ? "" : dayName(last) + ", ") + clock(event.end);
    return text + (event.recurring ? " \u00b7 repeats" : "");
  }
  var ANSWERED = {
    none: "You have not answered yet.", accepted: "You accepted.", tentative: "You answered tentative.",
    declined: "You declined.", organizer: "Your own meeting."
  };
  var ANSWERING = { accept: "Accepting", tentative: "Answering tentative", decline: "Declining" };
  var ANSWER_DONE = { accept: "Accepted", tentative: "Answered tentative", decline: "Declined" };
  function showInvite(event, id) {
    var box = $("invite");
    box.hidden = !event;
    if (!event) return;
    $("inviteWhen").textContent = event.start ? meetingWhen(event) : "";
    $("inviteWhere").textContent = event.location || "";
    $("inviteWhere").hidden = !event.location;
    $("inviteSaid").textContent = event.canceled ? "Canceled by the organizer." : ANSWERED[event.response] || "";
    $("inviteSaid").hidden = !$("inviteSaid").textContent;
    var asked = event.invited && !event.canceled;
    $("answers").hidden = !asked;
    Array.prototype.forEach.call($("answers").querySelectorAll("button"), function (button) {
      var answer = button.getAttribute("data-answer");
      var given = { accept: "accepted", tentative: "tentative", decline: "declined" }[answer] === event.response;
      button.setAttribute("aria-pressed", String(given));
      button.onclick = function () { respond(id, answer, event); };
    });
  }
  function respond(id, answer, event) {
    var showing = view.id;
    later(ANSWERING[answer], function () {
      toast(ANSWERING[answer] + "\u2026", "", null, true);
      api.act({ what: "respond", id: id, answer: answer }).then(function (result) {
        toast(ANSWER_DONE[answer] + (result.told === false ? ". The organizer asked for no answers." : ". " + (event.organizer || "The organizer") + " is told."));
        if (view.folder === "calendar") load(false, true);
        if (view.id !== showing) return;
        if (answer === "decline" && view.folder === "calendar") { location.hash = "#/calendar"; return; }   // Outlook takes a declined meeting out
        event.response = { accept: "accepted", tentative: "tentative", decline: "declined" }[answer];
        showInvite(event, id);
      }).catch(function (error) { toast((error.unsure ? "" : "Not answered. ") + error.message); });
    });
  }

  function openEvent(key) {
    var cut = key.lastIndexOf("@");
    var id = cut > 0 ? key.slice(0, cut) : key;
    var start = cut > 0 ? key.slice(cut + 1) : "";
    view.id = key;
    view.item = null;
    document.body.classList.add("reading");
    $("readerHint").hidden = true;
    $("backLabel").textContent = FOLDERS.calendar;
    renderEvents();
    var row = view.rows.filter(function (event) { return event.id === id && (!start || event.start === start); })[0];
    showEvent(row || { subject: "", organizer: "" }, id, null);
    $("letter").hidden = false;
    api.event(id).then(function (full) {
      if (view.id !== key) return;
      // of a repeating meeting the PC answers with the series: this one's day and hours come from the list
      if (row) ["start", "end", "day", "lastDay", "allDay", "response", "canceled"].forEach(function (name) { full[name] = row[name]; });
      if (shown.which) { full.mine = false; full.invited = false; }   // another calendar is read, not answered or changed
      showEvent(full, id, full);
      $("reader").scrollTop = 0;
      window.scrollTo(0, 0);
    }).catch(function (error) {
      if (view.id !== key) return;
      toast(error.message);
      if (!row) location.hash = "#/calendar";
    });
  }
  function showEvent(event, id, full) {
    $("letter").classList.remove("opening");
    $("subject").textContent = event.subject;
    paint($("face"), event.organizer || "?", "big");
    $("from").textContent = event.organizer || "";
    $("fromAddress").textContent = "";
    $("toLabel").textContent = "With:";
    $("to").textContent = full ? full.required || "(nobody else)" : "\u2026";
    $("ccRowLabel").textContent = "Optional:";
    $("ccRow").hidden = !(full && full.optional);
    $("cc").textContent = full ? full.optional : "";
    $("bccRow").hidden = true;
    $("when").textContent = "";
    $("files").hidden = true;
    $("viewRow").hidden = true;
    $("remove").hidden = true;
    $("moreToggle").hidden = true;
    $("tags").hidden = true;
    showMore(false);
    var actions = $("actions");
    actions.textContent = "";
    // your own entry, not a repeating one, and not one over several days with hours: those are changed in Outlook
    var changeable = !!full && full.mine && !pcBehind() && (!full.recurring || !pcBehind(NEEDS_MORE)) &&
      (full.allDay || new Date(full.end) - new Date(full.start) < 86400000);
    actions.hidden = !changeable;
    if (changeable) {
      var change = el("button", "primary", "Change");
      change.type = "button";
      change.onclick = function () {
        if (full.recurring) whichOnes("Change", full, function (scope) { openPlanner(full, scope); });
        else openPlanner(full);
      };
      var gone = el("button", "", full.meeting ? "Cancel the meeting" : "Delete");
      gone.type = "button";
      gone.onclick = function () {
        if (full.recurring) whichOnes(full.meeting ? "Cancel" : "Delete", full, function (scope) { removeEntry(full, scope); });
        else removeEntry(full);
      };
      actions.appendChild(change);
      actions.appendChild(gone);
    }
    showInvite(event, id);
    var box = $("body");
    box.textContent = "";
    if (!full) box.appendChild(el("p", "wait", "Opening\u2026"));
    else box.appendChild(el("pre", "", full.body.trim() || "(no description)"));
  }

  function openItem(id) {
    view.id = id;
    markOpened(id);
    document.body.classList.add("reading");
    $("readerHint").hidden = true;
    $("backLabel").textContent = folderName(view.folder);
    showMore(false);
    renderRows();
    // the list already knows the subject: show it and the way back at once, the rest when the PC answers
    var row = view.rows.filter(function (mail) { return mail.id === id; })[0];
    $("subject").textContent = row ? row.subject : "";
    $("body").textContent = "";
    // ... and how the message starts, which the list knows too: enough to read on while Outlook is busy
    if (row && row.preview) $("body").appendChild(el("p", "peek", row.preview + "\u2026"));
    $("body").appendChild(el("p", "wait", row && row.preview ? "Opening the rest\u2026" : "Opening\u2026"));
    $("letter").classList.add("opening");
    $("letter").hidden = false;
    api.get(id).then(function (item) {
      if (view.id !== id) return;
      view.item = item;
      view.formatted = true;
      if (item.saved) toast("Outlook on your PC is busy. This draft may not show your latest changes.");
      $("letter").classList.remove("opening");
      $("subject").textContent = item.subject;
      paint($("face"), item.from, "big");
      $("from").textContent = item.from;
      $("fromAddress").textContent = item.fromAddress && item.fromAddress !== item.from ? item.fromAddress : "";
      if (!item.draft && view.folder === "inbox") addPerson(item.from, item.fromAddress);
      $("toLabel").textContent = "To:";
      $("to").textContent = item.to || "(none)";
      $("ccRowLabel").textContent = "Cc:";
      $("ccRow").hidden = !item.cc;
      $("cc").textContent = item.cc;
      $("bccRow").hidden = !item.bcc;
      $("bcc").textContent = item.bcc || "";
      $("when").textContent = longTime(item.time);
      showInvite(item.invite || null, item.id);
      showTags(item);
      // read here is read in Outlook, as in Outlook's own phone app. Nobody waits for it, and a mail that
      // asks for a read receipt is left unread there: that question belongs on the PC.
      if (row && row.unread && !item.draft && !pcBehind() && view.folder !== "outbox") {
        row.unread = false;
        api.act({ what: "read", id: item.id }).catch(function () { /* still marked on this phone */ });
      }
      var doable = !pcBehind() && !item.draft && view.folder !== "outbox";
      $("moreToggle").hidden = !doable;
      $("doUnread").hidden = view.folder === "sent";
      $("doFlag").textContent = item.flagged ? "Clear the flag" : "Flag";
      $("doUnread").onclick = function () { showMore(false); markUnread(item); };
      $("doFlag").onclick = function () { showMore(false); flagMail(item); };
      $("doMove").onclick = function () { showMore(false); moveMail(item, view.folder); };
      $("doLabel").onclick = function () { showMore(false); labelMail(item); };
      $("doRule").hidden = pcBehind(NEEDS_MORE) || !item.from || view.folder === "sent";
      $("doRule").onclick = function () { showMore(false); ruleFor(item, view.folder); };
      // Delete moves it to Deleted Items; what is there already is only removed for good in Outlook
      $("remove").hidden = view.folder === "deleted" || pcBehind();
      $("remove").textContent = view.folder === "drafts" ? "Discard draft" : "Delete";
      $("remove").onclick = function () { removeMail(item, view.folder); };
      var files = $("files");
      files.textContent = "";
      files.hidden = !item.attachments.length;
      item.attachments.forEach(function (file) {
        var entry = el("li");
        var link = el("a", "", file.name);
        link.appendChild(el("small", "", fileSize(file.size)));
        var url = api.fileUrl(item.id, file.index);
        link.href = url || "#";
        if (url) link.setAttribute("download", file.name);
        else if (api.file) link.onclick = function (event) { event.preventDefault(); fetchFile(item.id, file); };
        else link.onclick = function (event) { event.preventDefault(); toast("Attachments open in the real app, not in the demo."); };
        entry.appendChild(link);
        files.appendChild(entry);
      });
      var actions = $("actions");
      actions.textContent = "";
      var modes = view.folder === "outbox" ? []   // on its way out: it can be stopped, not changed
        : item.draft ? [["draft", "Edit draft"]] : [["reply", "Reply"], ["replyAll", "Reply all"], ["forward", "Forward"]];
      actions.hidden = !modes.length;
      modes.forEach(function (pair, index) {
        var button = el("button", index === 0 ? "primary" : "", pair[1]);
        button.type = "button";
        button.setAttribute("data-mode", pair[0]);
        button.onclick = function () { openComposer(pair[0], item); };
        actions.appendChild(button);
      });
      showBody();
      $("letter").hidden = false;
      $("reader").scrollTop = 0;
      window.scrollTo(0, 0);
    }).catch(function (error) {
      if (view.id !== id) return;
      toast(error.message);
      location.hash = "#/" + view.folder;
    });
  }

  // Through the relay a file arrives sealed like everything else; the browser saves it from memory.
  function fetchFile(id, file) {
    toast("Getting " + file.name + "\u2026", "", null, true);
    api.file(id, file.index).then(function (got) {
      var blob = new Blob([window.TitipFar.fromBase64(got.data)], { type: "application/octet-stream" });
      var address = URL.createObjectURL(blob);
      var save = el("a");
      save.href = address;
      save.download = got.name || file.name;
      document.body.appendChild(save);
      save.click();
      save.remove();
      setTimeout(function () { URL.revokeObjectURL(address); }, 60000);
      toast("Saved " + (got.name || file.name));
    }).catch(function (error) { toast(error.message); });
  }

  // ---- what else can be done with a message: under More, next to Delete
  function showMore(on) {
    $("moreMenu").hidden = !on;
    $("moreToggle").setAttribute("aria-expanded", String(on));
  }
  function showTags(item) {
    var said = [];
    if (item.important) said.push("High importance");
    if (item.flagged) said.push("Flagged");
    (item.categories || []).forEach(function (name) { said.push(name); });
    $("tags").textContent = said.join(" \u00b7 ");
    $("tags").hidden = !said.length;
  }
  function rowOf(id) { return view.rows.filter(function (mail) { return mail.id === id; })[0]; }
  function markUnread(item) {
    api.act({ what: "unread", id: item.id }).then(function () {
      opened = opened.filter(function (id) { return id !== item.id; });
      store.set("titip.opened", JSON.stringify(opened));
      var row = rowOf(item.id);
      if (row) row.unread = true;
      toast("Marked as unread");
      if (view.id === item.id) location.hash = "#/" + view.folder;
    }).catch(function (error) { toast((error.unsure ? "" : "Not changed. ") + error.message); });
  }
  function flagMail(item) {
    var flag = !item.flagged;
    api.act({ what: flag ? "flag" : "unflag", id: item.id }).then(function () {
      item.flagged = flag;
      var row = rowOf(item.id);
      if (row) row.flagged = flag;
      toast(flag ? "Flagged" : "Flag cleared");
      if (view.id !== item.id) return;
      showTags(item);
      $("doFlag").textContent = flag ? "Clear the flag" : "Flag";
      renderRows();
    }).catch(function (error) { toast((error.unsure ? "" : "Not changed. ") + error.message); });
  }
  function moveMail(item, folder) {
    pickFrom("Move to", "", false, function () {
      var places = [];
      if (folder !== "inbox") places.push({ id: "inbox", name: "Inbox", note: "" });
      own.folders.forEach(function (one) {
        if (one.id !== folder) places.push({ id: one.id, name: one.name, note: [one.file, one["in"]].filter(Boolean).join(" / ") });
      });
      if (folder !== "junk") places.push({ id: "junk", name: "Junk", note: "" });
      return places;
    }, function (place) {
      later("Moving to " + place.name, function () {
        toast("Moving\u2026", "", null, true);
        api.act({ what: "move", id: item.id, to: place.id }).then(function () {
          toast("Moved to " + place.name);
          view.rows = view.rows.filter(function (mail) { return mail.id !== item.id; });
          if (view.id === item.id) location.hash = "#/" + view.folder;
          else if (view.folder === folder) renderRows();
          if (view.folder === folder) load(false, true);
        }).catch(function (error) { toast((error.unsure ? "" : "Not moved. ") + error.message); });
      });
    });
  }
  function labelMail(item) {
    pickFrom("Categories", "Outlook's categories. Tick the ones this mail belongs to.", true, function () {
      var names = own.categories.slice();
      (item.categories || []).forEach(function (name) { if (names.indexOf(name) < 0) names.push(name); });
      return names.map(function (name) { return { id: name, name: name, note: "", on: (item.categories || []).indexOf(name) >= 0 }; });
    }, function (ticked) {
      var names = ticked.map(function (one) { return one.name; });
      api.act({ what: "categorize", id: item.id, categories: names }).then(function () {
        item.categories = names;
        toast(names.length ? "Categories saved" : "Categories cleared");
        if (view.id === item.id) showTags(item);
      }).catch(function (error) { toast((error.unsure ? "" : "Not changed. ") + error.message); });
    }, pcBehind(NEEDS_MORE) ? {} : { extra: { label: "New category", run: function () {
      askName("New category", "It is made in Outlook, for every mail.", "", "Its name", function (name) {
        arrange({ what: "category", name: name }, "Category \u201c" + name + "\u201d made", function () { labelMail(item); });
      });
    } } });
  }

  // ---- your own folders and Outlook's categories: asked of the PC when first needed, then every ten minutes
  var OWN_FRESH = 600000;
  function askOwn() {
    if (pcBehind()) return Promise.resolve(own);
    if (own.known && Date.now() - own.asked < OWN_FRESH) return Promise.resolve(own);
    own.asked = Date.now();
    return api.folders().then(function (got) {
      own.folders = (got.folders || []).filter(function (one) { return one && isOwnFolder(one.id); });
      own.categories = (got.categories || []).map(String);
      own.known = true;
      drawOwnFolders();
      if (isOwnFolder(view.folder)) { $("folderName").textContent = folderName(view.folder); $("backLabel").textContent = folderName(view.folder); }
      return own;
    }, function (error) {
      own.asked = 0;
      throw error;
    });
  }
  function drawOwnFolders() {
    var box = $("ownFolders");
    box.textContent = "";
    $("ownNote").textContent = own.folders.length ? "Your folders" : "";
    $("ownNote").hidden = !own.folders.length;
    own.folders.forEach(function (one) {
      var link = el("a", "ownFolder");
      link.href = "#/" + one.id;
      link.setAttribute("data-folder", one.id);
      if (one.id === view.folder) link.setAttribute("aria-current", "page");
      var name = el("span", "ownName", one.name);
      var where = [one.file, one["in"]].filter(Boolean).join(" / ");
      if (where) name.appendChild(el("small", "", where));
      link.appendChild(name);
      if (one.unread) link.appendChild(el("span", "count", String(one.unread)));
      box.appendChild(link);
    });
  }

  // ---- a list to pick from: one place (a tap picks it) or several things (ticked, then Done)
  var picking = null;
  // more: {load: what to wait for first (default: your folders and categories), empty: what to say when there is nothing,
  //        extra: {label, run} a way on under the list, form: {value, hint, run} a name to type instead of a list}
  function pickFrom(title, note, several, options, done, more) {
    more = more || {};
    $("pickerTitle").textContent = title;
    $("pickerNote").textContent = note;
    $("pickerNote").hidden = !note;
    $("pickerDone").hidden = !several;
    $("pickerError").hidden = true;
    $("pickerForm").hidden = !more.form;
    $("pickerExtra").hidden = true;
    var list = $("pickerList");
    list.textContent = "";
    $("picker").hidden = false;
    picking = { several: several, done: done, shown: [], form: more.form || null };
    var mine = picking;
    if (more.form) {
      $("pickerText").value = more.form.value || "";
      $("pickerText").placeholder = more.form.hint || "";
      $("pickerText").focus();
      return;
    }
    list.appendChild(el("li", "peopleNote", "Asking your PC\u2026"));
    (more.load ? more.load() : askOwn()).then(function (got) {
      if (picking !== mine) return;
      list.textContent = "";
      mine.shown = options(got);
      if (!mine.shown.length) list.appendChild(el("li", "peopleNote", more.empty || (several ? "Outlook has no categories yet." : "There is no other folder to move it to.")));
      mine.shown.forEach(function (one) {
        var item = el("li");
        var button = el("button", "pickOne");
        button.type = "button";
        if (several) { button.setAttribute("role", "checkbox"); button.setAttribute("aria-checked", String(!!one.on)); }
        var name = el("span", "ownName", one.name);
        if (one.note) name.appendChild(el("small", "", one.note));
        button.appendChild(name);
        if (several) button.appendChild(el("span", "tickBox"));
        button.onclick = function () {
          if (!several) { closePicker(); done(one); return; }
          one.on = !one.on;
          button.setAttribute("aria-checked", String(one.on));
        };
        item.appendChild(button);
        list.appendChild(item);
      });
      if (more.extra) {
        $("pickerExtra").textContent = more.extra.label;
        $("pickerExtra").hidden = false;
        $("pickerExtra").onclick = function () { closePicker(); more.extra.run(); };
      }
    }).catch(function (error) {
      if (picking !== mine) return;
      list.textContent = "";
      list.appendChild(el("li", "peopleNote", error.message));
    });
  }
  // a name to type: for a new folder, a folder's new name, a new category
  function askName(title, note, value, hint, run) {
    pickFrom(title, note, false, null, null, { form: { value: value, hint: hint, run: run } });
  }
  function arrange(order, said, then) {
    toast("Asking your PC\u2026", "", null, true);
    return api.arrange(order).then(function (result) {
      own.known = false;   // the folders or the categories are not what they were
      toast(typeof said === "function" ? said(result) : said);
      if (then) then(result);
    }).catch(function (error) { toast((error.unsure ? "" : "Not changed. ") + error.message); });
  }

  function closePicker() {
    $("picker").hidden = true;
    picking = null;
  }

  // To Deleted Items, three seconds from now. A draft goes the same way; a mail in the Outbox does not leave.
  function removeMail(item, folder) {
    later("Deleting", function () {
      toast("Deleting\u2026", "", null, true);
      api.act({ what: "delete", id: item.id }).then(function () {
        toast(folder === "outbox" ? "Deleted. It will not be sent." : folder === "drafts" ? "Draft moved to Deleted" : "Moved to Deleted");
        view.rows = view.rows.filter(function (mail) { return mail.id !== item.id; });
        if (view.id === item.id) location.hash = "#/" + view.folder;
        else if (view.folder === folder) renderRows();
        refreshCounts();
        if (view.folder === folder) load(false, true);
      }).catch(function (error) { toast((error.unsure ? "" : "Not deleted. ") + error.message); });
    });
  }

  function closeItem() {
    view.id = "";
    view.item = null;
    document.body.classList.remove("reading");
    $("letter").hidden = true;
    $("readerHint").hidden = false;
    renderRows();
  }

  // ------------------------------------------------------------ people: names with their address, for To and Cc
  // They come from the PC (who wrote to you and who you wrote to, most often first) and from mail opened
  // here. Kept in this browser so they are there at once; unlinking the phone clears them.
  var PEOPLE_KEPT = 600, PEOPLE_ROWS = 6, PEOPLE_FRESH = 600000, PEOPLE_SOON = 60000;
  var people = peopleHere();
  var peopleAt = 0;
  var pick = { input: null, found: [], at: 0 };

  function peopleHere() {
    var kept = [];
    try { kept = JSON.parse(store.get("titip.people") || "[]"); } catch (error) { kept = []; }
    return Array.isArray(kept) ? kept.filter(function (person) { return person && person.address; }) : [];
  }
  function keepPeople() {
    if (DEMO) return;
    try { store.set("titip.people", JSON.stringify(people.slice(0, PEOPLE_KEPT))); } catch (error) { /* storage full */ }
  }
  function addPerson(name, address) {
    address = String(address || "").trim();
    if (!/^[^\s@<>;,]+@[^\s@<>;,]+$/.test(address)) return;
    var lower = address.toLowerCase();
    if (people.some(function (person) { return person.address.toLowerCase() === lower; })) return;
    people.push({ name: String(name || "").trim() || address, address: address });
    keepPeople();
  }
  function learnPeople() {
    if (Date.now() - peopleAt < PEOPLE_FRESH) return;
    peopleAt = Date.now();
    api.people().then(function (got) {
      var seen = {};
      var next = [];
      (got.people || []).concat(people).forEach(function (person) {   // the PC's order first: it knows who you write to most
        if (!person || !person.address) return;
        var lower = String(person.address).toLowerCase();
        if (seen[lower]) return;
        seen[lower] = true;
        next.push({ name: String(person.name || person.address), address: String(person.address) });
      });
      people = next;
      keepPeople();
      if (!(got.people || []).length) peopleAt = Date.now() - PEOPLE_FRESH + PEOPLE_SOON;   // the PC is still collecting
    }).catch(function () { peopleAt = Date.now() - PEOPLE_FRESH + PEOPLE_SOON; });
  }

  // ------------------------------------------------------------ who a mail goes to: one rounded name each
  // {name, address}. Picked from the suggestions it has both. Typed by hand it is an address, or only a
  // name (address ""), which Outlook looks up in its address book when it sends.
  var ADDRESS = /^[^\s@<>;,]+@[^\s@<>;,]+$/;
  var chosen = { toInput: [], ccInput: [], bccInput: [], withInput: [] };

  function handed(person) { return person.address || person.name; }   // what the PC is given for this one
  function typedPerson(text) {
    text = text.trim();
    var named = /^(.*)<([^<>]+)>$/.exec(text);
    if (named) return { name: named[1].trim().replace(/^["']+|["']+$/g, "") || named[2].trim(), address: named[2].trim() };
    return { name: text, address: ADDRESS.test(text) ? text : "" };
  }
  // "a@x.example; Priya Nair" -> people. A comma also ends a name that is typed here. In text that comes
  // from Outlook ("Okonkwo, Chidi; Hana Sato") it belongs to the name, unless every piece is an address.
  function peopleIn(text, typedHere) {
    var found = [];
    String(text || "").split(/[;\n]/).forEach(function (part) {
      var pieces = part.split(",");
      var each = typedHere || pieces.every(function (piece) { return piece.indexOf("@") >= 0; });
      (each ? pieces : [part]).forEach(function (piece) { if (piece.trim()) found.push(typedPerson(piece)); });
    });
    return found;
  }
  function fieldPeople(input) { return chosen[input.id].concat(peopleIn(input.value, true)); }   // the chips, and what is still being typed
  function drawChips(input) {
    var box = input.parentNode;
    Array.prototype.slice.call(box.querySelectorAll(".chip")).forEach(function (chip) { box.removeChild(chip); });
    chosen[input.id].forEach(function (person, index) {
      var chip = el("button", person.address ? "chip" : "chip asName");
      var cross = el("span", "chipX", "\u00d7");
      chip.type = "button";
      chip.title = person.address || "Outlook looks this name up when it sends";
      chip.setAttribute("aria-label", "Remove " + person.name);
      cross.setAttribute("aria-hidden", "true");
      chip.appendChild(el("span", "chipName", person.name));
      chip.appendChild(cross);
      chip.onmousedown = function (event) { event.preventDefault(); };   // the field keeps the keyboard
      chip.onclick = function () {
        chosen[input.id].splice(index, 1);
        drawChips(input);
        showComposeError("");
        rememberComposer();
        input.focus();
      };
      box.insertBefore(chip, input);
    });
    input.placeholder = chosen[input.id].length ? "" : input.getAttribute("data-hint") || "";
  }
  function addChips(input, list) {
    list.forEach(function (person) {
      var name = String(person.name || person.address || "").trim();
      var address = String(person.address || "").trim();
      var key = (address || name).toLowerCase();
      if (!key || chosen[input.id].some(function (had) { return handed(had).toLowerCase() === key; })) return;
      chosen[input.id].push({ name: name, address: address });
    });
    drawChips(input);
  }
  // start over: from what was being written (a list) or from a draft (the text Outlook shows in To)
  function setChips(input, value) {
    chosen[input.id] = [];
    input.value = "";
    addChips(input, Array.isArray(value) ? value.filter(Boolean) : peopleIn(value, false));
  }
  // What was typed becomes a chip: up to the last ; or , while typing, all of it when the field is left.
  function settle(input, all) {
    var text = input.value;
    var cut = all ? text.length : Math.max(text.lastIndexOf(";"), text.lastIndexOf(",")) + 1;
    if (!cut) return;
    addChips(input, peopleIn(text.slice(0, cut), true));
    input.value = text.slice(cut).replace(/^\s+/, "");
  }

  // ------------------------------------------------------------ suggestions while a name is typed
  function needleOf(input) { return input.value.trim().toLowerCase(); }
  function taken(input) {
    var have = {};
    chosen[input.id].forEach(function (person) { if (person.address) have[person.address.toLowerCase()] = true; });
    return have;
  }
  function findPeople(input) {
    var needle = needleOf(input);
    if (!needle) return [];
    var already = taken(input);
    var ranked = [];
    people.forEach(function (person, order) {
      var name = person.name.toLowerCase();
      var address = person.address.toLowerCase();
      if (already[address]) return;
      var rank = name.indexOf(needle) === 0 ? 0
        : (" " + name).indexOf(" " + needle) >= 0 ? 1
        : address.indexOf(needle) === 0 ? 2
        : name.indexOf(needle) >= 0 || address.indexOf(needle) >= 0 ? 3 : -1;
      if (rank >= 0) ranked.push([rank, order, person]);
    });
    ranked.sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; });
    return ranked.slice(0, PEOPLE_ROWS).map(function (entry) { return entry[2]; });
  }

  // Outlook's address book on the PC (the company directory): asked once the typing pauses. What it
  // answers is used for this visit and not kept on the phone; a name picked from it is.
  var BOOK_WAIT = 350, BOOK_SHORTEST = 2, BOOK_ROWS = 6, BOOK_AGAIN = 15000;
  var book = { known: {}, timer: 0 };   // typed text -> its matches, or "asking"
  function askBook(input, needle) {
    clearTimeout(book.timer);
    if (needle.length < BOOK_SHORTEST || book.known[needle]) return;
    book.timer = setTimeout(function () {
      if (document.activeElement !== input || needleOf(input) !== needle) return;
      book.known[needle] = "asking";
      showPeople(input, true);
      api.lookup(needle).then(function (got) {
        // an older Titip on the PC answers with its usual list, which is not a lookup
        return got && got.book && Array.isArray(got.people) ? got.people : [];
      }, function () {
        setTimeout(function () { delete book.known[needle]; }, BOOK_AGAIN);   // not reached: ask again later, not at once
        return [];
      }).then(function (found) {
        book.known[needle] = found.filter(function (person) {
          return person && ADDRESS.test(String(person.address || ""));
        }).map(function (person) {
          return { name: String(person.name || person.address), address: String(person.address) };
        });
        if (document.activeElement === input && needleOf(input) === needle) showPeople(input, true);
      });
    }, BOOK_WAIT);
  }

  function hidePeople() {
    $("people").hidden = true;
    pick.found = [];
    if (pick.input) pick.input.setAttribute("aria-expanded", "false");
  }
  function markPerson(index) {
    pick.at = index;
    Array.prototype.forEach.call($("people").querySelectorAll(".person"), function (button, position) {
      button.classList.toggle("on", position === index);
    });
  }
  // keep: the same typing, drawn again (the address book answered), so the marked row stays marked
  function showPeople(input, keep) {
    var list = $("people");
    var needle = needleOf(input);
    var found = findPeople(input);
    var known = book.known[needle];
    var fromBook = [];
    if (Array.isArray(known)) {
      var have = taken(input);
      found.forEach(function (person) { have[person.address.toLowerCase()] = true; });
      fromBook = known.filter(function (person) { return !have[person.address.toLowerCase()]; }).slice(0, BOOK_ROWS);
    }
    askBook(input, needle);
    list.textContent = "";
    if (!found.length && !fromBook.length && known !== "asking") { hidePeople(); return; }
    var marked = keep && pick.input === input ? pick.at : 0;
    pick.input = input;
    pick.found = found.concat(fromBook);
    function row(person) {
      var item = el("li");
      var button = el("button", "person");
      button.type = "button";
      button.appendChild(face(person.name));
      button.appendChild(el("span", "personName", person.name));
      button.appendChild(el("span", "personAddress", person.address));
      button.onmousedown = function (event) { event.preventDefault(); };   // the field keeps the keyboard
      button.onclick = function () { choosePerson(input, person); };
      item.appendChild(button);
      list.appendChild(item);
    }
    found.forEach(row);
    if (fromBook.length) list.appendChild(el("li", "peopleNote", "From the address book"));
    else if (known === "asking") list.appendChild(el("li", "peopleNote", "Searching the address book\u2026"));
    fromBook.forEach(row);
    var field = input.closest(".field");   // the list sits right under the field being typed in
    field.parentNode.insertBefore(list, field.nextSibling);
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    markPerson(marked < pick.found.length ? marked : 0);
  }
  function choosePerson(input, person) {
    input.value = "";
    addChips(input, [person]);
    addPerson(person.name, person.address);   // one from the address book is suggested at once next time
    hidePeople();
    showComposeError("");
    input.focus();
    rememberComposer();
  }
  ["toInput", "ccInput", "bccInput", "withInput"].forEach(function (id) {
    var input = $(id);
    input.parentNode.addEventListener("click", function (event) { if (event.target === input.parentNode) input.focus(); });
    input.addEventListener("input", function () { settle(input, false); showPeople(input); });
    input.addEventListener("focus", function () { showPeople(input); });
    input.addEventListener("blur", function () {
      setTimeout(function () {   // a tap on a suggestion or a chip comes first
        if (document.activeElement === input) return;
        settle(input, true);
        if (pick.input === input) hidePeople();
        rememberComposer();
      }, 250);
    });
    input.addEventListener("keydown", function (event) {
      var listed = !$("people").hidden && pick.input === input;
      if (listed && pick.found.length && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
        event.preventDefault();
        markPerson((pick.at + (event.key === "ArrowDown" ? 1 : pick.found.length - 1)) % pick.found.length);
      } else if (event.key === "Enter") {
        event.preventDefault();   // Enter takes the name, as in Outlook; it never sends
        if (listed && pick.found.length) { choosePerson(input, pick.found[pick.at]); return; }
        if (input.value.trim()) { settle(input, true); hidePeople(); rememberComposer(); return; }
        (id === "withInput" ? $("planNotes") : $("subjectField").hidden ? $("bodyInput") : $("subjectInput")).focus();
      } else if (event.key === "Escape" && listed) {
        event.stopPropagation();
        hidePeople();
      } else if (event.key === "Backspace" && !input.value && chosen[id].length) {
        chosen[id].pop();   // back over the last name
        drawChips(input);
        rememberComposer();
      }
    });
  });

  // ------------------------------------------------------------ composer
  var TITLES = { "new": "New message", reply: "Reply", replyAll: "Reply to all", forward: "Forward", draft: "Draft" };

  function composerNote(mode, source, listed) {
    var below = "Outlook adds the earlier messages below yours.";
    if (mode === "reply" && !listed) return "Goes to " + source.from + ". " + below;
    if (mode === "replyAll" && !listed) return "Goes to " + source.from + " and everyone else on the message. " + below;
    if (mode === "replyAll" && source.peopleCut) return "This mail went to more people than Titip lists. To answer all of them, use Outlook. " + below;
    if (mode === "reply" || mode === "replyAll") return below;
    if (mode === "forward") return "\u201c" + source.subject + "\u201d goes along below your text" + (source.attachments.length ? ", with its files." : ".");
    if (mode === "draft") return "Sent unchanged, it keeps its formatting. If you edit the text here, it goes as plain text.";
    return "";
  }

  // ---- files that go along: the ones picked here, and the ones the original brings (forward, draft)
  function filesSize() { return attached.reduce(function (sum, entry) { return sum + entry.size; }, 0); }
  function drawFiles() {
    var list = $("attached");
    list.textContent = "";
    function row(name, size, label, remove) {
      var item = el("li");
      var button = el("button", "fileChip");
      var cross = el("span", "chipX", "\u00d7");
      button.type = "button";
      button.setAttribute("aria-label", label + " " + name);
      cross.setAttribute("aria-hidden", "true");
      button.appendChild(icon(CLIP, "clip"));
      button.appendChild(el("span", "fileName", name));
      button.appendChild(el("small", "", fileSize(size)));
      button.appendChild(cross);
      button.onclick = function () { remove(); drawFiles(); showComposeError(""); rememberComposer(); };
      item.appendChild(button);
      list.appendChild(item);
    }
    carried.forEach(function (file) {
      if (!file.off) row(file.name, file.size, "Do not pass on", function () { file.off = true; });
    });
    attached.forEach(function (entry, index) {
      row(entry.name, entry.size, "Remove", function () { attached.splice(index, 1); });
    });
    list.hidden = !list.firstChild;
  }
  function addFiles(picked) {
    var refused = "";
    Array.prototype.forEach.call(picked, function (file) {
      if (attached.length >= MAX_FILES) { refused = "A mail from the phone can carry " + MAX_FILES + " files at most."; return; }
      if (filesSize() + file.size > api.FILES_BYTES) {
        refused = "\u201c" + file.name + "\u201d does not fit: the files of one mail can be " + Math.round(api.FILES_BYTES / 1000000) + " MB together" +
          (api === far ? " when they go through the relay." : ".");
        return;
      }
      attached.push({ file: file, name: file.name || "file", size: file.size });
    });
    drawFiles();
    showComposeError(refused);
    rememberComposer();
  }
  function randomId() {
    var bytes = crypto.getRandomValues(new Uint8Array(12));
    return Array.prototype.map.call(bytes, function (byte) { return (byte < 16 ? "0" : "") + byte.toString(16); }).join("");
  }
  function readPiece(blob) {   // as base64, the way a file travels inside a question to the PC
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () { resolve(String(reader.result).split(",")[1] || ""); };
      reader.onerror = function () { reject(new Error("This phone could not read the file. Attach it again.")); };
      reader.readAsDataURL(blob);
    });
  }
  // Hands the files to the PC piece by piece and resolves with the names the PC knows them by.
  function sendFiles(files, progress) {
    if (!files.length) return Promise.resolve([]);
    var pieces = [];
    var ids = files.map(function (entry) {
      var id = randomId();
      var count = Math.max(1, Math.ceil(entry.size / api.PIECE));
      for (var index = 0; index < count; index += 1) pieces.push({ entry: entry, id: id, index: index, last: index === count - 1 });
      return id;
    });
    var done = 0, next = 0, failed = null;
    function lane() {
      if (failed || next >= pieces.length) return Promise.resolve();
      var piece = pieces[next];
      next += 1;
      var from = piece.index * api.PIECE;
      return readPiece(piece.entry.file.slice(from, from + api.PIECE)).then(function (data) {
        return api.upload({ upload: piece.id, index: piece.index, last: piece.last, name: piece.entry.name, data: data });
      }).then(function () {
        done += 1;
        progress(done, pieces.length);
        return lane();
      }, function (error) { failed = failed || error; });
    }
    var lanes = [];
    for (var count = 0; count < api.LANES; count += 1) lanes.push(lane());
    return Promise.all(lanes).then(function () {
      if (failed) throw failed;
      return ids;
    });
  }

  function showBcc(on) {
    $("bccField").hidden = !on;
    $("bccToggle").hidden = on;
    $("bccToggle").setAttribute("aria-expanded", String(on));
  }

  // Either start from a message (source, or nothing for a new mail) or put back what was being written (saved).
  function openComposer(mode, source, saved) {
    var fields = saved || { to: "", cc: "", bcc: "", subject: "", body: "" };
    if (saved) {
      draft = saved.draft;
      attached = saved.files || [];
      carried = saved.carried || [];
    } else {
      var answer = mode === "reply" || mode === "replyAll";
      // who Outlook would address it to, for you to change. A Titip on the PC from before 0.9.0 does not say: then Outlook addresses it.
      var plan = answer && source.answer ? source.answer[mode] : null;
      draft = {
        mode: mode, id: source ? source.id : "", title: TITLES[mode], note: composerNote(mode, source, !!plan),
        who: source ? source.from : "", exact: !!plan || (mode === "forward" && !!source.answer),
        plan: plan ? { to: plan.to, cc: plan.cc } : null, important: false, signed: "", receipt: false, marks: false
      };
      if (plan) { fields.to = plan.to; fields.cc = plan.cc; }
      // Outlook's signature, as text you can change or take out: under a new mail, or the one for answers
      var sign = mode === "new" ? signature["new"] : mode === "draft" ? "" : signature.reply;
      if (sign) { draft.signed = sign; fields.body = "\n\n" + sign; }
      if (mode === "draft") {
        var listed = source.people || { to: source.to, cc: source.cc, bcc: source.bcc || "" };
        fields = { to: listed.to, cc: listed.cc, bcc: listed.bcc, subject: source.subject, body: source.body };
      }
      attached = [];
      carried = mode === "forward" || mode === "draft"
        ? source.attachments.map(function (file) { return { index: file.index, name: file.name, size: file.size, off: false }; }) : [];
    }
    var answering = draft.mode === "reply" || draft.mode === "replyAll";
    var loose = answering && !draft.exact;
    $("composeTitle").textContent = draft.title;
    $("replyNote").textContent = draft.note;
    $("replyNote").hidden = !draft.note;
    $("toField").hidden = loose;
    $("subjectField").hidden = draft.mode !== "new" && draft.mode !== "draft";
    $("ccLabel").textContent = loose ? "Add Cc" : "Cc";
    setChips($("toInput"), fields.to);
    setChips($("ccInput"), fields.cc);
    setChips($("bccInput"), fields.bcc || "");
    showBcc(chosen.bccInput.length > 0);
    $("subjectInput").value = fields.subject;
    $("bodyInput").value = fields.body;
    $("important").setAttribute("aria-pressed", String(!!draft.important));
    $("receipt").setAttribute("aria-pressed", String(!!draft.receipt));
    $("receipt").hidden = $("formatBar").hidden = pcBehind(NEEDS_MORE);
    $("formatNote").hidden = !draft.marks;
    drawFiles();
    showComposeError(saved && saved.lostFiles ? (saved.lostFiles === 1 ? "The file you attached was" : "The " + saved.lostFiles + " files you attached were") +
      " not kept when the app closed. Attach again." : "");
    hidePeople();
    $("composer").hidden = false;
    document.body.classList.add("composing");
    learnPeople();
    keepAwake();
    (loose || chosen.toInput.length ? $("bodyInput") : $("toInput")).focus();
    if (!saved && draft.signed) { try { $("bodyInput").setSelectionRange(0, 0); $("bodyInput").scrollTop = 0; } catch (error) { /* the text starts above the signature anyway */ } }
  }

  function composerValues() {
    return {
      draft: draft,
      to: fieldPeople($("toInput")),   // people, as {name, address}
      cc: fieldPeople($("ccInput")),
      bcc: fieldPeople($("bccInput")),
      subject: $("subjectInput").value.trim(),
      body: $("bodyInput").value,
      files: attached,
      carried: carried
    };
  }

  // Kept in the browser's storage, so it is still there after the app was closed. A file cannot be
  // kept there: the storage copy only says how many there were, this page keeps the files themselves.
  function rememberComposer() {
    if (!draft) return;
    written = composerValues();
    var copy = {};
    Object.keys(written).forEach(function (name) { copy[name] = written[name]; });
    copy.files = [];
    copy.lostFiles = written.files.length;
    store.set("titip.compose", JSON.stringify(copy));
  }

  // Away from home the PC looks for questions every 3 s when nobody asks and more than once a second
  // for two minutes after one. Asking while a mail is being written means Send finds the PC listening.
  var AWAKE_EVERY = 60000;
  function keepAwake() {
    if (api !== far || $("composer").hidden || document.hidden) return;
    api.state().catch(function () { /* only a nudge */ });
  }
  setInterval(keepAwake, AWAKE_EVERY);

  function closeComposer(forget) {
    hidePeople();
    $("composer").hidden = true;
    document.body.classList.remove("composing");
    if (forget) { store.drop("titip.compose"); written = null; }
    draft = null;
    $("write").focus();
  }

  function showComposeError(text) {
    $("composeError").textContent = text;
    $("composeError").hidden = !text;
  }

  // Everything that leaves or changes something waits three seconds behind Undo. One thing at a
  // time: what was already waiting is carried out when the next one starts.
  function later(label, run, undo) {
    if (pending) { clearTimeout(pending.timer); var before = pending; pending = null; before.run(); }
    var left = UNDO_SECONDS;
    pending = { run: run, timer: 0 };
    function tick() {
      if (!pending) return;
      if (left <= 0) { var job = pending; pending = null; job.run(); return; }
      toast(label + " in " + left + " s", "Undo", function () {
        clearTimeout(pending.timer);
        pending = null;
        hideToast();
        if (undo) undo();
      }, true);
      left -= 1;
      pending.timer = setTimeout(tick, 1000);
    }
    tick();
  }

  function submit(action) {
    var values = composerValues();
    var mode = draft.mode;
    var loose = (mode === "reply" || mode === "replyAll") && !draft.exact;
    if (action === "send") {
      if (!loose && !values.to.length && !values.cc.length && !values.bcc.length) { showComposeError("Add at least one recipient."); $("toInput").focus(); return; }
      var typed = values.body.trim();
      if ((!typed || typed === String(draft.signed || "").trim()) && mode !== "forward") { showComposeError("Write a message first."); $("bodyInput").focus(); return; }
    }
    var payload = {
      mode: mode, id: draft.id, action: action, to: values.to.map(handed), cc: values.cc.map(handed), bcc: values.bcc.map(handed),
      subject: values.subject, body: values.body
    };
    if (draft.exact) payload.recipients = "exact";   // what is listed here is who it goes to, nobody else
    // nobody added, nobody taken out: the PC then leaves the answer addressed as Outlook addressed it
    if (draft.exact && draft.plan && samePeople(values.to, draft.plan.to) && samePeople(values.cc, draft.plan.cc) && !values.bcc.length) payload.kept = true;
    if (draft.important) payload.importance = "high";
    if (draft.receipt) payload.receipt = true;
    if (draft.marks && /\*\*|__|\*\S|^\s*- /m.test(values.body)) payload.format = "marks";   // only when a mark is still in the text
    var dropped = values.carried.filter(function (file) { return file.off; });
    if (dropped.length) payload.drop = dropped.map(function (file) { return { index: file.index, name: file.name }; });
    rememberComposer();
    closeComposer(false);
    if (action === "draft") { deliver(payload, values); return; }
    later("Sending", function () { deliver(payload, values); }, function () { openComposer("", null, values); });
  }

  function samePeople(now, before) {
    function keys(list) { return list.map(function (person) { return handed(person).toLowerCase(); }).sort().join("\n"); }
    return keys(now) === keys(before || []);
  }

  function sentText(payload, values) {
    var names = values.to.concat(values.cc, values.bcc).map(function (person) { return person.name; });
    if (names.length) return "Sent to " + names[0] + (names.length > 1 ? " and " + (names.length - 1) + " more" : "");
    var who = values.draft.who;   // an answer Outlook addressed itself
    if (payload.mode === "replyAll") return who ? "Sent to " + who + " and everyone else on the message" : "Reply sent to everyone";
    return who ? "Sent to " + who : payload.mode === "reply" ? "Reply sent" : "Sent";
  }

  function deliver(payload, values) {
    var saving = payload.action === "draft";
    toast(saving ? "Saving\u2026" : "Sending\u2026", "", null, true);
    sendFiles(values.files || [], function (done, all) {
      toast((values.files.length === 1 ? "Handing the file to your PC: " : "Handing the files to your PC: ") + Math.round(done * 100 / all) + "%", "", null, true);
    }).then(function (names) {
      if (names.length) payload.files = names;
      if (names.length) toast(saving ? "Saving\u2026" : "Sending\u2026", "", null, true);
      return api.send(payload);
    }).then(function (result) {
      store.drop("titip.compose");
      written = null;
      toast(result.saved ? "Saved to Drafts" : sentText(payload, values));
      refreshCounts();
      if (view.folder !== "inbox") load(false, true);
      if (payload.mode === "draft") location.hash = "#/" + view.folder;
    }).catch(function (error) {
      hideToast();
      openComposer("", null, values);
      showComposeError((error.unsure ? "" : saving ? "Not saved. " : "Not sent. ") + error.message + " Your text is still here.");
    });
  }

  // ------------------------------------------------------------ routing
  function route() {
    var parts = location.hash.replace(/^#\/?/, "").split("/");
    var folder = FOLDERS[parts[0]] || isOwnFolder(parts[0]) ? parts[0] : "inbox";
    var id = parts.slice(1).join("/");
    var folderChanged = folder !== view.folder || !view.rows.length && !view.loading;
    if (folder !== view.folder) {
      view.query = "";
      $("q").value = "";
      if (!$("searchBox").hidden) { $("searchBox").hidden = true; $("searchToggle").setAttribute("aria-expanded", "false"); }
      showSaved({});
    }
    view.folder = folder;
    Array.prototype.forEach.call(document.querySelectorAll(".tabs a, .folderMenu a"), function (tab) {
      if (tab.getAttribute("data-folder") === folder) tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    });
    showFolders(false);
    document.title = folderName(folder) + " \u00b7 Titip";
    $("folderName").textContent = folderName(folder);
    if (isOwnFolder(folder) && !own.known) askOwn().catch(function () { /* the list of mail says what is wrong */ });
    $("write").lastChild.textContent = folder === "calendar" ? "New entry" : "New mail";
    $("searchToggle").hidden = folder === "calendar";   // the calendar is looked through by day, not searched
    $("refresh").setAttribute("aria-label", folder === "calendar" ? "Read the calendar again" : "Check for new mail");
    $("more").textContent = folder === "calendar" ? "Show later days" : "Show older mail";
    showOutbox();
    showCalendarBar();
    if (folderChanged) { view.rows = []; view.days = 0; load(false); }
    if (id) (folder === "calendar" ? openEvent : openItem)(id);
    else closeItem();
  }

  // every folder, under the folder's name in the bar; the ones used most stay at the thumb
  function showFolders(on) {
    $("folderMenu").hidden = !on;
    $("folderPick").setAttribute("aria-expanded", String(on));
    if (!on) return;
    $("folderDo").hidden = pcBehind(NEEDS_MORE);
    $("folderRename").hidden = $("folderGone").hidden = !isOwnFolder(view.folder);
    drawOwnFolders();
    if (!own.known && !pcBehind()) { $("ownNote").textContent = "Asking your PC for your folders\u2026"; $("ownNote").hidden = false; }
    askOwn().catch(function () { $("ownNote").textContent = "Your own folders could not be read just now."; $("ownNote").hidden = false; });
  }

  // ------------------------------------------------------------ a calendar entry: make one, change one of your own, take it out
  function two(number) { return (number < 10 ? "0" : "") + number; }
  function hourOf(date) { return two(date.getHours()) + ":" + two(date.getMinutes()); }
  function showPlanError(text) {
    $("planError").textContent = text;
    $("planError").hidden = !text;
  }
  function showPlanner(on) {
    $("planner").hidden = !on;
    document.body.classList.toggle("composing", on);
    if (!on) hidePeople();
  }
  // scope: of an entry that repeats, "one" (this time only) or "series"
  function openPlanner(event, scope) {
    var begins, ends;
    if (event) {
      begins = event.allDay ? dayStart(event.day) : new Date(event.start);
      ends = event.allDay ? dayStart(event.lastDay) : new Date(event.end);
    } else {
      begins = new Date(Date.now() + 3600000);   // the next full hour, half an hour long, as Outlook starts one
      begins.setMinutes(0, 0, 0);
      ends = new Date(begins.getTime() + 1800000);
    }
    var more = !pcBehind(NEEDS_MORE);
    var series = scope === "series";
    planned = {
      id: event ? event.id : "", days: event && event.allDay ? Math.round((ends - begins) / 86400000) + 1 : 1, meeting: !!(event && event.meeting),
      scope: scope || "", occurrence: scope === "one" ? event.start : "", people: more && event && scope !== "one"
    };
    $("planTitle").textContent = !event ? "New entry" : scope === "one" ? "Change this one" : series ? "Change the series" : "Change entry";
    $("planSubject").value = event ? event.subject : "";
    $("planDay").value = dayOf(begins);
    $("planFrom").value = event && event.allDay ? "09:00" : hourOf(begins);
    $("planTo").value = event && event.allDay ? "09:30" : hourOf(ends);
    $("planAllDay").checked = !!(event && event.allDay);
    // when a series takes place is changed in Outlook: here what it is called, where, who, and its text
    ["planDay", "planFrom", "planTo", "planAllDay"].forEach(function (id) { $(id).disabled = series; });
    $("planHours").hidden = $("planAllDay").checked;
    $("planWhere").value = event ? event.location || "" : "";
    $("planNotes").value = event ? (event.body || "").trim() : "";
    $("planRemindField").hidden = !more;
    var remind = !event ? "" : event.reminder === null || event.reminder === undefined ? "none" : String(event.reminder);
    if (!Array.prototype.some.call($("planRemind").options, function (option) { return option.value === remind; })) remind = "";
    $("planRemind").value = remind;
    planned.remind = remind;
    $("planRepeatField").hidden = !!event || !more;
    $("planRepeat").value = "";
    $("planTimes").value = "";
    $("planTimes").hidden = $("planTimesWord").hidden = true;
    // who is invited: listed for an entry of your own and changed here; of one time of a series it stays as the series has it
    $("withField").hidden = !!event && !planned.people;
    setChips($("withInput"), planned.people ? event.invitedPeople || [] : []);
    $("planNote").textContent = series ? "Changes every time it takes place. When it takes place is changed in Outlook."
      : planned.meeting ? "The people invited are told about the change." : "";
    $("planNote").hidden = !$("planNote").textContent;
    $("planFoot").textContent = event ? "Changed in Outlook's calendar on your PC." : "Goes into Outlook's calendar on your PC. With people in it, Outlook sends them an invitation.";
    showPlanError("");
    showPlanner(true);
    learnPeople();
    $("planSubject").focus();
  }
  function submitPlan() {
    var subject = $("planSubject").value.trim();
    var day = $("planDay").value;
    if (!subject) { showPlanError("Give it a title first."); $("planSubject").focus(); return; }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) { showPlanError("Pick a day."); $("planDay").focus(); return; }
    var people = $("withField").hidden ? [] : fieldPeople($("withInput"));
    var order = { what: "save", id: planned.id, subject: subject, location: $("planWhere").value.trim(), body: $("planNotes").value, "with": people.map(handed) };
    if (planned.scope) order.scope = planned.scope;
    if (planned.occurrence) order.occurrence = planned.occurrence;
    if (planned.id && planned.people) order.withExact = true;   // the people listed are everyone who is invited
    if (!$("planRemindField").hidden && $("planRemind").value !== planned.remind) order.reminder = $("planRemind").value === "none" ? "none" : Number($("planRemind").value);
    if (!planned.id && $("planRepeat").value) {
      var times = Number($("planTimes").value) || 0;
      if (times === 1 || times < 0 || times > 500) { showPlanError("It can repeat 2 to 500 times, or without an end."); $("planTimes").focus(); return; }
      order.repeat = { every: $("planRepeat").value, times: times };
    }
    if ($("planAllDay").checked) {
      order.allDay = true;
      order.day = day;
      order.days = planned.days;
    } else {
      var from = $("planFrom").value, to = $("planTo").value;
      if (!/^\d{2}:\d{2}/.test(from) || !/^\d{2}:\d{2}/.test(to)) { showPlanError("Say from when to when, or tick All day."); $("planFrom").focus(); return; }
      var first = dayStart(day);
      var begins = new Date(first.getFullYear(), first.getMonth(), first.getDate(), Number(from.slice(0, 2)), Number(from.slice(3, 5)));
      var ends = new Date(first.getFullYear(), first.getMonth(), first.getDate(), Number(to.slice(0, 2)), Number(to.slice(3, 5)));
      if (ends < begins) ends = new Date(ends.getTime() + 86400000);   // until after midnight
      order.start = begins.toISOString();
      order.end = ends.toISOString();
    }
    var job = planned;
    var telling = people.length > 0 || job.meeting;
    showPlanner(false);
    later(telling ? (job.id ? "Sending the change" : "Sending the invitation") : "Saving", function () {
      toast(telling ? "Sending\u2026" : "Saving\u2026", "", null, true);
      api.plan(order).then(function (result) {
        planned = null;
        toast(result.told ? (job.id || !people.length ? "Saved. The people invited are told." : "Invitation sent to " + people[0].name + (people.length > 1 ? " and " + (people.length - 1) + " more" : ""))
          : "Saved to the calendar");
        if (view.folder !== "calendar") return;
        if (view.id) location.hash = "#/calendar";
        view.rows = [];
        load(false);
      }).catch(function (error) {
        planned = job;
        showPlanner(true);
        showPlanError((error.unsure ? "" : "Not saved. ") + error.message);
      });
    }, function () { planned = job; showPlanner(true); });
  }
  // an entry that repeats: this one time, or all of them?
  function whichOnes(title, event, run) {
    pickFrom(title, "This entry repeats.", false, function () {
      return [{ id: "one", name: "Only this one", note: meetingWhen(event).replace(/ \u00b7 repeats$/, "") }, { id: "series", name: "The whole series", note: "Every time it takes place" }];
    }, function (picked) { run(picked.id); }, { load: function () { return Promise.resolve(); } });
  }
  function removeEntry(event, scope) {
    var order = { what: "remove", id: event.id };
    if (scope) order.scope = scope;
    if (scope === "one") order.occurrence = event.start;
    later(event.meeting ? "Cancelling the meeting" : "Deleting", function () {
      toast(event.meeting ? "Cancelling\u2026" : "Deleting\u2026", "", null, true);
      api.plan(order).then(function (result) {
        toast(result.told ? "Cancelled. The people invited are told." : "Deleted from the calendar");
        if (view.folder !== "calendar") return;
        if (view.id) location.hash = "#/calendar";
        view.rows = [];
        load(false);
      }).catch(function (error) { toast((error.unsure ? "" : "Not deleted. ") + error.message); });
    });
  }

  // ---- other calendars: more of your own, and the ones people share with you. To read.
  function showCalendarBar() {
    $("calBar").hidden = view.folder !== "calendar" || pcBehind(NEEDS_MORE);
    $("calName").textContent = shown.which ? shown.name : "Your calendar";
  }
  function pickCalendar() {
    pickFrom("Calendars", "", false, function (got) {
      var list = [{ id: "", name: "Your calendar", note: "" }];
      (got.calendars || []).forEach(function (one) { list.push({ id: String(one.id), name: String(one.name), note: String(one.group || "") }); });
      return list;
    }, function (picked) {
      shown = { which: picked.id, name: picked.name };
      showCalendarBar();
      if (view.id) location.hash = "#/calendar";
      view.rows = [];
      view.days = 0;
      load(false);
    }, { load: function () { return api.calendars(); } });
  }

  // ---- rules: Outlook's own, each switched on or off
  function openRules() {
    var before = {};
    pickFrom("Rules", "Outlook's rules for new mail, in the order they run. Tick the ones that are on.", true, function (got) {
      return (got.rules || []).map(function (rule) {
        before[rule.name] = !!rule.on;
        return { id: String(rule.name), name: String(rule.name), note: rule.sent ? "For mail you send" : "", on: !!rule.on };
      });
    }, function () {
      var changed = picking_last.filter(function (one) { return one.on !== before[one.name]; });
      if (!changed.length) return;
      toast("Asking your PC\u2026", "", null, true);
      changed.reduce(function (chain, one) {
        return chain.then(function () { return api.arrange({ what: "rule", name: one.name, on: one.on }); });
      }, Promise.resolve()).then(function () {
        toast(changed.length === 1 ? "\u201c" + changed[0].name + "\u201d is " + (changed[0].on ? "on" : "off") : changed.length + " rules changed");
      }).catch(function (error) { toast((error.unsure ? "" : "Not changed. ") + error.message); });
    }, { load: function () { return api.rules(); }, empty: "Outlook has no rules. Make one from a mail: More, Always move mail from this sender." });
  }
  var picking_last = [];   // every row of the list that was just closed with Done, ticked or not
  function ruleFor(item, folder) {
    pickFrom("Mail from " + item.from + " goes to", "Outlook makes a rule: from now on, new mail from this sender is moved there.", false, function () {
      var places = own.folders.map(function (one) { return { id: one.id, name: one.name, note: [one.file, one["in"]].filter(Boolean).join(" / ") }; });
      places.push({ id: "junk", name: "Junk", note: "" });
      return places;
    }, function (place) {
      later("Making the rule", function () {
        arrange({ what: "ruleFrom", id: item.id, to: place.id }, function (result) { return "Rule made: " + result.name; });
      });
    });
  }

  // ---- folders of your own: a new one, a new name, an empty one taken out
  function newFolder() {
    var inside = isOwnFolder(view.folder) ? view.folder : view.folder === "inbox" ? "inbox" : "";
    var where = inside === "inbox" ? "In the Inbox." : inside ? "In " + folderName(inside) + "." : "Next to the Inbox.";
    askName("New folder", where, "", "Its name", function (name) {
      arrange({ what: "folder", "in": inside, name: name }, "Folder \u201c" + name + "\u201d made", function () { askOwn().catch(function () { /* listed the next time */ }); });
    });
  }
  function renameFolder() {
    var folder = view.folder;
    askName("Rename the folder", "", folderName(folder), "Its new name", function (name) {
      arrange({ what: "rename", id: folder, name: name }, "Renamed to \u201c" + name + "\u201d", function () {
        askOwn().catch(function () { /* the old name stays on screen until then */ });
      });
    });
  }
  function removeFolder() {
    var folder = view.folder, name = folderName(folder);
    later("Taking out \u201c" + name + "\u201d", function () {
      arrange({ what: "unfolder", id: folder }, "Folder \u201c" + name + "\u201d taken out", function () {
        own.folders = own.folders.filter(function (one) { return one.id !== folder; });
        if (view.folder === folder) location.hash = "#/inbox";
      });
    });
  }

  // ---- formatting: the marks Outlook's own autoformat knows, put around what is selected
  function mark(sign) {
    var box = $("bodyInput");
    var from = box.selectionStart, to = box.selectionEnd, text = box.value;
    if (sign === "- ") {
      var first = text.lastIndexOf("\n", from - 1) + 1;
      var lines = text.slice(first, to).split("\n").map(function (line) { return /^\s*- /.test(line) ? line : "- " + line; }).join("\n");
      box.value = text.slice(0, first) + lines + text.slice(to);
      box.setSelectionRange(first + lines.length, first + lines.length);
    } else {
      var inside = text.slice(from, to);
      box.value = text.slice(0, from) + sign + inside + sign + text.slice(to);
      // nothing selected: the cursor sits between the marks, ready for the words
      box.setSelectionRange(from + sign.length, to + sign.length);
    }
    if (draft) draft.marks = true;
    $("formatNote").hidden = false;
    box.focus();
    rememberComposer();
  }

  // ------------------------------------------------------------ wiring
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    var meta = document.querySelectorAll('meta[name="theme-color"]');
    Array.prototype.forEach.call(meta, function (tag) { tag.setAttribute("content", theme === "dark" ? "#201f1c" : "#2f5b8c"); });
  }
  var dark = window.matchMedia("(prefers-color-scheme: dark)");
  applyTheme(store.get("titip.theme") || (dark.matches ? "dark" : "light"));
  $("theme").onclick = function () {
    var next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    store.set("titip.theme", next);
    applyTheme(next);
  };

  $("refresh").onclick = function () { load(false); refreshCounts(); };
  $("more").onclick = function () { load(true); };
  $("back").onclick = function () { location.hash = "#/" + view.folder; };
  $("write").onclick = function () {
    if (view.folder === "calendar" && !pcBehind()) openPlanner(null);
    else openComposer("new", null);
  };
  $("planCancel").onclick = function () { planned = null; showPlanner(false); $("write").focus(); };
  $("planForm").onsubmit = function (event) { event.preventDefault(); submitPlan(); };
  $("planForm").oninput = function () { showPlanError(""); };
  $("planForm").addEventListener("keydown", function (event) {   // Enter in a line of the form does not save it
    if (event.key === "Enter" && event.target.tagName === "INPUT" && event.target.id !== "withInput") event.preventDefault();
  });
  $("planAllDay").onchange = function () { $("planHours").hidden = this.checked; };
  $("pickerCancel").onclick = closePicker;
  $("pickerDone").onclick = function () {
    var job = picking;
    closePicker();
    if (!job) return;
    picking_last = job.shown;
    job.done(job.shown.filter(function (one) { return one.on; }));
  };
  $("pickerForm").onsubmit = function (event) {
    event.preventDefault();
    var job = picking;
    var name = $("pickerText").value.replace(/\s+/g, " ").trim();
    if (!job || !job.form) return;
    if (!name || /[\\\/;,]/.test(name)) { $("pickerError").textContent = "Give it a name, without \\ / ; or a comma."; $("pickerError").hidden = false; return; }
    closePicker();
    job.form.run(name);
  };
  $("calPick").onclick = pickCalendar;
  $("folderNew").onclick = function () { showFolders(false); newFolder(); };
  $("folderRename").onclick = function () { showFolders(false); renameFolder(); };
  $("folderGone").onclick = function () { showFolders(false); removeFolder(); };
  $("rulesOpen").onclick = function () { showFolders(false); openRules(); };
  Array.prototype.forEach.call(document.querySelectorAll("#formatBar button"), function (button) {
    button.onmousedown = function (event) { event.preventDefault(); };   // the text keeps its selection
    button.onclick = function () { mark(button.getAttribute("data-mark")); };
  });
  $("receipt").onclick = function () {
    if (!draft) return;
    draft.receipt = !draft.receipt;
    this.setAttribute("aria-pressed", String(draft.receipt));
    rememberComposer();
  };
  $("planRepeat").onchange = function () { $("planTimes").hidden = $("planTimesWord").hidden = !this.value; };
  $("picker").onclick = function (event) { if (event.target === $("picker")) closePicker(); };
  $("moreToggle").onclick = function () { showMore($("moreMenu").hidden); };
  $("important").onclick = function () {
    if (!draft) return;
    draft.important = !draft.important;
    this.setAttribute("aria-pressed", String(draft.important));
    rememberComposer();
  };
  $("deep").onchange = function () {
    view.deep = this.checked;
    if (view.query) { view.rows = []; load(false); }
  };
  $("folderPick").onclick = function () { showFolders($("folderMenu").hidden); };
  $("folderMenu").onclick = function (event) {   // a tap on the folder you are in closes the list too
    if (event.target.closest("a")) showFolders(false);
  };
  document.addEventListener("click", function (event) {
    if (!$("folderMenu").hidden && !event.target.closest("#folderMenu, #folderPick")) showFolders(false);
    if (!$("moreMenu").hidden && !event.target.closest("#moreMenu, #moreToggle")) showMore(false);
  });
  $("attach").onclick = function () {
    if (pcBehind()) { showComposeError(behindText()); return; }
    $("fileInput").click();
  };
  $("fileInput").onchange = function () { addFiles(this.files); this.value = ""; };
  $("bccToggle").onclick = function () { showBcc(true); $("bccInput").focus(); };
  $("viewToggle").onclick = function () { view.formatted = !view.formatted; showBody(); };
  $("discard").onclick = function () { closeComposer(true); };
  $("saveDraft").onclick = function () { submit("draft"); };
  $("composeForm").onsubmit = function (event) { event.preventDefault(); submit("send"); };
  $("composeForm").oninput = function () { showComposeError(""); rememberComposer(); };

  $("searchToggle").onclick = function () {
    var box = $("searchBox");
    box.hidden = !box.hidden;
    this.setAttribute("aria-expanded", String(!box.hidden));
    if (!box.hidden) $("q").focus();
    else if (view.query) { view.query = ""; $("q").value = ""; view.rows = []; load(false); }
  };
  var searchTimer = 0;
  $("q").oninput = function () {
    clearTimeout(searchTimer);
    var value = this.value.trim();
    searchTimer = setTimeout(function () {
      if (value === view.query) return;
      view.query = value;
      view.rows = [];
      load(false);
    }, 450);
  };
  $("searchBox").onsubmit = function (event) { event.preventDefault(); };

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    if (!$("picker").hidden) { closePicker(); return; }
    if (!$("moreMenu").hidden) { showMore(false); $("moreToggle").focus(); return; }
    if (!$("folderMenu").hidden) { showFolders(false); $("folderPick").focus(); return; }
    if (!$("planner").hidden) { planned = null; showPlanner(false); return; }
    if (!$("composer").hidden) { rememberComposer(); closeComposer(false); toast("Kept on this phone. Tap New mail to continue.", "Open", reopenSaved); }
  });

  function reopenSaved() {
    var saved = written;   // this page still has it, files and all
    if (!saved) { try { saved = JSON.parse(store.get("titip.compose") || "null"); } catch (error) { saved = null; } }
    hideToast();
    if (saved && saved.draft) openComposer("", null, saved);
  }

  // Back on screen (the icon was tapped again, the phone found a network): a list that failed is asked
  // for again at once, and one older than half a minute is refreshed without a tap.
  var AGED = 30000;
  var PAGE_OLD = 12 * 3600 * 1000;   // a Home Screen icon is not reloaded by the phone: after this long it reloads itself
  function wake() {
    if (document.hidden) return;
    if (Date.now() - LOADED > PAGE_OLD && $("composer").hidden && !pending && !view.id) { location.reload(); return; }
    if (view.stale) { retryStale(); return; }
    if (!view.query && firstPage() && $("composer").hidden && Date.now() - view.at > AGED) load(false, true);
  }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      if (pending) { clearTimeout(pending.timer); var job = pending; pending = null; job.run(); }
    } else {
      wake();
    }
  });
  window.addEventListener("online", wake);
  window.addEventListener("pageshow", function (event) { if (event.persisted) wake(); });

  setInterval(function () {
    if (!document.hidden && !view.query && firstPage() && $("composer").hidden) load(false, true);
  }, REFRESH_MS);

  window.addEventListener("hashchange", route);
  $("build").textContent = "Titip " + VERSION;
  showMe(DEMO ? "" : store.get("titip.me"));
  route();
  refreshCounts();
  if (store.get("titip.compose")) toast("You have an unfinished message.", "Open", reopenSaved, true);
}());
