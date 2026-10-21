// A fake TypeSafe API over TLS for free local benchmarks: HTTP/2 with 100 streams per connection.
// Usage: node fake_typesafe.cjs PORT CERT_DIR BASE_MS PER_QUESTION_MS [SPREAD [BURST TOKENS_PER_SECOND]]
// Each call takes (BASE_MS + PER_QUESTION_MS * questions) * lognormal(sigma=SPREAD).
// With BURST and TOKENS_PER_SECOND, input tokens come from a token bucket, as at TypeSafe:
// a call that finds too few tokens gets a 429 with retry-after-ms of 800 to 1,200 after 35 ms.
const fs = require("node:fs");
const http2 = require("node:http2");
const path = require("node:path");
const zlib = require("node:zlib");

const [port, certDir] = [Number(process.argv[2]), process.argv[3]];
const [baseMs, perQuestionMs, spread, burst, tokensPerSecond] = process.argv.slice(4, 9).map(Number);
let active = 0, peak = 0, calls = 0, questions = 0, rejected = 0;
let bucket = burst, filled = performance.now();

function take(tokens) {
  if (!tokensPerSecond) return true;
  const now = performance.now();
  bucket = Math.min(burst, bucket + (now - filled) / 1000 * tokensPerSecond);
  filled = now;
  if (bucket < tokens) return false;
  bucket -= tokens;
  return true;
}

function lognormal() {
  if (!spread) return 1;
  const u = 1 - Math.random(), v = Math.random();
  return Math.exp(spread * Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v));
}

function answer(payload) {
  const answers = {};
  for (const [name, q] of Object.entries(payload.questions)) {
    if (q.type === "noul") {
      answers[name] = { type: "noul", noul: 0.25 };
    } else if (q.type === "choice") {
      const labels = Object.keys(q.criteria);
      answers[name] = { type: "choice", choice: labels[0], confidence: 0.9,
        probabilities: Object.fromEntries(labels.map((label, i) => [label, i === 0 ? 1 : 0])) };
    } else {
      answers[name] = { type: "score", score: 1, confidence: 0.8,
        legend: Object.fromEntries(q.criteria.map((level, i) => [String(i), level])),
        probabilities: Object.fromEntries(q.criteria.map((_, i) => [String(i), 1 / q.criteria.length])) };
    }
  }
  return answers;
}

const server = http2.createSecureServer({
  key: fs.readFileSync(path.join(certDir, "key.pem")),
  cert: fs.readFileSync(path.join(certDir, "cert.pem")),
  settings: { maxConcurrentStreams: 100 },
}, (req, res) => {
  if (req.url === "/__stats") {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ calls, questions, peak, rejected }));
    return;
  }
  const chunks = [];
  req.on("data", chunk => chunks.push(chunk));
  req.on("end", () => {
    let body = Buffer.concat(chunks);
    // The TypeSafe SDK compresses request bodies of 1 KiB or more with zstd.
    if (req.headers["content-encoding"] === "zstd") body = zlib.zstdDecompressSync(body);
    const payload = JSON.parse(body);
    const count = Object.keys(payload.questions).length;
    const tokens = Math.round(body.length / 4.3);
    if (!take(tokens)) {
      rejected++;
      setTimeout(() => {
        res.writeHead(429, { "content-type": "application/json",
          "retry-after-ms": String(Math.round(800 + 400 * Math.random())), "retry-after": "1" });
        res.end(JSON.stringify({ detail: { error_type: "api_usage_error",
          message: "Rate limit exceeded. Please retry shortly." } }));
      }, 35);
      return;
    }
    active++; peak = Math.max(peak, active); calls++; questions += count;
    setTimeout(() => {
      active--;
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ model: "jev-fake", answers: answer(payload),
        usage: { input_tokens: tokens, output_tokens: 0 } }));
    }, (baseMs + perQuestionMs * count) * lognormal());
  });
});
server.listen(port, "127.0.0.1", () => console.log(`fake TypeSafe on ${port}`));
