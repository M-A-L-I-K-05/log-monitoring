"""HTTP-клиент к 4 backend-сервисам. Один метод = одно событие.
Все методы добавляют event_time как ISO-строку в payload.

Высокочастотные события (sensor_reading, cycle_completion, measurement)
буферизуются и отправляются пачкой при flush() в конце тика.
Остальные — летят немедленно (они редкие, оптимизация не нужна).
"""
from datetime import datetime

import requests

import config


class FactoryClient:
    def __init__(self):
        self._session = requests.Session()
        self.eq  = config.SERVICE_URLS["equipment"]
        self.pr  = config.SERVICE_URLS["production"]
        self.qa  = config.SERVICE_URLS["quality"]
        self.mt  = config.SERVICE_URLS["maintenance"]

        # Буферы для batch-эндпоинтов (заполняются в течение тика,
        # отправляются одним запросом при flush()).
        self._sensor_buffer: list[dict] = []
        self._cycle_buffer: list[dict] = []
        self._measurement_buffer: list[dict] = []

    # ─── низкоуровневый POST ──────────────────────────────────
    def _post(self, url: str, payload) -> None:
        try:
            self._session.post(url, json=payload, timeout=config.HTTP_TIMEOUT_SEC)
        except Exception as exc:
            raise RuntimeError(f"POST {url} failed: {exc}") from exc

    # ─── flush буферов ────────────────────────────────────────
    def flush(self) -> None:
        """Отправить накопленные batch-события. Вызывается в конце каждого тика."""
        if self._sensor_buffer:
            self._post(self.eq + "/sensor-reading/batch", self._sensor_buffer)
            self._sensor_buffer = []
        if self._cycle_buffer:
            self._post(self.eq + "/cycle-completion/batch", self._cycle_buffer)
            self._cycle_buffer = []
        if self._measurement_buffer:
            self._post(self.qa + "/measurement/batch", self._measurement_buffer)
            self._measurement_buffer = []

    # ─── reset (вызывается при /restart симулятора) ───────────
    def reset_remote_state(self) -> None:
        """Сбросить БД-таблицы всех 4 сервисов в исходное состояние.
        equipment: machine_status → idle
        production: TRUNCATE active_batches
        quality:    TRUNCATE measurements RESTART IDENTITY
        maintenance: TRUNCATE open_work_orders
        Игнорирует ошибки отдельных сервисов (если один лежит — остальные сбросятся)."""
        self._sensor_buffer = []
        self._cycle_buffer = []
        self._measurement_buffer = []
        for url in (self.eq + "/reset", self.pr + "/reset",
                    self.qa + "/reset", self.mt + "/reset"):
            try:
                self._session.post(url, timeout=config.HTTP_TIMEOUT_SEC)
            except Exception:
                pass

    # ─── sync fleet (вызывается по кнопке Sync Fleet) ─────────
    def sync_fleet(self) -> dict:
        """Синхронизация парка станков с equipment.

        Берёт config.MACHINES, шлёт на equipment /register-machines.
        Возвращает ответ сервиса для отображения в UI / логов.
        """
        machines = [
            {"machine_id": mid, "machine_type": mtype,
             "work_center": wc, "model": model,
             "install_date": config.INSTALL_DATE}
            for mid, mtype, wc, model in config.MACHINES
        ]
        payload = {
            "machines": machines,
            "init_state": "idle",
            "init_time": "2024-01-01 00:00:00",
        }
        try:
            r = self._session.post(
                self.eq + "/register-machines",
                json=payload,
                timeout=config.HTTP_TIMEOUT_SEC,
            )
            return r.json() if r.ok else {"sync": False, "status": r.status_code}
        except Exception as exc:
            return {"sync": False, "error": str(exc)}

    # ─── EQUIPMENT ─────────────────────────────────────────────
    def sensor_reading(self, machine, readings: dict, event_time: datetime) -> None:
        self._sensor_buffer.append({
            "machine_id":   machine.machine_id,
            "machine_type": machine.machine_type,
            "work_center":  machine.work_center,
            "readings":     readings,
            "event_time":   event_time.isoformat(),
        })

    def state_change(self, machine, old_state: str, new_state: str,
                     event_time: datetime, reason: str | None = None,
                     details: dict | None = None) -> None:
        self._post(self.eq + "/state-change", {
            "machine_id":   machine.machine_id,
            "machine_type": machine.machine_type,
            "work_center":  machine.work_center,
            "old_state":    old_state,
            "new_state":    new_state,
            "reason":       reason,
            "details":      details,
            "event_time":   event_time.isoformat(),
        })

    def alarm(self, machine, alarm_code: str, severity: str, message: str,
              event_time: datetime, details: dict | None = None) -> None:
        self._post(self.eq + "/alarm", {
            "machine_id":   machine.machine_id,
            "machine_type": machine.machine_type,
            "work_center":  machine.work_center,
            "alarm_code":   alarm_code,
            "severity":     severity,
            "message":      message,
            "details":      details,
            "event_time":   event_time.isoformat(),
        })

    def cycle_completion(self, machine, cycle_time_sec: float, part_count: int,
                         event_time: datetime, tool_id: str | None = None,
                         details: dict | None = None) -> None:
        self._cycle_buffer.append({
            "machine_id":     machine.machine_id,
            "machine_type":   machine.machine_type,
            "work_center":    machine.work_center,
            "cycle_time_sec": cycle_time_sec,
            "part_count":     part_count,
            "tool_id":        tool_id,
            "details":        details,
            "event_time":     event_time.isoformat(),
        })

    # ─── PRODUCTION ────────────────────────────────────────────
    def order_creation(self, order, event_time: datetime) -> None:
        self._post(self.pr + "/order-creation", {
            "order_id":     order.order_id,
            "product_code": order.product_code,
            "quantity":     order.total_quantity,
            "priority":     order.priority,
            "event_time":   event_time.isoformat(),
        })

    def batch_start(self, batch, work_center: str, event_time: datetime) -> None:
        self._post(self.pr + "/batch-start", {
            "batch_id":         batch.batch_id,
            "order_id":         batch.order_id,
            "product_code":     batch.product_code,
            "priority":         batch.priority,
            "work_center":      work_center,
            "planned_quantity": batch.quantity,
            "event_time":       event_time.isoformat(),
        })

    def batch_move(self, batch, from_center: str, to_center: str,
                   event_time: datetime) -> None:
        self._post(self.pr + "/batch-move", {
            "batch_id":    batch.batch_id,
            "from_center": from_center,
            "to_center":   to_center,
            "event_time":  event_time.isoformat(),
        })

    def batch_completion(self, batch, work_center: str, actual_quantity: int,
                         defect_count: int, duration_sec: float,
                         event_time: datetime) -> None:
        self._post(self.pr + "/batch-completion", {
            "batch_id":        batch.batch_id,
            "work_center":     work_center,
            "actual_quantity": actual_quantity,
            "defect_count":    defect_count,
            "duration_sec":    duration_sec,
            "event_time":      event_time.isoformat(),
        })

    # ─── QUALITY ───────────────────────────────────────────────
    def measurement(self, batch_id: str, part_id: str, part_index: int,
                    product_code: str, stage: str, machine_id: str,
                    work_center: str, parameter: str, value: float,
                    nominal: float, tolerance: float, unit: str,
                    result: str, event_time: datetime,
                    reason: str | None = None,
                    source_machine_id: str | None = None,
                    scenario_id: str | None = None) -> None:
        self._measurement_buffer.append({
            "batch_id":          batch_id,
            "part_id":           part_id,
            "part_index":        part_index,
            "product_code":      product_code,
            "stage":             stage,
            "machine_id":        machine_id,
            "work_center":       work_center,
            "parameter":         parameter,
            "value":             value,
            "nominal":           nominal,
            "tolerance":         tolerance,
            "unit":              unit,
            "result":            result,
            "reason":            reason,
            "source_machine_id": source_machine_id,
            "scenario_id":       scenario_id,
            "event_time":        event_time.isoformat(),
        })

    def scenario_event(self, event: str, scenario_id: str, machine_id: str,
                       scenario_type: str, severity: str | None,
                       parts_limit: int | None, event_time: datetime,
                       details: dict | None = None) -> None:
        """Лог запуска/остановки сценария — для ML и для трассировки."""
        self._post(self.qa + "/scenario-event", {
            "event":         event,
            "scenario_id":   scenario_id,
            "machine_id":    machine_id,
            "scenario_type": scenario_type,
            "severity":      severity,
            "parts_limit":   parts_limit,
            "details":       details or {},
            "event_time":    event_time.isoformat(),
        })

    def inspection_result(self, part_id: str, batch_id: str, work_center: str,
                          decision: str, event_time: datetime,
                          reason: str | None = None,
                          inspector_id: str | None = None) -> None:
        self._post(self.qa + "/inspection-result", {
            "part_id":      part_id,
            "batch_id":     batch_id,
            "work_center":  work_center,
            "decision":     decision,
            "reason":       reason,
            "inspector_id": inspector_id,
            "event_time":   event_time.isoformat(),
        })

    # ─── MAINTENANCE ───────────────────────────────────────────
    def work_order_creation(self, wo, event_time: datetime) -> None:
        self._post(self.mt + "/work-order-creation", {
            "wo_id":      wo.wo_id,
            "machine_id": wo.machine_id,
            "type":       wo.type,
            "priority":   wo.priority,
            "reason":     wo.reason,
            "event_time": event_time.isoformat(),
        })

    def work_order_assignment(self, wo, event_time: datetime) -> None:
        self._post(self.mt + "/work-order-assignment", {
            "wo_id":      wo.wo_id,
            "brigade_id": wo.assigned_brigade_id,
            "event_time": event_time.isoformat(),
        })

    def work_order_completion(self, wo, duration_min: float, event_time: datetime,
                              parts_replaced: list[str] | None = None) -> None:
        self._post(self.mt + "/work-order-completion", {
            "wo_id":          wo.wo_id,
            "duration_min":   duration_min,
            "parts_replaced": parts_replaced or [],
            "event_time":     event_time.isoformat(),
        })
