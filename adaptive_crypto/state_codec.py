"""Lossless JSON document decomposition into individually writable records."""
import json

from .core import DataError

LISTS = {"warnings": "warning", "outbox": "outbox", "positions": "position", "buy_watches": "buy_watch"}
ASSET_LISTS = {"trades": "trade", "consumed": "consumed"}


def dumps(value):
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))


def loads(value):
    def invalid(constant):
        raise DataError(f"Non-finite JSON value: {constant}")
    return json.loads(value, parse_constant=invalid)


def encode(document, *, previous=None, previous_rows=None):
    if not isinstance(document, dict):
        raise DataError("State must be an object")
    root = dict(document)
    rows = {}

    def record(collection, owner, key, index, value, old_value):
        row_key = collection, owner, key
        old_row = previous_rows.get(row_key) if previous_rows is not None else None
        payload = old_row[1] if old_row is not None and value == old_value else dumps(value)
        rows[row_key] = index, payload

    def split_list(container, field, collection, owner="", old_container=None):
        if field not in container:
            return
        values = container[field]
        if not isinstance(values, list):
            raise DataError(f"Invalid state list: {field}")
        old_values = (old_container or {}).get(field, [])
        for index, value in enumerate(values):
            record(collection, owner, str(index), index, value, old_values[index] if index < len(old_values) else None)
        container[field] = []

    for field, collection in LISTS.items():
        split_list(root, field, collection, old_container=previous)
    for field, collection in (("assets", "asset"), ("watches", "watch")):
        if field not in root:
            continue
        if not isinstance(root[field], dict):
            raise DataError(f"Invalid state mapping: {field}")
        for index, (name, value) in enumerate(root[field].items()):
            if not isinstance(name, str) or not isinstance(value, dict):
                raise DataError(f"Invalid {field} record")
            value = dict(value)
            old_value = (previous or {}).get(field, {}).get(name)
            if field == "assets":
                for subfield, subcollection in ASSET_LISTS.items():
                    split_list(value, subfield, subcollection, name, old_value)
                if old_value is not None:
                    old_value = {key: ([] if key in ASSET_LISTS else val) for key, val in old_value.items()}
            record(collection, "", name, index, value, old_value)
        root[field] = {}
    return dumps(root), rows


def decode(root_json, rows):
    """Reject malformed storage layouts instead of dropping unknown rows."""
    root = loads(root_json)
    if not isinstance(root, dict):
        raise DataError("Invalid state skeleton")
    grouped = {}
    for (collection, owner, key), (ordinal, payload) in rows.items():
        if type(ordinal) is not int or ordinal < 0:
            raise DataError("Invalid record ordinal")
        grouped.setdefault((collection, owner), []).append((ordinal, key, loads(payload)))

    def restore(container, field, collection, owner="", mapping=False):
        values = sorted(grouped.pop((collection, owner), []), key=lambda row: row[0])
        if field not in container:
            if values:
                raise DataError("Rows without a state container")
            return
        empty = {} if mapping else []
        if type(container[field]) is not type(empty) or container[field] != empty:
            raise DataError("Nonempty or malformed state skeleton")
        if [row[0] for row in values] != list(range(len(values))):
            raise DataError("Duplicate or missing record ordinals")
        if not mapping and any(key != str(index) for index, key, _ in values):
            raise DataError("Invalid list record key")
        if mapping and any(not isinstance(value, dict) for _, _, value in values):
            raise DataError("Invalid mapping record")
        container[field] = {key: value for _, key, value in values} if mapping else [value for _, _, value in values]

    for field, collection in LISTS.items():
        restore(root, field, collection)
    restore(root, "assets", "asset", mapping=True)
    restore(root, "watches", "watch", mapping=True)
    for name, asset in root.get("assets", {}).items():
        for field, collection in ASSET_LISTS.items():
            restore(asset, field, collection, name)
    if grouped:
        raise DataError("Unknown collection or orphaned state rows")
    return root
