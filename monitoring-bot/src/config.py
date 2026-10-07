"""Загрузка и валидация config.yaml.

Пользователь описывает источники метрик в секции targets. Каждый target
проходит через соответствующий preset: тот знает, какой коллектор создать,
какие дефолты применить, и как сопоставить короткие имена метрик из правил
полным именам в MetricStore.

Результат — AppConfig с плоскими списками collectors и rules, ровно в том
виде, в котором их ожидает ядро (main.py).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

import yaml
from dotenv import load_dotenv

from .presets import PRESETS, Preset


# ---------------------------------------------------------------------------
# Модели конфигурации
# ---------------------------------------------------------------------------

@dataclass
class CollectorCfg:
    type: str
    name: str
    interval: int
    options: dict


@dataclass
class RuleCfg:
    name: str
    match: str
    when: str
    for_: str
    severity: str
    message: str
    labels: dict
    metric_short: str = ""    # короткое имя метрики из конфига ('cpu', 'queue')


@dataclass
class AppConfig:
    token: str
    chat_ids: list[int]
    admin_ids: list[int]
    poll_tick: int
    collectors: list[CollectorCfg]
    rules: list[RuleCfg]


# ---------------------------------------------------------------------------
# Подстановка ${VAR} и ${VAR:-default} из окружения
# ---------------------------------------------------------------------------

_ENV_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(text: str) -> str:
    def repl(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        return os.environ.get(name, default if default is not None else "")
    return _ENV_RE.sub(repl, text)


def _parse_int_list(value: str) -> list[int]:
    return [int(x) for x in re.split(r"[,\s]+", value.strip()) if x]


# ---------------------------------------------------------------------------
# Компиляция target'а
# ---------------------------------------------------------------------------

# Ключи target'а верхнего уровня, не относящиеся к опциям коллектора.
_TARGET_META_KEYS = {"name", "preset", "interval", "rules", "options"}


def _target_options(preset: Preset, target: dict[str, Any]) -> dict[str, Any]:
    """Собирает options коллектора из defaults пресета и полей target'а.

    Приоритет (от низкого к высокому):
      1. preset.defaults               — что знает сам сервис
      2. поля target'а верхнего уровня — что написал пользователь
      3. target.options                — явный оверрайд поверх всего
    """
    options: dict[str, Any] = dict(preset.defaults)

    for key, value in target.items():
        if key in _TARGET_META_KEYS:
            continue
        options[preset.rename_keys.get(key, key)] = value

    options.update(target.get("options") or {})

    if preset.compile_options is not None:
        options = preset.compile_options(options)

    return options


def _compile_rule(target_name: str, rule: dict[str, Any],
                  preset: Preset) -> RuleCfg:
    """Собирает RuleCfg из компактного описания правила в target'е.

    Пример:
        - id: cpu-high
          metric: cpu
          above: 90
          for: 60s
          severity: warning
          say: "CPU {value:.1f}%"

    'metric' разворачивается в glob-паттерн через metric_aliases пресета.
    Условие собирается из above / below / equal.
    """
    rid = rule.get("id")
    if not rid:
        raise ValueError(f"target {target_name!r}: у правила нет id")

    metric = rule.get("metric")
    if not metric:
        # Явная подсказка: 'on' в YAML 1.1 парсится как bool True,
        # и пользователь получит загадочное «нет metric».
        if True in rule:
            raise ValueError(
                f"target {target_name!r}, правило {rid!r}: "
                f"ключ 'on' зарезервирован YAML как boolean, "
                f"используйте 'metric'"
            )
        raise ValueError(f"target {target_name!r}, правило {rid!r}: нет metric")

    # Короткое имя — берём полное из пресета; полное — используем как есть.
    pattern = preset.metric_aliases.get(metric, metric)

    conditions: list[str] = []
    if "above" in rule:
        conditions.append(f"value > {rule['above']}")
    if "below" in rule:
        conditions.append(f"value < {rule['below']}")
    if "equal" in rule:
        conditions.append(f"value == {rule['equal']}")
    if not conditions:
        raise ValueError(
            f"target {target_name!r}, правило {rid!r}: "
            f"укажите одно из above / below / equal"
        )

    return RuleCfg(
        name=f"{target_name}.{rid}",
        match=pattern,
        when=" and ".join(conditions),
        for_=rule.get("for", "0s"),
        severity=rule.get("severity", "warning"),
        message=rule.get("say", ""),
        labels=rule.get("labels") or {},
        metric_short=metric,
    )


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def load_config(path: str = "config.yaml", env_file: str = ".env") -> AppConfig:
    load_dotenv(env_file, override=False)

    try:
        token = os.environ["TG_TOKEN"]
        chat_ids = _parse_int_list(os.environ["TG_CHAT_IDS"])
    except KeyError as e:
        raise RuntimeError(f"Не задана переменная окружения: {e.args[0]}")

    admin_ids = _parse_int_list(os.environ.get("TG_ADMIN_IDS", ""))
    poll_tick = int(os.environ.get("POLL_TICK", "5"))

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(_expand_env(f.read())) or {}

    targets = raw.get("targets")
    if not targets:
        raise RuntimeError(f"{path}: секция targets пуста или отсутствует")

    collectors: list[CollectorCfg] = []
    rules: list[RuleCfg] = []
    seen: set[str] = set()

    for target in targets:
        name = target.get("name")
        if not name:
            raise ValueError("target без name")
        if name in seen:
            raise ValueError(f"target {name!r}: дубликат имени")
        seen.add(name)

        preset_name = target.get("preset")
        if not preset_name:
            raise ValueError(f"target {name!r}: не указан preset")
        if preset_name not in PRESETS:
            raise ValueError(
                f"target {name!r}: неизвестный preset {preset_name!r}. "
                f"Доступные: {sorted(PRESETS)}"
            )
        preset = PRESETS[preset_name]

        collectors.append(CollectorCfg(
            type=preset.collector_type,
            name=name,
            interval=int(target.get("interval", preset.default_interval)),
            options=_target_options(preset, target),
        ))

        for rule in target.get("rules") or []:
            rules.append(_compile_rule(name, rule, preset))

    return AppConfig(
        token=token,
        chat_ids=chat_ids,
        admin_ids=admin_ids,
        poll_tick=poll_tick,
        collectors=collectors,
        rules=rules,
    )
