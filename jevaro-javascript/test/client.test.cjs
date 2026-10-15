const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { createRequire } = require("node:module");
const vm = require("node:vm");
const { tableFromArrays, tableFromIPC, tableToIPC, RecordBatchReader } = require("apache-arrow");
const { TypeSafeClient, APIError, box, choice, image, Image, noul, score, withAssets } = require("jevaro");

const PNG = Uint8Array.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2]);
const JPEG = Uint8Array.from([0xff, 0xd8, 0xff, 3, 4]);

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
  for (const name of ["box", "image", "Image", "withAssets"]) assert.equal(typeof esm[name], "function", name);
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

test("a Table from apache-arrow's ES module build is written by that build", async () => {
  const esm = await import("apache-arrow");
  assert.notEqual(esm.Table, require("apache-arrow").Table);
  let sent;
  const client = new TypeSafeClient({ fetch: async (url, options) => { sent = options.body; return response(); } });
  // tableFromArrays dictionary-encodes strings; the other build's writer would drop the index type.
  const table = esm.tableFromArrays({ text: ["one"] });
  await (await client.systemOne({ states: table, questions: { n: noul() } })).readAll();
  assert.deepEqual(new Uint8Array(await sent.get("states").arrayBuffer()), esm.tableToIPC(table, "stream"));
});

test("an Arrow Table sets the expected row count", async () => {
  const client = new TypeSafeClient({ fetch: async () => response() });
  const reader = await client.systemOne({ states: tableFromArrays({ text: ["one", "two"] }), questions: { n: noul() } });
  await assert.rejects(reader.readAll(), /expected 2 rows, got 1/);
});

test("rowsPerCall is sent as rows_per_call in JSON and in a multipart request part", async () => {
  const bodies = [];
  const client = new TypeSafeClient({ fetch: async (url, options) => { bodies.push(options.body); return response([0.25, 0.75]); } });
  await (await client.systemOne({ states: ["one", "two"], questions: { n: noul() }, rowsPerCall: 32 })).readAll();
  assert.deepEqual(JSON.parse(bodies[0]), JSON.parse(JSON.stringify({
    states: ["one", "two"], questions: { n: noul() }, model: "jev-latest", rows_per_call: 32,
  })));
  const table = tableFromArrays({ text: ["one", "two"] });
  await (await client.systemOne({ states: table, stateColumn: "text", questions: { n: noul() }, rowsPerCall: 8 })).readAll();
  assert.deepEqual(JSON.parse(await bodies[1].get("request").text()), JSON.parse(JSON.stringify({
    questions: { n: noul() }, model: "jev-latest", state_column: "text", rows_per_call: 8,
  })));
});

test("request validation", async () => {
  const client = new TypeSafeClient({ fetch: () => assert.fail("must not fetch") });
  for (const input of [{}, { states: [] }, { state: "a", states: ["b"] }, { states: "bad" },
    { states: { text: ["a"] } }, { states: ["a"], stateColumn: "text" }, { state: "a", stateColumn: "text" },
    { states: tableFromArrays({ text: [] }) }, { states: ["a"], rowsPerCall: 0 },
    { states: ["a"], rowsPerCall: 1.5 }, { states: ["a"], rowsPerCall: "2" }]) {
    await assert.rejects(client.systemOne({ ...input, questions: { n: noul() } }), TypeError);
  }
});

test("images are sent as file parts that the request's assets name", async () => {
  let sent;
  const client = new TypeSafeClient({ fetch: async (url, options) => { sent = options.body; return response([0.25, 0.75]); } });
  const photo = image(PNG);
  const badge = image(new Blob([JPEG], { type: "image/webp" }));
  const thumb = Image.fromBase64(Buffer.from("other").toString("base64"));
  const logo = image(PNG.slice(0, 9));
  // An image from the SDK's ES module build is recognized too.
  const esmImage = (await import("@typesafe-ai/sdk")).image(JPEG);
  const reader = await client.systemOne({
    states: [{ photo }, withAssets`Shelf: ${esmImage}`, "Logo: <logo>"],
    questions: {
      mug: box("the mug in `photo`"),
      same: noul({ question: "Is `photo` the person in `badge`?", badge }),
      gesture: choice("Which gesture?", { up: thumb, none: null }),
    },
    assets: { "<logo>": logo },
    boxFormat: "geoarrow",
  });
  await reader.readAll().catch(() => {});
  assert.deepEqual([...sent.keys()], ["request", "asset-0", "asset-1", "asset-2", "asset-3", "asset-4"]);
  const request = JSON.parse(await sent.get("request").text());
  assert.deepEqual(request.states, [{ photo: String(photo) }, `Shelf: ${esmImage}`, "Logo: <logo>"]);
  assert.deepEqual(request.questions.mug, { type: "box", instructions: "the mug in `photo`" });
  assert.equal(request.questions.same.instructions.badge, String(badge));
  assert.deepEqual(request.questions.gesture.criteria, { up: String(thumb), none: null });
  assert.equal(request.box_format, "geoarrow");
  assert.equal(request.boxFormat, undefined);
  // Named assets first, then images in the order the JSON holds them: questions, then states.
  const names = ["<logo>", String(badge), String(thumb), String(photo), String(esmImage)];
  assert.deepEqual(request.assets, Object.fromEntries(names.map((name, i) => [name, { type: "image", part: `asset-${i}` }])));
  const parts = await Promise.all([0, 1, 2, 3, 4].map(async i => {
    const part = sent.get(`asset-${i}`);
    return [part.name, part.type, [...new Uint8Array(await part.arrayBuffer())]];
  }));
  assert.deepEqual(parts, [
    ["asset-0", "image/png", [...PNG.slice(0, 9)]], ["asset-1", "image/jpeg", [...JPEG]],
    ["asset-2", "application/octet-stream", [...Buffer.from("other")]], ["asset-3", "image/png", [...PNG]],
    ["asset-4", "image/jpeg", [...JPEG]],
  ]);
});

test("images beside an Arrow Table, and JSON without images", async () => {
  const bodies = [];
  const client = new TypeSafeClient({ fetch: async (url, options) => { bodies.push(options.body); return response([0.25, 0.75]); } });
  const mug = image(PNG);
  const table = tableFromArrays({ text: ["one", "two"] });
  await (await client.systemOne({ states: table, questions: { kind: choice("Which mug?", { this: mug, none: null }) } })).readAll();
  assert.deepEqual([...bodies[0].keys()], ["request", "states", "asset-0"]);
  assert.deepEqual(JSON.parse(await bodies[0].get("request").text()).assets, { [String(mug)]: { type: "image", part: "asset-0" } });
  await (await client.systemOne({ states: ["one", "two"], questions: { n: noul() }, boxFormat: "typesafe" })).readAll();
  assert.equal(typeof bodies[1], "string");
  assert.equal(JSON.parse(bodies[1]).box_format, "typesafe");
});

test("image request validation", async () => {
  const client = new TypeSafeClient({ fetch: () => assert.fail("must not fetch") });
  for (const input of [{ boxFormat: "pixels" }, { assets: { "": image(PNG) } }, { assets: { photo: PNG } }]) {
    await assert.rejects(client.systemOne({ state: "a", questions: { n: noul() }, ...input }), TypeError);
  }
});

test("an image in assets is written by its name wherever it appears, and sent once", async () => {
  let sent;
  const client = new TypeSafeClient({ fetch: async (url, options) => { sent = options.body; return response(); } });
  const photo = image(PNG);
  await (await client.systemOne({
    state: { photo, caption: withAssets`See ${photo}.` },
    questions: { same: noul({ question: "Same as `example`?", example: photo }) },
    assets: { "<p>": photo },
  })).readAll();
  assert.deepEqual([...sent.keys()], ["request", "asset-0"]);
  const request = JSON.parse(await sent.get("request").text());
  assert.deepEqual(request.state, { photo: "<p>", caption: "See <p>." });
  assert.equal(request.questions.same.instructions.example, "<p>");
  assert.deepEqual(request.assets, { "<p>": { type: "image", part: "asset-0" } });
});
