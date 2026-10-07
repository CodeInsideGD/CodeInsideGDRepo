"""Presets — знание о сервисах, вынесенное из конфига в код.

Preset описывает один тип источника метрик: какой коллектор использовать,
какие дефолты применить, как связать короткие имена метрик из правил
с полными именами в MetricStore. Пользовательский config.yaml тогда
содержит только то, что действительно меняется от стенда к стенду.

Добавить новый сервис — значит добавить одну запись в PRESETS.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class Preset:
    """Описание одного сервиса-источника метрик."""

    collector_type: str
    """Имя типа в реестре collectors.REGISTRY."""

    default_interval: int = 15
    """Интервал опроса, если в target'е не указан явно."""

    defaults: dict[str, Any] = field(default_factory=dict)
    """Опции коллектора по умолчанию; пользовательские поля их перекрывают."""

    rename_keys: dict[str, str] = field(default_factory=dict)
    """Удобное имя ключа в target'е → реальная опция коллектора."""

    metric_aliases: dict[str, str] = field(default_factory=dict)
    """Короткое имя метрики для 'metric:' в правилах → полное имя в store."""

    compile_options: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    """Хук для преобразования options перед передачей в коллектор."""


# ---------------------------------------------------------------------------
# HAProxy
# ---------------------------------------------------------------------------

# Соответствие коротких имён метрик CSV-колонкам 'show stat'.
_HAPROXY_METRICS: dict[str, dict[str, str]] = {
    "sessions":          {"csv": "scur",  "metric": "current_sessions"},
    "queue":             {"csv": "qcur",  "metric": "queued"},
    "rate":              {"csv": "rate",  "metric": "session_rate"},
    "traffic_in":        {"csv": "bin",   "metric": "bytes_in",       "unit": "bytes"},
    "traffic_out":       {"csv": "bout",  "metric": "bytes_out",      "unit": "bytes"},
    "errors_response":   {"csv": "dresp", "metric": "response_errors"},
    "errors_connection": {"csv": "econ",  "metric": "connection_errors"},
    "retries":           {"csv": "wretr", "metric": "retries"},
}

# Строковый статус HAProxy → число (для правил вида 'below: 1').
_HAPROXY_STATUS_MAP: dict[str, int] = {
    "UP": 1, "DOWN": 0, "MAINT": -1, "DRAIN": -1, "no check": 1,
}


def _compile_haproxy_options(options: dict[str, Any]) -> dict[str, Any]:
    """Собирает 'fields' из короткого списка metrics: [sessions, queue, ...].

    'status' — особая метрика: она не входит в fields, а вычисляется
    коллектором через status_map.
    """
    metrics = options.pop("metrics", None)
    if metrics is None:
        return options

    fields: list[dict[str, str]] = []
    for name in metrics:
        if name == "status":
            continue
        spec = _HAPROXY_METRICS.get(name)
        if spec is None:
            raise ValueError(
                f"Неизвестная метрика haproxy: {name!r}. "
                f"Доступные: {sorted(_HAPROXY_METRICS)} + 'status'"
            )
        field = {"csv": spec["csv"], "metric": spec["metric"]}
        if "unit" in spec:
            field["unit"] = spec["unit"]
        fields.append(field)

    options["fields"] = fields
    options.setdefault("status_map", _HAPROXY_STATUS_MAP)
    return options


# ---------------------------------------------------------------------------
# http_json
# ---------------------------------------------------------------------------

def _compile_http_json_options(options: dict[str, Any]) -> dict[str, Any]:
    """Позволяет писать extract как словарь {metric_name: json_path}.

    Пользователь пишет:
        extract:
          nginx.active:  connections.active
          nginx.waiting: connections.waiting

    Коллектор ожидает список словарей; преобразуем на этапе загрузки.
    """
    extract = options.get("extract")
    if isinstance(extract, dict):
        options["extract"] = [
            {"name": name, "path": path} for name, path in extract.items()
        ]
    return options


# ---------------------------------------------------------------------------
# Реестр пресетов
# ---------------------------------------------------------------------------

PRESETS: dict[str, Preset] = {

    "system": Preset(
        collector_type="system",
        default_interval=10,
        defaults={"cpu": True, "memory": True, "disk": ["/"]},
        metric_aliases={
            "cpu":       "sys.cpu.usage_percent",
            "mem":       "sys.mem.used_percent",
            "mem_bytes": "sys.mem.used_bytes",
            "mem_avail": "sys.mem.avail_bytes",
            "load1":     "sys.load.1m",
            "load5":     "sys.load.5m",
            "load15":    "sys.load.15m",
            "disk_free": "sys.disk.free_bytes",
            "disk_used": "sys.disk.used_percent",
            "net_rx":    "sys.net.rx_bytes_total",
            "net_tx":    "sys.net.tx_bytes_total",
            "temp":      "sys.temp.celsius",
        },
    ),

    # По умолчанию собираем метрики по каждому серверу бэкенда отдельно
    # (servers: true). Агрегатную BACKEND-строку можно включить через
    # 'backend: true' в target'е, но обычно она не нужна: она маскирует
    # падение одного endpoint'а, пока живы остальные.
    "haproxy": Preset(
        collector_type="haproxy",
        default_interval=10,
        defaults={"servers": True, "backend": False},
        metric_aliases={
            "sessions":          "haproxy.current_sessions",
            "queue":             "haproxy.queued",
            "rate":              "haproxy.session_rate",
            "traffic_in":        "haproxy.bytes_in",
            "traffic_out":       "haproxy.bytes_out",
            "errors_response":   "haproxy.response_errors",
            "errors_connection": "haproxy.connection_errors",
            "retries":           "haproxy.retries",
            "status":            "haproxy.status",
        },
        compile_options=_compile_haproxy_options,
    ),

    "nginx": Preset(
        collector_type="nginx",
        default_interval=30,
        metric_aliases={
            "service":         "nginx.service.running",
            "config":          "nginx.config.valid",
            "port":            "nginx.port.listening",
            "upstream":        "nginx.upstream.status",
        },
    ),

    "xui": Preset(
        collector_type="xui",
        default_interval=30,
        defaults={
            "auth": "bearer",
            "verify_ssl": False,
            "include": ["server_status", "inbounds"],
        },
        rename_keys={"url": "base_url", "token": "api_token"},
        metric_aliases={
            "cpu":            "xui.server.cpu_percent",
            "mem":            "xui.server.mem_bytes",
            "mem_total":      "xui.server.mem_total",
            "uptime":         "xui.server.uptime_sec",
            "xray_running":   "xui.server.xray_running",
            "inbound.up":     "xui.inbound.up",
            "inbound.down":   "xui.inbound.down",
            "inbound.enable": "xui.inbound.enable",
        },
    ),

    "wireguard": Preset(
        collector_type="wireguard",
        default_interval=30,
        metric_aliases={
            "handshake": "wg.peer.last_handshake_seconds",
            "rx":        "wg.peer.rx_bytes",
            "tx":        "wg.peer.tx_bytes",
        },
    ),

    "http_json": Preset(
        collector_type="http_json",
        default_interval=15,
        compile_options=_compile_http_json_options,
    ),

    "command": Preset(
        collector_type="command",
        default_interval=15,
    ),
}
