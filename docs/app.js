/* Titip phone app. No framework, no build step. Mail text only ever reaches the page through
   textContent or a script-less sandboxed frame, never innerHTML. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var VERSION = "0.8.1";   // shown under the list: a Home Screen icon can keep running an old page for days
  var LOADED = Date.now();
  var FOLDERS = { inbox: "Inbox", sent: "Sent", drafts: "Drafts" };
  var PAGE = 40;
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
  var view = { folder: "inbox", id: "", rows: [], more: false, query: "", loading: false, item: null, formatted: true, at: 0, stale: false };
  var draft = null;       // what the composer is writing: {mode, id, title, note}
  var pending = null;     // a send waiting out its undo window
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
      options.keepalive = true;
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
    list: function (folder, offset, query) {
      return request("/api/mail?folder=" + folder + "&limit=" + PAGE + "&offset=" + offset + "&q=" + encodeURIComponent(query));
    },
    get: function (id) { return request("/api/item?id=" + encodeURIComponent(id)); },
    send: function (payload) { return request("/api/send", payload); },
    people: function () { return request("/api/people"); },
    lookup: function (typed) { return request("/api/people?q=" + encodeURIComponent(typed)); },
    fileUrl: function (id, index) { return "/api/attachment?id=" + encodeURIComponent(id) + "&index=" + index; }
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
      });
      demoData = data;
      return data;
    });
  }
  var demo = {
    state: function () {
      return demoLoad().then(function (data) {
        var counts = { inbox: 0, sent: 0, drafts: 0 };
        data.mail.forEach(function (mail) { counts[mail.folder] += 1; });
        return { ok: true, demo: true, account: data.me.address, name: data.me.name, counts: counts };
      });
    },
    list: function (folder, offset, query) {
      return demoLoad().then(function (data) {
        var needle = query.toLowerCase().trim();
        var rows = data.mail.filter(function (mail) {
          return mail.folder === folder &&
            (!needle || (mail.subject + " " + mail.from + " " + mail.to).toLowerCase().indexOf(needle) >= 0);
        }).sort(function (a, b) { return a.time < b.time ? 1 : -1; });
        return {
          folder: folder,
          more: rows.length > offset + PAGE,
          mail: rows.slice(offset, offset + PAGE).map(function (mail) {
            return {
              id: mail.id, subject: mail.subject, from: mail.from, to: mail.to, time: mail.time, unread: !!mail.unread,
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
        copy.draft = mail.folder === "drafts";
        copy.html = mail.html || "";
        copy.attachments = mail.attachments.map(function (file, index) {
          return { index: index + 1, name: file.name, size: file.size };
        });
        return copy;
      });
    },
    send: function (payload) {
      return demoLoad().then(function (data) {
        var source = data.mail.filter(function (candidate) { return candidate.id === payload.id; })[0];
        var to = [].concat(payload.to || []).join("; ");
        var subject = payload.subject || "";
        if (payload.mode === "reply" || payload.mode === "replyAll") {
          to = to || source.from;
          subject = "RE: " + source.subject;
        }
        if (payload.mode === "forward") subject = "FW: " + source.subject;
        if (payload.mode === "draft") data.mail.splice(data.mail.indexOf(source), 1);
        data.mail.push({
          id: "demo.n" + Date.now(), folder: payload.action === "draft" ? "drafts" : "sent",
          from: data.me.name, fromAddress: data.me.address, to: to, cc: [].concat(payload.cc || []).join("; "),
          subject: subject || "(no subject)", body: payload.body, time: new Date().toISOString(), attachments: []
        });
        return payload.action === "draft" ? { ok: true, saved: true } : { ok: true, sent: true };
      });
    },
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
        var unsure = new Error("Your PC did not confirm it. Look in Sent before you send it again.");
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
    list: function (folder, offset, query) { return farRequest("mail", { folder: folder, limit: PAGE, offset: offset, q: query }); },
    get: function (id) { return farRequest("item", { id: id }); },
    send: function (payload) { return farRequest("send", payload, true); },
    people: function () { return farRequest("people", {}); },
    lookup: function (typed) { return farRequest("people", { q: typed }); },
    fileUrl: function () { return ""; },
    file: function (id, index) { return farRequest("attachment", { id: id, index: index }); }
  };

  var DEMO = FORCED_DEMO || (HOSTED && !ticket);
  var api = DEMO ? demo : ticket ? far : pc;

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
      var unread = view.folder === "inbox" && mail.unread && opened.indexOf(mail.id) < 0;
      var row = el("button", "row" + (unread ? " unread" : ""));
      row.type = "button";
      if (mail.id === view.id) row.setAttribute("aria-current", "true");
      var who = view.folder === "inbox" ? mail.from : (mail.to || "(no recipient yet)");
      row.appendChild(face(who));
      var name = el("span", "who");
      if (view.folder === "drafts") name.appendChild(el("span", "tagDraft", "[Draft] "));
      name.appendChild(document.createTextNode(who));
      row.appendChild(name);
      row.appendChild(el("span", "time", shortTime(mail.time)));
      var subject = el("span", "subject");
      if (unread) subject.appendChild(el("span", "sr", "Unread. "));
      if (mail.attachments) {
        var clip = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        clip.setAttribute("viewBox", "0 0 24 24");
        clip.setAttribute("class", "clip");
        clip.setAttribute("role", "img");
        clip.setAttribute("aria-label", "Has attachments");
        var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", "M20 11l-8.5 8.5a5 5 0 0 1-7-7L13 4a3.5 3.5 0 0 1 5 5l-8.5 8.5a2 2 0 0 1-3-3L14 7");
        clip.appendChild(path);
        subject.appendChild(clip);
      }
      subject.appendChild(document.createTextNode(mail.subject));
      row.appendChild(subject);
      row.appendChild(el("span", "preview", mail.preview));
      row.onclick = function () { location.hash = "#/" + view.folder + "/" + mail.id; };
      item.appendChild(row);
      list.appendChild(item);
    });
    $("more").hidden = !view.more;
    if (!view.rows.length) {
      if (view.query) showState("No match", "Nothing in " + FOLDERS[view.folder] + " matches \u201c" + view.query + "\u201d.");
      else if (view.folder === "drafts") showState("No drafts", "Mail you save from the phone waits here, and in Outlook's Drafts.");
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
    if (DEMO) return;
    try { store.set("titip.list." + folder, JSON.stringify({ at: Date.now(), mail: page.mail, more: !!page.more })); }
    catch (error) { /* storage full: the list is simply asked for again next time */ }
  }

  var STALE_AGAIN = 15000;   // a list that could not be refreshed is asked for again this soon, without a tap
  var staleTimer = 0;
  function retryStale() {
    clearTimeout(staleTimer);
    if (!view.stale || view.query) return;
    if (document.hidden || !$("composer").hidden) { staleTimer = setTimeout(retryStale, STALE_AGAIN); return; }
    load(false, true);
  }

  var loads = 0;   // only the newest list asked for is shown: a tap on another folder never waits for the one before
  function load(append, silent) {
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
    if (query && !append) showState("Searching\u2026", "Looking through every mail file. This can take a few seconds.");
    return api.list(folder, offset, query).then(function (page) {
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
  function showSaved(page) {
    clearTimeout(savedTimer);
    var note = $("savedNote");
    note.hidden = !page.saved;
    if (!page.saved) return;
    note.textContent = "Outlook on your PC is busy. This is the list from " + clockTime(page.savedAt) + ".";
    savedTimer = setTimeout(function () {
      if (document.hidden || view.query || view.rows.length > PAGE || !$("composer").hidden) return;
      load(false, true);
    }, SAVED_AGAIN);
  }

  function refreshCounts() {
    api.state().then(function (state) {
      showMe(state.name || state.account);
      $("demoNote").hidden = !state.demo;
      var count = $("draftCount");
      count.hidden = !state.counts.drafts;
      count.textContent = state.counts.drafts;
    }).catch(function () { /* the list shows the real error */ });
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
  }

  function openItem(id) {
    view.id = id;
    markOpened(id);
    document.body.classList.add("reading");
    $("readerHint").hidden = true;
    $("backLabel").textContent = FOLDERS[view.folder];
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
      $("to").textContent = item.to || "(none)";
      $("ccRow").hidden = !item.cc;
      $("cc").textContent = item.cc;
      $("when").textContent = longTime(item.time);
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
      var modes = item.draft ? [["draft", "Edit draft"]] : [["reply", "Reply"], ["replyAll", "Reply all"], ["forward", "Forward"]];
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
  var chosen = { toInput: [], ccInput: [] };

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
  ["toInput", "ccInput"].forEach(function (id) {
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
        ($("subjectField").hidden ? $("bodyInput") : $("subjectInput")).focus();
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

  function composerNote(mode, source) {
    if (mode === "reply") return "Goes to " + source.from + ". Outlook adds the earlier messages below yours.";
    if (mode === "replyAll") return "Goes to " + source.from + " and everyone else on the message. Outlook adds the earlier messages below yours.";
    if (mode === "forward") return "\u201c" + source.subject + "\u201d and its attachments go along.";
    if (mode === "draft") return "Sent unchanged, it keeps its formatting. If you edit the text here, it goes as plain text.";
    return "";
  }

  // Either start from a message (source, or nothing for a new mail) or put back what was being written (saved).
  function openComposer(mode, source, saved) {
    var fields = saved || { to: "", cc: "", subject: "", body: "" };
    if (saved) {
      draft = saved.draft;
    } else {
      draft = { mode: mode, id: source ? source.id : "", title: TITLES[mode], note: composerNote(mode, source), who: source ? source.from : "" };
      if (mode === "draft") fields = { to: source.to, cc: source.cc, subject: source.subject, body: source.body };
    }
    var answering = draft.mode === "reply" || draft.mode === "replyAll";
    $("composeTitle").textContent = draft.title;
    $("replyNote").textContent = draft.note;
    $("replyNote").hidden = !draft.note;
    $("toField").hidden = answering;
    $("subjectField").hidden = draft.mode !== "new" && draft.mode !== "draft";
    $("ccLabel").textContent = answering ? "Add Cc" : "Cc";
    setChips($("toInput"), fields.to);
    setChips($("ccInput"), fields.cc);
    $("subjectInput").value = fields.subject;
    $("bodyInput").value = fields.body;
    showComposeError("");
    hidePeople();
    $("composer").hidden = false;
    document.body.classList.add("composing");
    learnPeople();
    keepAwake();
    (answering || chosen.toInput.length ? $("bodyInput") : $("toInput")).focus();
  }

  function composerValues() {
    return {
      draft: draft,
      to: fieldPeople($("toInput")),   // people, as {name, address}
      cc: fieldPeople($("ccInput")),
      subject: $("subjectInput").value.trim(),
      body: $("bodyInput").value
    };
  }

  function rememberComposer() {
    if (draft) store.set("titip.compose", JSON.stringify(composerValues()));
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
    if (forget) store.drop("titip.compose");
    draft = null;
    $("write").focus();
  }

  function showComposeError(text) {
    $("composeError").textContent = text;
    $("composeError").hidden = !text;
  }

  function submit(action) {
    var values = composerValues();
    var mode = draft.mode;
    var answering = mode === "reply" || mode === "replyAll";
    if (action === "send") {
      if (!answering && !values.to.length) { showComposeError("Add at least one recipient."); $("toInput").focus(); return; }
      if (!values.body.trim() && mode !== "forward") { showComposeError("Write a message first."); $("bodyInput").focus(); return; }
    }
    var payload = { mode: mode, id: draft.id, action: action, to: values.to.map(handed), cc: values.cc.map(handed), subject: values.subject, body: values.body };
    rememberComposer();
    closeComposer(false);
    if (action === "draft") { deliver(payload, values); return; }
    var left = UNDO_SECONDS;
    pending = { payload: payload, values: values, timer: 0 };
    function tick() {
      if (!pending) return;
      if (left <= 0) { var job = pending; pending = null; deliver(job.payload, job.values); return; }
      toast("Sending in " + left + " s", "Undo", function () {
        clearTimeout(pending.timer);
        var job = pending;
        pending = null;
        hideToast();
        openComposer("", null, job.values);
      }, true);
      left -= 1;
      pending.timer = setTimeout(tick, 1000);
    }
    tick();
  }

  function sentText(payload, values) {
    var who = values.draft.who;
    if (payload.mode === "reply" && !who) return "Reply sent";
    if (payload.mode === "replyAll") return who ? "Sent to " + who + " and everyone else on the message" : "Reply sent to everyone";
    if (payload.mode !== "reply") {
      var names = values.to.map(function (person) { return person.name; });
      if (!names.length) return "Sent";
      who = names[0] + (names.length > 1 ? " and " + (names.length - 1) + " more" : "");
    }
    return "Sent to " + who;
  }

  function deliver(payload, values) {
    toast(payload.action === "draft" ? "Saving\u2026" : "Sending\u2026", "", null, true);
    api.send(payload).then(function (result) {
      store.drop("titip.compose");
      toast(result.saved ? "Saved to Drafts" : sentText(payload, values));
      refreshCounts();
      if (view.folder !== "inbox") load(false, true);
      if (payload.mode === "draft") location.hash = "#/" + view.folder;
    }).catch(function (error) {
      hideToast();
      openComposer("", null, values);
      showComposeError((error.unsure ? "" : payload.action === "draft" ? "Not saved. " : "Not sent. ") + error.message + " Your text is still here.");
    });
  }

  // ------------------------------------------------------------ routing
  function route() {
    var parts = location.hash.replace(/^#\/?/, "").split("/");
    var folder = FOLDERS[parts[0]] ? parts[0] : "inbox";
    var id = parts.slice(1).join("/");
    var folderChanged = folder !== view.folder || !view.rows.length && !view.loading;
    if (folder !== view.folder) { view.query = ""; $("q").value = ""; }
    view.folder = folder;
    Array.prototype.forEach.call(document.querySelectorAll(".tabs a"), function (tab) {
      if (tab.getAttribute("data-folder") === folder) tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    });
    document.title = FOLDERS[folder] + " \u00b7 Titip";
    $("folderName").textContent = FOLDERS[folder];
    if (folderChanged) { view.rows = []; load(false); }
    if (id) openItem(id);
    else closeItem();
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
  $("write").onclick = function () { openComposer("new", null); };
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
    if (event.key === "Escape" && !$("composer").hidden) { rememberComposer(); closeComposer(false); toast("Kept on this phone. Tap New mail to continue.", "Open", reopenSaved); }
  });

  function reopenSaved() {
    var saved = null;
    try { saved = JSON.parse(store.get("titip.compose") || "null"); } catch (error) { saved = null; }
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
    if (!view.query && view.rows.length <= PAGE && $("composer").hidden && Date.now() - view.at > AGED) load(false, true);
  }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      if (pending) { clearTimeout(pending.timer); var job = pending; pending = null; deliver(job.payload, job.values); }
    } else {
      wake();
    }
  });
  window.addEventListener("online", wake);
  window.addEventListener("pageshow", function (event) { if (event.persisted) wake(); });

  setInterval(function () {
    if (!document.hidden && !view.query && view.rows.length <= PAGE && $("composer").hidden) load(false, true);
  }, REFRESH_MS);

  window.addEventListener("hashchange", route);
  $("build").textContent = "Titip " + VERSION;
  showMe(DEMO ? "" : store.get("titip.me"));
  route();
  refreshCounts();
  if (store.get("titip.compose")) toast("You have an unfinished message.", "Open", reopenSaved, true);
}());
