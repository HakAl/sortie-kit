"""Structural checking for our packaged schemas, not a general JSON Schema engine."""
from importlib.resources import files
import json
import re

from .layout import Refused


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Refused("duplicate JSON key: " + key)
            result[key] = value
        return result

    def forbidden(value):
        raise Refused("wire JSON requires integer number syntax: " + value)

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_float=forbidden,
                          parse_constant=forbidden)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Refused("invalid receipt JSON: " + str(exc)) from exc


def asset(name):
    return files("sortie_kit").joinpath("data", "receipts", name).read_bytes()


KEYWORDS = {"$schema", "$defs", "$ref", "title", "description", "type", "const", "enum",
            "properties", "required", "additionalProperties", "items", "minItems", "maxItems",
            "uniqueItems", "minLength", "maxLength", "pattern", "minimum", "maximum",
            "oneOf", "allOf", "if", "then"}


def check_schema(schema):
    if set(schema) - KEYWORDS:
        raise Refused("unsupported packaged schema keyword")
    for group in ("properties", "$defs"):
        for child in schema.get(group, {}).values():
            check_schema(child)
    for group in ("oneOf", "allOf"):
        for child in schema.get(group, []):
            check_schema(child)
    for key in ("items", "if", "then"):
        if key in schema:
            check_schema(schema[key])


def shape(value, schema, root=None, at="receipt"):
    root = root or schema
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/$defs/") or ref[8:] not in root.get("$defs", {}):
            raise Refused("unsupported packaged schema reference")
        shape(value, root["$defs"][ref[8:]], root, at)
    kinds = {"object": dict, "array": list, "string": str, "integer": int,
             "boolean": bool, "null": type(None)}
    if "type" in schema and type(value) is not kinds[schema["type"]]:
        raise Refused(at + ": expected " + schema["type"])
    for key in ("const", "enum"):
        if key in schema:
            choices = [schema[key]] if key == "const" else schema[key]
            if not any(type(value) is type(c) and value == c for c in choices):
                raise Refused(at + ": unexpected value")
    if isinstance(value, dict):
        if set(schema.get("required", [])) - value.keys():
            raise Refused(at + ": missing required fields")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False and value.keys() - props.keys():
            raise Refused(at + ": unknown fields")
        for key in value.keys() & props.keys():
            shape(value[key], props[key], root, at + "." + key)
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", len(value)):
            raise Refused(at + ": invalid item count")
        if schema.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            raise Refused(at + ": duplicate items")
        if "items" in schema:
            for index, child in enumerate(value):
                shape(child, schema["items"], root, at + "[" + str(index) + "]")
    if isinstance(value, str):
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value)):
            raise Refused(at + ": invalid string length")
        # ECMA-262 "$" is end of input. Python's also matches before a final newline,
        # which would let "claude\n" pass as a token and differ from "claude".
        pattern = schema.get("pattern")
        if pattern is not None and pattern.endswith("$"):
            # An odd run of backslashes before it means the dollar itself is escaped.
            body = pattern[:-1]
            if (len(body) - len(body.rstrip("\\"))) % 2 == 0:
                pattern = body + "\\Z"
        if pattern is not None and re.search(pattern, value) is None:
            raise Refused(at + ": invalid string format")
    if type(value) is int:
        if not schema.get("minimum", value) <= value <= schema.get("maximum", value):
            raise Refused(at + ": integer out of range")
    for child in schema.get("allOf", []):
        shape(value, child, root, at)
    if "oneOf" in schema:
        matches = 0
        for child in schema["oneOf"]:
            try:
                shape(value, child, root, at)
                matches += 1
            except Refused:
                pass
        if matches != 1:
            raise Refused(at + ": expected one supported variant")
    if "if" in schema:
        try:
            shape(value, schema["if"], root, at)
        except Refused:
            return
        shape(value, schema["then"], root, at)


def validate_shape(value, name, versions=(3,)):
    """Live validation accepts the current format only; reports may read older receipts."""
    version = value.get("version") if isinstance(value, dict) else None
    if type(version) is not int or version not in versions:
        raise Refused(name + ": unsupported format version")
    schema = decode(asset(name + "-v" + str(version) + ".schema.json"))
    check_schema(schema)
    shape(value, schema)
