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
    $("addresses").textContent = (status.addresses.length
      ? "Phone address: " + status.addresses.join(" or ") + ". "
      : "This PC has no network address a phone could use right now. ") + (status.httpsNote || "");
    $("warn").textContent = status.https
      ? "The first time, the phone warns that the connection is not private. That is expected: this PC made its own certificate. Tap Advanced, then Proceed (Safari: Show Details, then visit this website)."
      : "The link to the phone is not encrypted. Use it only on a network you trust.";
  }

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
      ["pair", "unpair", "quit"].forEach(function (id) { $(id).disabled = true; });
    });
  };

  refresh();
  poll = setInterval(refresh, 5000);
}());
