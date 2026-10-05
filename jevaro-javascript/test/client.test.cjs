const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { createRequire } = require("node:module");
const vm = require("node:vm");
const { tableFromArrays, tableFromIPC, tableToIPC, RecordBatchReader } = require("apache-arrow");
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

test("runs without process and calls global fetch with its browser receiver", async () => {
  const entry = require.resolve("jevaro");
  let sent, browserGlobal;
  const context = vm.createContext({
    exports: {}, require: createRequire(entry),
    Headers, AbortController, AbortSignal, setTimeout, clearTimeout,
    fetch: async function (url, options) {
      assert.equal(this, browserGlobal);
      sent = { url, ...options };
      return response([0.25, 0.75]);
    },
  });
  browserGlobal = vm.runInContext("globalThis", context);
  vm.runInContext(readFileSync(entry, "utf8"), context);
  const client = new context.exports.TypeSafeClient();
  assert.equal(client.apiKey, undefined);
  assert.equal(client.defaultModel, "jev-latest");
  const reader = await client.systemOne({ states: ["one", "two"], questions: { n: noul() } });
  const values = [];
  for await (const batch of reader) for (const row of batch) values.push(row.answer);
  assert.deepEqual(values, [0.25, 0.75]);
  assert.equal(sent.url, "http://127.0.0.1:8000/v1/systemone");
  assert.equal(sent.headers.has("authorization"), false);
});

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

test("an Arrow Table is sent as a multipart form with JSON request fields", async () => {
  let sent;
  const client = new TypeSafeClient({ apiKey: "test-key", defaultHeaders: { "Content-Type": "text/plain" }, fetch: async (url, options) => {
    sent = { url, ...options };
    return response([0.25, 0.75]);
  } });
  const table = tableFromArrays({ id: Int32Array.from([1, 2]), text: ["one", "two"] });
  const reader = await client.systemOne({ states: table, stateColumn: "text", questions: { n: noul("Yes?") } });
  assert.equal((await reader.readAll()).reduce((rows, batch) => rows + batch.numRows, 0), 2);
  assert.ok(sent.body instanceof FormData);
  assert.equal(sent.headers.has("content-type"), false);
  assert.equal(sent.headers.get("authorization"), "Bearer test-key");
  assert.deepEqual([...sent.body.keys()], ["request", "states"]);
  const request = sent.body.get("request");
  assert.equal(request.type, "application/json");
  assert.deepEqual(JSON.parse(await request.text()), JSON.parse(JSON.stringify({
    questions: { n: noul("Yes?") }, model: "jev-latest", state_column: "text",
  })));
  const states = sent.body.get("states");
  assert.equal(states.type, "application/vnd.apache.arrow.stream");
  assert.equal(states.name, "states.arrows");
  const decoded = tableFromIPC(new Uint8Array(await states.arrayBuffer()));
  assert.deepEqual(decoded.toArray().map(row => row.toJSON()), [{ id: 1, text: "one" }, { id: 2, text: "two" }]);
});

test("an Arrow Table sets the expected row count", async () => {
  const client = new TypeSafeClient({ fetch: async () => response() });
  const reader = await client.systemOne({ states: tableFromArrays({ text: ["one", "two"] }), questions: { n: noul() } });
  await assert.rejects(reader.readAll(), /expected 2 rows, got 1/);
});

test("request validation", async () => {
  const client = new TypeSafeClient({ fetch: () => assert.fail("must not fetch") });
  for (const input of [{}, { states: [] }, { state: "a", states: ["b"] }, { states: "bad" },
    { states: { text: ["a"] } }, { states: ["a"], stateColumn: "text" }, { state: "a", stateColumn: "text" },
    { states: tableFromArrays({ text: [] }) }]) {
    await assert.rejects(client.systemOne({ ...input, questions: { n: noul() } }), TypeError);
  }
});
