#vkr #ml #anomaly-detection #forecasting #analysis

# Анализ алгоритмов для ML-части ВКР — финальные рекомендации

> Дата анализа: 2026-05-17. Цель: определить оптимальный стек алгоритмов для детекции аномалий + predictive maintenance на структурированных JSON-логах симулированного завода. Ограничение — 30 дней до защиты, упор на прогностический анализ.

---

## TL;DR — финальное решение

|Задача|Было|**Стало**|Почему|
|---|---|---|---|
|Парсинг логов|Drain|**❌ убрать совсем**|Логи уже structured JSON, Drain не нужен|
|Извлечение данных|—|**Loki API + pandas**|Прямой парсинг JSON в DataFrame|
|Anomaly detection|Isolation Forest|**PyOD: ECOD + Isolation Forest**|ECOD проще, объяснимее, без тюнинга; IForest как второй взгляд|
|Forecasting|Prophet|**Prophet (оставить) + опционально Darts**|Prophet даёт доверительные интервалы = predictive maintenance|

**Итоговый стек: Loki API → pandas → PyOD (ECOD primary, IsolationForest secondary) + Prophet (forecasting). Drain выкидываем.**

---

## 1. Drain — убираем полностью

### Что такое Drain и зачем он был

Drain (и его актуальная реализация Drain3) — это **online log parser**, который превращает **неструктурированный текст** логов в шаблоны. Пример из документации Drain3:

```
вход:  "connect to 10.0.0.1"
       "connect to 10.0.0.2"
выход: template "connect to <IP>"
```

Он строит дерево фиксированной глубины, токенизирует строки, группирует похожие в кластеры-шаблоны. Это **не ML** — это алгоритм кластеризации строк по структуре.

### Почему он нам НЕ нужен

Ключевой факт: **наши логи уже структурированы**. Через `python-json-logger` каждая запись — это готовый JSON:

```json
{
  "level": "INFO",
  "service": "equipment",
  "event": "sensor_reading",
  "entity_id": "M-HOB-01",
  "event_time": "2026-01-01T08:00:15",
  "details": {"machine_type": "hobbing", "readings": {"vibration_rms_mm_s": 1.4, ...}}
}
```

Drain нужен когда у тебя логи вида `"172.17.0.1 - - [06/Aug/2024] GET /index.html 200"` — сырой текст, который надо распарсить. Из подтверждения в документации Drain3: _«template mining accuracy can be improved if you feed it with only the unstructured free-text portion of log messages, by first removing structured parts like timestamp, hostname»_ — то есть Drain создан для обратной задачи (текст → структура), которую мы УЖЕ решили на этапе логирования.

Современный консенсус (OpenObserve, Coralogix, и др.): _«When you find yourself dealing with an existing unstructured log format, the most effective solution is to parse those logs into JSON format»_. Мы это сделали сразу — логируем в JSON с самого начала.

### Что вместо Drain

Прямое извлечение из Loki в pandas:

1. HTTP-запрос к Loki API (`/loki/api/v1/query_range`) с LogQL-фильтром
2. Ответ — JSON с массивом логов
3. `json.loads()` каждой строки → `pandas.DataFrame`
4. Pivot по `machine_id` + `parameter` → готовая таблица фичей

**Экономия: ~1-2 дня работы и одна зависимость меньше.** Для защиты: «логи структурированы на этапе генерации (structured logging), поэтому этап парсинга шаблонов (Drain) избыточен — данные сразу машиночитаемы».

> **Важно для защиты:** Drain можно **упомянуть в обзоре** как стандартный подход для unstructured-логов, и обосновать его отсутствие: «мы применили structured logging by design, что исключает необходимость template mining». Это покажет, что ты знаешь область, но сделал осознанный архитектурный выбор.

---

## 2. Anomaly detection — PyOD вместо голого Isolation Forest

### Проблема с «просто Isolation Forest»

Isolation Forest (2008) хорош, но:

- Требует тюнинга `contamination` (доля аномалий — а мы её не знаем заранее)
- Недетерминирован (random splits) — разные прогоны дают разные результаты
- Менее объясним: «дерево изолировало точку за N разбиений» — тяжело интерпретировать на защите

### Решение: PyOD как зонтичная библиотека

**PyOD** (Python Outlier Detection) — стандарт де-факто. Из документации: _«PyOD includes more than 50 detection algorithms... with more than 26 million downloads»_. Единый API в стиле sklearn:

```python
from pyod.models.ecod import ECOD
clf = ECOD()
clf.fit(X_train)
scores = clf.decision_function(X_test)   # anomaly score
labels = clf.predict(X_test)             # 0 = норма, 1 = аномалия
```

Один и тот же интерфейс для **всех** алгоритмов — можно менять модель одной строкой.

### Какие конкретно модели из PyOD

#### Primary: ECOD (Empirical-CDF Outlier Detection, 2022)

ECOD — современный (TKDE 2022), но **простой и объяснимый**. Идея: аномалии — это редкие события в хвостах распределения. Для каждого признака строит эмпирическую функцию распределения (ECDF), считает «насколько значение в хвосте».

Плюсы для нас:

- **Без гиперпараметров** — не нужно тюнить (в отличие от IForest contamination)
- **Детерминирован** — одинаковый результат каждый раз
- **Объясним** — можно показать по каждому признаку его вклад в аномальность (хвостовая вероятность). Для защиты — золото: «деталь аномальна, потому что vibration в 99.7-м перцентиле»
- **Быстрый** — линейная сложность
- Свежий paper для ссылки: Li et al., «ECOD: Unsupervised Outlier Detection Using Empirical Cumulative Distribution Functions», IEEE TKDE 2022

#### Secondary: Isolation Forest (оставить как второй взгляд)

Оставляем IForest как **дополнительную** модель для сравнения. На защите: «мы сравнили хвостовой детектор (ECOD) с ансамблевым (Isolation Forest)». Это показывает методологическую грамотность.

#### Альтернатива/упоминание: HBOS (Histogram-Based Outlier Score)

Из сравнительного исследования (Springer ICATH 2024): _«both HBOS and Isolation Forest attained a commendable accuracy rate of 95%»_. HBOS ещё проще ECOD (просто гистограммы), очень быстрый. Можно упомянуть в обзоре, использовать опционально.

### Итоговая стратегия anomaly detection

```
Поток sensor_reading из Loki
  → pandas DataFrame (фичи: все сенсоры по машине в момент времени)
  → ECOD (primary): хвостовая аномальность по каждому признаку
  → IsolationForest (secondary): мультивариативная изоляция
  → агрегация: точка аномальна если оба согласны (или score выше порога)
```

Для защиты: ECOD ловит **одномерные хвостовые** аномалии с объяснением, IForest — **многомерные** комбинации. Вместе покрывают спектр.

---

## 3. Forecasting / predictive maintenance — Prophet остаётся

### Почему Prophet — правильный выбор для predictive maintenance

Тема ВКР — прогностический анализ. Prophet даёт именно то, что нужно:

- Прогноз `yhat` + **доверительный интервал** `yhat_lower / yhat_upper`
- Детекция аномалий = реальное значение вышло за интервал прогноза
- Это и есть **predictive maintenance**: «прогнозируем тренд сенсора, ловим отклонение ДО того как оно станет alarm»

Из практики (Medium, Ersinesen 2025): _«The Prophet library is a versatile tool for time-series anomaly detection, particularly suited for data with underlying trends»_. Наши сценарии (tool_wear_acceleration, furnace_drift) — именно постепенные тренды, под которые Prophet заточен.

### Важная настройка для наших данных

Наш завод работает 24/7 без выходных и смен. Поэтому в Prophet **нужно отключить недельную и годовую сезонность**:

```python
from prophet import Prophet
m = Prophet(
    weekly_seasonality=False,    # нет рабочей недели
    yearly_seasonality=False,    # нет годовых циклов
    daily_seasonality=False,     # 24/7, нет дневного ритма
    interval_width=0.99,         # 99% доверительный интервал
)
```

Иначе Prophet «изобретёт» несуществующие паттерны на синтетических данных.

### Ограничение Prophet, которое надо знать для защиты

Из обзора (ACM Computing Surveys, Deep Learning for TSAD 2024): _«forecasting-based models often struggle with rapidly and continuously changing time series... these models tend to generate increased prediction errors as the number of time points grows, limiting their utility primarily to very short-term predictions»_.

Вывод: Prophet хорош для **постепенных трендов** (tool_wear, furnace_drift), но плохо ловит **резкие** аномалии (coolant_failure за 1 минуту). Поэтому резкие — отдаём anomaly detection (ECOD), плавные — Prophet. Это естественное разделение труда между двумя подходами.

### Опционально: Darts (если будет время)

**Darts** (Unit8) — современная библиотека, объединяющая ARIMA, Prophet, нейронки (N-BEATS, NHITS, Transformer) под единым sklearn-style API. Из их репозитория: _«it is trivial to apply PyOD models on time series... or to wrap any of Darts forecasting models to obtain fully fledged anomaly detection»_.

Преимущество для защиты: можно **сравнить** Prophet с другими моделями в одном фреймворке (Exponential Smoothing, ARIMA, N-BEATS) и показать почему выбрал Prophet. Но это **бонус, не обязательно** — добавляет 1-2 дня.

Из обзора forecasting-моделей для predictive maintenance (Frontiers in Manufacturing Technology 2024): сравнивали Linear/Polynomial Regression, Exponential Smoothing, ARIMA, Prophet — все рабочие, выбор зависит от характера данных. Это хорошая ссылка для обоснования в записке.

---

## 4. Итоговый стек и pipeline

### Архитектура ML-сервиса

```
┌─────────────────────────────────────────────────────┐
│ ML Service (FastAPI, отдельный контейнер :8006)     │
│                                                       │
│  ┌─────────────┐   фоновый поток раз в N минут       │
│  │ Loki Client │ ──── LogQL query_range ────┐        │
│  └─────────────┘                            ↓        │
│  ┌──────────────────────────────────────────────┐   │
│  │ pandas: JSON → DataFrame → ресемпл по 1 мин   │   │
│  └──────────────────────────────────────────────┘   │
│         ↓                            ↓                │
│  ┌──────────────┐          ┌──────────────────┐      │
│  │ ECOD + IForest│          │ Prophet (per ряд)│      │
│  │ (PyOD)        │          │ forecast+interval│      │
│  │ anomaly score │          │ trend deviation  │      │
│  └──────────────┘          └──────────────────┘      │
│         ↓                            ↓                │
│  ┌──────────────────────────────────────────────┐   │
│  │ Запись результатов в Postgres (таблица        │   │
│  │ ml_anomalies + ml_forecasts)                  │   │
│  └──────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
                          ↓
              Grafana (PostgreSQL datasource)
              дашборд «Аномалии и прогнозы»
```

### Зависимости (ml/requirements.txt)

```
fastapi
uvicorn
requests          # Loki API
pandas
numpy
pyod              # ECOD, IsolationForest, HBOS
scikit-learn      # PyOD зависит, плюс метрики
prophet           # forecasting
psycopg[binary]   # запись результатов в Postgres
# darts           # опционально, если останется время
```

**Drain3 НЕ в списке.** Убрали.

### Поток работы

1. **Сбор:** фоновый поток ML-сервиса раз в минуту дёргает Loki API, тянет свежие sensor_reading
2. **Подготовка:** pandas парсит JSON, делает pivot (строки = время, колонки = machine+sensor), ресемпл на 1-минутный шаг, forward-fill пропусков
3. **Anomaly detection:** ECOD + IForest на свежем окне → anomaly_score на каждую точку
4. **Forecasting:** Prophet (обученный заранее на истории) прогнозирует следующие N точек с интервалом → если реальное вышло за интервал, флаг
5. **Запись:** результаты в Postgres
6. **Визуализация:** Grafana читает Postgres, рисует аномалии и прогнозы

---

## 5. Сравнительная таблица для записки

|Критерий|Drain|ECOD|IForest|HBOS|Prophet|Darts|
|---|---|---|---|---|---|---|
|Назначение|парсинг текста|anomaly|anomaly|anomaly|forecast|forecast|
|Нужен нам?|❌ нет|✅ primary|✅ secondary|⚪ опц.|✅ да|⚪ опц.|
|Сложность реализации|—|очень низкая|низкая|очень низкая|средняя|средняя|
|Гиперпараметры|—|нет|contamination|n_bins|seasonality|много|
|Детерминирован|да|да|нет|да|да|зависит|
|Объяснимость|высокая|**высокая**|средняя|высокая|**высокая**|низкая|
|Свежесть (paper)|2017|2022|2008|2012|2017|2022+|
|Доверит. интервал|—|—|—|—|✅|✅|

---

## 6. Что говорить на защите

**Обоснование выбора (научная грамотность):**

1. **«Structured logging исключает этап парсинга шаблонов.»** Drain/Drain3 — стандарт для unstructured-логов, но мы применили JSON-логирование by design, поэтому template mining не требуется. (Показывает знание области + осознанный выбор.)
    
2. **«ECOD как основной детектор: непараметрический, объяснимый, без тюнинга.»** Ссылка на TKDE 2022. В отличие от Isolation Forest не требует задания contamination и детерминирован.
    
3. **«Prophet для прогностической части: доверительные интервалы дают predictive maintenance.»** Прогнозируем сенсорный тренд, отклонение от интервала = ранний сигнал. Ссылка на Frontiers Manufacturing Tech 2024.
    
4. **«Разделение труда: ECOD/IForest ловят резкие и многомерные аномалии в моменте, Prophet — постепенные тренды с упреждением.»** Это покрывает оба типа сценариев (резкий coolant_failure vs плавный tool_wear). Ссылка на ACM Computing Surveys 2024 про ограничения forecasting-моделей.
    
5. **«PyOD как единая библиотека: 50+ алгоритмов, единый API, 26M загрузок — индустриальный стандарт.»** Можно показать сравнение нескольких детекторов на одних данных.
    

**Метрики для оценки (нужно посчитать):**

- Anomaly detection: precision, recall, F1 на размеченных сценариях
- Forecasting: MAE, MAPE, coverage доверительного интервала
- Early detection: за сколько времени ДО alarm модель заметила тренд

---

## 7. План реализации (после предзащиты)

- [ ] **День 10:** Loki client + pandas pipeline (извлечение, ресемпл). Проверить что данные тянутся.
- [ ] **День 11:** PyOD — ECOD + IForest. Обучить на нормальном датасете, прогнать на сценариях, посчитать precision/recall.
- [ ] **День 12:** Prophet — обучить per-ряд (главные сенсоры), отключить сезонность, настроить интервалы. Детекция выхода за интервал.
- [ ] **День 13:** ML-сервис FastAPI: фоновый поток, Loki polling, запись в Postgres (ml_anomalies, ml_forecasts).
- [ ] **День 14:** Grafana дашборд «Аномалии и прогнозы»: наложение прогноза на реальные данные, маркеры аномалий.
- [ ] **День 15:** калибровка, метрики, опционально Darts для сравнения.

---

## 8. Решения одной строкой

- **Drain — выкинуть.** Логи уже JSON.
- **Isolation Forest — оставить, но добавить ECOD как основной** (через PyOD).
- **Prophet — оставить**, отключить сезонность, использовать интервалы для predictive maintenance.
- **Darts — опционально**, если останется время на сравнение моделей.
- **Pipeline:** Loki API → pandas → PyOD + Prophet → Postgres → Grafana.

> Главное: меньше зависимостей (минус Drain), современнее и объяснимее детектор (ECOD), сохранён прогностический фокус (Prophet). Стек проще в реализации, но не хуже — а по объяснимости даже лучше.