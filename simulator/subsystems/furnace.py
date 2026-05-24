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
            # печь свободна → попробуем сформировать новую загрузку
            if machine.state in ("idle", "cooldown"):
                if machine.state == "cooldown":
                    # cooldown печи не нужен, она всегда idle
                    machine.state = "idle"
                    machine.state_changed_at = now
                new_load = self._try_start_load(machine, now)
                if new_load is not None:
                    self.state.furnace_loads[machine.machine_id] = new_load
                    machine.state = "running"
                    machine.state_changed_at = now
                    machine.current_batch_id = None  # печь работает с loaded
                    self.client.state_change(
                        machine, old_state="idle", new_state="furnace_loading",
                        event_time=now,
                        details={"load_id": new_load.load_id,
                                 "product_codes": ",".join(new_load.product_codes),
                                 "total_parts": new_load.total_parts},
                    )
        else:
            # печь работает → двигаем фазы
            self._advance_phase(machine, load, now)
            self._maybe_emit_sensor(machine, load, now)

    # ─── алгоритм формирования загрузки ───────────────────────
    def _try_start_load(self, machine, now: datetime) -> FurnaceLoad | None:
        queue = self.state.queues["waiting_furnace"]
        if not queue:
            return None

        # Пересортировка по приоритету: rush → urgent → normal.
        # Внутри одного приоритета FIFO сохраняется (sorted стабильна).
        sorted_batches = sorted(queue, key=lambda b: config.PRIORITY_ORDER[b.priority])
        queue.clear()
        queue.extend(sorted_batches)

        head_batch = queue[0]

        # Отбираем подряд партии любого product_code до заполнения вместимости.
        # Смешивание разрешено: цикл печи (нагрев/цементация/закалка) одинаков
        # для всех типоразмеров (см. конспект 2026-05-23, секция о печи).
        selected = []
        total = 0
        for batch in list(queue):
            if total + batch.quantity > config.FURNACE_CAPACITY_PARTS:
                break
            selected.append(batch)
            total += batch.quantity

        if not selected:
            return None

        # условия старта
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

        # вынимаем выбранные партии из очереди
        for b in selected:
            queue.remove(b)
            b.stage = "heat_treatment"
            b.current_machine_id = machine.machine_id
            b.stage_started_at = now

        # Уникальные product_code в загрузке (могут смешиваться).
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

        # batch_move для каждой партии: from "waiting_furnace" to "heat_treatment"
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
        next_phase = config.FURNACE_NEXT_PHASE[old_phase]
        load.phase = next_phase
        load.phase_started_at = now
        load.last_sensor_sent_at = None

        if next_phase == "empty":
            # Выгрузка завершена. Промежуточный state_change "furnace_unloading→furnace_empty"
            # не шлём — реальный переход "furnace_unloading→idle" сделает _unload().
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
            self.client.batch_completion(
                batch, work_center="heat_treatment",
                actual_quantity=batch.quantity,
                defect_count=batch.fails_count,
                duration_sec=duration,
                event_time=now,
            )
            # после HT обязательно идёт grinding
            batch.stage = "waiting_grinding"
            self.client.batch_move(batch, from_center="heat_treatment",
                                   to_center="grinding", event_time=now)
            batch.current_machine_id = None
            self.state.queues["queue_grinding"].append(batch)
            # spot-check после heat_treatment
            if "heat_treatment" not in batch.spot_checked_at:
                self.state.pending_spot_checks.append((batch.batch_id, "heat_treatment"))

        # печь освобождается
        self.state.furnace_loads.pop(machine.machine_id, None)
        machine.state = "idle"
        machine.state_changed_at = now
        self.client.state_change(machine, old_state="furnace_unloading",
                                 new_state="idle", event_time=now)
        logger.info("furnace %s unloaded %s (%d parts)",
                    machine.machine_id, load.load_id, load.total_parts)

    # ─── сенсорика по фазам ──────────────────────────────────
    def _maybe_emit_sensor(self, machine, load: FurnaceLoad, now: datetime) -> None:
        # Догоняем все sensor_readings, пропущенные за тик.
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