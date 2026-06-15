from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict[str, Any]:
    schema_path = resources.files("councli.schemas").joinpath(f"{name}.schema.json")
    return json.loads(schema_path.read_text(encoding="utf-8"))


def validate_json_schema_subset(value: Any, schema: dict[str, Any], *, path: str = "$") -> list[str]:
    """Validate the JSON Schema subset used by councli protocol artifacts."""
    errors: list[str] = []
    schema_type = schema.get("type")
    if schema_type is not None and not matches_type(value, schema_type):
        return [f"{path} expected {schema_type}, got {json_type(value)}"]

    if "enum" in schema and value not in schema["enum"]:
        return [f"{path} expected one of {schema['enum']!r}, got {value!r}"]

    if isinstance(value, dict):
        required = schema.get("required") or []
        for field in required:
            if field not in value:
                errors.append(f"{path}.{field} is required")
        properties = schema.get("properties") or {}
        for field, subschema in properties.items():
            if field in value and isinstance(subschema, dict):
                errors.extend(validate_json_schema_subset(value[field], subschema, path=f"{path}.{field}"))
        additional = schema.get("additionalProperties", True)
        if additional is False:
            allowed = set(properties)
            for field in value:
                if field not in allowed:
                    errors.append(f"{path}.{field} is not allowed")
        elif isinstance(additional, dict):
            for field, item in value.items():
                if field not in properties:
                    errors.extend(validate_json_schema_subset(item, additional, path=f"{path}.{field}"))

    if isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(validate_json_schema_subset(item, item_schema, path=f"{path}[{index}]"))

    return errors


def matches_type(value: Any, schema_type: Any) -> bool:
    if isinstance(schema_type, list):
        return any(matches_type(value, item) for item in schema_type)
    if schema_type == "object":
        return isinstance(value, dict)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "null":
        return value is None
    return True


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__
