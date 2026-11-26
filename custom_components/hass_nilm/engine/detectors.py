"""Deterministic appliance detectors: step, cycling, and multiphase."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime

from .models import (
    UNKNOWN_PROGRAM,
    ApplianceProfile,
    Archetype,
    CompletedCycle,
    ProgramFingerprint,
)

_RAMP_WINDOW_SECONDS = 15.0


@dataclass(slots=True)
class DetectorStepOutput:
    """Raw output of a detector before line-level conservation clamping."""

    is_running: bool
    phase: str
    raw_power_w: float
    provisional_energy_commit_kwh: float = 0.0
    cycle_start_time: datetime | None = None
    high_blocks_in_cycle: int = 0
    completed_cycle: CompletedCycle | None = None


def match_program_fingerprint(
    programs: tuple[ProgramFingerprint, ...],
    *,
    energy_kwh: float,
    duration_min: float,
    high_blocks: int,
) -> tuple[str, float]:
    """Match cycle metrics against labeled ProgramFingerprints with ambiguity protection."""
    if not programs:
        return UNKNOWN_PROGRAM, 0.0

    candidates: list[tuple[float, str]] = []
    for fp in programs:
        if fp.expected_high_blocks is not None and high_blocks != fp.expected_high_blocks:
            continue
        e_tol = max(0.01, fp.energy_tolerance_kwh)
        d_tol = max(1.0, fp.duration_tolerance_min)
        e_diff = abs(energy_kwh - fp.expected_energy_kwh)
        d_diff = abs(duration_min - fp.expected_duration_min)
        if e_diff > e_tol or d_diff > d_tol:
            continue
        norm_dist = 0.6 * (e_diff / e_tol) + 0.4 * (d_diff / d_tol)
        confidence = max(0.05, min(1.0, 1.0 - 0.5 * norm_dist))
        candidates.append((confidence, fp.name))

    if not candidates:
        return UNKNOWN_PROGRAM, 0.0

    candidates.sort(key=lambda item: item[0], reverse=True)
    best_conf, best_name = candidates[0]
    if len(candidates) >= 2 and (best_conf - candidates[1][0]) < 0.12:
        return UNKNOWN_PROGRAM, round(best_conf, 3)
    return best_name, round(best_conf, 3)


class BaseApplianceDetector:
    """Common state and lifecycle hooks for an appliance detector."""

    def __init__(self, profile: ApplianceProfile, *, line_skew_hold_s: float = 0.0) -> None:
        self.profile = profile
        self._confirm_hold_s = (
            profile.min_on_s if profile.dedicated_meter else max(profile.min_on_s, line_skew_hold_s)
        )
        self._recent: deque[tuple[datetime, float]] = deque(maxlen=64)
        self.total_energy_kwh: float = 0.0
        self.current_cycle_energy_kwh: float = 0.0
        self.last_cycle: CompletedCycle | None = None
        self._last_int_ts: datetime | None = None
        self._last_clamped_w: float = 0.0
        self._defer_energy: bool = profile.max_duration_s > 0.0

    def restore_total_energy(self, energy_kwh: float) -> None:
        """Seed cumulative energy from Home Assistant RestoreEntity."""
        if energy_kwh > self.total_energy_kwh:
            self.total_energy_kwh = float(energy_kwh)

    def integrate_clamped_power(
        self,
        timestamp: datetime,
        clamped_power_w: float,
        *,
        is_running: bool,
        max_gap_s: float,
        just_confirmed: bool = False,
    ) -> float:
        """Integrate conservation-clamped power via trapezoidal Riemann sum."""
        delta_kwh = 0.0
        if not just_confirmed and self._last_int_ts is not None:
            dt_s = (timestamp - self._last_int_ts).total_seconds()
            if 0.0 < dt_s <= max_gap_s and (self._last_clamped_w > 0.0 or clamped_power_w > 0.0):
                delta_kwh = ((self._last_clamped_w + clamped_power_w) * 0.5 * dt_s) / 3_600_000.0
                if delta_kwh > 0.0:
                    if is_running and self._defer_energy:
                        # held until the run proves it is this appliance
                        self.current_cycle_energy_kwh += delta_kwh
                    else:
                        self.total_energy_kwh += delta_kwh
                        if is_running:
                            self.current_cycle_energy_kwh += delta_kwh
        self._last_int_ts = timestamp
        self._last_clamped_w = clamped_power_w
        return delta_kwh

    def _push_recent(self, ts: datetime, power_w: float) -> tuple[float, float]:
        """Record sample in ramp window and return (min_recent_w, max_recent_w) before ts."""
        cutoff = ts.timestamp() - _RAMP_WINDOW_SECONDS
        # Keep the latest sample older than the window as baseline reference: HA reports
        # on change, so that value is still the line level when sampling is sparse.
        while len(self._recent) >= 2 and self._recent[1][0].timestamp() <= cutoff:
            self._recent.popleft()
        if self._recent:
            vals = [p for _, p in self._recent]
            min_w, max_w = min(vals), max(vals)
        else:
            min_w = max_w = power_w
        self._recent.append((ts, power_w))
        return min_w, max_w

    def step(self, ts: datetime, residual_w: float, *, max_gap_s: float) -> DetectorStepOutput:
        """Advance detector state by one residual sample."""
        raise NotImplementedError


class StepDetector(BaseApplianceDetector):
    """NILM Blueprint-style rising/falling edge detector for single-state loads."""

    def __init__(self, profile: ApplianceProfile, *, line_skew_hold_s: float = 0.0) -> None:
        super().__init__(profile, line_skew_hold_s=line_skew_hold_s)
        self._state: str = "off"  # off | pending | running
        self._start_ts: datetime | None = None
        self._latched_w: float = 0.0
        self._peak_w: float = 0.0
        self._pending_prev_ts: datetime | None = None
        self._pending_prev_w: float = 0.0
        self._pending_kwh: float = 0.0
        self._low_since: datetime | None = None

    def _matches_on_edge(self, residual_w: float, min_recent_w: float) -> tuple[bool, float]:
        p = self.profile
        if p.dedicated_meter:
            if p.min_power_w <= residual_w <= p.max_power_w + p.edge_tolerance_w:
                return True, min(residual_w, p.max_power_w)
            return False, 0.0
        delta_up = residual_w - min_recent_w
        low = max(1.0, p.min_power_w - p.edge_tolerance_w)
        high = p.max_power_w + p.edge_tolerance_w
        if residual_w >= p.min_power_w * 0.85 and low <= delta_up <= high:
            target_w = p.nominal_power_w if p.nominal_power_w is not None else delta_up
            return True, max(p.min_power_w, min(target_w, p.max_power_w, residual_w))
        return False, 0.0

    def _matches_off_condition(self, ts: datetime, residual_w: float, max_recent_w: float) -> bool:
        p = self.profile
        assert self._start_ts is not None
        if (ts - self._start_ts).total_seconds() >= p.max_on_s:
            return True
        if residual_w < p.min_power_w * 0.55:
            return True
        if not p.dedicated_meter:
            delta_down = max_recent_w - residual_w
            tol = max(p.edge_tolerance_w, self._latched_w * 0.25)
            if abs(delta_down - self._latched_w) <= tol:
                return True
        return False

    def step(self, ts: datetime, residual_w: float, *, max_gap_s: float) -> DetectorStepOutput:
        min_recent_w, max_recent_w = self._push_recent(ts, residual_w)
        p = self.profile

        if self._state == "off":
            matched, latched_w = self._matches_on_edge(residual_w, min_recent_w)
            if matched:
                self._start_ts = ts
                self._latched_w = latched_w
                self._peak_w = residual_w if p.dedicated_meter else latched_w
                self._pending_prev_ts = ts
                self._pending_prev_w = latched_w
                self._pending_kwh = 0.0
                if self._confirm_hold_s <= 0.0:
                    self._state = "running"
                    self.current_cycle_energy_kwh = 0.0
                    return DetectorStepOutput(
                        is_running=True,
                        phase="running",
                        raw_power_w=latched_w,
                        cycle_start_time=ts,
                        high_blocks_in_cycle=1 if latched_w >= p.high_block_threshold_w else 0,
                    )
                self._state = "pending"
            return DetectorStepOutput(is_running=False, phase="off", raw_power_w=0.0)

        if self._state == "pending":
            assert self._start_ts is not None
            if residual_w < p.min_power_w * 0.65:
                if self._low_since is None:
                    self._low_since = ts
                if (ts - self._low_since).total_seconds() >= p.off_debounce_s:
                    self._state = "off"
                    self._start_ts = None
                    self._pending_kwh = 0.0
                    self._low_since = None
                    return DetectorStepOutput(is_running=False, phase="off", raw_power_w=0.0)
            else:
                self._low_since = None

            cur_w = (
                residual_w
                if p.dedicated_meter
                else min(residual_w, p.nominal_power_w or self._latched_w)
            )
            self._peak_w = max(self._peak_w, cur_w)
            if self._pending_prev_ts is not None:
                dt_s = (ts - self._pending_prev_ts).total_seconds()
                if 0.0 < dt_s <= max_gap_s:
                    self._pending_kwh += ((self._pending_prev_w + cur_w) * 0.5 * dt_s) / 3_600_000.0
            self._pending_prev_ts = ts
            self._pending_prev_w = cur_w

            if (ts - self._start_ts).total_seconds() >= self._confirm_hold_s:
                self._state = "running"
                if not self._defer_energy:
                    self.total_energy_kwh += self._pending_kwh
                self.current_cycle_energy_kwh = self._pending_kwh
                commit = self._pending_kwh
                self._pending_kwh = 0.0
                return DetectorStepOutput(
                    is_running=True,
                    phase="running",
                    raw_power_w=cur_w,
                    provisional_energy_commit_kwh=commit,
                    cycle_start_time=self._start_ts,
                    high_blocks_in_cycle=1 if self._peak_w >= p.high_block_threshold_w else 0,
                )
            return DetectorStepOutput(is_running=False, phase="off", raw_power_w=0.0)

        # _state == "running"
        assert self._start_ts is not None
        if p.max_duration_s > 0.0 and (ts - self._start_ts).total_seconds() >= p.max_duration_s:
            # Overran: this run belongs to another appliance. Drop the held energy.
            self._state = "off"
            self._start_ts = None
            self.current_cycle_energy_kwh = 0.0
            self._last_clamped_w = 0.0  # no trapezoid tail leaks into the total
            return DetectorStepOutput(is_running=False, phase="off", raw_power_w=0.0)
        off_now = self._matches_off_condition(ts, residual_w, max_recent_w)
        timed_out = (ts - self._start_ts).total_seconds() >= p.max_on_s
        if off_now and not timed_out and p.off_debounce_s > 0.0:
            # Hold through short dips; close at the moment the dip began if it lasts.
            if self._low_since is None:
                self._low_since = ts
            if (ts - self._low_since).total_seconds() < p.off_debounce_s:
                return DetectorStepOutput(
                    is_running=True,
                    phase="running",
                    raw_power_w=min(residual_w, self._latched_w),
                    cycle_start_time=self._start_ts,
                    high_blocks_in_cycle=1 if self._peak_w >= p.high_block_threshold_w else 0,
                )
        elif not off_now:
            self._low_since = None
        if off_now:
            end_ts = self._low_since if (self._low_since and p.off_debounce_s > 0.0) else ts
            self._low_since = None
            dur_min = max(0.0, (end_ts - self._start_ts).total_seconds() / 60.0)
            cycle_kwh = round(self.current_cycle_energy_kwh, 4)
            completed: CompletedCycle | None = None
            valid = cycle_kwh >= p.min_cycle_energy_kwh or p.min_cycle_energy_kwh <= 0.0
            if self._defer_energy and valid:
                self.total_energy_kwh += self.current_cycle_energy_kwh
            if valid:
                hb = 1 if self._peak_w >= p.high_block_threshold_w else 0
                prog, conf = match_program_fingerprint(
                    p.programs,
                    energy_kwh=cycle_kwh,
                    duration_min=dur_min,
                    high_blocks=hb,
                )
                completed = CompletedCycle(
                    appliance_id=p.appliance_id,
                    appliance_name=p.name,
                    start_time=self._start_ts,
                    end_time=end_ts,
                    duration_min=round(dur_min, 2),
                    energy_kwh=cycle_kwh,
                    peak_power_w=round(self._peak_w, 1),
                    high_blocks=hb,
                    phases_seen=("running",),
                    matched_program=prog,
                    confidence=conf,
                )
                self.last_cycle = completed
            self._state = "off"
            self._start_ts = None
            self.current_cycle_energy_kwh = 0.0
            return DetectorStepOutput(
                is_running=False,
                phase="off",
                raw_power_w=0.0,
                completed_cycle=completed,
            )

        if p.dedicated_meter:
            cur_w = min(residual_w, p.max_power_w * 1.25)
        elif p.nominal_power_w is not None:
            cur_w = min(residual_w, p.nominal_power_w)
        else:
            if p.min_power_w <= residual_w <= p.max_power_w:
                self._latched_w = 0.8 * self._latched_w + 0.2 * residual_w
            cur_w = min(residual_w, self._latched_w)
        self._peak_w = max(self._peak_w, cur_w)
        return DetectorStepOutput(
            is_running=True,
            phase="running",
            raw_power_w=cur_w,
            cycle_start_time=self._start_ts,
            high_blocks_in_cycle=1 if self._peak_w >= p.high_block_threshold_w else 0,
        )


class CyclingDetector(BaseApplianceDetector):
    """Thermostatic / pulsed load detector (ovens, induction hobs, fridges, irons)."""

    def __init__(self, profile: ApplianceProfile, *, line_skew_hold_s: float = 0.0) -> None:
        super().__init__(profile, line_skew_hold_s=line_skew_hold_s)
        self._pulse = StepDetector(
            ApplianceProfile(
                appliance_id=profile.appliance_id,
                name=profile.name,
                archetype=Archetype.STEP,
                min_power_w=profile.min_power_w,
                max_power_w=profile.max_power_w,
                nominal_power_w=profile.nominal_power_w,
                edge_tolerance_w=profile.edge_tolerance_w,
                min_on_s=profile.min_on_s,
                max_on_s=profile.max_on_s,
                high_block_threshold_w=profile.high_block_threshold_w,
                min_cycle_energy_kwh=0.0,
                dedicated_meter=profile.dedicated_meter,
            ),
            line_skew_hold_s=line_skew_hold_s,
        )
        self._session_active: bool = False
        self._session_start_ts: datetime | None = None
        self._pause_since_ts: datetime | None = None
        self._last_pulse_end_ts: datetime | None = None
        self._pulse_count: int = 0
        self._session_peak_w: float = 0.0

    def step(self, ts: datetime, residual_w: float, *, max_gap_s: float) -> DetectorStepOutput:
        p = self.profile
        sub = self._pulse.step(ts, residual_w, max_gap_s=max_gap_s)
        if sub.provisional_energy_commit_kwh > 0.0:
            self.total_energy_kwh += sub.provisional_energy_commit_kwh
            self.current_cycle_energy_kwh += sub.provisional_energy_commit_kwh

        if sub.is_running:
            if not self._session_active:
                self._session_active = True
                self._session_start_ts = sub.cycle_start_time or ts
                self._pulse_count = 1
                self._session_peak_w = sub.raw_power_w
            elif self._pause_since_ts is not None:
                self._pulse_count += 1
            self._pause_since_ts = None
            self._last_pulse_end_ts = ts
            self._session_peak_w = max(self._session_peak_w, sub.raw_power_w)

            assert self._session_start_ts is not None
            if (ts - self._session_start_ts).total_seconds() >= p.max_on_s:
                return self._close_session(ts)

            return DetectorStepOutput(
                is_running=True,
                phase="running",
                raw_power_w=sub.raw_power_w,
                cycle_start_time=self._session_start_ts,
                high_blocks_in_cycle=self._pulse_count,
            )

        if not self._session_active:
            return DetectorStepOutput(is_running=False, phase="off", raw_power_w=0.0)

        # Session active, pulse currently off -> paused_thermal
        if self._pause_since_ts is None:
            self._pause_since_ts = ts
            self._last_pulse_end_ts = ts

        pause_s = (ts - self._pause_since_ts).total_seconds()
        assert self._session_start_ts is not None
        total_s = (ts - self._session_start_ts).total_seconds()
        if pause_s >= p.max_pause_s or total_s >= p.max_on_s:
            return self._close_session(self._last_pulse_end_ts or ts)

        return DetectorStepOutput(
            is_running=True,
            phase="paused_thermal",
            raw_power_w=0.0,
            cycle_start_time=self._session_start_ts,
            high_blocks_in_cycle=self._pulse_count,
        )

    def _close_session(self, effective_end_ts: datetime) -> DetectorStepOutput:
        p = self.profile
        assert self._session_start_ts is not None
        dur_min = max(0.0, (effective_end_ts - self._session_start_ts).total_seconds() / 60.0)
        cycle_kwh = round(self.current_cycle_energy_kwh, 4)
        completed: CompletedCycle | None = None
        if cycle_kwh >= p.min_cycle_energy_kwh and self._pulse_count >= max(1, p.min_high_blocks):
            prog, conf = match_program_fingerprint(
                p.programs,
                energy_kwh=cycle_kwh,
                duration_min=dur_min,
                high_blocks=self._pulse_count,
            )
            completed = CompletedCycle(
                appliance_id=p.appliance_id,
                appliance_name=p.name,
                start_time=self._session_start_ts,
                end_time=effective_end_ts,
                duration_min=round(dur_min, 2),
                energy_kwh=cycle_kwh,
                peak_power_w=round(self._session_peak_w, 1),
                high_blocks=self._pulse_count,
                phases_seen=("running", "paused_thermal")
                if self._pulse_count > 1
                else ("running",),
                matched_program=prog,
                confidence=conf,
            )
            self.last_cycle = completed

        self._session_active = False
        self._session_start_ts = None
        self._pause_since_ts = None
        self._last_pulse_end_ts = None
        self._pulse_count = 0
        self._session_peak_w = 0.0
        self.current_cycle_energy_kwh = 0.0
        return DetectorStepOutput(
            is_running=False,
            phase="off",
            raw_power_w=0.0,
            completed_cycle=completed,
        )


class MultiphaseDetector(BaseApplianceDetector):
    """Finite-state detector for multi-phase appliances (washing machine, dishwasher, dryer)."""

    def __init__(self, profile: ApplianceProfile, *, line_skew_hold_s: float = 0.0) -> None:
        super().__init__(profile, line_skew_hold_s=line_skew_hold_s)
        self._state: str = "off"  # off | pending | active
        self._start_ts: datetime | None = None
        self._pending_prev_ts: datetime | None = None
        self._pending_prev_w: float = 0.0
        self._pending_kwh: float = 0.0
        self._idle_since_ts: datetime | None = None
        self._last_active_ts: datetime | None = None
        self._high_since_ts: datetime | None = None
        self._in_high_block: bool = False
        self._high_blocks: int = 0
        self._peak_w: float = 0.0
        self._candidate_phase: str | None = None
        self._candidate_phase_since: datetime | None = None
        self._active_phase: str = "running"
        self._phases_seen: list[str] = []

    def _reset_cycle(self) -> None:
        self._state = "off"
        self._start_ts = None
        self._pending_prev_ts = None
        self._pending_prev_w = 0.0
        self._pending_kwh = 0.0
        self._idle_since_ts = None
        self._last_active_ts = None
        self._high_since_ts = None
        self._in_high_block = False
        self._high_blocks = 0
        self._peak_w = 0.0
        self._candidate_phase = None
        self._candidate_phase_since = None
        self._active_phase = "running"
        self._phases_seen.clear()
        self.current_cycle_energy_kwh = 0.0

    def _record_phase(self, phase: str) -> None:
        if phase not in ("off", "idle_pause") and (
            not self._phases_seen or self._phases_seen[-1] != phase
        ):
            self._phases_seen.append(phase)

    def _resolve_phase(self, ts: datetime, power_w: float) -> str:
        p = self.profile
        if power_w <= p.idle_threshold_w:
            self._candidate_phase = None
            self._candidate_phase_since = None
            return "idle_pause"

        has_seen_high = self._high_blocks > 0 or self._in_high_block
        if p.phases:
            matched_specs = [
                spec
                for spec in p.phases
                if spec.min_power_w <= power_w <= spec.max_power_w
                and (spec.after_high_block is None or spec.after_high_block == has_seen_high)
            ]
            # A duration candidate must hold continuously; drop it once its spec stops matching.
            if self._candidate_phase is not None and all(
                spec.name != self._candidate_phase for spec in matched_specs
            ):
                self._candidate_phase = None
                self._candidate_phase_since = None
            # Evaluate the most specific spec (longest min_duration_s first, e.g. sustained spin)
            matched_specs.sort(key=lambda s: s.min_duration_s, reverse=True)
            for spec in matched_specs:
                if spec.min_duration_s <= 0.0:
                    self._active_phase = spec.name
                    self._record_phase(spec.name)
                    return spec.name
                if self._candidate_phase != spec.name:
                    self._candidate_phase = spec.name
                    self._candidate_phase_since = ts
                elif (
                    self._candidate_phase_since is not None
                    and (ts - self._candidate_phase_since).total_seconds() >= spec.min_duration_s
                ):
                    self._active_phase = spec.name
                    self._record_phase(spec.name)
                    return spec.name
            # Fallback to any zero-duration matching spec while waiting for min_duration_s
            for spec in matched_specs:
                if spec.min_duration_s <= 0.0:
                    return spec.name
            if self._active_phase != "running":
                return self._active_phase

        # Built-in semantic fallback phases when custom PhaseSpecs are not supplied
        if power_w >= p.high_block_threshold_w:
            phase = "heating"
        elif not has_seen_high:
            phase = "prewash"
        else:
            phase = "wash_rinse"
        self._active_phase = phase
        self._record_phase(phase)
        return phase

    def _update_high_blocks(self, ts: datetime, power_w: float) -> None:
        p = self.profile
        if power_w >= p.high_block_threshold_w:
            if self._high_since_ts is None:
                self._high_since_ts = ts
            if (
                not self._in_high_block
                and (ts - self._high_since_ts).total_seconds() >= p.high_block_min_s
            ):
                self._in_high_block = True
                self._high_blocks += 1
        elif power_w < p.high_block_threshold_w * 0.7:
            self._high_since_ts = None
            self._in_high_block = False

    def step(self, ts: datetime, residual_w: float, *, max_gap_s: float) -> DetectorStepOutput:
        p = self.profile
        self._push_recent(ts, residual_w)
        activation_w = max(p.idle_threshold_w, p.min_power_w)

        if self._state == "off":
            if residual_w >= activation_w and residual_w <= p.max_power_w + p.edge_tolerance_w:
                self._start_ts = ts
                self._last_active_ts = ts
                self._peak_w = residual_w
                self._pending_prev_ts = ts
                self._pending_prev_w = min(residual_w, p.max_power_w)
                self._pending_kwh = 0.0
                self._update_high_blocks(ts, residual_w)
                if self._confirm_hold_s <= 0.0:
                    self._state = "active"
                    phase = self._resolve_phase(ts, residual_w)
                    return DetectorStepOutput(
                        is_running=True,
                        phase=phase,
                        raw_power_w=min(residual_w, p.max_power_w),
                        cycle_start_time=ts,
                        high_blocks_in_cycle=self._high_blocks,
                    )
                self._state = "pending"
            return DetectorStepOutput(is_running=False, phase="off", raw_power_w=0.0)

        if self._state == "pending":
            assert self._start_ts is not None
            if residual_w <= p.idle_threshold_w:
                self._reset_cycle()
                return DetectorStepOutput(is_running=False, phase="off", raw_power_w=0.0)

            cur_w = min(residual_w, p.max_power_w)
            self._peak_w = max(self._peak_w, cur_w)
            self._last_active_ts = ts
            self._update_high_blocks(ts, cur_w)
            if self._pending_prev_ts is not None:
                dt_s = (ts - self._pending_prev_ts).total_seconds()
                if 0.0 < dt_s <= max_gap_s:
                    self._pending_kwh += ((self._pending_prev_w + cur_w) * 0.5 * dt_s) / 3_600_000.0
            self._pending_prev_ts = ts
            self._pending_prev_w = cur_w

            if (ts - self._start_ts).total_seconds() >= self._confirm_hold_s:
                self._state = "active"
                self.total_energy_kwh += self._pending_kwh
                self.current_cycle_energy_kwh = self._pending_kwh
                commit = self._pending_kwh
                self._pending_kwh = 0.0
                phase = self._resolve_phase(ts, cur_w)
                return DetectorStepOutput(
                    is_running=True,
                    phase=phase,
                    raw_power_w=cur_w,
                    provisional_energy_commit_kwh=commit,
                    cycle_start_time=self._start_ts,
                    high_blocks_in_cycle=self._high_blocks,
                )
            return DetectorStepOutput(is_running=False, phase="off", raw_power_w=0.0)

        # _state == "active"
        assert self._start_ts is not None
        total_elapsed_s = (ts - self._start_ts).total_seconds()

        if residual_w <= p.idle_threshold_w:
            self._update_high_blocks(ts, 0.0)
            self._candidate_phase = None
            self._candidate_phase_since = None
            if self._idle_since_ts is None:
                self._idle_since_ts = ts
            idle_s = (ts - self._idle_since_ts).total_seconds()
            has_valid_work = (
                self.current_cycle_energy_kwh >= p.min_cycle_energy_kwh
                and self._high_blocks >= p.min_high_blocks
            )
            # Close after end_idle_hold_s once valid work happened, or abort after max_pause_s
            if (
                (has_valid_work and idle_s >= p.end_idle_hold_s)
                or (not has_valid_work and idle_s >= max(p.end_idle_hold_s, p.max_pause_s))
                or total_elapsed_s >= p.max_on_s
            ):
                return self._finalize_cycle(self._last_active_ts or ts, valid=has_valid_work)

            return DetectorStepOutput(
                is_running=True,
                phase="idle_pause",
                raw_power_w=0.0,
                cycle_start_time=self._start_ts,
                high_blocks_in_cycle=self._high_blocks,
            )

        # Active power above idle_threshold_w
        self._idle_since_ts = None
        self._last_active_ts = ts
        cur_w = residual_w if p.dedicated_meter else min(residual_w, p.max_power_w)
        self._peak_w = max(self._peak_w, cur_w)
        self._update_high_blocks(ts, cur_w)
        phase = self._resolve_phase(ts, cur_w)

        if total_elapsed_s >= p.max_on_s:
            has_valid_work = (
                self.current_cycle_energy_kwh >= p.min_cycle_energy_kwh
                and self._high_blocks >= p.min_high_blocks
            )
            return self._finalize_cycle(ts, valid=has_valid_work)

        return DetectorStepOutput(
            is_running=True,
            phase=phase,
            raw_power_w=cur_w,
            cycle_start_time=self._start_ts,
            high_blocks_in_cycle=self._high_blocks,
        )

    def _finalize_cycle(self, end_ts: datetime, *, valid: bool) -> DetectorStepOutput:
        p = self.profile
        assert self._start_ts is not None
        completed: CompletedCycle | None = None
        if valid:
            dur_min = max(0.0, (end_ts - self._start_ts).total_seconds() / 60.0)
            cycle_kwh = round(self.current_cycle_energy_kwh, 4)
            prog, conf = match_program_fingerprint(
                p.programs,
                energy_kwh=cycle_kwh,
                duration_min=dur_min,
                high_blocks=self._high_blocks,
            )
            phases_tuple = tuple(self._phases_seen) if self._phases_seen else ("running",)
            completed = CompletedCycle(
                appliance_id=p.appliance_id,
                appliance_name=p.name,
                start_time=self._start_ts,
                end_time=end_ts,
                duration_min=round(dur_min, 2),
                energy_kwh=cycle_kwh,
                peak_power_w=round(self._peak_w, 1),
                high_blocks=self._high_blocks,
                phases_seen=phases_tuple,
                matched_program=prog,
                confidence=conf,
            )
            self.last_cycle = completed

        self._reset_cycle()
        return DetectorStepOutput(
            is_running=False,
            phase="off",
            raw_power_w=0.0,
            completed_cycle=completed,
        )


def build_detector(
    profile: ApplianceProfile, *, line_skew_hold_s: float = 0.0
) -> BaseApplianceDetector:
    """Instantiate the appropriate detector for an ApplianceProfile."""
    if profile.archetype == Archetype.STEP:
        return StepDetector(profile, line_skew_hold_s=line_skew_hold_s)
    if profile.archetype == Archetype.CYCLING:
        return CyclingDetector(profile, line_skew_hold_s=line_skew_hold_s)
    if profile.archetype == Archetype.MULTIPHASE:
        return MultiphaseDetector(profile, line_skew_hold_s=line_skew_hold_s)
    raise ValueError(f"Unsupported archetype: {profile.archetype}")
