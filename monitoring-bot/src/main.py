from __future__ import annotations
import asyncio, sys, traceback

from .config import load_config
from .collectors import build
from .store import MetricStore
from .rules import Rule, RuleEngine
from .bot import MonitorBot


async def collector_loop(c, store: MetricStore, stop: asyncio.Event):
    while not stop.is_set():
        try:
            metrics = await c.collect()
            for m in metrics:
                store.put(m)
            store.mark_ok(c.name)
        except Exception as e:
            store.mark_err(c.name, f"{type(e).__name__}: {e}")
            traceback.print_exc()
        try:
            await asyncio.wait_for(stop.wait(), timeout=c.interval)
        except asyncio.TimeoutError:
            pass


async def rules_loop(store: MetricStore, engine: RuleEngine, bot: MonitorBot,
                     tick: float, stop: asyncio.Event):
    while not stop.is_set():
        try:
            await engine.process(store.snapshot(), bot.silenced)
        except Exception:
            traceback.print_exc()
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick)
        except asyncio.TimeoutError:
            pass


async def main(config_path: str):
    cfg = load_config(config_path)
    store = MetricStore(history=240)

    # пока bot не создан — заглушка-буфер для алертов на старте
    bot = MonitorBot.__new__(MonitorBot)   # noqa — создадим после engine

    # Правильный порядок: создаём engine с временными обработчиками,
    # потом bot, потом подменяем обработчики.
    holders = {}

    async def on_fire(alert, text):
        if "bot" in holders:
            await holders["bot"].notify_alert(alert, text)

    async def on_resolve(alert):
        if "bot" in holders:
            await holders["bot"].notify_resolve(alert)

    engine = RuleEngine(
        rules=[Rule(r) for r in cfg.rules],
        on_fire=on_fire, on_resolve=on_resolve,
    )
    bot = MonitorBot(cfg, store, engine)
    holders["bot"] = bot

    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(collector_loop(build(c), store, stop), name=f"col:{c.name}")
        for c in cfg.collectors
    ]
    tasks.append(asyncio.create_task(
        rules_loop(store, engine, bot, cfg.poll_tick, stop), name="rules"))

    try:
        await bot.dp.start_polling(bot.bot)
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "config.yaml"))
