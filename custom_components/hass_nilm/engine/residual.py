"""Hierarchical residual power computation with parent-child skew protection."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from .filters import MedianFilter
from .models import LineSample, SourceLineConfig


@dataclass(frozen=True, slots=True)
class ResidualResult:
    """Computed residual metrics for one LineSample."""

    parent_power_w: float
    submeters_power_w: float
    raw_residual_w: float
    filtered_residual_w: float
    skew_guard_active: bool


class ResidualTracker:
    """Subtract known child sub-meters from a parent meter while guarding against polling skew."""

    def __init__(self, config: SourceLineConfig) -> None:
        self._config = config
        self._filter = MedianFilter(config.median_window)
        # (timestamp, parent_w, submeters_sum_w) within skew_hold_s plus one older baseline
        self._history: deque[tuple[datetime, float, float]] = deque(maxlen=256)
        self._prev_parent_w: float | None = None
        self._prev_submeters_sum_w: float = 0.0
        self._stable_residual_w: float = 0.0
        self._skew_until: datetime | None = None
        self._skew_anchor_parent_w: float = 0.0
        self._skew_anchor_sub_w: float = 0.0

    def update(self, sample: LineSample) -> ResidualResult:
        """Process a LineSample and return raw + skew-safe filtered residual power."""
        parent_w = max(0.0, float(sample.parent_power_w))
        sub_sum_w = sum(max(0.0, float(s.power_w)) for s in sample.submeters)
        raw_residual_w = max(0.0, parent_w - sub_sum_w - self._config.baseline_floor_w)

        if self._prev_parent_w is not None and self._config.skew_hold_s > 0:
            parent_delta = parent_w - self._prev_parent_w
            sub_delta = sub_sum_w - self._prev_submeters_sum_w
            children_jumped = abs(sub_delta) >= self._config.skew_delta_threshold_w
            # Only arm skew hold if parent did NOT move in lockstep with the child transition
            # and the child is not simply catching up with an earlier parent move.
            if (
                children_jumped
                and abs(parent_delta - sub_delta) >= self._config.skew_delta_threshold_w
                and not self._is_child_catch_up(parent_w, sub_sum_w)
            ):
                self._skew_until = sample.timestamp + timedelta(seconds=self._config.skew_hold_s)
                self._skew_anchor_parent_w = self._prev_parent_w
                self._skew_anchor_sub_w = self._prev_submeters_sum_w

        skew_active = False
        if self._skew_until is not None:
            if sample.timestamp < self._skew_until:
                total_parent_move = parent_w - self._skew_anchor_parent_w
                total_sub_move = sub_sum_w - self._skew_anchor_sub_w
                if abs(total_parent_move - total_sub_move) < self._config.skew_delta_threshold_w:
                    # Parent caught up with child transition early; release skew hold immediately.
                    self._skew_until = None
                else:
                    skew_active = True
            else:
                self._skew_until = None

        effective_residual_w = self._stable_residual_w if skew_active else raw_residual_w
        filtered_w = self._filter.update(effective_residual_w)

        if not skew_active:
            self._stable_residual_w = filtered_w
        self._prev_parent_w = parent_w
        self._prev_submeters_sum_w = sub_sum_w
        self._remember(sample.timestamp, parent_w, sub_sum_w)

        return ResidualResult(
            parent_power_w=round(parent_w, 2),
            submeters_power_w=round(sub_sum_w, 2),
            raw_residual_w=round(raw_residual_w, 2),
            filtered_residual_w=round(max(0.0, filtered_w), 2),
            skew_guard_active=skew_active,
        )

    def _remember(self, ts: datetime, parent_w: float, sub_sum_w: float) -> None:
        """Keep samples inside skew_hold_s plus the latest older one as baseline."""
        self._history.append((ts, parent_w, sub_sum_w))
        cutoff = ts - timedelta(seconds=self._config.skew_hold_s)
        while len(self._history) >= 2 and self._history[1][0] <= cutoff:
            self._history.popleft()

    def _is_child_catch_up(self, parent_w: float, sub_sum_w: float) -> bool:
        """True when parent already moved by the same amount within skew_hold_s."""
        threshold = self._config.skew_delta_threshold_w
        return any(
            abs((parent_w - hist_parent) - (sub_sum_w - hist_sub)) < threshold
            for _, hist_parent, hist_sub in self._history
        )
