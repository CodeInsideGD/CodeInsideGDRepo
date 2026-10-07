"""Движок правил.

Правило описывает условие на метрику и длительность, в течение которой
условие должно держаться, чтобы сработал алерт. Один RuleEngine обслуживает
все правила; он же гасит алерты, когда метрика вернулась в норму, и
автоматически сбрасывает «зависшие» алерты, чьи метрики пропали из store.

Правило матчится по полному имени метрики или по её ключу (name+labels),
дополнительно фильтруется по статическим лейблам из конфига.
"""
from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from .config import RuleCfg
from .models import Metric
from .utils import fmt_bytes, parse_duration


# ---------------------------------------------------------------------------
# Модель активного алерта
# ---------------------------------------------------------------------------

@dataclass
class ActiveAlert:
    """Алерт, который сейчас «горит» — условие держится дольше for_."""

    rule: str
    key: str
    since: float
    metric: Metric
    severity: str


# ---------------------------------------------------------------------------
# Правило
# ---------------------------------------------------------------------------

class Rule:
    """Одно правило: match + when + for + шаблон сообщения."""

    def __init__(self, cfg: RuleCfg):
        self.cfg = cfg
        self.duration = parse_duration(cfg.for_)

    @property
    def short_metric(self) -> str:
        """Короткое имя метрики для отображения — из конфига, или полное."""
        return self.cfg.metric_short or self.cfg.match

    def matches(self, m: Metric) -> bool:
        """Проверяет, относится ли метрика к этому правилу.

        Основное совпадение — по glob-паттерну cfg.match против имени
        метрики и её ключа (с лейблами). Дополнительно — по статическим
        лейблам из cfg.labels: например, {instance: haproxy-in}.
        """
        if not (fnmatch.fnmatch(m.name, self.cfg.match)
                or fnmatch.fnmatch(m.key, self.cfg.match)):
            return False
        for k, v in self.cfg.labels.items():
            if m.labels.get(k) != v:
                return False
        return True

    def evaluate(self, m: Metric) -> bool:
        """Вычисляет выражение cfg.when для конкретной метрики.

        Доступные переменные: value, labels, source. Встроенные функции
        отключены — выражение пишется только на числах и сравнениях.
        """
        env = {"value": m.value, "labels": m.labels, "source": m.source}
        try:
            return bool(eval(self.cfg.when, {"__builtins__": {}}, env))
        except Exception:
            return False

    def render(self, m: Metric) -> str:
        """Собирает текст сообщения из шаблона cfg.message.

        В шаблон подставляются:
          {value}    — сырое значение
          {value_h}  — человекочитаемое (для байтов — GiB/MiB, иначе value)
          {source}   — имя коллектора
          {name}     — имя метрики
          {labels}   — словарь лейблов целиком
          {<label>}  — любой лейбл метрики напрямую: {proxy}, {path}, {port}

        Ключевые поля идут последними, чтобы не перекрываться одноимёнными
        лейблами — на этот случай остаётся доступ через {labels[<key>]}.
        """
        ctx = {
            **m.labels,
            "value": m.value,
            "value_h": fmt_bytes(m.value) if m.unit == "bytes" else m.value,
            "labels": m.labels,
            "source": m.source,
            "name": m.name,
        }
        try:
            return self.cfg.message.format(**ctx) or f"{self.cfg.name} сработало"
        except Exception:
            return f"{self.cfg.name}: value={m.value}"


# ---------------------------------------------------------------------------
# Движок
# ---------------------------------------------------------------------------

class RuleEngine:
    """Прогоняет метрики через все правила и управляет состоянием алертов.

    Двухфазная логика для одной серии (правило + метрика):
      1. Условие истинно → запоминаем время первого срабатывания.
         Если держится дольше for_ — поднимаем алерт (если ещё не поднят).
      2. Условие ложно → сбрасываем таймер ожидания и, если алерт был
         поднят, шлём resolve.

    Отдельным проходом сбрасываются алерты, чьи метрики исчезли из
    снапшота — например, коллектор перестал их отдавать.
    """

    def __init__(
        self,
        rules: list[Rule],
        on_fire: Callable[[ActiveAlert, str], Awaitable[None]],
        on_resolve: Callable[[ActiveAlert], Awaitable[None]],
    ):
        self.rules = rules
        self.on_fire = on_fire
        self.on_resolve = on_resolve
        # (rule_name, metric_key) → время первого срабатывания
        self._pending: dict[tuple[str, str], float] = {}
        # (rule_name, metric_key) → активный алерт
        self.active: dict[tuple[str, str], ActiveAlert] = {}

    async def process(self, metrics: list[Metric], silenced: set[str]) -> None:
        """Прогнать снапшот метрик через все правила.

        silenced — имена правил, для которых алерты подавлены. Срабатывание
        всё равно фиксируется в state, но сообщение не отправляется.
        """
        seen: set[tuple[str, str]] = set()

        for m in metrics:
            for r in self.rules:
                if not r.matches(m):
                    continue

                key = (r.cfg.name, m.key)
                seen.add(key)

                if r.evaluate(m):
                    await self._raise_if_due(r, m, key, silenced)
                else:
                    await self._resolve(key)

        # Метрики, которых больше нет в снапшоте, — снимаем их алерты.
        for key in list(self.active.keys()):
            if key not in seen:
                await self.on_resolve(self.active.pop(key))

    # -- внутренние переходы -------------------------------------------------

    async def _raise_if_due(
        self, r: Rule, m: Metric, key: tuple[str, str], silenced: set[str],
    ) -> None:
        first_seen = self._pending.setdefault(key, time.time())

        if key in self.active:
            return                                    # уже горит
        if time.time() - first_seen < r.duration:
            return                                    # for_ ещё не выдержан
        if r.cfg.name in silenced:
            return                                    # алерт подавлен вручную

        alert = ActiveAlert(
            rule=r.cfg.name,
            key=m.key,
            since=first_seen,
            metric=m,
            severity=r.cfg.severity,
        )
        self.active[key] = alert
        await self.on_fire(alert, r.render(m))

    async def _resolve(self, key: tuple[str, str]) -> None:
        self._pending.pop(key, None)
        if key in self.active:
            await self.on_resolve(self.active.pop(key))
