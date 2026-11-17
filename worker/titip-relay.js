/* Titip relay: holds encrypted things for one PC and its phones, and can read none of them.

   The key never reaches it (see titip/remote.py). What it does know: a random channel name, sizes,
   and when the PC and the phone last asked.

   Autopilot page (one state, a short list of orders; kept in KV):
     PUT    /c/<channel>/state        PC, X-Titip-Write: store the state (the first write claims the channel)
     GET    /c/<channel>/state        phone: read the state
     POST   /c/<channel>/orders       phone: leave an order (encrypted, at most 20 waiting)
     POST   /c/<channel>/orders/take  PC, X-Titip-Write: read the orders and clear them
     DELETE /c/<channel>              PC, X-Titip-Write: forget this channel

   Mail from anywhere (question and answer, seconds apart; kept in a Durable Object because KV can
   take a minute to show a write in another city):
     POST   /c/<channel>/ask/<id>     phone: leave a question (encrypted, at most 30 waiting)
     POST   /c/<channel>/asks/take    PC, X-Titip-Write: read the questions and clear them
     PUT    /c/<channel>/answer/<id>  PC, X-Titip-Write: leave the answer
     GET    /c/<channel>/answer/<id>  phone: read the answer once (204 while there is none)

   Storage: one KV namespace bound as RELAY (expires after 7 days without a write) and one Durable
   Object class bound as ASKS (wipes itself 5 minutes after the last write).
   Deploy:  cd worker && npx wrangler kv namespace create RELAY   (put the id in wrangler.toml)
            npx wrangler deploy
*/

const MAX_STATE = 300000;
const MAX_ORDER = 8000;
const MAX_ORDERS = 20;
const MAX_ASK = 200000;
const MAX_ASKS = 30;
const MAX_ANSWER = 2000000;
const ASKS_LIVE_MS = 5 * 60 * 1000;
const KEEP_SECONDS = 7 * 24 * 3600;
const CHANNEL = /^[A-Za-z0-9_-]{16,64}$/;
const ASK_ID = /^[A-Za-z0-9_-]{8,40}$/;
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

/* One per channel. Questions wait under "q:<id>", answers under "a:<id>". */
export class Asks {
  constructor(state) {
    this.storage = state.storage;
  }

  async fetch(request) {
    const parts = new URL(request.url).pathname.split("/").filter(Boolean);
    const what = parts[0];
    const id = parts[1] || "";
    const method = request.method;
    if (what === "ask" && method === "POST") {
      const body = await request.text();
      const waiting = await this.storage.list({ prefix: "q:", limit: MAX_ASKS });
      if (waiting.size >= MAX_ASKS) return reply(429, "The PC has not picked up the last questions yet.");
      await this.storage.put("q:" + id, body);
      await this.storage.setAlarm(Date.now() + ASKS_LIVE_MS);
      return reply(200, "ok");
    }
    if (what === "take" && method === "POST") {
      const waiting = await this.storage.list({ prefix: "q:", limit: MAX_ASKS });
      if (!waiting.size) return reply(200, '{"asks":[]}', "application/json");
      await this.storage.delete(Array.from(waiting.keys()));
      const asks = Array.from(waiting, (pair) => ({ id: pair[0].slice(2), body: pair[1] }));
      return reply(200, JSON.stringify({ asks: asks }), "application/json");
    }
    if (what === "answer" && method === "PUT") {
      await this.storage.put("a:" + id, await request.text());
      await this.storage.setAlarm(Date.now() + ASKS_LIVE_MS);
      return reply(200, "ok");
    }
    if (what === "answer" && method === "GET") {
      const answer = await this.storage.get("a:" + id);
      if (answer === undefined) return reply(204, null);
      await this.storage.delete("a:" + id);
      return reply(200, answer);
    }
    if (what === "forget" && method === "POST") {
      await this.storage.deleteAll();
      return reply(200, "ok");
    }
    return reply(404, "Not found.");
  }

  async alarm() {
    await this.storage.deleteAll();
  }
}

function asks(env, channel, path, method, body) {
  const stub = env.ASKS.get(env.ASKS.idFromName(channel));
  return stub.fetch("https://asks/" + path, { method: method, body: body });
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
    if (env.ASKS) await asks(env, channel, "forget", "POST");
    return reply(200, "ok");
  }

  // ---- mail from anywhere
  if (!env.ASKS) return reply(404, "Not found.");
  const id = parts[3] || "";
  if (parts[2] === "ask" && parts.length === 4 && method === "POST") {
    const body = await request.text();
    if (!ASK_ID.test(id)) return reply(404, "Not found.");
    if (!body || body.length > MAX_ASK) return reply(413, "Too large.");
    return asks(env, channel, "ask/" + id, "POST", body);
  }
  if (what === "asks/take" && method === "POST") {
    if (!(await isOwner(request, env, channel, false))) return reply(404, "Nothing here.");
    return asks(env, channel, "take", "POST");
  }
  if (parts[2] === "answer" && parts.length === 4 && ASK_ID.test(id)) {
    if (method === "GET") return asks(env, channel, "answer/" + id, "GET");
    if (method === "PUT") {
      const body = await request.text();
      if (!body || body.length > MAX_ANSWER) return reply(413, "Too large.");
      if (!(await isOwner(request, env, channel, false))) return reply(404, "Nothing here.");
      return asks(env, channel, "answer/" + id, "PUT", body);
    }
  }
  return reply(404, "Not found.");
}

export default { fetch: handle };
