# Images: multipart vs base64

Should a client send Jev images as `multipart/form-data` file parts or as
base64 in the JSON body? `bench.py` sends the same requests to the TypeSafe
API both ways, with and without zstd compression of the body. It records
bytes sent, client encoding time, latency, TypeSafe's processing time,
throughput, tokens, and answers.

## Findings

**Send images as multipart.** Base64 never made TypeSafe slower, and the
answers were the same. But base64 sends a third more bytes, which costs
throughput whenever the uplink is full. It also leaves a quarter less room
under the 32 MiB body limit, and zstd doesn't recover that room.

Two runs used `jev-1.14.0` over HTTP/1.1 from an Apple M3 laptop:

| Run | Link (`networkQuality`) | Cloudflare location | Requests |
| --- | --- | --- | --- |
| `wifi-hotspot-2026-10-15` | Wi-Fi: 6.7 Mbit/s up, 57.8 down, 70 ms base round trip | Atlanta | 645 |
| `fiber-ethernet`, 2026-10-16 | Gigabit Ethernet and fiber: 347 Mbit/s up, 394 down, 11 ms | Ashburn | 8,516 |

**Base64 sends exactly a third more bytes, and zstd takes them back.** JPEG
and PNG files are already compressed, so zstd shrinks base64 text back to the
image's size but does nothing for multipart photos. Compression helps only
flat images. Bytes sent, relative to multipart:

| Request | Multipart | multipart+zstd | base64 | base64+zstd |
| --- | --- | --- | --- | --- |
| photo M, 1 JPEG | 302 KiB | 1.00× | 1.33× | 1.00× |
| photo_png L, 1 PNG | 3,904 KiB | 1.00× | 1.33× | 1.00× |
| document M, 1 PNG | 91 KiB | 0.88× | 1.33× | 0.94× |
| drawing M, 1 PNG | 7 KiB | 0.52× | 1.24× | 0.54× |
| frames, 8 JPEGs | 2,605 KiB | 1.00× | 1.33× | 1.00× |

**TypeSafe processes base64 as fast as multipart.** On fiber, across 240
paired cases, TypeSafe's median processing time differed from multipart's by
−6 ms for base64, −1.5 ms for base64+zstd, and −1 ms for multipart+zstd. The
Wi-Fi run found +2, +3, and +1 ms. On fiber, 2 of 48 per-row 95% intervals
excluded zero, about what chance gives, and in opposite directions: base64 was
38 ms faster for L drawings and 24 ms slower with an image in instructions.
Processing time depends on the images: on fiber, 86 ms for one S photo, 124 ms
for an L JPEG, 182 ms for an L PNG, and 330 ms for 8 M frames. Repeating a
request was no faster, so there was no cache.

**One request at a time, base64 costs only its upload time.** On fiber,
latency minus processing time fit 27 ms plus bytes sent at 525 Mbit/s, so a
third more of a 300 KiB photo is about 2 ms. Base64 sent a median of 99 KiB
more per request and took a median of 4 ms *less*. A line through all 240
paired differences crosses zero extra bytes at −1 ms (95% interval −9 to +7
ms). Every per-row median for every transport was within 37 ms of multipart's,
at latencies of 100–430 ms. The only intervals that excluded zero were the
same two rows, matching their processing times. On Wi-Fi, the fit was 37 ms
plus bytes at 3.9 Mbit/s, so the extra third is real time: about 200 ms for an
M photo, though each upload's speed varied from 0.6 to 10 Mbit/s.

**Once the uplink is full, base64 delivers 14–16% fewer images.** Images per
second, with each transport relative to multipart:

| Run | Images | Concurrency | Mbit/s sent | multipart | multipart+zstd | base64 | base64+zstd |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Fiber | photo M | 32 | 305–375 | 129/s | 0.93× | 0.86× | 0.96× |
| Fiber | photo M | 128 | 417–469 | 163/s | 1.12× | 0.84× | 1.05× |
| Fiber | photo_png M | 32 | 519–582 | 37/s | 1.09× | 0.84× | 1.08× |
| Wi-Fi | photo M | 8 | 8–10 | 3.2/s | 1.10× | 0.86× | 1.17× |
| Wi-Fi | photo M | 32 | 5–7 | 2.9/s | 0.84× | 0.56× | 0.79× |

On fiber, base64 sent the most Mbit/s in each full-link row but delivered the
fewest images. Uploads alone would give 0.75×; processing time narrows the
gap. The zstd variants were within the ±10–20% that one transport's two blocks
differed. At concurrency 8 on fiber, the link wasn't full: every transport
reached 41–53 images/s, and the order was noise.

**The 32 MiB limit counts the decompressed body.** Seven lossless L photos
are 27.4 MiB as multipart and 36.5 MiB as base64. With long context, on fiber:

| Transport | Sent | Result |
| --- | --- | --- |
| multipart | 27.4 MiB | `200` in 1.3 s, 33,461 input tokens |
| multipart+zstd | 27.4 MiB | `200` in 1.0 s |
| base64 | 36.5 MiB | `413` in 0.4 s: "Request body of at least 38299919 bytes exceeds the hard maximum of 33554432 bytes" |
| base64+zstd | 27.4 MiB | `413` in 0.4 s: "at least 33554433 bytes" |

The base64 rejection quotes the body's exact `Content-Length`, and the
base64+zstd rejection the first byte over the limit while decompressing. So
base64 can carry at most 24 MiB of images, compressed or not. On Wi-Fi the
same rejections were costlier:

- **base64+zstd's 413** came only after its whole 78-second upload.
- **Uncompressed base64** ended in a closed connection (`ReadError`) instead
  of a 413. A client that retries connection errors uploads it again; this
  harness did, four times, so the limits phase no longer retries.

**The context limit usually comes first.** Seven 1568 × 1176 images are
33,461 tokens, over the default 32,000 per question: without long context,
multipart got `400 max_tokens_exceeded`. Six should fit, and six lossless L
photos are 30.5 MiB even as base64. So base64 meets the body limit first only
with long context, or with files larger per pixel.

**Answers don't depend on the transport.** On fiber, input tokens matched
multipart's in all 240 paired cases, and so did all 228 Choices. Noul values
differed by at most 0.14 for base64 and 0.17 for multipart+zstd, so the
spread is TypeSafe's run-to-run variation. 6 of 648 Box comparisons
differed by more than 0.1. All six were on 512 × 384 images with low
confidence on at least one side, spread across every transport. Against the
drawn targets, every transport got 95% of yes/no and 100% of Choice answers
right, with a median Box IoU of 0.88–0.89. The Wi-Fi run agreed: 64 of 64
token counts and 60 of 60 Choices.

**Encoding costs little.** On the M3 in CPython, base64 took 2.5 ms per MB of
image. zstd added 0.2 ms per MB to multipart and 1 ms per MB to base64 text,
and gzip took 15 to 37 ms per MB. Building a multipart body took under 0.1 ms
per MB. Next to network and processing time, none of these mattered in either
run.

So multipart is better or equal on every measure: the fewest bytes, no
encoding CPU, full throughput on a busy link, and the most room under the
body limit. zstd on multipart is harmless, which matters because the
TypeSafe SDKs apply it to every body, but it helps only flat graphics. If a
client must send JSON, zstd recovers base64's throughput, but not its room
under the limit.

## Run it

You need [uv](https://docs.astral.sh/uv/) and a TypeSafe API key. Run these
from the repository root; uv installs the pinned httpx2, Pillow, and NumPy
the first time.

```sh
# Free: bytes and client encoding time for every transport
uv run benchmarks/multipart-vs-base64/bench.py offline

# Print the battery's plan, upload volume, and cost without sending anything
uv run benchmarks/multipart-vs-base64/bench.py battery --profile full --dry-run

# On a fast wired connection: about 5.7 GB to upload, about $1, about 5 minutes
export TYPESAFE_API_KEY=...
uv run benchmarks/multipart-vs-base64/bench.py battery --profile full --network-quality --label fiber-ethernet

# Tables for one run, or several side by side
uv run benchmarks/multipart-vs-base64/summarize.py benchmarks/multipart-vs-base64/results/fiber-ethernet
uv run benchmarks/multipart-vs-base64/summarize.py benchmarks/multipart-vs-base64/results/{wifi-hotspot-2026-10-15,fiber-ethernet}
```

`--profile quick` is for slow or metered links: about 0.35 GB, or 0.47 GB
with `--phases sizes,placement,throughput,limits`, which adds the 32 MiB
request. `--phases` runs a subset, and `--reps` changes the repetitions.
`--http2` switches from HTTP/1.1, the SDK default, to HTTP/2.
`--network-quality` first records macOS `networkQuality` (about 20 seconds)
so runs on different links can be compared. Results go to `results/<label>/`,
which is gitignored: `requests.jsonl` has one line per request,
`throughput.jsonl` one line per throughput block, and `runs.jsonl` the
environment.

## What it sends

**Transports.** Every request is built four ways:

| Transport | Body | Notes |
| --- | --- | --- |
| `multipart` | A `request` JSON part, then one file part per image | What the TypeSafe docs recommend over HTTP |
| `multipart+zstd` | The same, zstd-compressed (level 3) | What the TypeSafe Python SDK 0.7.5 sends |
| `base64` | One JSON body; each image in `data_base64` | |
| `base64+zstd` | The same, zstd-compressed (level 3) | |

`multipart+gzip` and `base64+gzip` (level 6) are available with
`--transports`.

**Images.** `corpus.py` draws four kinds of image at 512 × 384 (S),
1024 × 768 (M), and 1568 × 1176 (L, the largest the API accepts at 4:3). Each
has one red target, a circle, square, triangle, or star, whose box is known.

| Kind | Format | S | M | L |
| --- | --- | --- | --- | --- |
| photo: 1/f-noise texture, lighting, grain, blurred distractors | JPEG, quality 90 | 80 KiB | 300 KiB | 670 KiB |
| photo_png: the same kind of scene, lossless | PNG | 430 KiB | 1.7 MiB | 3.9 MiB |
| document: text, a table, and a red stamp | PNG | 26 KiB | 90 KiB | 160 KiB |
| drawing: flat shapes | PNG | 2 KiB | 6 KiB | 11 KiB |

The same seed gives the same bytes with the pinned Pillow and NumPy, so runs
on different networks send identical images.

**Phases.**

- **sizes:** every kind and size, one image in the state, with four questions:
  a Noul (is the target in the left half?), a Choice (its shape), a Score
  (how much of the image it covers), and a Box (where it is).
- **placement:** a photo in the state and an example picture in a Noul's
  `instructions` (2 images); a Choice whose four options are example pictures
  in `criteria` (5); 8 camera frames in the state with a Noul (did the
  target move?), a Choice, and a Box on the last frame (8); and an album of
  12 small photos with a Choice of which one shows a star (12).
- **throughput:** M photos with the four questions, many requests at once per
  transport, in A B C D D C B A blocks so drift in the network affects every
  transport alike.
- **limits:** 7 lossless L photos, 27.4 MiB as multipart and 36.5 MiB as
  base64, against the 32 MiB body limit. The request turns on long context,
  because 7 images of this size exceed the default per-question context. It
  isn't retried.

In **sizes** and **placement**, each case goes through all four transports
back to back on one kept-alive HTTP/1.1 connection, and the first transport
rotates from case to case. So every comparison is paired: the same images and
questions, a few seconds apart. Bodies are built just before sending; their
encoding time is recorded separately from latency. Throughput bodies are
built before each block starts.

Each response's `x-envoy-upstream-service-time` header gives the time from
TypeSafe's proxy forwarding the request to the response, recorded as
TypeSafe's processing time. Cloudflare, in front of that proxy, takes the
whole upload first, so latency minus processing time is mostly the upload and
the round trip.

## Caveats

- **Synthetic images.** The photos have the file sizes of real photos at each
  size, but they are not real photos. Tokens depend only on pixel dimensions
  (about 856, 2,400, and 5,030 for S, M, and L with one Noul), so file size
  and format change bytes sent, not tokens.
- **Python client.** Encoding times are for CPython's `base64`, `json`, and
  `compression.zstd` on one machine; other clients will differ.
- **One account, one day per run.** The two runs reached different Cloudflare
  locations on different days. TypeSafe's latency and rate limits change over
  time, and Images are a preview feature.
- **Throughput blocks drift.** A transport's two blocks differed by up to
  20%, so throughput ratios within about ±10% of 1.00× are not differences.
- **No rate limiting.** No request in either run got a `429`, so these
  results don't show how base64 interacts with token or request limits.
