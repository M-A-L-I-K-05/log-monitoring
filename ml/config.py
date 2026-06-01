"""Конфигурация ML-сервиса: все настройки в одном месте (ENV переопределяет)."""
import os


def _f(name, default):
    return float(os.environ.get(name, default))


def _i(name, default):
    return int(os.environ.get(name, default))


# ─── Внешние сервисы ───────────────────────────────────────────
LOKI_URL = os.environ.get("LOKI_URL", "http://loki:3100")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://admin:secret@postgres:5432/factory")

# ─── Извлечение логов из Loki ──────────────────────────────────
MAX_LOG_LINES = _i("ML_MAX_LOG_LINES", "300000")
LOKI_PAGE_LIMIT = _i("ML_LOKI_PAGE_LIMIT", "5000")
LOKI_TIMEOUT_SEC = _f("ML_LOKI_TIMEOUT_SEC", "30")
# Loki ограничивает ДЛИНУ диапазона запроса (max_query_length, по умолчанию ~30д).
# «Последние N записей» берём за это окно реального времени — данные пишутся в
# Loki в реальном времени, поэтому свежий сбор всегда попадает в окно.
LOKI_MAX_QUERY_DAYS = _i("ML_LOKI_MAX_QUERY_DAYS", "29")
# Обучение: запрашиваем последние TRAIN_FETCH_LIMIT строк (direction=backward,
# без временного окна — работает при любой скорости симулятора).
TRAIN_FETCH_LIMIT = _i("ML_TRAIN_FETCH_LIMIT", "150000")

# Метки потоков (см. promtail-config.yaml: relabel → service_name, labels → event/level).
SENSOR_SERVICE = "equipment"
SENSOR_EVENT = "sensor_reading"
SCENARIO_SERVICE = "quality"          # scenario_event POST'ится в quality-сервис
SCENARIO_EVENT = "scenario_event"     # разметка для /evaluate
ALARM_EVENT = "alarm"

# ─── Подготовка фичей ──────────────────────────────────────────
# Демпинг шума: усредняем 4 показания по 15с в одну минуту → σ шума падает вдвое (√4).
# Переобучение НЕ требуется, и это намеренно: train_mean инвариантен к усреднению
# (среднее минутных средних = среднее 15с), а train_std остаётся 15-секундным.
# Минутные средние «тихие» (σ/2), но меряются «громкой» 15с-нормой → z шума ≈ вдвое
# меньше, хвосты не доходят до ANOMALY_Z. Дрейф сценария (сдвиг среднего) усреднение
# сохраняет → z_дрейф = Δ/σ₁₅ как был, ловится. ECOD/IForest калиброваны на 15с-
# разбросе, на минутных средних почти молчат — ок, они вспомогательные (по ИЛИ).
RESAMPLE_RULE = os.environ.get("ML_RESAMPLE", "1min")
# Интервал эмиссии сенсоров симулятором (с). Нужен, чтобы пересчитать сырую σ
# (15с, на ней учится детектор) в σ ресемплированного ряда (RESAMPLE_RULE) для
# норма-полосы Prophet: Prophet прогнозирует именно ресемплированный ряд, и шум
# в нём тише в √n раз (n = бин/интервал независимых показаний). См.
# detectors.MachineDetector.train_std_resampled.
SENSOR_INTERVAL_SEC = _f("ML_SENSOR_INTERVAL_SEC", "15")
MIN_TRAIN_POINTS = _i("ML_MIN_TRAIN_POINTS", "30")       # алгоритмический минимум внутри fit
TRAIN_POINTS = _i("ML_TRAIN_POINTS", "1000")             # нужно на комбинацию (machine_type, product_code)

# Главные сенсоры для Prophet-прогноза.
# Для обычных станков ключ = machine_type.
# Для печи ключ = "furnace__<phase>" — у каждой фазы свои аномальные сенсоры.
# Detection (ECOD/IForest) использует ВСЕ сенсоры; forecast — только эти.
MAIN_SENSORS = {
    # ── По типу станка (применяется для всех шестерён если нет конкретного ключа) ──
    "turning":              ["vibration_rms_mm_s", "spindle_load_percent", "spindle_bearing_temp"],
    "hobbing":              ["vibration_rms_mm_s", "spindle_load_percent", "hob_bearing_temp"],
    "shaving":              ["vibration_rms_mm_s", "spindle_load_percent"],
    "grinding":             ["vibration_rms_mm_s", "wheel_bearing_temp", "coolant_flow", "spindle_power_kw"],
    # ── Печь: по фазе ─────────────────────────────────────────────────────────────
    "furnace__carburizing": ["carbon_potential", "furnace_temp_zone1"],
    "furnace__quenching":   ["quench_oil_temp", "quench_oil_flow", "furnace_temp_zone1"],
    # ── Переопределения по (тип станка, тип шестерни) — если нужен другой набор ──
    # Пример: "turning__HEL-L": ["vibration_rms_mm_s", "spindle_bearing_temp"]
    # Если ключ отсутствует — используется общий ключ типа станка выше.
}
# Типы станков, у которых нет «аномальных» сенсоров — пропускаем.
SKIP_MACHINE_TYPES = {"inspection"}
# Для печи обучаем модели только для аномальных фаз.
FURNACE_ML_PHASES = {"carburizing", "quenching"}

# ─── Детекторы аномалий (PyOD) ─────────────────────────────────
CONTAMINATION = _f("ML_CONTAMINATION", "0.02")          # используется только для fit
IFOREST_RANDOM_STATE = _i("ML_IFOREST_SEED", "42")
# ECOD/IForest — вспомогательный МНОГОМЕРНЫЙ сигнал («несколько сенсоров странны
# вместе»), добавляется к главному z-правилу по ИЛИ. "any" — хотя бы один детектор,
# "both" — оба. Главный триггер всё равно z-правило ниже; детекторы только докидывают
# аномалии-комбинации, которых z по одному сенсору не видит.
ANOMALY_COMBINE = os.environ.get("ML_COMBINE", "any")
# После fit порог автоматически поднимается до mean + N*std обучающих scores.
# 3.0 → ~0.1% ложных на чистом гауссовом baseline. 0 → оставить порог от contamination.
THRESHOLD_SIGMA = _f("ML_THRESHOLD_SIGMA", "3.0")

# z-правило (ГЛАВНЫЙ триггер): аномалия, если ЛЮБОЙ сенсор вышел за ANOMALY_Z σ
# обучающей нормы (берём максимум по сенсорам). Это та же σ, что в симуляторе
# (z = (x-mean)/std). 2.8 — чуть ниже 3σ отбраковки, т.е. ловим в зоне раннего
# предупреждения, до брака. Соединяется с ECOD/IForest по ИЛИ. 0 → выключить.
ANOMALY_Z = _f("ML_ANOMALY_Z", "2.8")
# Персистентность (SPC run-rule): аномалию пишем, только если кандидат держится
# PERSIST_N показаний ПОДРЯД. Одиночные шумовые хвосты гаусса так отсекаются,
# а устойчивый дрейф (сдвиг среднего) проходит. 1 → выключить (писать одиночные).
PERSIST_N = _i("ML_PERSIST_N", "3")

# ─── Forecasting (Prophet) ─────────────────────────────────────
PROPHET_INTERVAL_WIDTH = _f("ML_PROPHET_INTERVAL", "0.99")
FORECAST_HORIZON_MIN = _i("ML_FORECAST_HORIZON_MIN", "30")
# Прогноз тяжелее детекции (Prophet fit на ряд) — в фоне делаем реже.
FORECAST_EVERY_RUNS = _i("ML_FORECAST_EVERY_RUNS", "5")
# Bulk-бюджет Prophet: сколько последних sensor_reading тянуть ОДНИМ запросом по
# ВСЕМ станкам (только по меткам {service,event}, без `| json` — Loki отдаёт сырые
# строки быстро, JSON парсим в Python), потом режем по (станок, контекст) на стороне
# клиента и переносим на непрерывную ось. Так вместо 12 тяжёлых per-station `| json |
# entity_id=` запросов (entity_id НЕ метка → скан всего потока на каждый станок → на
# высокой скорости таймауты, и фоновый поток висит, не давая идти детекции) — один
# лёгкий запрос. Делится на ~16 станков, так что для печи остаётся с запасом карбюр-
# минут. Пагинация по 5000 (потолок Loki), так что число может быть большим.
# 80000 (а не 40000): quenching — КОРОТКАЯ фаза, на 40000 в окно влезало лишь ~58 мин
# (на полу PROPHET_MIN_POINTS), Prophet цеплялся за шумовой наклон и хвост прогноза
# задевал нижнюю норму furnace_temp_zone1 → мигающая ложная карточка. На 80000 quenching
# набирает ~135 мин → наклон выпрямляется (−0.15→−0.02/мин), запас до нормы +5σ. Замер:
# Final qualifying work/Day 14.md §«ложная аномалия quenching». Логику Prophet НЕ трогаем.
PROPHET_BULK_FETCH = _i("ML_PROPHET_BULK_FETCH", "80000")
# Сырой бюджет per-station (дремлющая ветка _run_prophet → ml_forecasts; bulk выше её
# не использует). Держим умеренным, чтобы `| json`-запрос не был тяжёлым при ручном
# вызове /run-once с forecast. Потолок страницы Loki — 5000.
PROPHET_FETCH_POINTS = _i("ML_PROPHET_FETCH_POINTS", "1500")
# Окно фита Prophet: последние N МИНУТ контекста (после переноса на непрерывную ось,
# см. features.to_continuous_minutes). Это «окно памяти» — старые нормальные циклы за
# его пределами не размывают текущий дрейф. Реальные простои между партиями выкинуты,
# поэтому N — это N минут РАБОТЫ в этом режиме, а не календарного времени.
PROPHET_SERIES_POINTS = _i("ML_PROPHET_SERIES_POINTS", "200")
# Cold-start guard: минимум минут контекста, чтобы Prophet вообще строил прогноз.
# Ряд теперь набирается из ИСТОРИИ контекста (а не из текущего цикла), поэтому этот
# порог срабатывает лишь в самом начале, когда контекста ещё почти не было. На коротком
# ряду Prophet экстраполирует шум на 30 мин за полосу (ложные карточки) — ≥60 мин это
# убирает (проверено: на ≥45 мин ложных 0). Отдельно от MIN_TRAIN_POINTS(30, мин. fit).
PROPHET_MIN_POINTS = _i("ML_PROPHET_MIN_POINTS", "60")
# Прогнозный контур (отдельный от детекции): включается тумблером в WebUI.
# Цель периодичности — раз в PROPHET_CYCLE_SEC секунд; фон сам считает счётчик
# тиков (PROPHET_CYCLE_SEC / интервал фона), напр. 15/5 = каждые 3 тика.
# Замер: полный цикл (bulk-fetch ~6с + ~43 фита ~5с) ≤ ~11с в худшем случае,
# поэтому 15с дают комфортный запас и при этом обновляют карточки вдвое чаще.
PROPHET_BACKGROUND_ENABLED = os.environ.get(
    "ML_PROPHET_BACKGROUND_ENABLED", "false").lower() == "true"
PROPHET_CYCLE_SEC = _f("ML_PROPHET_CYCLE_SEC", "15")
# Время жизни строки витрины ml_prophet_status: если станок/сенсор не обновлялся
# дольше — строку выбрасываем (станок встал, сменил фазу печи на сенсоры другой
# фазы и т.п.), чтобы карточки Grafana не показывали застрявший статус.
PROPHET_STATUS_TTL_SEC = _f("ML_PROPHET_STATUS_TTL_SEC", "300")

# ─── Хранение весов на диске (volume) ──────────────────────────
# Веса детекторов сохраняются как версии в MODELS_DIR (монтируется как
# named volume → переживают пересборку/перезапуск контейнера). Обучение —
# явное (через UI/endpoint); скоринг идёт по загруженной активной версии.
MODELS_DIR = os.environ.get("ML_MODELS_DIR", "/app/models")

# ─── Фоновый поток ─────────────────────────────────────────────
BACKGROUND_ENABLED = os.environ.get("ML_BACKGROUND_ENABLED", "true").lower() == "true"
BACKGROUND_INTERVAL_SEC = _f("ML_BACKGROUND_INTERVAL_SEC", "5")
# Запас окна скоринга поверх интервала фона (реальные секунды). Окно Loki =
# интервал + запас, чтобы соседние прогоны перекрывались и не теряли точки на
# стыке. Считается ДИНАМИЧЕСКИ от текущего интервала фона (см. main._Background).
SCORING_MARGIN_SEC = _f("ML_SCORING_MARGIN_SEC", "5")
# Дефолтное окно скоринга для ручных вызовов (/detect и т.п.), в реальных минутах:
# интервал + запас. Фоновый поток пересчитывает своё окно сам при смене интервала.
SCORING_LOOKBACK_MIN = _f("ML_SCORING_LOOKBACK_MIN",
                           str(round((BACKGROUND_INTERVAL_SEC + SCORING_MARGIN_SEC) / 60, 4)))

# ─── Оценка качества (/evaluate) ───────────────────────────────
# Допуск по времени при сопоставлении аномалии с окном сценария (виртуальные минуты).
EVAL_TOLERANCE_MIN = _f("ML_EVAL_TOLERANCE_MIN", "5")
