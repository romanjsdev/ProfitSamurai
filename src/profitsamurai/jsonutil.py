"""JSON values that stay inside the type checker."""

import json
from typing import cast

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


def loads(data: str | bytes) -> object:
    return cast(object, json.loads(data))


def expect_json(value: object) -> JsonValue:
    if value is None or isinstance(value, str | float | bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        items = cast(list[object], value)
        return [expect_json(item) for item in items]
    if isinstance(value, dict):
        raw = cast(dict[object, object], value)
        mapped: dict[str, JsonValue] = {}
        for key, item in raw.items():
            if not isinstance(key, str):
                raise TypeError("expected string key")
            mapped[key] = expect_json(item)
        return mapped
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def expect_object(value: object) -> JsonObject:
    parsed = expect_json(value)
    if not isinstance(parsed, dict):
        raise TypeError("expected JSON object")
    return parsed


def as_object_list(value: JsonValue | None) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    objects: list[JsonObject] = []
    for item in value:
        if isinstance(item, dict):
            objects.append(item)
    return objects


def pick_int(row: JsonObject, key: str) -> int | None:
    value = row.get(key)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def nested_object(row: JsonObject, key: str) -> JsonObject | None:
    value = row.get(key)
    if isinstance(value, dict):
        return value
    return None
