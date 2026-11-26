"""Line-level NILM coordinator engine enforcing conservation and Riemann integration."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import replace

from .detectors import (
    BaseApplianceDetector,
    DetectorStepOutput,
    build_detector,
    match_program_fingerprint,
)
from .models import (
    ApplianceProfile,
    ApplianceStatus,
    CompletedCycle,
    LineFrame,
    LineSample,
    SourceLineConfig,
)
from .residual import ResidualTracker


def _clamp_powers_by_priority(
    raw_powers: dict[str, float],
    priorities: dict[str, int],
    ceiling_w: float,
) -> dict[str, float]:
    """Allocate residual power across active appliances so sum(attributed) <= ceiling_w."""
    remaining_w = max(0.0, ceiling_w)
    tiers: dict[int, list[str]] = defaultdict(list)
    for app_id, pw in raw_powers.items():
        if pw > 0.0:
            tiers[priorities[app_id]].append(app_id)

    allocated: dict[str, float] = {app_id: 0.0 for app_id in raw_powers}
    for prio in sorted(tiers):
        tier_ids = sorted(tiers[prio])
        tier_demand = sum(raw_powers[aid] for aid in tier_ids)
        if tier_demand <= 0.0 or remaining_w <= 0.0:
            continue
        if tier_demand <= remaining_w:
            for aid in tier_ids:
                allocated[aid] = raw_powers[aid]
            remaining_w -= tier_demand
        else:
            scale = remaining_w / tier_demand
            for aid in tier_ids:
                allocated[aid] = raw_powers[aid] * scale
            remaining_w = 0.0
    return allocated


class NilmLineEngine:
    """Pure deterministic NILM engine for one monitored electrical line."""

    def __init__(
        self,
        config: SourceLineConfig,
        profiles: Iterable[ApplianceProfile] = (),
    ) -> None:
        self.config = config
        self._residual = ResidualTracker(config)
        self._detectors: dict[str, BaseApplianceDetector] = {}
        for prof in profiles:
            self.add_or_update_profile(prof)

    @property
    def profiles(self) -> dict[str, ApplianceProfile]:
        """Return registered appliance profiles keyed by appliance_id."""
        return {aid: det.profile for aid, det in self._detectors.items()}

    def add_or_update_profile(self, profile: ApplianceProfile) -> None:
        """Register a new appliance detector or replace config while keeping total energy."""
        prev_energy = (
            self._detectors[profile.appliance_id].total_energy_kwh
            if profile.appliance_id in self._detectors
            else 0.0
        )
        prev_last_cycle = (
            self._detectors[profile.appliance_id].last_cycle
            if profile.appliance_id in self._detectors
            else None
        )
        det = build_detector(profile, line_skew_hold_s=self.config.skew_hold_s)
        det.restore_total_energy(prev_energy)
        det.last_cycle = prev_last_cycle
        self._detectors[profile.appliance_id] = det

    def remove_profile(self, appliance_id: str) -> None:
        """Remove an appliance detector from the line."""
        self._detectors.pop(appliance_id, None)

    def restore_appliance_energy(self, appliance_id: str, total_energy_kwh: float) -> None:
        """Restore cumulative kWh for an appliance (used on Home Assistant startup)."""
        if appliance_id in self._detectors:
            self._detectors[appliance_id].restore_total_energy(total_energy_kwh)

    def process_sample(self, sample: LineSample) -> LineFrame:
        """Feed one telemetry snapshot and return conservation-bounded statuses and events."""
        res = self._residual.update(sample)

        step_outputs: dict[str, DetectorStepOutput] = {}
        raw_powers: dict[str, float] = {}
        priorities: dict[str, int] = {}

        # Step higher-priority detectors first and subtract dedicated/higher-tier demand
        ordered_detectors = sorted(
            self._detectors.values(),
            key=lambda d: (d.profile.priority, d.profile.appliance_id),
        )
        available_for_next_w = res.filtered_residual_w
        for det in ordered_detectors:
            aid = det.profile.appliance_id
            out = det.step(
                sample.timestamp,
                available_for_next_w,
                max_gap_s=self.config.max_integration_gap_s,
            )
            step_outputs[aid] = out
            raw_powers[aid] = max(0.0, out.raw_power_w)
            priorities[aid] = det.profile.priority
            if out.is_running and out.raw_power_w > 0.0 and not det.profile.dedicated_meter:
                available_for_next_w = max(0.0, available_for_next_w - out.raw_power_w)

        clamped = _clamp_powers_by_priority(
            raw_powers,
            priorities,
            ceiling_w=res.filtered_residual_w,
        )

        statuses: dict[str, ApplianceStatus] = {}
        completed_cycles: list[CompletedCycle] = []

        for aid, det in self._detectors.items():
            out = step_outputs[aid]
            attr_w = round(clamped[aid], 2)
            tail_kwh = det.integrate_clamped_power(
                sample.timestamp,
                attr_w,
                is_running=out.is_running,
                max_gap_s=self.config.max_integration_gap_s,
                just_confirmed=out.provisional_energy_commit_kwh > 0.0,
            )
            if out.completed_cycle is not None:
                cc = out.completed_cycle
                if tail_kwh > 0.0 and cc.end_time == sample.timestamp:
                    new_kwh = round(cc.energy_kwh + tail_kwh, 4)
                    prog, conf = match_program_fingerprint(
                        det.profile.programs,
                        energy_kwh=new_kwh,
                        duration_min=cc.duration_min,
                        high_blocks=cc.high_blocks,
                    )
                    cc = replace(cc, energy_kwh=new_kwh, matched_program=prog, confidence=conf)
                    det.last_cycle = cc
                completed_cycles.append(cc)

            dur_min = (
                round(max(0.0, (sample.timestamp - out.cycle_start_time).total_seconds() / 60.0), 2)
                if out.is_running and out.cycle_start_time is not None
                else 0.0
            )
            statuses[aid] = ApplianceStatus(
                appliance_id=aid,
                name=det.profile.name,
                archetype=det.profile.archetype,
                is_running=out.is_running,
                phase=out.phase,
                attributed_power_w=attr_w,
                total_energy_kwh=round(det.total_energy_kwh, 4),
                current_cycle_energy_kwh=round(det.current_cycle_energy_kwh, 4),
                current_cycle_duration_min=dur_min,
                high_blocks_in_cycle=out.high_blocks_in_cycle,
                last_cycle=det.last_cycle,
            )

        total_inferred_w = round(sum(clamped.values()), 2)
        unexplained_w = round(max(0.0, res.filtered_residual_w - total_inferred_w), 2)

        return LineFrame(
            timestamp=sample.timestamp,
            parent_power_w=res.parent_power_w,
            submeters_power_w=res.submeters_power_w,
            raw_residual_w=res.raw_residual_w,
            filtered_residual_w=res.filtered_residual_w,
            skew_guard_active=res.skew_guard_active,
            total_inferred_power_w=total_inferred_w,
            unexplained_power_w=unexplained_w,
            appliances=statuses,
            completed_cycles=tuple(completed_cycles),
        )
