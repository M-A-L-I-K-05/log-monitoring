# ML-сервис — контекст для ИИ (что реализовано)

> **Назначение этой заметки.** Это НЕ шпаргалка к защите и НЕ учебник. Это
> технический хэндофф для ИИ-ассистента: пользователь даёт эту заметку + код
> репозитория и задаёт вопросы по ML-сервису. Прочитав заметку и указанные
> файлы, отвечай точно и не выдумывай. Где сказано «verify в коде» — открой файл
> и проверь, заметка могла устареть.
>
> Репозиторий: `/home/vboxuser/projects/vkr-log-monitoring`. ML-сервис: каталог `ml/`.
> Тема ВКР: «Разработка интеллектуальной системы мониторинга и предиктивного
> анализа логов микросервисной архитектуры». Инженерная задача — **сам сервис**
> (интерактивный, с интеграцией в Grafana), а не только модель.

---

## 0. TL;DR

Отдельный микросервис `ml` (FastAPI, контейнер на :8006). Тянет structured-JSON
логи из Loki → pandas → детекция аномалий (**PyOD: ECOD + IsolationForest**) +
прогноз трендов (**Prophet**) → пишет результаты в **PostgreSQL** (3 таблицы) →
визуализация в **Grafana**. Работает и фоновым потоком (авто), и по REST-вызовам.
Существующий код (симулятор + 4 сервиса + `init.sql`) НЕ менялся при создании ML.

---

## 1. Карта файлов (ответственность модулей)

| Файл | Что делает |
|---|---|
| `ml/config.py` | Все настройки (ENV переопределяет): URL Loki/БД, окно выборки, метки потоков, MAIN_SENSORS по типу станка, параметры детекторов/Prophet, фон. |
| `ml/loki_client.py` | `query_range` к Loki с пагинацией назад по времени; `fetch_events(service,event,lookback)` → список распарсенных JSON-dict. `ping()`. |
| `ml/features.py` | `records_to_long` (JSON→длинная таблица), `machine_frames` (pivot по станку: index=время, cols=сенсоры, ресемпл 1 мин + ffill), `main_series` (ряды для Prophet). |
| `ml/detectors.py` | `MachineDetector` — ECOD(primary)+IForest(secondary) на один станок: `fit(wide)`, `score(wide)`→DataFrame(score_ecod, score_iforest, is_anomaly, top_sensor). |
| `ml/forecaster.py` | `forecast_series(series)` — Prophet (сезонность off, interval 0.99) → DataFrame(ts, yhat, yhat_lower/upper, actual, breach). |
| `ml/store.py` | psycopg pool; `ensure_tables` (CREATE IF NOT EXISTS); `new_run/finalize_run/insert_anomalies/insert_forecasts/truncate_all`. |
| `ml/pipeline.py` | Оркестратор `Pipeline`: `train`, `run_once`, `evaluate`, `status`. Держит обученные детекторы в `self.detectors` (память). |
| `ml/main.py` | FastAPI + endpoints + фоновый поток `_Background` + lifespan (store.init + старт фона). |
| `ml/Dockerfile` | python:3.11-slim, pip install, **фикс prophet makefile** (см. §7), запуск uvicorn :8006. |
| `ml/requirements.txt` | fastapi, uvicorn, requests, pandas, numpy, scipy, scikit-learn, pyod, prophet, psycopg[binary], psycopg-pool, python-json-logger. |
| `docker-compose.yml` | Добавлен блок `ml` (:8006, env LOKI_URL+DATABASE_URL, volume ./ml:/app, --reload, label logging=promtail, depends_on postgres+loki). |
| `grafana/provisioning/dashboards/ML_Anomalies.json` | Дашборд «ML — Аномалии и прогнозы» (Postgres-датасорс). |

---

## 2. Поток данных и КРИТИЧНЫЕ детали времени

```
Симулятор → HTTP POST → сервисы (equipment/quality/...) → JSON в stdout
  → Promtail → Loki → [ML тянет query_range] → pandas → ECOD/IForest + Prophet
  → Postgres (ml_anomalies/ml_forecasts/ml_runs) → Grafana
```

- **Метки потоков Loki** (из promtail-config): `service_name` (= имя compose-сервиса),
  `event`, `level`. Селектор: `{service_name="equipment", event="sensor_reading"}`.
- **Тело строки** — JSON: `level, service, event, entity_id, event_time, details{...}`.
  Для sensor_reading: `details.readings = {sensor: value}`, `details.machine_type`.
- **ГЛАВНОЕ про время.** Loki индексирует по РЕАЛЬНОМУ времени приёма. Симулятор
  гонит виртуальное время ускоренно. Поэтому: запрашиваем НЕДАВНЕЕ реальное окно
  (`REAL_LOOKBACK_MIN`, дефолт 30), а ось ряда строим по `event_time` из JSON
  (виртуальное). Ресемпл 1 мин — по виртуальному времени. Не путать.
- **Кто что логирует** (важно для фильтров): sensor_reading → `equipment`;
  `scenario_event` → **`quality`** (симулятор POST'ит на quality `/scenario-event`,
  сам в Loki не пишет доменные события). measurement/inspection_result → quality.

---

## 3. Алгоритмы (что и зачем)

- **ECOD** (PyOD, primary): хвостовая аномальность по ECDF каждого признака.
  Без гиперпараметров, детерминирован, объясним (вклад сенсора). 
- **IsolationForest** (PyOD, secondary): многомерная изоляция, random_state фиксирован.
- Комбинация: `ANOMALY_COMBINE` = `any` (по умолчанию: хотя бы один) | `both`.
- Модель — **на каждый станок** (фичи = все его сенсоры). Инспекция пропускается
  (`SKIP_MACHINE_TYPES`).
- **Prophet** (forecaster): прогноз `yhat` + интервал 99%; `breach` = факт вышел
  за интервал = ранний предиктивный сигнал. Сезонность off (24/7-завод, синтетика).
  Считается только по `MAIN_SENSORS[machine_type]`; тяжёлый → в фоне раз в
  `FORECAST_EVERY_RUNS`.
- `top_sensor` в аномалии — сенсор с макс. |z| относительно обучающего окна
  (объяснимость; приблизительно, не внутренности ECOD).

---

## 4. Endpoints (контракты)

База: `http://localhost:8006` (в сети compose — `http://ml:8006`). Swagger: `/docs`.

| Метод/путь | Тело | Действие |
|---|---|---|
| `GET /health` | — | `{status, service, loki_ready}` |
| `GET /status` | — | обученные станки, run_count, last_summary, фон |
| `POST /train` | `{real_lookback_min?}` | обучить детекторы по станкам на окне |
| `POST /run-once` | `{real_lookback_min?, forecast?}` | полный прогон: detect (+forecast) → запись |
| `POST /detect` | `{real_lookback_min?}` | только детекция |
| `POST /forecast` | `{real_lookback_min?}` | прогон с forecast=true |
| `POST /evaluate` | `{real_lookback_min?}` | метрики по разметке scenario_event |
| `POST /loop` | `{enabled, interval_sec?}` | вкл/выкл фоновый поток |
| `POST /reset` | — | TRUNCATE ml-таблиц + сброс памяти моделей |

---

## 5. Хранение

- **Результаты → PostgreSQL** (таблицы создаёт сам сервис, `store.DDL`):
  - `ml_runs` (метаданные прогона: kind, окно, n_points/anomalies/forecasts, params JSONB)
  - `ml_anomalies` (по точке: event_time, score_ecod, score_iforest, is_anomaly, top_sensor, top_sensor_z)
  - `ml_forecasts` (ts, yhat, yhat_lower/upper, actual, breach)
- **Веса моделей → СЕЙЧАС в памяти** (`Pipeline.detectors`), на диск/в БД НЕ
  сохраняются. При рестарте контейнера теряются и переобучаются автоматически.
  Это противоречит «правильному» паттерну (веса в файле/volume) — см. §9.

---

## 6. Фоновый поток (`main._Background`)

- Включён по умолчанию (`ML_BACKGROUND_ENABLED=true`), интервал `BACKGROUND_INTERVAL_SEC` (120 c).
- Логика: при пустых детекторах или каждые `RETRAIN_EVERY_RUNS` (10) прогонов →
  `train()`; затем `run_once()` каждый тик. Forecast внутри run_once — раз в
  `FORECAST_EVERY_RUNS` (5). Управляется `/loop`.

---

## 7. НЕ-ОЧЕВИДНОЕ / гочи (чтобы ИИ не галлюцинировал)

1. **scenario_event под `service_name="quality"`**, не simulator. (Конфиг
   `SCENARIO_SERVICE="quality"`. Изначально была ошибка — указан simulator,
   `/evaluate` находил 0 окон.)
2. **Виртуальное vs реальное время** (см. §2) — ось рядов по `event_time` из JSON.
3. **Prophet + cmdstanpy баг.** prophet 1.1.6 кладёт обрезанный bundled cmdstan
   без `makefile`; cmdstanpy≥1.3 в `validate_cmdstan_path` его отклоняет →
   `'Prophet' object has no attribute 'stan_backend'`. Фикс в Dockerfile:
   `touch cmdstan-*/makefile` (компиляция не нужна — модель уже собрана). Без
   фикса forecasts=0.
4. **Веса не персистятся** (память) — §5/§9.
5. **`/restart` симулятора чистит Loki и сбрасывает виртуальное время** на
   2026-01-01 08:00 → в Loki может быть несколько «эпох» с пересекающимися
   виртуальными временами. Для чистого ML-прогона — рестарт сим + один цельный прогон.
6. **furnace/grinding появляются в данных поздно** — детали доходят до печи/
   шлифовки спустя виртуальные часы; на коротком прогоне их рядов может не быть.
7. **Grafana**: дашборд ссылается на Postgres-датасорс по uid `PCC52D03280B7034C`
   (общий с рабочими дашбордами, из persistent-volume). Время дашборда зафиксировано
   2026-01-01..2027 под виртуальные таймстемпы (обычный «last 6h» не покажет данные).

---

## 8. Как запустить/проверить

```bash
docker compose up -d ml                     # поднять (образ уже собран)
# симулятор должен генерить данные: на :8005 /restart, /speed {multiplier:100}, /start, создать заказы
# ручной прогон ML (изнутри сети или с хоста на :8006):
curl -XPOST localhost:8006/train     -d '{"real_lookback_min":30}' -H 'Content-Type: application/json'
curl -XPOST localhost:8006/run-once  -d '{"real_lookback_min":30,"forecast":true}' -H 'Content-Type: application/json'
curl -XPOST localhost:8006/evaluate  -d '{"real_lookback_min":30}' -H 'Content-Type: application/json'
# Grafana: http://localhost:3000 → дашборд «ML — Аномалии и прогнозы»
```
Проверка БД: `docker exec postgres psql -U admin -d factory -c "SELECT count(*) FROM ml_anomalies;"`

Последний проверенный прогон: train 11 станков; run-once 4363 точки / 153 аномалии /
12889 прогнозов / 0 ошибок Prophet; evaluate window_recall=1.0, lead-time ≈104 мин.

---

## 9. Статус: реализовано vs к доработке

**Реализовано:** конвейер Loki→pandas→ECOD/IForest+Prophet→Postgres; фоновый поток
+ ручные endpoints; интеграция с Grafana; метрики (/evaluate); Dockerfile/compose.

**К доработке (пользователь планирует, ещё НЕ сделано):**
- **Интерактивное обучение** — сейчас обучение есть через `POST /train`, но без
  параметров модели в запросе/UI. Цель: задавать контаминацию/окно/набор сенсоров
  интерактивно (и, возможно, лёгкий UI или богаче endpoints).
- **Хранение весов в volume** — сейчас веса в памяти. Цель: pickle/joblib
  обученных детекторов в `ml/models/` на смонтированный volume + загрузка при
  старте (обучил один раз — переживает рестарт). Потребует расширения
  `store.py`/`pipeline.py` + строки volume в docker-compose.

---

## 10. Известное слабое место (честно)

Точечные precision/recall низкие (~0.11 / 0.07 в тесте) при window_recall=1.0.
Причины (НЕ баг): окно сценария размечено целиком, а у gradual ранняя зона ещё
«нормальна» по сенсорам; `CONTAMINATION=0.02` даёт фон ложных срабатываний на
нормальных точках. Ручки калибровки: `ML_CONTAMINATION`, гранулярность разметки
(метить только фазу scrap), `ANOMALY_COMBINE=both`. Это следующий шаг — калибровка
метрик, а не исправление дефекта.
