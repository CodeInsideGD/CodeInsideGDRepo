from __future__ import annotations
import asyncio, csv, json, re, ssl, time, fnmatch
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


# ---------- nginx ----------
@register
class NginxCollector(Collector):
    """Мониторинг nginx: статус сервиса, слушаемые порты, апстримы.

    Работает в двух режимах:

      1. Локальный nginx (по умолчанию):
           check_cmd: ["systemctl", "is-active", "nginx"]
           config_cmd: ["nginx", "-T"]

      2. nginx в docker:
           check_cmd: ["docker", "exec", "nginx", "pgrep", "-c", "nginx"]
           config_cmd: ["docker", "exec", "nginx", "nginx", "-T"]

    Сервис считается работающим, если check_cmd завершилась с кодом 0.
    Конфиг читается через config_cmd, парсятся:
      - listen-директивы → список (host, port) для TCP-проверки
      - upstream-блоки → список (name, host:port) для TCP-проверки

    Для каждого порта и каждого апстрима делается попытка TCP-connect.
    Это отличает «есть в конфиге» от «реально работает».
    """
    type_name = "nginx"

    # Regex для извлечения listen-адресов из вывода 'nginx -T'.
    # Примеры: 'listen 80;', 'listen 443 ssl;', 'listen 127.0.0.1:8080;',
    #          'listen [::]:80 ipv6only=on;', 'listen unix:/path/sock;'
    _RE_LISTEN = re.compile(r"^\s*listen\s+([^;]+);", re.MULTILINE)
    _RE_UPSTREAM_OPEN = re.compile(r"^\s*upstream\s+(\S+)\s*\{")
    _RE_SERVER_LINE = re.compile(r"^\s*server\s+([^\s;]+)")

    async def _run(self, cmd: list[str]) -> tuple[int, str, str]:
        """Запускает команду, возвращает (returncode, stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return (proc.returncode or 0,
                out.decode(errors="replace"),
                err.decode(errors="replace"))

    async def _check_tcp(self, host: str, port: int,
                         timeout: float = 2.0) -> bool:
        """Проверяет TCP-доступность host:port."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True
        except Exception:
            return False

    def _parse_listen(self, arg: str) -> tuple[str, int] | None:
        """Разбирает аргумент listen-директивы.

        '80'              → ('127.0.0.1', 80)
        '443 ssl'         → ('127.0.0.1', 443)
        '127.0.0.1:8080'  → ('127.0.0.1', 8080)
        '[::]:80'         → ('::1', 80)
        '[::1]:8080'      → ('::1', 8080)
        'unix:/path'      → None
        """
        addr = arg.split()[0] if arg.split() else ""
        if not addr or addr.startswith("unix:"):
            return None

        if addr.startswith("["):
            idx = addr.find("]")
            if idx < 0:
                return None
            host = addr[1:idx]
            rest = addr[idx + 1:]
            if not rest.startswith(":") or not rest[1:].isdigit():
                return None
            port = int(rest[1:])
            if host in ("::", ""):
                host = "::1"
            return host, port

        if ":" in addr:
            host, port_str = addr.rsplit(":", 1)
            if not port_str.isdigit():
                return None
            if host in ("0.0.0.0", ""):
                host = "127.0.0.1"
            return host, int(port_str)

        if addr.isdigit():
            return "127.0.0.1", int(addr)
        return None

    def _parse_upstreams(self, config: str) -> list[tuple[str, str]]:
        """Возвращает список (upstream_name, 'host:port') из конфига.

        Парсер построчный: отслеживает вход в блок 'upstream NAME {'
        и выход по '}'. Внутри блока берёт строки 'server ...'.
        """
        result: list[tuple[str, str]] = []
        current: str | None = None
        for line in config.splitlines():
            m = self._RE_UPSTREAM_OPEN.match(line)
            if m:
                current = m.group(1)
                continue
            if current is not None and line.strip() == "}":
                current = None
                continue
            if current is not None:
                m = self._RE_SERVER_LINE.match(line)
                if m:
                    addr = m.group(1)
                    if addr.startswith("unix:"):
                        continue
                    result.append((current, addr))
        return result

    async def collect(self) -> list[Metric]:
        o = self.options
        check_cmd = o.get("check_cmd", ["systemctl", "is-active", "nginx"])
        config_cmd = o.get("config_cmd", ["nginx", "-T"])
        timeout = float(o.get("connect_timeout", 2.0))

        out: list[Metric] = []

        # --- 1. Статус сервиса ------------------------------------------------
        rc, _, _ = await self._run(check_cmd)
        service_running = 1.0 if rc == 0 else 0.0
        out.append(Metric("nginx.service.running", service_running, self.name))

        # --- 2. Читаем конфиг -------------------------------------------------
        rc, cfg_out, cfg_err = await self._run(config_cmd)
        config_valid = 1.0 if rc == 0 else 0.0
        out.append(Metric("nginx.config.valid", config_valid, self.name))

        if not config_valid:
            # Конфиг битый — дальше парсить нечего.
            return out

        # --- 3. Слушаемые порты ----------------------------------------------
        # Собираем уникальные (host, port) из всех listen-директив.
        seen: set[tuple[str, int]] = set()
        for m in self._RE_LISTEN.finditer(cfg_out):
            parsed = self._parse_listen(m.group(1))
            if parsed and parsed not in seen:
                seen.add(parsed)

        for host, port in sorted(seen):
            alive = await self._check_tcp(host, port, timeout)
            out.append(Metric(
                "nginx.port.listening",
                1.0 if alive else 0.0,
                self.name,
                {"host": host, "port": str(port)},
            ))

        # --- 4. Апстримы ------------------------------------------------------
        for upname, server in self._parse_upstreams(cfg_out):
            host, _, port_str = server.rpartition(":")
            if not host or not port_str.isdigit():
                continue
            port = int(port_str)
            if host in ("0.0.0.0", ""):
                host = "127.0.0.1"
            alive = await self._check_tcp(host, port, timeout)
            out.append(Metric(
                "nginx.upstream.status",
                1.0 if alive else 0.0,
                self.name,
                {"upstream": upname, "server": server},
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
