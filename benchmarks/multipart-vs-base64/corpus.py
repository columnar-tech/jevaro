"""Deterministic test images, each with one red target whose shape and box are known.

Four kinds of image, at three sizes:

- photo: a photo-like scene (1/f noise texture, lighting, grain, soft
  distractor blobs), saved as baseline JPEG at quality 90.
- photo_png: the same kind of scene saved as PNG, the largest files.
- document: a page of text, a table, and a red stamp, saved as PNG like a
  screenshot.
- drawing: flat shapes on a plain background, saved as PNG, the smallest files.

The same spec always gives the same bytes with the pinned Pillow and NumPy, so
two runs on different networks send identical images. Results record each
image's SHA-256 to confirm that.
"""

import hashlib
import io
import math
from dataclasses import dataclass
from functools import cache

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

KINDS = ("photo", "photo_png", "document", "drawing")
SIZES = {"S": (512, 384), "M": (1024, 768), "L": (1568, 1176)}
SHAPES = ("circle", "square", "triangle", "star")
RED = (214, 32, 38)
# Distractor colors: none of them is red, so "the red object" is always the target.
DISTRACTORS = [(40, 90, 200), (30, 150, 70), (230, 190, 40), (120, 70, 160), (90, 90, 90), (20, 160, 170), (240, 140, 30)]
WORDS = ("invoice total amount due account payment order shipping address customer reference date item quantity "
         "price tax subtotal balance received approved pending notes terms delivery warehouse region manager "
         "signature contract renewal policy number claim status").split()
FIELD_SIZE = 2048


@dataclass(frozen=True)
class Target:
    shape: str
    box: tuple[float, float, float, float]  # left, top, right, bottom, as fractions of width and height

    @property
    def area(self):
        left, top, right, bottom = self.box
        return (right - left) * (bottom - top)


@dataclass(frozen=True)
class Picture:
    data: bytes
    format: str  # "jpg" or "png", as the API's `format`
    width: int
    height: int
    target: Target | None

    @property
    def content_type(self):
        return "image/jpeg" if self.format == "jpg" else "image/png"

    @property
    def sha256(self):
        return hashlib.sha256(self.data).hexdigest()


def _rng(*key):
    digest = hashlib.sha256(repr(key).encode()).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "little"))


@cache
def _noise_field(index):
    """A 2048 × 2048 field with a natural image's 1/f amplitude spectrum, scaled to 0..1."""
    rng = _rng("field", index)
    fy = np.fft.fftfreq(FIELD_SIZE)[:, None]
    fx = np.fft.rfftfreq(FIELD_SIZE)[None, :]
    f = np.hypot(fx, fy)
    f[0, 0] = 1.0
    spectrum = (rng.standard_normal(f.shape) + 1j * rng.standard_normal(f.shape)) / f
    spectrum[0, 0] = 0
    field = np.fft.irfft2(spectrum, s=(FIELD_SIZE, FIELD_SIZE)).astype(np.float32)
    lo, hi = np.percentile(field, [1, 99])
    return np.clip((field - lo) / (hi - lo), 0, 1)


def _crop(rng, field, width, height):
    y = int(rng.integers(0, FIELD_SIZE - height + 1))
    x = int(rng.integers(0, FIELD_SIZE - width + 1))
    out = field[y:y + height, x:x + width]
    if rng.random() < 0.5:
        out = out[:, ::-1]
    if rng.random() < 0.5:
        out = out[::-1, :]
    return out


def shape_points(shape, cx, cy, r):
    """Outline of a shape centered at (cx, cy) with half-size r, and its exact bounding box."""
    if shape == "circle":
        return None, (cx - r, cy - r, cx + r, cy + r)
    if shape == "square":
        pts = [(cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r), (cx - r, cy + r)]
    elif shape == "triangle":
        pts = [(cx, cy - r), (cx + r, cy + r * 0.8), (cx - r, cy + r * 0.8)]
    else:
        pts = [(cx + (r if i % 2 == 0 else r * 0.45) * math.sin(math.pi * i / 5),
                cy - (r if i % 2 == 0 else r * 0.45) * math.cos(math.pi * i / 5)) for i in range(10)]
    xs, ys = zip(*pts)
    return pts, (min(xs), min(ys), max(xs), max(ys))


def draw_shape(draw, shape, cx, cy, r, fill, outline=None, width=0):
    pts, box = shape_points(shape, cx, cy, r)
    if pts is None:
        draw.ellipse(box, fill=fill, outline=outline, width=width)
    else:
        draw.polygon(pts, fill=fill, outline=outline, width=width)
    return box


def _place(rng, width, height, side_fraction, x_half=None):
    """A center and half-size for a target whose side is side_fraction of the shorter image side."""
    r = side_fraction * min(width, height) / 2
    margin = r + 4
    lo, hi = margin, width - margin
    if x_half == "left":
        hi = max(lo + 1, width / 2 - r)
    elif x_half == "right":
        lo = min(hi - 1, width / 2 + r)
    cx = float(rng.uniform(lo, hi))
    cy = float(rng.uniform(margin, height - margin))
    return cx, cy, r


def _target(shape, box, width, height):
    left, top, right, bottom = box
    return Target(shape, (left / width, top / height, right / width, bottom / height))


def _save(img, fmt):
    buffer = io.BytesIO()
    if fmt == "jpg":
        img.convert("RGB").save(buffer, "JPEG", quality=90)
    else:
        img.save(buffer, "PNG")
    return buffer.getvalue()


def _photo_background(rng, width, height):
    lum = _crop(rng, _noise_field(int(rng.integers(0, 6))), width, height)
    chroma = _crop(rng, _noise_field(int(rng.integers(6, 9))), width, height)
    # Muted natural palettes: foliage, sky and water, stone, sand.
    palettes = [((34, 60, 30), (150, 180, 110)), ((30, 50, 80), (170, 200, 225)), ((60, 60, 65), (200, 200, 195)),
                ((90, 75, 50), (225, 210, 170))]
    dark, light = (np.array(c, np.float32) for c in palettes[int(rng.integers(0, len(palettes)))])
    img = dark + (light - dark) * lum[..., None]
    img += (chroma[..., None] - 0.5) * np.array([12, 18, -14], np.float32)
    gradient = np.linspace(1.12, 0.82, height, dtype=np.float32)[:, None, None]
    return np.clip(img * gradient, 0, 255)


def _photo(rng, width, height, shape, side_fraction, x_half=None, background=None):
    base = _photo_background(rng, width, height) if background is None else background
    img = Image.fromarray(base.astype(np.uint8))
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for _ in range(int(rng.integers(3, 7))):
        color = DISTRACTORS[int(rng.integers(0, len(DISTRACTORS)))]
        cx, cy, r = _place(rng, width, height, float(rng.uniform(0.06, 0.25)))
        draw_shape(draw, ("circle", "square")[int(rng.integers(0, 2))], cx, cy, r, (*color, 200))
    layer = layer.filter(ImageFilter.GaussianBlur(radius=max(1.0, width / 400)))
    img.paste(layer, (0, 0), layer)
    target = None
    if shape:
        cx, cy, r = _place(rng, width, height, side_fraction, x_half)
        mask = Image.new("L", (width, height), 0)
        box = draw_shape(ImageDraw.Draw(mask), shape, cx, cy, r, 255)
        shaded = np.array(RED, np.float32) * np.linspace(1.15, 0.75, height, dtype=np.float32)[:, None, None]
        red = Image.fromarray(np.clip(np.broadcast_to(shaded, (height, width, 3)), 0, 255).astype(np.uint8))
        img.paste(red, (0, 0), mask.filter(ImageFilter.GaussianBlur(radius=0.8)))
        target = _target(shape, box, width, height)
    grain = rng.normal(0, 4.0, (height, width, 3)).astype(np.float32)
    out = np.clip(np.asarray(img, np.float32) + grain, 0, 255).astype(np.uint8)
    return Image.fromarray(out), target


@cache
def _font(size):
    return ImageFont.load_default(size=size)


def _document(rng, width, height, shape, side_fraction, x_half=None):
    img = Image.new("RGB", (width, height), (250, 250, 246))
    draw = ImageDraw.Draw(img)
    unit = max(10, height // 48)
    draw.rectangle((0, 0, width, unit * 3), fill=(28, 52, 96))
    draw.text((unit, unit // 2), f"STATEMENT {int(rng.integers(10000, 99999))}", font=_font(unit * 2), fill=(255, 255, 255))
    y = unit * 4
    while y < height * 0.55:
        words = " ".join(WORDS[int(i)] for i in rng.integers(0, len(WORDS), int(rng.integers(6, 14))))
        draw.text((unit, y), words.capitalize() + ".", font=_font(unit), fill=(40, 40, 40))
        y += int(unit * 1.6)
    columns = 4
    cell_w = (width - 2 * unit) / columns
    while y < height - unit * 2:
        for c in range(columns):
            x0 = unit + c * cell_w
            draw.rectangle((x0, y, x0 + cell_w, y + unit * 1.6), outline=(170, 170, 170))
            text = WORDS[int(rng.integers(0, len(WORDS)))] if c == 0 else f"{rng.uniform(1, 9999):,.2f}"
            draw.text((x0 + unit / 3, y + unit / 4), text, font=_font(unit), fill=(50, 50, 50))
        y += int(unit * 1.6)
    target = None
    if shape:
        cx, cy, r = _place(rng, width, height, side_fraction, x_half)
        box = draw_shape(draw, shape, cx, cy, r, RED)
        target = _target(shape, box, width, height)
    return img, target


def _drawing(rng, width, height, shape, side_fraction, x_half=None):
    backgrounds = [(245, 245, 240), (230, 238, 245), (240, 235, 225), (225, 240, 228)]
    img = Image.new("RGB", (width, height), backgrounds[int(rng.integers(0, len(backgrounds)))])
    draw = ImageDraw.Draw(img)
    for _ in range(int(rng.integers(3, 7))):
        color = DISTRACTORS[int(rng.integers(0, len(DISTRACTORS)))]
        cx, cy, r = _place(rng, width, height, float(rng.uniform(0.06, 0.25)))
        draw_shape(draw, SHAPES[int(rng.integers(0, len(SHAPES)))], cx, cy, r, color)
    target = None
    if shape:
        cx, cy, r = _place(rng, width, height, side_fraction, x_half)
        box = draw_shape(draw, shape, cx, cy, r, RED)
        target = _target(shape, box, width, height)
    return img, target


@cache
def picture(kind, size, seed, shape=None, x_half=None):
    """One image of this kind and size; seed picks its content, including a random target shape unless given."""
    width, height = SIZES[size]
    rng = _rng(kind, size, seed)
    shape = shape or SHAPES[int(rng.integers(0, len(SHAPES)))]
    side_fraction = float(rng.uniform(0.1, 0.6))
    x_half = x_half or ("left", "right")[int(rng.integers(0, 2))]
    if kind in ("photo", "photo_png"):
        img, target = _photo(rng, width, height, shape, side_fraction, x_half)
    elif kind == "document":
        img, target = _document(rng, width, height, shape, side_fraction, x_half)
    elif kind == "drawing":
        img, target = _drawing(rng, width, height, shape, side_fraction, x_half)
    else:
        raise ValueError(f"Unknown kind {kind!r}")
    fmt = "jpg" if kind == "photo" else "png"
    return Picture(_save(img, fmt), fmt, width, height, target)


def frames(size, seed, count, moves):
    """count JPEG photo frames of one scene, oldest first; the red target moves across them only if moves."""
    width, height = SIZES[size]
    rng = _rng("frames", size, seed)
    shape = SHAPES[int(rng.integers(0, len(SHAPES)))]
    background = _photo_background(rng, width, height)
    r = float(rng.uniform(0.1, 0.25)) * min(width, height) / 2
    start = np.array([rng.uniform(r + 4, width * 0.4), rng.uniform(r + 4, height - r - 4)])
    end = np.array([rng.uniform(width * 0.6, width - r - 4), rng.uniform(r + 4, height - r - 4)]) if moves else start
    out = []
    for i in range(count):
        cx, cy = start + (end - start) * (i / max(1, count - 1))
        frame_rng = _rng("frame", size, seed, i)
        img = Image.fromarray(background.astype(np.uint8))
        mask = Image.new("L", (width, height), 0)
        box = draw_shape(ImageDraw.Draw(mask), shape, float(cx), float(cy), r, 255)
        img.paste(Image.new("RGB", (width, height), RED), (0, 0), mask)
        grain = frame_rng.normal(0, 4.0, (height, width, 3)).astype(np.float32)
        img = Image.fromarray(np.clip(np.asarray(img, np.float32) + grain, 0, 255).astype(np.uint8))
        out.append(Picture(_save(img, "jpg"), "jpg", width, height, _target(shape, box, width, height)))
    return out


@cache
def example(shape):
    """A plain example picture of one shape, in gray on white, to define an answer option by picture."""
    width, height = SIZES["S"]
    img = Image.new("RGB", (width, height), (255, 255, 255))
    box = draw_shape(ImageDraw.Draw(img), shape, width / 2, height / 2, height * 0.35, (110, 110, 110))
    return Picture(_save(img, "png"), "png", width, height, _target(shape, box, width, height))


if __name__ == "__main__":
    import sys
    import time

    out_dir = sys.argv[1] if len(sys.argv) > 1 else None
    for kind in KINDS:
        for size in SIZES:
            start = time.perf_counter()
            pics = [picture(kind, size, seed) for seed in range(3)]
            elapsed = (time.perf_counter() - start) / len(pics)
            sizes = ", ".join(f"{len(p.data) / 1024:,.0f}" for p in pics)
            print(f"{kind:9} {size} {SIZES[size][0]}x{SIZES[size][1]}  KiB: {sizes}  ({elapsed * 1000:.0f} ms each)")
            if out_dir:
                for seed, p in enumerate(pics):
                    with open(f"{out_dir}/{kind}-{size}-{seed}.{p.format}", "wb") as f:
                        f.write(p.data)
