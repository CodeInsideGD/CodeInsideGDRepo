# monitoring-bot

Telegram-бот для мониторинга серверных метрик: системные ресурсы,
HAProxy (входной/выходной), WireGuard, панель 3x-ui.

## Возможности

- Сбор метрик с разных источников через **коллекторы**
  (system, haproxy, wireguard, xui, http_json, command).
- Гибкие правила: пороги, длительность, severity, шаблоны сообщений.
- Команды в Telegram: `/status`, `/metrics`, `/rules`, `/alerts`,
  `/collectors`, `/silence`, `/unsilence`.
- Конфигурация через YAML + `.env` для секретов.

## Архитектура
collectors → MetricStore → RuleEngine → Telegram


- **Коллекторы** опрашивают источники и возвращают список `Metric`.
- **MetricStore** — кольцевой буфер последних значений.
- **RuleEngine** прогоняет метрики через правила и поднимает/сбрасывает алерты.
- **Бот** отвечает на команды и рассылает уведомления.

## Установка

```bash
git clone git@github.com:CodeInsideGD/monitoring-bot.git
cd monitoring-bot

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
# заполнить .env своими значениями

# отредактировать config.yaml под свой стенд
.venv/bin/python -m src.main config.yaml



