"""Lossless JSON form of saves.

Top level: {"format": "qsp-save", "engine": "5.7.0" | "5.9", "encoding": ..., "save": {...}}.

Text that JSON/UTF-8 cannot carry -- a lone UTF-16 surrogate, which the
engines store happily -- is written as {"parts": ["text", 55296, "text"]}:
plain strings interleaved with the offending code units as numbers. That
keeps the rest of the text readable and unpack -> pack byte-exact.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from . import v59, v570
from .codec import Encoding, SaveFormatError

FORMAT = "qsp-save"
MODULES = {v570.ENGINE: v570, v59.ENGINE: v59}


def _is_surrogate(ch: str) -> bool:
    return 0xD800 <= ord(ch) <= 0xDFFF


def _dump_text(text: str) -> Any:
    if not any(map(_is_surrogate, text)):
        return text
    parts: list[str | int] = []
    chunk = []
    for ch in text:
        if _is_surrogate(ch):
            if chunk:
                parts.append("".join(chunk))
                chunk = []
            parts.append(ord(ch))
        else:
            chunk.append(ch)
    if chunk:
        parts.append("".join(chunk))
    return {"parts": parts}


def _load_text(value: Any, where: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and set(value) == {"parts"} and isinstance(value["parts"], list):
        out = []
        for part in value["parts"]:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, int) and not isinstance(part, bool) and 0xD800 <= part <= 0xDFFF:
                out.append(chr(part))
            else:
                raise SaveFormatError(f"{where}: text parts must be strings or surrogate code units, got {part!r}")
        return "".join(out)
    raise SaveFormatError(f"{where}: expected text, got {value!r}")


def _dump_variant(v: v59.Variant) -> dict[str, Any]:
    if isinstance(v.value, list):
        value: Any = [_dump_variant(item) for item in v.value]
    elif isinstance(v.value, str):
        value = _dump_text(v.value)
    else:
        value = v.value
    return {"type": v.type.name, "value": value}


def _load_variant(d: dict[str, Any], where: str) -> v59.Variant:
    try:
        var_type = v59.VarType[d["type"]] if isinstance(d["type"], str) else v59.VarType(d["type"])
    except (KeyError, ValueError):
        raise SaveFormatError(f"{where}: unknown value type {d.get('type')!r}") from None
    raw = d.get("value")
    match v59.BASE_TYPE[var_type]:
        case v59.VarType.TUPLE:
            if not isinstance(raw, list):
                raise SaveFormatError(f"{where}: a tuple value must be a list")
            value: Any = [_load_variant(item, f"{where}[{i}]") for i, item in enumerate(raw)]
        case v59.VarType.NUM:
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise SaveFormatError(f"{where}: a {var_type.name} value must be an integer")
            value = raw
        case _:
            value = _load_text(raw, where)
    return v59.Variant(var_type, value)


def _dump(obj: Any) -> Any:
    if isinstance(obj, v59.Variant):
        return _dump_variant(obj)
    if dataclasses.is_dataclass(obj):
        return {f.name: _dump(getattr(obj, f.name)) for f in dataclasses.fields(obj) if f.name != "encoding"}
    if isinstance(obj, list):
        return [_dump(item) for item in obj]
    if isinstance(obj, str):
        return _dump_text(obj)
    return obj


def _load(cls: type, data: Any, where: str) -> Any:
    if cls is v59.Variant:
        return _load_variant(data, where)
    if not isinstance(data, dict):
        raise SaveFormatError(f"{where}: expected an object")
    hints = {f.name: f for f in dataclasses.fields(cls) if f.name != "encoding"}
    unknown = set(data) - hints.keys()
    if unknown:
        raise SaveFormatError(f"{where}: unknown field(s) {', '.join(sorted(unknown))}")
    kwargs = {}
    for name, f in hints.items():
        if name not in data:
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
                raise SaveFormatError(f"{where}: missing field {name!r}")
            continue
        kwargs[name] = _load_value(f.type, data[name], f"{where}.{name}", cls)
    return cls(**kwargs)


def _load_value(annotation: str, value: Any, where: str, owner: type) -> Any:
    # Annotations are strings (from __future__ import annotations); resolve the
    # handful of shapes used by the save models.
    module = v59 if owner.__module__ == v59.__name__ else v570
    if annotation.startswith("list[") and annotation.endswith("]"):
        if not isinstance(value, list):
            raise SaveFormatError(f"{where}: expected a list")
        inner = annotation[5:-1]
        return [_load_value(inner, item, f"{where}[{i}]", owner) for i, item in enumerate(value)]
    if annotation == "str":
        return _load_text(value, where)
    if annotation == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SaveFormatError(f"{where}: expected an integer")
        return value
    if annotation == "bool":
        if not isinstance(value, bool):
            raise SaveFormatError(f"{where}: expected true or false")
        return value
    if annotation == "VarType":
        return v59.VarType[value]
    cls = getattr(module, annotation, None)
    if cls is None or not dataclasses.is_dataclass(cls):
        raise TypeError(f"unsupported annotation {annotation!r}")
    return _load(cls, value, where)


def to_json(save: v570.Save | v59.Save) -> dict[str, Any]:
    engine = v570.ENGINE if isinstance(save, v570.Save) else v59.ENGINE
    return {"format": FORMAT, "engine": engine, "encoding": save.encoding.value, "save": _dump(save)}


def from_json(doc: dict[str, Any]) -> v570.Save | v59.Save:
    if doc.get("format") != FORMAT:
        raise SaveFormatError(f"not a qspsav JSON document (format={doc.get('format')!r})")
    module = MODULES.get(doc.get("engine"))
    if module is None:
        raise SaveFormatError(f"unknown engine {doc.get('engine')!r}, expected one of {', '.join(MODULES)}")
    save = _load(module.Save, doc.get("save"), "save")
    try:
        save.encoding = Encoding(doc.get("encoding", Encoding.UCS2.value))
    except ValueError:
        raise SaveFormatError(f"unknown encoding {doc.get('encoding')!r}") from None
    if module is v570 and save.encoding is not Encoding.UCS2:
        raise SaveFormatError("5.7.0 saves are always UCS-2")
    return save


def dumps(save: v570.Save | v59.Save) -> str:
    return json.dumps(to_json(save), ensure_ascii=False, indent=2) + "\n"


def loads(text: str) -> v570.Save | v59.Save:
    return from_json(json.loads(text))
