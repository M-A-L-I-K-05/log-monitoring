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

Печные сценарии (trigger_phase-логика):
- Каждый сценарий привязан к конкретной фазе (trigger_phase из конфига).
- Когда печь входит в trigger_phase при активном сценарии:
  все детали текущей загрузки немедленно помечаются scenario_marked_indices.
- Когда trigger_phase заканчивается:
  загрузка ВЫБРАСЫВАЕТСЯ на измерение (не завершает цикл), печь → обслуживание.
  Quality видит пометки → все детали fail.
- Если сценарий запущен до trigger_phase — ждёт входа в неё.
- Если запущен после trigger_phase — не захватывает текущую загрузку.
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
            # Пометка деталей при входе в trigger-фазу сценария
            if machine.active_scenario_id:
                trigger = self._get_trigger_phase(machine.active_scenario_id)
                if trigger and load.phase == trigger and not load.scenario_phase_applied:
                    self._mark_scenario_parts(machine, load)
            self._maybe_emit_sensor(machine, load, now)

    # ─── алгоритм формирования загрузки ───────────────────────
    def _try_start_load(self, machine, now: datetime) -> FurnaceLoad | None:
        # Не запускаем новую загрузку, пока сценарий ожидает обслуживания.
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

        # Сценарий завершил trigger-фазу → выбрасываем загрузку на измерение.
        if machine.active_scenario_id:
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

        # Если на печи был активен сценарий — авто-завершаем (он не захватил
        # trigger_phase, иначе был бы eject раньше).
        sid = machine.active_scenario_id
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
                             "wo_duration_min": duration_min},
                )
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

    def _eject_scenario_load(self, machine, load: FurnaceLoad, now: datetime) -> None:
        """Trigger-фаза завершена при активном сценарии: выбрасываем загрузку
        на измерение (все детали уже помечены), печь → обслуживание."""
        sid = machine.active_scenario_id

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
        machine.current_batch_id = None

        self.client.state_change(
            machine, old_state=f"furnace_{load.phase}",
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
                             "ejected_batches": len(load.batch_ids),
                             "trigger_phase": load.phase,
                             "wo_duration_min": duration_min},
                )

        logger.info("furnace %s: ejected load %s (%d batches, trigger_phase=%s, scenario=%s)",
                    machine.machine_id, load.load_id, len(load.batch_ids), load.phase, sid)

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
        result = {}
        for name, (mean, std, _unit) in profile.items():
            value = random.gauss(mean, std)
            modifier = machine.anomaly_modifier.get(name)
            if modifier is not None:
                value *= modifier
            result[name] = round(value, 4)
        return result
