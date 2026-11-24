"""Time sources. Production uses wall-clock time; tests and simulations use a
manually advanced clock so runs are deterministic and fast."""
import time


class SystemClock:
    def now(self) -> float:
        return time.time()


class SimClock:
    def __init__(self, start: float = 0.0):
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt
