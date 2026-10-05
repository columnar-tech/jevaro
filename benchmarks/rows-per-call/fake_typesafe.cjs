// A fake TypeSafe API over TLS for free local benchmarks: HTTP/2 with 100 streams per connection.
// Usage: node fake_typesafe.cjs PORT CERT_DIR BASE_MS PER_QUESTION_MS [SPREAD]
// Each call takes (BASE_MS + PER_QUESTION_MS * questions) * lognormal(sigma=SPREAD).
const fs = require("node:fs");
const http2 = require("node:http2");
const path = require("node:path");

const [port, certDir] = [Number(process.argv[2]), process.argv[3]];
const [baseMs, perQuestionMs, spread] = process.argv.slice(4, 7).map(Number);
let active = 0, peak = 0, calls = 0, questions = 0;

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
    res.end(JSON.stringify({ calls, questions, peak }));
    return;
  }
  const chunks = [];
  req.on("data", chunk => chunks.push(chunk));
  req.on("end", () => {
    const body = Buffer.concat(chunks);
    const payload = JSON.parse(body);
    const count = Object.keys(payload.questions).length;
    active++; peak = Math.max(peak, active); calls++; questions += count;
    setTimeout(() => {
      active--;
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ model: "jev-fake", answers: answer(payload),
        usage: { input_tokens: Math.round(body.length / 4.3), output_tokens: 0 } }));
    }, (baseMs + perQuestionMs * count) * lognormal());
  });
});
server.listen(port, "127.0.0.1", () => console.log(`fake TypeSafe on ${port}`));
