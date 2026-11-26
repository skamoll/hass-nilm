"""Recorder history scanner bridge running off the Home Assistant event loop."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .coordinator import state_to_watts
from .engine import ScanReport, scan_power_series


def _extract_and_scan(
    states: list[State],
    *,
    idle_threshold_w: float,
    min_step_w: float,
    merge_gap_s: float,
    high_power_threshold_w: float,
    min_cycle_energy_kwh: float,
) -> ScanReport:
    samples: list[tuple[datetime, float]] = []
    for st in states:
        pw = state_to_watts(st)
        if pw is not None:
            samples.append((st.last_updated, pw))
    return scan_power_series(
        samples,
        idle_threshold_w=idle_threshold_w,
        min_step_w=min_step_w,
        merge_gap_s=merge_gap_s,
        high_power_threshold_w=high_power_threshold_w,
        min_cycle_energy_kwh=min_cycle_energy_kwh,
    )


async def async_scan_entity_history(
    hass: HomeAssistant,
    entity_id: str,
    *,
    days: int = 14,
    idle_threshold_w: float = 30.0,
    min_step_w: float = 120.0,
    merge_gap_s: float = 900.0,
    high_power_threshold_w: float = 1000.0,
    min_cycle_energy_kwh: float = 0.05,
) -> dict[str, Any]:
    """Fetch Recorder states on executor thread and return a JSON-serializable ScanReport."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder import history as recorder_history

    end_time = dt_util.utcnow()
    start_time = end_time - timedelta(days=max(1, int(days)))

    def _job() -> ScanReport:
        hist_map = recorder_history.state_changes_during_period(
            hass,
            start_time,
            end_time,
            entity_id,
            no_attributes=False,
            include_start_time_state=True,
        )
        states = hist_map.get(entity_id, [])
        return _extract_and_scan(
            states,
            idle_threshold_w=idle_threshold_w,
            min_step_w=min_step_w,
            merge_gap_s=merge_gap_s,
            high_power_threshold_w=high_power_threshold_w,
            min_cycle_energy_kwh=min_cycle_energy_kwh,
        )

    if "recorder" in hass.data:
        report = await get_instance(hass).async_add_executor_job(_job)
    else:
        report = await hass.async_add_executor_job(_job)

    return {
        "entity_id": entity_id,
        "days": days,
        "sample_count": report.sample_count,
        "estimated_baseline_w": report.estimated_baseline_w,
        "step_clusters": [
            {
                "center_power_w": c.center_power_w,
                "min_power_w": c.min_power_w,
                "max_power_w": c.max_power_w,
                "occurrences": c.occurrences,
            }
            for c in report.step_clusters
        ],
        "cycle_clusters": [
            {
                "occurrences": c.occurrences,
                "median_duration_min": c.median_duration_min,
                "median_energy_kwh": c.median_energy_kwh,
                "median_peak_w": c.median_peak_w,
                "typical_high_blocks": c.typical_high_blocks,
                "suggested_archetype": c.suggested_archetype,
            }
            for c in report.cycle_clusters
        ],
    }
