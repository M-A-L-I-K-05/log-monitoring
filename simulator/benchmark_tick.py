"""Замер времени одного тика симуляции под реальной нагрузкой.

Сначала прокручиваем симулятор много тиков (warmup), чтобы появились заказы,
партии и запущенные станки. Только потом измеряем нагруженные тики.

ВАЖНО: для корректных цифр HTTP-нагрузки нужны запущенные backend-сервисы
(docker compose up). Без них POST-ы упадут — симулятор продолжит работать,
но число отразит только CPU-часть тика, без сети и БД.
"""
import time

from client import FactoryClient
from state import SimulationState
from subsystems.equipment import EquipmentSubsystem
from subsystems.furnace import FurnaceSubsystem
from subsystems.maintenance import MaintenanceSubsystem
from subsystems.orders import OrdersSubsystem
from subsystems.production import ProductionDispatcher
from subsystems.quality import QualitySubsystem
from subsystems.scenarios import ScenariosController

SPEED = 1000          # на чём гоняем — симулирует pressure для 1000x
WARMUP_TICKS = 500    # ~14 виртуальных часов: успеют появиться заказы и запуститься станки
MEASURE_TICKS = 30

state = SimulationState()
state.set_speed(SPEED)
client = FactoryClient()
scenarios = ScenariosController(state)

subsystems = [
    OrdersSubsystem(state, client),
    ProductionDispatcher(state, client),
    EquipmentSubsystem(state, client),
    FurnaceSubsystem(state, client),
    QualitySubsystem(state, client),
    MaintenanceSubsystem(state, client),
    scenarios,
]

state.resume()


def run_tick() -> None:
    state.advance_time(0.1)
    now = state.virtual_time
    for sub in subsystems:
        try:
            sub.tick(now)
        except Exception:
            pass
    try:
        client.flush()
    except Exception:
        pass


# ─── Warmup ─────────────────────────────────────────────────
print(f"warmup {WARMUP_TICKS} тиков на скорости {SPEED}x (без замеров)...")
warmup_start = time.perf_counter()
for _ in range(WARMUP_TICKS):
    run_tick()
warmup_elapsed = time.perf_counter() - warmup_start

running = [m for m in state.machines.values() if m.state == "running"]
n_running = len(running)
n_setup = sum(1 for m in state.machines.values() if m.state == "setup")
n_idle = sum(1 for m in state.machines.values() if m.state == "idle")
n_cooldown = sum(1 for m in state.machines.values() if m.state == "cooldown")

print(f"warmup done за {warmup_elapsed:.1f} сек wall-clock")
print(f"  running: {n_running}/{len(state.machines)}")
print(f"  setup:   {n_setup}")
print(f"  idle:    {n_idle}")
print(f"  cooldown:{n_cooldown}")
print(f"  активных партий: {len(state.batches)}")
print(f"  виртуальное время: {state.virtual_time.isoformat()}")
print()

if n_running == 0:
    print("ВНИМАНИЕ: ни одного running станка — замер будет нерепрезентативный.")
    print("Попробуй увеличить WARMUP_TICKS или SPEED.")
    print()

# ─── Замер ──────────────────────────────────────────────────
print(f"замер {MEASURE_TICKS} тиков...")
times = []
for i in range(MEASURE_TICKS):
    start = time.perf_counter()
    run_tick()
    elapsed_ms = (time.perf_counter() - start) * 1000
    times.append(elapsed_ms)
    print(f"тик {i + 1:02d}: {elapsed_ms:.3f} мс")

avg = sum(times) / len(times)
print()
print(f"среднее: {avg:.3f} мс")
print(f"минимум: {min(times):.3f} мс")
print(f"максимум: {max(times):.3f} мс")
print()
print(f"оценка эффективной скорости при тике 100мс:")
print(f"  100 виртсек / ({avg:.1f} + 100) мс = {100000.0 / (avg + 100):.0f}x")
