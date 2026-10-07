from __future__ import annotations

import fnmatch
from typing import Any, Iterator

import fnmatch
from typing import Any, Iterator


def walk_json(obj: Any, path: str) -> Iterator[Any]:
    """Очень простой JSONPath: точки, * для list/dict.
    Пример: obj.*.up  → пройтись по всем элементам obj и взять up."""
    if not path:
        yield obj
        return
    parts = path.split(".", 1)
    head, rest = parts[0], parts[1] if len(parts) > 1 else ""
    if head == "*":
        if isinstance(obj, list):
            for it in obj:
                yield from walk_json(it, rest)
        elif isinstance(obj, dict):
            for v in obj.values():
                yield from walk_json(v, rest)
    else:
        if isinstance(obj, dict) and head in obj:
            yield from walk_json(obj[head], rest)


def parse_duration(s: str | int | float) -> float:
    if isinstance(s, (int, float)):
        return float(s)
    s = s.strip().lower()
    if s.endswith("ms"): return float(s[:-2]) / 1000
    if s.endswith("s"):  return float(s[:-1])
    if s.endswith("m"):  return float(s[:-1]) * 60
    if s.endswith("h"):  return float(s[:-1]) * 3600
    return float(s)


def fmt_bytes(v: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(v) < 1024:
            return f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} PiB"
