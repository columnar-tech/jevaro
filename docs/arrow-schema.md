# Arrow schema

Jevaro returns an Arrow IPC stream with one column per question and one row
per state. All columns, struct members, and list elements are non-nullable,
except a Box answer's `confidence`. Row position matches the input state
position.

PyArrow writes the schema with the first batch, so we send a zero-row batch
to make the schema available before any answers arrive. Each later batch
contains one or more result rows. Save the stream with an `.arrows` suffix and read
it with `pyarrow.ipc.open_stream`.

## Extension metadata

Each field carries two UTF-8 metadata entries:

| Key | Contents |
| --- | --- |
| `ARROW:extension:name` | `jevaro.choice`, `jevaro.score`, `jevaro.noul`, or `jevaro.box` |
| `ARROW:extension:metadata` | JSON describing the shared answer data |

Readers without these extension classes see the physical types and metadata.
The Jevaro SDKs use this representation; extension registration is not required.
There is no top-level schema metadata.

## Choice

```text
struct<
  choice: uint8 not null,
  confidence: float64 not null,
  probabilities: fixed_size_list<item: float64 not null>[N] not null
>
```

```json
{"labels":["returns","shipping","billing","other"]}
```

`N` is the number of labels, from 1 to 255. `choice` is a zero-based index:
`2` means `"billing"` in this example. Probability positions use the same
label order. Store the selected index and confidence as returned; do not
recompute them from the probabilities.

To reconstruct a Choice answer, add `"type": "choice"`, resolve the selected
label, and zip the labels with the probability vector.

## Score

```text
struct<
  score: float64 not null,
  confidence: float64 not null,
  probabilities: fixed_size_list<item: float64 not null>[N] not null
>
```

```json
{"legend":["Can wait","Within a few days","Today"]}
```

`N` is the number of levels, from 2 to 10. A level's index in `legend` is its
number. Entries can be strings, objects, or arrays. `score` may be fractional.
Probability positions correspond to those level numbers.

To reconstruct a Score answer, add `"type": "score"`, map the legend to keys
`0` through `N - 1`, and use those keys for the probabilities. JSON object
keys are strings; the Python SDK's original answer objects use integer keys.

## Noul

Storage is one `float64`. It has no shared answer metadata:

```json
{}
```

The value is the probability of yes. To reconstruct the answer, wrap it as
`{"type": "noul", "noul": value}`. Noul has no separate confidence field.

## Box

A Box answer locates one thing in an image. The struct holds the answer's
fields, with the same names as TypeSafe's JSON:

```text
struct<
  box: struct<
    top: float64 not null,
    bottom: float64 not null,
    left: float64 not null,
    right: float64 not null
  > not null,
  confidence: float64
>
```

```json
{}
```

Each edge is a fraction of the image's size, measured from its top-left
corner: `top` and `bottom` of its height, `left` and `right` of its width. A
row from `to_pylist()` looks like this:

```json
{"box": {"top": 0.25, "bottom": 0.82, "left": 0.44, "right": 0.84}, "confidence": 0.92}
```

`confidence` is null when TypeSafe doesn't report one. To reconstruct a Box
answer, add `"type": "box"`. To crop the image to the box in pixels, multiply
`left` and `right` by its width, and `top` and `bottom` by its height.

### GeoArrow layout

With `"box_format": "geoarrow"` in the request, the `box` field is a
[GeoArrow](https://geoarrow.org/) box instead. Its field metadata has
`ARROW:extension:name` `geoarrow.box` and `ARROW:extension:metadata` `{}`:

```text
struct<
  box: struct<
    xmin: float64 not null,
    ymin: float64 not null,
    xmax: float64 not null,
    ymax: float64 not null
  > not null,
  confidence: float64
>
```

The same answer is:

```json
{"box": {"xmin": 0.44, "ymin": 0.25, "xmax": 0.84, "ymax": 0.82}, "confidence": 0.92}
```

`x` runs across the image's width and `y` down its height, so `xmin` is
`left`, `ymin` is `top`, `xmax` is `right`, and `ymax` is `bottom`. The box
has no CRS. The `jevaro.box` metadata is `{}` in both layouts; the `box`
field's names tell them apart.

GeoArrow tools look for `geoarrow.box` columns at the top level. This makes
one from the `box` field, keeping its metadata:

```python
import pyarrow as pa
import pyarrow.compute as pc

box = table.schema.field("mug").type.field("box")
boxes = pa.table([pc.struct_field(table["mug"], "box")], schema=pa.schema([box.with_name("mug")]))
```

With [geoarrow-pyarrow](https://geoarrow.org/geoarrow-python/) imported,
`boxes["mug"]` is a `geoarrow.box` column, and
`geoarrow.pyarrow.as_wkt(boxes["mug"])` gives
`POLYGON ((0.44 0.25, 0.84 0.25, 0.84 0.82, 0.44 0.82, 0.44 0.25))`.

## Precision and contents

All numeric answers, including Box edges, use `float64` to preserve the
TypeSafe Python SDK's floats.
Fixed-size probability lists need no offsets. Shared labels and legends are
stored once in the schema; each Choice selection uses one byte per row.

The table contains the answer data. Input states and their images, question
definitions, instructions, Choice descriptions, model names, and token usage
are omitted.
The Score legend is retained because it is part of the answer object.

The writer checks answer names, probability keys, and Score legends against
the schema. A mismatch aborts the stream.

## Read a saved stream

From the repository root, using the file produced by the
[quickstart](quickstart.md):

```sh
.venv/bin/python scripts/read_results.py jevaro-results.arrows --limit 3
```

[read_results.py](../scripts/read_results.py) needs only PyArrow and the file.
It prints full answers as JSON lines using the extension names and metadata.

To inspect the labels directly:

```python
import json
import pyarrow as pa

with pa.ipc.open_stream("jevaro-results.arrows") as reader:
    field = reader.schema.field("department")
    labels = json.loads(field.metadata[b"ARROW:extension:metadata"])["labels"]
    for batch in reader:
        for row in batch.to_pylist():
            print(labels[row["department"]["choice"]])
```

A saved partial stream does not prove that all inputs were evaluated. The HTTP
SDKs check the expected row count while reading; retain that count if you need
to check a saved file separately.
