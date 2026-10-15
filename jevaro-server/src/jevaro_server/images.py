"""Images: a request's assets, the references that use them, and the assets each call sends.

TypeSafe matches each asset's reference as plain text in the strings of a request's
state, instructions, and criteria, and rejects a request with an asset it doesn't
reference. So each upstream call carries only the assets that its state and
questions refer to.
"""

import base64
import binascii
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typesafe_sdk import Image

from .packing import strings

DANGER_ZONE = "typesafe-danger-zone-"
IMAGES_HEADER = "TypeSafe-Danger-Zone-Images"
MAX_REFERENCE_BYTES = 1024
MAX_CALL_REFERENCE_BYTES = 65_536
# Image parts may also omit their Content-Type.
IMAGE_PART_TYPES = {None, "application/octet-stream", "image/png", "image/jpeg", "image/jpg"}


class ImageError(ValueError):
    """Image input that TypeSafe would reject."""


class Asset(BaseModel):
    """One entry of a request's `assets`, as TypeSafe accepts it."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["image"] | None = "image"
    part: str | None = Field(default=None, min_length=1)
    data_base64: str | None = None
    format: Literal["png", "jpg", "jpeg"] | None = None

    @model_validator(mode="after")
    def validate_asset(self):
        if self.part is None and self.data_base64 is None:
            raise ValueError("An asset needs part or data_base64")
        if self.data_base64 is not None and self.format is None:
            raise ValueError("format is required with data_base64")
        return self


def forwarded_headers(headers):
    """The request's TypeSafe-Danger-Zone-* headers, to send upstream unchanged; the first of repeats."""
    forwarded = {}
    for name, value in headers.items():
        if name.lower().startswith(DANGER_ZONE):
            forwarded.setdefault(name.lower(), value)
    return forwarded


def images_enabled(headers):
    return headers.get(IMAGES_HEADER, "").strip().lower() == "true"


def check_reference(reference):
    if not reference.strip():
        raise ImageError("An asset reference can't be empty or only whitespace")
    try:
        size = len(reference.encode())
    except UnicodeEncodeError as error:
        raise ImageError(f"Asset reference {reference[:40]!r} is not valid Unicode text") from error
    if size > MAX_REFERENCE_BYTES:
        raise ImageError(f"Asset reference {reference[:40]!r}... is over {MAX_REFERENCE_BYTES} bytes")


def load_assets(assets, parts=None):
    """Check each asset and return its image file bytes, by reference.

    parts maps the names of a multipart request's image parts to (media type, bytes),
    or is None for a JSON request.
    """
    images = {}
    claimed = {}
    for reference, asset in assets.items():
        check_reference(reference)
        if asset.part is not None:
            if parts is None:
                raise ImageError(f"Asset {reference!r} names a part; send parts in a multipart/form-data request")
            if asset.part not in parts:
                raise ImageError(f"Asset {reference!r} names part {asset.part!r}, which the request doesn't have")
            if asset.part in claimed:
                raise ImageError(f"Assets {claimed[asset.part]!r} and {reference!r} both name part {asset.part!r}")
            claimed[asset.part] = reference
            media_type, data = parts[asset.part]
            if media_type not in IMAGE_PART_TYPES:
                raise ImageError(f"Part {asset.part!r} has Content-Type {media_type}; send image/png, "
                                 "image/jpeg, or application/octet-stream")
            data = bytes(data)
        else:
            try:
                data = base64.b64decode(asset.data_base64, validate=True)
            except (binascii.Error, ValueError) as error:
                raise ImageError(f"Asset {reference!r} has invalid data_base64: use the standard alphabet "
                                 "with = padding and no whitespace") from error
        if not data:
            raise ImageError(f"Asset {reference!r} is empty")
        images[reference] = data
    if parts and (unclaimed := [name for name in parts if name not in claimed]):
        raise ImageError(f"Part {unclaimed[0]!r} is not named by any asset")
    return images


class References:
    """Find references in JSON values as TypeSafe matches them.

    A string that is exactly a reference matches first. Otherwise references match
    from left to right without overlapping, the longest first where several start
    at the same place. Object keys never match.
    """

    def __init__(self, names):
        self.names = frozenset(names)
        self.lengths = sorted({len(name) for name in self.names}, reverse=True)
        # Only positions where some reference could start are tried.
        self.starts = self.names and re.compile("[" + "".join(sorted({re.escape(name[0]) for name in self.names})) + "]")

    def find(self, value, found, skip=()):
        """Add each reference in value to found, a dict used as an ordered set; ignore strings in skip."""
        if not self.names:
            return
        shortest = self.lengths[-1]
        for text in strings(value):
            if text in self.names:
                found[text] = None
            elif len(text) > shortest and text not in skip:
                end = 0
                for match in self.starts.finditer(text):
                    start = match.start()
                    if start < end:
                        continue
                    for length in self.lengths:
                        candidate = text[start:start + length]
                        if len(candidate) == length and candidate in self.names:
                            found[candidate] = None
                            end = start + length
                            break


def reference_bytes(references):
    return sum(len(reference.encode()) for reference in references)


class Images:
    """A request's images, and the ones each upstream call needs.

    Images in the questions go with every call. Each state also brings the
    request's images it refers to, plus generated ones: the images from its
    Arrow binary values, already written into it as references.
    """

    def __init__(self, assets, questions):
        self.images = {reference: Image(data) for reference, data in assets.items()}
        self.references = References(self.images)
        shared = {}
        for question in questions.values():
            wire = question.model_dump(mode="json")
            self.references.find([wire.get("instructions"), wire.get("criteria")], shared)
        self.shared = {reference: self.images[reference] for reference in shared}
        self.shared_bytes = reference_bytes(shared)
        self.used = set(shared)

    def check(self, state, generated, row):
        """Record the request's images that a state uses, and check the references of its call."""
        found = {}
        self.references.find(state, found, generated)
        self.used.update(found)
        size = self.shared_bytes + reference_bytes(found) + reference_bytes(generated)
        if size > MAX_CALL_REFERENCE_BYTES:
            raise ImageError(f"Row {row}: its references and the questions' total {size:,} bytes; "
                             f"TypeSafe allows {MAX_CALL_REFERENCE_BYTES:,} per request")

    def check_used(self):
        """After checking every state: reject an image that nothing refers to, as TypeSafe would."""
        if unused := [reference for reference in self.images if reference not in self.used]:
            raise ImageError(f"Asset {unused[0]!r} is not referenced in any state, instructions, or criteria")

    def for_state(self, state, generated):
        """The assets for one upstream call, by reference, or None if it sends no images.

        Where references start at the same place, TypeSafe matches the one that
        `assets` lists first. Listing them longest first makes it match the one
        that Jevaro found.
        """
        found = {}
        self.references.find(state, found, generated)
        assets = {**self.shared, **{reference: self.images[reference] for reference in found}}
        assets.update((reference, Image(data)) for reference, data in generated.items())
        return dict(sorted(assets.items(), key=lambda item: -len(item[0]))) or None
