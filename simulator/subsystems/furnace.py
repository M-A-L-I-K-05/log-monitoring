"""FurnaceSubsystem: batch-логика печи.

Печь обрабатывает партии группами:
- одна загрузка ≤ 80 деталей
- смешивание product_code разрешено (цикл печи одинаков для всех типоразмеров)
- 6 стадий: loading → heating → carburizing → quenching → tempering → unloading
- ~8 часов виртуального времени на полный цикл

Алгоритм выбора следующей загрузки:
1. Сортируем waiting_furnace по приоритету (rush → urgent → normal), FIFO внутри
2. Отбираем партии подряд (любого product_code) пока не заполнится 80
3. Запускаем загрузку если выполнено любое из:
   - заполнение ≥ 60%
   - голова очереди ждёт ≥ 30 виртуальных минут
   - среди отобранных есть rush

Печные сценарии (модификатор применяется ТОЛЬКО в trigger_phase — phase-lock):

GRADUAL (under/over_carburizing, trigger_phase=carburizing):
- Дрейф растёт по времени фазы carburizing, шагами каждые FURNACE_DRIFT_STEP_MIN
  минут (а не одной ступенью на этап). За фазу прибавляет
  FURNACE_GRADUAL_DRIFT_PER_LOAD[severity] и сохраняется между загрузками.
- drift >= scrap_threshold (0.85): все детали загрузки помечаются (отбраковка).
- drift >= stop_threshold (1.0): загрузка ВЫБРАСЫВАЕТСЯ, печь → обслуживание.
  Остаток 0.15 укладывается в фазу — печь не успевает сменить этап.
- Загрузки с drift < scrap проходят цикл нормально (зона раннего предупреждения).

STEP (quench_distortion, trigger_phase=quenching):
- При входе в quenching разыгрывается исход: «поймали вовремя» (CAUGHT_PROB) —
  аномалия саморазрешается в [3, CATCH_MIN) мин, таймер закалки сбрасывается,
  брака нет; иначе отбраковка в [CATCH_MIN, SCRAP_MAX] мин — все детали fail,
  загрузка выбрасывается, печь → обслуживание.

Выброшенные по сценарию партии ЗАМОРАЖИВАЮТСЯ (frozen_furnace_batches) и
ставятся на измерение только ПОСЛЕ завершения ремонта печи (maintenance.py).
"""
import logging
import random
from datetime import datetime, timedelta

import config
from domain.furnace_load import FurnaceLoad

logger = logging.getLogger("simulator.furnace")


class FurnaceSubsystem:
    name = "furnace"

    def __init__(self, state, client):
        self.state = state
        self.client = client

    def tick(self, now: datetime) -> None:
        with self.state.lock:
            for machine in self.state.machines.values():
                if machine.machine_type != "furnace":
                    continue
                self._tick_furnace(machine, now)

    # ─── per-furnace ─────────────────────────────────────────
    def _tick_furnace(self, machine, now: datetime) -> None:
        load = self.state.furnace_loads.get(machine.machine_id)

        if load is None:
            if machine.state in ("idle", "cooldown"):
                if machine.state == "cooldown":
                    machine.state = "idle"
                    machine.state_changed_at = now
                new_load = self._try_start_load(machine, now)
                if new_load is not None:
                    self.state.furnace_loads[machine.machine_id] = new_load
                    machine.state = "running"
                    machine.state_changed_at = now
                    machine.current_batch_id = None
                    self.client.state_change(
                        machine, old_state="idle", new_state="furnace_loading",
                        event_time=now,
                        details={"load_id": new_load.load_id,
                                 "product_codes": ",".join(new_load.product_codes),
                                 "total_parts": new_load.total_parts},
                    )
        else:
            self._advance_phase(machine, load, now)
            # После advance_phase загрузки может не быть (eject или unload)
            load = self.state.furnace_loads.get(machine.machine_id)
            if load is None:
                return
            # Сценарная логика: дрейф (gradual) / окно поимки (step).
            if machine.active_scenario_id:
                meta = self.state.scenarios_registry.get(machine.active_scenario_id)
                if meta and meta.get("status") == "active":
                    if meta.get("mode") == "gradual":
                        self._handle_gradual(machine, load, meta, now)
                    else:
                        self._handle_step(machine, load, meta, now)
                    # сценарий мог выбросить загрузку
                    load = self.state.furnace_loads.get(machine.machine_id)
                    if load is None:
                        return
            self._maybe_emit_sensor(machine, load, now)

    # ─── алгоритм формирования загрузки ───────────────────────
    def _try_start_load(self, machine, now: datetime) -> FurnaceLoad | None:
        # Не запускаем новую загрузку, пока печь ждёт/проходит ремонт по сценарию:
        #  - есть замороженные сценарием партии (ждут измерения после WO),
        #  - есть незакрытый наряд на этой печи,
        #  - в очереди на ремонт есть запись по этой печи,
        #  - активный сценарий уже завершился и ждёт обслуживания.
        if self.state.frozen_furnace_batches.get(machine.machine_id):
            return None
        if any(wo.machine_id == machine.machine_id
               for wo in self.state.work_orders.values()):
            return None
        if any(mid == machine.machine_id
               for mid, _t, _d in self.state.pending_scenario_wos):
            return None
        if machine.active_scenario_id:
            meta = self.state.scenarios_registry.get(machine.active_scenario_id)
            if meta and meta.get("status") in ("auto_completed", "stopped"):
                return None

        queue = self.state.queues["waiting_furnace"]
        if not queue:
            return None

        # Пересортировка по приоритету: rush → urgent → normal.
        sorted_batches = sorted(queue, key=lambda b: config.PRIORITY_ORDER[b.priority])
        queue.clear()
        queue.extend(sorted_batches)

        head_batch = queue[0]

        selected = []
        total = 0
        for batch in list(queue):
            # Печь грузится годными деталями: выбывшие в браке слотов не занимают.
            if total + batch.effective_quantity > config.FURNACE_CAPACITY_PARTS:
                break
            selected.append(batch)
            total += batch.effective_quantity

        if not selected:
            return None

        fill_ratio = total / config.FURNACE_CAPACITY_PARTS
        head_wait_min = ((now - head_batch.stage_started_at).total_seconds() / 60.0
                         if head_batch.stage_started_at else 0)
        has_rush = any(b.priority == "rush" for b in selected)

        can_start = (
            fill_ratio >= config.FURNACE_MIN_FILL_RATIO
            or head_wait_min >= config.FURNACE_MAX_WAIT_MIN
            or has_rush
        )
        if not can_start:
            return None

        for b in selected:
            queue.remove(b)
            b.stage = "heat_treatment"
            b.current_machine_id = machine.machine_id
            b.stage_started_at = now
            # Пометку деталей НЕ делаем здесь: она произойдёт при входе
            # в trigger_phase сценария (см. _tick_furnace → _mark_scenario_parts).

        unique_codes = sorted({b.product_code for b in selected})

        load = FurnaceLoad(
            load_id=self.state.next_load_id(),
            machine_id=machine.machine_id,
            product_codes=unique_codes,
            batch_ids=[b.batch_id for b in selected],
            total_parts=total,
            phase="loading",
            phase_started_at=now,
        )

        for b in selected:
            self.client.batch_move(b, from_center="waiting_furnace",
                                   to_center="heat_treatment", event_time=now)

        logger.info("furnace %s started load %s (codes=%s, %d parts, %d batches)",
                    machine.machine_id, load.load_id, unique_codes, total, len(selected))
        return load

    # ─── переходы фаз ────────────────────────────────────────
    def _advance_phase(self, machine, load: FurnaceLoad, now: datetime) -> None:
        duration_sec = config.FURNACE_PHASE_DURATIONS_SEC[load.phase]
        elapsed = (now - load.phase_started_at).total_seconds()
        if elapsed < duration_sec:
            return

        old_phase = load.phase

        # Fallback: если детали уже помечены (дрейф/время пересекли порог
        # отбраковки), но загрузку ещё не выбросили, а trigger-фаза кончается —
        # выбрасываем сейчас. В норме выброс происходит раньше, в _handle_*.
        # Непомеченные загрузки (drift<scrap / поймали вовремя) идут дальше.
        if machine.active_scenario_id and load.scenario_phase_applied:
            trigger = self._get_trigger_phase(machine.active_scenario_id)
            if trigger and old_phase == trigger:
                self._eject_scenario_load(machine, load, now)
                return

        next_phase = config.FURNACE_NEXT_PHASE[old_phase]
        load.phase = next_phase
        load.phase_started_at = now
        load.last_sensor_sent_at = None

        if next_phase == "empty":
            self._unload(machine, load, now)
            return

        self.client.state_change(
            machine, old_state=f"furnace_{old_phase}",
            new_state=f"furnace_{next_phase}", event_time=now,
            details={"load_id": load.load_id},
        )

    def _unload(self, machine, load: FurnaceLoad, now: datetime) -> None:
        for batch_id in load.batch_ids:
            batch = self.state.batches.get(batch_id)
            if batch is None:
                continue
            duration = (now - batch.stage_started_at).total_seconds() if batch.stage_started_at else 0.0
            actual_qty = batch.quantity - len(batch.failed_indices)
            self.client.batch_completion(
                batch, work_center="heat_treatment",
                actual_quantity=actual_qty,
                defect_count=batch.fails_count,
                duration_sec=duration,
                event_time=now,
            )
            batch.last_processed_stage = "heat_treatment"
            batch.last_processed_machine_id = machine.machine_id
            batch.stage = "queue_measurement"
            batch.current_machine_id = None
            batch.quality_hold = True
            self.client.batch_move(batch, from_center="heat_treatment",
                                   to_center="measurement", event_time=now)
            self.state.queues["queue_measurement"].append(batch)

        self.state.furnace_loads.pop(machine.machine_id, None)
        machine.state = "idle"
        machine.state_changed_at = now
        self.client.state_change(machine, old_state="furnace_unloading",
                                 new_state="idle", event_time=now)

        # Сценарий НЕ завершаем: gradual-сценарий накапливает дрейф через
        # несколько загрузок (под/перецементация развивается медленно) и
        # завершится сам, когда дрейф дойдёт до stop_threshold и загрузка будет
        # выброшена. Эта загрузка прошла цикл нормально (drift < scrap) —
        # детали не помечены, идут на измерение как годные (ранняя стадия).
        logger.info("furnace %s unloaded %s (%d parts)",
                    machine.machine_id, load.load_id, load.total_parts)

    # ─── сценарный выброс загрузки ────────────────────────────
    def _get_trigger_phase(self, scenario_id: str) -> str | None:
        meta = self.state.scenarios_registry.get(scenario_id)
        if meta:
            return meta.get("trigger_phase")
        return None

    def _mark_scenario_parts(self, machine, load: FurnaceLoad) -> None:
        """Помечает все живые детали загрузки для сценарной инспекции."""
        sid = machine.active_scenario_id
        if not sid:
            return
        for batch_id in load.batch_ids:
            batch = self.state.batches.get(batch_id)
            if batch is None:
                continue
            for idx in range(1, batch.quantity + 1):
                if idx not in batch.failed_indices:
                    batch.scenario_marked_indices[idx] = sid
        load.scenario_phase_applied = True
        logger.info("furnace %s: marked all parts in load %s for scenario %s",
                    machine.machine_id, load.load_id, sid)

    # ─── GRADUAL: накопление дрейфа в carburizing ─────────────
    def _handle_gradual(self, machine, load: FurnaceLoad, meta: dict,
                        now: datetime) -> None:
        """Дрейф растёт шагами каждые FURNACE_DRIFT_STEP_MIN минут, пока загрузка
        в trigger-фазе. drift>=scrap → пометка деталей; drift>=stop → выброс.
        Дрейф сохраняется на станке между загрузками."""
        trigger = meta.get("trigger_phase")
        if trigger is None or load.phase != trigger:
            return   # дрейф растёт только в trigger-фазе (carburizing)

        if load.last_drift_step_at is None:
            # Начинаем отсчёт шагов с момента первого тика в фазе (не задним
            # числом от phase_started_at, если сценарий запущен посреди фазы).
            load.last_drift_step_at = now
            return

        step_delta = timedelta(minutes=config.FURNACE_DRIFT_STEP_MIN)
        severity = meta.get("severity", config.DEFAULT_SEVERITY)
        per_load = config.FURNACE_GRADUAL_DRIFT_PER_LOAD.get(severity, 0.4)
        phase_min = config.FURNACE_PHASE_DURATIONS_SEC[trigger] / 60.0
        n_steps = max(1.0, phase_min / config.FURNACE_DRIFT_STEP_MIN)
        increment = per_load / n_steps
        scrap = meta.get("scrap_threshold", config.DRIFT_SCRAP_THRESHOLD)
        stop = meta.get("stop_threshold", config.DRIFT_STOP_THRESHOLD)
        sensors_final = meta.get("sensors_final", {})

        next_at = load.last_drift_step_at + step_delta
        while next_at <= now:
            machine.drift_progress = min(1.0, machine.drift_progress + increment)
            load.last_drift_step_at = next_at
            # модификаторы под текущий дрейф
            for sensor, target in sensors_final.items():
                machine.anomaly_modifier[sensor] = 1.0 + (target - 1.0) * machine.drift_progress
            # пересекли границу отбраковки → помечаем всю загрузку
            if machine.drift_progress >= scrap and not load.scenario_phase_applied:
                self._mark_scenario_parts(machine, load)
            # дошли до stop → выброс (внутри той же фазы)
            if machine.drift_progress >= stop:
                self._eject_scenario_load(machine, load, next_at)
                return
            next_at += step_delta

    # ─── STEP: гарантированный брак в quenching ──────────────
    def _handle_step(self, machine, load: FurnaceLoad, meta: dict,
                     now: datetime) -> None:
        """quench_distortion: аномалия в quenching. Вероятности исхода НЕТ —
        без внешней поимки партия ГАРАНТИРОВАННО уходит в брак. Отбраковка и
        выброс наступают в [CATCH_MIN, SCRAP_MAX_MIN] мин от начала закалки.
        «Поимка» в первые CATCH_MIN минут возможна только внешним вызовом
        catch_step_scenario (см. ниже)."""
        trigger = meta.get("trigger_phase")
        if trigger is None or load.phase != trigger or load.phase_started_at is None:
            return

        if load.scenario_resolve_at is None:
            load.scenario_outcome = "scrap"
            load.scenario_resolve_at = load.phase_started_at + timedelta(
                minutes=random.uniform(config.FURNACE_STEP_CATCH_MIN,
                                       config.FURNACE_STEP_SCRAP_MAX_MIN))

        if now < load.scenario_resolve_at:
            return

        if not load.scenario_phase_applied:
            self._mark_scenario_parts(machine, load)
        self._eject_scenario_load(machine, load, load.scenario_resolve_at)

    def catch_step_scenario(self, machine_id: str) -> bool:
        """Внешняя «поимка» step-сценария печи в окне [0, CATCH_MIN) фазы
        quenching. Задел под ML/диагностику: вызывается endpoint'ом сервиса
        (equipment/maintenance), когда модель успевает среагировать. Без этого
        вызова автоматической поимки НЕТ — партия уходит в брак.

        Возвращает True, если поимка удалась (окно ещё открыто, брака нет,
        таймер закалки сброшен), иначе False."""
        with self.state.lock:
            machine = self.state.machines.get(machine_id)
            if machine is None or not machine.active_scenario_id:
                return False
            load = self.state.furnace_loads.get(machine_id)
            meta = self.state.scenarios_registry.get(machine.active_scenario_id)
            if load is None or meta is None or meta.get("mode") != "step":
                return False
            trigger = meta.get("trigger_phase")
            if trigger is None or load.phase != trigger or load.phase_started_at is None:
                return False
            elapsed_min = (self.state.virtual_time
                           - load.phase_started_at).total_seconds() / 60.0
            if elapsed_min >= config.FURNACE_STEP_CATCH_MIN or load.scenario_phase_applied:
                return False   # окно закрылось или партия уже помечена в брак
            self._resolve_step_caught(machine, load, meta, self.state.virtual_time)
            return True

    def _resolve_step_caught(self, machine, load: FurnaceLoad, meta: dict,
                             now: datetime) -> None:
        """Поймали вовремя: брака нет, таймер закалки сбрасывается, сценарий
        завершается без обслуживания. Закалка идёт заново с нуля."""
        sid = machine.active_scenario_id
        # сброс таймера фазы quenching — закалка начинается заново
        load.phase_started_at = now
        load.last_sensor_sent_at = None
        load.scenario_outcome = None
        load.scenario_resolve_at = None
        # снимаем модификаторы сразу (фаза quench продолжается уже в норме)
        for key in meta.get("sensors", {}):
            machine.anomaly_modifier.pop(key, None)
        meta["status"] = "stopped"
        meta["ended_at"] = now
        machine.active_scenario_id = None
        self.client.state_change(
            machine, old_state=f"furnace_{load.phase}",
            new_state=f"furnace_{load.phase}", event_time=now,
            details={"load_id": load.load_id,
                     "reason": "scenario_caught_in_time",
                     "scenario_id": sid},
        )
        if sid:
            self.client.scenario_event(
                event="auto_completed",
                scenario_id=sid,
                machine_id=machine.machine_id,
                scenario_type=meta.get("scenario_type", "scenario"),
                severity=meta.get("severity"),
                parts_limit=meta.get("parts_limit_effective", meta.get("parts_limit")),
                event_time=now,
                details={"outcome": "caught", "furnace_load_id": load.load_id},
            )
        logger.info("furnace %s: scenario %s caught in time on load %s (no scrap)",
                    machine.machine_id, sid, load.load_id)

    # ─── сценарный выброс загрузки (заморозка до конца ремонта) ─
    def _eject_scenario_load(self, machine, load: FurnaceLoad,
                             now: datetime) -> None:
        """Загрузка ушла в брак: замораживаем все партии (frozen_furnace_batches),
        печь → обслуживание. На измерение партии попадут только ПОСЛЕ ремонта
        (maintenance._complete_wo). Время ремонта покрывает остывание печи."""
        sid = machine.active_scenario_id

        held: list[str] = []
        for batch_id in load.batch_ids:
            batch = self.state.batches.get(batch_id)
            if batch is None:
                continue
            batch.is_frozen = True
            batch.frozen_reason = "furnace_scenario"
            batch.current_machine_id = machine.machine_id
            # stage остаётся heat_treatment — партия физически в печи, ждёт ремонта
            held.append(batch_id)
        self.state.frozen_furnace_batches[machine.machine_id] = held

        ejected_phase = load.phase
        self.state.furnace_loads.pop(machine.machine_id, None)
        machine.state = "idle"
        machine.state_changed_at = now
        machine.current_batch_id = None

        self.client.state_change(
            machine, old_state=f"furnace_{ejected_phase}",
            new_state="idle", event_time=now,
            details={"load_id": load.load_id,
                     "reason": "scenario_ejection",
                     "scenario_id": sid},
        )

        if sid:
            meta = self.state.scenarios_registry.get(sid)
            if meta:
                duration_min = meta.get("wo_duration_min", 120.0)
                scenario_type = meta.get("scenario_type", "scenario")
                self.state.pending_scenario_wos.append(
                    (machine.machine_id, scenario_type, float(duration_min))
                )
                meta["status"] = "auto_completed"
                meta["ended_at"] = now
                self.client.scenario_event(
                    event="auto_completed",
                    scenario_id=sid,
                    machine_id=machine.machine_id,
                    scenario_type=scenario_type,
                    severity=meta.get("severity"),
                    parts_limit=meta.get("parts_limit_effective", meta.get("parts_limit")),
                    event_time=now,
                    details={"furnace_load_id": load.load_id,
                             "ejected_batches": len(held),
                             "trigger_phase": ejected_phase,
                             "drift_progress": round(machine.drift_progress, 4),
                             "wo_duration_min": duration_min},
                )

        logger.info("furnace %s: ejected load %s (%d batches, phase=%s, scenario=%s) — frozen until WO",
                    machine.machine_id, load.load_id, len(held), ejected_phase, sid)

    # ─── сенсорика по фазам ──────────────────────────────────
    def _maybe_emit_sensor(self, machine, load: FurnaceLoad, now: datetime) -> None:
        sensor_step = timedelta(seconds=config.SENSOR_INTERVAL_SEC)
        if load.last_sensor_sent_at is None:
            load.last_sensor_sent_at = load.phase_started_at
        next_sensor_at = load.last_sensor_sent_at + sensor_step
        while next_sensor_at <= now:
            readings = self._generate_furnace_readings(machine, load.phase)
            if readings:
                self.client.sensor_reading(machine, readings, event_time=next_sensor_at)
            load.last_sensor_sent_at = next_sensor_at
            next_sensor_at += sensor_step

    def _generate_furnace_readings(self, machine, phase: str) -> dict[str, float]:
        profile = config.FURNACE_SENSOR_PROFILES.get(phase, {})
        # Phase-lock: модификатор сценария действует ТОЛЬКО в его trigger-фазе.
        # В остальных фазах сенсоры в норме, даже если ключ есть в anomaly_modifier
        # (например, carbon_potential присутствует и в heating, и в carburizing).
        apply_modifier = False
        if machine.active_scenario_id:
            trigger = self._get_trigger_phase(machine.active_scenario_id)
            apply_modifier = (trigger is not None and phase == trigger)
        result = {}
        for name, (mean, std, _unit) in profile.items():
            value = config.bounded_gauss(mean, std)
            if apply_modifier:
                modifier = machine.anomaly_modifier.get(name)
                if modifier is not None:
                    value *= modifier
            result[name] = round(value, 4)
        return result
