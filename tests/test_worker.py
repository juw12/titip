"""The relay itself (worker/titip-relay.js), run in Node with stand-ins for Cloudflare's KV and Durable Object."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HARNESS = r"""
import { handle, Asks } from "./relay.mjs";

function kv() {
  const kept = new Map();
  return {
    get: async (key) => (kept.has(key) ? kept.get(key) : null),
    put: async (key, value) => { kept.set(key, value); },
    delete: async (key) => { kept.delete(key); },
  };
}
function storage(alarms) {
  const kept = new Map();
  return {
    kept,
    get: async (key) => kept.get(key),
    put: async (key, value) => { kept.set(key, value); },
    delete: async (keys) => { for (const key of [].concat(keys)) kept.delete(key); },
    deleteAll: async () => { kept.clear(); },
    list: async ({ prefix, limit }) => new Map([...kept].filter(([key]) => key.startsWith(prefix)).sort().slice(0, limit)),
    setAlarm: async (when) => { alarms.push(when); },
  };
}
const alarms = [];
const objects = new Map();
const env = {
  RELAY: kv(),
  ASKS: {
    idFromName: (name) => name,
    get: (name) => {
      if (!objects.has(name)) objects.set(name, new Asks({ storage: storage(alarms) }));
      const object = objects.get(name);
      return { fetch: (url, init) => object.fetch(new Request(url, init)) };
    },
  },
};
const base = "https://relay.example/c/";
const CH = "channel-one-0123456789", OTHER = "channel-two-0123456789";
const WRITE = "w".repeat(32);

async function call(method, path, body, write, bindings) {
  const headers = write ? { "X-Titip-Write": write } : {};
  const reply = await handle(new Request(base + path, { method, body, headers }), bindings || env);
  return [reply.status, await reply.text(), reply.headers.get("Access-Control-Allow-Origin")];
}

const out = {};
out.claim = await call("PUT", CH + "/state", "sealed-state", WRITE);
out.read = await call("GET", CH + "/state");
out.overwriteByStranger = await call("PUT", CH + "/state", "evil", "x".repeat(32));

out.takeEmpty = await call("POST", CH + "/asks/take", null, WRITE);
out.ask1 = await call("POST", CH + "/ask/question-0001", "sealed-ask-1");
out.ask2 = await call("POST", CH + "/ask/question-0002", "sealed-ask-2");
out.askBadName = await call("POST", CH + "/ask/a b", "x");
out.askEmpty = await call("POST", CH + "/ask/question-0003", "");
out.askHuge = await call("POST", CH + "/ask/question-0004", "x".repeat(200001));
out.askOtherChannel = await call("POST", OTHER + "/ask/question-0001", "for-someone-else");
out.takeByStranger = await call("POST", CH + "/asks/take", null, "x".repeat(32));
out.takeWithoutToken = await call("POST", CH + "/asks/take");
out.take = await call("POST", CH + "/asks/take", null, WRITE);
out.takeAgain = await call("POST", CH + "/asks/take", null, WRITE);

out.answerNotThereYet = await call("GET", CH + "/answer/question-0001");
out.answerByStranger = await call("PUT", CH + "/answer/question-0001", "forged", "x".repeat(32));
out.answerHuge = await call("PUT", CH + "/answer/question-0001", "x".repeat(2000001), WRITE);
out.answer = await call("PUT", CH + "/answer/question-0001", "sealed-answer-1", WRITE);
out.answerFromOtherChannel = await call("GET", OTHER + "/answer/question-0001");
out.answerRead = await call("GET", CH + "/answer/question-0001");
out.answerReadTwice = await call("GET", CH + "/answer/question-0001");

for (let i = 0; i < 31; i += 1) out.flood = await call("POST", CH + "/ask/flood-" + String(i).padStart(4, "0"), "x");
out.alarms = alarms.length;
await objects.get(CH).alarm();
out.afterAlarm = await call("POST", CH + "/asks/take", null, WRITE);

await call("POST", CH + "/ask/question-0009", "left-behind");
out.forget = await call("DELETE", CH, null, WRITE);
out.stateAfterForget = await call("GET", CH + "/state");
out.leftAfterForget = objects.get(CH).storage.kept.size;

out.orders = await call("POST", OTHER + "/orders", "an-order");
out.preflight = await call("OPTIONS", CH + "/ask/question-0001");
out.withoutMailBinding = await call("POST", CH + "/ask/question-0001", "x", null, { RELAY: env.RELAY });
out.unknown = await call("GET", CH + "/nothing");
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
class Relay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as folder:
            shutil.copyfile(ROOT / "worker" / "titip-relay.js", Path(folder) / "relay.mjs")
            (Path(folder) / "check.mjs").write_text(HARNESS, encoding="utf-8")
            done = subprocess.run(
                [shutil.which("node"), "check.mjs"], cwd=folder, capture_output=True, text=True, encoding="utf-8", timeout=120,
            )
        if done.returncode:
            raise AssertionError(done.stderr[-3000:])
        cls.out = json.loads(done.stdout.strip().splitlines()[-1])

    def status(self, name):
        return self.out[name][0]

    def test_the_autopilot_routes_still_work(self):
        self.assertEqual(self.out["claim"][:2], [200, "ok"])
        self.assertEqual(self.out["read"][:2], [200, "sealed-state"])
        self.assertEqual(self.status("overwriteByStranger"), 403)
        self.assertEqual(self.status("orders"), 404)  # nobody owns that channel
        self.assertEqual(self.status("unknown"), 404)

    def test_questions_wait_for_the_pc_and_are_handed_over_once(self):
        self.assertEqual(json.loads(self.out["takeEmpty"][1]), {"asks": []})
        self.assertEqual((self.status("ask1"), self.status("ask2")), (200, 200))
        self.assertEqual(json.loads(self.out["take"][1]), {"asks": [
            {"id": "question-0001", "body": "sealed-ask-1"}, {"id": "question-0002", "body": "sealed-ask-2"},
        ]})
        self.assertEqual(json.loads(self.out["takeAgain"][1]), {"asks": []})

    def test_only_the_pc_that_owns_the_channel_takes_questions_and_answers(self):
        self.assertEqual(self.status("takeByStranger"), 404)
        self.assertEqual(self.status("takeWithoutToken"), 404)
        self.assertEqual(self.status("answerByStranger"), 404)

    def test_an_answer_is_read_once_and_only_on_its_own_channel(self):
        self.assertEqual(self.status("answerNotThereYet"), 204)
        self.assertEqual(self.status("answer"), 200)
        self.assertEqual(self.status("answerFromOtherChannel"), 204)
        self.assertEqual(self.out["answerRead"][:2], [200, "sealed-answer-1"])
        self.assertEqual(self.status("answerReadTwice"), 204)

    def test_sizes_names_and_the_number_waiting_are_limited(self):
        self.assertEqual(self.status("askBadName"), 404)
        self.assertEqual(self.status("askEmpty"), 413)
        self.assertEqual(self.status("askHuge"), 413)
        self.assertEqual(self.status("answerHuge"), 413)
        self.assertEqual(self.status("flood"), 429)  # the 31st question while 30 wait

    def test_everything_is_wiped_by_the_alarm_and_by_a_new_code(self):
        self.assertGreater(self.out["alarms"], 0)
        self.assertEqual(json.loads(self.out["afterAlarm"][1]), {"asks": []})
        self.assertEqual(self.status("forget"), 200)
        self.assertEqual(self.status("stateAfterForget"), 404)
        self.assertEqual(self.out["leftAfterForget"], 0)

    def test_a_phone_on_another_site_may_ask_and_an_old_deploy_just_says_no(self):
        self.assertEqual(self.out["ask1"][2], "*")
        self.assertEqual(self.out["answerNotThereYet"][2], "*")
        self.assertEqual(self.status("preflight"), 204)
        self.assertEqual(self.status("withoutMailBinding"), 404)


if __name__ == "__main__":
    unittest.main()
