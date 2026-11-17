/* Trade the single-use code in the URL fragment for a device cookie, then open the mail app. */
(function () {
  "use strict";
  var code = location.hash.slice(1);
  var msg = document.getElementById("msg");
  if (!code) return;
  msg.textContent = "Pairing\u2026";
  fetch("/api/pair", {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-Titip": "1", "Content-Type": "application/json" },
    body: JSON.stringify({ code: code })
  }).then(function (reply) {
    return reply.json().then(function (data) { return [reply.ok, data]; });
  }).then(function (result) {
    if (result[0]) {
      history.replaceState(null, "", "/pair");
      location.replace("/");
    } else {
      msg.textContent = result[1].error || "Pairing failed.";
    }
  }).catch(function () {
    msg.textContent = "Couldn't reach Titip on your PC.";
  });
}());
