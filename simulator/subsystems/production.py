"""ProductionDispatcher: назначает партии из очередей на свободные станки.

Маршрут единый: turning → hobbing → shaving → heat_treatment → grinding → inspection.
МЕЖДУ ЭТАПАМИ партия проходит измерение на M-GMM (queue_measurement → idle M-GMM).
Печь обрабатывается отдельной подсистемой FurnaceSubsystem.

quality_hold: партия с этим флагом НЕ назначается ни на один производственный
станок — она ждёт, пока quality снимет hold (после измерения).
"""
import random
from datetime import datetime, timedelta

import config


class ProductionDispatcher:
    name = "production"

    # Соответствие очередь → тип станка.
    # waiting_furnace обрабатывается FurnaceSubsystem, тут пропускается.
    QUEUE_TO_MACHINE_TYPE = {
        "pending":           "turning",
        "queue_hobbing":     "hobbing",
        "queue_shaving":     "shaving",
        "queue_grinding":    "grinding",
        "queue_measurement": "inspection",   # партии, ожидающие M-GMM
        "queue_inspection":  "inspection",   # финальная инспекция (тоже M-GMM)
    }

    def __init__(self, state, client):
        self.state = state
        self.client = client

    def tick(self, now: datetime) -> None:
        with self.state.lock:
            for queue_key, machine_type in self.QUEUE_TO_MACHINE_TYPE.items():
                self._assign_from_queue(queue_key, machine_type, now)

    # ─── helpers ──────────────────────────────────────────────
    def _assign_from_queue(self, queue_key: str, machine_type: str,
                           now: datetime) -> None:
        queue = self.state.queues[queue_key]
        if not queue:
            return

        free_machines = [
            m for m in self.state.machines.values()
            if m.machine_type == machine_type and m.state == "idle"
        ]
        if not free_machines:
            return

        # Семантика quality_hold:
        # - На производственных очередях (pending/queue_hobbing/queue_shaving/
        #   queue_grinding/queue_inspection): partия с hold=True пропускается;
        #   она ещё не прошла измерение после предыдущего этапа.
        # - На очереди queue_measurement: hold НЕ применяется (партия как раз
        #   ждёт назначения на M-GMM, чтобы измерение прошло и сняло hold).
        ignore_hold = (queue_key == "queue_measurement")

        # Пересортировка по приоритету: rush → urgent → normal.
        sorted_batches = sorted(queue, key=lambda b: config.PRIORITY_ORDER[b.priority])
        queue.clear()
        queue.extend(sorted_batches)

        # пока есть свободные станки и партии в очереди — назначаем
        while free_machines:
            batch = self._pop_next_ready(queue, ignore_hold)
            if batch is None:
                break
            machine = free_machines.pop(0)
            self._start_batch_on_machine(machine, batch, now, queue_key)

    def _pop_next_ready(self, queue, ignore_hold: bool):
        """Берём первую партию: с hold (если ignore_hold) или без."""
        for i, b in enumerate(queue):
            if ignore_hold or not b.quality_hold:
                del queue[i]
                return b
        return None

    # ─── запуск партии на станке ─────────────────────────────
    def _start_batch_on_machine(self, machine, batch, now: datetime,
                                source_queue: str) -> None:
        from_stage = batch.stage

        # M-GMM: партия пришла на измерение.
        if machine.machine_type == "inspection":
            stage_after = batch.last_processed_stage or "inspection"
            # для очереди queue_inspection (финал) stage_after = "inspection"
            if source_queue == "queue_inspection":
                stage_after = "inspection"

            new_stage = "measurement" if stage_after != "inspection" else "inspection"
            batch.stage = new_stage
            batch.current_machine_id = machine.machine_id
            batch.stage_started_at = now
            batch.parts_done_in_stage = 0

            # План измерения строит equipment при переходе setup→running
            # (когда зонд откалиброван). Здесь фиксируем только целевой этап
            # и сбрасываем счётчики поштучного измерения.
            machine.measuring_after_stage = stage_after
            machine.measurement_plan = []
            machine.measurement_done = 0
            machine.measurement_total = 0
        else:
            # Обычный станок: запускаем обработку партии.
            new_stage = machine.work_center
            batch.stage = new_stage
            batch.current_machine_id = machine.machine_id
            batch.stage_started_at = now
            batch.parts_done_in_stage = 0
            # сбрасываем пометки прошлого этапа (новые ставятся в equipment)
            # ВАЖНО: не очищаем scenario_marked_indices — там detals прошлых
            # этапов, которые нужны quality для своих stage. Чистим только
            # последний обработавший станок-id (он перезапишется в _transition).
            machine.measuring_after_stage = None
            machine.measurement_plan = []
            machine.measurement_done = 0
            machine.measurement_total = 0

        # обновляем станок
        old_state = machine.state
        machine.state = "setup"
        machine.state_changed_at = now
        machine.current_batch_id = batch.batch_id
        machine.parts_done_in_batch = 0
        machine.last_sensor_sent_at = None
        # сбрасываем флаг сценария-завершения, только если на этом станке
        # сейчас нет активного сценария (он мог быть запущен заранее)
        # active_scenario_id мы НЕ сбрасываем — он держится между партиями
        # пока сам сценарий не auto_completed.

        self.client.batch_move(batch, from_center=from_stage,
                               to_center=batch.stage, event_time=now)
        self.client.state_change(machine, old_state=old_state, new_state="setup",
                                 event_time=now,
                                 details={"batch_id": batch.batch_id,
                                          "role": ("measurement" if machine.machine_type == "inspection" else "processing")})

