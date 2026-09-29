"use strict";

const { RecordBatchReader } = require("apache-arrow");
const { choice, noul, score } = require("@typesafe-ai/sdk");

const MEDIA_TYPE = "application/vnd.apache.arrow.stream";

class APIError extends Error {
  constructor(status, body) {
    super(`Jevaro returned HTTP ${status}`);
    this.name = "APIError";
    this.status = status;
    this.body = body;
  }
}

class TypeSafeClient {
  constructor(config = {}) {
    const env = typeof process === "undefined" ? {} : (process.env ?? {});
    this.apiKey = config.apiKey ?? env.TYPESAFE_API_KEY;
    this.baseURL = (config.baseURL ?? env.TYPESAFE_BASE_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");
    this.defaultModel = config.defaultModel ?? env.TYPESAFE_DEFAULT_MODEL ?? "jev-latest";
    this.defaultHeaders = config.defaultHeaders;
    this.timeout = config.timeout ?? 60_000;
    this.fetch = config.fetch ?? ((input, init) => globalThis.fetch(input, init));
  }

  async systemOne(request, options = {}) {
    const singular = Object.hasOwn(request, "state");
    const plural = Object.hasOwn(request, "states");
    if (singular === plural) throw new TypeError("Supply exactly one of state or states");
    if (plural && (!Array.isArray(request.states) || !request.states.length)) {
      throw new TypeError("states must be a nonempty array");
    }
    if (!request.questions || !Object.keys(request.questions).length) {
      throw new TypeError("questions must be a nonempty object");
    }
    const expectedRows = plural ? request.states.length : 1;
    const timeout = options.timeout ?? this.timeout;
    if (!Number.isFinite(timeout) || timeout <= 0) throw new TypeError("timeout must be positive milliseconds");
    const controller = new AbortController();
    const signal = options.signal
      ? AbortSignal.any([controller.signal, options.signal]) : controller.signal;
    const timer = setTimeout(() => controller.abort(new Error("Timed out opening Jevaro stream")), timeout);
    timer.unref?.();
    let response;
    let reader;
    try {
      const headers = new Headers(this.defaultHeaders);
      headers.set("Content-Type", "application/json");
      headers.set("Accept", MEDIA_TYPE);
      if (this.apiKey) headers.set("Authorization", `Bearer ${this.apiKey}`);
      new Headers(options.headers).forEach((value, key) => headers.set(key, value));
      response = await this.fetch(`${this.baseURL}/v1/systemone`, {
        method: "POST", headers, signal,
        body: JSON.stringify({ ...request, model: request.model ?? this.defaultModel }),
      });
      if (!response.ok) {
        const text = await response.text();
        let body;
        try { body = JSON.parse(text); } catch { body = text; }
        throw new APIError(response.status, body);
      }
      if (response.headers.get("content-type")?.split(";", 1)[0] !== MEDIA_TYPE || !response.body) {
        throw new Error("Expected an Arrow IPC stream from Jevaro");
      }
      reader = await RecordBatchReader.from(response.body);
      await reader.open();
      if (!reader.schema || !reader.isStream()) throw new Error("Missing Arrow stream schema");

      // Keep a native Arrow reader, including its schema, readAll and stream adapters.
      // Check row count even if a peer closes cleanly at an IPC message boundary.
      let rows = 0;
      let cancelled = false;
      const next = reader.next.bind(reader);
      const cancel = reader.cancel.bind(reader);
      reader.cancel = async () => {
        cancelled = true;
        controller.abort();
        try { await cancel(); } catch { /* An aborted HTTP body can reject on close. */ }
      };
      reader.next = async () => {
        if (cancelled) return { done: true, value: undefined };
        try {
          const result = await next();
          if (!result.done) rows += result.value.numRows;
          if ((result.done && rows !== expectedRows) || rows > expectedRows) {
            throw new Error(`Incomplete Jevaro stream: expected ${expectedRows} rows, got ${rows}`);
          }
          return result;
        } catch (error) {
          await reader.cancel();
          throw error;
        }
      };
      reader.return = async () => {
        await reader.cancel();
        return { done: true, value: undefined };
      };
      reader[Symbol.asyncIterator] = () => reader;
      return reader;
    } catch (error) {
      controller.abort();
      if (reader) await reader.cancel().catch(() => {});
      else if (response?.body && !response.body.locked) await response.body.cancel().catch(() => {});
      throw error;
    } finally {
      // This timeout covers opening the schema, not the duration of a large batch.
      // The caller's AbortSignal remains active for the lifetime of the stream.
      clearTimeout(timer);
    }
  }
}

exports.TypeSafeClient = TypeSafeClient;
exports.APIError = APIError;
exports.choice = choice;
exports.noul = noul;
exports.score = score;
