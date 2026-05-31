"""EquipmentSubsystem: циклы обычных станков (включая M-GMM).

Поведение по состояниям (для обработки = turning/hobbing/shaving/grinding):
- setup → running (по таймеру SETUP_TIME_SEC)
- running:
    * каждые SENSOR_INTERVAL_SEC — sensor_reading (с anomaly_modifier сценария)
    * каждые CYCLE_TIME_SEC[type] — обработана 1 деталь; при активном сценарии
      деталь помечается в batch.scenario_marked_indices.
    * если tool_wear ≥ TOOL_WEAR_TRIGGER ПОСЕРЕДИНЕ партии → НЕМЕДЛЕННО
      stop, заморозка партии, запрос ТО, станок → maintenance.
    * если parts_done == quantity → state=cooldown
- cooldown → idle (по таймеру COOLDOWN_TIME_SEC):
    * партия отправляется на ИЗМЕРЕНИЕ (M-GMM) с quality_hold=True
    * перед следующим этапом маршрута partия проходит M-GMM как операцию.

Для M-GMM (machine_type="inspection") поведение особое:
- setup → running (тоже наладка)
- running: время = (число измеряемых деталей) × INSPECTION_TIME_PER_PART_SEC[stage]
  По истечении — задача в quality.pending_measurements (там и логи, и БД).
- После измерения партия идёт на СЛЕДУЮЩИЙ этап маршрута (определяется
  stage_after) и quality_hold снимается.
"""
import logging
import random
from datetime import datetime, timedelta

import config

logger = logging.getLogger("simulator.equipment")


class EquipmentSubsystem:
    name = "equipment"

    def __init__(self, state, client, quality):
        self.state = state
        self.client = client
        self.quality = quality   # для поштучного измерения на M-GMM

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
            if machine.machine_type == "inspection":
                self._tick_running_inspection(machine, now)
            else:
                self._tick_running_processing(machine, now)
        elif machine.state == "cooldown":
            self._tick_cooldown(machine, now)
        # idle, maintenance, fault — ничего не делаем

    def _tick_setup(self, machine, now: datetime) -> None:
        # M-GMM: только закрепление детали + калибровка зонда — быстрее.
        setup_sec = (config.INSPECTION_SETUP_TIME_SEC
                     if machine.machine_type == "inspection"
                     else config.SETUP_TIME_SEC)
        elapsed = (now - machine.state_changed_at).total_seconds()
        if elapsed < setup_sec:
            return
        # setup завершён → running
        machine.state = "running"
        machine.state_changed_at = now
        machine.last_sensor_sent_at = None
        # M-GMM: зонд откалиброван → строим план измерения партии.
        if machine.machine_type == "inspection":
            batch = self.state.batches.get(machine.current_batch_id)
            if batch is not None:
                stage = machine.measuring_after_stage or "inspection"
                machine.measurement_plan = self.quality.build_plan(
                    batch, stage, machine.machine_id)
                machine.measurement_done = 0
                machine.measurement_total = len(machine.measurement_plan)
        self.client.state_change(machine, old_state="setup", new_state="running",
                                 event_time=now)

    # ─── running для ОБЫЧНЫХ станков (обработка деталей) ─────
    def _tick_running_processing(self, machine, now: datetime) -> None:
        batch = self.state.batches.get(machine.current_batch_id)
        if batch is None:
            return
        if batch.is_frozen:
            # партия заморожена (например, идёт ремонт по сценарию)
            return

        # 1. Догоняем все sensor_readings.
        sensor_step = timedelta(seconds=config.SENSOR_INTERVAL_SEC)
        if machine.last_sensor_sent_at is None:
            machine.last_sensor_sent_at = machine.state_changed_at
        next_sensor_at = machine.last_sensor_sent_at + sensor_step
        while next_sensor_at <= now:
            readings = self._generate_sensor_readings(machine, batch.product_code)
            self.client.sensor_reading(machine, readings, event_time=next_sensor_at,
                                       product_code=batch.product_code)
            machine.last_sensor_sent_at = next_sensor_at
            next_sensor_at += sensor_step

        # 2. Догоняем cycle_completion.
        cycle_mult = config.CYCLE_TIME_MULT_BY_PRODUCT.get(batch.product_code, {}).get(machine.machine_type, 1.0)
        cycle_sec = config.CYCLE_TIME_SEC[machine.machine_type] * cycle_mult
        elapsed_running = (now - machine.state_changed_at).total_seconds()
        # Обрабатываем только ГОДНЫЕ детали: выбывшие на прошлых этапах
        # (failed_indices) не прогоняются через станок повторно — поэтому и
        # прогресс, и время этапа считаются по годному количеству.
        good = batch.good_indices()
        eff_qty = len(good)
        expected_done = int(elapsed_running // cycle_sec)
        expected_done = min(expected_done, eff_qty)

        while machine.parts_done_in_batch < expected_done:
            # Перед обработкой следующей детали проверяем tool_wear.
            # Если уже на пороге → НЕМЕДЛЕННО стоп, ни одна "красная" деталь не идёт.
            if machine.tool_wear >= config.TOOL_WEAR_TRIGGER and not machine.tool_wear_alarm_sent:
                self._freeze_for_tool_wear(machine, batch, now)
                return

            machine.parts_done_in_batch += 1
            batch.parts_done_in_stage = machine.parts_done_in_batch
            machine.cycles_since_maintenance += 1
            machine.tool_wear = min(1.0, machine.tool_wear
                                    + config.TOOL_WEAR_PER_CYCLE[machine.machine_type])

            # Дрейф: обновляем drift_progress и anomaly_modifier для gradual-сценариев.
            if machine.active_scenario_id:
                meta = self.state.scenarios_registry.get(machine.active_scenario_id)
                if meta and meta.get("mode") == "gradual":
                    pace = meta.get("pace") or 0.0
                    machine.drift_progress = min(1.0, machine.drift_progress + pace)
                    for sensor, target in meta.get("sensors_final", {}).items():
                        machine.anomaly_modifier[sensor] = (
                            1.0 + (target - 1.0) * machine.drift_progress
                        )

            cycle_event_time = machine.state_changed_at + timedelta(
                seconds=machine.parts_done_in_batch * cycle_sec
            )

            # Пометка детали под активным сценарием — только если drift_progress
            # перешёл порог отбраковки (scrap_threshold). Для step: порог=0.0,
            # поэтому все детали тегаются. Для gradual: фаза 1 не тегается.
            if machine.active_scenario_id:
                meta = self.state.scenarios_registry.get(machine.active_scenario_id)
                if meta:
                    scrap_threshold = meta.get("scrap_threshold", 0.0)
                    if machine.drift_progress >= scrap_threshold:
                        part_idx = good[machine.parts_done_in_batch - 1]
                        batch.scenario_marked_indices[part_idx] = machine.active_scenario_id

            # cycle_completion
            self.client.cycle_completion(
                machine,
                cycle_time_sec=cycle_sec,
                part_count=1,
                event_time=cycle_event_time,
                details={"batch_id": batch.batch_id,
                         "part_seq": machine.parts_done_in_batch,
                         "tool_wear": round(machine.tool_wear, 4),
                         "drift_progress": round(machine.drift_progress, 4)},
            )

            # Сценарий: проверка лимита завершения.
            # gradual: stop_threshold ИЛИ конец партии в фазе 2 (drift >= scrap).
            # step:    parts_cap исчерпан.
            if machine.active_scenario_id and self._scenario_limit_reached(machine, batch):
                self._auto_complete_scenario(machine, batch, now)
                return

        # 3. Партия закончена → cooldown
        if machine.parts_done_in_batch >= eff_qty:
            self._transition_to_cooldown(machine, batch, now)

    # ─── running для M-GMM (инспекционные станки) ────────────
    def _tick_running_inspection(self, machine, now: datetime) -> None:
        batch = self.state.batches.get(machine.current_batch_id)
        if batch is None:
            return

        # M-GMM сенсорику тоже шлёт (ambient_temp/humidity/air_pressure),
        # но без anomaly_modifier (он чист в норме).
        sensor_step = timedelta(seconds=config.SENSOR_INTERVAL_SEC)
        if machine.last_sensor_sent_at is None:
            machine.last_sensor_sent_at = machine.state_changed_at
        next_sensor_at = machine.last_sensor_sent_at + sensor_step
        while next_sensor_at <= now:
            readings = self._generate_sensor_readings(machine, batch.product_code)
            self.client.sensor_reading(machine, readings, event_time=next_sensor_at,
                                       product_code=batch.product_code)
            machine.last_sensor_sent_at = next_sensor_at
            next_sensor_at += sensor_step

        # Поштучное измерение: каждые time_per_part виртуальных секунд —
        # одна деталь из плана. Деталь меряется здесь же (quality логирует
        # результат), failed_indices/fails_count растут по ходу.
        stage = machine.measuring_after_stage or "inspection"
        time_per_part = config.INSPECTION_TIME_PER_PART_SEC.get(stage, 60)
        elapsed = (now - machine.state_changed_at).total_seconds()
        expected = int(elapsed // time_per_part)

        while (machine.measurement_done < expected
               and machine.measurement_done < len(machine.measurement_plan)):
            item = machine.measurement_plan[machine.measurement_done]
            measured_at = machine.state_changed_at + timedelta(
                seconds=(machine.measurement_done + 1) * time_per_part)
            decision = self.quality.measure_plan_item(
                batch, item, stage, machine.machine_id, measured_at)
            machine.measurement_done += 1
            # Спот-контроль: деталь забракована → доизмерить 2 соседние.
            # План растёт, прогресс на станке продолжается с текущей позиции.
            if item["mode"] == "spot" and decision == "fail":
                machine.measurement_plan.extend(
                    self.quality.spot_neighbors(batch, item["idx"]))
                machine.measurement_total = len(machine.measurement_plan)

        # Измерение завершено, когда измерены все запланированные детали
        # И прошло время на весь объём (чтобы станок не «телепортировался»).
        if machine.measurement_done < len(machine.measurement_plan):
            return
        finished_at = machine.state_changed_at + timedelta(
            seconds=max(1, len(machine.measurement_plan)) * time_per_part)
        if now < finished_at:
            return

        # M-GMM не нагревается → cooldown не нужен. Сразу idle, партия
        # маршрутизируется на следующий этап / в done.
        self._route_after_measurement(machine, batch, finished_at)
        machine.state = "idle"
        machine.state_changed_at = finished_at
        machine.current_batch_id = None
        machine.parts_done_in_batch = 0
        machine.last_sensor_sent_at = None
        machine.measuring_after_stage = None
        machine.measurement_plan = []
        machine.measurement_done = 0
        machine.measurement_total = 0
        self.client.state_change(machine, old_state="running", new_state="idle",
                                 event_time=finished_at,
                                 details={"role": "measurement",
                                          "stage": stage,
                                          "batch_id": batch.batch_id})

    # ─── cooldown (только для обрабатывающих станков) ────────
    def _tick_cooldown(self, machine, now: datetime) -> None:
        # M-GMM не нагреваются — они в cooldown не уходят
        # (см. _tick_running_inspection: сразу running → idle).
        elapsed = (now - machine.state_changed_at).total_seconds()
        if elapsed < config.COOLDOWN_TIME_SEC:
            return

        batch = self.state.batches.get(machine.current_batch_id)
        if batch is not None:
            self._route_after_processing(machine, batch, now)

        # станок свободен
        machine.state = "idle"
        machine.state_changed_at = now
        machine.current_batch_id = None
        machine.parts_done_in_batch = 0
        machine.last_sensor_sent_at = None
        self.client.state_change(machine, old_state="cooldown", new_state="idle",
                                 event_time=now)

    def _transition_to_cooldown(self, machine, batch, now: datetime) -> None:
        duration_sec = (now - batch.stage_started_at).total_seconds() if batch.stage_started_at else 0.0
        # Считаем актуальное "годное количество" партии. (выбытие может ещё
        # произойти на измерении, но к этому моменту мы фиксируем то что есть.)
        actual_qty = batch.quantity - len(batch.failed_indices)
        self.client.batch_completion(
            batch,
            work_center=batch.stage,
            actual_quantity=actual_qty,
            defect_count=batch.fails_count,
            duration_sec=duration_sec,
            event_time=now,
        )
        old_state = machine.state
        machine.state = "cooldown"
        machine.state_changed_at = now
        self.client.state_change(machine, old_state=old_state, new_state="cooldown",
                                 event_time=now)
        # Этап завершён — фиксируем для маршрутизации на измерение
        batch.last_processed_stage = batch.stage
        batch.last_processed_machine_id = machine.machine_id

    # ─── маршрутизация ───────────────────────────────────────
    def _route_after_processing(self, machine, batch, now: datetime) -> None:
        """После обработки на обычном станке → партия идёт на M-GMM на измерение."""
        stage_just_done = batch.last_processed_stage or batch.stage
        # помечаем партию ожидающей измерения
        batch.stage = "queue_measurement"
        batch.current_machine_id = None
        batch.parts_done_in_stage = 0
        batch.quality_hold = True
        # последний обработавший станок — сохраним для quality (source_machine_id)
        # (уже выставлен в _transition_to_cooldown)
        # batch_move в виртуальный work_center "measurement" — для трассировки
        self.client.batch_move(batch, from_center=stage_just_done,
                               to_center="measurement", event_time=now)
        self.state.queues["queue_measurement"].append(batch)
        # batch.last_processed_stage сохранён — production будет знать,
        # что после измерения партия пойдёт в config.NEXT_STAGE[stage]
        # (НЕ "measurement", а реальный следующий этап).

    def _route_after_measurement(self, machine, batch, now: datetime) -> None:
        """После M-GMM → следующий этап маршрута, либо done."""
        stage_just_measured = machine.measuring_after_stage or batch.last_processed_stage or "inspection"
        # снимаем quality_hold — теперь production может назначить
        batch.quality_hold = False

        # Если только что мерили после inspection (финал) — партия done.
        # ИЛИ если grinding был последним этапом? Нет, ROUTE завершается
        # на inspection. Поэтому после "inspection" → done.
        if stage_just_measured == "inspection":
            duration_sec = (now - batch.stage_started_at).total_seconds() if batch.stage_started_at else 0.0
            actual_qty = batch.quantity - len(batch.failed_indices)
            self.client.batch_completion(
                batch, work_center="inspection",
                actual_quantity=actual_qty,
                defect_count=batch.fails_count,
                duration_sec=duration_sec,
                event_time=now,
            )
            batch.stage = "done"
            batch.current_machine_id = None
            self.state.counters["batches_done"] += 1
            # Все неотбракованные шестерни прошли весь маршрут → pass.
            self.state.counters["parts_pass"] += batch.effective_quantity
            self.state.batches.pop(batch.batch_id, None)
            return

        # Иначе — следующий этап производства.
        # Если все детали выбыли (100% брак) — партия done, дальше не идёт.
        remaining = batch.quantity - len(batch.failed_indices)
        if remaining <= 0:
            duration_sec = (now - batch.stage_started_at).total_seconds() if batch.stage_started_at else 0.0
            self.client.batch_completion(
                batch, work_center=stage_just_measured,
                actual_quantity=0,
                defect_count=batch.fails_count,
                duration_sec=duration_sec,
                event_time=now,
            )
            batch.stage = "done"
            batch.current_machine_id = None
            self.state.counters["batches_done"] += 1
            # 100% брак — годных нет, parts_pass += 0 (явно для единообразия).
            self.state.counters["parts_pass"] += batch.effective_quantity
            self.state.batches.pop(batch.batch_id, None)
            return

        next_stage = config.NEXT_STAGE.get(stage_just_measured)
        if next_stage is None or next_stage == "done":
            batch.stage = "done"
            batch.current_machine_id = None
            self.state.counters["batches_done"] += 1
            self.state.counters["parts_pass"] += batch.effective_quantity
            self.state.batches.pop(batch.batch_id, None)
            return

        queue_key = config.QUEUE_BEFORE[next_stage]
        batch.stage = "waiting_" + next_stage
        batch.current_machine_id = None
        self.state.queues[queue_key].append(batch)
        self.client.batch_move(batch, from_center="measurement",
                               to_center=next_stage, event_time=now)

    # ─── заморозка партии при tool_wear ──────────────────────
    def _freeze_for_tool_wear(self, machine, batch, now: datetime) -> None:
        """tool_wear >= TRIGGER посреди партии → НЕМЕДЛЕННО стоп, WO, maintenance."""
        machine.tool_wear_alarm_sent = True
        self.client.alarm(
            machine,
            alarm_code="TOOL_WEAR_HIGH",
            severity="warning",
            message=f"Tool wear reached {machine.tool_wear:.2%}",
            event_time=now,
            details={"tool_wear": round(machine.tool_wear, 4)},
        )
        # запросить ремонт через общий механизм
        self.state.pending_tool_change_requests.append(machine.machine_id)
        # заморозить партию
        batch.is_frozen = True
        batch.frozen_reason = "tool_wear"
        # станок останется в "running" (не cooldown!) — после ремонта вернётся
        # в running и доработает партию с текущего parts_done_in_batch.
        # Но MaintenanceSubsystem назначает WO только когда machine.state="idle".
        # Поэтому переводим в "idle" (с current_batch_id, который тоже сохраняем).
        old_state = machine.state
        machine.state = "idle"
        machine.state_changed_at = now
        self.client.state_change(machine, old_state=old_state, new_state="idle",
                                 event_time=now, reason="tool_wear_freeze",
                                 details={"batch_id": batch.batch_id})

    # ─── автозавершение сценария по лимиту деталей ──────────
    def _scenario_limit_reached(self, machine, batch) -> bool:
        """Проверяет условие завершения сценария в зависимости от режима.

        gradual:
          - drift_progress >= stop_threshold → стоп (аномалия полная)
          - drift_progress >= scrap_threshold И конец партии → стоп
            (не начинаем новую партию в зоне отбраковки)
          - drift_progress < scrap_threshold И конец партии → НЕ стоп
            (продолжаем на следующей партии, это фаза 1)

        step:
          - кол-во помеченных деталей >= parts_cap → стоп
        """
        sid = machine.active_scenario_id
        meta = self.state.scenarios_registry.get(sid) if sid else None
        if not meta:
            return False

        mode = meta.get("mode", "step")

        if mode == "gradual":
            stop_threshold = meta.get("stop_threshold", 1.0)
            scrap_threshold = meta.get("scrap_threshold", config.DRIFT_SCRAP_THRESHOLD)
            if machine.drift_progress >= stop_threshold:
                return True
            if (machine.drift_progress >= scrap_threshold
                    and machine.parts_done_in_batch >= batch.effective_quantity):
                return True
            return False
        else:  # step
            parts_cap = meta.get("parts_cap")
            if parts_cap is None:
                # fallback на старый механизм если parts_cap не задан
                parts_cap = meta.get("parts_limit_effective", meta.get("parts_limit", 10))
            tagged = sum(1 for s in batch.scenario_marked_indices.values() if s == sid)
            return tagged >= parts_cap

    def _auto_complete_scenario(self, machine, batch, now: datetime) -> None:
        """Сценарий исчерпал лимит → alarm + WO + станок в maintenance + заморозка партии."""
        sid = machine.active_scenario_id
        meta = self.state.scenarios_registry.get(sid)
        if not meta:
            return

        scenario_type = meta.get("scenario_type", "scenario")
        tagged = sum(1 for s in batch.scenario_marked_indices.values() if s == sid)

        # Alarm: фиксирует аномалию в Loki/Grafana с именами искажённых сенсоров.
        self.client.alarm(
            machine,
            alarm_code=f"PROCESS_{scenario_type.upper()}",
            severity="critical",
            message=f"Process anomaly completed: {scenario_type}",
            event_time=now,
            details={
                "scenario_id": sid,
                "mode": meta.get("mode", "step"),
                "drift_progress": round(machine.drift_progress, 4),
                "sensors_affected": {k: round(v, 4)
                                     for k, v in machine.anomaly_modifier.items()},
                "parts_tagged": tagged,
            },
        )

        # Если на станке остались необработанные детали — партия замораживается.
        if machine.parts_done_in_batch < batch.effective_quantity:
            batch.is_frozen = True
            batch.frozen_reason = f"scenario:{scenario_type}"

        # WO от сценария: длительность из реестра.
        duration_min = meta.get("wo_duration_min", 30.0)
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
            details={"mode": meta.get("mode", "step"),
                     "drift_progress": round(machine.drift_progress, 4),
                     "parts_tagged": tagged,
                     "batch_id": batch.batch_id,
                     "wo_duration_min": duration_min},
        )

        # Если вся партия закончена — cooldown → idle → M-GMM.
        if machine.parts_done_in_batch >= batch.effective_quantity:
            self._transition_to_cooldown(machine, batch, now)
            return

        # Иначе: партия заморожена, станок → idle, MaintenanceSubsystem подхватит WO.
        old_state = machine.state
        machine.state = "idle"
        machine.state_changed_at = now
        self.client.state_change(
            machine, old_state=old_state, new_state="idle",
            event_time=now, reason="scenario_complete_freeze",
            details={"batch_id": batch.batch_id, "scenario_id": sid},
        )

    # ─── генерация sensor_reading ─────────────────────────────
    def _generate_sensor_readings(self, machine, product_code: str | None) -> dict[str, float]:
        profile = config.SENSOR_PROFILES.get(machine.machine_type, {})
        # Модификаторы по типоразмеру шестерни.
        product_mods = config.SENSOR_MODIFIERS_BY_PRODUCT.get(product_code, {}) if product_code else {}
        result = {}
        for name, (mean, std, _unit) in profile.items():
            value = random.gauss(mean, std)
            product_mult = product_mods.get(name)
            if product_mult is not None:
                value *= product_mult
            anomaly_mult = machine.anomaly_modifier.get(name)
            if anomaly_mult is not None:
                value *= anomaly_mult
            result[name] = round(value, 4)
        return result
