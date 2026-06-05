# Day 9 — 2026-05-28 · Хендофф для Claude Code

> Этот файл — не учебный конспект (как Day 1–8), а **брифинг для следующего экземпляра Claude**, который продолжит работу с этим репозиторием на ПК. Цель: чтобы ты за 5 минут понял, что это за проект, в каком состоянии код, что изменилось за эту сессию и какие инварианты нельзя ломать. Работа велась с ноутбука перед предзащитой; теперь продолжение на ПК.

---

## 0. TL;DR что произошло за сессию

Симулятор завода переведён с «партия как неделимое число» на **поштучную модель с выбытием брака**, а измерение на M-GMM — с «один таймер на партию» на **поштучное инкрементальное измерение** с отдельной панелью в WebUI. Плюс почищена семантика счётчиков, исправлен баг застоя станка после ремонта, добавлена очистка Loki на рестарте.

Все изменения — в каталоге `simulator/` (ядро симулятора и WebUI). Backend-сервисы (`services/*`), схема БД (`postgres/init/init.sql`) и Grafana **не трогались**.

---

## 1. Что это за проект (ориентация)

ВКР: мониторинг логов и предиктивное обслуживание имитируемого завода шестерён.

Стек (`docker-compose.yml`): **simulator** (FastAPI, генерит события) → 4 backend-сервиса **equipment / production / quality / maintenance** (принимают события, пишут в БД + логируют) → **Postgres** (состояние) + **Loki/Promtail** (логи) + **Grafana** (дашборды).

Симулятор (`simulator/`):
- `main.py` — FastAPI, сборка подсистем, эндпоинты управления (`/start /stop /restart /speed /scenarios/* /orders/*`).
- `loop.py` — главный цикл: на каждом тике `state.advance_time()` → `subsystem.tick(now)` по очереди → `client.flush()`.
- `state.py` — `SimulationState`: всё состояние + виртуальные часы + `snapshot()` для `/status`.
- `client.py` — HTTP-клиент к 4 сервисам; sensor/cycle/measurement буферизуются и шлются пачкой во `flush()`.
- `domain/` — `Machine`, `Batch`, `Order`, `WorkOrder`, `FurnaceLoad`.
- `subsystems/` — `orders`, `production`, `equipment`, `furnace`, `quality`, `maintenance`, `scenarios`.
- `webui/` — `index.html` + `app.js` (поллит `/status` раз в секунду) + `style.css`. **Статика, монтируется в контейнер — правки видны после Ctrl+Shift+R, без пересборки.**

Маршрут партии: `turning → hobbing → shaving → heat_treatment(печь) → grinding → inspection`. **После каждого** этапа партия идёт на измерение на M-GMM (станки типа `inspection`, их 2: `M-GMM-01/02`). Печь — отдельная подсистема (`furnace`), остальные обрабатывающие станки — `equipment`.

Запуск: `docker compose up -d --build`; WebUI на `http://localhost:8005`; Grafana `:3000`; Loki API `:3100`; Postgres БД `factory`, юзер `admin`/`secret`. Первый запуск: WebUI → **Sync Fleet** (наполнить `machines`) → **Start**.

---

## 2. Исходное состояние на входе сессии (что было ДО)

- **Измерение партии — разом**: `equipment._tick_running_inspection` ждал один таймер `machine.measurement_total_sec`, затем клал задачу в `state.pending_measurements`; `quality.tick()` доставал её и `_measure_batch()` мерил все нужные детали одним вызовом в конце. `parts_done_in_stage` при измерении не двигался → в UI всегда `0/X`.
- **Брак не выбывал из потока**: обрабатывающие станки прогоняли полный `batch.quantity` на каждом этапе, включая уже забракованные детали. `failed_indices` влиял только на выборку измерения и на отчётный `actual_quantity`.
- **Счётчики**: `parts_pass`/`parts_fail` инкрементировались по каждой **измеренной** детали → `parts_pass` сильно занижен (меряется горстка).
- **WebUI**: бейдж `measurement hold` в столбце «Заморозка» (показывал `quality_hold`); прогресс `parts_done/quantity`; отдельной панели измерительных станков не было.
- **Печь**: уже была реализована логика `trigger_phase` (сценарий печи помечает детали при входе в свою фазу и выбрасывает загрузку при выходе) — из предыдущей сессии.

---

## 3. Что переделано — по темам (с файлами и функциями)

### A. Выбытие брака: модель «годного количества»
Идея: после отбраковки деталь физически выбывает; дальше обрабатывается, считается в прогрессе и занимает время **только годное количество** = `quantity − len(failed_indices)`.

- `domain/batch.py`: добавлены `@property effective_quantity` и метод `good_indices() -> list[int]` (отсортированные индексы 1..quantity без `failed_indices`).
- `subsystems/equipment.py::_tick_running_processing`: цикл идёт по `good = batch.good_indices()`, `eff_qty = len(good)`; `expected_done = min(elapsed//cycle_sec, eff_qty)`; завершение этапа при `parts_done_in_batch >= eff_qty`; пометка сценария ставится на **реальный** индекс `good[parts_done_in_batch-1]`.
- `subsystems/equipment.py`: `_scenario_limit_reached` и `_auto_complete_scenario` сравнивают с `batch.effective_quantity` (было `batch.quantity`).
- `subsystems/scenarios.py::_effective_limit`: остаток считается от `effective_quantity`.
- `subsystems/furnace.py::_try_start_load`: печь пакует загрузку по `effective_quantity` (брак не занимает слотов).
- `state.py`: `_batch_snapshot()` добавляет в каждую партию поле `good_quantity`.
- `webui/app.js`: прогресс и ключ сортировки используют `good_quantity ?? quantity`.

Инвариант: `failed_indices` меняется **только на измерении** (M-GMM), не во время обработки, поэтому `good_indices()` стабилен в пределах одного этапа.

### B. Поштучное измерение на M-GMM + отдельная панель
Измерение теперь идёт деталь-за-деталью по виртуальному времени, **управляется equipment'ом** (не quality по таймеру).

- `domain/machine.py`: **добавлены** `measurement_plan: list` (очередь деталей: `{idx, mode, scenario_id, source, force_pass}`), `measurement_done: int`, `measurement_total: int`. **Удалено** `measurement_total_sec`.
- `subsystems/quality.py`:
  - **Удалён** `_measure_batch()`; `tick()` теперь no-op (измерение больше не по таймеру).
  - **Добавлены**: `build_plan(batch, stage, gmm_id) -> list[dict]` (строит план: scenario поголовно / final 10% / verify 10% / spot 1 деталь; здесь же потребляется флаг `verify_next_batch_with_sample`); `spot_neighbors(batch, spot_idx)` (2 соседние при спот-фейле); `measure_plan_item(batch, item, stage, gmm_id, now) -> str` (меряет одну деталь через `_measure_part`).
  - Сохранены `_measure_part`, `_gen_value`, `_lookup_scenario`.
- `subsystems/equipment.py`:
  - `__init__(self, state, client, quality)` — теперь принимает ссылку на quality.
  - `_tick_setup`: при переходе инспекционного станка `setup→running` строит `machine.measurement_plan = quality.build_plan(...)`.
  - `_tick_running_inspection` **переписан**: каждые `INSPECTION_TIME_PER_PART_SEC[stage]` виртуальных секунд меряет одну деталь из плана (`quality.measure_plan_item`), инкрементит `measurement_done`. Если spot-деталь = fail → `plan.extend(quality.spot_neighbors(...))`, `measurement_total` растёт. Завершение, когда измерены все запланированные И прошло время на весь объём → `_route_after_measurement` + станок в `idle`, поля измерения сброшены. **`pending_measurements` больше не используется.**
- `subsystems/production.py`: ветка inspection в `_start_batch_on_machine` упрощена (только фиксирует `measuring_after_stage`, сбрасывает `measurement_*`); **удалён** метод `_count_parts_to_measure` (его роль взял `build_plan`).
- `main.py`: `quality_sub = QualitySubsystem(...)` создаётся первым и передаётся в `EquipmentSubsystem(state, client, quality_sub)`.
- `state.py`: `_inspection_station(m)` + секция `inspection_stations` в `snapshot()` (machine_id, state, batch_id, product_code, stage_after, parts_total, parts_done, mode).
- `webui/index.html`: новая `<section>` «Измерительные станки (M-GMM)» после «Загрузки печей».
- `webui/app.js`: рендер панели `inspection-stations` (карточка на станок: партия, типоразмер, этап, режим, `done/total`, прогресс-бар).
- `webui/style.css`: `.inspection-stations/.inspection-cell/.insp-*` + `.tag-badge/.tag-muted`.

Побочный плюс: синхронные `inspection_result`-POST'ы теперь размазаны по времени, а не одним всплеском под `state.lock` (раньше это подвешивало `/status`).

### C. Семантика счётчиков `parts_pass` / `parts_fail`
Должны отражать **шестерни**, а не строки измерений.

- `subsystems/quality.py::_measure_part`: убран инкремент `parts_pass` (больше не считаем измеренные-годные). `parts_fail += 1` остаётся — каждая забракованная шестерня считается один раз (она же добавляется в `failed_indices`).
- `subsystems/equipment.py::_route_after_measurement`: в **каждой** точке перехода партии в `done` (`batches_done += 1`) добавлено `parts_pass += batch.effective_quantity` — все неотбракованные шестерни, прошедшие весь маршрут, засчитываются как pass при завершении.

Итог: `parts_fail` = всего выбраковано шестерён; `parts_pass` = всего завершило маршрут годными. На партию каждая шестерня учитывается ровно раз.

### D. Баг застоя станка после ремонта (был предсуществующий)
- `subsystems/maintenance.py::_complete_wo`, ветка возобновления `maintenance→running`: после ремонта `parts_done_in_batch` сохранялся, но `state_changed_at = now`, из-за чего `expected_done = (now − state_changed_at)//cycle_sec` считался **с нуля** → станок простаивал ≈ `parts_done × cycle_sec`. Фикс: `state_changed_at = now − parts_done_in_batch × cycle_sec` (cycle_sec считается как в equipment: `CYCLE_TIME_SEC[type] × CYCLE_TIME_MULT_BY_PRODUCT[code][type]`), плюс `last_sensor_sent_at = now` (без всплеска сенсоров за период ТО). Добавлен импорт `timedelta`.

### E. WebUI: бирки вместо вводящих в заблуждение значений
- `webui/app.js` (рендер таблицы партий):
  - Убран бейдж `measurement hold` — `quality_hold` это **маршрутизация, не заморозка**; в столбце «Заморозка» теперь только реальные `is_frozen` (tool_wear / scenario).
  - Прогресс: в очередях (`pending`, `waiting_*`, `queue_measurement`, `queue_inspection`) → бирка **`queued`**; на `measurement` → **`measuring`**; на `inspection` → **`final QC`**; на обработке → реальное `N/good`.
  - Брак: на `measurement`/`inspection` → **`—`** (результат только по завершении); в остальных → число `fails_count`.
- Бирки делаются **на английском** (короткие статус-теги), при этом заголовки/подписи остаются русскими — это явное предпочтение пользователя.

### F. Очистка Loki на `/restart`
- `docker-compose.yml`: в сервис `simulator` смонтирован `/var/run/docker.sock`.
- `simulator/requirements.txt`: добавлен `docker==7.1.0`.
- `main.py`: `_clear_loki_async()` — в фоновом потоке (`threading.Thread(daemon=True)`) делает `rm -rf /loki/chunks …` в контейнере loki и `loki.restart()`; вызывается из эндпоинта `/restart`, чтобы HTTP-ответ не висел.

> Нюанс из этой сессии: если запросить Loki «через границу» такой очистки, можно поймать `failed to load chunk` (индекс ссылается на удалённые chunks). На свежих данных проблемы нет.

---

## 4. Функции/поля: добавлено / удалено / переименовано

| Файл | Добавлено | Удалено |
|---|---|---|
| `domain/batch.py` | `effective_quantity` (property), `good_indices()` | — |
| `domain/machine.py` | `measurement_plan`, `measurement_done`, `measurement_total` | `measurement_total_sec` |
| `subsystems/quality.py` | `build_plan()`, `spot_neighbors()`, `measure_plan_item()` | `_measure_batch()` |
| `subsystems/production.py` | — | `_count_parts_to_measure()` |
| `subsystems/equipment.py` | `quality` в `__init__`; перепис. `_tick_running_inspection`; план в `_tick_setup` | использование `pending_measurements` |
| `subsystems/maintenance.py` | сдвиг `state_changed_at` на возобновлении | — |
| `state.py` | `_batch_snapshot()`, `_inspection_station()`, секция `inspection_stations` | — |
| `main.py` | `quality_sub` проводка; `_clear_loki_async()` | — |

---

## 5. Инварианты и подводные камни (НЕ сломать)

1. **`quality_hold` — это маршрутный гейт, не заморозка.** Нельзя убирать из движка: без него партия уйдёт на следующий этап, не закончив измерение. В UI его просто не показываем.
2. **Измерение управляет equipment, не quality.** `quality.tick()` — no-op. План строится в `equipment._tick_setup`, исполняется в `_tick_running_inspection`. Не возвращай `pending_measurements`.
3. **`failed_indices` пополняется только в `quality._measure_part`** (на M-GMM). Поэтому `good_indices()`/`effective_quantity` стабильны во время обработки на станке.
4. **`parts_pass` считается ТОЛЬКО при `done`** (`parts_pass += effective_quantity`), `parts_fail` — при каждом скрапе. Не вернуть инкремент pass в `_measure_part` — иначе снова будут считаться только измеренные.
5. **Спот-фейл динамически растит план** (`measurement_total`), прогресс продолжается с текущей позиции — это ожидаемое поведение, не баг.
6. **Время этапа считается от `state_changed_at`** через `expected_done = elapsed//cycle_sec`. Любая пауза/возобновление станка с сохранённым `parts_done_in_batch` ОБЯЗАНА сдвигать `state_changed_at` назад (см. фикс D), иначе застой.
7. **Loki индексирует по реальному времени приёма; виртуальное `event_time` — внутри JSON лога.** Для запросов фильтруй по `event_time` в коде, а окно запроса бери с запасом. Не используй `| json | label=…` на больших выборках (упирается в лимит серий) — бери line-filter `|= "..."` и парси JSON сам.
8. **WebUI — статика.** Проверять только после Hard Reload (Ctrl+Shift+R).

---

## 6. Как проверить, что всё живо

```bash
# симулятор отвечает, секция панели на месте
curl -s localhost:8005/status | python3 -c "import sys,json;d=json.load(sys.stdin);print('inspection_stations' in d, len(d['active_batches']))"
# measurements пишутся
docker exec postgres psql -U admin -d factory -t -c "SELECT count(*) FROM measurements;"
# ошибок рефактора нет
docker logs simulator 2>&1 | grep -iE "error|traceback" | tail
```
Проверка поштучного измерения: запустить ×100, дождаться партии на M-GMM — в панели `done/total` должен **расти инкрементом**. Проверка выбытия брака: партия с `fails>0` на обработке — счётчик идёт до `good_quantity`, не до `quantity`.

---

## 7. Не сделано / открытые задачи

- **3-й M-GMM** — пользователь добавит сам (одна строка в `config.MACHINES` + Sync Fleet). Узкое место: 2 станка на измерение после всех 6 этапов.
- **`heat_treatment` в таблице партий** показывает `0/X` (печь не даёт пер-парт прогресс) — можно пометить биркой `in furnace`, но пока оставлено (у печи своя панель «Загрузки печей»).
- **`duplicate key … insert_batch`** в логах production — гонка при многократном `/restart` (сброс счётчика партий vs асинхронный TRUNCATE `active_batches`). Не связано с измерением; на одиночном рестарте не воспроизводится.

---

## 8. Память (auto-memory)

В `~/.claude/projects/.../memory/` лежат feedback-записи, релевантные проекту:
- проверять данные симулятора постфактум из Loki по виртуальному окну, не ловить вживую;
- короткие статус-бейджи в WebUI — на английском, длинные подписи — на русском.
