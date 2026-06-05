# Конспект — 2026-05-20

Темы: цикл симулятора, потоки, dataclass, аннотации типов, состояние симулятора, Grafana.

---

## 1. Изменение цикла симулятора

В `simulator/loop.py` заменили `print` на `raise` — теперь при ошибке цикл падает и не продолжает крутиться.

```python
except Exception as exc:
    raise RuntimeError(f"loop iteration failed: {exc}") from exc
```

### Синтаксис `raise ... from ...`

- `raise NewError(...) from old_exc` — поднять новую ошибку, **причиной** которой была старая.
- В терминале выводятся обе ошибки: сначала оригинальная, потом новая.
- Помогает понять и **что** упало, и **где** именно.

Без `from` — увидишь только верхнюю ошибку, потеряешь контекст.

---

## 2. Потоки в симуляторе

Программа всегда стартует с **одного главного потока** (его создаёт uvicorn). Каждый `Thread(...)` добавляет ещё один поверх.

```
uvicorn (главный поток) ─ обрабатывает HTTP-запросы
└── sim-loop (создаётся в loop.start()) ─ крутит симуляцию
```

### Жизненный цикл потока

- Поток существует пока выполняется его функция (`_run`).
- Когда `while not self._stop_event.is_set()` → False, цикл выходит, поток закрывается **автоматически**.
- При повторном вызове `start()` создаётся **новый** объект `Thread`.

### Защита от двойного запуска

```python
if self._thread is not None and self._thread.is_alive():
    return
```

Не даёт создать второй sim-loop, если один уже работает.

---

## 3. Декоратор `@dataclass`

При оборачивании класса декоратором Python автоматически генерирует:

| Метод | Что делает |
|---|---|
| `__init__` | Конструктор со всеми полями как параметрами |
| `__repr__` | Читаемое представление: `Machine(machine_id='M1', ...)` |
| `__eq__` | Сравнение **по содержимому полей**, не по адресу в памяти |

Опционально:
- `frozen=True` → запрет менять поля (immutable)
- `order=True` → добавит `__lt__`, `__le__`, `__gt__`, `__ge__`

### Ловушка с изменяемыми дефолтами

```python
@dataclass
class A:
    items: list = []   # ← ValueError! Python запрещает.
```

Почему: дефолт вычисляется **один раз** при определении класса, и все экземпляры будут делить один список.

Правильно — через `field(default_factory=...)`:

```python
anomaly_modifier: dict[str, float] = field(default_factory=dict)
```

`default_factory` хранит **функцию** (не объект). При каждом `__init__` функция вызывается заново → новый словарь для каждого экземпляра.

Передаётся **`dict` без скобок** — это сам класс/функция, не экземпляр. `dict()` создаст пустой словарь.

---

## 4. Аннотации типов

```python
dict[str, deque[Batch]]
set[int]
tuple[str, int, str]
```

- Это **подсказки** для разработчика, IDE и mypy.
- Python **не проверяет** их в рантайме — код выполнится с любыми типами.
- `set[int]` означает «множество, элементы — `int`».
- `dict[str, Batch]` — ключи строки, значения — экземпляры `Batch`.
- `tuple[str, int, str]` — кортеж ровно из трёх элементов в этом порядке.

### `set[int]` vs `a[0]` — разные механизмы

| Запись | Что вызывается |
|---|---|
| `a[0]` | `a.__getitem__(0)` — у экземпляра |
| `set[int]` | `set.__class_getitem__(int)` — у класса |

Python различает по тому, что стоит слева: экземпляр или класс.

---

## 5. Множества и кортежи

### `set` — множество уникальных элементов

- Без порядка.
- Дубликаты автоматически выбрасываются.
- По индексу обращаться **нельзя**.
- `in` работает очень быстро — основное применение.

```python
s = set()           # пустое множество
s.add(4)
4 in s              # True — быстрая проверка
```

### `tuple` — упорядоченный набор элементов

В кортеже нет «ключ-значение», просто элементы по порядку. Размер фиксированный.

```python
("batch_001", "hobbing")    # tuple[str, str]
```

### Распаковка в `for`

```python
for mid, mtype, wc in config.MACHINES:
    ...
```

Каждый кортеж распаковывается по позициям в переменные. Количество переменных должно совпадать с размером кортежа — иначе `ValueError`.

---

## 6. `collections.deque`

Двусторонняя очередь — оптимизирована для добавления/удаления **с обоих концов**.

| Операция | `list` | `deque` |
|---|---|---|
| `append` в конец | O(1) | O(1) |
| `pop` с конца | O(1) | O(1) |
| `pop(0)` с начала | O(n) — сдвиг! | O(1) — `popleft()` |
| Доступ `x[5]` | O(1) | O(n) — медленнее |

В симуляторе очереди партий между участками — это `deque`, потому что добавление в конец и забор с начала.

---

## 7. Состояние симулятора (`SimulationState`)

Единое место, где живёт всё состояние. Содержит:

- **Виртуальные часы** — `virtual_time`, `speed`, `running`
- **Парк станков** — `dict[str, Machine]`
- **Бригады** — `dict[str, Brigade]`
- **Очереди партий** — по одной на каждый этап маршрута (`pending`, `queue_hobbing`, `waiting_furnace`, …)
- **Активные сущности** — заказы, партии, work orders, furnace loads
- **Очереди задач для подсистем**:
  - `pending_spot_checks: deque[tuple[str, str]]` — `(batch_id, stage)` для Quality
  - `pending_inspection_measurements: deque[tuple[str, int, str]]` — `(batch_id, part_idx, machine_id)` для финальной инспекции
  - `pending_tool_change_requests: deque[str]` — `machine_id` для Maintenance
- **Счётчики** — для дашборда (orders_total, inspections_pass и т.д.)
- **ID-генераторы** — `_order_seq`, `_batch_seq`, `_wo_seq`, `_load_seq` (счётчики, не последовательности)

### Почему разные имена очередей

Партия движется по маршруту:

```
pending → turning → queue_hobbing → hobbing → queue_shaving → shaving
→ waiting_furnace → heat_treatment → queue_grinding → grinding
→ queue_inspection → inspection
```

Разные имена нужны чтобы понимать **где именно** стоит партия. Каждая подсистема забирает из своей очереди.

---

## 8. `threading.RLock` — потокобезопасность

Два потока (uvicorn + sim-loop) работают с одним `state`. Без блокировки возможны:

1. **`RuntimeError`** — итерация по словарю во время его модификации.
2. **Несогласованный снимок** — `/status` вернёт смесь старых и новых данных.
3. **Потерянные инкременты** — `self._order_seq += 1` не атомарен.

### Паттерн `with self._lock:`

```python
with self._lock:
    # код выполняется эксклюзивно
```

Эквивалент:
```python
self._lock.acquire()
try:
    ...
finally:
    self._lock.release()
```

Гарантирует освобождение замка даже при ошибке.

### Почему `RLock`, а не `Lock`

`RLock` (Reentrant Lock) — **один и тот же поток** может захватить его повторно без deadlock. Нужно когда метод под локом вызывает другой метод тоже под локом.

---

## 9. `QualitySubsystem` — три точки контроля

1. **Spot-check после hobbing** — 1 случайная деталь, 2-3 измерения. Если fail → ещё 2 соседних детали.
2. **Spot-check после heat_treatment** — то же самое.
3. **Финальная инспекция** — 10% выборка с полным набором измерений.

Equipment-подсистема **регистрирует** задачи в очередях `state.pending_*`. Quality-подсистема **выполняет** их на следующем тике.

---

## 10. `FurnaceLoad` — особенность печи

Обычный станок обрабатывает одну партию. Печь же **загружается несколькими партиями сразу** до заполнения вместимости (`FURNACE_CAPACITY_PARTS`).

`FurnaceLoad` представляет одну такую совместную загрузку:
- `batch_ids` — список партий
- `product_code` — один на загрузку (нельзя смешивать рецепты)
- `phase` — текущий этап (loading → heating → carburizing → quenching → tempering → unloading)

`furnace_loads: dict[str, FurnaceLoad]` с ключом `machine_id`: в каждой печи в момент времени **только одна** загрузка.

---

## 11. Pause/Resume vs Stop/Start

Два разных механизма:

| Механизм | Что делает |
|---|---|
| `_stop_event.set()` | Завершает поток sim-loop полностью |
| `state.running = False` | Симулятор продолжает тикать, но пропускает работу |

В текущем коде `pause()`/`resume()` всегда вызываются вместе с `stop_event` — то есть избыточны. Можно было бы реализовать **паузу без убийства потока** через одну переменную `running` — поток крутится, но не работает.

Сейчас оставлено для читаемости (инкапсуляция).

---

## 12. Grafana — проблема с правами

### Что произошло

После `docker compose down + up` Grafana начала падать с:
```
mkdir: can't create directory '/var/lib/grafana/plugins': Permission denied
```

### Причина

- Grafana внутри контейнера работает под **UID 472**.
- Папка `./grafana/data` на хосте принадлежит `vboxuser`.
- У UID 472 только `r-x` права → не может писать.

### Решение

Добавили init-контейнер, который запускается перед Grafana и меняет владельца папки:

```yaml
grafana-init:
  image: busybox
  user: "0"
  command: ["chown", "-R", "472", "/var/lib/grafana"]
  volumes:
    - ./grafana/data:/var/lib/grafana
  restart: "no"

grafana:
  user: "472"
  depends_on:
    - grafana-init
```

- `busybox` — минимальный образ с `chown`.
- `user: "0"` — запуск под root, чтобы иметь право менять владельца.
- `restart: "no"` — выполнил chown и умер.
- Через bind mount `chown` внутри контейнера меняет права на хосте.

### Замечание про безопасность

Контейнер может менять права **только на смонтированных через volumes** директориях. К остальной файловой системе хоста доступа нет — Docker изолирует.

### Куда Grafana пишет данные

- `./grafana/provisioning/` — datasources, конфиги (в git).
- `./grafana/data/grafana.db` — SQLite база: дашборды, пользователи, плагины (НЕ в git).

Дашборды сохраняются в `grafana.db` и переживают `docker compose down`.

---

## 13. PostgreSQL — таблицы для дашборда

Четыре таблицы:
- `machines`
- `machine_status`
- `active_batches`
- `open_work_orders`

Запросы для панелей Grafana:
```sql
SELECT * FROM machines;
SELECT * FROM machine_status;
SELECT * FROM active_batches;
SELECT * FROM open_work_orders;
```

Datasource в Grafana: PostgreSQL, host `postgres:5432`, db `factory`, user `admin`, password `secret`.

Запросы с сортировкой (свежие сверху):
```sql
SELECT * FROM machine_status ORDER BY state_changed_at DESC;
SELECT * FROM active_batches ORDER BY batch_id DESC;
SELECT * FROM open_work_orders ORDER BY wo_id DESC;
```

---

## 14. Рефакторинг `snapshot()` через `asdict()`

Раньше для каждого типа объекта в snapshot были выписаны нужные поля вручную:
```python
"machines": [
    {"machine_id": m.machine_id, "machine_type": m.machine_type, ...}
    for m in self.machines.values()
],
```

Переделали на единообразный вызов `asdict()`:
```python
"machines": [asdict(m) for m in self.machines.values()],
"active_batches": [asdict(b) for b in self.batches.values()],
"queues": {key: [asdict(b) for b in q] for key, q in self.queues.items()},
"furnace_loads": [asdict(fl) for fl in self.furnace_loads.values()],
"open_work_orders": [asdict(wo) for wo in self.work_orders.values()],
```

Все классы — dataclass'ы, поэтому работает. FastAPI сам конвертирует `datetime` → ISO-строку, `set` → list. Минусы: в ответ попадают все поля включая служебные (`anomaly_modifier`, `inspection_sample_indices`). Это допустимо — фронтенд берёт нужное.

### `dict(self.counters)` — защитная копия

Зачем оборачивать словарь в `dict()` при возврате:
```python
"counters": dict(self.counters)
```

`snapshot()` отдаёт словарь FastAPI, который сериализует его **после выхода из `with self._lock:`**. К моменту сериализации лок уже отпущен — другой поток (sim-loop) может изменить `self.counters` прямо во время итерации FastAPI.

`dict(self.counters)` создаёт копию **внутри лока** → FastAPI работает со своей копией, ничто не мешает.

То же самое было раньше с `list(fl.batch_ids)`.

---

## 15. Подсистема `OrdersSubsystem`

Создаёт заказы каждые `ORDER_INTERVAL_MIN_RANGE` (4–8 виртуальных часов).

### Цикл tick
1. Если первый тик — задаём время следующего заказа.
2. Если ещё не пора — выходим.
3. Создаём заказ → разбиваем на партии → регистрируем в state → шлём события клиентам.
4. Назначаем время следующего заказа.

### Взвешенный выбор

```python
def weighted_choice(items: list[tuple]) -> str:
    keys = [k for k, _ in items]
    weights = [w for _, w in items]
    return random.choices(keys, weights=weights, k=1)[0]
```

- `random.choices(keys, weights=weights, k=N)` — возвращает **список из N** элементов с учётом весов.
- `k=1` + `[0]` → достаём один элемент.
- Сумма весов не обязана быть 1.0 — Python нормализует. Веса определяют **соотношения**.

### `_` (underscore) — соглашение «эта переменная не нужна»

```python
keys = [k for k, _ in items]      # нужен только ключ, вес игнорируем
weights = [w for _, w in items]   # нужен только вес, ключ игнорируем
```

Технически можно было назвать `x` — Python не различает. `_` явно сообщает читателю: «здесь распаковка, второе значение я сознательно не использую».

### `random.uniform(lo, hi)`

Случайное **дробное** число от `lo` до `hi`. В отличие от `randint`, который возвращает только целые.

### `min(size, remaining)` — нарезка последней партии

```python
qty = min(size, remaining)
```
Если осталось деталей меньше стандартного размера партии — последняя партия будет неполной. `qty` = quantity (стандартное сокращение).

---

## 16. Сенсорные данные на высоких скоростях

При `1000x` за один тик (0.1 реальной секунды) проходит 100 виртуальных секунд. Между показаниями сенсоров — 15 виртуальных секунд.

Логика в equipment.py:
```python
if (now - machine.last_sensor_sent_at).total_seconds() >= SENSOR_INTERVAL_SEC:
    # отправить ОДНО показание прямо сейчас
    machine.last_sensor_sent_at = now
```

Пропущенные показания **не накапливаются** и **не отправляются**. Просто отправляется одно с актуальным временем. **Плотность данных на высокой скорости падает** — для ML это нужно учитывать.

---

## 17. WebUI — переделка кнопок управления

Было: `Start | Pause | Resume | Stop`. Стало: `Start | Stop | Restart`.

Изменения в трёх местах:
1. `webui/index.html` — три кнопки вместо четырёх.
2. `webui/app.js` — три handler'а.
3. `simulator/main.py` — починен `/restart` (была опечатка: функция называлась `stop()`, конфликт имён; вызывала несуществующий `loop.restart()`).
4. `webui/style.css` — удалил неиспользуемый класс `.btn-yellow`.

### Браузерное кэширование сломало UI

После замены кнопок в HTML машины перестали отображаться **до нажатия Start**. Причина:

- Браузер закэшировал **старый JS** (где были handler'ы для `btn-pause` и `btn-resume`).
- Новый HTML не имеет таких кнопок → `$("btn-pause")` возвращает `null`.
- На строке `$("btn-pause").onclick = ...` → `TypeError`.
- Необработанная ошибка на верхнем уровне **останавливает выполнение всего скрипта**.
- Ниже не выполнились: остальные handler'ы, `setInterval(fetchStatus, ...)`, `fetchStatus()`.
- Поэтому polling /status не запустился — машины не появлялись.
- При нажатии Start — внутри `postCmd` есть `fetchStatus()`, который вызывался вручную → один рендер.

Решение — `Ctrl + Shift + R` (хард-релоад). На production эту проблему решают:
1. Версионирование в URL: `app.js?v=2`
2. Хэш в имени файла: `app.abc123.js` (webpack, vite)
3. Cache-Control заголовки

---

## 18. Реализация `restart` — теория ссылок в Python

### Почему `state = SimulationState()` в endpoint не сработает

В Python переменные — это **независимые имена**, ссылающиеся на объекты. Когда создаём loop:

```python
state = SimulationState()
loop = SimulationLoop(state, subsystems)
```

Внутри `SimulationLoop.__init__`:
```python
def __init__(self, state, ...):
    self.state = state    # ← новая переменная, скопирована СВЯЗКА
```

Получается:
```
state (в main)    ──┐
                    ├──► [SimulationState объект]
loop.state        ──┘
```

Если в endpoint написать `state = SimulationState()`:
```
state (в main)    ──► [НОВЫЙ объект]
loop.state        ──► [СТАРЫЙ объект]    ← по-прежнему держит!
```

- Loop, подсистемы, scenarios — все продолжают работать со СТАРЫМ state.
- Новый никем не используется, кроме main.
- **Старый не освобождается** (на него есть ссылки) → утечка памяти при каждом restart.

### Нет команды «обновить переменную на уровне выше»

Python такого не предоставляет. Есть `global`, но это только для глобальных переменных в том же модуле. Атрибуты других объектов (`loop.state`) — отдельные переменные, на которые из дочернего скоупа не повлиять.

Можно сделать обёртку (`StateHolder`), которая держит state внутри, и все смотрят через `holder.state`. Но это усложнение архитектуры.

### Правильное решение — `state.reset()`

Один объект `SimulationState`, все ссылки на него живы. Просто **обнуляем содержимое**:

```python
def reset(self) -> None:
    with self._lock:
        self.virtual_time = config.SIM_START_TIME
        self.speed = config.DEFAULT_SPEED
        # пересоздаём машины и бригады
        self.machines.clear()
        # ... добавляем заново
        # очищаем все коллекции через .clear()
        # обнуляем счётчики через цикл по ключам
        # сбрасываем ID-генераторы
```

Важно: `reset()` **не трогает `self.running`** — это отдельная семантика (запущена/нет). Reset — только про данные.

### `reset` vs `restart` — разница в семантике

- **`reset`** — действие над **данными**: обнулить, вернуть к дефолту.
- **`restart`** — действие над **процессом**: остановить и запустить заново.

Поэтому:
- `state.reset()` — у state нет процесса, есть только данные.
- `loop.restart()` — у loop есть процесс (поток), его можно перезапустить.

### `loop.restart()` — простой вариант

```python
def restart(self) -> None:
    if self.state.virtual_time == config.SIM_START_TIME:
        return    # уже в начальном состоянии — ничего не делаем
    self.state.reset()
```

Если симуляция работала — после reset виртуальное время = SIM_START_TIME, данные обнулены, `running` остаётся True → поток на следующем тике начинает с чистого листа.

### `thread.join(timeout=N)`

Не убивает поток. **Ждёт** его естественного завершения до N секунд:
1. Если поток завершился раньше → продолжаем сразу.
2. Если за N секунд не завершился → продолжаем без ожидания (поток остаётся жить).

Поток завершается сам, когда внутри `_run` условие `while not self._stop_event.is_set()` станет ложным.

---

## 19. Архитектурный TODO (на потом)

При `restart` нужно также сбрасывать данные в БД:
- `active_batches` → очистить (production)
- `open_work_orders` → очистить (maintenance)
- `machine_status` → вернуть к `idle` + начальной дате (equipment)

Подход: добавить эндпоинты `POST /reset` в каждый из трёх сервисов, в `FactoryClient` метод `reset_all()`, вызывать в `loop.restart()`. Сейчас разделение чистое — каждая таблица принадлежит одному сервису. Если появятся shared tables — обсудить отдельно (вариант: выделить новый сервис-владелец).

Не реализовано сегодня — оставлено на следующую сессию.

---

## Итог дня

- Цикл симулятора теперь падает при ошибке (через `raise from`).
- Разобрались с потоками, RLock, потокобезопасностью.
- Глубоко прошли по `@dataclass`, `field()`, аннотациям типов.
- Изучили структуру `SimulationState` и роль каждой очереди.
- Поняли логику QualitySubsystem и FurnaceLoad.
- Починили Grafana через init-контейнер для прав.
- Подключили PostgreSQL к Grafana, написали базовые запросы.
- Унифицировали `snapshot()` через `asdict()`.
- Разобрали OrdersSubsystem: `weighted_choice`, `random.choices`, `_`, `random.uniform`, `min`.
- Поняли поведение сенсоров на высоких скоростях (данные не буферизуются).
- Переделали кнопки WebUI: Start / Stop / Restart.
- Поймали и поняли проблему браузерного кэша.
- Глубоко разобрали ссылочную семантику Python, реализовали `state.reset()` и `loop.restart()`.
