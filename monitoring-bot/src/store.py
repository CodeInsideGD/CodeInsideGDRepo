from __future__ import annotations
import time
from collections import deque, defaultdict
from .models import Metric


class MetricStore:
    """Кольцевой буфер последних значений по каждому metric.key
    + быстрый доступ к last."""

    def __init__(self, history: int = 120):
        self.history = history
        self.series: dict[str, deque[tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=history))
        self.last: dict[str, Metric] = {}
        self.last_source_ok: dict[str, float] = {}
        self.last_source_err: dict[str, str] = {}

    def put(self, m: Metric):
        self.series[m.key].append((m.ts, m.value))
        self.last[m.key] = m

    def mark_ok(self, source: str):
        self.last_source_ok[source] = time.time()
        self.last_source_err.pop(source, None)

    def mark_err(self, source: str, err: str):
        self.last_source_err[source] = f"{time.strftime('%H:%M:%S')} {err}"

    def snapshot(self) -> list[Metric]:
        return list(self.last.values())

    def find(self, pattern: str) -> list[Metric]:
        import fnmatch
        return [m for m in self.last.values()
                if fnmatch.fnmatch(m.name, pattern) or fnmatch.fnmatch(m.key, pattern)]
