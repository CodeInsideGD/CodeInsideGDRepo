from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass                     # ← было: @dataclass(slots=True)
class Metric:
    name: str
    value: float
    source: str
    labels: dict[str, str] = field(default_factory=dict)
    unit: str = ""
    ts: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        if not self.labels:
            return self.name
        lbl = ",".join(f"{k}={v}" for k, v in sorted(self.labels.items()))
        return f"{self.name}{{{lbl}}}"
