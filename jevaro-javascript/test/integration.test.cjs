// Invoked against the offline HTTP fixture by jevaro-server/tests/test_proxy.py.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { tableFromArrays } = require("apache-arrow");
const { TypeSafeClient, choice, score, noul } = require("jevaro");

const run = process.env.JEVARO_TEST_URL ? test : test.skip;
const client = new TypeSafeClient({ baseURL: process.env.JEVARO_TEST_URL, apiKey: "jevaro-test-key" });
const questions = {
  department: choice("Which department?", { returns: null, other: null }),
  urgency: score("Urgency?", ["later", "today"]),
  refund: noul("Refund?"),
};

run("schema arrives while every upstream answer is blocked; cancel closes HTTP", async () => {
  const reader = await client.systemOne({ states: [{ id: 100, gate: true }, { id: 101, gate: true }], questions }, { timeout: 2000 });
  const field = reader.schema.fields.find(f => f.name === "department");
  assert.equal(field.metadata.get("ARROW:extension:name"), "jevaro.choice");
  assert.deepEqual(JSON.parse(field.metadata.get("ARROW:extension:metadata")), { labels: ["returns", "other"] });
  assert.equal((await reader.next()).value.numRows, 0);
  await reader.cancel();
});

run("all three Python Arrow extension storage types decode in JavaScript in input order", async () => {
  const reader = await client.systemOne({ states: [{ id: 1, delay: 0.08 }, { id: 2 }, { id: 3 }], questions });
  const rows = [];
  for await (const batch of reader) {
    for (const row of batch) rows.push(row);
  }
  assert.deepEqual(rows.map(r => r.refund), [0.001, 0.002, 0.003]);
  assert.deepEqual(rows.map(r => r.department.choice), [1, 0, 1]);
  assert.equal(rows[0].department.confidence, 0.12345678901234567);
  assert.equal(rows[0].urgency.score, 0.9876543210987654);
  assert.deepEqual([...rows[0].urgency.probabilities], [0.5, 0.5]);
});

run("Arrow Table rows become states in input order", async () => {
  const table = tableFromArrays({ id: Int32Array.from([31, 32, 33]), text: ["a", "b", "c"] });
  const rows = [];
  for await (const batch of await client.systemOne({ states: table, questions })) {
    for (const row of batch) rows.push(row);
  }
  assert.deepEqual(rows.map(r => r.refund), [0.031, 0.032, 0.033]);
  const byColumn = await client.systemOne({ states: table, stateColumn: "text", questions });
  assert.equal((await byColumn.readAll()).reduce((count, batch) => count + batch.numRows, 0), 3);
});

run("a Table from apache-arrow's ES module build reaches the server intact", async () => {
  const { tableFromArrays: esmTableFromArrays } = await import("apache-arrow");
  const table = esmTableFromArrays({ text: ["a", "b"] });
  const reader = await client.systemOne({ states: table, stateColumn: "text", questions });
  assert.equal((await reader.readAll()).reduce((count, batch) => count + batch.numRows, 0), 2);
});

run("upstream failure propagates as a broken HTTP stream", async () => {
  const reader = await client.systemOne({ states: [{ id: 5 }, { id: 6, statuses: [401] }], questions });
  await assert.rejects(reader.readAll());
});

run("caller abort signal remains active after receiving schema", async () => {
  const controller = new AbortController();
  const reader = await client.systemOne({ state: { id: 200, gate: true }, questions }, { signal: controller.signal });
  await reader.next(); // Initial zero-row batch.
  controller.abort();
  await assert.rejects(reader.next());
});
