"use strict";

const arrow = require("apache-arrow");
const { box, choice, Image, image, noul, score, withAssets } = require("@typesafe-ai/sdk");

const { RecordBatchReader, Table } = arrow;
const MEDIA_TYPE = "application/vnd.apache.arrow.stream";
// The TypeSafe SDK marks its images and `withAssets` text with these symbols, so that
// objects from its ES module and CommonJS builds, or another copy, are recognized.
const IMAGE = Symbol.for("@typesafe-ai/sdk/image");
const TEXT_WITH_ASSETS = Symbol.for("@typesafe-ai/sdk/text-with-assets");
// Labels for image parts; the API detects each image's format from its bytes.
const IMAGE_TYPES = [["image/png", [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]], ["image/jpeg", [0xff, 0xd8, 0xff]]];

const isImage = value => typeof value === "object" && value !== null && (IMAGE in value || value instanceof Image);

// JSON for the request, with each image written as its reference. `found` collects the
// images, by reference, from the request's content and from `withAssets` text.
function encodeRequest(json, found) {
  return JSON.stringify(json, function (key, value) {
    const original = this[key];
    if (isImage(original)) found.set(String(original), original);
    else if (typeof original === "object" && original !== null && TEXT_WITH_ASSETS in original) {
      for (const asset of original.assets) found.set(String(asset), asset);
    }
    return value;
  });
}

// An image's file bytes as a Blob labeled with its format; a Blob's own type is ignored.
async function imageBlob(data) {
  const blob = data instanceof Blob;
  const head = blob ? new Uint8Array(await data.slice(0, 8).arrayBuffer()) : data.subarray(0, 8);
  const type = IMAGE_TYPES.find(([, signature]) => signature.every((byte, i) => head[i] === byte))?.[0];
  // Blob copies byte arrays, but can't read one backed by a SharedArrayBuffer.
  const bytes = blob || data.buffer instanceof ArrayBuffer ? data : data.slice();
  return new Blob([bytes], { type: type ?? "application/octet-stream" });
}

// apache-arrow ships separate CommonJS and ES module builds. Each build's
// `instanceof Table` accepts the other's tables, but its writer then drops
// dictionary index types. Write a Table with the build that created it.
async function writeStates(table) {
  if (Table.prototype.isPrototypeOf(table)) return arrow.tableToIPC(table, "stream");
  const esm = await import("apache-arrow");
  if (esm.Table.prototype.isPrototypeOf(table)) return esm.tableToIPC(table, "stream");
  throw new TypeError("Create Arrow states with the apache-arrow version this SDK uses");
}

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
    const arrow = plural && request.states instanceof Table;
    if (plural && !arrow && (!Array.isArray(request.states) || !request.states.length)) {
      throw new TypeError("states must be a nonempty array or an apache-arrow Table");
    }
    if (arrow && !request.states.numRows) throw new TypeError("Arrow states must have at least one row");
    if (Object.hasOwn(request, "stateColumn") && !arrow) throw new TypeError("stateColumn requires Arrow states");
    if (!request.questions || !Object.keys(request.questions).length) {
      throw new TypeError("questions must be a nonempty object");
    }
    if (request.rowsPerCall !== undefined && (!Number.isInteger(request.rowsPerCall) || request.rowsPerCall < 1)) {
      throw new TypeError("rowsPerCall must be a positive integer");
    }
    if (request.boxFormat !== undefined && !["typesafe", "geoarrow"].includes(request.boxFormat)) {
      throw new TypeError('boxFormat must be "typesafe" or "geoarrow"');
    }
    const images = new Map();
    for (const [name, asset] of Object.entries(request.assets ?? {})) {
      if (!name.trim()) throw new TypeError("Asset names must be nonempty strings");
      if (!isImage(asset)) throw new TypeError(`Asset "${name}" must be an image from image() or Image.fromBase64()`);
      images.set(name, asset);
    }
    const expectedRows = arrow ? request.states.numRows : plural ? request.states.length : 1;
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
      const { states, stateColumn, rowsPerCall, boxFormat, assets, ...fields } = request;
      const json = { ...fields, model: fields.model ?? this.defaultModel };
      if (!arrow && plural) json.states = states;
      if (stateColumn !== undefined) json.state_column = stateColumn;
      if (rowsPerCall !== undefined) json.rows_per_call = rowsPerCall;
      if (boxFormat !== undefined) json.box_format = boxFormat;
      let payload = encodeRequest(json, images);
      if (arrow || images.size) {
        // Arrow states and images travel in a multipart form beside the JSON request fields.
        // fetch sets the multipart Content-Type, including its boundary.
        headers.delete("Content-Type");
        const parts = [];
        if (images.size) {
          // Each image is a file part that the request's assets name, as TypeSafe accepts them.
          json.assets = {};
          for (const [name, asset] of images) {
            const part = `asset-${parts.length}`;
            json.assets[name] = { type: "image", part };
            parts.push([part, await imageBlob(asset.data)]);
          }
          payload = encodeRequest(json, new Map());
        }
        const form = new FormData();
        form.append("request", new Blob([payload], { type: "application/json" }));
        if (arrow) form.append("states", new Blob([await writeStates(states)], { type: MEDIA_TYPE }), "states.arrows");
        for (const [part, blob] of parts) form.append(part, blob, part);
        payload = form;
      }
      response = await this.fetch(`${this.baseURL}/v1/systemone`, { method: "POST", headers, signal, body: payload });
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
exports.box = box;
exports.choice = choice;
exports.Image = Image;
exports.image = image;
exports.noul = noul;
exports.score = score;
exports.withAssets = withAssets;
