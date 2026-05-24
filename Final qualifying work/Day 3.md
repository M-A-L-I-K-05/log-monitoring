#vkr #day-03 #docker-compose #loki #promtail #grafana

# День 3 — Docker Compose, стек логирования и сквозной pipeline

> Дата: 2026-05-13 — 2026-05-14 Статус: ✅ завершён

---

# Contents

- [[#День 3 — Docker Compose, стек логирования и сквозной pipeline]]
    - [[#Цели на день]]
    - [[#Что сделано]]
        - [[#1 Структура проекта]]
        - [[#2 docker-compose.yml для четырёх сервисов и Postgres]]
        - [[#3 Переименование полей в логах]]
        - [[#4 Loki, конфиг и интеграция]]
        - [[#5 Promtail, конфиг и docker_sd_configs]]
        - [[#6 Grafana и provisioning datasource]]
        - [[#7 Проверка end-to-end]]
    - [[#Ключевые концепции для запоминания]]
        - [[#docker.sock как API, не файл с логами]]
        - [[#Метаданные Docker vs содержимое логов]]
        - [[#Архитектура хранения Loki]]
        - [[#Multi-tenancy и tenant ID]]
        - [[#Promtail и фильтрация по Docker label]]
        - [[#JsonFormatter, rename_fields и extra]]
        - [[#Immutability образов и dangling layers]]
        - [[#Exec form в Compose и healthcheck]]
        - [[#High cardinality, почему labels должно быть мало]]
    - [[#Шероховатости и компромиссы]]
    - [[#План на завтра — День 4]]
        - [[#Модель завода]]
        - [[#Ревизия событий и эндпоинтов]]
        - [[#Сценарии симулятора]]
        - [[#Архитектура симулятора и виртуальные часы]]
        - [[#SQL-схема для Postgres, проектирование]]
        - [[#Принцип «где что живёт»]]
        - [[#Что НЕ делаем завтра]]

---

## Цели на день

- [x] Создать единый `docker-compose.yml` для четырёх FastAPI-сервисов
- [x] Добавить PostgreSQL в стек (без интеграции с сервисами, просто запускается)
- [x] Bind mount на код сервисов + `--reload` для dev-режима
- [x] Healthcheck для Postgres + `depends_on: service_healthy` для сервисов
- [x] Переименовать поля логов: `name` → `service`, `message` → `event`
- [x] Добавить Loki в compose с минимальным конфигом
- [x] Добавить Promtail с `docker_sd_configs` и фильтром по docker-label
- [x] Добавить Grafana с автопровижинингом datasource Loki
- [x] Убедиться через Grafana Explore, что логи попадают в Loki и фильтруются по labels

---

## Что сделано

### 1. Структура проекта

~/projects/vkr-log-monitoring/
├── docker-compose.yml
├── grafana/
│   └── provisioning/
│       ├── dashboards/
│       └── datasources/
│           └── datasources.yaml
├── loki/
│   └── loki-config.yaml
├── promtail/
│   └── promtail-config.yaml
├── postgres/
│   └── init/
├── services/
│   ├── equipment/
│   ├── production/
│   ├── quality/
│   └── maintenance/
├── ml/                 # пусто, для дней 7–12
├── simulator/          # пусто, для дней 5–6
└── webui/              # пусто, для дней 5–6

К концу дня в репозитории появились: 1 файл compose, 3 конфига для observability-стека и правки в `main.py` всех четырёх сервисов.

---

### 2. docker-compose.yml для четырёх сервисов и Postgres

Финальный файл объединяет 7 сервисов в одну сеть: `postgres`, `equipment`, `production`, `quality`, `maintenance`, `loki`, `promtail`, `grafana`.

Ключевые принципы, реализованные в файле:

- **`build` для собственных сервисов, `image` для готовых.** Четыре FastAPI собираются из локальных Dockerfile, Postgres / Loki / Promtail / Grafana — берутся из Docker Hub с зафиксированными версиями.
- **Bind mount + `--reload`.** Код каждого сервиса монтируется в `/app`, uvicorn запущен с `--reload` → правишь файл на хосте → контейнер сам перезапускает приложение, без `docker compose restart` и без `--build`.
- **Healthcheck Postgres.** Сервисы стартуют только после `condition: service_healthy` базы. Без этого FastAPI падал бы первые секунды с «connection refused», когда Postgres ещё инициализируется.
- **`labels: logging: "promtail"`** на четырёх сервисах. Promtail собирает логи только с контейнеров с этой меткой. Postgres помечен не был — его шумные логи в Loki не идут.
- **Exec form для `command`.** Везде массивы вместо строк: `["uvicorn", "main:app", ...]`. Корректная обработка сигналов и PID 1.
- **Named volume `postgres-data`** для персистенса базы. Переживёт `docker compose down`.
- **Named volume `loki-data`** для чанков и индекса Loki.
- **`user: "0"`** на Loki — обход типовых permission denied на volume.

Запуск всего стека:
```bash
docker compose up -d
```

Один файл, одна команда, поднимается всё.

---

### 3. Переименование полей в логах

В Day 2 в JSON-логе одновременно были `name` и `message`, причём `message` дублировал значение из `extra.event`. Сегодня почищено.

В `setup_logging` всех четырёх сервисов добавлены два правила переименования:

```python
formatter = jsonlogger.JsonFormatter(
    "%(asctime)s %(levelname)s %(name)s %(message)s",
    rename_fields={
        "asctime": "timestamp",
        "levelname": "level",
        "name": "service",
        "message": "event",
    },
)
```

Эндпоинты упростились:
```python
# было
logger.info("sensor_reading", extra={"event": "sensor_reading", "entity_id": ..., "details": ...})
# стало
logger.info("sensor_reading", extra={"entity_id": ..., "details": ...})
```

Слово `sensor_reading` теперь автоматически становится значением поля `event` через rename. Поле `message` исчезло из JSON, дублирование снято.

Итоговая схема:
```json
{
  "timestamp": "2026-05-14 19:54:37,484",
  "level": "INFO",
  "service": "equipment",
  "event": "sensor_reading",
  "entity_id": "M03",
  "details": {"temperature": 75.5, "vibration": 0.4}
}
```

---

### 4. Loki: конфиг и интеграция

Создан `loki/loki-config.yaml`:

```yaml
auth_enabled: false

server:
  http_listen_port: 3100

common:
  instance_addr: 127.0.0.1
  path_prefix: /loki
  storage:
    filesystem:
      chunks_directory: /loki/chunks
      rules_directory: /loki/rules
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory

schema_config:
  configs:
    - from: 2024-01-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h
```

Разбор по параметрам:

- `auth_enabled: false` — multi-tenancy выключена, все данные пишутся под дефолтным tenant ID `fake`.
- `server.http_listen_port: 3100` — порт API. К нему ходит Promtail (push) и Grafana (query).
- `common.storage.filesystem` — хранение чанков на локальном диске. Альтернативы (S3, GCS) не нужны.
- `replication_factor: 1` — одна реплика, single-binary режим.
- `ring.kvstore.store: inmemory` — координация компонентов держится в памяти.
- `schema_config tsdb + v13` — современная схема индекса. Дата `from: 2024-01-01` покрывает все логи.

В compose:
```yaml
loki:
  image: grafana/loki:3.0.0
  container_name: loki
  user: "0"
  ports:
    - "3100:3100"
  volumes:
    - ./loki/loki-config.yaml:/etc/loki/loki-config.yaml:ro
    - loki-data:/loki
  command: ["-config.file=/etc/loki/loki-config.yaml"]
  restart: unless-stopped
```

Проверка после запуска: `curl http://localhost:3100/ready` отвечает `ready`.

---

### 5. Promtail: конфиг и docker_sd_configs

Создан `promtail/promtail-config.yaml`:

```yaml
server:
  http_listen_port: 9080
  grpc_listen_port: 0

positions:
  filename: /tmp/positions.yaml

clients:
  - url: http://loki:3100/loki/api/v1/push

scrape_configs:
  - job_name: docker
    docker_sd_configs:
      - host: unix:///var/run/docker.sock
        refresh_interval: 5s
        filters:
          - name: label
            values: ["logging=promtail"]
    relabel_configs:
      - source_labels: [__meta_docker_container_label_com_docker_compose_service]
        target_label: service_name
    pipeline_stages:
      - json:
          expressions:
            level: level
            event: event
      - labels:
          level: ""
          event: ""
```

Разбор по секциям:

- **`docker_sd_configs`** — service discovery через Docker socket. Promtail сам опрашивает Docker и узнаёт, какие контейнеры запущены.
- **`filters: logging=promtail`** — собирать логи только с контейнеров, имеющих docker-label `logging=promtail` (четыре наших сервиса). Postgres отфильтрован.
- **`relabel_configs`** — берём метаданные `__meta_docker_container_label_com_docker_compose_service` (имя compose-сервиса) и выставляем как label `service_name` в Loki. Это «откуда лог пришёл».
- **`pipeline_stages.json`** — парсим тело лога как JSON, извлекаем поля `level` и `event`.
- **`pipeline_stages.labels`** — превращаем извлечённые поля в labels Loki. Теперь по `level` и `event` тоже можно фильтровать на уровне индекса.

В compose:
```yaml
promtail:
  image: grafana/promtail:3.0.0
  container_name: promtail
  restart: unless-stopped
  command: ["-config.file=/etc/promtail/promtail-config.yaml"]
  volumes:
    - ./promtail/promtail-config.yaml:/etc/promtail/promtail-config.yaml:ro
    - /var/lib/docker/containers:/var/lib/docker/containers:ro
    - /var/run/docker.sock:/var/run/docker.sock
  depends_on:
    - loki
```

Промонтированы два внешних пути с хоста:
- `/var/run/docker.sock` — для service discovery.
- `/var/lib/docker/containers` — где Docker хранит файлы логов (read-only).

---

### 6. Grafana и provisioning datasource

Создан `grafana/provisioning/datasources/datasources.yaml`:

```yaml
apiVersion: 1

datasources:
  - name: Loki
    type: loki
    access: proxy
    orgId: 1
    url: http://loki:3100
    basicAuth: false
    isDefault: true
    version: 1
    editable: true
```

Это автопровижининг: при старте Grafana сама создаёт datasource Loki, не нужно тыкать в GUI.

В compose:
```yaml
grafana:
  image: grafana/grafana:11.0.0
  container_name: grafana
  restart: unless-stopped
  ports:
    - "3000:3000"
  environment:
    - GF_PATHS_PROVISIONING=/etc/grafana/provisioning
    - GF_AUTH_ANONYMOUS_ENABLED=true
    - GF_AUTH_ANONYMOUS_ORG_ROLE=Admin
  volumes:
    - ./grafana/provisioning:/etc/grafana/provisioning:ro
    - grafana-data:/var/lib/grafana
  depends_on:
    - loki
```

Анонимный вход с правами Admin — для dev-режима нормально, в production был бы пароль.

---

### 7. Проверка end-to-end

```bash
# Запуск всего стека
docker compose up -d

# Curl-запросы для генерации событий
curl http://localhost:8001/health
curl -X POST http://localhost:8001/sensor-reading \
     -H "Content-Type: application/json" \
     -d '{"machine_id":"M03","temperature":75.5,"vibration":0.4}'
# ... и т.д.
```

В Grafana → Explore → Loki, запрос:

```logql
{service_name="maintenance", event="health_check"}
```

Запрос вернул записи с label `service_name=maintenance` и `event=health_check`. Pipeline работает целиком:

FastAPI → stdout → Docker → Promtail → Loki → Grafana

Это момент, когда система впервые «видна как система»: одна точка наблюдения за всеми сервисами.

---

## Ключевые концепции для запоминания

### docker.sock как API, не файл с логами

- `/var/run/docker.sock` — это **UNIX-сокет**, не текстовый файл.
- Концептуально — локальный HTTP-сервер Docker daemon, доступный через файловую систему вместо сетевого порта.
- К нему обращаются запросами, получают JSON-ответы.
- Проверка из терминала: `curl --unix-socket /var/run/docker.sock http://localhost/containers/json` вернёт массив JSON с метаданными всех контейнеров.

### Метаданные Docker vs содержимое логов

Две разные категории информации, которые Promtail сшивает:

| Метаданные Docker | Содержимое логов |
|---|---|
| Имя контейнера, ID, образ | JSON-строки из stdout |
| Docker labels (`logging=promtail`) | Поля `event`, `level`, `details` |
| Источник: API через `docker.sock` | Источник: файлы `/var/lib/docker/containers/.../*-json.log` |

Promtail знает метаданные **заранее** (получил при service discovery) и привязывает их к каждой лог-строке из потока этого контейнера. Никакой «корреляции двух источников» нет — есть привязка контекста к потоку.

### Архитектура хранения Loki

Loki строится на двух идеях:

**1. Stream** — уникальное сочетание labels = отдельный поток. Логи внутри одного stream хранятся вместе.
- `{service_name="equipment", level="INFO", event="health_check"}` → один stream
- `{service_name="equipment", level="ERROR", event="alarm"}` → другой stream

**2. Index (сочетание stream и временного участка) отдельно от Chunks**
- Index содержит **только labels** и ссылки на чанки. Маленький, быстрый.
- Chunks содержат **тело логов** в сжатом виде (gzip). Большие, читаются только при запросе.

Запрос `{service_name="equipment"} |= "alarm"` выполняется так:
1. Index ищет все streams с label `service_name=equipment`.
2. Загружаются их chunks за нужный диапазон времени.
3. Grep по содержимому в памяти.

Отличие от Elasticsearch: ELK индексирует каждое слово → дорогой и быстрый поиск. Loki индексирует только labels → дёшево хранить, медленнее grep по содержимому. Это сознательный компромисс.

Физически в нашем случае:

/loki/
├── chunks/
│   └── fake/          # чанки (fake — дефолтный tenant)
├── index/             # индекс labels
├── wal/               # write-ahead log
└── compactor/         # рабочая папка фоновой компактификации

### Multi-tenancy и tenant ID

- Loki поддерживает разделение данных между «арендаторами».
- Включается через `auth_enabled: true` + заголовок `X-Scope-OrgID` в каждом запросе клиента.
- У нас выключено → все данные пишутся под дефолтным tenant ID **`fake`**.
- Нужно для SaaS-сценариев, изоляции команд, разделения окружений dev/staging/prod.
- Для одного проекта не нужно, но в записке упомянуть как «архитектурную возможность» можно.

### Promtail и фильтрация по Docker label

В compose-файле каждому контейнеру можно вешать произвольные labels:
```yaml
labels:
  logging: "promtail"
```

Это **метаданные Docker**, не часть логов. Используются как **фильтр**: Promtail подписан только на контейнеры с таким label. Postgres его не имеет → его логи в Loki не пойдут.

Удобный механизм: контролируешь, что мониторится, прямо из compose-файла, без правок конфига Promtail.

### JsonFormatter, rename_fields и extra

Особенность `python-json-logger`, отличающая его от стандартного `Formatter`:

| Стандартный `Formatter` | `JsonFormatter` |
|---|---|
| Format-строка — шаблон финального текста | Format-строка — список **полей LogRecord**, которые включить в JSON |
| `extra={"event": "x"}` нужно явно прописать как `%(event)s` | `extra` добавляется в JSON **автоматически** |

Поэтому код:
```python
logger.info("sensor_reading", extra={"entity_id": "M03", "details": {...}})
```

с format-строкой `"%(asctime)s %(levelname)s %(name)s %(message)s"` даёт JSON, в котором есть и стандартные поля (timestamp, level, service, event), и поля из `extra` (entity_id, details). Format-строка отвечает за **встроенные поля LogRecord**, `extra` — за **бизнесовые** поля. Они работают параллельно.

### Immutability образов и dangling layers

- Docker layers — content-addressable: ID = SHA256 от содержимого.
- Image — манифест, ссылающийся на последовательность layer-ID.
- Tag — указатель на манифест, обычная метка.

При rebuild:
- Если ничего не изменилось → все layers cached, тот же манифест, ничего не создаётся.
- Если изменился код в build context → `COPY . .` получает новый hash → новый layer + новый манифест. Tag переуказывается на новый, старый становится **dangling** (без тега).

Dangling images накапливаются как мусор. Очистка:
```bash
docker image prune       # удалить dangling
docker system prune      # + контейнеры, сети
```

**В dev-цикле с bind mount + `--reload`** повторный `--build` для правки только кода — лишняя работа: код подхватывается через монтирование, образ пересобирать не нужно. `--build` нужен только при изменении `Dockerfile` или `requirements.txt`.

### Exec form в Compose и healthcheck

Везде, где запускается процесс, используется exec form (массив):

```yaml
command: ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001", "--reload"]

healthcheck:
  test: ["CMD", "pg_isready", "-U", "admin", "-d", "factory"]
```

В healthcheck первый элемент массива — специальный маркер для Docker, не bash-команда:

| Маркер | Поведение |
|---|---|
| `CMD` | exec напрямую, без shell |
| `CMD-SHELL` | обернуть остальное в `/bin/sh -c`, нужно для pipes и подстановки `$VAR` |
| `NONE` | отключить унаследованный из image healthcheck |

Концептуально похоже на форму CMD в Dockerfile, но синтаксически явно.

### High cardinality, почему labels должно быть мало

В Loki каждое уникальное сочетание labels → новый stream. Streams индексируются индивидуально.

Если выбрать labels с высокой кардинальностью (например, `user_id`, `request_id`, `machine_id`):
- Каждое значение → отдельный stream.
- Streams растут лавинообразно.
- Index распухает.
- Loki начинает тормозить или падать.

Правило: 3-5 labels с **низкой кардинальностью** (десятки уникальных значений максимум). У нас сейчас: `service_name` (4 значения), `level` (~3 значения), `event` (~15 значений). Безопасно.

Поля с высокой кардинальностью (`entity_id`, `batch_id`, `order_id`) **не должны** становиться labels. Они живут в теле лога и доступны через `| json | entity_id="M03"` при запросах. Это медленнее, но не разрушает индекс.

---

## Шероховатости и компромиссы

- **Дублирование `service_name` (label) и `service` (поле JSON).** Намеренно: первое — индекс для быстрых запросов, второе — содержимое лога. Можно было оставить только одно, но двойственность даёт гибкость: label для быстрого поиска, поле для аналитики после `| json`.
- **`user: "0"` для Loki.** Запуск под root внутри контейнера — обход типовых permission denied на volume. Для дипломного проекта приемлемо, в production решали бы через корректную настройку прав.
- **Анонимный Admin в Grafana.** Удобно для dev-режима, в production был бы реальный логин с паролем.
- **`__pycache__` папки попали в bind mount.** Кеш Python генерируется внутри контейнера и виден на хосте. Не мешает, но косметика на потом — добавить `.dockerignore` со строкой `__pycache__`.
- **Promtail официально EOL с 2 марта 2026.** Для дипломного проекта не критично, в записке упоминается как известное ограничение и направление развития. Замена — Grafana Alloy, миграция через `alloy convert`. Не делаем сейчас, лишняя работа.
- **Логи uvicorn идут в нашем формате через root logger.** Поле `service` для них принимает значение `uvicorn.error` или `uvicorn.access` (имя соответствующего логгера), а не имя микросервиса. Не критично: для Loki источник определяется через label `service_name` (имя контейнера), не через JSON-поле.

---

## План на завтра — День 4

> Цель: спроектировать концепцию полностью — модель завода, события, сценарии симулятора, схему БД, архитектуру симулятора. **Без кода**. Артефакт дня — развёрнутый Obsidian-документ, по которому в дни 5-7 будет идти механическая реализация без вопросов «что делать».

### Модель завода

- [ ] Определить количество машин на каждом из 7 участков (заготовительный, токарный, зубообрабатывающий, термический, шлифовальный, контрольный, упаковка). Без излишней детализации, ровно столько, чтобы было правдоподобно.
- [ ] Для каждой машины — ID, тип, нормальные параметры (рабочая температура, вибрация, длительность цикла).
- [ ] Зафиксировать в таблице или схеме (drawio).
- [ ] Цель ВКР не в моделировании завода, а в системе логирования. Уровень детализации выбирается соответственно.

### Ревизия событий и эндпоинтов

- [ ] Пройтись по четырём существующим сервисам и их Pydantic-моделям.
- [ ] Если в процессе дизайна обнаружится, что какие-то поля не нужны, а каких-то не хватает — записать в TODO для дня 5.
- [ ] Решить, нужны ли подтипы события (если да — добавлять через значения поля `event`, не через новые эндпоинты).

### Сценарии симулятора

- [ ] **Нормальная работа** — фоновый поток событий. Определить частоту: сколько событий в минуту в нормальном режиме генерирует каждый участок.
- [ ] **3-4 аномалии** — конкретные сценарии:
    - Перегрев оборудования.
    - Каскадный отказ.
    - Вспышка брака.
    - Тихая деградация.
- [ ] Для каждой аномалии — описать какие именно события генерируются, в какой последовательности, как они отличаются от нормы статистически. Это критично: IsolationForest должен иметь шанс их детектировать.

### Архитектура симулятора и виртуальные часы

- [ ] Решить: симулятор — отдельный сервис в compose. Имеет свой `Dockerfile`, шлёт HTTP-запросы к четырём сервисам через docker network, экспонирует управляющий HTTP API.
- [ ] **Виртуальные часы**: класс `VirtualClock`, методы `tick()`, `set_speed(multiplier)`. Один тик симулятора = N миллисекунд реального времени. Скорости: ×1, ×10, ×100, ×1000.
- [ ] Где хранится «состояние мира» симулятора: в памяти процесса, пока не в БД.
- [ ] Управляющий HTTP API: `POST /scenario/start`, `POST /scenario/stop`, `POST /speed`, `GET /status`.

### SQL-схема для Postgres (проектирование)

- [ ] Прикинуть таблицы: справочники (`machines`, `work_centers`) + транзакционные (`orders`, `batches`, `measurements`, `work_orders`).
- [ ] Прикинуть колонки и связи между таблицами.
- [ ] Опционально — нарисовать ER-диаграмму в drawio.
- [ ] Никакого кодирования миграций сегодня — только схема на бумаге.

### Принцип «где что живёт»

К концу дня должен быть чёткий ответ на вопрос: для каждого куска данных — где он хранится и почему.

Базовое разделение:
- **В логи** — события с временной меткой, происходящие однократно: `sensor_reading`, `alarm`, `batch_start`, `measurement`. Это история.
- **В Postgres** — справочники и накопительное состояние: список машин, активные заказы, открытые наряды на ТО. Это «настоящее».
- **В памяти симулятора** — модель машин, текущее состояние «мира». Пока без Postgres, потом часть переедет в БД.

### Что НЕ делаем завтра

- ❌ Не пишем код симулятора. Только дизайн.
- ❌ Не подключаем Postgres к сервисам через SQLAlchemy. Только схема таблиц на бумаге.
- ❌ Не делаем WebUI. Дизайн его API — да, реализацию — нет.
- ❌ Не трогаем Drain3, IsolationForest, Prophet.
- ❌ Не моделируем реальный КПП-завод во всех деталях. Берём ровно столько, сколько нужно для дипломного демо.

> Цель дня 4 — **полная карта проекта на бумаге**. Если завтра вечером посмотреть на документ в Obsidian, должно быть понятно, что делать в дни 5, 6 и 7, без необходимости снова возвращаться к проектированию.