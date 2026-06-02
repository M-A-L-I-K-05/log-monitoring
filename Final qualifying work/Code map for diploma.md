# Карта кода для диплома: где какую логику смотреть

Назначение: при написании ВКР (в т.ч. в браузерной версии с архивом проекта) — для каждой главы/подглавы указано, **в каком файле и какой функции/классе** искать реальную логику. Номера строк даны как ориентир (могут сдвинуться при правках) — опирайся на имена функций/классов.

Привязка к оглавлению из `Final qualifying work/Diploma structure.md`. Теоретические подглавы (обзор, постановка задачи) кода не имеют — для них указан конспект-источник.

**Где что лежит в архиве:**
- Конспекты и материалы — в директории `Final qualifying work/` (там же лежит этот файл): `Day 1.md … Day 14.md`, `Factory logic.md`, `Factory model.md`, `ML algorithms analysis.md`, `ML service — учебный план.md`, `Diploma structure.md`.
- Когда ниже указан конспект, ищи его именно в `Final qualifying work/`.
- Ссылки вида «память «…»» — это локальные заметки, **в архив не входят**; рядом всегда дан конспект или файл кода как замена.

---

## ГЛАВА 1. Анализ предметной области и обзор решений
> Теоретическая глава, своего кода почти нет. Источники — конспекты.

- **1.1 Наблюдаемость микросервисов** — кода нет. Контекст: `docker-compose.yml` (видно 10 сервисов как пример микросервисной системы).
- **1.2 Предметная область (завод зубчатых колёс)** — конспекты `Final qualifying work/Factory logic.md`, `Final qualifying work/Factory model.md`. Маршрут станков — `simulator/config.py` (список `MACHINES`, типы станков и порядок).
- **1.3 Методы детекции аномалий** — конспект `Final qualifying work/ML algorithms analysis.md` (ECOD, Isolation Forest, Prophet — теория). Код-иллюстрации брать из главы 3.
- **1.4 Обзор стеков логирования** — кода нет; фактический выбор виден в `docker-compose.yml` (loki, promtail, grafana) и `promtail/promtail-config.yaml`, `loki/loki-config.yaml`.
- **1.5 Постановка задачи** — кода нет; требования сформулировать по факту реализованного.

---

## ГЛАВА 2. Проектирование системы
> Здесь описывается замысел; код приводить минимально, основная глубина — в гл. 3.

- **2.1 Общая архитектура (10 сервисов)** — `docker-compose.yml` (полный состав, порты, зависимости, volumes). Порты: equipment 8001, production 8002, quality 8003, maintenance 8004, simulator 8005, ml 8006, loki 3100, grafana 3000, postgres 5432.
- **2.2 Проектирование симулятора** — доменная модель: `simulator/domain/` (`batch.py`, `order.py`, `work_order.py`, `machine.py`, `furnace_load.py`). Состав подсистем: `simulator/main.py` (как они собираются), `simulator/state.py` (общее состояние).
- **2.3 Техпроцесс и сценарии аномалий** — фазы печи: `simulator/config.py` (`FURNACE_PHASE_DURATIONS_SEC`). Каталог сценариев: `simulator/subsystems/scenarios.py` → `ScenariosController.catalog()` (строка ~461). Типы gradual/step — `simulator/subsystems/furnace.py` → `_handle_gradual` (~282), `_handle_step` (~324).
- **2.4 Конвейер логов** — `promtail/promtail-config.yaml` (сбор), `loki/loki-config.yaml` (хранение). Формат лога и его отправка: `simulator/client.py`. Лимиты запросов Loki (5000 строк/запрос, диапазон ≤30 дней) видны в `ml/loki_client.py` → `query_range()` (23).
- **2.5 Двухконтурная схема ML** — обзорно: `ml/pipeline.py`, класс `Pipeline` (строка 33). Контур детекции → `run_once` (134). Предиктивный контур → `run_prophet_cycle` (289). Здесь — только схема, детали в 3.4/3.5.
- **2.6 Модель данных PostgreSQL** — `postgres/init/init.sql` (схема всех таблиц завода). ML-таблицы создаются в `ml/store.py` → `ensure_tables()` (104).

---

## ГЛАВА 3. Реализация системы
> Основная техническая глава. Код — в приложения, в тексте только ключевые фрагменты + ссылки.

- **3.1 Стек и развёртывание** — `docker-compose.yml` целиком; Dockerfile сервисов (`ml/Dockerfile`). Конфиги ML — `ml/config.py` (все параметры с комментариями).

- **3.2 Реализация симулятора** — главный цикл: `simulator/loop.py` → `SimulationLoop._run()` (78), `fast_forward` (39), виртуальное время. Тик производства: `simulator/subsystems/production.py`. Управление через API/WebUI: `simulator/main.py` (эндпоинты `/start /stop /speed /scenarios/*`), `simulator/webui/`.
  *→ приложение В.*

- **3.3 Микросервисы домена** — по одному файлу на сервис: `services/equipment/main.py`, `services/production/main.py`, `services/quality/main.py`, `services/maintenance/main.py` (FastAPI-эндпоинты + запись в БД).
  *→ приложение В.*

- **3.4 Контур детекции аномалий** — обучение детекторов: `ml/detectors.py`, класс `MachineDetector` → `fit()` (50), `score()` (104), `meta()` (87). Прогон: `ml/pipeline.py` → `run_once()` (134), правило персистентности → `_apply_persistence()` (228). Ключ детектора по типу+продукту → `_det_key()` (29). Параметры: `ml/config.py` (`ANOMALY_Z`, `PERSIST_N`, `MAIN_SENSORS`).
  *→ приложение Г.*

- **3.5 Предиктивный контур Prophet** — обёртка Prophet: `ml/forecaster.py` → `forecast_series()` (30). Цикл: `ml/pipeline.py` → `run_prophet_cycle()` (289), проверка сенсора → `_prophet_check_sensor()` (376), согласование σ → `_prophet_band_std()` (362), выбор сенсоров → `_prophet_sensors()` (355). Подготовка рядов: `ml/features.py` → `to_continuous_minutes()` (66, минутный ресемпл σ_min=σ/√n), `prophet_frames()` (93, разрез bulk по станок×контекст). Bulk-выгрузка: `ml/loki_client.py` → `fetch_recent()` (116). Параметры: `ml/config.py` (`PROPHET_BULK_FETCH`, `PROPHET_SERIES_POINTS`, `PROPHET_MIN_POINTS`, `PROPHET_CYCLE_SEC`, `FORECAST_HORIZON_MIN`).
  *→ приложение Д. Разбор математики σ и почему Prophet ловит step — конспект `Final qualifying work/Day 14.md`.*

- **3.6 Хранение и витрины** — `ml/store.py`: `insert_anomalies()` (149), `insert_forecasts()` (172), `upsert_prophet_status()` (202), TTL-чистка `prune_prophet_status()` (228), `truncate_all()` (244). Версии моделей: `ml/model_store.py`.
  *→ приложение Е.*

- **3.7 Визуализация Grafana + WebUI** — дашборды: `grafana/provisioning/dashboards/ML_Detection.json`, `ML_Prophet.json` (карточки станков), `Quality_Scenarios.json`. Провижининг: `dashboards.yaml`. WebUI ML: `ml/webui/` (`index.html`, `app.js`, `style.css`).
  *→ приложение Ж. Скриншоты делать с запущенного стенда.*

---

## ГЛАВА 4. Эксперимент и оценка
> Часть скрипта оценки (4.3) ещё предстоит написать — приложение З.

- **4.1 Методика эксперимента** — управление прогоном: `simulator/main.py` (API `/scenarios/start` — задаёт ground truth), фоновый цикл ML: `ml/main.py` → `_Background._loop()` (85). Ускорение времени: `simulator/loop.py` → `fast_forward` (39).
- **4.2 Валидация симулятора** — генерация показаний (что валидируем): `simulator/subsystems/furnace.py` → `_generate_furnace_readings()` (486); измерения качества: `simulator/subsystems/quality.py` → `_gen_value()` (230), `_measure_part()` (110). Результаты валидации (3σ, физичность фаз) описаны в конспектах `Final qualifying work/` — ищи день, где валидировался симулятор (Day 9–11).
- **4.3 Количественная оценка детекции (precision/recall/lead-time)** — **код предстоит написать** (приложение З). Опора: запуск сценариев `simulator/subsystems/scenarios.py` → `start_scenario()` (177) даёт ground truth; срабатывания читать из Postgres (таблицы из `ml/store.py`); готовый расчёт по окнам сценариев уже частично есть — `ml/pipeline.py` → `evaluate()` (425), `_scenario_windows()` (505), `in_window()` (451).
- **4.4 Оценка Prophet** — данные из `ml_prophet_status` (поле `lead_min` = упреждение), пишется в `ml/store.py` → `upsert_prophet_status()` (202); логика упреждения — `ml/pipeline.py` → `_prophet_check_sensor()` (376).
- **4.5 Производительность** — бенчмарк тика симулятора: `simulator/benchmark_tick.py`. Синхронный бюджет цикла Prophet — конспект `Final qualifying work/Day 14.md` §5 (fetch ~6 с + фиты). Период: `ml/config.py` (`PROPHET_CYCLE_SEC=15`).
- **4.6 Обсуждение и ограничения** — кода нет; вывод по метрикам из 4.3–4.5.

---

## Быстрый индекс «файл → что внутри»

| Файл | Роль | Главы |
|---|---|---|
| `docker-compose.yml` | состав и порты 10 сервисов | 2.1, 3.1 |
| `postgres/init/init.sql` | схема БД завода | 2.6 |
| `simulator/config.py` | станки, маршрут, фазы печи | 1.2, 2.3 |
| `simulator/loop.py` | главный цикл, виртуальное время | 3.2, 4.1 |
| `simulator/main.py` | API/WebUI управления | 3.2, 4.1 |
| `simulator/domain/*.py` | доменная модель | 2.2 |
| `simulator/subsystems/furnace.py` | печь, генерация показаний, сценарии | 2.3, 4.2 |
| `simulator/subsystems/quality.py` | измерения и брак | 4.2 |
| `simulator/subsystems/scenarios.py` | каталог и запуск сценариев | 2.3, 4.1, 4.3 |
| `services/*/main.py` | микросервисы домена | 3.3 |
| `ml/config.py` | все ML-параметры | 3.1, 3.4, 3.5, 4.5 |
| `ml/detectors.py` | обучение/скоринг детектора | 3.4 |
| `ml/pipeline.py` | оба контура + evaluate | 2.5, 3.4, 3.5, 4.3, 4.4 |
| `ml/forecaster.py` | обёртка Prophet | 3.5 |
| `ml/features.py` | подготовка рядов, минутный ресемпл | 3.5 |
| `ml/loki_client.py` | запросы к Loki | 2.4, 3.5 |
| `ml/store.py` | запись результатов в Postgres | 3.6 |
| `ml/main.py` | API ML + фоновый цикл | 4.1 |
| `grafana/provisioning/dashboards/*.json` | 3 дашборда | 3.7 |
| `promtail/*.yaml`, `loki/*.yaml` | конвейер логов | 2.4 |
