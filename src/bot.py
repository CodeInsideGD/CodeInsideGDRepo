from __future__ import annotations

import fnmatch
import os
import re
import socket
import time

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.client.session.aiohttp import AiohttpSession

from .config import AppConfig
from .store import MetricStore
from .rules import RuleEngine, ActiveAlert
from .utils import fmt_bytes


class MonitorBot:
    def __init__(self, cfg: AppConfig, store: MetricStore, engine: RuleEngine):
        self.cfg = cfg
        self.store = store
        self.engine = engine

        session = AiohttpSession(timeout=int(os.environ.get("TG_TIMEOUT", "30")))
        self._tune_connector(session)

        # Без parse_mode: сообщения отправляются как plain text.
        # Это избавляет от ошибок Telegram при символах < и > в условиях
        # правил (например, 'status < 1'), которые HTML-парсер принимает
        # за начало тега.
        self.bot = Bot(cfg.token, session=session)
        self.dp = Dispatcher()
        self.silenced: set[str] = set()
        self._register()

    @staticmethod
    def _tune_connector(session: AiohttpSession) -> None:
        """Подмешивает в AiohttpSession безопасные параметры TCPConnector.

        На этом хосте IPv4-адреса Telegram blackhole'ятся, работает только IPv6,
        поэтому форсим family=AF_INET6. Параметры вроде happy_eyeballs_delay
        поддерживаются не всеми версиями aiohttp — передаём только те, что
        реально есть в сигнатуре TCPConnector установленной версии.
        """
        import inspect
        from aiohttp import TCPConnector

        supported = set(inspect.signature(TCPConnector.__init__).parameters)
        desired = {
            "family": socket.AF_INET6,
            "ttl_dns_cache": 300,
            "happy_eyeballs_delay": None,
            "interleave": None,
        }
        session._connector_init.update(
            {k: v for k, v in desired.items() if k in supported}
        )

    def _register(self):
        @self.dp.message(Command("start"))
        async def _start(m: Message):
            await m.answer(
                "Server Monitor\n"
                "/status — сводка\n"
                "/metrics [pattern] — метрики\n"
                "/alerts — активные алерты\n"
                "/rules — список правил\n"
                "/silence NAME — заглушить правило\n"
                "/unsilence NAME — вернуть\n"
                "/collectors — состояние источников"
            )

        @self.dp.message(Command("status"))
        async def _status(m: Message):
            await m.answer(self._render_status(), disable_notification=True)

        @self.dp.message(Command("metrics"))
        async def _metrics(m: Message):
            parts = (m.text or "").split(maxsplit=1)
            pat = parts[1].strip() if len(parts) > 1 else "*"
            await m.answer(self._render_metrics(pat), disable_notification=True)

        @self.dp.message(Command("alerts"))
        async def _alerts(m: Message):
            if not self.engine.active:
                await m.answer("Активных алертов нет")
                return
            lines = []
            for a in self.engine.active.values():
                dur = int(time.time() - a.since)
                lines.append(
                    f"[{a.severity.upper()}] {a.rule} ({dur}s)\n"
                    f"  {a.metric.key} = {a.metric.value}"
                )
            await m.answer("\n".join(lines), disable_notification=True)

        @self.dp.message(Command("rules"))
        async def _rules(m: Message):
            await m.answer(_render_rules(self.engine.rules, self.silenced),
                           disable_notification=True)

        @self.dp.message(Command("silence"))
        async def _silence(m: Message):
            parts = (m.text or "").split()
            if len(parts) < 2:
                await m.answer("usage: /silence NAME")
                return
            self.silenced.add(parts[1])
            await m.answer(f"Заглушено: {parts[1]}")

        @self.dp.message(Command("unsilence"))
        async def _unsilence(m: Message):
            parts = (m.text or "").split()
            if len(parts) >= 2:
                self.silenced.discard(parts[1])
            await m.answer("ok")

        @self.dp.message(Command("collectors"))
        async def _collectors(m: Message):
            lines = []
            for c in self.cfg.collectors:
                ok = self.store.last_source_ok.get(c.name)
                err = self.store.last_source_err.get(c.name)
                if err:
                    state = f"ошибка: {err}"
                elif ok:
                    state = f"ok, {int(time.time()-ok)}s назад"
                else:
                    state = "нет данных"
                lines.append(f"{c.name} ({c.type}, {c.interval}s) — {state}")
            await m.answer("\n".join(lines), disable_notification=True)

    def _render_status(self) -> str:
        by = {m.name: m for m in self.store.snapshot()}
        def g(n): return by.get(n)
        def v(n, fmt="{}"):
            m = g(n)
            return fmt.format(m.value) if m else "-"
        cpu  = v("sys.cpu.usage_percent", "{:.1f}%")
        mem  = v("sys.mem.used_percent",  "{:.1f}%")
        free = "-"
        if (m := g("sys.disk.free_bytes")):
            free = fmt_bytes(m.value)
        l1 = v("sys.load.1m", "{:.2f}")
        alerts = len(self.engine.active)
        return (f"CPU: {cpu}\n"
                f"RAM: {mem}\n"
                f"Диск /: {free}\n"
                f"Load 1m: {l1}\n"
                f"Алертов: {alerts}")

    def _render_metrics(self, pattern: str) -> str:
        all_metrics = self.store.snapshot()
        matched = [
            m for m in all_metrics
            if fnmatch.fnmatch(m.name, pattern)
            or fnmatch.fnmatch(m.key, pattern)
        ]
        if not matched:
            return f"Нет метрик по '{pattern}'"

        by_ns: dict[str, list] = {}
        for m in matched:
            ns = m.name.split(".", 1)[0]
            by_ns.setdefault(ns, []).append(m)

        max_lines = 100
        emitted = 0

        head = f"Метрики ({len(matched)})"
        if pattern != "*":
            head += f"  ·  {pattern}"
        parts = [head]

        for ns in sorted(by_ns):
            if emitted >= max_lines:
                break
            group = sorted(by_ns[ns], key=lambda x: x.key)
            parts.append(f"\n{ns}")
            for m in group:
                if emitted >= max_lines:
                    break
                short = m.name[len(ns) + 1:] if m.name.startswith(ns + ".") else m.name
                if m.labels:
                    lbls = ",".join(f"{k}={v}" for k, v in sorted(m.labels.items()))
                    short = f"{short}{{{lbls}}}"
                val = _format_metric_value(m.value, m.unit)
                parts.append(f"  {short} = {val}")
                emitted += 1

        if emitted < len(matched):
            parts.append(f"\n… (показано {emitted} из {len(matched)})")
        return "\n".join(parts)

    async def notify_alert(self, alert: ActiveAlert, text: str):
        body = (f"[{alert.severity.upper()}] {alert.rule}\n"
                f"{text}\n"
                f"{alert.metric.key}")
        for cid in self.cfg.chat_ids:
            try:
                await self.bot.send_message(cid, body)
            except Exception as e:
                print(f"send_message error: {e}")

    async def notify_resolve(self, alert: ActiveAlert):
        body = (f"[resolved] {alert.rule}\n"
                f"{alert.metric.key}")
        for cid in self.cfg.chat_ids:
            try:
                await self.bot.send_message(cid, body, disable_notification=True)
            except Exception:
                pass


def _format_threshold(value: float) -> str:
    if abs(value) >= 10 * 1024 * 1024:
        return fmt_bytes(value)
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


def _describe_when(when: str) -> str:
    parts = []
    for cond in when.split(" and "):
        cond = cond.strip()
        m = re.match(r"value\s*([<>]=?|==|!=)\s*(\S+)", cond)
        if not m:
            parts.append(cond)
            continue
        op, raw = m.group(1), m.group(2)
        try:
            num = float(eval(raw, {"__builtins__": {}}, {}))
        except Exception:
            parts.append(f"{op} {raw}")
            continue
        parts.append(f"{op} {_format_threshold(num)}")
    return " and ".join(parts)


def _format_for(for_: str) -> str:
    return "сразу" if for_.strip() in ("0s", "0", "0m") else for_


def _format_metric_value(value: float, unit: str) -> str:
    if unit == "bytes":
        return fmt_bytes(value)
    if unit == "%":
        return f"{value:.1f}%"
    if unit == "s":
        return _format_seconds(value)
    if unit == "°C":
        return f"{value:.1f}°C"
    if value == int(value) and abs(value) < 1e12:
        return str(int(value))
    return f"{value:g}"


def _format_seconds(s: float) -> str:
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s / 60:.1f}m"
    if s < 86400:
        return f"{s / 3600:.1f}h"
    return f"{s / 86400:.1f}d"


def _render_rules(rules, silenced: set[str]) -> str:
    if not rules:
        return "Правил нет"

    by_target: dict[str, list] = {}
    for r in rules:
        target = r.cfg.name.split(".", 1)[0]
        by_target.setdefault(target, []).append(r)

    lines = [f"Правила ({len(rules)})"]
    for target, group in by_target.items():
        lines.append(f"\n{target}")
        for r in group:
            rid = r.cfg.name.split(".", 1)[1]
            cond = _describe_when(r.cfg.when)
            duration = _format_for(r.cfg.for_)
            sil = "  silenced" if r.cfg.name in silenced else ""
            lines.append(
                f"  {rid}    {r.short_metric} {cond}    {duration}    "
                f"{r.cfg.severity}{sil}"
            )
    return "\n".join(lines)
