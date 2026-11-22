/* Titip phone app. No framework, no build step. Mail text only ever reaches the page through
   textContent or a script-less sandboxed frame, never innerHTML. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var VERSION = "0.15.1";   // shown under the list: a Home Screen icon can keep running an old page for days
  var LOADED = Date.now();
  var FOLDERS = {
    inbox: "Inbox", sent: "Sent", drafts: "Drafts", outbox: "Outbox", deleted: "Deleted", junk: "Junk", calendar: "Calendar",
    tasks: "Tasks", notes: "Notes", contacts: "Contacts"
  };
  var PAGES = { tasks: true, notes: true, contacts: true };   // Outlook's other pages: lists of things that are not mail
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
  var view = { folder: "inbox", id: "", rows: [], more: false, query: "", deep: false, show: "", loading: false, item: null, formatted: true, at: 0, stale: false, days: 0, from: "" };
  var own = { folders: [], categories: [], colours: null, asked: 0, known: false };   // your own folders and Outlook's categories, as the PC last listed them
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
    list: function (folder, offset, query, deep, show) {
      return request("/api/mail?folder=" + folder + "&limit=" + PAGE + "&offset=" + offset + "&q=" + encodeURIComponent(query) + (deep ? "&deep=1" : "") + (show ? "&show=" + show : ""));
    },
    person: function (who) { return request("/api/person?q=" + encodeURIComponent(who)); },
    get: function (id) { return request("/api/item?id=" + encodeURIComponent(id)); },
    send: function (payload) { return request("/api/send", payload); },
    people: function () { return request("/api/people"); },
    lookup: function (typed) { return request("/api/people?q=" + encodeURIComponent(typed)); },
    fileUrl: function (id, index) { return "/api/attachment?id=" + encodeURIComponent(id) + "&index=" + index; },
    calendar: function (offset, days, which, fresh) {
      return request("/api/calendar?offset=" + offset + "&days=" + days + (which ? "&which=" + encodeURIComponent(which) : "") + (fresh ? "&fresh=1" : ""));
    },
    invite: function (id) { return request("/api/invite?id=" + encodeURIComponent(id)); },
    calendars: function () { return request("/api/calendars"); },
    rules: function () { return request("/api/rules"); },
    arrange: function (order) { return request("/api/arrange", order); },
    away: function () { return request("/api/away"); },
    push: function (endpoint) { return request("/api/push?endpoint=" + encodeURIComponent(endpoint || "")); },
    pushSet: function (order) { return request("/api/pushset", order); },
    event: function (id) { return request("/api/event?id=" + encodeURIComponent(id)); },
    act: function (order) { return request("/api/act", order); },
    plan: function (order) { return request("/api/plan", order); },
    folders: function () { return request("/api/folders"); },
    signature: function () { return request("/api/signature"); },
    things: function (kind, query, done) { return request("/api/things?kind=" + kind + "&q=" + encodeURIComponent(query || "") + (done ? "&done=1" : "")); },
    thing: function (kind, id) { return request("/api/thing?kind=" + kind + "&id=" + encodeURIComponent(id)); },
    keep: function (order) { return request("/api/keep", order); },
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
      // tasks, notes and contacts, each with an id and the day or time it is given relative to now
      data.pages = { tasks: [], notes: [], contacts: [] };
      (data.tasks || []).forEach(function (task, index) {
        var due = task.dueInDays === undefined ? "" : dayOf(new Date(midnight.getFullYear(), midnight.getMonth(), midnight.getDate() + task.dueInDays));
        data.pages.tasks.push({ id: "demo.t" + index, kind: "tasks", title: task.title, due: due, done: !!task.done, important: !!task.important, body: task.body || "" });
      });
      (data.notes || []).forEach(function (note, index) {
        data.pages.notes.push({ id: "demo.o" + index, kind: "notes", body: note.body, time: new Date(now - (note.minutesAgo || 0) * 60000).toISOString() });
      });
      (data.contacts || []).forEach(function (contact, index) {
        data.pages.contacts.push({
          id: "demo.k" + index, kind: "contacts", title: contact.name, company: contact.company || "", job: contact.job || "",
          email: contact.email || "", mobile: contact.mobile || "", work: contact.phone || "", body: contact.body || ""
        });
      });
      data.away = false;
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
  // when an entry that repeats takes place, the first twelve times at most: what Outlook works out from the pattern
  function demoTimes(first, repeat) {
    if (!repeat) return [first];
    var wanted = Math.min(repeat.times || 12, 12), found = [], every = repeat.every, interval = repeat.interval || 1;
    function at(year, month, day) { return new Date(year, month, day, first.getHours(), first.getMinutes()); }
    function week(date) { return Math.floor((Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) / 86400000 + 3) / 7); }   // weeks begin on Monday
    if (every === "day" || every === "week" || every === "weekday") {
      var days = every === "weekday" ? [1, 2, 3, 4, 5] : repeat.days && repeat.days.length > 1 ? repeat.days.concat([first.getDay()]) : null;
      for (var ahead = 0; found.length < wanted && ahead < 4400; ahead += 1) {
        var moment = at(first.getFullYear(), first.getMonth(), first.getDate() + ahead);
        var fits = every === "day" ? ahead % interval === 0
          : days ? days.indexOf(moment.getDay()) >= 0 && (week(moment) - week(first)) % interval === 0 : ahead % (7 * interval) === 0;
        if (fits) found.push(moment);
      }
      return found;
    }
    var last = function (year, month) { return new Date(year, month + 1, 0).getDate(); };
    var nth = repeat.last && first.getDate() + 7 > last(first.getFullYear(), first.getMonth()) ? 5 : Math.floor((first.getDate() - 1) / 7) + 1;
    for (var step = 0; found.length < wanted && step < 600; step += 1) {
      var month = first.getMonth() + step * (every === "year" ? 12 : interval), day = first.getDate();
      var days_in = last(first.getFullYear(), month);
      if (every === "monthNth") {
        var same = [];
        for (var number = 1; number <= days_in; number += 1) if (at(first.getFullYear(), month, number).getDay() === first.getDay()) same.push(number);
        day = nth >= 5 || nth > same.length ? same[same.length - 1] : same[nth - 1];
      } else if (day > days_in) continue;   // the 31st: a month without one has none
      found.push(at(first.getFullYear(), month, day));
    }
    return found;
  }
  function demoRow(one) {   // what the list shows of a task, a note or a contact
    if (one.kind === "notes") {
      var text = String(one.body || "").trim(), cut = text.indexOf("\n");
      return { id: one.id, kind: "notes", title: (cut < 0 ? text : text.slice(0, cut)).trim() || "(empty note)",
        preview: cut < 0 ? "" : text.slice(cut + 1).replace(/\s+/g, " ").trim().slice(0, 180), time: one.time };
    }
    if (one.kind === "tasks") return { id: one.id, kind: "tasks", title: one.title, due: one.due, start: one.start || "", done: !!one.done, important: !!one.important };
    return { id: one.id, kind: "contacts", title: one.title, company: one.company, email: one.email, phone: one.mobile || one.work };
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
        return { ok: true, demo: true, account: data.me.address, name: data.me.name, counts: counts, away: !!data.away };
      });
    },
    list: function (folder, offset, query, deep, show) {
      return demoLoad().then(function (data) {
        var needle = query.toLowerCase().trim();
        var rows = data.mail.filter(function (mail) {
          return mail.folder === folder && (!show || (show === "unread" ? mail.unread : mail.flagged)) &&
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
          id: "demo.n" + Date.now(), folder: payload.action === "draft" ? "drafts" : payload.later ? "outbox" : "sent",
          from: data.me.name, fromAddress: data.me.address, to: to, cc: shown(payload.cc), bcc: shown(payload.bcc),
          subject: subject || "(no subject)", body: payload.body, time: new Date().toISOString(), attachments: files,
          important: payload.importance === "high", receipt: !!payload.receipt
        });
        return payload.action === "draft" ? { ok: true, saved: true } : payload.later ? { ok: true, sent: true, later: true } : { ok: true, sent: true };
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
        if (order.what === "category") {
          data.categories.push(order.name);
          data.colours = data.colours || {};
          data.colours[order.name] = order.colour || "";
          return { ok: true, name: order.name };
        }
        if (order.what === "away") { data.away = !!order.on; return { ok: true, away: data.away }; }
        if (order.what === "awayText") {
          data.awayText = order.text;
          if (typeof order.outside === "string") data.awayOutside = order.outside;
          return { ok: true, text: order.text, outside: data.awayOutside || "" };
        }
        if (order.what === "empty") {
          var inside = data.mail.filter(function (mail) { return mail.folder === order.folder; });
          inside.forEach(function (mail) {
            if (order.folder === "deleted") data.mail.splice(data.mail.indexOf(mail), 1);
            else mail.folder = "deleted";
          });
          return { ok: true, emptied: inside.length, left: 0 };
        }
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
        copy.optionalPeople = demoPeople(data, event.optional || "").filter(function (person) { return person.name !== data.me.name; });
        return copy;
      });
    },
    act: function (order) {
      return demoLoad().then(function (data) {
        var mail = data.mail.filter(function (candidate) { return candidate.id === order.id; })[0];
        if (order.what !== "respond" && !mail) throw new Error("That message was moved or deleted. Refresh the list.");
        if (order.what === "delete") { mail.folder = "deleted"; return { ok: true, deleted: true }; }
        if (order.what === "erase") { data.mail.splice(data.mail.indexOf(mail), 1); return { ok: true, erased: true }; }
        if (order.what === "move") { mail.folder = order.to; return { ok: true, moved: true }; }
        if (order.what === "read" || order.what === "unread") { mail.unread = order.what === "unread"; return { ok: true, unread: mail.unread }; }
        if (order.what === "flag" || order.what === "unflag") { mail.flagged = order.what === "flag"; return { ok: true, flagged: mail.flagged }; }
        if (order.what === "categorize") { mail.categories = order.categories.slice(); return { ok: true, categories: mail.categories }; }
        var event = mail ? data.events[mail.invite] : (data.events || []).filter(function (candidate) {   // of a series: the one time named
          return candidate.id === order.id && (!order.occurrence || candidate.start.slice(0, 16) === String(order.occurrence).slice(0, 16));
        })[0];
        if (!event) throw new Error("That is not an invitation.");
        event.response = { accept: "accepted", tentative: "tentative", decline: "declined" }[order.answer];
        return { ok: true, responded: order.answer, told: true };
      });
    },
    folders: function () {
      return demoLoad().then(function (data) {
        return { folders: (data.folders || []).map(function (folder) { return { id: folder.id, name: folder.name, "in": folder["in"], file: "", unread: 0 }; }),
          categories: (data.categories || []).slice(), colours: data.colours || {}, cut: false };
      });
    },
    signature: function () { return demoLoad().then(function (data) { return data.signature || { "new": "", reply: "" }; }); },
    away: function () {
      return demoLoad().then(function (data) {
        return { on: !!data.away, text: data.awayText === undefined ? "I am out of the office and will answer when I am back." : data.awayText,
          outside: data.awayOutside || "" };
      });
    },
    things: function (kind, query, done) {
      return demoLoad().then(function (data) {
        var needle = String(query || "").toLowerCase().trim();
        var rows = data.pages[kind].filter(function (one) { return kind !== "tasks" || !!one.done === !!done; }).map(demoRow).filter(function (row) {
          return !needle || Object.keys(row).map(function (name) { return String(row[name]); }).join(" ").toLowerCase().indexOf(needle) >= 0;
        });
        rows.sort(function (a, b) {
          if (kind === "tasks") return (a.due || "9999") < (b.due || "9999") ? -1 : 1;
          if (kind === "notes") return a.time < b.time ? 1 : -1;
          return a.title.toLowerCase() < b.title.toLowerCase() ? -1 : 1;
        });
        return { kind: kind, things: rows, more: false };
      });
    },
    thing: function (kind, id) {
      return demoLoad().then(function (data) {
        var one = data.pages[kind].filter(function (candidate) { return candidate.id === id; })[0];
        if (!one) throw new Error("That is not what it was when the list was read. Refresh the list.");
        var full = demoRow(one);
        full.body = one.body || "";
        if (kind === "tasks") full.remind = one.remind || "";
        if (kind === "contacts") {
          full.emails = one.email ? [one.email] : [];
          full.job = one.job || "";
          full.mobile = one.mobile || "";
          full.work = one.work || "";
          full.address = "";
          full.phones = [["Mobile", one.mobile], ["Work", one.work]].filter(function (pair) { return pair[1]; }).map(function (pair) { return { label: pair[0], number: pair[1] }; });
        }
        return full;
      });
    },
    keep: function (order) {
      return demoLoad().then(function (data) {
        var list = data.pages[order.kind];
        var one = order.id ? list.filter(function (candidate) { return candidate.id === order.id; })[0] : null;
        if (order.id && !one) throw new Error("That is not what it was when the list was read. Refresh the list.");
        if (order.what === "remove") { list.splice(list.indexOf(one), 1); return { ok: true, removed: true }; }
        if (order.what === "done" || order.what === "undone") { one.done = order.what === "done"; return { ok: true, done: one.done }; }
        if (!one) { one = { id: "demo.p" + Date.now(), kind: order.kind, done: false }; list.push(one); }
        one.body = order.body || "";
        if (order.kind === "notes") one.time = new Date().toISOString();
        else if (order.kind === "tasks") {
          if (order.start && !order.due) throw new Error("A task that starts on a day is due on a day too. Give it a due day.");
          if (order.start && order.start > order.due) throw new Error("It is due before it starts.");
          one.title = order.title; one.due = order.due || ""; one.important = !!order.important;
          if (order.start !== undefined) one.start = order.start || "";
          if (order.remind !== undefined) one.remind = order.remind || "";
        }
        else { one.title = order.title; one.company = order.company || ""; one.job = order.job || ""; one.email = order.email || ""; one.mobile = order.mobile || ""; one.work = order.phone || ""; }
        return { ok: true, saved: true };
      });
    },
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
        var people = order["with"] || [], optional = order.optional || [];
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
          if (order.busy) event.busy = order.busy;
          if (order["private"] !== undefined && order.scope !== "one") event["private"] = !!order["private"];
          return event;
        }
        if (!rows.length) {
          var id = "demo.n" + Date.now();
          var moments = demoTimes(first, order.repeat);   // twelve of them at most, in the demo
          for (var number = 0; number < moments.length; number += 1) {
            data.events.push(timed(written({
              id: id, organizer: data.me.name, meeting: people.length + optional.length > 0, canceled: false, invited: false, mine: true,
              response: people.length + optional.length ? "organizer" : "", recurring: !!order.repeat, busy: "busy", required: people.join("; "),
              optional: optional.join("; "), reminder: 15
            }), moments[number].getTime() - first.getTime()));
          }
          return { ok: true, saved: true, told: people.length + optional.length > 0 };
        }
        var series = rows[0].recurring && order.scope === "series";
        rows.forEach(function (event) {
          written(event);
          if (!series) timed(event, 0);
          if ((order.withExact || order.optionalExact) && order.scope !== "one") {
            if (order.withExact) event.required = people.join("; ");
            if (order.optionalExact) event.optional = optional.join("; ");
            event.meeting = !!(event.required || event.optional);
            event.response = event.meeting ? "organizer" : "";
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
    person: function (who) {
      return demoLoad().then(function (data) {
        var wanted = String(who).toLowerCase();
        var kept = data.pages.contacts.filter(function (one) { return one.title.toLowerCase() === wanted || (one.email || "").toLowerCase() === wanted; })[0];
        if (kept) {
          return { found: true, known: true, name: kept.title, address: kept.email, job: kept.job, department: "", company: kept.company, office: "",
            phones: [["Mobile", kept.mobile], ["Work", kept.work]].filter(function (pair) { return pair[1]; }).map(function (pair) { return { label: pair[0], number: pair[1] }; }) };
        }
        var listed = (data.book || []).concat(data.people || []).filter(function (one) { return one.name.toLowerCase() === wanted || one.address.toLowerCase() === wanted; })[0];
        if (!listed && wanted.indexOf("@") < 0) return { found: false };
        return { found: true, known: false, name: listed ? listed.name : who, address: listed ? listed.address : who, job: "", department: "", company: "", office: "", phones: [] };
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
  function farRequest(route, params, sending, since, tries, quiet) {
    var started = since || Date.now();
    return relay.ask(route, params, sending ? FAR_SEND_WAIT : FAR_READ_WAIT).then(function (answer) {
      if (answer.status === 503 && answer.data.busy && !sending && !quiet && Date.now() - started < BUSY_WAIT) {
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
    list: function (folder, offset, query, deep, show) { return farRequest("mail", { folder: folder, limit: PAGE, offset: offset, q: query, deep: deep ? "1" : "", show: show || "" }); },
    person: function (who) { return farRequest("person", { q: who }); },
    get: function (id) { return farRequest("item", { id: id }); },
    send: function (payload) { return farRequest("send", payload, true); },
    people: function () { return farRequest("people", {}); },
    lookup: function (typed) { return farRequest("people", { q: typed }); },
    fileUrl: function () { return ""; },
    file: function (id, index) { return farRequest("attachment", { id: id, index: index }); },
    calendar: function (offset, days, which, fresh) { return farRequest("calendar", { offset: offset, days: days, which: which || "", fresh: fresh ? "1" : "" }); },
    invite: function (id) { return farRequest("invite", { id: id }); },
    calendars: function () { return farRequest("calendars", {}); },
    rules: function () { return farRequest("rules", {}); },
    arrange: function (order) { return farRequest("arrange", order, true); },
    away: function () { return farRequest("away", {}); },
    push: function (endpoint) { return farRequest("push", { endpoint: endpoint || "" }); },
    pushSet: function (order) { return farRequest("pushset", order, true); },
    event: function (id) { return farRequest("event", { id: id }); },
    act: function (order) {   // like a send: never made twice. Marking as read happens behind a tap: tried once, quietly
      return order.what === "read" ? farRequest("act", order, false, 0, FAR_TRIES, true) : farRequest("act", order, true);
    },
    plan: function (order) { return farRequest("plan", order, true); },
    folders: function () { return farRequest("folders", {}); },
    signature: function () { return farRequest("signature", {}); },
    things: function (kind, query, done) { return farRequest("things", { kind: kind, q: query || "", done: done ? "1" : "" }); },
    thing: function (kind, id) { return farRequest("thing", { kind: kind, id: id }); },
    keep: function (order) { return farRequest("keep", order, true); },
    upload: function (piece) { return farRequest("upload", piece); },   // the same piece twice does no harm
    PIECE: 96000, LANES: 3, FILES_BYTES: 5000000   // a question through the relay holds 200,000 characters once sealed
  };

  var DEMO = FORCED_DEMO || (HOSTED && !ticket);
  var api = DEMO ? demo : ticket ? far : pc;

  // The public page is updated before the PC has moved to the same version, and a PC can stay behind.
  // What 0.9.0 added is then said to be on its way, instead of asked of a PC that does not know the question.
  var NEEDS = "0.9.0", NEEDS_MORE = "0.10.0", NEEDS_PAGES = "0.11.0", NEEDS_FULL = "0.12.0", NEEDS_OPEN = "0.13.0", NEEDS_PUSH = "0.14.0", NEEDS_REAL = "0.14.1", NEEDS_LAST = "0.15.0";   // what came with a version asks for that one
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
    if (PAGES[view.folder]) { renderThings(); return; }
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
      if (picked) {
        row.setAttribute("aria-pressed", String(!!picked[mail.id]));
        if (picked[mail.id]) subject.insertBefore(el("span", "sr", "Selected. "), subject.firstChild);
      }
      row.onclick = function () {
        if (Date.now() - swiped < 500) return;   // the finger that pushed the row aside, or held it, did not tap it
        if (picked) {   // several are being picked: a tap ticks this one, or takes the tick off
          if (picked[mail.id]) delete picked[mail.id];
          else picked[mail.id] = true;
          showPick();
          renderRows();
          return;
        }
        location.hash = "#/" + view.folder + "/" + mail.id;
      };
      item.appendChild(row);
      list.appendChild(item);
    });
    $("more").hidden = !view.more;
    if (!view.rows.length) {
      if (view.show && !view.query) showState(view.show === "unread" ? "Nothing unread" : "Nothing flagged", "No " + view.show + " mail in " + folderName(view.folder) + ".");
      else if (view.query) showState("No match", "Nothing in " + folderName(view.folder) + " matches \u201c" + view.query + "\u201d" + (view.deep ? ", not in the text of the mail either." : "."));
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
      if (!view.rows.length) {
        $("rows").textContent = "";
        $("more").hidden = true;
        showState("Reading your calendar\u2026", "The first time can take Outlook a minute or two. After that it is kept for ten minutes.");
      }
    }
    if (!silent) $("refresh").classList.add("busy");
    var anew = !!view.anew && !append;   // the Refresh button: read it again now, not from what the PC kept
    view.anew = false;
    return api.calendar(offset, CAL_DAYS, shown.which, anew).then(function (page) {
      if (mine !== loads || view.folder !== "calendar") return;
      view.rows = append ? view.rows.concat(page.events) : page.events;
      view.days = offset + CAL_DAYS;
      if (!append) { view.from = page.from; view.at = Date.now(); view.stale = false; clearTimeout(staleTimer); }
      if (!append && !shown.which) { remind.events = page.events; remind.at = Date.now(); }
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
    if (PAGES[view.folder] && pcBehind(NEEDS_PAGES)) {
      $("rows").textContent = "";
      $("more").hidden = true;
      showState("Not yet", behindText(NEEDS_PAGES));
      return Promise.resolve();
    }
    if (PAGES[view.folder]) return loadThings(silent);
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
    var show = pcBehind(NEEDS_FULL) ? "" : view.show;
    var narrowed = query || show;   // a search, or only what is unread or flagged: asked of Outlook each time, and not kept
    var offset = append ? view.rows.length : 0;
    if (!append && !silent) {
      showState("");
      showSaved({});
      if (!narrowed && !view.rows.length) {
        var kept = keptList(folder);
        if (kept) { view.rows = kept.mail; view.more = false; view.at = kept.at; renderRows(); }
      }
      // rows already on screen stay there while the fresh list is on its way; the arrows turn meanwhile
      if (narrowed || !view.rows.length) { skeleton(); $("more").hidden = true; }
    }
    if (!silent) $("refresh").classList.add("busy");
    if (query && !append) showState("Searching\u2026", view.deep ? "Looking through the text of every mail. This can take a while." : "Looking through every mail file. This can take a few seconds.");
    return api.list(folder, offset, query, !!query && view.deep, show).then(function (page) {
      if (mine !== loads) return;
      view.rows = append ? view.rows.concat(page.mail) : page.mail;
      view.more = page.more;
      if (!append) {
        view.stale = false;
        clearTimeout(staleTimer);
        if (!narrowed) { view.at = Date.now(); keepList(folder, page); }
      }
      renderRows();
      if (!append) showSaved(page);
      if (!append && !query) learnPeople();
    }).catch(function (error) {
      if (mine !== loads) return;
      if (append) { toast(error.message); return; }
      if (silent && !view.stale) return;   // a quiet refresh that failed: the next one tries again
      if (!silent) {
        if (!narrowed && view.rows.length) {
          // the list from before stays readable; say that it is old, and why
          var note = $("savedNote");
          note.textContent = error.message + " This is the list from " + whenText(view.at) + ".";
          note.hidden = false;
        } else {
          $("rows").textContent = "";
          showState("Can't show your mail", error.message, true);
        }
      }
      view.stale = !narrowed;
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
      if (document.hidden || view.query || view.show || !firstPage() || !$("composer").hidden) return;
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
      showAway(state.away);
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
    $("answerNote").hidden = !asked || pcBehind(NEEDS_FULL);
    $("answerNote").value = "";
    $("suggestOpen").hidden = !asked || pcBehind(NEEDS_FULL) || !event.start;
    $("suggest").hidden = true;
    $("suggestOpen").onclick = function () {
      $("suggest").hidden = !$("suggest").hidden;
      if ($("suggest").hidden) return;
      $("suggestDay").value = dayOf(new Date(event.start));
      $("suggestFrom").value = hourOf(new Date(event.start));
      $("suggestTo").value = hourOf(new Date(event.end));
    };
    $("suggestSend").onclick = function () {
      var day = $("suggestDay").value, from = $("suggestFrom").value, to = $("suggestTo").value;
      if (!/^\d{4}-\d{2}-\d{2}$/.test(day) || !/^\d{2}:\d{2}/.test(from) || !/^\d{2}:\d{2}/.test(to)) { toast("Pick a day and the hours first."); return; }
      var first = dayStart(day);
      var begins = new Date(first.getFullYear(), first.getMonth(), first.getDate(), Number(from.slice(0, 2)), Number(from.slice(3, 5)));
      var ends = new Date(first.getFullYear(), first.getMonth(), first.getDate(), Number(to.slice(0, 2)), Number(to.slice(3, 5)));
      // in words, in the answer the organizer reads: Outlook's object model has no call for a proposal of its own kind
      var words = "I suggest another time: " + begins.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long", year: "numeric" }) +
        ", " + clock(begins.toISOString()) + " to " + clock(ends.toISOString()) + ".";
      $("answerNote").value = words + ($("answerNote").value.trim() ? " " + $("answerNote").value.trim() : "");
      // and in the fields Outlook's own proposal uses, so the organizer's Outlook can take the time over (0.14.1; an older PC sends the words)
      respond(id, "tentative", event, "", { start: begins.toISOString(), end: ends.toISOString() });
    };
    Array.prototype.forEach.call($("answers").querySelectorAll("button"), function (button) {
      var answer = button.getAttribute("data-answer");
      var given = { accept: "accepted", tentative: "tentative", decline: "declined" }[answer] === event.response;
      button.setAttribute("aria-pressed", String(given));
      button.onclick = function () { respond(id, answer, event); };
    });
  }
  // `another` is another time ({start, end}) that goes with a tentative answer
  function respond(id, answer, event, scope, another) {
    // a meeting that repeats, answered from the calendar: this one time, or all of them?
    if (!scope && view.folder === "calendar" && event.recurring && event.start && !pcBehind(NEEDS_OPEN)) {
      whichOnes("Answer", event, function (which) { respond(id, answer, event, which, another); });
      return;
    }
    var showing = view.id;
    var order = { what: "respond", id: id, answer: answer };
    if (another && answer === "tentative" && !pcBehind(NEEDS_REAL)) order.propose = another;
    if (scope === "one") order.occurrence = event.start;
    var note = $("answerNote").hidden ? "" : $("answerNote").value.trim();
    if (note) order.note = note;   // a few words the organizer reads with the answer
    later(ANSWERING[answer], function () {
      toast(ANSWERING[answer] + "\u2026", "", null, true);
      api.act(order).then(function (result) {
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
    $("letter").classList.remove("bare");
    $("subject").textContent = event.subject;
    paint($("face"), event.organizer || "?", "big");
    $("from").textContent = event.organizer || "";
    setWho(event.organizer ? { name: event.organizer, address: "" } : null);
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
    // your own entry; one that repeats when the PC can name one time of it
    var changeable = !!full && full.mine && !pcBehind() && (!full.recurring || !pcBehind(NEEDS_MORE));
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
    $("letter").classList.remove("bare");
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
      setWho(item.draft ? null : { name: item.from, address: item.fromAddress || "" });
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
      // what an invitation is for is looked up now, not for every mail the PC reads ahead: it can take Outlook a while
      if (item.inviteAsk && !item.invite) {
        api.invite(item.id).then(function (got) {
          if (!got.invite) return;
          item.invite = got.invite;
          if (view.id === item.id) showInvite(item.invite, item.id);
        }).catch(function () { /* the mail itself is readable without it */ });
      }
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
      $("remove").hidden = pcBehind() || (view.folder === "deleted" && pcBehind(NEEDS_OPEN));
      $("remove").textContent = view.folder === "drafts" ? "Discard draft" : view.folder === "deleted" ? "Delete for good" : "Delete";
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
  // ---- one person: job, department, office and numbers, from the company directory or your contacts
  var who = null;
  function setWho(person) {
    who = person && !pcBehind(NEEDS_FULL) ? person : null;
    $("from").classList.toggle("tap", !!who);
    // a name is a button only while there is something to show for it
    if (who) { $("from").setAttribute("role", "button"); $("from").setAttribute("tabindex", "0"); }
    else { $("from").removeAttribute("role"); $("from").removeAttribute("tabindex"); }
  }
  function showPerson() {
    if (!who) return;
    var asked = who, card = null;
    pickFrom(asked.name || asked.address, asked.address && asked.address !== asked.name ? asked.address : "", false, function (got) {
      card = got;
      if (!got.found) return [];
      var facts = [got.job, got.department, got.company, got.office].filter(Boolean).join(" \u00b7 ");
      $("pickerNote").textContent = [got.address, facts].filter(Boolean).join(" \u00b7 ");
      $("pickerNote").hidden = !$("pickerNote").textContent;
      var rows = got.address ? [{ id: "mail", name: "Write a mail", note: got.address }] : [];
      (got.phones || []).forEach(function (phone) { rows.push({ id: "tel", name: phone.number, note: "Call \u00b7 " + phone.label, number: phone.number }); });
      return rows;
    }, function (picked) {
      if (picked.id === "tel") { location.href = "tel:" + picked.number.replace(/[^0-9+]/g, ""); return; }
      writeTo(card.name || asked.name, card.address);
    }, { load: function () { return api.person(asked.address || asked.name); },
      empty: "Outlook's address book and your contacts say nothing more about this person." });
  }
  $("from").onclick = showPerson;
  $("from").onkeydown = function (event) { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); showPerson(); } };
  // ---- a row pushed aside with a finger: to the left it is deleted (behind Undo), to the right marked read or unread
  var swipe = null, swiped = 0, held = 0, holding = false, SWIPE_FAR = 96;
  function toggleRead(mail, folder) {
    var unread = mail.unread && opened.indexOf(mail.id) < 0;
    api.act({ what: unread ? "read" : "unread", id: mail.id }).then(function (result) {
      opened = opened.filter(function (id) { return id !== mail.id; });
      store.set("titip.opened", JSON.stringify(opened));
      mail.unread = result && result.left === "receipt" ? true : !unread;
      toast(result && result.left === "receipt" ? "It asks for a read receipt, so it stays unread in Outlook until you open it there."
        : unread ? "Marked as read" : "Marked as unread");
      if (view.folder === folder) renderRows();
      refreshCounts();
    }).catch(function (error) { toast((error.unsure ? "" : "Not changed. ") + error.message); });
  }
  $("rows").addEventListener("touchstart", function (event) {
    var row = event.target.closest ? event.target.closest(".row") : null;
    swipe = null;
    if (!row || event.touches.length !== 1 || PAGES[view.folder] || view.folder === "calendar" || pcBehind()) return;
    var mail = view.rows[Array.prototype.indexOf.call($("rows").querySelectorAll(".row"), row)];
    clearTimeout(held);
    if (!mail || picked) return;   // while mails are being picked a row is tapped, not pushed
    held = setTimeout(function () {
      if (!swipe || swipe.on) return;
      swipe = null;
      holding = true;   // the finger is still down: when it lifts, that is not a tap on the row
      picked = {};
      picked[mail.id] = true;
      showPick();
      // the rows stay the ones they are (the finger is on one of them): they are ticked in place
      Array.prototype.forEach.call($("rows").querySelectorAll(".row"), function (one) { one.setAttribute("aria-pressed", String(one === row)); });
    }, 550);
    swipe = {
      row: row, mail: mail, x: event.touches[0].clientX, y: event.touches[0].clientY, dx: 0, on: false,
      remove: view.folder !== "deleted", read: view.folder === "inbox" || isOwnFolder(view.folder)
    };
  }, { passive: true });
  $("rows").addEventListener("touchmove", function (event) {
    if (!swipe) return;
    var dx = event.touches[0].clientX - swipe.x, dy = event.touches[0].clientY - swipe.y;
    if (!swipe.on) {
      if (Math.abs(dx) > 8 || Math.abs(dy) > 8) clearTimeout(held);   // it moves: not a finger held still
      if (Math.abs(dy) > 10 && Math.abs(dy) >= Math.abs(dx)) { swipe = null; return; }   // the list is being scrolled
      if (Math.abs(dx) < 14) return;
      swipe.on = true;
    }
    if ((dx < 0 && !swipe.remove) || (dx > 0 && !swipe.read)) dx = 0;
    swipe.dx = dx;
    var item = swipe.row.parentNode;
    item.classList.toggle("swiping", dx !== 0);
    item.classList.toggle("far", Math.abs(dx) >= SWIPE_FAR);
    if (dx) {
      item.setAttribute("data-side", dx < 0 ? "delete" : "read");
      item.setAttribute("data-swipe", dx < 0 ? (view.folder === "drafts" ? "Discard" : "Delete")
        : swipe.mail.unread && opened.indexOf(swipe.mail.id) < 0 ? "Mark as read" : "Mark as unread");
    } else {   // this way nothing can be done here: nothing is promised behind the row
      item.removeAttribute("data-side");
      item.removeAttribute("data-swipe");
    }
    swipe.row.style.transform = dx ? "translateX(" + dx + "px)" : "";
    if (event.cancelable) event.preventDefault();   // the list stays where it is while a row is pushed aside
  }, { passive: false });
  function endSwipe(event) {
    clearTimeout(held);
    if (holding) { holding = false; swiped = Date.now(); }
    var done = swipe;
    swipe = null;
    if (!done || !done.on) return;
    swiped = Date.now();
    var item = done.row.parentNode;
    done.row.style.transform = "";
    item.classList.remove("swiping", "far");
    item.removeAttribute("data-swipe");
    item.removeAttribute("data-side");
    if (event.type === "touchcancel" || Math.abs(done.dx) < SWIPE_FAR) return;   // not far enough: nothing happens
    if (done.dx < 0) removeMail(done.mail, view.folder);
    else toggleRead(done.mail, view.folder);
  }
  $("rows").addEventListener("touchend", endSwipe);
  $("rows").addEventListener("touchcancel", endSwipe);
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
      return names.map(function (name) {
        return { id: name, name: name, note: "", on: (item.categories || []).indexOf(name) >= 0, dot: own.colours ? String(own.colours[name] || "") : undefined };
      });
    }, function (ticked) {
      var names = ticked.map(function (one) { return one.name; });
      api.act({ what: "categorize", id: item.id, categories: names }).then(function () {
        item.categories = names;
        toast(names.length ? "Categories saved" : "Categories cleared");
        if (view.id === item.id) showTags(item);
      }).catch(function (error) { toast((error.unsure ? "" : "Not changed. ") + error.message); });
    }, pcBehind(NEEDS_MORE) ? {} : { extra: { label: "New category", run: function () {
      askName("New category", "It is made in Outlook, for every mail.", "", "Its name", function (name) {
        var make = function (colour) {
          var order = { what: "category", name: name };
          if (colour !== undefined) order.colour = colour;
          arrange(order, "Category \u201c" + name + "\u201d made", function () { labelMail(item); });
        };
        if (pcBehind(NEEDS_LAST)) { make(); return; }
        pickFrom("Colour", "For \u201c" + name + "\u201d, in Outlook and here.", false, function () {
          return COLOURS.map(function (colour) { return { id: colour[0], name: colour[1], note: "", dot: colour[0] }; });
        }, function (chosen) { make(chosen.id); }, { load: function () { return Promise.resolve(); } });
      });
    } } });
  }

  // the colours a category can be given here: the ones Outlook has that are told apart at a glance
  var COLOURS = [["", "No colour"], ["red", "Red"], ["orange", "Orange"], ["yellow", "Yellow"], ["green", "Green"], ["teal", "Teal"],
    ["blue", "Blue"], ["purple", "Purple"], ["gray", "Gray"]];
  // ---- your own folders and Outlook's categories: asked of the PC when first needed, then every ten minutes
  var OWN_FRESH = 600000;
  function askOwn() {
    if (pcBehind()) return Promise.resolve(own);
    if (own.known && Date.now() - own.asked < OWN_FRESH) return Promise.resolve(own);
    own.asked = Date.now();
    return api.folders().then(function (got) {
      own.folders = (got.folders || []).filter(function (one) { return one && isOwnFolder(one.id); });
      own.categories = (got.categories || []).map(String);
      own.colours = got.colours && typeof got.colours === "object" ? got.colours : null;   // a PC before 0.15.0 does not say
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
        if (one.dot !== undefined) {   // a category's colour; a ring for one that has none
          var dot = el("span", "hue");
          dot.setAttribute("data-colour", one.dot);
          button.appendChild(dot);
        }
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
    var good = folder === "deleted";   // deleted before: this time it is gone for good, and the countdown says so
    later(good ? "Deleting for good" : "Deleting", function () {
      toast("Deleting\u2026", "", null, true);
      api.act({ what: good ? "erase" : "delete", id: item.id }).then(function () {
        toast(good ? "Deleted for good" : folder === "outbox" ? "Deleted. It will not be sent." : folder === "drafts" ? "Draft moved to Deleted" : "Moved to Deleted");
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
  var chosen = { toInput: [], ccInput: [], bccInput: [], withInput: [], optInput: [] };

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
  ["toInput", "ccInput", "bccInput", "withInput", "optInput"].forEach(function (id) {
    var input = $(id);
    input.parentNode.addEventListener("click", function (event) { if (event.target === input.parentNode) input.focus(); });
    input.addEventListener("input", function () { settle(input, false); showPeople(input); });
    input.addEventListener("focus", function () { input.parentNode.classList.add("typing"); showPeople(input); });
    input.addEventListener("blur", function () {
      setTimeout(function () {   // a tap on a suggestion or a chip comes first
        if (document.activeElement === input) return;
        input.parentNode.classList.remove("typing");   // only now: the line the empty box took goes, and what is below moves up
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
        (id === "withInput" || id === "optInput" ? $("planNotes") : $("subjectField").hidden ? $("bodyInput") : $("subjectInput")).focus();
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
        plan: plan ? { to: plan.to, cc: plan.cc } : null, important: false, signed: "", receipt: false, marks: false, later: false
      };
      if (plan) { fields.to = plan.to; fields.cc = plan.cc; }
      if (source && mode !== "draft" && mode !== "new") {
        // the subject as Outlook will write it, to change if you like. Untouched, Outlook's own stays (it may say AW: or TR:).
        var prefix = mode === "forward" ? "FW: " : "RE: ";
        fields.subject = source.subject.slice(0, 3).toUpperCase() === prefix.trim() ? source.subject : prefix + source.subject;
        draft.subjectWas = fields.subject;
      }
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
    $("subjectField").hidden = false;
    $("ccLabel").textContent = loose ? "Add Cc" : "Cc";
    setChips($("toInput"), fields.to);
    setChips($("ccInput"), fields.cc);
    setChips($("bccInput"), fields.bcc || "");
    showBcc(chosen.bccInput.length > 0);
    $("subjectInput").value = fields.subject;
    $("bodyInput").value = fields.body;
    $("important").setAttribute("aria-pressed", String(!!draft.important));
    $("receipt").setAttribute("aria-pressed", String(!!draft.receipt));
    $("laterFlip").hidden = pcBehind(NEEDS_PAGES);
    showLater(!!draft.later);
    $("receipt").hidden = $("formatBar").hidden = pcBehind(NEEDS_MORE);
    $("picture").hidden = pcBehind(NEEDS_OPEN);
    Array.prototype.forEach.call(document.querySelectorAll("#formatBar [data-needs]"), function (key) { key.hidden = pcBehind(NEEDS_OPEN); });
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
    if (mode !== "new" && mode !== "draft" && values.subject === draft.subjectWas) payload.subject = "";   // not changed: Outlook writes it
    if (draft.exact) payload.recipients = "exact";   // what is listed here is who it goes to, nobody else
    // nobody added, nobody taken out: the PC then leaves the answer addressed as Outlook addressed it
    if (draft.exact && draft.plan && samePeople(values.to, draft.plan.to) && samePeople(values.cc, draft.plan.cc) && !values.bcc.length) payload.kept = true;
    if (draft.important) payload.importance = "high";
    if (draft.receipt) payload.receipt = true;
    if (draft.later && action === "send") {
      var leaves = laterMoment();
      if (!leaves || leaves.getTime() < Date.now() + 60000) { showComposeError("Pick a day and a time that are still to come, or press Send later again to send it now."); $("laterDay").focus(); return; }
      payload.later = leaves.toISOString();
    }
    if (draft.marks && /\*\*|__|==|\*\S|\]\(https?:|^\s*- |^# /m.test(values.body)) payload.format = "marks";   // only when a mark is still in the text
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
      toast(result.saved ? "Saved to Drafts" : result.later ? "Leaves " + dayName(new Date(payload.later)) + ", " + clock(payload.later) + ". It waits in the Outbox until then."
        : sentText(payload, values));
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
      picked = null;
      view.query = "";
      view.show = "";
      showShows();
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
    $("write").lastChild.textContent = { calendar: "New entry", tasks: "New task", notes: "New note", contacts: "New contact" }[folder] || "New mail";
    $("searchToggle").hidden = folder === "calendar";   // the calendar is looked through by day, not searched
    $("deep").parentNode.hidden = !!PAGES[folder];
    showShows();
    showPick();
    $("q").placeholder = PAGES[folder] ? "Search " + FOLDERS[folder].toLowerCase() : "Search subject or person";
    showThingBar();
    $("refresh").setAttribute("aria-label", folder === "calendar" ? "Read the calendar again" : "Check for new mail");
    $("more").textContent = folder === "calendar" ? "Show later days" : "Show older mail";
    showOutbox();
    showCalendarBar();
    if (folderChanged) { view.rows = []; view.days = 0; load(false); }
    if (id) (folder === "calendar" ? openEvent : PAGES[folder] ? openThing : openItem)(id);
    else closeItem();
  }

  // every folder, under the folder's name in the bar; the ones used most stay at the thumb
  function showFolders(on) {
    $("folderMenu").hidden = !on;
    $("folderPick").setAttribute("aria-expanded", String(on));
    if (!on) return;
    $("folderDo").hidden = pcBehind(NEEDS_MORE);
    $("folderRename").hidden = $("folderGone").hidden = !isOwnFolder(view.folder);
    $("emptyOpen").hidden = (view.folder !== "deleted" && view.folder !== "junk") || pcBehind(NEEDS_OPEN);
    $("emptyOpen").textContent = view.folder === "junk" ? "Empty Junk" : "Empty Deleted Items";
    $("pushOpen").hidden = DEMO || pcBehind(NEEDS_PUSH);
    $("pushOpen").textContent = "Notifications: " + (store.get("titip.push") === "1" ? "on" : "off");
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
    $("planEndDay").value = dayOf(ends);
    var full = !pcBehind(NEEDS_FULL);
    $("optField").hidden = !full || (!!event && !planned.people);
    setChips($("optInput"), full && planned.people ? event.optionalPeople || [] : []);
    $("planBusyField").hidden = $("planPrivateField").hidden = !full;
    $("planBusy").value = event && event.busy ? event.busy : "";   // a new entry: busy, or free when it is all day, as Outlook decides
    planned.busy = $("planBusy").value;
    planned.day = $("planDay").value;
    $("planPrivate").checked = !!(event && event["private"]);
    $("planPrivate").disabled = scope === "one";   // Outlook keeps this for the whole series
    planned["private"] = $("planPrivate").checked;
    Array.prototype.forEach.call($("planRepeat").querySelectorAll("[data-needs]"), function (option) {
      option.hidden = option.disabled = option.getAttribute("data-needs") === "last" ? pcBehind(NEEDS_LAST) : !full;
    });
    $("planFrom").value = event && event.allDay ? "09:00" : hourOf(begins);
    $("planTo").value = event && event.allDay ? "09:30" : hourOf(ends);
    $("planAllDay").checked = !!(event && event.allDay);
    // when a series takes place is changed in Outlook: here what it is called, where, who, and its text
    ["planDay", "planEndDay", "planFrom", "planTo", "planAllDay"].forEach(function (id) { $(id).disabled = series; });
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
    nameRepeats();
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
  // What the ways to repeat are called for the day picked ("on the second Tuesday"), and the days of the week
  // for an entry that repeats weekly. `moved`: the day was changed, so the days pressed start over.
  var WEEKDAY_ORDER = [1, 2, 3, 4, 5, 6, 0];   // Monday first, numbered as the phone counts them (Sunday 0)
  function nameRepeats(moved) {
    var day = $("planDay").value;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return;
    var date = dayStart(day);
    var weekday = date.toLocaleDateString([], { weekday: "long" });
    var nth = Math.floor((date.getDate() - 1) / 7);
    var lastOne = date.getDate() + 7 > new Date(date.getFullYear(), date.getMonth() + 1, 0).getDate();
    $("planNth").textContent = "Every month: " + ["first", "second", "third", "fourth", "last"][nth] + " " + weekday;
    $("planLast").textContent = "Every month: last " + weekday;
    var behind = pcBehind(NEEDS_LAST);
    $("planNth").hidden = $("planNth").disabled = behind;
    $("planLast").hidden = $("planLast").disabled = behind || !lastOne || nth === 4;   // the fifth is the last already
    if ($("planRepeat").selectedOptions[0] && $("planRepeat").selectedOptions[0].disabled) {
      $("planRepeat").value = "";
      $("planTimes").hidden = $("planTimesWord").hidden = true;
    }
    var weekly = /^week(:|$)/.test($("planRepeat").value) && !behind && !$("planRepeatField").hidden;
    $("planDaysField").hidden = !weekly;
    var box = $("planDays");
    if (!box.firstChild) {
      WEEKDAY_ORDER.forEach(function (number) {
        var name = new Date(2024, 0, number === 0 ? 7 : number).toLocaleDateString([], { weekday: "short" });   // 1 January 2024 was a Monday
        var button = el("button", "", name.slice(0, 2));
        button.type = "button";
        button.setAttribute("data-day", String(number));
        button.setAttribute("aria-label", new Date(2024, 0, number === 0 ? 7 : number).toLocaleDateString([], { weekday: "long" }));
        button.setAttribute("aria-pressed", "false");
        button.onclick = function () { button.setAttribute("aria-pressed", String(button.getAttribute("aria-pressed") !== "true")); };
        box.appendChild(button);
      });
    }
    Array.prototype.forEach.call(box.children, function (button) {
      var own = Number(button.getAttribute("data-day")) === date.getDay();
      if (moved || own || !weekly) button.setAttribute("aria-pressed", String(own));
      button.disabled = own;   // the day it starts on is always one of them
    });
  }
  function weekdaysPicked() {
    return Array.prototype.filter.call($("planDays").children, function (button) { return button.getAttribute("aria-pressed") === "true"; })
      .map(function (button) { return Number(button.getAttribute("data-day")); });
  }
  function submitPlan() {
    var subject = $("planSubject").value.trim();
    var day = $("planDay").value;
    if (!subject) { showPlanError("Give it a title first."); $("planSubject").focus(); return; }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) { showPlanError("Pick a day."); $("planDay").focus(); return; }
    var people = $("withField").hidden ? [] : fieldPeople($("withInput"));
    var optional = $("optField").hidden ? [] : fieldPeople($("optInput"));
    var invited = people.concat(optional);
    var lastDay = $("planEndDay").value;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(lastDay) || lastDay < day) lastDay = day;   // left as it was while the day moved on: one day
    var order = { what: "save", id: planned.id, subject: subject, location: $("planWhere").value.trim(), body: $("planNotes").value, "with": people.map(handed) };
    if (!$("optField").hidden) {
      order.optional = optional.map(handed);
      if (planned.id) order.optionalExact = true;   // the ones listed are everyone who is optional
    }
    if (!$("planBusyField").hidden && $("planBusy").value && $("planBusy").value !== planned.busy) order.busy = $("planBusy").value;
    if (!$("planPrivateField").hidden && !$("planPrivate").disabled && (!planned.id || $("planPrivate").checked !== planned["private"])) order["private"] = $("planPrivate").checked;
    if (planned.scope) order.scope = planned.scope;
    if (planned.occurrence) order.occurrence = planned.occurrence;
    if (planned.id && planned.people) order.withExact = true;   // the people listed are everyone who is invited
    if (!$("planRemindField").hidden && $("planRemind").value !== planned.remind) order.reminder = $("planRemind").value === "none" ? "none" : Number($("planRemind").value);
    if (!planned.id && $("planRepeat").value) {
      var times = Number($("planTimes").value) || 0;
      if (times === 1 || times < 0 || times > 500) { showPlanError("It can repeat 2 to 500 times, or without an end."); $("planTimes").focus(); return; }
      var how = $("planRepeat").value.split(":");   // "week:2" is every second week
      order.repeat = { every: how[0] === "monthLast" ? "monthNth" : how[0], times: times };
      if (how[1]) order.repeat.interval = Number(how[1]);
      if (how[0] === "monthLast") order.repeat.last = true;
      var on = weekdaysPicked();
      if (how[0] === "week" && !$("planDaysField").hidden && on.length > 1) {   // on several days of the week
        order.repeat.days = on;
        order.repeat.day = dayStart(day).getDay();   // which of them it starts on, by this phone's clock
      }
    }
    if ($("planAllDay").checked) {
      order.allDay = true;
      order.day = day;
      order.days = Math.round((dayStart(lastDay) - dayStart(day)) / 86400000) + 1;
    } else {
      var from = $("planFrom").value, to = $("planTo").value;
      if (!/^\d{2}:\d{2}/.test(from) || !/^\d{2}:\d{2}/.test(to)) { showPlanError("Say from when to when, or tick All day."); $("planFrom").focus(); return; }
      var first = dayStart(day);
      var begins = new Date(first.getFullYear(), first.getMonth(), first.getDate(), Number(from.slice(0, 2)), Number(from.slice(3, 5)));
      var last = dayStart(lastDay);
      var ends = new Date(last.getFullYear(), last.getMonth(), last.getDate(), Number(to.slice(0, 2)), Number(to.slice(3, 5)));
      if (ends < begins) ends = new Date(ends.getTime() + 86400000);   // until after midnight
      order.start = begins.toISOString();
      order.end = ends.toISOString();
    }
    var job = planned;
    var telling = invited.length > 0 || job.meeting;
    showPlanner(false);
    later(telling ? (job.id ? "Sending the change" : "Sending the invitation") : "Saving", function () {
      toast(telling ? "Sending\u2026" : "Saving\u2026", "", null, true);
      api.plan(order).then(function (result) {
        planned = null;
        toast(result.told ? (job.id || !invited.length ? "Saved. The people invited are told." : "Invitation sent to " + invited[0].name + (invited.length > 1 ? " and " + (invited.length - 1) + " more" : ""))
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
    showRemindBar();
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

  // ------------------------------------------------------------ tasks, notes and contacts: Outlook's other pages
  function showThingBar() {
    var tasks = view.folder === "tasks";
    $("thingBar").hidden = !tasks;
    if (!tasks) { view.done = false; return; }
    $("thingSay").textContent = view.done ? "Done tasks" : "Open tasks";
    $("thingFlip").textContent = view.done ? "Show the open ones" : "Show the done ones";
  }
  function dueText(one) {
    if (!one.due) return "No due date";
    var today = dayOf(new Date());
    var name = one.due === today ? "today" : one.due === dayOf(new Date(Date.now() + 86400000)) ? "tomorrow" : dayName(dayStart(one.due));
    return "Due " + name + (!one.done && one.due < today ? " \u00b7 overdue" : "");
  }
  function renderThings() {
    var list = $("rows");
    var kind = view.folder;
    list.textContent = "";
    view.rows.forEach(function (one) {
      var item = el("li");
      var late = kind === "tasks" && !one.done && one.due && one.due < dayOf(new Date());
      var row = el("button", "row" + (kind === "contacts" ? " contact" : " thing") + (late ? " unread" : "") + (one.done ? " off" : ""));
      row.type = "button";
      if (one.id === view.id) row.setAttribute("aria-current", "true");
      if (kind === "contacts") row.appendChild(face(one.title));
      var name = el("span", "who");
      if (one.important) name.appendChild(el("span", "bang", "! "));
      name.appendChild(document.createTextNode(one.title));
      row.appendChild(name);
      row.appendChild(el("span", "time", kind === "notes" ? shortTime(one.time) : ""));
      var under = kind === "tasks" ? dueText(one) : kind === "notes" ? one.preview : [one.company, one.email || one.phone].filter(Boolean).join(" \u00b7 ");
      row.appendChild(el("span", "preview", under || ""));
      row.onclick = function () { location.hash = "#/" + kind + "/" + one.id; };
      item.appendChild(row);
      list.appendChild(item);
    });
    $("more").hidden = true;
    if (view.rows.length) { showState(""); return; }
    if (view.query) showState("No match", "Nothing in " + FOLDERS[kind] + " matches \u201c" + view.query + "\u201d.");
    else if (kind === "tasks") showState(view.done ? "Nothing done yet" : "Nothing to do", view.done ? "Tasks you tick off show here." : "Outlook's tasks show here. New task makes one.");
    else if (kind === "notes") showState("No notes", "Outlook's notes show here. New note makes one.");
    else showState("No contacts", "Outlook's contacts show here. New contact makes one.");
  }
  function loadThings(silent) {
    if (view.loading && silent) return Promise.resolve();
    loads += 1;
    var mine = loads;
    var kind = view.folder;
    view.loading = true;
    if (!silent) {
      showState("");
      showSaved({});
      if (!view.rows.length) skeleton();
      $("more").hidden = true;
      $("refresh").classList.add("busy");
    }
    return api.things(kind, view.query, kind === "tasks" && view.done).then(function (page) {
      if (mine !== loads || view.folder !== kind) return;
      view.rows = page.things || [];
      view.more = false;
      view.at = Date.now();
      renderThings();
    }).catch(function (error) {
      if (mine !== loads || (silent && view.rows.length)) return;
      $("rows").textContent = "";
      showState("Can't show your " + FOLDERS[kind].toLowerCase(), error.message, true);
    }).then(function () {
      if (mine !== loads) return;
      view.loading = false;
      $("refresh").classList.remove("busy");
    });
  }
  function openThing(id) {
    var kind = view.folder;
    view.id = id;
    view.item = null;
    document.body.classList.add("reading");
    $("readerHint").hidden = true;
    $("backLabel").textContent = FOLDERS[kind];
    showMore(false);
    renderThings();
    var row = rowOf(id);
    var letter = $("letter");
    letter.classList.remove("opening");
    letter.classList.add("bare");
    $("subject").textContent = row ? row.title : "";
    ["tags", "files", "viewRow", "invite", "remove", "moreToggle", "actions"].forEach(function (part) { $(part).hidden = true; });
    $("body").textContent = "";
    $("body").appendChild(el("p", "wait", "Opening\u2026"));
    letter.hidden = false;
    api.thing(kind, id).then(function (full) {
      if (view.id !== id || view.folder !== kind) return;
      showThing(kind, full);
      $("reader").scrollTop = 0;
      window.scrollTo(0, 0);
    }).catch(function (error) {
      if (view.id !== id) return;
      toast(error.message);
      location.hash = "#/" + kind;
    });
  }
  function showThing(kind, full) {
    $("subject").textContent = full.title;
    var facts = kind === "tasks" ? [full.done ? "Done" : "", full.start ? "Starts " + dayName(dayStart(full.start)) : "", dueText(full),
      full.important ? "High importance" : "", full.remind ? "Reminder " + longTime(full.remind) : ""]
      : kind === "notes" ? [longTime(full.time)] : [full.job, full.company, full.address];
    $("tags").textContent = facts.filter(Boolean).join(" \u00b7 ");
    $("tags").hidden = !$("tags").textContent;
    var ways = $("files");   // how a contact is reached: a tap writes a mail, or dials
    ways.textContent = "";
    if (kind === "contacts") {
      (full.emails || []).forEach(function (address) {
        var entry = el("li"), link = el("a", "", address);
        link.href = "mailto:" + address;
        link.onclick = function (event) { event.preventDefault(); writeTo(full.title, address); };
        entry.appendChild(link);
        ways.appendChild(entry);
      });
      (full.phones || []).forEach(function (phone) {
        var entry = el("li"), link = el("a", "", phone.number);
        link.href = "tel:" + phone.number.replace(/[^0-9+]/g, "");
        link.appendChild(el("small", "", phone.label));
        entry.appendChild(link);
        ways.appendChild(entry);
      });
    }
    ways.hidden = !ways.firstChild;
    var box = $("body");
    box.textContent = "";
    var text = String(full.body || "").trim();
    if (kind === "notes") {   // a note's first line is its title: the text under it is the rest
      var cut = text.indexOf("\n");
      text = cut < 0 ? "" : text.slice(cut + 1).trim();
    }
    if (text) box.appendChild(el("pre", "", text));
    var actions = $("actions");
    actions.textContent = "";
    function act(label, primary, run) {
      var button = el("button", primary ? "primary" : "", label);
      button.type = "button";
      button.onclick = run;
      actions.appendChild(button);
    }
    if (kind === "tasks") act(full.done ? "Not done" : "Done", true, function () { tickThing(full, !full.done); });
    if (kind === "contacts" && (full.emails || []).length) act("Write a mail", true, function () { writeTo(full.title, full.emails[0]); });
    act("Change", kind === "notes" || (kind === "contacts" && !(full.emails || []).length), function () { openThinger(kind, full); });
    act("Delete", false, function () { removeThing(kind, full); });
    actions.hidden = false;
  }
  function writeTo(name, address) {
    openComposer("new", null);
    addChips($("toInput"), [{ name: name, address: address }]);
    $("subjectInput").focus();
  }
  function afterThing(kind, said) {
    toast(said);
    if (view.folder !== kind) return;
    if (view.id) location.hash = "#/" + kind;
    view.rows = [];
    load(false);
  }
  function tickThing(full, done) {
    api.keep({ kind: "tasks", what: done ? "done" : "undone", id: full.id }).then(function () {
      afterThing("tasks", done ? "Done" : "Open again");
    }).catch(function (error) { toast((error.unsure ? "" : "Not changed. ") + error.message); });
  }
  function removeThing(kind, full) {
    later("Deleting", function () {
      toast("Deleting\u2026", "", null, true);
      api.keep({ kind: kind, what: "remove", id: full.id }).then(function () {
        afterThing(kind, "Moved to Deleted");
      }).catch(function (error) { toast((error.unsure ? "" : "Not deleted. ") + error.message); });
    });
  }
  var NAMES = { tasks: "task", notes: "note", contacts: "contact", away: "automatic reply" };
  var editing = null;   // the task, note or contact in the form: {kind, id}
  function showThinger(on) {
    $("thinger").hidden = !on;
    document.body.classList.toggle("composing", on);
    if (!on) editing = null;
  }
  function openThinger(kind, full) {
    editing = { kind: kind, id: full ? full.id : "" };
    $("thingTitleBar").textContent = (full ? "Change the " : "New ") + NAMES[kind];
    Array.prototype.forEach.call(document.querySelectorAll("#thingForm [data-kinds]"), function (field) {
      field.hidden = field.getAttribute("data-kinds").split(" ").indexOf(kind) < 0;
    });
    $("thingNameLabel").textContent = kind === "contacts" ? "Name" : "Task";
    $("thingName").placeholder = kind === "contacts" ? "First and last name" : "What is to be done";
    $("thingName").value = full && kind !== "notes" ? full.title : "";
    $("thingDue").value = full ? full.due || "" : "";
    // the start day and the reminder: with a PC from 0.15.0 on
    if (kind === "tasks") $("thingStartField").hidden = $("thingRemindField").hidden = pcBehind(NEEDS_LAST);
    $("thingStart").value = full ? full.start || "" : "";
    var reminds = full && full.remind ? new Date(full.remind) : null;
    $("thingRemindDay").value = reminds ? dayOf(reminds) : "";
    $("thingRemindAt").value = reminds ? hourOf(reminds) : "";
    $("thingImportant").checked = !!(full && full.important);
    $("thingCompany").value = full ? full.company || "" : "";
    $("thingJob").value = full ? full.job || "" : "";
    $("thingEmail").value = full ? full.email || "" : "";   // the first address, the one that is saved: not a second one in its place
    $("thingMobile").value = full ? full.mobile || "" : "";
    $("thingPhone").value = full ? full.work || "" : "";
    $("thingBody").value = full ? full.body || "" : "";
    $("thingBody").placeholder = kind === "notes" ? "Write the note. Its first line is its title." : "Notes";
    $("thingFoot").textContent = kind === "away" ? "What colleagues who write to you are told while automatic replies are on."
      : "Kept in Outlook's " + FOLDERS[kind].toLowerCase() + " on your PC.";
    if (kind === "away") {
      $("thingBody").placeholder = "For colleagues: I am out of the office until\u2026";
      // the text for people outside: offered when the PC could read it (0.14.1 and later)
      $("thingOutsideField").hidden = !full || full.outside === null || full.outside === undefined || pcBehind(NEEDS_REAL);
      $("thingOutside").value = full && full.outside ? full.outside : "";
    }
    // two boxes one over the other need their names over them; one box alone says what it is in its footnote
    var two = kind === "away" && !$("thingOutsideField").hidden;
    $("thingBodyField").className = two ? "field grow named" : "field grow";
    $("thingBodyLabel").className = two ? "" : "sr";
    $("thingBodyLabel").textContent = two ? "People in your organization are told" : "Text";
    if (two) {
      $("thingBody").placeholder = "I am out of the office until\u2026";
      $("thingFoot").textContent = "What people who write to you are told while automatic replies are on.";
    }
    $("thingError").hidden = true;
    dimUnset();
    showThinger(true);
    (kind === "notes" ? $("thingBody") : $("thingName")).focus();
  }
  // a day or a time of a task that is not set is dimmed: an empty box may show today's date
  function dimUnset() {
    ["thingStart", "thingDue", "thingRemindDay", "thingRemindAt"].forEach(function (id) { $(id).classList.toggle("unset", !$(id).value); });
  }
  ["thingStart", "thingDue", "thingRemindDay", "thingRemindAt"].forEach(function (id) { $(id).oninput = $(id).onchange = dimUnset; });
  function submitThing() {
    var job = editing;
    var kind = job.kind;
    if (kind === "away") {
      var told = $("thingBody").value.trim();
      if (!told) { $("thingError").textContent = "Write what people are to be told first."; $("thingError").hidden = false; $("thingBody").focus(); return; }
      showThinger(false);
      var order = { what: "awayText", text: told };
      if (!$("thingOutsideField").hidden) order.outside = $("thingOutside").value.trim();
      arrange(order, "The automatic reply says the new text");
      return;
    }
    var order = {
      kind: kind, what: "save", id: job.id, title: $("thingName").value.trim(), body: $("thingBody").value, due: $("thingDue").value,
      important: $("thingImportant").checked, company: $("thingCompany").value.trim(), job: $("thingJob").value.trim(),
      email: $("thingEmail").value.trim(), mobile: $("thingMobile").value.trim(), phone: $("thingPhone").value.trim()
    };
    function refuse(text, field) { $("thingError").textContent = text; $("thingError").hidden = false; $(field).focus(); }
    if (kind === "notes" && !order.body.trim()) { refuse("Write the note first.", "thingBody"); return; }
    if (kind === "tasks" && !order.title) { refuse("Say what is to be done first.", "thingName"); return; }
    if (kind === "tasks" && !$("thingStartField").hidden) {
      order.start = $("thingStart").value;
      if (order.start && !order.due) { refuse("A task that starts on a day is due on a day too. Give it a due day.", "thingDue"); return; }
      if (order.start && order.start > order.due) { refuse("It is due before it starts.", "thingDue"); return; }
      var remindDay = $("thingRemindDay").value, remindAt = $("thingRemindAt").value || "09:00";
      if (!remindDay && $("thingRemindAt").value) { refuse("Pick the day of the reminder too.", "thingRemindDay"); return; }
      var on = remindDay ? dayStart(remindDay) : null;
      order.remind = on ? new Date(on.getFullYear(), on.getMonth(), on.getDate(), Number(remindAt.slice(0, 2)), Number(remindAt.slice(3, 5))).toISOString() : "";
    }
    if (kind === "contacts" && !order.title) { refuse("Give it a name first.", "thingName"); return; }
    if (kind === "contacts" && order.email && !ADDRESS.test(order.email)) { refuse("That e-mail address is not complete.", "thingEmail"); return; }
    showThinger(false);
    toast("Saving\u2026", "", null, true);
    api.keep(order).then(function () {
      if (kind === "contacts" && order.email) addPerson(order.title, order.email);
      afterThing(kind, "Saved");
    }).catch(function (error) {
      editing = job;
      showThinger(true);
      $("thingError").textContent = (error.unsure ? "" : "Not saved. ") + error.message;
      $("thingError").hidden = false;
    });
  }

  // ---- a mail that leaves later: it waits in Outlook's Outbox until the time picked
  function showLater(on) {
    $("laterField").hidden = !on;
    $("laterFlip").setAttribute("aria-pressed", String(on));
    $("send").textContent = on ? "Send later" : "Send";
    if (!on || $("laterDay").value) return;
    var morning = new Date(Date.now() + 86400000);   // tomorrow at eight, to start from
    $("laterDay").value = dayOf(morning);
    $("laterTime").value = "08:00";
  }
  function laterMoment() {
    var day = $("laterDay").value, time = $("laterTime").value;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day) || !/^\d{2}:\d{2}/.test(time)) return null;
    var first = dayStart(day);
    return new Date(first.getFullYear(), first.getMonth(), first.getDate(), Number(time.slice(0, 2)), Number(time.slice(3, 5)));
  }

  // ---- automatic replies (out of office): the switch. The text of the reply is the one set in Outlook.
  var away = null;   // true, false, or null when the mailbox does not say
  function showAway(state) {
    away = state === true || state === false ? state : null;
    $("awayNote").hidden = away !== true;
    $("awayOpen").hidden = away === null || pcBehind(NEEDS_PAGES);
    $("awayOpen").textContent = "Automatic replies: " + (away ? "on" : "off");
  }
  function switchAway(on) {
    arrange({ what: "away", on: on }, on ? "Automatic replies are on" : "Automatic replies are off", function () { showAway(on); refreshCounts(); });
  }
  function pickAway() {
    var known = null;
    pickFrom("Automatic replies", "They are " + (away ? "on" : "off") + ". The text people get is the one set in Outlook.", false, function (got) {
      known = got;
      if (got.on === true || got.on === false) showAway(got.on);
      var rows = [{ id: away ? "off" : "on", name: away ? "Switch off" : "Switch on", note: away ? "" : "People who write to you get your out-of-office answer" }];
      if (got.text !== null && got.text !== undefined) {   // the PC could read what colleagues are told: it is shown, and can be changed
        $("pickerNote").textContent = "They are " + (away ? "on" : "off") + ". " + (got.text ? "People in your organization are told: \u201c" +
          (got.text.length > 240 ? got.text.slice(0, 240) + "\u2026" : got.text) + "\u201d" : "No text is set for them yet.");
        var both = got.outside !== null && got.outside !== undefined && !pcBehind(NEEDS_REAL);
        rows.push({ id: "text", name: "Change the text", note: both ? "What people are told, in your organization and outside it. Whether people outside get an answer, and dates, are set in Outlook."
          : "What people in your organization are told. For people outside, and for dates, use Outlook." });
      }
      return rows;
    }, function (chosen) {
      if (chosen.id === "text") { openThinger("away", { id: "", body: known.text || "", outside: known.outside }); return; }
      switchAway(chosen.id === "on");
    }, { load: function () { return pcBehind(NEEDS_OPEN) ? Promise.resolve({ on: away, text: null }) : api.away().catch(function () { return { on: away, text: null }; }); } });
  }
  // ---- notices while Titip is closed: new mail and Outlook's reminders, through the phone's own push service.
  // The PC seals each notice for this phone; the push service passes it on and cannot read it (titip/push.py).
  function pushCannot() {   // "" when this page can be told while it is closed, else why not, in words
    if (!("serviceWorker" in navigator) || !window.isSecureContext) return "This browser cannot be told while Titip is closed.";
    if (!("PushManager" in window) || !("Notification" in window)) {
      return window.TitipFar && window.TitipFar.ios && !window.TitipFar.standalone
        ? "On an iPhone, notices come to the Home Screen icon. Add Titip to the Home Screen (Share, Add to Home Screen), open it from there, and switch them on."
        : "This browser cannot be told while Titip is closed.";
    }
    return "";
  }
  function keyBytes(text) {   // the PC's public key, as the browser wants it
    var plain = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((text.length + 3) % 4));
    var bytes = new Uint8Array(plain.length);
    for (var index = 0; index < plain.length; index += 1) bytes[index] = plain.charCodeAt(index);
    return bytes;
  }
  function pushWorker() {
    return navigator.serviceWorker.register("sw.js").then(function () { return navigator.serviceWorker.ready; }).catch(function () {
      throw new Error("Notices need the page that works on any network (switch on Mail from anywhere on the PC page and open Titip from its code). " +
        "The page on your own network has no certificate a phone accepts for this.");
    });
  }
  function pickPush() {
    var cannot = pushCannot(), state = null;
    pickFrom("Notifications", cannot, false, function (got) {
      if (cannot) return [];
      state = got;
      var on = !!got.sub && got.on;
      store.set("titip.push", on ? "1" : "0");
      $("pickerNote").textContent = on ? "On. This phone is told about new mail and Outlook's reminders while Titip is closed."
        : "Off. Switched on, this phone is told about new mail and Outlook's reminders while Titip is closed. Your PC seals each notice for this phone: " +
          "the phone maker's push service passes it on and cannot read it.";
      $("pickerNote").hidden = false;
      return on ? [{ id: "test", name: "Send a test", note: "A notice from your PC, now" }, { id: "off", name: "Switch off", note: "" }]
        : [{ id: "on", name: "Switch on", note: "The phone asks once whether Titip may send notices" }];
    }, function (chosen) {
      if (chosen.id === "on") switchPush(state);
      else if (chosen.id === "off") {
        var endpoint = state.sub.endpoint;
        state.sub.unsubscribe().catch(function () { /* the PC is told all the same */ }).then(function () { return api.pushSet({ what: "off", endpoint: endpoint }); })
          .then(function () { store.set("titip.push", "0"); toast("Notifications are off"); })
          .catch(function (error) { toast("Switched off on this phone. " + error.message); });
      } else {
        toast("Asking your PC\u2026", "", null, true);
        api.pushSet({ what: "test", endpoint: state.sub.endpoint }).then(function (result) {
          toast(result.reached ? "A test is on its way to this phone." : "The test did not leave. " + result.why);
        }).catch(function (error) { toast(error.message); });
      }
    }, { load: function () {
      if (cannot) return Promise.resolve({});
      return pushWorker().then(function (worker) { return worker.pushManager.getSubscription(); }).then(function (sub) {
        return api.push(sub ? sub.endpoint : "").then(function (got) { got.sub = sub; return got; });
      });
    }, empty: cannot });
  }
  function switchPush(state) {
    // asked for here, in the tap itself: a phone only shows the question when a finger asked
    var asked;
    try { asked = Notification.requestPermission(); } catch (error) { asked = Promise.reject(error); }
    Promise.resolve(asked).then(function (answer) {
      if (answer !== "granted") throw new Error("The phone did not allow it. Allow notifications for Titip in the phone's settings, then switch on again.");
      toast("Switching on\u2026", "", null, true);
      return navigator.serviceWorker.ready;
    }).then(function (worker) {
      // a sign-up from before (another PC key) is given up first: a browser keeps one, for one key
      return Promise.resolve(state.sub ? state.sub.unsubscribe() : null).catch(function () { return null; }).then(function () {
        return worker.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(state.key) });
      });
    }).then(function (made) {
      return api.pushSet({ what: "on", subscription: made.toJSON() });
    }).then(function (result) {
      store.set("titip.push", "1");
      toast(result.reached ? "Notifications are on. A test is on its way." : "Switched on, but the test did not leave. " + result.why);
    }).catch(function (error) { toast("Not switched on. " + error.message); });
  }
  $("pushOpen").onclick = function () { showFolders(false); pickPush(); };
  if ("serviceWorker" in navigator) {
    // a tap on a notice while Titip is open: the worker says where it leads
    navigator.serviceWorker.addEventListener("message", function (event) {
      var said = event.data || {}, where = String(said.url || "").charAt(0) === "#" ? said.url : "";
      if (said.titip === "open" && where) location.hash = where;
      if (said.titip === "notice") {   // it arrived while Titip is open: said here as well, and the list looks for the mail
        toast(String(said.title || "Titip") + (said.body ? ": " + said.body : ""), where ? "Show" : "", function () { hideToast(); location.hash = where; });
        if (!document.hidden && !view.query && !view.show && firstPage() && $("composer").hidden) load(false, true);
      }
    });
    // kept signed up: the worker is looked for again each time the page opens, so a new one replaces the old
    // On the page for any network the worker also keeps the app's own files, so the icon opens without a network.
    if (HOSTED || store.get("titip.push") === "1") navigator.serviceWorker.register("sw.js").catch(function () { /* said when the sheet is opened */ });
  }
  // ---- everything in Deleted Items, for good; everything in Junk, to Deleted Items
  $("emptyOpen").onclick = function () {
    var folder = view.folder, good = folder === "deleted";
    showFolders(false);
    pickFrom(good ? "Empty Deleted Items" : "Empty Junk", good ? "Everything in Deleted Items is deleted for good. This cannot be undone."
      : "Everything in Junk is moved to Deleted Items.", false, function () {
      return [{ id: "go", name: good ? "Delete everything for good" : "Move everything to Deleted Items", note: "" }];
    }, function () {
      toast("Emptying\u2026", "", null, true);
      api.arrange({ what: "empty", folder: folder }).then(function (result) {
        toast(result.left ? result.emptied + " gone, " + result.left + " still there. Empty it again for the rest."
          : result.emptied + (good ? " deleted for good" : " moved to Deleted"));
        if (view.folder === folder) { view.rows = []; load(false); }
        refreshCounts();
      }).catch(function (error) { toast((error.unsure ? "" : "Not emptied. ") + error.message); });
    }, { load: function () { return Promise.resolve(); } });
  };
  // ---- several mails at once: ticked in the list, then deleted, marked, flagged or moved together
  var picked = null;   // the ids ticked, while mails are being picked; null otherwise
  function showPick() {
    var on = !!picked && !PAGES[view.folder] && view.folder !== "calendar";
    if (!on) picked = null;
    $("pickBar").hidden = !on;
    $("pickOpen").hidden = !!PAGES[view.folder] || view.folder === "calendar" || pcBehind();
    $("pickOpen").textContent = on ? "Stop selecting" : "Select several mails";
    if (!on) return;
    var count = Object.keys(picked).length, marks = view.folder === "inbox" || isOwnFolder(view.folder);
    $("pickSay").textContent = count ? count + " selected" : "Tap the mails to select";
    $("pickDelete").textContent = view.folder === "deleted" ? "Delete for good" : "Delete";
    $("pickDelete").hidden = view.folder === "deleted" && pcBehind(NEEDS_OPEN);
    $("pickRead").hidden = $("pickUnread").hidden = !marks;
    $("pickFlag").hidden = $("pickMove").hidden = view.folder === "outbox" || view.folder === "drafts";
    ["pickDelete", "pickRead", "pickUnread", "pickFlag", "pickMove"].forEach(function (id) { $(id).disabled = !count; });
  }
  function startPick(id) {
    picked = {};
    if (id) picked[id] = true;
    showPick();
    renderRows();
  }
  function stopPick() {
    picked = null;
    showPick();
    renderRows();
  }
  // one after the other, so Outlook is asked for one thing at a time; what failed is counted and said
  function bulk(order, doing, done) {
    var ids = Object.keys(picked || {}), folder = view.folder, failed = 0, why = "";
    toast(doing + "\u2026", "", null, true);
    stopPick();
    ids.reduce(function (chain, id) {
      return chain.then(function () {
        return api.act(order(id)).catch(function (error) { failed += 1; why = error.message; });
      });
    }, Promise.resolve()).then(function () {
      toast(failed ? (ids.length - failed) + " of " + ids.length + " done. " + why : done(ids.length));
      opened = opened.filter(function (id) { return ids.indexOf(id) < 0; });
      store.set("titip.opened", JSON.stringify(opened));
      if (view.folder === folder) { view.rows = []; load(false); }
      refreshCounts();
    });
  }
  function mails(count) { return count + (count === 1 ? " mail" : " mails"); }
  $("pickOpen").onclick = function () { if (picked) stopPick(); else startPick(""); };
  $("pickDone").onclick = stopPick;
  $("pickDelete").onclick = function () {
    var count = Object.keys(picked || {}).length, good = view.folder === "deleted", keep = picked;
    later(good ? "Deleting " + mails(count) + " for good" : "Deleting " + mails(count), function () {
      picked = keep;
      bulk(function (id) { return { what: good ? "erase" : "delete", id: id }; }, "Deleting",
        function (done) { return mails(done) + (good ? " deleted for good" : " moved to Deleted"); });
    });
  };
  $("pickRead").onclick = function () {
    bulk(function (id) { return { what: "read", id: id }; }, "Marking", function (done) { return mails(done) + " marked as read"; });
  };
  $("pickUnread").onclick = function () {
    bulk(function (id) { return { what: "unread", id: id }; }, "Marking", function (done) { return mails(done) + " marked as unread"; });
  };
  $("pickFlag").onclick = function () {
    bulk(function (id) { return { what: "flag", id: id }; }, "Flagging", function (done) { return mails(done) + " flagged"; });
  };
  $("pickMove").onclick = function () {
    var folder = view.folder, count = Object.keys(picked || {}).length, keep = picked;
    pickFrom("Move " + mails(count) + " to", "", false, function () {
      var places = [];
      if (folder !== "inbox") places.push({ id: "inbox", name: "Inbox", note: "" });
      own.folders.forEach(function (one) {
        if (one.id !== folder) places.push({ id: one.id, name: one.name, note: [one.file, one["in"]].filter(Boolean).join(" / ") });
      });
      if (folder !== "junk") places.push({ id: "junk", name: "Junk", note: "" });
      return places;
    }, function (place) {
      later("Moving " + mails(count) + " to " + place.name, function () {
        picked = keep;
        bulk(function (id) { return { what: "move", id: id, to: place.id }; }, "Moving", function (done) { return mails(done) + " moved to " + place.name; });
      });
    });
  };

  // ---- reminders on this phone: while Titip is open, an entry that is due says so here and rings
  var REMIND_EVERY = 30000, REMIND_READ = 1800000;
  var remind = { on: store.get("titip.remind") === "1", events: [], at: 0, told: [] };
  try { remind.told = JSON.parse(store.get("titip.reminded") || "[]"); } catch (error) { remind.told = []; }
  function showRemindBar() {
    $("remindBar").hidden = view.folder !== "calendar" || pcBehind(NEEDS_PAGES);
    $("remindSay").textContent = remind.on ? "Reminders ring on this phone while Titip is open." : "Reminders do not ring on this phone.";
    $("remindFlip").textContent = remind.on ? "Switch off" : "Switch on";
  }
  function flipRemind() {
    remind.on = !remind.on;
    store.set("titip.remind", remind.on ? "1" : "0");
    if (remind.on && window.Notification && Notification.permission === "default") {
      try { Notification.requestPermission(); } catch (error) { /* said on the page all the same */ }
    }
    showRemindBar();
    if (remind.on) checkReminders();
  }
  function ring(due) {   // one line for everything that is due now: two entries at the same time are both said
    var text = "Coming up: " + due.map(function (event) {
      return event.subject + ", " + (event.allDay ? "all day" : clock(event.start)) + (event.location ? ", " + event.location : "");
    }).join(" \u00b7 ");
    toast(text, "Show", function () { hideToast(); location.hash = "#/calendar" + (due.length === 1 ? "/" + eventKey(due[0]) : ""); }, true);
    try {   // two short notes; a phone on silent stays silent
      var Sound = window.AudioContext || window.webkitAudioContext;
      var sound = new Sound(), tone = sound.createOscillator(), level = sound.createGain();
      tone.frequency.value = 880;
      level.gain.value = 0.08;
      tone.connect(level);
      level.connect(sound.destination);
      tone.start();
      tone.stop(sound.currentTime + 0.35);
    } catch (error) { /* no sound on this phone */ }
    try { if (window.Notification && Notification.permission === "granted") new Notification("Titip", { body: text }); } catch (error) { /* shown on the page */ }
  }
  function checkReminders() {
    if (!remind.on || document.hidden || pcBehind(NEEDS_PAGES)) return;
    var now = Date.now();
    if (now - remind.at > REMIND_READ && !view.loading) {   // the PC keeps the calendar ten minutes: this asks seldom
      remind.at = now;
      api.calendar(0, CAL_DAYS, "", false).then(function (page) { remind.events = page.events || []; checkReminders(); }).catch(function () { remind.at = now - REMIND_READ + 300000; });
      return;
    }
    var due = remind.events.filter(function (event) {
      if (event.reminder === null || event.reminder === undefined || event.canceled || event.response === "declined") return false;
      var from = new Date(event.start).getTime() - event.reminder * 60000;
      return now >= from && now <= new Date(event.end).getTime() && remind.told.indexOf(eventKey(event)) < 0;
    });
    if (!due.length) return;
    remind.told = remind.told.concat(due.map(eventKey)).slice(-60);   // said once, also after the page is opened again
    store.set("titip.reminded", JSON.stringify(remind.told));
    ring(due);
  }
  setInterval(checkReminders, REMIND_EVERY);

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
    if (sign === "link") {   // [the words](https://...), with the cursor where the address goes
      var words = text.slice(from, to) || "link";
      box.value = text.slice(0, from) + "[" + words + "](https://)" + text.slice(to);
      var at = from + words.length + 11;
      box.setSelectionRange(at, at);
    } else if (sign === "- " || sign === "# ") {
      var first = text.lastIndexOf("\n", from - 1) + 1;
      var lines = text.slice(first, to).split("\n").map(function (line) { return line.indexOf(sign) === 0 || (sign === "- " && /^\s*- /.test(line)) ? line : sign + line; }).join("\n");
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

  $("refresh").onclick = function () { view.anew = true; load(false); refreshCounts(); };
  $("more").onclick = function () { load(true); };
  $("back").onclick = function () { location.hash = "#/" + view.folder; };
  $("write").onclick = function () {
    if (PAGES[view.folder] && !pcBehind(NEEDS_PAGES)) openThinger(view.folder, null);
    else if (view.folder === "calendar" && !pcBehind()) openPlanner(null);
    else openComposer("new", null);
  };
  $("planCancel").onclick = function () { planned = null; showPlanner(false); $("write").focus(); };
  $("planForm").onsubmit = function (event) { event.preventDefault(); submitPlan(); };
  $("planForm").oninput = function () { showPlanError(""); };
  $("planForm").addEventListener("keydown", function (event) {   // Enter in a line of the form does not save it
    if (event.key === "Enter" && event.target.tagName === "INPUT" && event.target.id !== "withInput" && event.target.id !== "optInput") event.preventDefault();
  });
  $("planAllDay").onchange = function () { $("planHours").hidden = this.checked; };
  $("planDay").onchange = function () {   // the last day goes along with the first, earlier or later: an entry stays as long as it was
    var day = /^\d{4}-\d{2}-\d{2}$/;
    if (!planned || !day.test(this.value)) return;
    if (day.test(planned.day) && day.test($("planEndDay").value)) {
      var moved = Math.round((dayStart(this.value) - dayStart(planned.day)) / 86400000);
      var last = dayStart($("planEndDay").value);
      $("planEndDay").value = dayOf(new Date(last.getFullYear(), last.getMonth(), last.getDate() + moved));
    }
    if ($("planEndDay").value < this.value) $("planEndDay").value = this.value;
    planned.day = this.value;
    nameRepeats(true);
  };
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
  $("laterFlip").onclick = function () {
    if (!draft) return;
    draft.later = !draft.later;
    showLater(draft.later);
    showComposeError("");
    rememberComposer();
  };
  $("thingCancel").onclick = function () { showThinger(false); $("write").focus(); };
  $("thingForm").onsubmit = function (event) { event.preventDefault(); submitThing(); };
  $("thingForm").oninput = function () { $("thingError").hidden = true; };
  $("thingForm").addEventListener("keydown", function (event) {
    if (event.key === "Enter" && event.target.tagName === "INPUT") event.preventDefault();
  });
  $("thingFlip").onclick = function () { view.done = !view.done; view.rows = []; showThingBar(); load(false); };
  $("awayOpen").onclick = function () { showFolders(false); pickAway(); };
  $("awayOff").onclick = function () { switchAway(false); };
  $("remindFlip").onclick = flipRemind;
  $("receipt").onclick = function () {
    if (!draft) return;
    draft.receipt = !draft.receipt;
    this.setAttribute("aria-pressed", String(draft.receipt));
    rememberComposer();
  };
  $("planRepeat").onchange = function () {
    $("planTimes").hidden = $("planTimesWord").hidden = !this.value;
    nameRepeats();
  };
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
  // a picture that shows inside the text: it goes along as a file, and a line in the text says where it is shown
  $("picture").onclick = function () { $("pictureInput").click(); };
  $("pictureInput").onchange = function () {
    var before = attached.length;
    addFiles(this.files);
    this.value = "";
    var lines = attached.slice(before).map(function (entry) { return "[picture: " + entry.name + "]"; });
    if (!lines.length) return;
    var box = $("bodyInput"), at = box.selectionStart, text = box.value;
    var put = (at > 0 && text.charAt(at - 1) !== "\n" ? "\n" : "") + lines.join("\n") + "\n";
    box.value = text.slice(0, at) + put + text.slice(at);
    box.setSelectionRange(at + put.length, at + put.length);
    rememberComposer();
  };
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
    else if (view.query || view.show) { view.query = ""; view.show = ""; showShows(); $("q").value = ""; view.rows = []; load(false); }
  };
  // only what is unread, or flagged: the three words under the search box
  function showShows() {
    $("shows").hidden = !!PAGES[view.folder] || view.folder === "calendar" || pcBehind(NEEDS_FULL);
    Array.prototype.forEach.call($("shows").querySelectorAll("button"), function (button) {
      button.setAttribute("aria-pressed", String(button.getAttribute("data-show") === view.show));
    });
  }
  Array.prototype.forEach.call($("shows").querySelectorAll("button"), function (button) {
    button.onclick = function () {
      var wanted = button.getAttribute("data-show");
      if (wanted === view.show) return;
      view.show = wanted;
      showShows();
      view.rows = [];
      load(false);
    };
  });
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
    if (!$("thinger").hidden) { showThinger(false); return; }
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
    if (!view.query && !view.show && firstPage() && $("composer").hidden && Date.now() - view.at > AGED) load(false, true);
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
    if (!document.hidden && !view.query && !view.show && firstPage() && $("composer").hidden) load(false, true);
  }, REFRESH_MS);

  window.addEventListener("hashchange", route);
  $("build").textContent = "Titip " + VERSION;
  showMe(DEMO ? "" : store.get("titip.me"));
  route();
  refreshCounts();
  if (store.get("titip.compose")) toast("You have an unfinished message.", "Open", reopenSaved, true);
}());
