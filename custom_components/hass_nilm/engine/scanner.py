"""Historical power series scanner for discovering step signatures and appliance cycles."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import median


@dataclass(frozen=True, slots=True)
class StepCluster:
    """Cluster of rising power steps observed in historical data."""

    center_power_w: float
    min_power_w: float
    max_power_w: float
    occurrences: int


@dataclass(frozen=True, slots=True)
class CycleCluster:
    """Cluster of recurring active consumption sessions."""

    occurrences: int
    median_duration_min: float
    median_energy_kwh: float
    median_peak_w: float
    typical_high_blocks: int
    suggested_archetype: str


@dataclass(frozen=True, slots=True)
class ScanReport:
    """Summary returned by scan_power_series()."""

    sample_count: int
    estimated_baseline_w: float
    step_clusters: tuple[StepCluster, ...]
    cycle_clusters: tuple[CycleCluster, ...]


def _cluster_1d_steps(
    values: list[float], rel_tol: float = 0.15, abs_tol: float = 120.0
) -> list[StepCluster]:
    if not values:
        return []
    sorted_vals = sorted(values)
    groups: list[list[float]] = [[sorted_vals[0]]]
    for val in sorted_vals[1:]:
        cur_med = median(groups[-1])
        if abs(val - cur_med) <= max(abs_tol, cur_med * rel_tol):
            groups[-1].append(val)
        else:
            groups.append([val])
    clusters = [
        StepCluster(
            center_power_w=round(float(median(g)), 1),
            min_power_w=round(min(g), 1),
            max_power_w=round(max(g), 1),
            occurrences=len(g),
        )
        for g in groups
        if len(g) >= 2
    ]
    clusters.sort(key=lambda c: c.occurrences, reverse=True)
    return clusters


def scan_power_series(
    samples: Sequence[tuple[datetime, float]],
    *,
    idle_threshold_w: float = 30.0,
    min_step_w: float = 120.0,
    merge_gap_s: float = 900.0,
    high_power_threshold_w: float = 1000.0,
    min_cycle_energy_kwh: float = 0.05,
    max_sample_gap_s: float = 900.0,
) -> ScanReport:
    """Analyze a historical (timestamp, power_w) series in pure Python."""
    ordered = sorted(
        ((ts, max(0.0, float(pw))) for ts, pw in samples),
        key=lambda item: item[0],
    )
    if len(ordered) < 3:
        baseline = ordered[0][1] if ordered else 0.0
        return ScanReport(
            sample_count=len(ordered),
            estimated_baseline_w=round(baseline, 1),
            step_clusters=(),
            cycle_clusters=(),
        )

    powers_sorted = sorted(pw for _, pw in ordered)
    p10_idx = max(0, int(len(powers_sorted) * 0.10))
    baseline_w = powers_sorted[p10_idx]
    eff_idle_w = max(idle_threshold_w, baseline_w + 15.0)

    # 1. Rising step extraction
    rising_steps: list[float] = []
    for idx in range(1, len(ordered)):
        dt_s = (ordered[idx][0] - ordered[idx - 1][0]).total_seconds()
        if 0.0 < dt_s <= 60.0:
            delta = ordered[idx][1] - ordered[idx - 1][1]
            if delta >= min_step_w:
                rising_steps.append(delta)
    step_clusters = _cluster_1d_steps(rising_steps)

    # 2. Active cycle extraction with pause merging
    raw_cycles: list[tuple[float, float, float, int, bool]] = []
    in_cycle = False
    start_ts: datetime | None = None
    last_active_ts: datetime | None = None
    prev_ts: datetime | None = None
    prev_w: float = 0.0
    energy_kwh: float = 0.0
    peak_w: float = 0.0
    high_blocks: int = 0
    in_high: bool = False
    had_low_active: bool = False

    def flush_cycle() -> None:
        nonlocal \
            in_cycle, \
            start_ts, \
            last_active_ts, \
            energy_kwh, \
            peak_w, \
            high_blocks, \
            in_high, \
            had_low_active
        if in_cycle and start_ts is not None and last_active_ts is not None:
            dur_min = (last_active_ts - start_ts).total_seconds() / 60.0
            if energy_kwh >= min_cycle_energy_kwh and dur_min >= 1.0:
                raw_cycles.append((dur_min, energy_kwh, peak_w, high_blocks, had_low_active))
        in_cycle = False
        start_ts = None
        last_active_ts = None
        energy_kwh = 0.0
        peak_w = 0.0
        high_blocks = 0
        in_high = False
        had_low_active = False

    for ts, pw in ordered:
        net_w = max(0.0, pw - baseline_w)
        if in_cycle and prev_ts is not None:
            dt_s = (ts - prev_ts).total_seconds()
            if 0.0 < dt_s <= max_sample_gap_s:
                energy_kwh += ((prev_w + net_w) * 0.5 * dt_s) / 3_600_000.0

        if pw > eff_idle_w:
            if not in_cycle:
                in_cycle = True
                start_ts = ts
                energy_kwh = 0.0
                peak_w = net_w
                high_blocks = 0
                in_high = False
                had_low_active = False
            last_active_ts = ts
            peak_w = max(peak_w, net_w)
            if net_w >= high_power_threshold_w:
                if not in_high:
                    in_high = True
                    high_blocks += 1
            else:
                if net_w < high_power_threshold_w * 0.6:
                    in_high = False
                if net_w < high_power_threshold_w * 0.4:
                    had_low_active = True
        elif in_cycle and last_active_ts is not None:
            in_high = False
            if (ts - last_active_ts).total_seconds() > merge_gap_s:
                flush_cycle()

        prev_ts = ts
        prev_w = net_w

    flush_cycle()

    # Group similar cycles by (duration, energy) within 25% tolerance
    cycle_groups: list[list[tuple[float, float, float, int, bool]]] = []
    for cyc in sorted(raw_cycles, key=lambda c: c[1]):
        placed = False
        for grp in cycle_groups:
            med_d = float(median(x[0] for x in grp))
            med_e = float(median(x[1] for x in grp))
            if abs(cyc[0] - med_d) <= max(15.0, med_d * 0.25) and abs(cyc[1] - med_e) <= max(
                0.15, med_e * 0.25
            ):
                grp.append(cyc)
                placed = True
                break
        if not placed:
            cycle_groups.append([cyc])

    cycle_clusters: list[CycleCluster] = []
    for grp in cycle_groups:
        typ_hb = int(round(median(x[3] for x in grp)))
        low_ratio = sum(1 for x in grp if x[4]) / len(grp)
        if typ_hb >= 1 and low_ratio >= 0.5:
            archetype = "multiphase"
        elif typ_hb >= 2:
            archetype = "cycling"
        else:
            archetype = "step"
        cycle_clusters.append(
            CycleCluster(
                occurrences=len(grp),
                median_duration_min=round(float(median(x[0] for x in grp)), 1),
                median_energy_kwh=round(float(median(x[1] for x in grp)), 3),
                median_peak_w=round(float(median(x[2] for x in grp)), 1),
                typical_high_blocks=typ_hb,
                suggested_archetype=archetype,
            )
        )
    cycle_clusters.sort(key=lambda c: c.occurrences, reverse=True)

    return ScanReport(
        sample_count=len(ordered),
        estimated_baseline_w=round(baseline_w, 1),
        step_clusters=tuple(step_clusters[:10]),
        cycle_clusters=tuple(cycle_clusters[:10]),
    )
