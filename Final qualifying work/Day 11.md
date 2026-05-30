# Day 11 — Пересчёт targets сценариев по 3σ (брифинг для Claude Code)

> Это инструкция, **не** готовый код. Описана логика и точные числа. Реализацию делаешь ты. Читай также Day 9 (инварианты) и Day 10 (режимы gradual/step).

---

## 0. Зачем это

В Day 10 были введены два режима: `gradual` (дрейф) и `step` (ступенька). Targets для сенсоров в `SCENARIOS_BY_MACHINE_TYPE` были взяты из прежнего конфига — они задумывались для step и выбирались произвольно. Для gradual это приводило к двум проблемам:

1. **`sensor_scale` по severity искажал targets** — при одном и том же `drift_progress` сенсоры были разными для разных severity, т.е. граница начала брака (`scrap_threshold`) физически оказывалась в разных точках.
2. **Targets не были привязаны к реальным нормам** — непонятно, при каком отклонении сенсора деталь реально уходит в брак.

**Решение:** привязать targets к статистической норме сенсорных данных. Использовать **3σ** как границу «вне нормы» — это стандарт SPC (Statistical Process Control, Уолтер Шухарт). 99.7% нормальных значений укладываются в ±3σ; выход за эту границу — надёжный сигнал аномалии.

---

## 1. Ключевые принципы (итог обсуждения)

### Gradual сценарии

- **`sensor_scale` НЕ применяется** к gradual-targets. Targets берутся напрямую из формулы 3σ.
- **Target** = значение `anomaly_modifier` при `drift_progress = 1.0` (полная деградация).
- При `drift_progress = scrap_threshold (0.85)` значение сенсора должно быть ровно на 3σ границе.
- **Severity влияет только на `pace`** (шаг накопления drift_progress за деталь):
  - `light`:  `pace = 0.85 / 180` (≈180 деталей до начала брака)
  - `clear`:  `pace = 0.85 / 120` (≈120 деталей)
  - `gross`:  `pace = 0.85 / 60`  (≈60 деталей)

Формула расчёта target из 3σ:
```
# Параметр растёт (↑):
target = 1.0 + (3 × std / mean) / scrap_threshold

# Параметр падает (↓):
target = 1.0 - (3 × std / mean) / scrap_threshold
```
Проверка: при drift=0.85 → modifier = 1.0 + (target−1.0)×0.85 = 1 ± 3σ/mean ✓

### Step сценарии

- **`sensor_scale` остаётся** — severity масштабирует итоговый modifier (как было).
- Условие корректности: даже при `light` severity (sensor_scale=0.6) значение должно превышать 3σ границу.
- Формула нового target если старый не проходил:
```
new_target = 1.0 + (3 × std / mean) / 0.6      # для ↑
new_target = 1.0 - (3 × std / mean) / 0.6      # для ↓
```

---

## 2. Новые targets для GRADUAL сценариев

Источник norm: `SENSOR_PROFILES` в `config.py`. `scrap_threshold = 0.85`.

### tool_wear (turning)
| Параметр | mean | std | Направление | Новый target |
|---|---|---|---|---|
| vibration_rms_mm_s | 0.8 | 0.15 | ↑ | **1.66** |
| spindle_load_percent | 45.0 | 5.0 | ↑ | **1.39** |
| spindle_bearing_temp | 45.0 | 3.0 | ↑ | **1.24** |

### hob_wear (hobbing)
| Параметр | mean | std | Направление | Новый target |
|---|---|---|---|---|
| vibration_rms_mm_s | 1.4 | 0.25 | ↑ | **1.63** |
| spindle_load_percent | 62.0 | 6.0 | ↑ | **1.34** |
| hob_bearing_temp | 55.0 | 4.0 | ↑ | **1.26** |

### shaver_wear (shaving)
| Параметр | mean | std | Направление | Новый target |
|---|---|---|---|---|
| vibration_rms_mm_s | 0.8 | 0.20 | ↑ | **1.88** |
| spindle_load_percent | 30.0 | 4.0 | ↑ | **1.47** |

### under_carburizing (furnace, фаза carburizing)
| Параметр | mean | std | Направление | Новый target |
|---|---|---|---|---|
| carbon_potential | 1.10 | 0.05 | ↓ | **0.84** |
| furnace_temp_zone1 | 927.0 | 4.0 | ↓ | **0.985** |
| furnace_temp_zone2 | 927.0 | 4.0 | ↓ | **0.985** |
| furnace_temp_zone3 | 927.0 | 4.0 | ↓ | **0.985** |

### over_carburizing (furnace, фаза carburizing)
| Параметр | mean | std | Направление | Новый target |
|---|---|---|---|---|
| carbon_potential | 1.10 | 0.05 | ↑ | **1.16** |
| furnace_temp_zone1 | 927.0 | 4.0 | ↑ | **1.015** |
| furnace_temp_zone2 | 927.0 | 4.0 | ↑ | **1.015** |
| furnace_temp_zone3 | 927.0 | 4.0 | ↑ | **1.015** |

---

## 3. Пересчёт targets для STEP сценариев

Только те параметры, у которых light severity не обеспечивал выход за 3σ.  
Остальные параметры step-сценариев — **без изменений**.

| Сценарий | Параметр | Старый target | Новый target |
|---|---|---|---|
| chatter | vibration_rms_mm_s | 1.80 | **1.90** |
| chatter | spindle_load_percent | 1.25 | **1.49** |
| workpiece_loose | vibration_rms_mm_s | 1.50 | **1.90** |
| workpiece_loose | work_spindle_rpm | 1.10 | **1.63** |
| grinding_chatter | vibration_rms_mm_s | 1.80 | **2.25** |
| grinding_chatter | wheel_bearing_temp | 1.15 | **1.32** |
| grinding_burn | spindle_power_kw | 1.80 | **2.00** |

---

## 4. Что менять в коде

### `simulator/config.py`

**A. Новые pace-константы по severity:**
```python
DRIFT_PACE_BY_SEVERITY = {
    "light": 0.85 / 180,   # ≈ 0.00472
    "clear": 0.85 / 120,   # ≈ 0.00708 (текущий)
    "gross": 0.85 / 60,    # ≈ 0.01417
}
```
Убрать `DRIFT_PACE` (было единственное число).

**B. В `SCENARIOS_BY_MACHINE_TYPE`:**
- Обновить targets для всех gradual сценариев (Таблица §2).
- Обновить targets для помеченных step сценариев (Таблица §3).

### `simulator/subsystems/scenarios.py` — `start_scenario()`

Для gradual: убрать `sensor_scale` из расчёта targets:
```python
# БЫЛО:
sensors_final[k] = 1.0 + (mult - 1.0) * sensor_scale

# СТАЛО (gradual):
sensors_final[k] = mult   # target берётся напрямую из конфига
```

Для gradual: pace берётся из `DRIFT_PACE_BY_SEVERITY[severity]`:
```python
pace = config.DRIFT_PACE_BY_SEVERITY[severity]
```

Для step: `sensor_scale` остаётся как есть.

---

## 5. Инварианты (из Day 9) — НЕ ломать

Все инварианты Day 9 в силе. Эти изменения затрагивают только:
- числа в `config.py` (targets, pace)
- одну строку в `start_scenario` (убрать sensor_scale для gradual)

Логику измерений, quality, M-GMM — **не трогать**.

---

## 6. Что фактически доделано сегодня (итог реализации)

Помимо пересчёта targets из §1–§4, в этот же день переработана логика печи и проведена итоговая валидация.

### 6.1. Targets и режимы (Day 11)
- Пересчитаны все gradual-targets по 3σ без `sensor_scale` (§2); step-targets подправлены там, где light не выходил за 3σ (§3).
- Печные gradual-targets выставлены по той же 3σ-формуле без scale (точные значения в конфиге): under — `carbon_potential 0.840`, зоны `0.9848`; over — `1.160`, зоны `1.0152`.
- `coolant_temp_c` в `coolant_loss` поднят 1.35 → **1.40** (при light было +2.94σ, стало +3.4σ).

### 6.2. Переработка логики печи (`furnace.py`, `config.py`, `scenarios.py`, `maintenance.py`)
- **Phase-lock**: `anomaly_modifier` действует только в `trigger_phase` сценария; в остальных фазах сенсоры в норме (важно: `carbon_potential` есть и в heating, и в carburizing).
- **Печной gradual** (under/over_carburizing): дрейф растёт по времени фазы carburizing шагами **каждые 5 мин** (240/5 = 48 шагов/фаза), а не одной ступенью. Дрейф сохраняется между загрузками. Severity = число загрузок до брака:
  | severity | прирост/загрузку | брак на загрузке |
  |---|---|---|
  | gross | 0.55 | 2 |
  | clear | 0.38 | 3 |
  | light | 0.27 | 4 |
  На загрузке отбраковки дрейф пересекает scrap (0.85) и доходит до stop (1.0) **внутри** той же фазы carburizing (запас 40–85 мин) — отдельная задержка перед обслуживанием не нужна.
- **Печной step** (quench_distortion): **вероятность исхода убрана** — без внешней поимки партия всегда уходит в брак. В фазе quenching: `elapsed < 8 мин` → подфаза `normal` (ещё можно остановить), `>= 8 мин` → `scrap`; выброс в [8, 28] мин. «Поимка» в первые 8 мин оставлена как задел `FurnaceSubsystem.catch_step_scenario` (вызов из endpoint equipment/maintenance в будущем).
- **Заморозка до ремонта**: выброшенные по сценарию печные партии замораживаются (`frozen_furnace_batches`) и идут на измерение только ПОСЛЕ завершения WO печи (время ремонта покрывает остывание).
- **Вместимость печи** `FURNACE_CAPACITY_PARTS` 200 → **160**.

### 6.3. Чистка
- Удалён мёртвый механизм `verify_next_batch_with_sample` («следующая партия 10%-выборкой»): станочный брак ≤17 деталей < минимальной партии 25, условие ≥80% не срабатывало; для печи он и не был реализован. Поголовная пометка + 10%-выборка годного остатка внутри партии — сохранены.

### 6.4. WebUI
- Step-таблица: добавлен столбец **Phase** (для печи normal/scrap по времени; для станков всегда scrap); поля Processed/Remaining для печи показывают время (`10min`).
- Карточка печи для step показывает прошедшее/оставшееся время до порога.
- Убрано поле «Лимит деталей» из модалки запуска сценария (не используется).
- Добавлена секция **«Очередь перед измерением»** (по аналогии с очередью перед печью).
- Заголовок «Active scenarios» → «Активные сценарии».

### 6.5. Валидация — вердикт
- **3σ** (численно): все сенсоры всех сценариев выходят за mean±3σ в точке брака.
- **Дрейф печи** (симуляция): подтверждены load-counts по severity и stop внутри carburizing.
- **Живой прогон** (speed 1000, без ошибок в логах): печной gradual (phase-lock, перенос дрейфа между загрузками, брак на загрузке 2 для gross, выброс → заморозка → WO → измерение, `parts_fail=140`); печной step (normal→scrap на 8 мин, всегда брак, вся загрузка заморожена → WO); станочный step chatter (брак на измерении).
- **Итог:** данные валидны и обучаемы. Можно переходить к ML-модели.
