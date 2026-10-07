from __future__ import annotations
import asyncio, csv, json, ssl, time, fnmatch
from abc import ABC, abstractmethod
from typing import Any
import aiohttp, psutil

from .models import Metric
from .utils import walk_json
from .config import CollectorCfg


# ---------- base ----------
class Collector(ABC):
    type_name: str = ""

    def __init__(self, cfg: CollectorCfg):
        self.name = cfg.name
        self.interval = cfg.interval
        self.options = cfg.options or {}

    @abstractmethod
    async def collect(self) -> list[Metric]: ...


REGISTRY: dict[str, type[Collector]] = {}


def register(cls: type[Collector]):
    REGISTRY[cls.type_name] = cls
    return cls


def build(cfg: CollectorCfg) -> Collector:
    if cfg.type not in REGISTRY:
        raise ValueError(f"Неизвестный тип коллектора: {cfg.type}")
    return REGISTRY[cfg.type](cfg)


# ---------- system ----------
@register
class SystemCollector(Collector):
    type_name = "system"

    async def collect(self) -> list[Metric]:
        o, out = self.options, []
        if o.get("cpu", True):
            pct = await asyncio.to_thread(psutil.cpu_percent, 0.5)
            out.append(Metric("sys.cpu.usage_percent", pct, self.name, unit="%"))
            l1, l5, l15 = psutil.getloadavg()
            out += [
                Metric("sys.load.1m",  l1,  self.name),
                Metric("sys.load.5m",  l5,  self.name),
                Metric("sys.load.15m", l15, self.name),
            ]
        if o.get("memory", True):
            m = psutil.virtual_memory()
            out += [
                Metric("sys.mem.used_percent", m.percent,   self.name, unit="%"),
                Metric("sys.mem.used_bytes",   m.used,      self.name, unit="bytes"),
                Metric("sys.mem.avail_bytes",  m.available, self.name, unit="bytes"),
            ]
        for path in o.get("disk", ["/"]):
            d = psutil.disk_usage(path)
            lbl = {"path": path}
            out += [
                Metric("sys.disk.used_percent", d.percent, self.name, lbl, "%"),
                Metric("sys.disk.free_bytes",   d.free,    self.name, lbl, "bytes"),
            ]
        for iface in o.get("net", []):
            io = psutil.net_io_counters(pernic=True).get(iface)
            if io:
                lbl = {"iface": iface}
                out += [
                    Metric("sys.net.rx_bytes_total", io.bytes_recv, self.name, lbl, "bytes"),
                    Metric("sys.net.tx_bytes_total", io.bytes_sent, self.name, lbl, "bytes"),
                ]
        if o.get("temp", False):
            try:
                for name, entries in psutil.sensors_temperatures().items():
                    for e in entries[:2]:
                        lbl = {"chip": name, "label": e.label or ""}
                        out.append(Metric("sys.temp.celsius", e.current, self.name, lbl, "°C"))
            except Exception:
                pass
        return out


# ---------- command ----------
@register
class CommandCollector(Collector):
    """Запускает команду и парсит stdout.
    parsers: regex | lines | json"""
    type_name = "command"

    async def collect(self) -> list[Metric]:
        o = self.options
        proc = await asyncio.create_subprocess_exec(
            *o["cmd"], stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate(o.get("input", "").encode())
        text = stdout.decode(errors="replace")
        parser = o.get("parser", "regex")
        if parser == "regex":
            return self._regex(text)
        if parser == "lines":
            return self._lines(text)
        if parser == "json":
            return self._json(text)
        return []

    def _regex(self, text: str) -> list[Metric]:
        import re
        out = []
        for spec in self.options.get("extract", []):
            for m in re.finditer(spec["pattern"], text, re.MULTILINE):
                try:
                    v = float(m.group(spec.get("value_group", 1)))
                except (ValueError, IndexError):
                    continue
                lbl = {k: m.group(v) for k, v in spec.get("labels", {}).items()}
                out.append(Metric(spec["name"], v, self.name, lbl, spec.get("unit", "")))
        return out

    def _lines(self, text: str) -> list[Metric]:
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            p = line.split()
            if len(p) < 2:
                continue
            try:
                v = float(p[1])
            except ValueError:
                continue
            out.append(Metric(p[0], v, self.name))
        return out

    def _json(self, text: str) -> list[Metric]:
        data = json.loads(text)
        out = []
        for spec in self.options.get("extract", []):
            for v in walk_json(data, spec["path"]):
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
                out.append(Metric(spec["name"], v, self.name,
                                  spec.get("labels", {}), spec.get("unit", "")))
        return out


# ---------- http_json ----------
@register
class HttpJsonCollector(Collector):
    type_name = "http_json"

    async def collect(self) -> list[Metric]:
        o = self.options
        ssl_ctx = None
        if o.get("verify_ssl") is False:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        async with aiohttp.ClientSession() as s:
            if "login" in o:
                lg = o["login"]
                async with s.post(lg["url"], json=lg.get("json"),
                                  data=lg.get("data"),
                                  ssl=ssl_ctx,
                                  timeout=aiohttp.ClientTimeout(total=10)) as r:
                    await r.read()

            async with s.request(
                o.get("method", "GET"), o["url"],
                headers=o.get("headers"), json=o.get("json"), data=o.get("data"),
                ssl=ssl_ctx, timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                data = json.loads(await r.text())

        out = []
        for spec in o.get("extract", []):
            for v in walk_json(data, spec["path"]):
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
                out.append(Metric(spec["name"], v, self.name,
                                  spec.get("labels", {}), spec.get("unit", "")))
        return out


# ---------- haproxy ----------
@register
class HaproxyCollector(Collector):
    """Коллектор HAProxy stats.

    Адресация stats-сокета:
      - tcp://host:port
      - unix:///path/to/sock
      - /plain/path.sock

    HAProxy НЕ закрывает соединение после 'show stat' — читаем чанками
    с таймаутом 'тишины' (read_timeout), а не до EOF. RST при уже
    полученных данных — штатный сценарий, выходим тихо.

    Что попадает в метрики:
      servers: true (дефолт) — по одной серии на каждый сервер бэкенда
                (svname = xray1, ep1, ...). Именно здесь видно падение
                конкретного endpoint'а.
      backend: true          — агрегатная строка бэкенда (svname=BACKEND).
                По умолчанию выключено: она маскирует падение одного
                сервера, пока живы остальные.
      FRONTEND-строки не собираются никогда.
    """
    type_name = "haproxy"

    async def _connect(self, target: str):
        if "://" in target:
            scheme, _, addr = target.partition("://")
        else:
            scheme, addr = "unix", target

        if scheme == "unix":
            return await asyncio.open_unix_connection(addr)
        if scheme == "tcp":
            host, _, port = addr.rpartition(":")
            return await asyncio.open_connection(host, int(port))
        raise ValueError(f"Неизвестная схема сокета: {scheme!r}")

    async def _read_until_idle(self, reader, read_timeout: float,
                               total_timeout: float) -> bytes:
        chunks: list[bytes] = []
        loop = asyncio.get_event_loop()
        deadline = loop.time() + total_timeout
        while loop.time() < deadline:
            try:
                chunk = await asyncio.wait_for(reader.read(65536),
                                               timeout=read_timeout)
            except asyncio.TimeoutError:
                break
            except ConnectionResetError as e:
                if chunks:
                    break
                raise RuntimeError(
                    "HAProxy закрыл соединение до отдачи данных "
                    "(проверь stats socket в конфиге)"
                ) from e
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    async def collect(self) -> list[Metric]:
        o = self.options
        target = o["socket"]
        try:
            reader, writer = await self._connect(target)
        except OSError as e:
            raise RuntimeError(
                f"HAProxy stats {target} недоступен: {e.strerror or e}"
            ) from e

        try:
            writer.write(b"show stat\n")
            await writer.drain()
            raw = await self._read_until_idle(
                reader,
                read_timeout=float(o.get("read_timeout", 1.5)),
                total_timeout=float(o.get("total_timeout", 8.0)),
            )
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

        lines = raw.decode(errors="replace").splitlines()
        if not lines:
            return []

        header = lines[0].lstrip("# ").split(",")
        include_servers = o.get("servers", True)
        include_backend = o.get("backend", False)
        include_proxies = o.get("proxies")
        status_map = o.get("status_map") or {}
        out: list[Metric] = []

        for line in lines[1:]:
            if not line:
                continue
            row = next(csv.reader([line]))
            rec = dict(zip(header, row))
            px, sv = rec.get("pxname", ""), rec.get("svname", "")

            # FRONTEND-строки — агрегат по всему входному трафику. Не нужны.
            if sv == "FRONTEND":
                continue

            is_server = sv != "BACKEND"
            if is_server and not include_servers:
                continue
            if not is_server and not include_backend:
                continue

            if include_proxies and not any(
                fnmatch.fnmatch(px, pat) for pat in include_proxies
            ):
                continue

            lbl = {"proxy": px, "server": sv, "instance": self.name}

            for f in o.get("fields", []):
                val = rec.get(f["csv"])
                if val in (None, ""):
                    continue
                try:
                    v = float(val)
                except ValueError:
                    continue
                out.append(Metric(
                    f"haproxy.{f['metric']}", v, self.name, lbl, f.get("unit", ""),
                ))

            if status_map:
                st = rec.get("status", "")
                if st in status_map:
                    out.append(Metric(
                        "haproxy.status", float(status_map[st]), self.name, lbl,
                    ))

        return out


# ---------- wireguard ----------
@register
class WireguardCollector(Collector):
    type_name = "wireguard"

    async def collect(self) -> list[Metric]:
        proc = await asyncio.create_subprocess_exec(
            "wg", "show", "all", "dump",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        out: list[Metric] = []
        for line in stdout.decode(errors="replace").splitlines():
            p = line.split("\t")
            if len(p) < 8:
                continue
            iface, pub, _psk, endpoint, allowed, hs, rx, tx = p[:8]
            lbl = {"iface": iface, "peer": pub[:12], "endpoint": endpoint}
            try:
                out.append(Metric("wg.peer.last_handshake_seconds",
                                  float(hs), self.name, lbl, "s"))
                out.append(Metric("wg.peer.rx_bytes", float(rx), self.name, lbl, "bytes"))
                out.append(Metric("wg.peer.tx_bytes", float(tx), self.name, lbl, "bytes"))
            except ValueError:
                continue
        return out


# ---------- 3x-ui ----------
@register
class XuiCollector(Collector):
    """Коллектор 3x-ui.

    Аутентификация:
      auth: bearer -> Authorization: Bearer <api_token>
      auth: cookie -> Cookie: 3x-ui=<api_token>

    base_url — корень панели без /panel, например http://127.0.0.1:2053.
    Пути настраиваются в options.paths (в разных форках они отличаются).
    """
    type_name = "xui"

    def _headers(self) -> dict[str, str]:
        auth = self.options.get("auth", "bearer").lower()
        token = self.options["api_token"]
        if auth == "bearer":
            return {"Authorization": f"Bearer {token}",
                    "Accept": "application/json"}
        if auth == "cookie":
            val = token if token.startswith("3x-ui=") else f"3x-ui={token}"
            return {"Cookie": val, "Accept": "application/json"}
        raise ValueError(f"Неизвестный auth={auth!r} (bearer|cookie)")

    async def _get_json(self, session, url, ssl_ctx, timeout):
        async with session.get(url, ssl=ssl_ctx,
                               timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            body = await r.text()
            if r.status != 200:
                raise RuntimeError(f"xui {url} HTTP {r.status}: {body[:200]}")
            return json.loads(body)

    async def collect(self) -> list[Metric]:
        o = self.options
        base = o["base_url"].rstrip("/")
        if base.endswith("/panel"):
            base = base[:-len("/panel")]

        ssl_ctx = None
        if o.get("verify_ssl") is False:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        paths = o.get("paths", {})
        status_path = paths.get("server_status", "/server/status")
        inb_path    = paths.get("inbounds",      "/panel/api/inbounds/list")

        headers = self._headers()
        include = set(o.get("include", ["inbounds", "server_status"]))
        out: list[Metric] = []

        async with aiohttp.ClientSession(headers=headers) as s:
            if "server_status" in include:
                j = await self._get_json(s, f"{base}{status_path}", ssl_ctx, 10)
                obj = j.get("obj") or j
                mapping = {
                    "cpu":         ("xui.server.cpu_percent",  "%"),
                    "mem.current": ("xui.server.mem_bytes",    "bytes"),
                    "mem.total":   ("xui.server.mem_total",    "bytes"),
                    "uptime":      ("xui.server.uptime_sec",   "s"),
                    "xray.state":  ("xui.server.xray_running", ""),
                }
                for path, (name, unit) in mapping.items():
                    cur = obj
                    for part in path.split("."):
                        cur = cur.get(part) if isinstance(cur, dict) else None
                        if cur is None:
                            break
                    if cur is None:
                        continue
                    if name.endswith("running"):
                        cur = float(bool(cur))
                    try:
                        out.append(Metric(name, float(cur), self.name, unit=unit))
                    except (TypeError, ValueError):
                        pass

            if "inbounds" in include:
                j = await self._get_json(s, f"{base}{inb_path}", ssl_ctx, 15)
                for ib in j.get("obj", []) or []:
                    lbl = {"id": str(ib.get("id")),
                           "remark": ib.get("remark", ""),
                           "port": str(ib.get("port", ""))}
                    out.append(Metric("xui.inbound.up",
                                      float(ib.get("up", 0)),
                                      self.name, lbl, "bytes"))
                    out.append(Metric("xui.inbound.down",
                                      float(ib.get("down", 0)),
                                      self.name, lbl, "bytes"))
                    out.append(Metric("xui.inbound.enable",
                                      float(bool(ib.get("enable"))),
                                      self.name, lbl))

        return out
