"""MaintenanceSubsystem: наряды на ТО и бригады.

Триггеры создания work_order:
- Превышение MAINTENANCE_CYCLES_THRESHOLD циклов с прошлого ТО (preventive)
- Превышение MAINTENANCE_HOURS_THRESHOLD часов с прошлого ТО (preventive)
- tool_wear ≥ TOOL_WEAR_TRIGGER — приходит запрос из equipment (high priority)
- Сценарий завершил лимит → equipment кладёт в state.pending_scenario_wos
  с (machine_id, scenario_type, wo_duration_min).

Назначение:
- На каждом тике пытаемся назначить created → assigned, если есть свободная бригада
- ТО можно начинать только если станок в idle (не прерываем running)

Завершение:
- По таймеру (длительность зависит от типа: scenario имеет свою длительность),
  станок → idle, бригада свободна, tool_wear=0, cycles=0. Если у станка был
  активный сценарий — снимаем его, размораживаем партию.
"""
import logging
import random
from datetime import datetime, timedelta

import config
from domain.work_order import WorkOrder

logger = logging.getLogger("simulator.maintenance")


class MaintenanceSubsystem:
    name = "maintenance"

    def __init__(self, state, client):
        self.state = state
        self.client = client

    def tick(self, now: datetime) -> None:
        with self.state.lock:
            self._handle_tool_wear_requests(now)
            self._handle_scenario_wos(now)
            self._create_preventive_wos(now)
            self._assign_wos(now)
            self._complete_wos(now)

    # ─── 1. Запросы из equipment по износу инструмента ────────
    def _handle_tool_wear_requests(self, now: datetime) -> None:
        while self.state.pending_tool_change_requests:
            machine_id = self.state.pending_tool_change_requests.popleft()
            existing = any(wo.machine_id == machine_id
                           for wo in self.state.work_orders.values())
            if existing:
                continue
            self._create_wo(machine_id, wo_type="tool_wear",
                            priority="high", reason="tool_wear_threshold",
                            now=now)

    # ─── 1b. Запросы от завершившихся сценариев ──────────────
    def _handle_scenario_wos(self, now: datetime) -> None:
        while self.state.pending_scenario_wos:
            machine_id, scenario_type, duration_min = self.state.pending_scenario_wos.popleft()
            existing = any(wo.machine_id == machine_id
                           for wo in self.state.work_orders.values())
            if existing:
                # сохраняем в очередь обратно, чтобы попробовать позже
                # (теоретически такого быть не должно, т.к. сценарии не пересекаются)
                continue
            self._create_wo(machine_id, wo_type="scenario",
                            priority="high", reason=scenario_type,
                            now=now,
                            expected_duration_sec=duration_min * 60)

    # ─── 2. Плановые preventive WO ────────────────────────────
    def _create_preventive_wos(self, now: datetime) -> None:
        for machine in self.state.machines.values():
            if any(wo.machine_id == machine.machine_id for wo in self.state.work_orders.values()):
                continue
            triggered_by = self._check_preventive_trigger(machine, now)
            if triggered_by is not None:
                self._create_wo(machine.machine_id, wo_type="preventive",
                                priority="normal", reason=triggered_by, now=now)

    def _check_preventive_trigger(self, machine, now: datetime) -> str | None:
        if machine.cycles_since_maintenance >= config.MAINTENANCE_CYCLES_THRESHOLD:
            return "cycles_threshold"
        last = machine.last_maintenance_at or self.state.virtual_time
        hours_since = (now - last).total_seconds() / 3600.0
        if hours_since >= config.MAINTENANCE_HOURS_THRESHOLD:
            return "hours_threshold"
        return None

    def _create_wo(self, machine_id: str, wo_type: str, priority: str,
                   reason: str, now: datetime,
                   expected_duration_sec: float | None = None) -> None:
        if expected_duration_sec is None:
            duration_min = random.uniform(*config.WO_DURATION_MIN_RANGE)
            expected_duration_sec = duration_min * 60
        wo = WorkOrder(
            wo_id=self.state.next_wo_id(),
            machine_id=machine_id,
            type=wo_type,
            priority=priority,
            reason=reason,
            created_at=now,
            expected_duration_sec=expected_duration_sec,
        )
        self.state.work_orders[wo.wo_id] = wo
        self.client.work_order_creation(wo, event_time=now)
        logger.info("created WO %s on %s (%s, %s, %.0f min)",
                    wo.wo_id, machine_id, wo_type, reason, expected_duration_sec / 60)

    # ─── 3. Назначение свободным бригадам ─────────────────────
    def _assign_wos(self, now: datetime) -> None:
        free_brigades = [b for b in self.state.brigades.values() if not b.is_busy]
        if not free_brigades:
            return
        created_wos = sorted(
            (wo for wo in self.state.work_orders.values() if wo.status == "created"),
            key=lambda wo: 0 if wo.priority == "high" else 1,
        )
        for wo in created_wos:
            if not free_brigades:
                break
            machine = self.state.machines.get(wo.machine_id)
            if machine is None:
                continue
            # станок должен быть свободен (нельзя прервать running)
            if machine.state not in ("idle",):
                continue
            brigade = free_brigades.pop(0)
            brigade.is_busy = True
            brigade.current_wo_id = wo.wo_id
            wo.status = "assigned"
            wo.assigned_brigade_id = brigade.brigade_id
            wo.assigned_at = now

            old_state = machine.state
            machine.state = "maintenance"
            machine.state_changed_at = now

            self.client.work_order_assignment(wo, event_time=now)
            self.client.state_change(machine, old_state=old_state,
                                     new_state="maintenance", event_time=now,
                                     reason=wo.type,
                                     details={"wo_id": wo.wo_id,
                                              "brigade_id": brigade.brigade_id})
            logger.info("assigned WO %s → %s on %s",
                        wo.wo_id, brigade.brigade_id, wo.machine_id)

    # ─── 4. Завершение по таймеру ─────────────────────────────
    def _complete_wos(self, now: datetime) -> None:
        for wo in list(self.state.work_orders.values()):
            if wo.status != "assigned":
                continue
            elapsed = (now - wo.assigned_at).total_seconds()
            if elapsed < wo.expected_duration_sec:
                continue
            self._complete_wo(wo, now)

    def _complete_wo(self, wo: WorkOrder, now: datetime) -> None:
        brigade = self.state.brigades.get(wo.assigned_brigade_id)
        if brigade:
            brigade.is_busy = False
            brigade.current_wo_id = None

        machine = self.state.machines.get(wo.machine_id)
        if machine is None:
            wo.status = "completed"
            self.state.work_orders.pop(wo.wo_id, None)
            return

        # Снимаем активный сценарий со станка, если он был.
        # Модификаторы сенсоров чистятся ниже (общий проход).
        if machine.active_scenario_id:
            sid = machine.active_scenario_id
            meta = self.state.scenarios_registry.get(sid)
            if meta:
                # снимаем sensor modifiers
                for key in meta.get("sensors", {}):
                    machine.anomaly_modifier.pop(key, None)
                # помечаем сценарий как завершённый (cleanup сделает ScenariosController.tick)
                meta["status"] = meta.get("status") if meta.get("status") in ("auto_completed", "stopped") else "stopped"
            machine.active_scenario_id = None

        machine.state = "idle"
        machine.state_changed_at = now
        machine.tool_wear = 0.0
        machine.tool_wear_alarm_sent = False
        machine.cycles_since_maintenance = 0
        machine.last_maintenance_at = now

        # Размораживаем партию, если она была заморожена на этом станке.
        # (ситуация после tool_wear или после auto_completed сценария)
        if machine.current_batch_id:
            batch = self.state.batches.get(machine.current_batch_id)
            if batch and batch.is_frozen:
                batch.is_frozen = False
                batch.frozen_reason = None
                # Партия остаётся на станке; equipment продолжит обработку
                # с текущего parts_done_in_batch. Возвращаем станок в running.
                machine.state = "running"
                # ВАЖНО: сдвигаем «начало обработки» назад на уже сделанные
                # детали. Иначе expected_done = (now - state_changed_at)//cycle_sec
                # считается с нуля, и станок простаивает ~parts_done*cycle_sec,
                # пока виртуальное время «догонит» уже обработанные детали.
                cyc_mult = config.CYCLE_TIME_MULT_BY_PRODUCT.get(
                    batch.product_code, {}).get(machine.machine_type, 1.0)
                cycle_sec = config.CYCLE_TIME_SEC[machine.machine_type] * cyc_mult
                machine.state_changed_at = now - timedelta(
                    seconds=machine.parts_done_in_batch * cycle_sec)
                # Сенсоры продолжаем с момента возобновления (без всплеска за
                # период обслуживания).
                machine.last_sensor_sent_at = now
                self.client.state_change(
                    machine, old_state="maintenance", new_state="running",
                    event_time=now,
                    reason="resume_after_maintenance",
                    details={"wo_id": wo.wo_id, "batch_id": batch.batch_id},
                )
            else:
                # Нет партии или партия не заморожена → станок в idle
                self.client.state_change(
                    machine, old_state="maintenance", new_state="idle",
                    event_time=now, details={"wo_id": wo.wo_id},
                )
                # Если current_batch_id указывает на партию, которой нет в state.batches
                # (уже завершена) — почистим
                if machine.current_batch_id and machine.current_batch_id not in self.state.batches:
                    machine.current_batch_id = None
        else:
            self.client.state_change(machine, old_state="maintenance",
                                     new_state="idle", event_time=now,
                                     details={"wo_id": wo.wo_id})

        duration_min = (now - wo.assigned_at).total_seconds() / 60.0
        self.client.work_order_completion(wo, duration_min=duration_min,
                                          event_time=now)

        wo.status = "completed"
        self.state.work_orders.pop(wo.wo_id, None)
        logger.info("completed WO %s on %s", wo.wo_id, wo.machine_id)
