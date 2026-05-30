"""Batch — состояние одной партии в памяти симулятора."""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Batch:
    batch_id: str
    order_id: str
    product_code: str
    priority: str
    quantity: int

    # Текущая стадия
    stage: str = "pending"  # pending|turning|hobbing|shaving|waiting_furnace|heat_treatment|grinding|inspection|done
    current_machine_id: str | None = None

    created_at: datetime | None = None
    stage_started_at: datetime | None = None
    parts_done_in_stage: int = 0

    # Какой этап только что закончен (для маршрутизации между обработкой
    # и измерением: после стадии X partия идёт на M-GMM с last_processed_stage=X).
    last_processed_stage: str | None = None
    # Станок, на котором партия проходила last_processed_stage.
    # Нужен quality для корректного source_machine_id в БД для scenario-fail
    # измерений.
    last_processed_machine_id: str | None = None

    # Партия не уходит на следующий этап, пока quality не отпустит флаг.
    # Ставится equipment, снимается quality.
    quality_hold: bool = False

    # ── Выбывшие/бракованные индексы деталей и пометки сценария ──
    # failed_indices: детали, которые не пойдут дальше (брак). После выбытия
    # «годное количество» = quantity - len(failed_indices).
    failed_indices: set[int] = field(default_factory=set)
    # scenario_marked_indices[part_idx] = scenario_id, при обработке под сценарием.
    scenario_marked_indices: dict[int, str] = field(default_factory=dict)
    # Накопление брака по причинам (для UI и summary в логах).
    defects_by_reason: dict[str, int] = field(default_factory=dict)

    # ── Заморозка партии (см. equipment._tick_running) ──
    # is_frozen=True означает: станок встал в maintenance, партия зависла на
    # этом станке (не дорабатывается). После ремонта → False и продолжение
    # с текущего parts_done_in_batch у станка.
    is_frozen: bool = False
    frozen_reason: str | None = None  # "tool_wear" | "scenario:<name>" — для UI

    # Накопленный брак (по всем причинам, для UI-счётчика)
    fails_count: int = 0

    # ── Годное количество ──
    @property
    def effective_quantity(self) -> int:
        """Сколько деталей реально осталось в потоке = исходное минус выбывшие
        (брак с прошлых этапов). Именно столько обрабатывается на следующих
        станках, отражается в прогрессе и определяет время этапа."""
        return self.quantity - len(self.failed_indices)

    def good_indices(self) -> list[int]:
        """Отсортированные индексы ещё годных деталей (1..quantity без брака)."""
        if not self.failed_indices:
            return list(range(1, self.quantity + 1))
        return [i for i in range(1, self.quantity + 1)
                if i not in self.failed_indices]
