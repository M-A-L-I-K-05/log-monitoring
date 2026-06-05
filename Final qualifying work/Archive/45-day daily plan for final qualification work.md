## TL;DR

- **Дни 1–15 (до предзащиты)**: жёсткий технический спринт. К дню 13 — end-to-end демо в одной `docker-compose up`; дни 14–15 — дашборды и слайды. Ключевые рисковые точки: Loki/Promtail (дни 4–5) и интеграция Drain3 → Isolation Forest (дни 9–10), на каждый заложен запасной день.
- **Дни 16–45 (до защиты)**: 70% времени — пояснительная записка (~60–70 страниц по 4 главам), 20% — доработки и стабилизация системы, 10% — презентация + защитное слово + 3 полные репетиции.
- **Что НЕ делать**: не лезть в DeepLog/LogBERT/Transformer, не писать ML с нуля, не менять стек, не переписывать код "красиво" в последнюю неделю. Ваша позиция на защите — «архитектурная интеграция готовых FOSS-компонентов», и она правильная.

---

## Key Findings (стратегический контекст перед планом)

1. **Loki/Promtail — главный rabbit hole.** Известные проблемы: `permission denied` на `/run/promtail/positions.yaml`, права на `/var/lib/docker/containers`, SELinux, неотображение ошибок прав на уровне `info`. Решение, которое экономит 1–2 дня: в `docker-compose.yml` для контейнеров loki и promtail сразу прописать `user: "0"` (root), смонтировать `/var/run/docker.sock` и использовать `docker_sd_configs` вместо чтения файлов. Promtail официально EOL с 2 марта 2026 — но для ВКР это не проблема, в записке честно укажите это и обоснуйте выбор. Альтернатива при затыке — Docker Loki logging driver (логи льются прямо из Docker без агента).
    
2. **Drain3 интуитивно прост.** Парс-дерево фиксированной глубины (по умолчанию `depth=4`), токенизация → группировка по длине → similarity threshold (`sim_th=0.4` по умолчанию) → шаблон вида `User <*> logged in`. Полностью укладывается в 3–4 часа изучения по README + одной статье.
    
3. **Isolation Forest — без математики на 1–2 часа.** Интуиция: аномалии «изолируются» меньшим числом случайных разбиений → короче путь в дереве → выше anomaly score (ближе к 1). Параметры на защиту: `contamination` (ожидаемая доля аномалий), `n_estimators=100`, `max_samples='auto'`. Подаётся вектор частот шаблонов в скользящем окне.
    
4. **Prophet vs ARIMA.** Для часовых данных за пару недель Prophet проще (`fit/predict` за 4 строки, автоматически берёт тренд + недельную/дневную сезонность). ARIMA требует выбора `(p,d,q)`, проверки стационарности (ADF-тест), ACF/PACF — больше времени, больше что объяснять. **Берите Prophet.** ARIMA оставьте как «рассмотренную альтернативу» в обзоре главы 1 ВКР.
    
5. **Архитектурный поток данных** (выучить наизусть, это вопрос №1 на защите):
    
    ```
    3 FastAPI-сервиса (структурированный JSON в stdout)
         ↓ (Docker logging driver / json-file)
    Promtail (читает /var/lib/docker/containers/*-json.log,
              pipeline_stages: json → labels)
         ↓ HTTP POST /loki/api/v1/push
    Loki (хранит чанки, индексирует только labels)
         ↓ LogQL HTTP /loki/api/v1/query_range
        ┌──────────────┴──────────────┐
    Grafana                       ML-service (FastAPI)
    (дашборды, LogQL)             ├─ pull логов окном (5–15 мин)
                                   ├─ Drain3 → шаблоны
                                   ├─ frequency vector
                                   ├─ IsolationForest.decision_function
                                   ├─ /anomalies, /forecast, /health
                                   └─ Prophet на серии error-count/min
    ```
    

---

## Details

# Часть 1. Поденный план до предзащиты (дни 1–15)

**Формат каждого дня**: 5 часов. Если день перевалил — режьте scope, не сидите ночью.

---

### День 1 — Реанимация проекта + структура репозитория

**Задачи (практика 4 ч):**

- [x] Создать чистый репозиторий: `vkr-log-monitoring/` с папками `services/` (микросервисы), `ml/` (ML-сервис), `loki/`, `promtail/`, `grafana/`, `compose.yml`.
- [x] Установить Docker Desktop / docker.io + docker-compose-plugin. Проверить: `docker run hello-world`, `docker compose version`.
- [ ] Зарисовать в Excalidraw / draw.io, сохранить PNG в `docs/architecture/`.
- [ ] Создать Obsidian-vault `vkr-notes/` со структурой: `00_meta`, `10_architecture`, `20_components`, `30_ml`, `40_defense`. Перенести диаграмму туда.
- [ ] Завести `BACKLOG.md` со списком 15-дневных задач — вычёркивать ежедневно.

**Изучение (1 ч):**

- Перечитать ТЗ ВКР, выписать формулировки целей/задач в Obsidian — пригодятся для введения записки.
- Просмотреть оглавление: https://docs.docker.com/compose/ (только Networking + Services раздела, 30 мин).

**Артефакт:** пустой, но структурированный репозиторий + диаграмма архитектуры + чек-лист на 15 дней.

**Связь с целью:** без скелета и плана 15 дней превратятся в хаос. Сегодня вы покупаете предсказуемость.

---

### День 2 — 2 FastAPI микросервиса с JSON-логированием

**Задачи (практика 4 ч):**

- [ ] `services/order-service/`: FastAPI с эндпоинтами `/orders` (POST), `/orders/{id}` (GET), `/health`. Имитировать «бизнес-логику»: иногда возвращать 500, иногда задержку.
- [ ] `services/auth-service/`: `/login` (POST), `/verify` (GET), `/health`. Аналогично.
- [ ] Подключить `python-json-logger` или `structlog` для структурированного JSON в stdout. Поля: `timestamp`, `level`, `service`, `event`, `request_id`, `user_id`, `latency_ms`. Запустить локально, убедиться что в консоли валидный JSON.
- [ ] Написать `services/load-generator/generate.py` — bash/python-скрипт, который дёргает эндпоинты в случайном порядке и иногда вызывает ошибки (это нужно, чтобы Isolation Forest имел что детектить).
- [ ] Каждый сервис — свой `Dockerfile` (`python:3.11-slim` + `uvicorn`).

**Изучение (1 ч):**

- https://www.sheshbabu.com/posts/fastapi-structured-logging/ (15 мин).
- https://betterstack.com/community/guides/logging/logging-with-fastapi/ — раздел «JSON formatting» (20 мин).
- https://apitally.io/blog/fastapi-logging-guide — раздел «python-json-logger» (15 мин).

**Артефакт:** 2 микросервиса с одинаковой схемой JSON-логов, генератор нагрузки.

**Связь с целью:** структурированные логи — фундамент. Без них Drain3 будет работать плохо, а Loki не даст красивых лейблов в Grafana.

---

### День 3 — Третий сервис + docker-compose только для приложений

**Задачи (практика 3.5 ч):**

- [ ] `services/payment-service/`: `/charge`, `/refund`, `/health`. Сделать его «болтуном» — много DEBUG/INFO логов + редкие ERROR-всплески (для имитации аномалии).
- [ ] `compose.app.yml` (только приложения, без observability): три сервиса + load-generator. Проверить, что `docker compose up` поднимает всё, эндпоинты доступны на разных портах (8001/8002/8003).
- [ ] Проверить, что Docker пишет логи в `/var/lib/docker/containers/<id>/<id>-json.log` (`docker inspect` найдёт путь). Это вход для Promtail.

**Изучение (1.5 ч):**

- https://docs.docker.com/compose/compose-file/ — только разделы `services`, `networks`, `volumes`, `depends_on` (45 мин).
- https://pytutorial.com/fastapi-microservices-with-docker-compose/ — пройти полностью (45 мин).

**Артефакт:** `docker compose -f compose.app.yml up` поднимает 3 сервиса, нагрузчик льёт трафик, в логах видны JSON-строки.

**Связь с целью:** теперь у вас есть «производственное микросервисное приложение» — фраза в записке и на защите.

---

### День 4 — Loki + Grafana (БЕЗ Promtail сначала)

**Задачи (практика 4 ч):**

- [ ] Скачать `loki-config.yaml` из `https://raw.githubusercontent.com/grafana/loki/main/cmd/loki/loki-local-config.yaml`.
- [ ] Добавить в `compose.yml` сервис `loki` (image: `grafana/loki:3.0.0`, port 3100, `user: "0"` чтобы избежать permission denied) и `grafana` (image: `grafana/grafana-oss:latest`, port 3000).
- [ ] Авто-провижининг datasource: `grafana/provisioning/datasources/loki.yaml` с `url: http://loki:3100`.
- [ ] Запустить `docker compose up`. Проверить: `curl http://localhost:3100/ready` → `ready`. Зайти в Grafana (admin/admin), убедиться, что Loki datasource подключён.
- [ ] Залить тестовый лог в Loki напрямую через `curl` POST на `/loki/api/v1/push` — чтобы научиться формату payload (он понадобится позже для отладки).

**Изучение (1 ч):**

- https://grafana.com/docs/loki/latest/get-started/quick-start/quick-start/ — quickstart (30 мин).
- https://grafana.com/docs/loki/latest/setup/install/docker/ — раздел Docker Compose (15 мин).
- https://grafana.com/docs/loki/latest/reference/loki-http-api/ — только `/push` и `/query_range` (15 мин).

**Артефакт:** Loki + Grafana работают, datasource подключён, через `curl` залит и виден в Explore тестовый лог.

**Связь с целью:** разнесение инсталляции Loki от Promtail = меньше точек отказа. Если завтра Promtail сломается, Loki точно работает.

⚠️ **Если Loki не стартует**: типовая причина — права на `/loki/wal` и `/loki/chunks`. Решение: `volumes: - loki_data:/loki` (named volume) + `user: "0"`. Не теряйте >1 часа на это — переключайтесь на root user.

---

### День 5 — Promtail (БУФЕРНЫЙ ДЕНЬ ВКЛЮЧЁН: ГОТОВЬТЕСЬ К БОЛИ)

**Задачи (практика 4.5 ч):**

- [ ] Добавить `promtail` в `compose.yml`. Конфиг — `promtail/config.yml`:
    - `clients: - url: http://loki:3100/loki/api/v1/push`
    - `scrape_configs` с `docker_sd_configs` (фильтр по label `logging=promtail`).
    - `pipeline_stages`: `docker:` → `json: expressions: {level, service, event, request_id}` → `labels: {service, level}`.
- [ ] К каждому сервису в `compose.app.yml` добавить `labels: logging: "promtail"`.
- [ ] Смонтировать в Promtail: `/var/run/docker.sock:/var/run/docker.sock:ro`, `/var/lib/docker/containers:/var/lib/docker/containers:ro`. Поставить `user: "0"`.
- [ ] Проверить в Grafana Explore: `{service="order-service"}` должен показывать логи. Если labels `level`, `service` не появились — проверить `pipeline_stages`.

**Изучение (0.5 ч, по необходимости больше):**

- https://grafana.com/docs/loki/latest/send-data/promtail/troubleshooting/ — прочитать целиком (20 мин).
- https://oneuptime.com/blog/post/2026-01-21-promtail-troubleshooting/view — пробежать список ошибок (10 мин).
- Готовый рабочий пример: https://github.com/ruanbekker/docker-promtail-loki — скопировать структуру в случае проблем.

**Артефакт:** в Grafana Explore видны логи из всех 3 сервисов с правильными labels.

**Если не успели:** план Б на день 6 — переключиться на **Docker Loki logging driver** (`docker plugin install grafana/loki-docker-driver`, в compose: `logging: driver: loki`). Это убирает Promtail из стека, но даёт те же данные в Loki. Минус — в записке нужно поменять терминологию. Это запасной парашют, не основной.

⚠️ **Типовые засады, которые сожрут часы:**

- Permission denied на positions.yaml → `user: "0"` или mount volume с правильными правами.
- Логи не приходят, ошибок нет → `--log.level=debug` в Promtail; ищите `Ignoring file`.
- Labels не парсятся → в `pipeline_stages` сначала `docker:` (обёртка), потом `json:` на `log` field.

---

### День 6 — Запас под Loki/Promtail + первый дашборд Grafana

**Если день 5 закрыт (50% вероятность):**

**Задачи (3 ч):**

- [ ] Создать дашборд «Microservices Overview» в Grafana вручную (понимать, что куда). 4 панели:
    1. Time series: `sum(rate({service=~".+"}[1m])) by (service)` — частота логов.
    2. Time series: `sum(rate({service=~".+"} |= "ERROR" [1m])) by (service)` — ошибки.
    3. Logs panel: `{service="$service"} | json | level=~"$level"`.
    4. Stat: `count_over_time({service=~".+"}[5m])` — общий объём.
- [ ] Добавить переменные dashboard: `$service`, `$level`.
- [ ] Экспортировать JSON дашборда в `grafana/provisioning/dashboards/overview.json` для воспроизводимости.

**Изучение (2 ч):**

- https://grafana.com/blog/2023/05/18/6-easy-ways-to-improve-your-log-dashboards-with-grafana-and-grafana-loki/ (45 мин).
- LogQL шпаргалка: https://grafana.com/docs/loki/latest/query/ — разделы Log Queries и Metric Queries (45 мин).
- Видео: «How to Self-Host Loki & Promtail with Docker Compose» (YouTube, 30 мин).

**Если день 5 не закрыт:** добивайте Promtail. Дашборд можно сжать до 2 панелей и сделать в день 14.

**Артефакт:** работающий дашборд, JSON-провижининг.

**Связь с целью:** дашборд = демонстрационный артефакт №1 на предзащите. Раньше начнёте — больше времени на полировку.

---

### День 7 — Drain3 standalone (без Loki, изолированно)

**Задачи (практика 3 ч):**

- [ ] Создать `ml/drain_playground/` с venv. `pip install drain3`.
- [ ] Скрипт `drain_demo.py`: подать 200 строк из ваших логов (выгрузить вручную через `docker compose logs order-service > sample.log`), напечатать получившиеся шаблоны.
- [ ] Поэкспериментировать с `drain3.ini`: `sim_th=0.4` vs `0.6`, `depth=3` vs `5`. Записать в Obsidian, как меняется число шаблонов.
- [ ] Настроить masking: regex для IP, UUID, чисел в `drain3.ini` (готовые примеры в репозитории Drain3 в `/examples`).
- [ ] Сохранить state в file persistence — попробовать перезапустить и убедиться, что шаблоны загружаются.

**Изучение (2 ч):**

- https://github.com/logpai/Drain3 — README целиком (45 мин).
- https://medium.com/@srikrishnan.tech/drain3-the-unsung-hero-of-templatizing-logs-for-machine-learning-8b83ba1ef480 (30 мин) — как работают параметры.
- https://medium.com/@lets.see.1016/how-drain3-works-parsing-unstructured-logs-into-structured-format-3458ce05b69a (30 мин) — про parse tree.
- https://deepwiki.com/logpai/Drain3/3-drain-algorithm — формальный обзор алгоритма (15 мин).

**Артефакт:** 100+ строк → ~10–20 шаблонов, документ в Obsidian с описанием параметров.

**Связь с целью:** Drain3 — это треть вашей защиты. Сегодня вы должны мочь нарисовать parse tree на доске.

---

### День 8 — Drain3 + Loki интеграция (pull-pipeline)

**Задачи (практика 4 ч):**

- [ ] `ml/log_fetcher.py`: функция `fetch_logs(start, end, service=None)` — ходит в Loki HTTP API `/loki/api/v1/query_range` с LogQL `{service=~".+"}`, возвращает list of dicts.
- [ ] `ml/template_miner.py`: класс-обёртка над `TemplateMiner` с persistence в файл `ml/state/drain.bin`. Метод `mine_window(logs)` возвращает `dict[template_id -> count]`.
- [ ] `ml/pipeline.py`: связка `fetch_logs(now-15min, now)` → `mine_window` → печать топ-10 шаблонов. Запустить, убедиться, что результат разумный.
- [ ] Добавить в Obsidian схему: «Loki ← HTTP ← log_fetcher → Drain3 → frequency vector».

**Изучение (1 ч):**

- https://grafana.com/docs/loki/latest/reference/loki-http-api/#query-logs-within-a-range-of-time — разобраться с параметрами `query`, `start`, `end`, `limit` (30 мин).
- LogQL: `rate`, `count_over_time` — потребуются для Prophet (30 мин).

**Артефакт:** скрипт, который из Loki вытягивает реальные логи и выводит шаблоны.

**Связь с целью:** это сердце ML-пайплайна. К концу дня вы можете объяснить «как лог из FastAPI оказывается в виде вектора частот шаблонов».

---

### День 9 — Isolation Forest: теория + standalone

**Задачи (практика 3 ч):**

- [ ] `ml/anomaly_demo.ipynb` (или просто .py): сгенерировать синтетические данные `make_blobs` + 5 outliers, обучить `IsolationForest(contamination=0.1, n_estimators=100, random_state=42)`, визуализировать `decision_function` через `DecisionBoundaryDisplay`.
- [ ] Записать в Obsidian (это ответы на защите):
    - Что значит «isolate»: меньше разбиений = аномалия = короткий путь в дереве.
    - Что такое `contamination`: ожидаемая доля аномалий, влияет на порог.
    - Что выдаёт `predict`: −1 = аномалия, +1 = норма.
    - Что выдаёт `decision_function`: чем меньше (отрицательнее), тем аномальнее.
    - Что выдаёт `score_samples`: отрицательная норма; в реальной интерпретации score близок к 0 — норма, к −1 — аномалия.

**Изучение (2 ч):**

- https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.IsolationForest.html (20 мин).
- https://www.datacamp.com/tutorial/isolation-forest — лучшая интуиция, читается за 30 мин.
- Видео «Outlier & Anomaly Detection using Isolation Forest» (YouTube, ~15 мин).
- Опционально, если останется время: https://mbrenndoerfer.com/writing/isolation-forest-anomaly-detection-... — там есть формула path length и harmonic numbers, но НЕ обязательно для защиты бакалавриата.
- Оригинальная статья Liu, Ting, Zhou (2008) — прочитать abstract + введение, остальное не нужно.

**Артефакт:** ноутбук с визуализацией, страничка в Obsidian с готовыми формулировками.

**Связь с целью:** ML-знания. Минимально достаточный объём, без rabbit hole.

---

### День 10 — Isolation Forest на реальных шаблонах

**Задачи (практика 4 ч):**

- [ ] `ml/anomaly_detector.py`: класс `AnomalyDetector`. Метод `train(historical_windows: list[dict[template_id, count]])` — превращает в матрицу N×K (K = число шаблонов), `IsolationForest.fit`. Метод `score(window)` → anomaly_score, is_anomaly.
- [ ] Сгенерировать 100 «нормальных» окон через load-generator (запустить нагрузку на час с обычным трафиком), плюс 5 окон во время «инцидента» (сами руками вызовите ошибки в payment-service на 5 минут).
- [ ] Обучить детектор на нормальных, проверить на аномальных. Должны видеть, что score аномальных окон ниже.
- [ ] Записать в Obsidian: «как фиксированный размер вектора при появлении нового шаблона». Решение для защиты: при обнаружении нового template_id — новая колонка, переобучение с расширенной матрицей раз в час.

**Изучение (1 ч):**

- https://github.com/logpai/loglizer — README на 15 мин (понять, что это стандартный pipeline; вы делаете упрощённую версию).
- https://github.com/WraySmith/log-anomaly — README (15 мин), увидеть, что у других идея та же: parse → window → vector → ML.
- https://medium.com/@iamvikramkumar5/day-6-ai-assisted-devops-aiops-project-for-log-anomaly-detection-using-ai-ml... (15 мин) — почти ваш кейс.

**Артефакт:** работающий детектор, на синтетическом инциденте срабатывает.

**Связь с целью:** к концу дня у вас есть функционирующий ML-компонент.

---

### День 11 — FastAPI ML-сервис: /anomalies, /health

**Задачи (практика 4 ч):**

- [ ] `ml/main.py`: FastAPI приложение с эндпоинтами:
    - `GET /health` → `{"status": "ok", "model_trained_at": ...}`.
    - `GET /anomalies?window_minutes=15` → `{"score": -0.05, "is_anomaly": true, "top_templates": [...], "timestamp": ...}`.
    - `POST /retrain` → запускает переобучение на последних N часах.
- [ ] Background task (APScheduler или просто `asyncio.create_task`): каждые 5 минут вытягивать окно, считать score, складывать в простой in-memory list (или SQLite).
- [ ] Опционально: пушить score обратно в Loki как лог-строку — тогда его можно визуализировать в Grafana как time series.
- [ ] Свой `Dockerfile` для ML-сервиса. Добавить в общий `compose.yml`.

**Изучение (1 ч):**

- https://machinelearningmastery.com/step-by-step-guide-to-deploying-machine-learning-models-with-fastapi-and-docker/ (30 мин).
- https://blog.jetbrains.com/pycharm/2024/09/how-to-use-fastapi-for-machine-learning/ (30 мин).
- Опционально видео ArjanCodes «How to Use FastAPI» (если что-то не понятно по структуре).

**Артефакт:** `curl http://localhost:8004/anomalies` возвращает живые данные.

**Связь с целью:** API-слой = демонстрационный артефакт №2. На защите вы дёргаете его в браузере или через Postman.

---

### День 12 — Prophet и /forecast

**Задачи (практика 3.5 ч):**

- [ ] `ml/forecaster.py`: функция `fetch_error_series(hours=24)` — LogQL `sum(count_over_time({level="error"}[1m]))` через Loki API, возвращает `pd.DataFrame[ds, y]`.
- [ ] `Forecaster.train(series)` → `Prophet().fit(df)`. `Forecaster.predict(horizon_minutes=60)` → DataFrame со столбцами `ds, yhat, yhat_lower, yhat_upper`.
- [ ] Добавить эндпоинт `GET /forecast?horizon_minutes=60` в FastAPI ML-сервис.
- [ ] Проверить: `pip install prophet` ставится долго и иногда требует `cmdstan`. Если у вас Linux + Python 3.11 — обычно ставится без проблем. На Windows под WSL — то же.
- [ ] Записать в Obsidian формулировки для защиты:
    - Prophet = additive model: `y(t) = g(t) + s(t) + h(t) + ε`, где g — тренд, s — сезонность (Fourier series), h — праздники.
    - Что выдаёт прогноз: yhat (точечный), yhat_lower/upper (95% CI).
    - Почему берём Prophet: робустность к пропускам, не требует ручной настройки `(p,d,q)`, автоматически подхватывает дневную/недельную сезонность.

**Изучение (1.5 ч):**

- https://facebook.github.io/prophet/docs/quick_start.html (30 мин).
- https://www.datacamp.com/tutorial/facebook-prophet — раздел «How Prophet works» (30 мин).
- https://medium.com/@tarangds/traditional-prediction-models-prophet-arima... — для пары предложений «Prophet vs ARIMA» в записке (15 мин).
- Опционально, если упадёт `pip install prophet`: документация по installation https://github.com/facebook/prophet#installation. Запасной план — `statsmodels.tsa.arima.model.ARIMA(order=(2,1,2))`, гайд: https://machinelearningmastery.com/arima-for-time-series-forecasting-with-python/.

**Артефакт:** `/forecast` возвращает прогноз количества ошибок на час вперёд.

**Связь с целью:** последний ML-кусок. После сегодня все компоненты есть — остаётся склеить.

⚠️ **Если Prophet не ставится за 1 час** — переключитесь на `statsmodels` ARIMA. Не теряйте день.

---

### День 13 — End-to-end: всё в одной composition

**Задачи (практика 4 ч):**

- [ ] Объединить `compose.app.yml` и observability в один `compose.yml`. Сервисы: `order`, `auth`, `payment`, `load-generator`, `loki`, `promtail`, `grafana`, `ml-service`.
- [ ] Один `docker compose up` должен поднять ВСЁ. Проверить чек-лист:
    - [ ] Все health endpoints OK.
    - [ ] Логи в Grafana Explore.
    - [ ] `curl localhost:8004/anomalies` отвечает.
    - [ ] `curl localhost:8004/forecast` отвечает.
    - [ ] Дашборд в Grafana показывает данные.
- [ ] `docker compose down -v` + `up` — должно подняться с нуля без ручных действий.
- [ ] Записать инструкцию в `README.md`: как запустить, что куда смотреть, какие порты.
- [ ] Создать `Makefile` с командами `make up`, `make down`, `make logs`, `make demo` — это пригодится на защите.

**Изучение (1 ч):**

- Пробежать https://docs.docker.com/compose/networking/ (понимать, как сервисы видят друг друга по именам).
- Пробежать `docker compose --help` — флаги `-f`, `--profile`, `up -d`, `logs -f`.

**Артефакт:** один `git clone && docker compose up` поднимает всю систему.

**Связь с целью:** это и есть «работающая демонстрация». Технически дип закрыт сегодня. Дальше — полировка.

---

### День 14 — Дашборды Grafana + сценарий демо

**Задачи (практика 4 ч):**

- [ ] Доделать главный дашборд «System Overview»:
    - Логи (raw) с фильтрами по сервису.
    - Time series: error rate per service.
    - Time series: anomaly score (если пушите в Loki) или Stat-панель (читает прямо из ML API через JSON datasource plugin — но это сложно, лучше пушить в Loki).
    - Time series: forecast vs actual (Prophet predictions vs реальный error count).
    - Stat: «Текущий статус» (норма/аномалия) — большой цветной индикатор.
- [ ] Сохранить JSON дашборда в `grafana/provisioning/`.
- [ ] Подготовить «инцидент-сценарий» для демо: запускаем нагрузчик в режиме «atomic» (только хорошие запросы) → 5 минут даём Prophet натренироваться → запускаем «инцидент» (`curl -X POST localhost:8003/_inject_failure`) → видим, что error rate взлетел, anomaly score упал, прогноз показывает рост → возвращаем в норму.
- [ ] Проверить сценарий 2 раза подряд. Записать тайминги.

**Изучение (1 ч):**

- Пройти https://grafana.com/docs/grafana/latest/dashboards/build-dashboards/ — раздел Variables и Panel options (30 мин).
- Видео «6 easy ways to improve log dashboards» (Grafana Labs blog, ~15 мин чтения).

**Артефакт:** красивый дашборд + проверенный сценарий демонстрации.

**Связь с целью:** на предзащите вы запустите этот сценарий вживую. Если он работает с двух раз — на защите тоже сработает.

---

### День 15 — Презентация к предзащите + первый прогон

**Задачи (практика 3.5 ч):**

- [ ] Слайды (10–12 штук, не больше). Структура:
    1. Титул.
    2. Проблема: микросервисы → много логов → ручной анализ невозможен (1 цифра-факт: «N логов в секунду в типичной системе»).
    3. Цель + задачи (3–4 пункта).
    4. Архитектура потока данных (диаграмма из дня 1).
    5. Стек (логотипы FastAPI, Loki, Promtail, Drain3, sklearn, Prophet, Grafana, Docker).
    6. Drain3: пример лог → шаблон.
    7. Isolation Forest: интуиция в одном слайде.
    8. Prophet: тренд + сезонность.
    9. **СКРИНШОТ дашборда** (или запись экрана 30 сек).
    10. Демонстрация (живая или видео-запасной вариант).
    11. План на оставшийся месяц (доработка X, Y, написание ПЗ).
    12. Спасибо/вопросы.
- [ ] **Записать видео-демонстрацию (1–2 минуты)** OBS Studio / `asciinema` — это страховка, если в день предзащиты не работает интернет/проектор.
- [ ] Прогнать защиту вслух с таймером (целевое время — 5–7 минут).

**Изучение (1.5 ч):**

- Пробежать чек-лист «как делать тех.презентацию» — любой ресурс, например https://www.beautiful.ai/.
- Если не сделано — подготовить ответы на «Q&A bank» (см. ниже, раздел Caveats).

**Артефакт:** PDF слайдов + видео демо + записанная репетиция.

**Связь с целью:** предзащита.

---

# Часть 2. Дни 16–45 (после предзащиты — до защиты)

После предзащиты у вас будут замечания комиссии. **Фиксируйте всё письменно прямо во время предзащиты**. Большинство замечаний — поверхностные косметические, серьёзные (если есть) обычно сводятся к «непонятна архитектура» или «слабо обоснована применимость».

## Неделя 3 (дни 16–22): Фундамент пояснительной записки + правки по замечаниям

**Цель недели:** ~25 страниц записки готово. Все замечания предзащиты закрыты.

### Дни 16–17: правки замечаний + введение

- День 16: разобрать замечания комиссии. Закрыть критичные (1–2 шт.) в коде. Если замечания только косметические — выделить день на стабилизацию: добавить логирование в ML-сервис, обработать edge cases (что если Loki недоступен? что если шаблонов 0?), нагрузочные тесты.
- День 17: начать писать **Введение** (3–4 страницы): актуальность, объект/предмет, цель, задачи, методы, практическая значимость, структура работы. Шаблон формулировок: см. https://se.moevm.info/doku.php/diplomants:start:thesis_structure — лучшая русскоязычная инструкция «что писать в каждом разделе ВКР».

### Дни 18–22: Глава 1. Аналитический обзор (12–15 страниц)

Структура главы: 1.1. Анализ предметной области (микросервисная архитектура, проблема логирования, теорема Брюера на 1 абзац). 1.2. Обзор существующих решений (ELK Stack, Splunk, Datadog, Promtail+Loki — сравнительная таблица: open source / стоимость / сложность / индексирование). 1.3. Обзор подходов к парсингу логов (regex, grok, Spell, Drain — почему Drain). 1.4. Обзор алгоритмов детектирования аномалий в логах (One-Class SVM, Autoencoder, Isolation Forest, DeepLog, LogBERT — почему IF: легковесность, интерпретируемость, не требует разметки). 1.5. Обзор моделей прогнозирования временных рядов (ARIMA, Prophet, LSTM — почему Prophet). 1.6. Постановка задачи и требования к системе (функциональные/нефункциональные).

**Источники для главы 1** (5 ч на сбор + 15 ч на текст за неделю):

- arxiv.org поиском «log anomaly detection survey» — 2–3 свежие статьи 2023–2025 для библиографии.
- Liu et al. 2008 «Isolation Forest».
- He et al. 2017 «Drain».
- Taylor & Letham 2018 «Forecasting at scale» (Prophet).
- 2–3 русскоязычных источника, чтобы комиссия не придралась.

⚠️ Минимум 25 источников для бакалавриата (некоторые вузы требуют 35).

## Неделя 4 (дни 23–29): Глава 2 (архитектура) + полировка кода

### Дни 23–26: Глава 2. Проектирование системы (12–15 страниц)

2.1. Архитектура системы (та самая диаграмма потока данных). 2.2. Описание компонентов (по 0.5–1 странице на каждый: FastAPI-сервисы, Promtail, Loki, Drain3, IsolationForest, Prophet, Grafana, ML-сервис). 2.3. Протоколы и форматы взаимодействия (HTTP, JSON, LogQL, форматы payload). 2.4. Структура данных (схема JSON-лога, схема ответа /anomalies, схема state Drain3). 2.5. Алгоритмы обработки (псевдокод pipeline: fetch → parse → vectorize → score). 2.6. Развёртывание (Docker Compose, схема контейнеров, порты).

**Главное правило**: НЕ вставляйте код. Только схемы, диаграммы, формулы, таблицы.

### Дни 27–29: доработка системы (последняя возможность)

Разумные улучшения, не разрушающие:

- Persistence для anomaly history в SQLite.
- Алертинг через Grafana (если время есть): правило, которое срабатывает когда anomaly_score < threshold.
- Конфиг через env vars (вынести параметры IF, Prophet, размер окна).
- Хотя бы pytest-skeleton с 3–5 тестами для ML-сервиса (это покажет «зрелость» — отдельный балл).
- README на английском + русском для красоты в репозитории.

**Что НЕ добавлять, даже если очень хочется:**

- Веб-фронтенд кроме Grafana.
- Аутентификацию (комиссия не оценит, время потеряете).
- Kubernetes / Helm.
- Замену Drain3 на нейросеть.
- Поддержку 5+ микросервисов.

## Неделя 5 (дни 30–36): Глава 3 (реализация) + Глава 4 (тестирование)

### Дни 30–33: Глава 3. Программная реализация (10–12 страниц)

3.1. Используемые технологии и библиотеки (таблица: компонент → версия → роль). 3.2. Структура проекта (дерево каталогов). 3.3. Реализация микросервисов (без листингов кода: «что делает», «какие endpoints», «как логирует»). 3.4. Реализация модуля сбора логов (Promtail config, pipeline_stages). 3.5. Реализация модуля парсинга (Drain3 config, persistence). 3.6. Реализация модуля детектирования (IsolationForest hyperparams, формирование вектора). 3.7. Реализация модуля прогнозирования (Prophet config, формирование ряда). 3.8. Реализация API и интеграции (endpoints, форматы ответов).

### Дни 34–36: Глава 4. Тестирование и результаты (8–10 страниц)

4.1. Методика тестирования (функциональные тесты, нагрузочные). 4.2. Сценарии тестирования (норма, инцидент с увеличением ошибок, инцидент с замедлением). 4.3. Результаты тестов с **скриншотами дашбордов и графиков** (чем больше скриншотов — тем лучше; цель — ~15–20 рисунков на всю записку). 4.4. Анализ результатов: время отклика, точность детектирования (даже грубая, например «при инжекте инцидента из 3 случаев из 3 anomaly_score падает ниже −0.05»), сравнение фактического и прогнозного error rate. 4.5. Ограничения системы и направления развития.

## Неделя 6 (дни 37–43): Заключение + презентация + защитное слово

### Дни 37–38: Заключение + оформление

- Заключение (2–3 страницы): что сделано (по задачам из введения), что получено, практическая значимость, направления развития.
- Список литературы по ГОСТ Р 7.0.5–2008. Используйте Zotero/Mendeley для автогенерации, потом вручную правьте формат.
- Приложения: листинг compose.yml, скриншоты, схемы.
- Аннотация (русский + английский), реферат.
- Полная вычитка всей записки за один проход. Проверка на антиплагиат (вуз обычно требует 60–80% оригинальности).

### Дни 39–41: Презентация защиты (15 слайдов)

Структура:

1. Титул.
2. Актуальность (1 цифра, 1 проблема, 1 цитата).
3. Цель + задачи.
4. Объект, предмет.
5. Аналитический обзор (1 сравнительная таблица).
6. Архитектура (та самая диаграмма).
7. Drain3: пример вход → шаблон.
8. Isolation Forest: 2D визуализация + контур decision_function.
9. Prophet: график trend + seasonality + forecast.
10. Архитектура развёртывания (docker-compose).
11. **Скриншот дашборда** (главный «вау»-слайд).
12. Сценарий «инцидент» — 3 кадра: норма / инцидент / прогноз.
13. Метрики работы (количество шаблонов, время отклика, и т.п.).
14. Заключение (вывод по каждой задаче).
15. Спасибо / готов к вопросам.

### Дни 42–43: Защитное слово (текст + 3 репетиции)

- Текст ровно на 7 минут (≈ 900–1000 слов). Структура:
    - Здравствуйте, я (имя), тема: ...
    - Актуальность (1 минута).
    - Цель и задачи (30 секунд).
    - Архитектура (1.5 минуты — это ваше главное).
    - Ключевые компоненты (1.5 минуты — Drain3, IF, Prophet).
    - Результаты (1 минута + скриншоты).
    - Заключение (30 секунд).
    - Доклад окончен, готов ответить на вопросы.
- Распечатать на бумаге (страховка) + положить в смартфон.
- 3 полные репетиции с таймером, желательно одну — перед родственником/другом.

## Неделя 7 (дни 44–45): Буфер

- День 44: финальная репетиция, генерация PDF записки, печать переплёта (если требуется), копия на флешке + Google Drive + email самому себе. **Подготовка Q&A bank**: 30 типовых вопросов комиссии с готовыми ответами 30–60 секунд каждый.
- День 45: защита.

---

# Часть 3. Приоритеты обучения (что учить в первую очередь и в каком объёме)

## Минимум по ML

**Только это, и не больше:**

- Что такое supervised vs unsupervised (5 минут — Wikipedia).
- Как работает random forest (для аналогии) — StatQuest video, 17 мин.
- Isolation Forest: интуиция + параметры (DataCamp tutorial, 30 мин). Формулу path length знать на пальцах: «чем меньше разбиений нужно, тем аномальнее».
- Контаминирование, anomaly score интерпретация (10 мин из sklearn docs).
- TF-IDF (опционально, 15 мин на Wikipedia) — на случай вопроса «почему просто частоты, а не TF-IDF?». Ответ: «у нас фиксированный набор шаблонов в окне, IDF не даёт большого выигрыша при малых K, но методика расширяема».

**НЕ учить:**

- Глубокое обучение, RNN, LSTM, Transformer, BERT, attention.
- Backpropagation, gradient descent.
- Метрики precision/recall/F1 в деталях (хватит знать, что они есть).
- Dimensionality reduction (PCA, t-SNE).
- Cross-validation для time series.

## Минимум по Loki/Grafana/Promtail

- Архитектура: Loki индексирует labels, не контент → дёшево, эффективно (5 мин).
- Promtail: pipeline_stages (json, regex, labels, timestamp) — 30 мин документации.
- LogQL: log queries (`{label="x"} |= "search"`), metric queries (`rate`, `count_over_time`, `sum by`) — 1 час.
- Grafana: data sources, dashboards, panels, variables — 1 час.
- HTTP API Loki: только `/push`, `/query_range`, `/ready` — 20 мин.

## Минимум по Drain3

- Идея: parse tree фиксированной глубины (depth=4), tokenize → first layer = log length → similarity matching → group → template.
- Параметры: `sim_th` (порог сходства, 0.4), `depth` (глубина, 4), `max_children`.
- Masking: regex для IP, чисел, UUID — улучшает шаблоны.
- Persistence: file/Kafka/Redis — у вас file.
- Время изучения: 3 часа максимум.

## Минимум по временным рядам

- Декомпозиция time series = trend + seasonality + residual (15 мин).
- Stationarity (5 мин — на уровне «в Prophet не нужна, в ARIMA нужна»).
- Prophet: additive model, формула `y(t) = g + s + h + ε`, что выдаёт `yhat, yhat_lower, yhat_upper`.
- Параметры Prophet: `seasonality_mode`, `changepoint_prior_scale` — но в ВКР используете defaults и об этом честно пишете.
- ARIMA(p,d,q): только базовое определение, для пары предложений в обзоре.
- Время изучения: 2 часа максимум.

## ❌ Что НЕ изучать (rabbit holes, в которые легко провалиться)

- **DeepLog, LogBERT, LogAnomaly, LogRobust, NeuralLog** — упоминание в обзоре главы 1, ОДИН абзац: «В современных исследованиях применяются нейросетевые подходы (DeepLog, LogBERT), однако их использование требует размеченных данных и значительных вычислительных ресурсов, что не соответствует требованиям лёгкости развёртывания, поставленным в данной работе».
- **Transformer, attention** — даже не упоминать.
- **Реализация Isolation Forest с нуля** — это студенческое самоистязание, sklearn закрывает вопрос.
- **Реализация Prophet/ARIMA с нуля** — то же самое.
- **Kafka, Spark Streaming** — только если комиссия задаст вопрос «как масштабировать», ответ: «введением Kafka между Promtail и Loki как буфера». На этом стоп.
- **Kubernetes** — никогда.
- **Custom log parser** — Drain3 закрывает.
- **OpenTelemetry, Jaeger, traces** — это другая сторона observability, не ваша.

---

# Часть 4. Оценки времени, риски, упрощения

## Где наиболее вероятны задержки

|Задача|Риск, ч|Что делать|
|---|---|---|
|Loki+Promtail настройка|+4–8 ч|План Б: Docker logging driver (день 5–6).|
|`pip install prophet` (cmdstan)|+2–4 ч|План Б: ARIMA через statsmodels.|
|Drain3 даёт мусор шаблонов|+2 ч|Поправить regex-masking для UUID/чисел/timestamps.|
|Isolation Forest всегда говорит «норма»|+2–3 ч|Проверить feature scaling, увеличить contamination до 0.2, проверить, что инцидент действительно отличается.|
|Docker Compose сервисы не видят друг друга|+1–2 ч|Использовать service names, не localhost; проверить network.|
|Permission denied в томах|+2 ч|`user: "0"` и monted volumes с `:ro`.|
|Загрузка пустого Loki через API при перезапусках|+1 ч|Использовать named volumes, не bind mounts с временным каталогом.|
|Презентация делается в последний день|+5 ч|Сделать в день 15 (предзащита) первую версию.|

## Что делать, если отстаёте от графика

Правило: **режьте scope, не качество**. Приоритеты сохранения (от «не трогать» к «можно отрезать»):

1. **Не трогать:** end-to-end pipeline (Loki + Drain3 + IF + FastAPI endpoint). Это сердцевина.
2. **Резерв 1:** убрать Prophet/forecast, оставить только anomaly detection. Защитное слово: «прогнозирование вынесено в направления дальнейшего развития». Минус 2 балла, но ВКР живёт.
3. **Резерв 2:** упростить до 2 микросервисов вместо 3.
4. **Резерв 3:** дашборд в Grafana — 2 панели вместо 5.
5. **Резерв 4:** отказаться от persistence Drain3 (in-memory) — сразу минус 1 ч работы.
6. **Резерв 5:** отказаться от автопровижининга Grafana — настроить руками раз и сделать скриншот.

## Что упростить если совсем не успеваете к предзащите

- 1 FastAPI-сервис вместо 3, дёргаемый load-generator’ом из bash-цикла.
- Promtail заменить на Docker Loki driver — экономия 1 дня.
- Prophet заменить на «скользящее среднее ошибок за последний час» — это не AI, но честный baseline forecast (минус 3 балла, но за 1 день).
- Демо записать видео заранее, на предзащите показать видео — снимает риск live-демо.

---

# Часть 5. Конкретные ресурсы (все бесплатные, английские в приоритете)

## Isolation Forest (1–2 часа максимум)

|Ресурс|Время|Комментарий|
|---|---|---|
|https://www.datacamp.com/tutorial/isolation-forest|30 мин|Лучшая интуиция с картинками.|
|https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.IsolationForest.html|15 мин|Параметры, методы.|
|https://scikit-learn.org/stable/auto_examples/ensemble/plot_isolation_forest.html|20 мин|Готовый пример с визуализацией.|
|https://www.youtube.com/watch?v=kN--TRv1UDY|15 мин|Видео-объяснение.|
|https://medium.com/mlthinkbox/anomaly-detection-with-isolation-forest-in-scikit-learn-99417dcc3971|20 мин|Практика.|

## Drain3 (3 часа)

|Ресурс|Время|Комментарий|
|---|---|---|
|https://github.com/logpai/Drain3|45 мин|README + examples (обязательно).|
|https://medium.com/@srikrishnan.tech/drain3-the-unsung-hero-of-templatizing-logs-for-machine-learning-8b83ba1ef480|30 мин|Параметры тонкой настройки.|
|https://medium.com/@lets.see.1016/how-drain3-works-parsing-unstructured-logs-into-structured-format-3458ce05b69a|30 мин|Parse tree visualization.|
|https://deepwiki.com/logpai/Drain3/3-drain-algorithm|30 мин|Формальный обзор алгоритма.|
|Оригинальная статья He et al. 2017 «Drain» (ICWS)|45 мин|Только intro + algorithm section.|

## Prophet / ARIMA (2 часа)

|Ресурс|Время|Комментарий|
|---|---|---|
|https://facebook.github.io/prophet/docs/quick_start.html|30 мин|Официальный quickstart.|
|https://www.datacamp.com/tutorial/facebook-prophet|40 мин|Декомпозиция + параметры.|
|https://machinelearningmastery.com/arima-for-time-series-forecasting-with-python/|30 мин|Только если идёте в план Б с ARIMA.|
|https://medium.com/@tarangds/traditional-prediction-models-prophet-arima-83bc8b980ec4|15 мин|Сравнение для абзаца в записке.|

## Loki + Promtail + Grafana (3–4 часа)

|Ресурс|Время|Комментарий|
|---|---|---|
|https://grafana.com/docs/loki/latest/get-started/quick-start/quick-start/|30 мин|Официальный quickstart.|
|https://grafana.com/docs/loki/latest/setup/install/docker/|30 мин|Docker Compose инструкция.|
|https://medium.com/@netopschic/implementing-the-log-monitoring-stack-using-promtail-loki-and-grafana-using-docker-compose-bcb07d1a51aa|30 мин|Полный практический туториал.|
|https://github.com/ruanbekker/docker-promtail-loki|30 мин|Готовый рабочий пример (форкните).|
|https://grafana.com/docs/loki/latest/send-data/promtail/troubleshooting/|20 мин|Прочитать ДО, не ПОСЛЕ.|
|https://oneuptime.com/blog/post/2026-01-21-promtail-troubleshooting/view|20 мин|Расширенный troubleshooting.|
|https://grafana.com/docs/loki/latest/query/|30 мин|LogQL шпаргалка.|
|https://grafana.com/blog/2023/05/18/6-easy-ways-to-improve-your-log-dashboards-with-grafana-and-grafana-loki/|30 мин|Дашборды на максималках.|
|YouTube: https://www.youtube.com/watch?v=AtxQHiFBn7k|30 мин|«How to Self-Host Loki & Promtail with Docker Compose».|

## Docker Compose (multi-service) (2 часа)

|Ресурс|Время|Комментарий|
|---|---|---|
|https://docs.docker.com/compose/|30 мин|Только Networking + Compose file.|
|https://pytutorial.com/fastapi-microservices-with-docker-compose/|45 мин|Прямо ваш кейс.|
|https://testdriven.io/blog/fastapi-docker-traefik/|30 мин (только разделы compose)|Готовый production-grade пример.|
|https://docs.docker.com/reference/samples/fastapi/|15 мин|Docker official samples.|

## FastAPI для ML API (2 часа)

|Ресурс|Время|Комментарий|
|---|---|---|
|https://fastapi.tiangolo.com/tutorial/|60 мин|Только First Steps + Path Parameters + Body.|
|https://machinelearningmastery.com/step-by-step-guide-to-deploying-machine-learning-models-with-fastapi-and-docker/|30 мин|ML-эндпоинт + Docker.|
|https://blog.jetbrains.com/pycharm/2024/09/how-to-use-fastapi-for-machine-learning/|30 мин|Хорошая структура.|
|YouTube ArjanCodes «How to Use FastAPI» https://www.youtube.com/watch?v=SORiTsvnU28|30 мин|Если предпочитаете видео.|

## Структурированное логирование FastAPI (1 час)

|Ресурс|Время|Комментарий|
|---|---|---|
|https://www.sheshbabu.com/posts/fastapi-structured-logging/|20 мин|Минимальный JSON logger.|
|https://betterstack.com/community/guides/logging/logging-with-fastapi/|25 мин|Полный гайд.|
|https://apitally.io/blog/fastapi-logging-guide|20 мин|Сравнение библиотек.|

---

# Сводная таблица: День → главный результат

|День|Главный результат|
|---|---|
|1|Структура репозитория, диаграмма архитектуры, чек-лист 15 дней.|
|2|2 FastAPI сервиса с JSON-логами + load generator.|
|3|3-й сервис, `compose.app.yml` поднимает все приложения.|
|4|Loki + Grafana работают в compose, datasource подключён.|
|5|Promtail тянет логи в Loki, в Grafana Explore видны структурированные логи.|
|6|Первый дашборд Grafana (или закрытие хвостов дня 5).|
|7|Drain3 standalone: 200 строк → 10–20 шаблонов.|
|8|log_fetcher (Loki API) → Drain3 → frequency vector.|
|9|Isolation Forest на синтетике, понимание параметров.|
|10|Anomaly detector на реальных шаблонах, ловит инжектированный инцидент.|
|11|FastAPI ML-сервис: `/health`, `/anomalies`.|
|12|Prophet интегрирован: `/forecast` возвращает прогноз ошибок.|
|13|Один `docker compose up` поднимает всю систему end-to-end.|
|14|Полированный дашборд + проверенный сценарий демо.|
|15|Слайды + видео-демо + репетиция. **ПРЕДЗАЩИТА.**|
|16–17|Закрытие замечаний предзащиты + Введение записки.|
|18–22|Глава 1 «Аналитический обзор» (12–15 стр).|
|23–26|Глава 2 «Проектирование» (12–15 стр).|
|27–29|Стабилизация системы, тесты, мелкие улучшения.|
|30–33|Глава 3 «Реализация» (10–12 стр).|
|34–36|Глава 4 «Тестирование и результаты» (8–10 стр).|
|37–38|Заключение, аннотация, литература, оформление.|
|39–41|Презентация защиты (15 слайдов).|
|42–43|Защитное слово + 3 репетиции.|
|44|Финальная репетиция, печать, бэкапы, Q&A bank.|
|45|**ЗАЩИТА.**|

---

## Caveats

1. **Тайминги — оценки, не гарантии.** Если у вас слабый интернет или старый ноутбук, Docker pulls и `pip install prophet` могут занять часы вместо минут. Закладывайте +20% на инфраструктурные простои в первой неделе.
    
2. **Проблема нового шаблона** в Isolation Forest. Если в окне появляется template_id, которого не было при обучении — у вас изменилась размерность вектора. Решения для защиты:
    
    - Простое: фиксировать топ-K шаблонов на этапе обучения, всё остальное → bucket "other".
    - Более грамотное: переобучать модель раз в час с расширенным словарём шаблонов.
    - На вопрос комиссии «как реагирует на drift» отвечайте через это.
3. **Promtail EOL с 2 марта 2026.** Это известный факт. В записке честно укажите как ограничение/направление развития: «Альтернативой является Grafana Alloy, миграция тривиальна через `alloy convert`». Не делайте сейчас миграцию, это +1 день впустую.
    
4. **Антиплагиат** — большинство вузов требует 60–80% оригинальности. Глава 1 (обзор) — наибольший риск. Перефразируйте всё своими словами, цитируйте через «согласно [N], …», не копируйте определения с Wikipedia.
    
5. **Скриншоты — это половина впечатления комиссии.** Делайте их на этапе тестирования (дни 34–36), сразу качественно: 1920×1080, светлая тема Grafana (не тёмная — на печати чернеет), без личных данных в правом углу.
    
6. **Часть ресурсов в плане — статьи на Medium/Towards Data Science.** Они часто за paywall. Если открывается preview — этого хватает; если нет — ищите аналог через Google Scholar или официальную документацию sklearn/Prophet/Drain3, она первична.
    
7. **«Не лезть в DeepLog/LogBERT» — это про реализацию.** В обзоре главы 1 их **необходимо упомянуть**, чтобы показать эрудицию. Один абзац, одна ссылка, и почему вы их не выбрали. Вот это правильный академический ход.
    
8. **Если на защите спросят «почему не нейросеть?»** — заготовленный ответ: «Использование нейросетевых подходов (DeepLog, LogBERT) требует размеченного датасета аномалий, значительных вычислительных ресурсов, и существенно усложняет интерпретацию результатов. Isolation Forest — стандартный baseline в задачах unsupervised anomaly detection, обеспечивает интерпретируемость через anomaly score и не требует разметки. Архитектура системы спроектирована модульно: ML-компонент изолирован за API, что позволяет в будущем заменить Isolation Forest на нейросетевой подход без изменения остальных компонентов».
    
9. **Q&A bank к защите готовьте заранее (день 44).** Минимум 30 вопросов. Главные:
    
    - Зачем Drain3, нельзя ли regex’ами? (regex руками не масштабируется на новые шаблоны).
    - Что такое contamination и как выбрали? (defaults + здравый смысл; в реальной системе настраивается на исторических данных).
    - Почему Loki, а не Elasticsearch? (Loki индексирует только labels → дешевле, проще, FOSS).
    - Что если Loki упадёт? (Promtail буферизует на диск; ML-сервис в graceful degradation отдаёт last known anomaly score).
    - Как масштабировать? (Loki в microservices mode, Kafka между Promtail и Loki, шардирование Drain3 state по сервисам).
    - Какова метрика качества? (F1 на инжектированных инцидентах; в production — ручная разметка алертов).
    - Почему Prophet, а не ARIMA? (Prophet робустнее к пропускам/выбросам, не требует подбора (p,d,q), автоматическая сезонность).
    - Что выдаёт anomaly_score численно? (decision_function: <0 = аномалия, ≈0 = граница; ≈ −1 = сильная аномалия).
    - Сколько шаблонов получается на ваших логах? (приведите конкретное число из ваших экспериментов).
    - Как обучали без меток? (unsupervised: модель учится на «обычном» поведении, всё что отличается — аномалия).
10. **Главный совет.** На защите 80% впечатления — от того, как вы рассказываете архитектуру (диаграмма потока данных, день 1) и как уверенно показываете живое демо. Технических деталей вас обычно не пытают. Полировка пайплайна и репетиции дают больше баллов, чем глубина теории.