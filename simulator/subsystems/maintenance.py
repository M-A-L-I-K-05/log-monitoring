"""MaintenanceSubsystem: наряды на ТО и бригады.

Триггеры создания work_order:
- Превышение MAINTENANCE_CYCLES_THRESHOLD циклов с прошлого ТО (preventive)
- Превышение MAINTENANCE_HOURS_THRESHOLD часов с прошлого ТО (preventive)
- tool_wear ≥ TOOL_WEAR_TRIGGER — приходит запрос из equipment (tool_wear, high priority)

Назначение:
- На каждом тике пытаемся назначить created → assigned, если есть свободная бригада
- ТО можно начинать только если станок в idle (не прерываем running)

Завершение:
- По таймеру (30-60 мин), станок → idle, бригада свободна, tool_wear=0, cycles=0
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
            self._create_preventive_wos(now)
            self._assign_wos(now)
            self._complete_wos(now)

    # ─── 1. Запросы из equipment по износу инструмента ────────
    def _handle_tool_wear_requests(self, now: datetime) -> None:
        while self.state.pending_tool_change_requests:
            machine_id = self.state.pending_tool_change_requests.popleft()
            # не создаём дубликат если уже есть открытый WO на этот станок
            existing = any(wo.machine_id == machine_id
                           for wo in self.state.work_orders.values())
            if existing:
                continue
            self._create_wo(machine_id, wo_type="tool_wear",
                            priority="high", reason="tool_wear_threshold",
                            now=now)

    # ─── 2. Плановые preventive WO ────────────────────────────
    def _create_preventive_wos(self, now: datetime) -> None:
        for machine in self.state.machines.values():
            # уже есть открытый WO?
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
                   reason: str, now: datetime) -> None:
        duration_min = random.uniform(*config.WO_DURATION_MIN_RANGE)
        wo = WorkOrder(
            wo_id=self.state.next_wo_id(),
            machine_id=machine_id,
            type=wo_type,
            priority=priority,
            reason=reason,
            created_at=now,
            expected_duration_sec=duration_min * 60,
        )
        self.state.work_orders[wo.wo_id] = wo
        self.client.work_order_creation(wo, event_time=now)
        logger.info("created WO %s on %s (%s, %s)", wo.wo_id, machine_id, wo_type, reason)

    # ─── 3. Назначение свободным бригадам ─────────────────────
    def _assign_wos(self, now: datetime) -> None:
        free_brigades = [b for b in self.state.brigades.values() if not b.is_busy]
        if not free_brigades:
            return
        # Сортируем WO по приоритету: high первыми
        created_wos = sorted(
            (wo for wo in self.state.work_orders.values() if wo.status == "created"),
            key=lambda wo: 0 if wo.priority == "high" else 1,
        )
        for wo in created_wos:
            if not free_brigades:
                break
            machine = self.state.machines[wo.machine_id]
            # станок должен быть свободен (нельзя прервать партию)
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
        brigade = self.state.brigades[wo.assigned_brigade_id]
        brigade.is_busy = False
        brigade.current_wo_id = None

        machine = self.state.machines[wo.machine_id]
        machine.state = "idle"
        machine.state_changed_at = now
        machine.tool_wear = 0.0
        machine.tool_wear_alarm_sent = False
        machine.cycles_since_maintenance = 0
        machine.last_maintenance_at = now

        duration_min = (now - wo.assigned_at).total_seconds() / 60.0
        self.client.work_order_completion(wo, duration_min=duration_min,
                                          event_time=now)
        self.client.state_change(machine, old_state="maintenance",
                                 new_state="idle", event_time=now,
                                 details={"wo_id": wo.wo_id})

        wo.status = "completed"
        self.state.work_orders.pop(wo.wo_id, None)
        logger.info("completed WO %s on %s", wo.wo_id, wo.machine_id)