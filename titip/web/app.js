/* Titip phone app. No framework, no build step. Mail text only ever reaches the page through
   textContent or a script-less sandboxed frame, never innerHTML. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var FOLDERS = { inbox: "Inbox", sent: "Sent", drafts: "Drafts" };
  var PAGE = 40;
  var UNDO_SECONDS = 5;
  var REFRESH_MS = 60000;
  var HOSTED = /\.github\.io$/.test(location.hostname);   // the public page: no Titip server behind it
  var FORCED_DEMO = /[?&]demo\b/.test(location.search);
  var ticket = FORCED_DEMO || !window.TitipFar ? null : window.TitipFar.ticket();   // this phone scanned the code for any network

  var view = { folder: "inbox", id: "", rows: [], more: false, query: "", loading: false, item: null, formatted: true };
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
        var to = payload.to || "";
        var subject = payload.subject || "";
        if (payload.mode === "reply" || payload.mode === "replyAll") {
          to = to || source.from;
          subject = "RE: " + source.subject;
        }
        if (payload.mode === "forward") subject = "FW: " + source.subject;
        if (payload.mode === "draft") data.mail.splice(data.mail.indexOf(source), 1);
        data.mail.push({
          id: "demo.n" + Date.now(), folder: payload.action === "draft" ? "drafts" : "sent",
          from: data.me.name, fromAddress: data.me.address, to: to, cc: payload.cc || "",
          subject: subject || "(no subject)", body: payload.body, time: new Date().toISOString(), attachments: []
        });
        return payload.action === "draft" ? { ok: true, saved: true } : { ok: true, sent: true };
      });
    },
    fileUrl: function () { return ""; }
  };
  // ---- your PC from any network: every request is a sealed question the PC answers through the relay
  var FAR_READ_WAIT = 30000, FAR_SEND_WAIT = 90000;
  var FAR_SILENT = "Your PC did not answer. Check that it is on, that Titip is running, and that \u201cMail from anywhere\u201d is switched on there.";
  var relay = ticket ? window.TitipFar.connect(ticket) : null;
  function farRequest(route, params, sending, since) {
    var started = since || Date.now();
    return relay.ask(route, params, sending ? FAR_SEND_WAIT : FAR_READ_WAIT).then(function (answer) {
      if (answer.status === 503 && answer.data.busy && !sending && Date.now() - started < BUSY_WAIT) {
        toast(BUSY_TEXT);
        return new Promise(function (resolve) { setTimeout(resolve, 3000); }).then(function () {
          return farRequest(route, params, false, started);
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
      throw new Error(error.message === "silent" ? FAR_SILENT : "Can't reach the relay. Check this phone's connection.");
    });
  }
  var far = {
    state: function () { return farRequest("state", {}); },
    list: function (folder, offset, query) { return farRequest("mail", { folder: folder, limit: PAGE, offset: offset, q: query }); },
    get: function (id) { return farRequest("item", { id: id }); },
    send: function (payload) { return farRequest("send", payload, true); },
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
    view.rows.forEach(function (mail) {
      var item = el("li");
      var unread = view.folder === "inbox" && mail.unread && opened.indexOf(mail.id) < 0;
      var row = el("button", "row" + (unread ? " unread" : ""));
      row.type = "button";
      if (mail.id === view.id) row.setAttribute("aria-current", "true");
      row.appendChild(el("span", "mark"));
      var who = view.folder === "inbox" ? mail.from : (mail.to ? "To " + mail.to : "(no recipient yet)");
      row.appendChild(el("span", "who", who));
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

  function load(append, silent) {
    if (view.loading) return Promise.resolve();
    view.loading = true;
    var folder = view.folder;
    var query = view.query;
    var offset = append ? view.rows.length : 0;
    if (!append && !silent) { showState(""); skeleton(); $("more").hidden = true; showSaved({}); }
    if (!silent) $("refresh").classList.add("busy");
    if (query && !append) showState("Searching\u2026", "Looking through every mail file. This can take a few seconds.");
    return api.list(folder, offset, query).then(function (page) {
      if (folder !== view.folder || query !== view.query) return;
      view.rows = append ? view.rows.concat(page.mail) : page.mail;
      view.more = page.more;
      renderRows();
      if (!append) showSaved(page);
    }).catch(function (error) {
      if (silent) return;
      if (!append) { $("rows").textContent = ""; showState("Can't show your mail", error.message, true); }
      else toast(error.message);
    }).then(function () {
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
        '<style>body{margin:12px;font:15px/1.5 system-ui,sans-serif;color:#16202b;background:#fff;overflow-wrap:anywhere}' +
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
    $("body").appendChild(el("p", "wait", "Opening\u2026"));
    $("letter").classList.add("opening");
    $("letter").hidden = false;
    api.get(id).then(function (item) {
      if (view.id !== id) return;
      view.item = item;
      view.formatted = true;
      if (item.saved) toast("Outlook on your PC is busy. This draft may not show your latest changes.");
      $("letter").classList.remove("opening");
      $("subject").textContent = item.subject;
      $("from").textContent = item.from + (item.fromAddress && item.fromAddress !== item.from ? "  <" + item.fromAddress + ">" : "");
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
    $("toInput").value = fields.to;
    $("ccInput").value = fields.cc;
    $("subjectInput").value = fields.subject;
    $("bodyInput").value = fields.body;
    showComposeError("");
    $("composer").hidden = false;
    document.body.classList.add("composing");
    (answering || $("toInput").value ? $("bodyInput") : $("toInput")).focus();
  }

  function composerValues() {
    return {
      draft: draft,
      to: $("toInput").value.trim(),
      cc: $("ccInput").value.trim(),
      subject: $("subjectInput").value.trim(),
      body: $("bodyInput").value
    };
  }

  function rememberComposer() {
    if (draft) store.set("titip.compose", JSON.stringify(composerValues()));
  }

  function closeComposer(forget) {
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
      if (!answering && !values.to) { showComposeError("Add at least one recipient."); $("toInput").focus(); return; }
      if (!values.body.trim() && mode !== "forward") { showComposeError("Write a message first."); $("bodyInput").focus(); return; }
    }
    var payload = { mode: mode, id: draft.id, action: action, to: values.to, cc: values.cc, subject: values.subject, body: values.body };
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
      var people = values.to.split(/[;,]/).map(function (part) { return part.trim(); }).filter(Boolean);
      if (!people.length) return "Sent";
      who = people[0] + (people.length > 1 ? " and " + (people.length - 1) + " more" : "");
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
    if (folderChanged) { view.rows = []; load(false); }
    if (id) openItem(id);
    else closeItem();
  }

  // ------------------------------------------------------------ wiring
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    var meta = document.querySelectorAll('meta[name="theme-color"]');
    Array.prototype.forEach.call(meta, function (tag) { tag.setAttribute("content", theme === "dark" ? "#12171d" : "#f4efe6"); });
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
  $("composeForm").oninput = rememberComposer;

  $("searchToggle").onclick = function () {
    var box = $("searchBox");
    box.hidden = !box.hidden;
    this.setAttribute("aria-expanded", String(!box.hidden));
    if (!box.hidden) $("q").focus();
    else if (view.query) { view.query = ""; $("q").value = ""; load(false); }
  };
  var searchTimer = 0;
  $("q").oninput = function () {
    clearTimeout(searchTimer);
    var value = this.value.trim();
    searchTimer = setTimeout(function () {
      if (value === view.query) return;
      view.query = value;
      view.loading = false;
      load(false);
    }, 450);
  };
  $("searchBox").onsubmit = function (event) { event.preventDefault(); };

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !$("composer").hidden) { rememberComposer(); closeComposer(false); toast("Kept on this phone. Tap Write to continue.", "Open", reopenSaved); }
  });

  function reopenSaved() {
    var saved = null;
    try { saved = JSON.parse(store.get("titip.compose") || "null"); } catch (error) { saved = null; }
    hideToast();
    if (saved && saved.draft) openComposer("", null, saved);
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      if (pending) { clearTimeout(pending.timer); var job = pending; pending = null; deliver(job.payload, job.values); }
    }
  });

  setInterval(function () {
    if (!document.hidden && !view.query && view.rows.length <= PAGE && $("composer").hidden) load(false, true);
  }, REFRESH_MS);

  window.addEventListener("hashchange", route);
  route();
  refreshCounts();
  if (store.get("titip.compose")) toast("You have an unfinished message.", "Open", reopenSaved, true);
}());
