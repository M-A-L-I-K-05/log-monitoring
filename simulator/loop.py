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
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self.state.resume()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="sim-loop")
        self._thread.start()

    def stop(self) -> None:
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        self.state.pause()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

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

    def _find_client(self):
        """Достать ссылку на FactoryClient из любой подсистемы, у которой он есть."""
        for sub in self.subsystems:
            client = getattr(sub, "client", None)
            if client is not None:
                return client
        return None