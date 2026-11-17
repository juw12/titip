/* Titip relay: holds one encrypted state and a short list of encrypted orders per PC.

   It cannot read either. The key never reaches it (see titip/remote.py). What it does know: a random
   channel name, the size of the state, and when the PC and the phone last asked.

     PUT    /c/<channel>/state        PC, X-Titip-Write: store the state (the first write claims the channel)
     GET    /c/<channel>/state        phone: read the state
     POST   /c/<channel>/orders       phone: leave an order (encrypted, at most 20 waiting)
     POST   /c/<channel>/orders/take  PC, X-Titip-Write: read the orders and clear them
     DELETE /c/<channel>              PC, X-Titip-Write: forget this channel

   Storage: one KV namespace bound as RELAY. Everything expires after 7 days without a write.
   Deploy:  cd worker && npx wrangler kv namespace create RELAY   (put the id in wrangler.toml)
            npx wrangler deploy
*/

const MAX_STATE = 300000;
const MAX_ORDER = 8000;
const MAX_ORDERS = 20;
const KEEP_SECONDS = 7 * 24 * 3600;
const CHANNEL = /^[A-Za-z0-9_-]{16,64}$/;
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, X-Titip-Write",
  "Access-Control-Max-Age": "86400",
};

function reply(status, body, type) {
  return new Response(body, {
    status: status,
    headers: Object.assign({ "Content-Type": type || "text/plain", "Cache-Control": "no-store" }, CORS),
  });
}

async function digest(text) {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(bytes), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function isOwner(request, env, channel, claim) {
  const token = request.headers.get("X-Titip-Write") || "";
  if (token.length < 20) return false;
  const hash = await digest(token);
  const known = await env.RELAY.get("owner:" + channel);
  if (known === null) {
    if (!claim) return false;
    await env.RELAY.put("owner:" + channel, hash, { expirationTtl: KEEP_SECONDS * 4 });
    return true;
  }
  return known === hash;
}

export async function handle(request, env) {
  if (request.method === "OPTIONS") return reply(204, null);
  const parts = new URL(request.url).pathname.split("/").filter(Boolean);
  if (parts[0] !== "c" || !CHANNEL.test(parts[1] || "")) return reply(404, "Not found.");
  const channel = parts[1];
  const what = parts.slice(2).join("/");
  const method = request.method;

  if (what === "state" && method === "PUT") {
    const body = await request.text();
    if (!body || body.length > MAX_STATE) return reply(413, "Too large.");
    if (!(await isOwner(request, env, channel, true))) return reply(403, "Not yours.");
    await env.RELAY.put("state:" + channel, body, { expirationTtl: KEEP_SECONDS });
    return reply(200, "ok");
  }
  if (what === "state" && method === "GET") {
    const state = await env.RELAY.get("state:" + channel);
    return state === null ? reply(404, "Nothing here.") : reply(200, state);
  }
  if (what === "orders" && method === "POST") {
    const body = await request.text();
    if (!body || body.length > MAX_ORDER) return reply(413, "Too large.");
    if ((await env.RELAY.get("owner:" + channel)) === null) return reply(404, "Nothing here.");
    const waiting = JSON.parse((await env.RELAY.get("orders:" + channel)) || "[]");
    if (waiting.length >= MAX_ORDERS) return reply(429, "The PC has not picked up the last orders yet.");
    waiting.push(body);
    await env.RELAY.put("orders:" + channel, JSON.stringify(waiting), { expirationTtl: 3600 });
    return reply(200, "ok");
  }
  if (what === "orders/take" && method === "POST") {
    if (!(await isOwner(request, env, channel, false))) return reply(404, "Nothing here.");
    const waiting = await env.RELAY.get("orders:" + channel);
    if (waiting === null || waiting === "[]") return reply(200, '{"orders":[]}', "application/json");
    await env.RELAY.delete("orders:" + channel);
    return reply(200, '{"orders":' + waiting + "}", "application/json");
  }
  if (what === "" && method === "DELETE") {
    if (!(await isOwner(request, env, channel, false))) return reply(404, "Nothing here.");
    await Promise.all(["state:", "orders:", "owner:"].map((prefix) => env.RELAY.delete(prefix + channel)));
    return reply(200, "ok");
  }
  return reply(404, "Not found.");
}

export default { fetch: handle };
