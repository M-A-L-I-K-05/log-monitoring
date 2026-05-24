"""EquipmentSubsystem: циклы обычных станков (всё кроме печи).

Поведение по состояниям:
- setup → running (по таймеру SETUP_TIME_SEC)
- running:
    * каждые SENSOR_INTERVAL_SEC — sensor_reading
    * каждые CYCLE_TIME_SEC[type] — обработана 1 деталь (cycle_completion + tool_wear++)
    * если tool_wear ≥ TOOL_WEAR_TRIGGER — alarm + запрос ТО
    * если parts_done == quantity → state=cooldown
- cooldown → idle (по таймеру COOLDOWN_TIME_SEC):
    * партия отправляется в очередь следующего участка
    * для инспекции — партия становится done и удаляется из активных

Инспекционные машины: cycle_completion на каждую деталь. Для деталей,
попавших в 10% выборку — отдельно регистрируется задача на quality.
"""
import random
from datetime import datetime, timedelta

import config


class EquipmentSubsystem:
    name = "equipment"

    def __init__(self, state, client):
        self.state = state
        self.client = client

    def tick(self, now: datetime) -> None:
        with self.state.lock:
            for machine in list(self.state.machines.values()):
                if machine.machine_type == "furnace":
                    continue   # печь — отдельная подсистема
                self._tick_machine(machine, now)

    # ─── per-machine ─────────────────────────────────────────
    def _tick_machine(self, machine, now: datetime) -> None:
        if machine.state == "setup":
            self._tick_setup(machine, now)
        elif machine.state == "running":
            self._tick_running(machine, now)
        elif machine.state == "cooldown":
            self._tick_cooldown(machine, now)
        # idle, maintenance, fault — ничего не делаем

    def _tick_setup(self, machine, now: datetime) -> None:
        elapsed = (now - machine.state_changed_at).total_seconds()
        if elapsed < config.SETUP_TIME_SEC:
            return
        # setup завершён → running
        machine.state = "running"
        machine.state_changed_at = now
        machine.last_sensor_sent_at = None
        self.client.state_change(machine, old_state="setup", new_state="running",
                                 event_time=now)

    def _tick_running(self, machine, now: datetime) -> None:
        batch = self.state.batches.get(machine.current_batch_id)
        if batch is None:
            return  # защита

        # 1. Догоняем все sensor_readings, которые должны были произойти.
        # Первое чтение — через SENSOR_INTERVAL_SEC после перехода в running.
        sensor_step = timedelta(seconds=config.SENSOR_INTERVAL_SEC)
        if machine.last_sensor_sent_at is None:
            machine.last_sensor_sent_at = machine.state_changed_at
        next_sensor_at = machine.last_sensor_sent_at + sensor_step
        while next_sensor_at <= now:
            readings = self._generate_sensor_readings(machine, batch.product_code)
            self.client.sensor_reading(machine, readings, event_time=next_sensor_at)
            machine.last_sensor_sent_at = next_sensor_at
            next_sensor_at += sensor_step

        # 2. Догоняем cycle_completion. Timestamp каждой детали = state_changed_at + N * cycle_sec.
        # Длительность цикла зависит от product_code партии (большие шестерни обрабатываются дольше).
        cycle_mult = config.CYCLE_TIME_MULT_BY_PRODUCT.get(batch.product_code, {}).get(machine.machine_type, 1.0)
        cycle_sec = config.CYCLE_TIME_SEC[machine.machine_type] * cycle_mult
        elapsed_running = (now - machine.state_changed_at).total_seconds()
        expected_done = int(elapsed_running // cycle_sec)
        expected_done = min(expected_done, batch.quantity)

        while machine.parts_done_in_batch < expected_done:
            machine.parts_done_in_batch += 1
            batch.parts_done_in_stage = machine.parts_done_in_batch
            machine.cycles_since_maintenance += 1
            machine.tool_wear = min(1.0, machine.tool_wear
                                    + config.TOOL_WEAR_PER_CYCLE[machine.machine_type])

            cycle_event_time = machine.state_changed_at + timedelta(
                seconds=machine.parts_done_in_batch * cycle_sec
            )

            # cycle_completion
            self.client.cycle_completion(
                machine,
                cycle_time_sec=cycle_sec,
                part_count=1,
                event_time=cycle_event_time,
                details={"batch_id": batch.batch_id,
                         "part_seq": machine.parts_done_in_batch,
                         "tool_wear": round(machine.tool_wear, 4)},
            )

            # Финальная инспекция: если деталь в выборке — отправляем quality
            if machine.machine_type == "inspection":
                idx = machine.parts_done_in_batch
                if idx in batch.inspection_sample_indices and idx not in batch.inspection_sampled_done:
                    batch.inspection_sampled_done.add(idx)
                    self.state.pending_inspection_measurements.append(
                        (batch.batch_id, idx, machine.machine_id, cycle_event_time)
                    )

            # Износ инструмента → запрос ТО
            if (machine.tool_wear >= config.TOOL_WEAR_TRIGGER
                    and not machine.tool_wear_alarm_sent):
                machine.tool_wear_alarm_sent = True
                self.client.alarm(
                    machine,
                    alarm_code="TOOL_WEAR_HIGH",
                    severity="warning",
                    message=f"Tool wear reached {machine.tool_wear:.2%}",
                    event_time=cycle_event_time,
                    details={"tool_wear": round(machine.tool_wear, 4)},
                )
                self.state.pending_tool_change_requests.append(machine.machine_id)

        # 3. Партия закончена → cooldown
        if machine.parts_done_in_batch >= batch.quantity:
            self._transition_to_cooldown(machine, batch, now)

    def _transition_to_cooldown(self, machine, batch, now: datetime) -> None:
        duration_sec = (now - batch.stage_started_at).total_seconds() if batch.stage_started_at else 0.0
        # отправляем batch_completion для текущего work_center
        self.client.batch_completion(
            batch,
            work_center=batch.stage,
            actual_quantity=batch.quantity,
            defect_count=batch.fails_count,
            duration_sec=duration_sec,
            event_time=now,
        )
        # переводим станок в cooldown
        old_state = machine.state
        machine.state = "cooldown"
        machine.state_changed_at = now
        self.client.state_change(machine, old_state=old_state, new_state="cooldown",
                                 event_time=now)

        # spot-check после hobbing или heat_treatment (только если этой стадией закончили)
        if batch.stage in config.SPOT_CHECK_STAGES and batch.stage not in batch.spot_checked_at:
            self.state.pending_spot_checks.append((batch.batch_id, batch.stage))

    def _tick_cooldown(self, machine, now: datetime) -> None:
        elapsed = (now - machine.state_changed_at).total_seconds()
        if elapsed < config.COOLDOWN_TIME_SEC:
            return

        # cooldown закончен → отпускаем партию дальше
        batch = self.state.batches.get(machine.current_batch_id)
        if batch is not None:
            self._route_batch_to_next_stage(machine, batch, now)

        # станок свободен
        machine.state = "idle"
        machine.state_changed_at = now
        machine.current_batch_id = None
        machine.parts_done_in_batch = 0
        machine.last_sensor_sent_at = None
        self.client.state_change(machine, old_state="cooldown", new_state="idle",
                                 event_time=now)

    def _route_batch_to_next_stage(self, machine, batch, now: datetime) -> None:
        next_stage = config.NEXT_STAGE.get(batch.stage)
        if next_stage is None or next_stage == "done":
            # завершение — после инспекции
            batch.stage = "done"
            batch.current_machine_id = None
            self.state.counters["batches_done"] += 1
            # удаляем партию из активных
            self.state.batches.pop(batch.batch_id, None)
            return

        # перемещаем в нужную очередь
        queue_key = config.QUEUE_BEFORE[next_stage]
        from_stage = batch.stage
        batch.stage = "waiting_" + next_stage
        batch.current_machine_id = None
        self.state.queues[queue_key].append(batch)
        # batch_move к следующему work_center
        self.client.batch_move(batch, from_center=from_stage, to_center=next_stage,
                               event_time=now)

    # ─── генерация sensor_reading ─────────────────────────────
    def _generate_sensor_readings(self, machine, product_code: str | None) -> dict[str, float]:
        profile = config.SENSOR_PROFILES.get(machine.machine_type, {})
        # Модификаторы по типоразмеру шестерни (большая → выше нагрузка/вибрация/темп).
        product_mods = config.SENSOR_MODIFIERS_BY_PRODUCT.get(product_code, {}) if product_code else {}
        result = {}
        for name, (mean, std, _unit) in profile.items():
            value = random.gauss(mean, std)
            # модификатор по product_code (постоянный множитель)
            product_mult = product_mods.get(name)
            if product_mult is not None:
                value *= product_mult
            # хук для сценариев аномалий (динамический множитель)
            anomaly_mult = machine.anomaly_modifier.get(name)
            if anomaly_mult is not None:
                value *= anomaly_mult
            result[name] = round(value, 4)
        return result