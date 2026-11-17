/* The page on the PC: Outlook status, QR code, paired phones. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var countdown = 0;

  function call(path, post) {
    var options = { headers: { "X-Titip": "1" } };
    if (post) { options.method = "POST"; options.headers["Content-Type"] = "application/json"; options.body = "{}"; }
    return fetch(path, options).then(function (reply) { return reply.json(); });
  }

  function show(status) {
    var mail = $("mail");
    mail.textContent = "";
    var flag = document.createElement("span");
    var silent = status.mail.busyFor || 0;
    var waiting = silent >= 60
      ? "Outlook has not answered for " + Math.floor(silent / 60) + " min. If its window is frozen too, close Outlook and open it again. "
      : "Outlook is busy right now; the phone may wait a little. ";
    if (status.mail.ok) {
      flag.className = status.mail.busy && silent >= 60 ? "bad" : "good";
      flag.textContent = status.mail.demo ? "Demo mail is ready. " : status.mail.busy ? waiting : "Outlook is connected. ";
      mail.appendChild(flag);
      mail.appendChild(document.createTextNode(
        status.mail.account + " \u00b7 " + status.mail.counts.inbox.toLocaleString() + " in Inbox, " +
        status.mail.counts.sent.toLocaleString() + " sent, " + status.mail.counts.drafts + " drafts"));
    } else {
      flag.className = "bad";
      flag.textContent = status.mail.busy ? waiting : "Outlook is not answering. ";
      mail.appendChild(flag);
      if (!status.mail.busy) mail.appendChild(document.createTextNode(status.mail.error || ""));
    }
    var list = $("devices");
    list.textContent = "";
    if (!status.devices.length) {
      var none = document.createElement("li");
      none.className = "soft";
      none.textContent = "No phone is paired yet.";
      list.appendChild(none);
    }
    status.devices.forEach(function (device) {
      var item = document.createElement("li");
      item.textContent = device.name + " \u00b7 paired " + device.paired.replace("T", " ") + " \u00b7 last used " + device.seen.replace("T", " ");
      list.appendChild(item);
    });
    $("unpair").disabled = !status.devices.length;
    showAuto(status.autopilot, status.far);
    showFarMail(status.far);
    showUpdate(status.update, status.version);
    $("addresses").textContent = (status.addresses.length
      ? "Phone address: " + status.addresses.join(" or ") + ". "
      : "This PC has no network address a phone could use right now. ") + (status.httpsNote || "");
    $("warn").textContent = status.https
      ? "The first time, the phone warns that the connection is not private. That is expected: this PC made its own certificate. Tap Advanced, then Proceed (Safari: Show Details, then visit this website)."
      : "The link to the phone is not encrypted. Use it only on a network you trust.";
  }

  function showAuto(auto, far) {
    var line = $("auto");
    line.textContent = "";
    var flag = document.createElement("span");
    flag.className = auto.available && auto.on ? "good" : "bad";
    flag.textContent = !auto.available ? "Cannot run here. " : auto.on ? "On. " : "Paused. ";
    line.appendChild(flag);
    var minutes = auto.lastCheck ? Math.max(0, Math.round((auto.now - auto.lastCheck) / 60)) : -1;
    line.appendChild(document.createTextNode(!auto.available
      ? "It needs Google Chrome signed in to Gmail, and the Python packages pywin32 and comtypes."
      : (auto.busy ? auto.busy + ". " : "") +
        (auto.on ? "Watching " + auto.mailboxes.join(" and ") + ". " : "Nothing is read or sent. ") +
        (minutes >= 0 ? "Last look " + (minutes ? minutes + " min ago. " : "just now. ") : "") +
        (auto.waiting ? auto.waiting + " waiting for you." : "")));
    $("far").disabled = !far.ready;
    $("farNote").textContent = !far.ready
      ? "Not set up: this needs a relay address in remote.json in Titip's folder. The README says how to run your own."
      : far.error ? far.error
      : far.lastContact ? "The relay heard from this PC at " + new Date(far.lastContact * 1000).toLocaleTimeString() + "." : "";
  }

  var mailFarOn = false, updateOn = true;

  function showFarMail(far) {
    mailFarOn = !!far.mail;
    var line = $("mailFarLine");
    line.textContent = "";
    var flag = document.createElement("span");
    flag.className = far.mail && far.mailError ? "bad" : "good";
    flag.textContent = "Outlook mail from anywhere: " + (far.mail ? "on. " : "off. ");
    line.appendChild(flag);
    line.appendChild(document.createTextNode(!far.ready
      ? "It needs the relay, which is not set up."
      : !far.mail ? "Outlook mail stays on your own network."
      : far.mailError ? far.mailError
      : far.lastAsk ? "A phone last asked at " + new Date(far.lastAsk * 1000).toLocaleTimeString() + "."
      : "No phone has asked yet."));
    $("mailFar").disabled = !far.ready;
    $("mailFar").textContent = far.mail ? "Switch off" : "Switch on";
  }

  function showUpdate(update, version) {
    updateOn = !!update.on;
    var line = $("updateLine");
    line.textContent = "";
    var flag = document.createElement("span");
    var trouble = ["error", "failed", "skipped", "held"].indexOf(update.state) >= 0;
    flag.className = trouble ? "bad" : "good";
    flag.textContent = "Titip " + version + (update.commit ? " (" + update.commit + "). " : ". ");
    line.appendChild(flag);
    line.appendChild(document.createTextNode(update.note +
      (update.checked && update.on ? " Last looked at " + new Date(update.checked * 1000).toLocaleTimeString() + "." : "")));
    $("updateSwitch").disabled = !update.available;
    $("updateSwitch").textContent = update.on ? "Switch off" : "Switch on";
    $("updateNow").disabled = !update.available || !update.on;
  }

  function order(path, body) {
    return fetch(path, {
      method: "POST", headers: { "X-Titip": "1", "Content-Type": "application/json" }, body: JSON.stringify(body)
    }).then(function (reply) { return reply.json(); }).then(show);
  }

  $("mailFar").onclick = function () {
    if (!mailFarOn && !window.confirm("Let a phone that scanned the code read and send your Outlook mail from any network?")) return;
    order("/desk/mailfar", { on: !mailFarOn });
  };
  $("updateSwitch").onclick = function () { order("/desk/update", { on: !updateOn }); };
  $("updateNow").onclick = function () {
    $("updateNow").disabled = true;
    order("/desk/update", { check: true });
  };

  function farCode(renew) {
    fetch("/desk/far", {
      method: "POST", headers: { "X-Titip": "1", "Content-Type": "application/json" }, body: JSON.stringify({ renew: !!renew })
    }).then(function (reply) { return reply.json(); }).then(function (pairing) {
      if (!pairing.ready) return;
      $("farBox").hidden = false;
      $("farNew").hidden = false;
      $("far").textContent = "Hide the code";
      var box = $("farQr");
      box.textContent = "";
      if (pairing.qr) {
        var svg = document.importNode(new DOMParser().parseFromString(pairing.qr, "image/svg+xml").documentElement, true);
        svg.setAttribute("role", "img");
        svg.setAttribute("aria-label", "QR code to open Titip from any network");
        box.appendChild(svg);
      } else {
        box.textContent = "Install the Python package \u201csegno\u201d to see a QR code.";
      }
    });
  }

  $("far").onclick = function () {
    if (!$("farBox").hidden) { $("farBox").hidden = true; $("farNew").hidden = true; $("far").textContent = "Show the code"; return; }
    farCode(false);
  };
  $("farNew").onclick = function () {
    if (window.confirm("Make a new code? Phones that scanned the old one are cut off.")) farCode(true);
  };

  function refresh() { call("/desk/status").then(show).catch(function () {}); }

  $("pair").onclick = function () {
    call("/desk/pair", true).then(function (pairing) {
      $("pairing").hidden = false;
      var box = $("qr");
      box.textContent = "";
      if (pairing.qr) {
        var parsed = new DOMParser().parseFromString(pairing.qr, "image/svg+xml");
        var svg = document.importNode(parsed.documentElement, true);
        svg.setAttribute("role", "img");
        svg.setAttribute("aria-label", "QR code to pair a phone");
        box.appendChild(svg);
      } else {
        box.textContent = "Install the Python package \u201csegno\u201d to see a QR code. The link below works too.";
      }
      $("url").textContent = pairing.url;
      $("pair").textContent = "Show a new QR code";
      var left = pairing.expiresIn;
      clearInterval(countdown);
      countdown = setInterval(function () {
        left -= 1;
        $("expires").textContent = left > 0 ? "Valid for " + Math.ceil(left / 60) + " min, once." : "Expired. Make a new one.";
        if (left <= 0) clearInterval(countdown);
      }, 1000);
      $("expires").textContent = "Valid for 5 min, once.";
    });
  };

  $("unpair").onclick = function () {
    if (!window.confirm("Cut off every paired phone? They will need a new QR code.")) return;
    call("/desk/unpair", true).then(show);
  };

  var poll = 0;
  $("quit").onclick = function () {
    call("/desk/quit", true).then(function () {
      clearInterval(poll);
      clearInterval(countdown);
      $("mail").textContent = "Titip is stopped. Phones cannot reach your mail.";
      $("stopped").textContent = "Stopped. Start it again from the Titip shortcut.";
      $("pairing").hidden = true;
      $("expires").textContent = "";
      ["pair", "unpair", "quit", "far", "farNew", "mailFar", "updateSwitch", "updateNow"].forEach(function (id) { $(id).disabled = true; });
    });
  };

  refresh();
  poll = setInterval(refresh, 5000);
}());
