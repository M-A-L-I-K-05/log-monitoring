# Конспект — 2026-05-23

Темы: пропуск событий на высокой скорости, HTTP-батчинг, catch-up loop с timestamps, ConnectionPool, adaptive sleep, /reset endpoints, каскадный reset подсистем, согласованность имён состояний печи.

---

## 1. Проблема: пропуск sensor_reading на speed=1000x

### Что наблюдалось

При `speed=1000x` в Loki sensor_readings приходили нерегулярно: то с интервалом 15 виртуальных секунд (как должно), то с пропусками по 30–60 секунд. Логика в equipment.py была:

```python
if (now - machine.last_sensor_sent_at).total_seconds() >= SENSOR_INTERVAL_SEC:
    self.client.sensor_reading(machine, readings, event_time=now)
    machine.last_sensor_sent_at = now
```

### Почему пропускалось

На `1000x` за один реальный тик (`TICK_REAL_SEC = 0.1` секунды) проходит **100 виртуальных секунд** — это 6–7 интервалов сенсора по 15 секунд. Но `if` отправлял только **одно** показание за тик. Остальные 5–6 терялись.

Плюс: каждое показание — синхронный `requests.post()`. На стенде с 20+ станками за тик нужно сделать сотни POST'ов. Они выполняются последовательно, сетевой стек не успевает → реальный тик растягивается, скорость падает ниже целевой `1000x`.

### Варианты решения

| Вариант | Идея | Минус |
|---|---|---|
| A | Уменьшить `TICK_REAL_SEC` (например, 0.01) | Не решает проблему медленных POST'ов, лишь сдвигает её |
| B | Catch-up: `while`-цикл с корректными timestamps | Шлёт все показания, но всё ещё много POST'ов |
| C | A + B + батчинг POST'ов | Реальное решение |

Выбрали **C**: catch-up + батчинг + connection pool.

---

## 2. Catch-up паттерн: `while` вместо `if`

### Идея

Вместо «послать одно показание если пора» — послать **все**, что должны были произойти за тик, **с правильными виртуальными timestamps**.

```python
sensor_step = timedelta(seconds=config.SENSOR_INTERVAL_SEC)
if machine.last_sensor_sent_at is None:
    machine.last_sensor_sent_at = machine.state_changed_at
next_sensor_at = machine.last_sensor_sent_at + sensor_step
while next_sensor_at <= now:
    readings = self._generate_sensor_readings(machine)
    self.client.sensor_reading(machine, readings, event_time=next_sensor_at)
    machine.last_sensor_sent_at = next_sensor_at
    next_sensor_at += sensor_step
```

### Что важно

- **`event_time` — это вычисленное время**, не `now`. Если тик длится 100 виртуальных секунд и за это время «должны были» произойти показания на `t+15`, `t+30`, `t+45`, `t+60`, `t+75`, `t+90` — каждое уходит со своим точным timestamp.
- **База отсчёта** (`state_changed_at`) важна. Если станок только что вошёл в `running`, первое показание идёт через 15с от момента входа в running, а не «через 15с от последнего показания на прошлом станке».
- В Loki показания теперь идут ровно каждые 15 виртуальных секунд: `06:46:01.200`, `06:46:16.200`, `06:46:31.200`, …

### Аналогично для cycle_completion

```python
cycle_event_time = machine.state_changed_at + timedelta(seconds=machine.parts_done_in_batch * cycle_sec)
```

Каждая деталь завершилась в момент `state_changed_at + N × cycle_sec`. Алармы (износ инструмента) шлются с тем же timestamp.

### Где применили

- `simulator/subsystems/equipment.py` — sensor_reading и cycle_completion
- `simulator/subsystems/furnace.py` — sensor_reading для каждой фазы (база: `load.phase_started_at`)

---

## 3. HTTP-батчинг

### Идея

Накопить однотипные события за тик в буфер, в конце тика отправить **одним POST**.

### Реализация в `simulator/client.py`

```python
class FactoryClient:
    def __init__(...):
        self._sensor_buffer: list[dict] = []
        self._cycle_buffer: list[dict] = []
        self._measurement_buffer: list[dict] = []
    
    def sensor_reading(self, machine, readings, event_time):
        # раньше: requests.post(...)
        # теперь: складываем в буфер
        self._sensor_buffer.append({
            "machine_id": machine.machine_id,
            "readings": readings,
            "event_time": event_time.isoformat(),
        })
    
    def flush(self) -> None:
        if self._sensor_buffer:
            requests.post(f"{EQUIPMENT_URL}/sensor-reading/batch",
                          json={"items": self._sensor_buffer})
            self._sensor_buffer.clear()
        # аналогично для _cycle_buffer и _measurement_buffer
```

### Что не батчится

- `state_change`, `batch_start`, `batch_move`, `batch_completion`, `order_creation` — этих событий мало (на тик: единицы), батчинг даст экономию ≈0. Оставили синхронные POST'ы.
- Батчатся только **высокочастотные**: sensor_reading, cycle_completion, measurement.

### Вызов flush

В конце каждого тика после прохода подсистем:

```python
# simulator/loop.py
for sub in self.subsystems:
    sub.tick(now)
if client is not None:
    client.flush()
```

### Backend-стороны

Нужны новые эндпоинты, принимающие массив:

```python
# services/equipment/main.py
@app.post("/sensor-reading/batch")
def sensor_reading_batch(data: SensorBatch):
    # один UPDATE machine_status на машину с МАКСИМАЛЬНЫМ event_time
    # (DB хранит только последний статус; логи в Loki хранят все)
```

Аналогично `/cycle-completion/batch` (только лог) и `/measurement/batch` (только лог) в quality.

### Почему MAX event_time в DB

Таблица `machine_status` — это **снимок текущего состояния**, не история. В рамках одного батча 6 показаний для одной машины — в DB важно лишь последнее. История хранится в Loki через структурированные логи.

---

## 4. `psycopg_pool.ConnectionPool` — переиспользование соединений

### Зачем

Раньше каждый запрос открывал новое соединение к PostgreSQL:

```python
with psycopg.connect(DATABASE_URL) as conn:
    with conn.cursor() as cur:
        cur.execute(...)
```

Открытие TCP + handshake + auth = ~10–20 мс. На сотни запросов в секунду — задержка ощутимая.

### Pool

```python
from psycopg_pool import ConnectionPool

pool = ConnectionPool(
    DATABASE_URL,
    min_size=2,    # минимум держим 2 готовых соединения
    max_size=10,   # максимум 10
    open=True,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    pool.close()

# использование
with pool.connection() as conn:
    with conn.cursor() as cur:
        cur.execute(...)
```

### Как это работает

- При старте сервиса pool открывает `min_size` соединений и держит их.
- `pool.connection()` отдаёт **готовое** соединение из пула. Никакого нового TCP-handshake.
- После `with` соединение возвращается в пул (не закрывается).
- Если все заняты — pool открывает ещё, до `max_size`.

### Где применили

- `services/equipment/main.py`
- `services/production/main.py`
- `services/maintenance/main.py`

В `requirements.txt` добавили `psycopg-pool==3.2.4`.

### Quality не трогали

Quality не пишет в БД (только логи), поэтому pool там не нужен.

---

## 5. Adaptive sleep в loop

### Проблема старой логики

Было:
```python
self.state.advance_time(config.TICK_REAL_SEC)
# работа
time.sleep(config.TICK_REAL_SEC)
```

Если работа за тик заняла 80 мс, а `TICK_REAL_SEC = 100 мс` — мы спим ещё 100 мс. Реальный тик = 180 мс вместо целевых 100. Скорость падает.

### Решение

```python
tick_started = time.perf_counter()
# работа
elapsed = time.perf_counter() - tick_started
sleep_left = config.TICK_REAL_SEC - elapsed
if sleep_left > 0:
    time.sleep(sleep_left)
```

- Спим **только остаток** до `TICK_REAL_SEC`.
- Если работа уже заняла больше — не спим, сразу следующий тик.

### `time.perf_counter()` vs `time.time()`

| Функция | Что измеряет | Точность |
|---|---|---|
| `time.time()` | Wall clock (UTC) | ~1 мс, может прыгать при NTP sync |
| `time.perf_counter()` | Монотонный счётчик от старта процесса | ~ns, не прыгает |

Для измерения интервалов всегда `perf_counter`. Для отображения текущего времени — `time.time()`.

---

## 6. Каскадный reset подсистем

### Проблема

После нажатия Restart симулятор стартовал заново (`state.reset()`), но **ордера не создавались**. Почему?

В `OrdersSubsystem` есть поле:
```python
self._next_order_at: datetime | None = None
```

После первой генерации заказа оно становится конкретным datetime в будущем. Когда `state.reset()` обнуляет `virtual_time` обратно в `SIM_START_TIME`, поле `_next_order_at` остаётся в **далёком будущем**. Условие `now >= self._next_order_at` никогда не срабатывает — нового заказа не будет.

### Где ещё то же самое

- `ScenariosController.active: dict` — активные сценарии переживают reset.
- `ScenariosController._seq` — счётчик ID продолжает расти после restart.

### Решение: соглашение «у подсистемы может быть метод reset()»

```python
# simulator/subsystems/orders.py
def reset(self) -> None:
    """Сброс при /restart симулятора."""
    self._next_order_at = None

# simulator/subsystems/scenarios.py
def reset(self) -> None:
    self.active.clear()
    self._seq = 0
```

### В loop.restart() — итерация по подсистемам

```python
def restart(self) -> None:
    if self.state.virtual_time == config.SIM_START_TIME:
        return
    self.state.reset()
    for sub in self.subsystems:
        reset_fn = getattr(sub, "reset", None)
        if callable(reset_fn):
            reset_fn()
    client = self._find_client()
    if client is not None:
        client.reset_remote_state()
```

### Паттерн «опциональный метод через getattr»

```python
reset_fn = getattr(sub, "reset", None)
if callable(reset_fn):
    reset_fn()
```

- `getattr(obj, "name", default)` — достать атрибут или вернуть default если нет.
- `callable(x)` — проверить что это вызываемый объект (функция/метод/класс).
- Альтернатива через `try/except AttributeError` — менее чистая, контролирует ошибку, а не наличие.
- Не требует наследования от какого-то общего базового класса. Подсистемы свободны.

---

## 7. /reset endpoints на backend

### Проблема

После Restart в БД остаются **старые** записи:
- `active_batches` — все партии прошлого прогона
- `open_work_orders` — work orders прошлого прогона
- `machine_status` — состояния машин на момент остановки

При следующем прогоне симулятор генерирует партии с теми же ID (счётчик `_batch_seq` сбрасывается) → **дубль primary key** → FK-ошибка.

### Решение

В каждый из трёх сервисов добавили POST `/reset`:

```python
# services/production/main.py
@app.post("/reset")
def reset():
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE active_batches")
    return {"status": "ok"}

# services/maintenance/main.py
@app.post("/reset")
def reset():
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE open_work_orders")
    return {"status": "ok"}

# services/equipment/main.py
@app.post("/reset")
def reset():
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE machine_status SET state='idle', state_changed_at=NOW()")
    return {"status": "ok"}
```

### TRUNCATE vs DELETE

| Команда | Скорость | Сброс счётчиков | Можно с WHERE |
|---|---|---|---|
| `DELETE FROM t` | Медленнее (по строкам) | Нет | Да |
| `TRUNCATE t` | Очень быстро (drop+create) | Да (если есть SERIAL) | Нет |

Для полного сброса таблицы `TRUNCATE` лучше: быстрее и сбрасывает auto-increment.

### Equipment — UPDATE, не TRUNCATE

В `machine_status` строки **должны** существовать — это снимок парка. TRUNCATE удалит сами строки. Поэтому UPDATE: возвращаем все машины в `idle` с текущим NOW().

### Клиент дёргает все три

```python
# simulator/client.py
def reset_remote_state(self) -> None:
    for url in (EQUIPMENT_URL, PRODUCTION_URL, MAINTENANCE_URL):
        try:
            requests.post(f"{url}/reset", timeout=2)
        except requests.RequestException as exc:
            logger.warning("reset failed: %s", exc)
```

Вызывается из `loop.restart()` **после** `state.reset()`.

---

## 8. Согласованность имён состояний печи

### Что было

Цепочка переходов печи генерировала state_changes с разными префиксами:

```
idle → loading → heating → carburizing → quenching → tempering → unloading → empty → idle
```

`idle → loading` (без префикса), потом `loading → heating` тоже без префикса. Где-то в коде префикс `furnace_` появлялся, где-то нет.

### Двойной state_change

`_advance_phase()` после `unloading` шлёт переход `unloading → empty`, потом `_unload()` — `empty → idle`. Получается **два** state_change подряд, при этом `empty` — фантомное состояние, печь в этот момент уже не работает.

### Что сделали

1. **Унифицировали префиксы**: все состояния печи — `furnace_loading`, `furnace_heating`, …, `furnace_unloading`.
   ```python
   # при старте новой загрузки
   self.client.state_change(machine, old_state="idle",
                            new_state="furnace_loading", ...)
   ```

2. **Убрали `furnace_empty`**: после `furnace_unloading` сразу идёт `idle`. В `_advance_phase()`:
   ```python
   if next_phase == "empty":
       # Промежуточный state_change "furnace_unloading→furnace_empty"
       # не шлём — реальный переход "furnace_unloading→idle" сделает _unload().
       self._unload(machine, load, now)
       return
   ```

   А в `_unload()`:
   ```python
   self.client.state_change(machine, old_state="furnace_unloading",
                            new_state="idle", event_time=now)
   ```

### Почему `next_phase == "empty"` всё ещё есть

В таблице `FURNACE_NEXT_PHASE` логически после `unloading` идёт `empty` — это маркер «загрузка завершена». Сам state_change для него не шлём, но фаза в `FurnaceLoad.phase` промежуточно есть. После `_unload()` объект `FurnaceLoad` удаляется из `state.furnace_loads` — и `empty` пропадает.

---

## 9. Foreign key — несогласованность config и init.sql

### Что было

```python
# simulator/config.py
("M-GMM-02", "gear_measurement_machine", "KLINGELNBERG-P26", ...)
```

```sql
-- postgres/init/init.sql
INSERT INTO machines VALUES ('M-CMM-01', 'gear_measurement_machine', 'ZEISS-PRISMO', ...);
```

Симулятор шлёт sensor_reading для `M-GMM-02`, equipment-сервис делает UPDATE `machine_status WHERE machine_id='M-GMM-02'` — строки нет, FK на `machines.machine_id` падает с ошибкой.

### Фикс

Поправили `init.sql`, заменили `M-CMM-01 ZEISS-PRISMO` на `M-GMM-02 KLINGELNBERG-P26`. Теперь обе стороны согласованы.

### Урок

При двух источниках истины (config симулятора и init.sql БД) рано или поздно они разойдутся. На production стенде стоит думать о генерации одного из другого — например, скрипт `init.sql` генерируется из `config.py` через шаблон. Не сделали в этой сессии, потому что задача и так была насыщенной — отметили как TODO.

---

## 10. `pending_inspection_measurements` — 4-кортеж

### Что было

```python
state.pending_inspection_measurements: deque[tuple[str, int, str]]
# (batch_id, part_idx, machine_id)
```

Equipment складывал задачу в эту очередь, когда part завершалась. Quality на следующем тике её забирал, считал измерения, отправлял `measurement` со временем `now`.

### Проблема

`now` в момент Quality-тика **отличается** от реального времени, когда деталь была измерена. Между equipment и quality тиками — целая виртуальная минута может пройти. В логах измерение оказывается с поздним timestamp → артефакты при анализе.

### Фикс

Добавили четвёртое поле — `event_time`:

```python
state.pending_inspection_measurements: deque[tuple[str, int, str, datetime]]
# (batch_id, part_idx, machine_id, event_time)
```

Equipment при постановке задачи кладёт **уже вычисленный** `cycle_event_time` (момент завершения детали). Quality его берёт и использует:

```python
batch_id, part_idx, machine_id, event_time = self.state.pending_inspection_measurements.popleft()
self.client.measurement(..., event_time=event_time)
```

---

## 11. Бенчмарк тика с warmup

### Что было

`benchmark_tick.py` запускал loop и сразу мерил тики. Проблема: в первые секунды **парк ещё в idle**, подсистемы почти ничего не делают → тик ~0.012 мс. Это не отражает реальность.

### Что стало

```python
# 500 warmup ticks на скорости 1000x — наполняем state
for _ in range(500):
    loop._run_once()
# теперь 30 измерительных тиков
times = []
for _ in range(30):
    t0 = time.perf_counter()
    loop._run_once()
    times.append(time.perf_counter() - t0)
```

### Результат

| Метрика | До | После |
|---|---|---|
| avg / tick | 0.012 мс | ~104 мс |
| Эффективная скорость | — | ~489x |

После добавления adaptive sleep и батчинга — эффективная скорость держится близко к целевой `1000x` (потому что `sleep_left` становится положительным и мы спим ровно остаток).

### Почему `tick = 104 мс` означает скорость ~489x

`TICK_REAL_SEC = 0.1` (целимся в 100 мс на тик).
За тик виртуально проходит `0.1 × 1000 = 100` виртуальных секунд.
Если реально тик длится 104 мс, эффективная скорость = `100 / 0.104 ≈ 961x`.

(В первом замере было ~489x — это до adaptive sleep и батчинга, тик длился ~200 мс.)

---

## 12. Архитектурное наблюдение: batching ≠ для всех событий

Не стоит автоматически батчить каждое событие. Решение зависит от **частоты**:

| Событие | Частота | Стоит ли батчить |
|---|---|---|
| sensor_reading | ~25 машин × 4/мин = 100/мин | Да |
| cycle_completion | ~25 машин × 1/мин = 25/мин | Да |
| measurement | при инспекциях, всплески | Да |
| state_change | единицы в минуту | Нет — мало |
| batch_start, batch_move | редко | Нет |
| alarm | очень редко | Нет |

Батчинг для редких событий **усложняет код без выигрыша**: задержка flush'а размывает «событийность», добавляется один лишний цикл сериализации.

---

## 13. Что изменилось в файлах (для самопроверки)

### `simulator/`
- `client.py` — буферы + flush + reset_remote_state (значительно изменён)
- `loop.py` — flush в конце тика, adaptive sleep, restart с каскадом reset
- `state.py` — type hint у `pending_inspection_measurements` (4-tuple)
- `subsystems/equipment.py` — catch-up loop для sensor + cycle
- `subsystems/furnace.py` — catch-up loop, `furnace_loading`, убран `furnace_empty`
- `subsystems/quality.py` — распаковка 4-кортежа
- `subsystems/orders.py` — метод `reset()`
- `subsystems/scenarios.py` — метод `reset()`
- `benchmark_tick.py` — warmup + измерение

### `services/`
- `equipment/main.py` — pool + `/sensor-reading/batch` + `/cycle-completion/batch` + `/reset`
- `production/main.py` — pool + `/reset`
- `maintenance/main.py` — pool + `/reset`
- `quality/main.py` — `/measurement/batch`
- `*/requirements.txt` — `psycopg-pool==3.2.4`

### `postgres/init/init.sql`
- `M-CMM-01 ZEISS-PRISMO` → `M-GMM-02 KLINGELNBERG-P26`

---

## Итог дня

- Поняли причину пропуска sensor_reading на 1000x: один POST на тик + синхронные запросы.
- Реализовали **catch-up loop** с правильными виртуальными timestamps (база — `state_changed_at` / `phase_started_at`).
- Сделали **HTTP-батчинг** для высокочастотных событий (sensor / cycle / measurement); state_change и прочее редкое — оставили синхронным.
- Заменили `psycopg.connect()` на `ConnectionPool` в трёх сервисах.
- Добавили **adaptive sleep** в loop — не пересыпаем, когда работа уже заняла большую часть тика.
- Починили Restart: добавили `/reset` endpoints на backend, метод `client.reset_remote_state()`, каскадный вызов `reset()` у подсистем через `getattr`.
- Унифицировали имена состояний печи (`furnace_loading`, …), убрали фантомное `furnace_empty` и двойной state_change.
- Поправили FK-ошибку в init.sql (M-GMM-02).
- Переписали бенчмарк с warmup-фазой — теперь измеряет нагруженный тик.

После всех изменений: sensor_reading приходят ровно каждые 15 виртуальных секунд, симулятор держится близко к целевой скорости 1000x, Restart полностью обнуляет состояние как в симуляторе, так и в БД.
