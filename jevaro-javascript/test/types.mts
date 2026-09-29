import { TypeSafeClient, choice, score, noul } from "jevaro";
import type { AsyncRecordBatchStreamReader } from "apache-arrow";

const client = new TypeSafeClient({ baseURL: "http://localhost:8000" });
const questions = { c: choice("Choose", { a: null, b: null }), s: score("Rate", ["low", "high"]), n: noul("Yes?") };
const reader: AsyncRecordBatchStreamReader = await client.systemOne({ states: ["one", { text: "two" }, ["three"]], questions });
for await (const batch of reader) console.log(batch.numRows);
await client.systemOne({ state: ["one", "two"], questions });
// @ts-expect-error state and states are mutually exclusive
await client.systemOne({ state: "one", states: ["two"], questions });
// @ts-expect-error a primitive number is not a state
await client.systemOne({ state: 12, questions });
