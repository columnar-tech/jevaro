"""Arrow IPC input: each row becomes one state, as the JSON the TypeSafe API accepts.

A binary value is an image file. It becomes a reference in the state, and its
bytes go with that row's upstream call as an asset.
"""

import json
from uuid import uuid4

import pyarrow as pa
import pyarrow.compute as pc

ROWS_PER_CHUNK = 1024
# Converting a chunk copies its images out of Arrow, so keep image chunks small.
IMAGE_ROWS_PER_CHUNK = 32
types = pa.types


class StatesError(ValueError):
    """Arrow input that cannot become TypeSafe states."""


def is_text(data_type):
    return types.is_string(data_type) or types.is_large_string(data_type) or types.is_string_view(data_type)


def is_list(data_type):
    return types.is_list(data_type) or types.is_large_list(data_type) or types.is_fixed_size_list(data_type)


def is_binary(data_type):
    return types.is_binary(data_type) or types.is_large_binary(data_type) or types.is_binary_view(data_type)


def child(path, name):
    return f"{path}.{name}" if path else name


def plain_type(data_type, path):
    """The type to cast to before to_pylist(), so every value is already JSON."""
    if isinstance(data_type, pa.JsonType):
        return data_type.storage_type
    if (is_text(data_type) or is_binary(data_type) or types.is_boolean(data_type)
            or types.is_integer(data_type) or types.is_floating(data_type) or types.is_null(data_type)):
        return data_type
    if (types.is_date(data_type) or types.is_time(data_type) or types.is_timestamp(data_type)
            or types.is_decimal(data_type)):
        return pa.string()
    if types.is_dictionary(data_type):
        return plain_type(data_type.value_type, path)
    if types.is_struct(data_type):
        names = [field.name for field in data_type]
        if len(set(names)) != len(names):
            raise StatesError(f"{path} has duplicate field names" if path
                              else "states has duplicate column names")
        return pa.struct([field.with_type(plain_type(field.type, child(path, field.name)))
                          for field in data_type])
    if is_list(data_type):
        value = data_type.value_field
        value = value.with_type(plain_type(value.type, f"{path}[]"))
        if types.is_fixed_size_list(data_type):
            return pa.list_(value, data_type.list_size)
        return pa.large_list(value) if types.is_large_list(data_type) else pa.list_(value)
    if types.is_map(data_type):
        key = plain_type(data_type.key_type, f"{path} key")
        if not is_text(key):
            raise StatesError(f"{path} is a map with {data_type.key_type} keys; JSON object keys are strings")
        return pa.map_(data_type.key_field.with_type(key),
                       data_type.item_field.with_type(plain_type(data_type.item_type, f"{path}{{}}")))
    raise StatesError(f"{path} has unsupported Arrow type {data_type}")


def reject_constant(name):
    raise ValueError(f"{name} is not valid JSON")


def parse_json(text):
    return None if text is None else json.loads(text, parse_constant=reject_constant)


def json_parser(data_type):
    """A function that parses arrow.json values nested in a to_pylist() value, or None."""
    if isinstance(data_type, pa.JsonType):
        return parse_json
    if types.is_dictionary(data_type):
        return json_parser(data_type.value_type)
    if types.is_struct(data_type):
        parsers = {field.name: parser for field in data_type if (parser := json_parser(field.type))}
        if parsers:
            return lambda value: None if value is None else {
                name: parsers[name](item) if name in parsers else item for name, item in value.items()
            }
    elif is_list(data_type) or types.is_map(data_type):
        nested = data_type.item_type if types.is_map(data_type) else data_type.value_type
        parser = json_parser(nested)
        if parser and types.is_map(data_type):
            return lambda value: None if value is None else {k: parser(v) for k, v in value.items()}
        if parser:
            return lambda value: None if value is None else [parser(item) for item in value]
    return None


def check_finite(array, path):
    """JSON has no NaN or infinity; the JSON request path rejects them too."""
    data_type = array.type
    if types.is_floating(data_type):
        if pc.any(pc.invert(pc.is_finite(array))).as_py():
            raise StatesError(f"{path} contains NaN or an infinity, which JSON cannot represent")
    elif types.is_dictionary(data_type):
        check_finite(array.dictionary_decode(), path)
    elif types.is_struct(data_type):
        for field, values in zip(data_type, array.flatten()):
            check_finite(values, child(path, field.name))
    elif is_list(data_type):
        check_finite(array.flatten(), f"{path}[]")
    elif types.is_map(data_type):
        start, end = array.offsets[0].as_py(), array.offsets[-1].as_py()
        check_finite(array.items.slice(start, end - start), f"{path}{{}}")


def contains(data_type, kind):
    """Whether data_type is, or nests, a type for which kind(type) is true."""
    if kind(data_type):
        return True
    if types.is_dictionary(data_type) or is_list(data_type):
        return contains(data_type.value_type, kind)
    if types.is_map(data_type):
        return contains(data_type.item_type, kind)
    if types.is_struct(data_type):
        return any(contains(field.type, kind) for field in data_type)
    return False


EXTENSION_NAME = b"ARROW:extension:name"


def check_extensions(field, path):
    """Reject binary data under an extension type this reader doesn't know, such as geoarrow.wkb.

    PyArrow reads such a field as its storage type, which would otherwise pass as image files.
    """
    name = (field.metadata or {}).get(EXTENSION_NAME)
    if name is not None and contains(field.type, is_binary):
        raise StatesError(f"{path} has Arrow extension type {name.decode(errors='replace')}; binary values "
                          "must be image files")
    data_type = field.type
    if types.is_dictionary(data_type):
        return
    if types.is_struct(data_type):
        for child_field in data_type:
            check_extensions(child_field, child(path, child_field.name))
    elif is_list(data_type):
        check_extensions(data_type.value_field, f"{path}[]")
    elif types.is_map(data_type):
        check_extensions(data_type.item_field, f"{path}{{}}")


class States:
    """Arrow rows as states, converted ROWS_PER_CHUNK rows at a time while iterating.

    Without a state column, each row becomes an object of its columns, like
    table.to_pylist(). With one, that column's values are the states.
    """

    def __init__(self, table, state_column=None):
        # Every image reference in this request: its own prefix, then a count.
        self.prefix = f"<image:{uuid4().hex[:20]}"
        self.count = 0
        self.images = 0
        if table.num_rows == 0:
            raise StatesError("states must have at least one row")
        for field in table.schema:
            if state_column is None or field.name == state_column:
                check_extensions(field, field.name)
        if state_column is None:
            if table.num_columns == 0:
                raise StatesError("states must have at least one column")
            self.column = table.to_struct_array()
            self.path = ""
            self.json_states = False
        else:
            if len(table.schema.get_all_field_indices(state_column)) != 1:
                raise StatesError(f"state_column {state_column!r} must name exactly one column")
            self.column = table.column(state_column)
            self.path = state_column
            value_type = self.column.type
            if types.is_dictionary(value_type):
                value_type = value_type.value_type
            self.json_states = isinstance(value_type, pa.JsonType)
            if not (is_text(value_type) or is_binary(value_type) or types.is_struct(value_type)
                    or is_list(value_type) or types.is_map(value_type) or isinstance(value_type, pa.JsonType)):
                raise StatesError(f"state_column {state_column!r} has type {self.column.type}; a state "
                                  "must be a string, image, struct, list, map, or arrow.json value")
        self.target = plain_type(self.column.type, self.path)
        self.parse = json_parser(self.column.type)
        self.binary = contains(self.column.type, is_binary)

    def __len__(self):
        return len(self.column)

    def __iter__(self):
        return (state for state, _ in self.rows())

    def rows(self):
        """Yield (state, images): images maps the references of the row's binary values to their bytes."""
        row = 0
        size = IMAGE_ROWS_PER_CHUNK if self.binary else ROWS_PER_CHUNK
        for chunk in self.column.chunks:
            for offset in range(0, len(chunk), size):
                part = chunk.slice(offset, size)
                try:
                    values = part.cast(self.target).to_pylist(maps_as_pydicts="strict")
                except (KeyError, ValueError, pa.ArrowException) as error:
                    # Within a struct, PyArrow re-raises a duplicate map key as a
                    # duplicate field name error; report the original KeyError.
                    if isinstance(error.__context__, KeyError):
                        error = error.__context__
                    reason = error.args[0] if isinstance(error, KeyError) and error.args else error
                    raise StatesError(f"Rows {row} to {row + len(part) - 1}: {reason}") from error
                for value in values:
                    images = {}
                    yield self.state(value, row, images), images
                    row += 1

    def state(self, value, row, images):
        if value is None:
            raise StatesError(f"Row {row}: {self.path} is null; a state cannot be null")
        if self.parse:
            try:
                value = self.parse(value)
            except ValueError as error:
                raise StatesError(f"Row {row}: invalid arrow.json value: {error}") from error
            if self.json_states and not isinstance(value, (str, dict, list)):
                raise StatesError(f"Row {row}: a JSON state must be a string, object, or array")
        if self.binary:
            value = self.lift_images(value, row, self.path, images)
        return value

    def lift_images(self, value, row, path, images):
        """value with each image file (bytes) replaced by a new reference, added to images."""
        if isinstance(value, bytes):
            if not value:
                raise StatesError(f"Row {row}: {path} is an empty image")
            reference = f"{self.prefix}{self.count:012x}>"
            self.count += 1
            images[reference] = value
            return reference
        if isinstance(value, dict):
            return {key: self.lift_images(item, row, child(path, key), images) for key, item in value.items()}
        if isinstance(value, list):
            return [self.lift_images(item, row, f"{path}[]", images) for item in value]
        return value

    def validate(self, check=None):
        """Convert every row once and discard it, so bad input fails before streaming starts.

        check, if given, is called with each state, its images, and its row number.
        Afterward, self.images is the number of images in all rows.
        """
        if contains(self.column.type, types.is_floating):
            for chunk in self.column.chunks:
                check_finite(chunk, self.path)
        self.images = 0
        for row, (state, images) in enumerate(self.rows()):
            self.images += len(images)
            if check:
                check(state, images, row)


def read_states(data, state_column=None, check=None):
    """Read a whole Arrow IPC stream and check that every row can become a state."""
    if bytes(data[:6]) == b"ARROW1":
        raise StatesError("states uses the Arrow IPC file format; send the IPC stream format")
    try:
        table = pa.ipc.open_stream(pa.py_buffer(data)).read_all()
        # read_all() only validates cheaply; a crafted stream can still carry offset
        # buffers that are out of bounds or non-monotonic. Left unchecked, those read
        # adjacent memory or abort the worker with an uncaught C++ exception during
        # conversion, so validate the buffers in full before touching any value.
        table.validate(full=True)
    except (pa.ArrowException, OSError) as error:  # Malformed metadata raises a plain OSError.
        raise StatesError(f"states is not a valid Arrow IPC stream: {error}") from error
    states = States(table, state_column)
    states.validate(check)
    return states
