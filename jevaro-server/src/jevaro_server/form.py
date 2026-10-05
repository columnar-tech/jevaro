"""Read named multipart/form-data parts into memory.

Starlette's request.form() spools each large file part to a temporary file and
moves every chunk through a worker thread. Calling python-multipart directly
keeps its fast boundary search and gives PyArrow one contiguous buffer.
"""

from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header


class FormError(ValueError):
    """A multipart body without exactly the expected parts."""


def media_type(value):
    """The lowercase media type of a Content-Type header value, without parameters."""
    return parse_options_header(value or "")[0].decode("latin-1").lower()


async def read_parts(request, names):
    """Return {name: (media type or None, bytearray)} for each of the named parts."""
    _, options = parse_options_header(request.headers["content-type"])
    boundary = options.get(b"boundary")
    if not boundary:
        raise FormError("multipart/form-data requires a boundary parameter")

    parts = {}
    headers = {}
    field = bytearray()
    value = bytearray()
    current = None
    finished = False

    def on_part_begin():
        headers.clear()

    def on_header_field(data, start, end):
        field.extend(data[start:end])

    def on_header_value(data, start, end):
        value.extend(data[start:end])

    def on_header_end():
        headers[bytes(field).lower()] = bytes(value)
        field.clear()
        value.clear()

    def on_headers_finished():
        nonlocal current
        disposition, params = parse_options_header(headers.get(b"content-disposition", b""))
        name = params.get(b"name", b"").decode("utf-8", "replace")
        if disposition.lower() != b"form-data" or not name:
            raise FormError("Each part needs Content-Disposition: form-data with a name")
        if name not in names:
            raise FormError(f"Unexpected part {name!r}; send only {' and '.join(map(repr, names))}")
        if name in parts:
            raise FormError(f"Duplicate part {name!r}")
        current = parts[name] = (media_type(headers.get(b"content-type")) or None, bytearray())

    def on_part_data(data, start, end):
        current[1].extend(memoryview(data)[start:end])

    def on_end():
        nonlocal finished
        finished = True

    parser = MultipartParser(boundary, {
        "on_part_begin": on_part_begin, "on_header_field": on_header_field,
        "on_header_value": on_header_value, "on_header_end": on_header_end,
        "on_headers_finished": on_headers_finished, "on_part_data": on_part_data,
        "on_end": on_end,
    })
    try:
        async for chunk in request.stream():
            parser.write(chunk)
        parser.finalize()
    except MultipartParseError as error:
        raise FormError(f"Malformed multipart body: {error}") from error
    if not finished:
        raise FormError("Incomplete multipart body: missing the closing boundary")
    if missing := [name for name in names if name not in parts]:
        raise FormError(f"Missing part {missing[0]!r}")
    return parts
