"""ProductionDispatcher: назначает партии из очередей на свободные станки.

Маршрут единый: turning → hobbing → shaving → heat_treatment → grinding → inspection.
Печь обрабатывается отдельной подсистемой FurnaceSubsystem.
"""
import random
from datetime import datetime

import config


class ProductionDispatcher:
    name = "production"

    # Соответствие очередь → тип станка
    # waiting_furnace обрабатывается FurnaceSubsystem, тут пропускается.
    QUEUE_TO_MACHINE_TYPE = {
        "pending":          "turning",
        "queue_hobbing":    "hobbing",
        "queue_shaving":    "shaving",
        "queue_grinding":   "grinding",
        "queue_inspection": "inspection",
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

        # Пересортировка по приоритету: rush → urgent → normal.
        # sorted в Python стабильна → внутри одного приоритета сохраняется FIFO.
        sorted_batches = sorted(queue, key=lambda b: config.PRIORITY_ORDER[b.priority])
        queue.clear()
        queue.extend(sorted_batches)

        # пока есть свободные станки и партии в очереди — назначаем
        while queue and free_machines:
            machine = free_machines.pop(0)
            batch = queue.popleft()
            self._start_batch_on_machine(machine, batch, now)

    def _start_batch_on_machine(self, machine, batch, now: datetime) -> None:
        from_stage = batch.stage
        new_stage = machine.work_center  # совпадает с work_center станка

        # обновляем партию
        batch.stage = new_stage
        batch.current_machine_id = machine.machine_id
        batch.stage_started_at = now
        batch.parts_done_in_stage = 0

        # если это финальная инспекция — выбираем 10% выборку
        if new_stage == "inspection":
            n_sample = max(1, int(round(batch.quantity * config.INSPECTION_SAMPLE_RATIO)))
            batch.inspection_sample_indices = set(
                random.sample(range(1, batch.quantity + 1), n_sample)
            )
            batch.inspection_sampled_done = set()

        # обновляем станок
        old_state = machine.state
        machine.state = "setup"
        machine.state_changed_at = now
        machine.current_batch_id = batch.batch_id
        machine.parts_done_in_batch = 0
        machine.last_sensor_sent_at = None

        # события
        self.client.batch_move(batch, from_center=from_stage,
                               to_center=new_stage, event_time=now)
        self.client.state_change(machine, old_state=old_state, new_state="setup",
                                 event_time=now,
                                 details={"batch_id": batch.batch_id})

