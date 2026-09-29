const { test } = require("node:test");
const assert = require("node:assert/strict");
const { tableFromArrays, tableToIPC, RecordBatchReader } = require("apache-arrow");
const { TypeSafeClient, APIError, choice, noul, score } = require("jevaro");

function response(values = [0.12345678901234567]) {
  const bytes = tableToIPC(tableFromArrays({ answer: new Float64Array(values) }), "stream");
  let position = 0;
  return new Response(new ReadableStream({
    pull(controller) {
      if (position === bytes.length) return controller.close();
      const end = Math.min(position + 7, bytes.length);
      controller.enqueue(bytes.slice(position, end));
      position = end;
    },
  }), { headers: { "content-type": "application/vnd.apache.arrow.stream" } });
}

test("official helpers, ESM exports, TypeSafe arguments and native Arrow reader", async () => {
  let sent;
  const client = new TypeSafeClient({ apiKey: "test-key", baseURL: "http://jevaro.test/", fetch: async (url, options) => {
    sent = { url, ...options };
    return response();
  } });
  const questions = { c: choice("Choose", { a: null, b: null }), s: score("Rate", ["low", "high"]), n: noul("Yes?") };
  const reader = await client.systemOne({ state: { text: "hello" }, questions });
  assert.ok(reader instanceof RecordBatchReader);
  assert.equal(reader.schema.fields[0].name, "answer");
  assert.equal(sent.url, "http://jevaro.test/v1/systemone");
  assert.equal(sent.headers.get("authorization"), "Bearer test-key");
  assert.deepEqual(JSON.parse(sent.body), JSON.parse(JSON.stringify({ state: { text: "hello" }, questions, model: "jev-latest" })));
  const batches = await reader.readAll();
  assert.equal(batches[0].getChild("answer").get(0), 0.12345678901234567);
  const esm = await import("jevaro");
  assert.equal(esm.TypeSafeClient, TypeSafeClient);
  assert.equal(esm.noul, noul);
});

test("a clean but incomplete stream is rejected", async () => {
  const client = new TypeSafeClient({ fetch: async () => response() });
  const reader = await client.systemOne({ states: ["one", "two"], questions: { n: noul() } });
  await assert.rejects(reader.readAll(), /expected 2 rows, got 1/);
});

test("breaking iteration aborts the HTTP request", async () => {
  let signal;
  const client = new TypeSafeClient({ fetch: async (_, options) => { signal = options.signal; return response(); } });
  const reader = await client.systemOne({ state: "one", questions: { n: noul() } });
  for await (const batch of reader) { assert.equal(batch.numRows, 1); break; }
  assert.equal(signal.aborted, true);
});

test("structured HTTP errors and wrong content type", async () => {
  const bad = new TypeSafeClient({ fetch: async () => Response.json({ detail: "bad state" }, { status: 422 }) });
  await assert.rejects(bad.systemOne({ state: "a", questions: { n: noul() } }), error => {
    assert.ok(error instanceof APIError);
    assert.equal(error.status, 422);
    assert.equal(error.body.detail, "bad state");
    return true;
  });
  const json = new TypeSafeClient({ fetch: async () => Response.json({}) });
  await assert.rejects(json.systemOne({ state: "a", questions: { n: noul() } }), /Arrow IPC stream/);
});

test("request validation", async () => {
  const client = new TypeSafeClient({ fetch: () => assert.fail("must not fetch") });
  for (const input of [{}, { states: [] }, { state: "a", states: ["b"] }, { states: "bad" }]) {
    await assert.rejects(client.systemOne({ ...input, questions: { n: noul() } }), TypeError);
  }
});
