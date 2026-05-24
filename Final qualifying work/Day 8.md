# Конспект — 2026-05-24

Темы: разграничение endpoint'ов backend-сервисов, завершение партии (`work_center == "inspection"`), Python-идиомы (`and`-цепочка, `break` vs `return`, `sorted` с `key`), приоритеты партий в production и furnace, смешивание product_code в печи, модификаторы по типоразмеру шестерни (cycle_time, sensor, quality).

---

## 1. Разграничение ответственности backend-сервисов

### Где какое событие обрабатывается

| Событие | Сервис | В БД |
|---|---|---|
| `sensor_reading` | equipment | UPDATE `machine_status.sensor_updated_at` |
| `state_change` | equipment | UPDATE `machine_status.current_state` |
| `alarm` | equipment | только лог |
| `cycle_completion` | equipment | только лог |
| `batch_start` | production | INSERT в `active_batches` |
| `batch_move` | production | UPDATE `current_wc`, `wc_entered_at` |
| `batch_completion` | **production** | UPDATE `actual_quantity` + DELETE при `work_center=="inspection"` |
| `measurement` | quality | только лог |
| `inspection_result` | quality | только лог |
| `work_order_*` | maintenance | INSERT/UPDATE в `open_work_orders` |

### Принцип разделения

- **Equipment** знает про **машину**: состояние, сенсоры, циклы.
- **Production** знает про **партию**: маршрут, перемещения, завершение этапов.
- **Quality** знает про **деталь**: измерения, решения pass/fail.
- **Maintenance** знает про **бригады и WO**.

`batch_completion` (завершение партии на участке) — это **производственный факт**, не оборудование. Поэтому идёт в production, а не в equipment.

### `cycle_completion` vs `batch_completion`

| | `cycle_completion` | `batch_completion` |
|---|---|---|
| Что значит | Станок закончил **одну деталь** | Партия завершила **участок** |
| Куда идёт | equipment | production |
| Кладёт в БД | нет, только лог | да (UPDATE) |
| Частота | очень частое (на каждую деталь) | редкое (одно на партию на участке) |

---

## 2. Завершение партии как событие

### Где симулятор объявляет партию `done`

`simulator/subsystems/equipment.py:_route_batch_to_next_stage()`:

```python
def _route_batch_to_next_stage(self, machine, batch, now):
    next_stage = config.NEXT_STAGE.get(batch.stage)
    if next_stage is None or next_stage == "done":
        batch.stage = "done"
        batch.current_machine_id = None
        self.state.counters["batches_done"] += 1
        self.state.batches.pop(batch.batch_id, None)
        logger.info("batch %s done", batch.batch_id)
        return
    ...
```

Это **локальное** событие симулятора: счётчик увеличился, партия удалена из словаря активных, лог в stdout симулятора. **Никакого специального HTTP-запроса наружу не отправляется**.

### Как backend узнаёт что партия закончила маршрут

Через `batch_completion(work_center="inspection")`. В `services/production/main.py`:

```python
@app.post("/batch-completion")
def batch_completion(data: BatchCompletion):
    logger.info("batch_completion", ...)
    persisted = db_update_batch_quantity(data.batch_id, data.actual_quantity, ...)
    if data.work_center == "inspection":
        persisted = db_delete_batch(data.batch_id, ...) and persisted
    return ...
```

**Семантический оверлоадинг одного endpoint'а двумя смыслами**:
1. Любой `batch_completion` → обновить `actual_quantity`.
2. Если `work_center == "inspection"` → дополнительно `DELETE FROM active_batches`.

Inspection — последний этап маршрута, поэтому это эквивалентно «партия done». Для ML это хороший корреляционный сигнал: можно построить пайплайн `batch_start` → … → `batch_completion(work_center="inspection")` через `batch_id`.

### Альтернатива (не делали)

Можно было ввести отдельный endpoint `/batch-done` и шлать его из `_route_batch_to_next_stage`. Текущий подход проще, не плодит схемы.

---

## 3. Идиома `x = f() and x`

### Конкретно из `production/main.py:274`

```python
persisted = db_update_batch_quantity(...)              # шаг 1
if data.work_center == "inspection":
    persisted = db_delete_batch(...) and persisted     # шаг 2
```

### Что такое `and` в Python

Не возвращает `True`/`False`. Возвращает **первый falsy операнд**, а если все truthy — последний:

```python
True  and True   # → True
True  and False  # → False
False and "anything"  # → False  (short-circuit: правое не вычисляется)
```

С булями работает как ожидаемо.

### Смысл строки

Накопление флага «всё ли прошло хорошо». `persisted` остаётся `True` только если **обе** операции (UPDATE и DELETE) успешны.

### Эквивалент развёрнуто

```python
delete_ok = db_delete_batch(...)
persisted = persisted and delete_ok
```

или

```python
delete_ok = db_delete_batch(...)
if not delete_ok:
    persisted = False
```

### Важный нюанс — порядок операндов

```python
persisted = db_delete_batch(...) and persisted   # вызов слева — всегда выполняется
persisted = persisted and db_delete_batch(...)   # вызов справа — может НЕ выполниться
```

Второй вариант из-за short-circuit: если `persisted` уже `False`, `db_delete_batch()` не вызовется. В коде продакшна выбран **первый** вариант — DELETE пытается выполниться независимо от исхода UPDATE.

---

## 4. `break` vs `return`

### Принципиальная разница

| | `break` | `return` |
|---|---|---|
| Прерывает | **только цикл** | **всю функцию** |
| Возвращает значение | нет | да |
| После него выполняется | код после цикла (в той же функции) | управление вызывающему |

### Пример

```python
def find_first_even(nums):
    result = None
    for n in nums:
        if n % 2 == 0:
            result = n
            break          # выходим из for, но НЕ из функции
    print("после цикла")    # ← выполнится
    return result
```

`break` — это инструкция, не выражение. Нельзя присвоить переменной (`x = break` → SyntaxError).

### Вложенные циклы

`break` выходит **только из ближайшего** цикла. В Python нет `break 2` как в PHP/Bash. Чтобы выйти из всех сразу — `return` (если в функции) или флаг.

### Пример из furnace.py

```python
for batch in list(queue):
    if total + batch.quantity > FURNACE_CAPACITY_PARTS:
        break             # выходим из for — печь полная
    selected.append(batch)
    total += batch.quantity

if not selected:           # ← код после break продолжается
    return None
```

После `break` метод **продолжается** — проверяет `selected`, считает условия старта, возвращает `FurnaceLoad`.

---

## 5. Приоритеты партий: rush > urgent > normal

### Что было

В `production._assign_from_queue()` партии из очереди брались чисто FIFO:

```python
batch = queue.popleft()
```

Приоритет на этом этапе не учитывался. Это противоречит реальной заводской логике — rush-партия должна обгонять normal.

### Что добавили

Константа в `config.py`:
```python
PRIORITY_ORDER = {"rush": 0, "urgent": 1, "normal": 2}
```

Перед циклом назначения партий в production и в `furnace._try_start_load()`:

```python
sorted_batches = sorted(queue, key=lambda b: config.PRIORITY_ORDER[b.priority])
queue.clear()
queue.extend(sorted_batches)
```

После этого `popleft()` / `queue[0]` берёт самую приоритетную партию.

### Почему FIFO внутри группы сохраняется автоматически

`sorted` в Python — **стабильная сортировка**. Это гарантия алгоритма TimSort: элементы с одинаковым ключом сохраняют исходный порядок.

```python
items = [('a', 2), ('b', 1), ('c', 2), ('d', 1)]
sorted(items, key=lambda x: x[1])
# → [('b', 1), ('d', 1), ('a', 2), ('c', 2)]
```

`b` стоял раньше `d` в исходном списке → так и остался раньше в результате. То же с `a` и `c`. Без дополнительной сортировки.

### Затраты по производительности

Бенчмарк sorted с lambda key на разных размерах очередей:

| Размер очереди | Время одной сортировки |
|---|---|
| 10 партий | 0.0007 мс |
| 100 партий | 0.005 мс |
| 1000 партий | 0.054 мс |

5 очередей × ~10 партий ≈ 0.0035 мс на тик. Тик длится 100 мс → overhead 0.003%. Полностью незаметно.

---

## 6. `sorted` с `key`-функцией и lambda

### Минимальный пример

```python
nums = [5, 1, 3]
sorted(nums, key=lambda x: -x)
# → [5, 3, 1]
```

### Кто вызывает lambda

`sorted` сам. Идёт по списку, каждый элемент передаёт в `key`-функцию, получает число для сравнения.

```
5 → lambda(5) → -5
1 → lambda(1) → -1
3 → lambda(3) → -3
сортирует элементы по полученным значениям -5, -1, -3 → [5, 3, 1]
```

### Что такое `key`

Параметр функции `sorted`, который **принимает другую функцию**. В Python функции — объекты, их можно передавать как аргументы.

`lambda b: x` — синтаксический сахар для анонимной функции в одну строку. Эквивалент:

```python
def get_key(b):
    return x
```

---

## 7. Печь: смешивание product_code разрешено

### Что было

Одна загрузка = один product_code (recipe-based scheduling). Из очереди отбирались только партии того же продукта что и `head_batch`:

```python
head_batch = queue[0]
recipe = head_batch.product_code
for batch in list(queue):
    if batch.product_code != recipe:
        continue
    ...
```

### Почему отказались

Логически: мы приняли упрощение, что цикл печи **одинаковый для всех типоразмеров** (одна глубина цементации, одинаковая температурная программа). Тогда смешивать разные product_code в одной загрузке — корректно.

Если параметры одинаковые → разделение по рецептам теряет смысл.

### Что стало

Убран фильтр по recipe:

```python
for batch in list(queue):
    if total + batch.quantity > config.FURNACE_CAPACITY_PARTS:
        break
    selected.append(batch)
    total += batch.quantity
```

`FurnaceLoad.product_code: str` → `product_codes: list[str]` (уникальные коды в загрузке). В логи `state_change` идёт `"product_codes": "HEL-L,SPUR-M"` через запятую.

### Что выиграли

1. Печь стартует быстрее — не ждёт «накопления одного рецепта».
2. Меньше pile-up в `waiting_furnace`.
3. Код проще, минус условие.

### Что не моделируем

В реальности даже при одинаковых параметрах печи обычно загрузка = один рецепт. Причины — учёт, прослеживаемость партий, отдельная сертификация. Это **управленческое** ограничение, не технологическое. У нас в модели нет учёта/сертификации, ограничение снимается.

---

## 8. Модификаторы по типоразмеру шестерни

### Идея

Четыре типоразмера (SPUR-S, SPUR-M, HEL-M, HEL-L) должны различаться в логах. Не дублировать профили станков для каждого типа, а ввести **множители** к baseline.

### Где различаются параметры

| Уровень | Зависимость от product_code |
|---|---|
| **Маршрут** | Один для всех (упрощение) |
| **Cycle time** | Да — большая шестерня обрабатывается дольше |
| **Sensor readings** | Да — выше нагрузка/вибрация/температура |
| **Quality measurements** | Да — другие допуски по AGMA |
| **Печь** | Нет (одинаковый цикл) |

### Cycle time мультипликаторы

```python
CYCLE_TIME_MULT_BY_PRODUCT = {
    "SPUR-S": {"turning": 0.7, "hobbing": 0.5, "shaving": 0.9, "grinding": 0.7,  "inspection": 0.9},
    "SPUR-M": {"turning": 1.0, "hobbing": 1.0, "shaving": 1.0, "grinding": 1.0,  "inspection": 1.0},
    "HEL-M":  {"turning": 1.0, "hobbing": 1.1, "shaving": 1.0, "grinding": 1.1,  "inspection": 1.05},
    "HEL-L":  {"turning": 1.3, "hobbing": 1.5, "shaving": 1.1, "grinding": 1.4,  "inspection": 1.15},
}
```

Эффективный cycle time hobbing:
- SPUR-S: 5 мин (быстрее, мелкая, модуль 1)
- SPUR-M: 10 мин (baseline)
- HEL-M: 11 мин
- HEL-L: 15 мин (модуль 3, ниже скорость резания, больше объём металла)

Соответствует документу дня 4 (5 мин ↔ 15 мин для крайних типов).

### Sensor modifiers — отдельный множитель на каждый параметр

```python
SENSOR_MODIFIERS_BY_PRODUCT = {
    "SPUR-S": {
        "spindle_load_percent": 0.70,
        "vibration_rms_mm_s":   0.80,
        "spindle_bearing_temp": 0.95,
        ...
    },
    ...
}
```

**Почему не один общий множитель**: вибрация и температура реагируют на нагрузку **по-разному**. Вибрация растёт почти линейно с силой резания, температура — медленно и нелинейно. Один общий множитель этого не отразит.

### Quality measurement specs — масштабирование по физике

```python
MEASUREMENT_SPECS_BY_PRODUCT = {
    "SPUR-S": {"profile_deviation": (0.0, 11.0, "um"), ...},
    "SPUR-M": {"profile_deviation": (0.0, 14.0, "um"), ...},  # baseline
    "HEL-M":  {"profile_deviation": (0.0, 14.0, "um"), "lead_deviation": (0.0, 14.0, "um"), ...},
    "HEL-L":  {"profile_deviation": (0.0, 18.0, "um"), "runout": (0.0, 30.0, "um"), ...},
}
```

**Физические зависимости** (по ISO 1328 и AGMA):
- `profile_deviation` ∝ модуль^0.4 — растёт с высотой зуба.
- `runout` ∝ √D — растёт с делительным диаметром.
- `pitch_deviation` ∝ модуль^0.3.
- `lead_deviation` ≈ ширина зуба ≈ модуль; для косозубых строже (контроль угла наклона критичен).
- `surface_roughness` после шлифования почти одинакова, на крупных чуть хуже.

Класс точности AGMA Q10–Q11 (ISO 6–7) для всех — стандарт автомобильных шестерен.

### Применение в коде

**Equipment** (`_generate_sensor_readings` получает product_code партии):
```python
product_mods = config.SENSOR_MODIFIERS_BY_PRODUCT.get(product_code, {})
for name, (mean, std, _unit) in profile.items():
    value = random.gauss(mean, std)
    product_mult = product_mods.get(name)
    if product_mult is not None:
        value *= product_mult
    # отдельно applied anomaly_modifier для сценариев аномалий
```

**Equipment** (cycle time):
```python
cycle_mult = config.CYCLE_TIME_MULT_BY_PRODUCT[batch.product_code][machine.machine_type]
cycle_sec = config.CYCLE_TIME_SEC[machine.machine_type] * cycle_mult
```

**Quality**:
```python
specs = config.MEASUREMENT_SPECS_BY_PRODUCT[batch.product_code]
lo, hi, unit = specs[param]
```

---

## 9. Источники по нормам точности шестерен (для диплома)

Открытых таблиц допусков AGMA Q10–Q11 и ISO 1328-1 в полном объёме в свободном доступе нет (стандарты платные). Использовал следующие источники для понимания структуры и порядка величин:

- [Gear Solutions: A New Standard in Gear Inspection](https://gearsolutions.com/features/a-new-standard-in-gear-inspection/) — общая структура AGMA Q-grades и переход к AGMA 2015 (A-grades).
- [Engineers Edge: AGMA Fine Pitch Tolerances / Quality Grades for Gears](https://www.engineersedge.com/gears/gear_toleances_fine_pitch.htm) — численные значения tooth-to-tooth composite и total tolerance для Q10/Q11 в зависимости от диаметра.
- [LEADRP: ISO 1328 — The Global Standard For Gear Accuracy](https://leadrp.net/blog/iso-1328-the-global-standard-for-gear-accuracy/) — описание ISO 1328-1, классы точности 1–12.
- [KHK Gears: Accuracy of Gears](https://khkgears.net/new/gear_knowledge/gear_technical_reference/accuracy_of_gears.html) — определения fpt, Fα, Fβ, Fr по JIS B 1702.
- [IGS Gear: Understanding Gear Accuracy Grades (DIN/AGMA)](https://igsgear.com/gear-accuracy-grades/) — соответствия между AGMA, DIN, ISO grades.
- [THORS: Gear Hobbing Cutting Parameters](https://thors.com/gear-hobbing-cutting-parameters-to-optimize-the-hobbing-process/) — зависимость cutting speed от модуля; модули >4 требуют двух проходов.
- [BD Gears: Gear Accuracy Grades — Comparing Standards](https://bdgears.com/gear-accuracy-grades-comparing-standards/) — таблица сопоставления AGMA/DIN/ISO.

Для точных таблиц допусков ISO 1328-1:2013 или AGMA 2000-A88 нужен доступ к стандартам через [iso.org](https://www.iso.org/standard/56000.html) или [members.agma.org](https://members.agma.org/MyAGMA/MyAGMA/Store/Item_Detail.aspx?iProductCode=1328_1_B14&Category=STANDARDS).

---

## Промежуточный итог

- Поняли разделение endpoint'ов backend-сервисов: equipment, production, quality, maintenance — каждый со своей зоной ответственности.
- Поняли, что партия становится `done` локально в симуляторе, без специального события; backend узнаёт через `batch_completion(work_center="inspection")`.
- Разобрали Python-идиомы: `and`-цепочка для накопления флага, `break` vs `return`, `sorted` + `key`-функция, стабильность сортировки.
- Добавили учёт приоритета (rush > urgent > normal) в production-диспетчере и в формировании загрузки печи. Подтвердили бенчмарком что overhead незаметен.
- Разрешили смешивание product_code в одной загрузке печи (отказ от recipe-based scheduling). `FurnaceLoad.product_code` → `product_codes: list[str]`.
- Ввели три уровня модификаторов по типоразмеру шестерни: cycle_time, sensor readings, quality measurement specs. Все четыре типоразмера (SPUR-S, SPUR-M, HEL-M, HEL-L) теперь различаются в логах.
- Собрали справку по источникам AGMA/ISO/DIN для диплома.

---

## 10. WebUI: фиксы из-за переименования и закэшированный JS

После переименования `FurnaceLoad.product_code: str` → `product_codes: list[str]` в WebUI стали отображаться:
- `[object Object]` в очереди перед печью
- `undefined (50 шт)` в загрузках печи

### Корневая причина каждой проблемы

**`[object Object]`** — в JS template literal `${id}` приводит объект к строке через `Object.prototype.toString()` → `"[object Object]"`. Раньше код был:
```javascript
$("queue-furnace").innerHTML = qf.map(id => `<span class="chip">${id}</span>`).join("");
```
Где `qf` — массив **объектов Batch** (после `asdict()` в snapshot), а не массив id. Каждый элемент превращался в `[object Object]`.

Фикс — обращение к нужному полю:
```javascript
qf.map(b => `<span class="chip">${b.batch_id}</span>`)
```

**`undefined`** — JS возвращает `undefined` при чтении несуществующего поля объекта. Поле в `FurnaceLoad` теперь называется `product_codes`, а WebUI читал старое `product_code`. Фикс:
```javascript
${load.product_codes.join(", ")}
```

### Кэш браузера

После правки JS-файла в браузере **продолжало** отображаться старое поведение. Причина — браузер использует закэшированный `app.js` из `Disk Cache`. Лечится **Ctrl+Shift+R** (Hard Reload), который пропускает кэш.

Эта проблема уже встречалась в [Конспекте 2026-05-20, секция 17](Конспект-2026-05-20.md) — стоит запомнить: **любая правка фронтенда требует Hard Reload для проверки**.

---

## 11. WebUI: сортировка таблицы активных партий

Сначала сделали фильтры через select-ы (по продукту/приоритету/стадии/станку) — функционально работало, но UX неудобный: 5 контролов сверху ломали компактность таблицы.

Заменили на **клик по заголовку столбца → сортировка по этому столбцу**. Двусторонняя: первый клик ▲ (asc), повторный ▼ (desc), клик по другому столбцу — переключение на него с ▲.

### Архитектура

В `app.js` глобальное состояние:
```javascript
const sortState = { column: "batch_id", dir: "asc" };
```

Каждой колонке соответствует **ключ-функция**:
```javascript
const SORT_KEYS = {
    batch_id:    (b) => b.batch_id,
    progress:    (b) => b.quantity > 0 ? b.parts_done_in_stage / b.quantity : 0,
    fails_count: (b) => b.fails_count,
    ...
};
```

Сортировка перед рендером:
```javascript
[...batches].sort((a, b) => {
    const ka = keyFn(a), kb = keyFn(b);
    if (ka < kb) return -1 * factor;
    if (ka > kb) return  1 * factor;
    return 0;
});  // factor = +1 для asc, -1 для desc
```

### Индикатор ▲/▼ через CSS

В HTML заголовки получили `data-sort="batch_id"`. CSS:
```css
table.sortable th.sort-asc::after  { content: "▲"; }
table.sortable th.sort-desc::after { content: "▼"; }
```

При смене сортировки JS навешивает/снимает классы `.sort-asc`/`.sort-desc` на нужный `<th>`.

### Нелексикографические столбцы

Два столбца требуют **специальной логики**, потому что алфавитная сортировка даёт неправильный результат:

**Приоритет** — лексикографически `normal < rush < urgent`, что бессмысленно. Используем ранг:
```javascript
const PRIORITY_RANK = { rush: 0, urgent: 1, normal: 2 };
```
При asc rush идёт первым, normal — последним.

**Прогресс** — сортировать строку `"50/80"` как текст некорректно (`"100/200"` будет «меньше» чем `"50/80"`). Сортируем долю `parts_done / quantity` — число.

---

## 12. Симметризация stage и сортировка по маршруту

### Что было

В `equipment._route_batch_to_next_stage` логика выглядела так:
```python
batch.stage = "waiting_" + next_stage if next_stage == "heat_treatment" else from_stage
```

То есть только перед печью партия получала префикс `waiting_`, в остальных очередях `stage` оставался **именем предыдущего этапа**. Партия в `queue_hobbing` имела `stage="turning"` — на дашборде это путало.

Дополнительно в `furnace._unload`:
```python
batch.stage = "heat_treatment"
self.state.queues["queue_grinding"].append(batch)
```
После выгрузки партия лежала в `queue_grinding` со `stage="heat_treatment"` — то есть значение `heat_treatment` означало одновременно «в печи» и «ждёт grinding».

### Что стало

Убрали условие в equipment:
```python
batch.stage = "waiting_" + next_stage
```
И в furnace `_unload`:
```python
batch.stage = "waiting_grinding"
```

Теперь у партии **12 возможных значений stage** — каждое соответствует ровно одному физическому положению:

| Ранг | Stage | Где партия |
|---|---|---|
| 1 | pending | очередь перед turning |
| 2 | turning | на токарной |
| 3 | waiting_hobbing | очередь перед hobbing |
| 4 | hobbing | на hobbing |
| 5 | waiting_shaving | очередь перед shaving |
| 6 | shaving | на shaving |
| 7 | waiting_heat_treatment | очередь перед печью |
| 8 | heat_treatment | в печи |
| 9 | waiting_grinding | очередь перед grinding |
| 10 | grinding | на grinding |
| 11 | waiting_inspection | очередь перед inspection |
| 12 | inspection | на inspection |
| 13 | done | удалена из state.batches (в active не видна) |

### Сортировка по stage

Алфавитная сортировка ставила бы `waiting_*` перед всеми остальными (буква `w`). По смыслу же `waiting_hobbing` должен идти после `turning`, не отдельным блоком.

Решение — STAGE_RANK как у приоритета:
```javascript
const STAGE_RANK = {
    pending: 1, turning: 2, waiting_hobbing: 3, hobbing: 4, ...
};
```

Теперь сортировка по столбцу «Стадия» ▲ выстраивает партии **по ходу маршрута** — сверху pending, снизу inspection. ▼ — наоборот.

---

## 13. Архитектура /restart и причина «не очищается БД»

### Симптом

Малик жалуется: «нажимаю Restart, БД не очищается». При том что `/reset` endpoint'ы на всех трёх сервисах есть и `FactoryClient.reset_remote_state()` их дёргает.

### Корневая причина

В `loop.restart()` было защитное условие:
```python
def restart(self) -> None:
    if self.state.virtual_time == config.SIM_START_TIME:
        return    # ← ранний выход
    self.state.reset()
    ...
    client.reset_remote_state()
```

Логика — «нечего сбрасывать, симулятор уже на старте». Но условие проверяет **только состояние симулятора**, игнорируя **состояние БД**. Они могут расходиться:

1. Поднял `docker compose up` → БД заполнена `init.sql`, симулятор только проснулся. `virtual_time == SIM_START_TIME`. Нажал Restart → early return, БД не чистится.
2. После любого `state.reset()` симулятор снова в `SIM_START_TIME` → повторный Restart тоже не работает.

### Вторичная проблема

В `reset_remote_state` ошибки сервисов **молча проглатываются**:
```python
try:
    self._session.post(url, timeout=config.HTTP_TIMEOUT_SEC)
except Exception:
    pass
```

Если сервис вернул 500 — узнать об этом нельзя, БД останется грязной. Стоит хотя бы логировать.

### Что делать

Убрать защитное условие. `state.reset()` и `TRUNCATE` идемпотентны — повторный вызов безопасен. Дёргать `restart` всегда.

### Реальная причина у Малика (оказалось)

В итоге выяснилось, что БД-то очищается, **но Grafana отдаёт устаревший снимок** из своего кэша (Grafana кэширует результаты SQL-запросов). Не проблема симулятора. Защитное условие в `loop.restart()` всё равно лучше убрать — оно потенциальный источник багов.

---

## 14. Sync Fleet: динамическая регистрация парка станков

### Проблема

Парк станков был статически прописан в **двух местах**:
- `simulator/config.py` → `MACHINES`
- `postgres/init/init.sql` → `INSERT INTO machines ...`

Если меняешь config (добавил/убрал станок) — БД не знает. Запуск симулятора с новой машиной → FK ошибки (`open_work_orders.machine_id` ссылается на несуществующую строку).

### Архитектурное решение

**Источник истины — `config.MACHINES`**. БД синхронизируется с ним явно по команде «Sync Fleet».

Принципы:
1. `init.sql` — только `CREATE TABLE`. Никаких INSERT.
2. Симулятор шлёт текущий `config.MACHINES` на equipment **по кнопке Sync Fleet**, не автоматически.
3. Equipment делает идемпотентный UPSERT + DELETE устаревших.
4. Этот sync — **отдельная операция**, не часть Restart. Restart — про runtime state, Sync — про парк машин.

### Endpoint `/register-machines` в equipment

Принимает payload:
```python
class FleetSync(BaseModel):
    machines: list[MachineSpec]
    init_state: str = "idle"
    init_time: str = "2024-01-01 00:00:00"
```

В **одной транзакции**:
1. `DELETE FROM machine_status WHERE machine_id != ALL(<ids>)` — освобождаем FK.
2. `DELETE FROM machines WHERE machine_id != ALL(<ids>)` — удаляем устаревшие.
3. `INSERT INTO machines ... ON CONFLICT (machine_id) DO UPDATE SET ...` — UPSERT.
4. `INSERT INTO machine_status ... ON CONFLICT DO NOTHING` — добавляем запись о состоянии для новых машин.
5. `UPDATE machine_status SET current_state = init_state, ...` — обнуляем state у всех.

Всё идемпотентно: повторный вызов с тем же payload оставляет БД в том же состоянии.

### Зачем UPSERT вместо TRUNCATE+INSERT

`TRUNCATE machines` упал бы по foreign key constraint от `machine_status` и `open_work_orders`. Можно было бы `TRUNCATE CASCADE`, но это сносит и зависимые данные — нежелательно.

UPSERT (`INSERT ... ON CONFLICT DO UPDATE`) — атомарный «обновить или вставить»:
- если строка с таким primary key есть — обновить (или ничего не делать, если все поля те же)
- если нет — вставить

PostgreSQL делает UPSERT тысячами в секунду. **16 машин занимают миллисекунды**, незаметно.

### Расширение config.MACHINES

Добавили четвёртое поле `model`:
```python
MACHINES = [
    ("M-LATHE-01", "turning",    "turning",        "CKE6150"),
    ("M-LATHE-02", "turning",    "turning",        "CKE6150"),
    ("M-LATHE-03", "turning",    "turning",        "CKE6150Z"),
    ...
]
INSTALL_DATE = "2024-01-01"
```

`install_date` пока единая константа — для дипломной модели достаточно. В реальном заводе у каждой машины свой год установки, но это легко расширить.

### Endpoint `/sync-fleet` в симуляторе и кнопка

В симуляторе:
```python
@app.post("/sync-fleet")
def sync_fleet():
    result = client.sync_fleet()
    return {"ok": True, "result": result}
```

`FactoryClient.sync_fleet()` собирает payload из `config.MACHINES` и шлёт на equipment.

В WebUI кнопка **«Sync Fleet»** справа от кнопок скорости, отделена `|`. При клике:
- `POST /sync-fleet` на симулятор
- кнопка меняет текст на `syncing…`, потом `synced N` (или `sync failed`)
- через 2 секунды возвращается к исходному виду

Английское название согласуется со стилем кнопок управления (Start/Stop/Restart). «Fleet» — стандартный английский термин для «парка машин» (machine fleet, equipment fleet).

### Workflow первого запуска

После этих изменений `init.sql` не заполняет `machines`. На свежей БД таблица пустая. Поэтому:
1. `docker compose up`
2. Открыть WebUI → нажать **Sync Fleet** (machines + machine_status наполнятся)
3. Нажать **Start**

Без Sync Fleet любое `state_change` упадёт (UPDATE 0 rows), а WO создаст FK ошибку.

### Поле `machines.status` — оставлен, но не используется

В исходной схеме есть `status TEXT NOT NULL DEFAULT 'active'`. Это поле для **soft-delete** (вывод машины из эксплуатации с сохранением истории). В нашей модели не используется: hard DELETE через `/register-machines` достаточен. Оставили без изменений как «на потом».

---

## Итог дня

К промежуточному итогу добавились:
- Поняли почему `[object Object]` и `undefined` в WebUI — техническая природа JS-template literals и доступа к несуществующему полю. Hard Reload — обязательное правило при правке фронта.
- Заменили фильтры в таблице активных партий на двустороннюю сортировку по клику на заголовок. Для приоритета и прогресса — нестандартные ключи (ранг, доля).
- Симметризовали значения `batch.stage` — все 12 промежуточных состояний различимы. Сортировка по маршруту через STAGE_RANK.
- Разобрали причину «БД не очищается при Restart» — защитное условие в `loop.restart()`. Дополнительно отметили что Grafana кэширует запросы.
- Реализовали Sync Fleet: динамическую регистрацию парка станков через `/register-machines` с UPSERT в одной транзакции. Единственный источник истины — `config.MACHINES`. `init.sql` теперь только схема.
