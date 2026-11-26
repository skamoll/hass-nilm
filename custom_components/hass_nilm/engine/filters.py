"""Edge-preserving streaming filters for power time series."""

from __future__ import annotations

from collections import deque
from statistics import median


class MedianFilter:
    """Bounded O(W) median filter that rejects single-sample glitches while keeping sharp steps."""

    def __init__(self, window: int = 3) -> None:
        normalized = max(1, int(window))
        if normalized % 2 == 0:
            normalized += 1
        self._window = min(normalized, 15)
        self._buffer: deque[float] = deque(maxlen=self._window)

    @property
    def window(self) -> int:
        """Return effective odd window size."""
        return self._window

    def reset(self) -> None:
        """Clear buffered samples."""
        self._buffer.clear()

    def update(self, value: float) -> float:
        """Push a new sample and return the current median."""
        self._buffer.append(float(value))
        if len(self._buffer) == 1:
            return self._buffer[0]
        return float(median(self._buffer))
