"""SimulationLoop — главный цикл в фоновом потоке."""
import threading
import time

import config


class SimulationLoop:
    def __init__(self, state, subsystems: list):
        self.state = state
        self.subsystems = subsystems
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        # Если поток уже жив (в т.ч. на паузе) — это «возобновить».
        if self.is_alive():
            self.state.resume()
            return
        self._stop_event.clear()
        self.state.resume()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="sim-loop")
        self._thread.start()

    def pause(self) -> None:
        """Пауза: замораживает state (часы + подсистемы), поток цикла остаётся
        жив (в отличие от stop)."""
        self.state.pause()

    def stop(self) -> None:
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        self.state.pause()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def fast_forward(self, minutes: float) -> None:
        """Перемотать виртуальное время на `minutes` минут, СГЕНЕРИРОВАВ все логи.

        Ставит перемотку в очередь state; прокручивает её главный цикл (drain),
        поэтому тиканье подсистем остаётся в одном потоке (без гонок). Блокирует
        вызывающий поток (HTTP-обработчик) до завершения. Если цикл не запущен —
        поднимает его на паузе, прокручивает и оставляет на паузе.
        """
        was_stopped = not self.is_alive()
        if was_stopped:
            self._stop_event.clear()
            self.state.pause()
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="sim-loop")
            self._thread.start()
        self.state.request_fast_forward(minutes)
        deadline = time.time() + 60.0   # предохранитель от вечного ожидания
        while self.state.ff_pending() > 0 and time.time() < deadline:
            time.sleep(0.02)
        # Перемотка из stopped → остаёмся на паузе (поток уже жив).
        if was_stopped:
            self.state.pause()

    def restart(self) -> None:
        if self.state.virtual_time == config.SIM_START_TIME:
            return
        self.state.reset()
        for sub in self.subsystems:
            reset_fn = getattr(sub, "reset", None)
            if callable(reset_fn):
                reset_fn()
        client = self._find_client()
        if client is not None:
            client.reset_remote_state()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ─── main loop body ──────────────────────────────────────
    def _run(self) -> None:
        client = self._find_client()
        while not self._stop_event.is_set():
            # Перемотка вперёд: если в очереди есть виртуальное время —
            # прокручиваем его целиком БЕЗ реального sleep (генерируя логи),
            # затем продолжаем обычный цикл.
            if self.state.ff_pending() > 0:
                self._drain_fast_forward(client)
                continue
            tick_started = time.perf_counter()
            try:
                self.state.advance_time(config.TICK_REAL_SEC)
                if self.state.running:
                    now = self.state.virtual_time
                    for sub in self.subsystems:
                        try:
                            sub.tick(now)
                        except Exception as exc:
                            raise RuntimeError(f"subsystem {sub.name} failed: {exc}") from exc
                    if client is not None:
                        client.flush()
            except Exception as exc:
                raise RuntimeError(f"loop iteration failed: {exc}") from exc
            # Adaptive sleep: спим только остаток до TICK_REAL_SEC.
            # Если тик уже длился дольше — не спим, держим скорость 1000x максимально близко.
            elapsed = time.perf_counter() - tick_started
            sleep_left = config.TICK_REAL_SEC - elapsed
            if sleep_left > 0:
                time.sleep(sleep_left)

    def _drain_fast_forward(self, client) -> None:
        """Прокрутить всю очередь перемотки чанками по FAST_FORWARD_STEP_SEC.

        На время перемотки принудительно держим running=True (иначе подсистемы
        не тикали бы). Если во время перемотки сработала авто-пауза сценария
        (state.running стал False внутри tick) — останавливаем перемотку и
        оставляем симулятор на паузе.
        """
        was_running = self.state.running
        self.state.resume()
        auto_paused = False
        while not self._stop_event.is_set():
            dt = self.state.take_ff_chunk(config.FAST_FORWARD_STEP_SEC)
            if dt <= 0:
                break
            now = self.state.virtual_time
            for sub in self.subsystems:
                try:
                    sub.tick(now)
                except Exception as exc:
                    raise RuntimeError(f"subsystem {sub.name} failed: {exc}") from exc
            if client is not None:
                client.flush()
            if not self.state.running:   # авто-пауза сценария во время перемотки
                auto_paused = True
                self.state.clear_ff()
                break
        if auto_paused or not was_running:
            self.state.pause()
        else:
            self.state.resume()

    def _find_client(self):
        """Достать ссылку на FactoryClient из любой подсистемы, у которой он есть."""
        for sub in self.subsystems:
            client = getattr(sub, "client", None)
            if client is not None:
                return client
        return None