"""Unit tests for the pure-Python historical power series scanner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.hass_nilm.engine import scan_power_series


def test_scan_power_series_discovers_steps_and_multiphase_cycles() -> None:
    """Scanner finds baseline, step clusters, and recurring multi-block cycles."""
    t0 = datetime(2026, 11, 20, 0, 0, 0, tzinfo=UTC)
    samples: list[tuple[datetime, float]] = []

    cur = t0
    for day in range(3):
        day_base = t0 + timedelta(days=day)
        # 2 hours idle at 20 W
        for m in range(0, 120, 1):
            samples.append((day_base + timedelta(minutes=m), 20.0))
        # Recurring multiphase cycle: 20 min @ 2020 W, 30 min @ 80 W, 10 min @ 2020 W
        for m in range(120, 140, 1):
            samples.append((day_base + timedelta(minutes=m), 2020.0))
        for m in range(140, 170, 1):
            samples.append((day_base + timedelta(minutes=m), 80.0))
        for m in range(170, 180, 1):
            samples.append((day_base + timedelta(minutes=m), 2020.0))
        # 2 hours idle again
        for m in range(180, 300, 1):
            samples.append((day_base + timedelta(minutes=m), 20.0))
        cur = day_base + timedelta(minutes=300)
    _ = cur

    report = scan_power_series(samples, idle_threshold_w=35.0, min_step_w=200.0)
    assert report.estimated_baseline_w == 20.0
    assert len(report.step_clusters) >= 1
    assert 1900.0 <= report.step_clusters[0].center_power_w <= 2050.0
    assert len(report.cycle_clusters) == 1
    cyc = report.cycle_clusters[0]
    assert cyc.occurrences == 3
    assert cyc.typical_high_blocks == 2
    assert cyc.suggested_archetype == "multiphase"
