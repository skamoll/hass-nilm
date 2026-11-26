"""Unsupervised appliance discovery from a single power series (Hart-style NILM).

Pure Python, no Home Assistant imports. Pipeline:

1. Clean: drop gaps, median-filter single-sample glitches (compressor inrush).
2. Edges: merge consecutive same-sign changes that happen within a short ramp
   window into one step; keep steps of at least ``min_edge_w``.
3. Pairing: match every falling edge with the open rising edge of the most
   similar magnitude (tolerance relative to the step), producing ON/OFF events.
4. Clustering: group events by power and duration on a log scale.
5. Describe: robust statistics (median and percentiles), events per day,
   periodicity (median gap between starts and its dispersion), hour-of-day
   profile and a non-binding hint of what kind of appliance it resembles.

The output are *candidates*: patterns the line repeats. Naming them is the
user's job; deriving detector parameters from them is not.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from statistics import median


@dataclass(frozen=True, slots=True)
class DiscoveryConfig:
    """Tuning knobs for pattern discovery. Defaults fit Tongou/Shelly-class meters."""

    min_edge_w: float = 30.0
    ramp_window_s: float = 20.0
    settle_window_s: float = 60.0
    median_window: int = 3
    offline_gap_s: float = 6 * 3600.0
    pair_tolerance_rel: float = 0.25
    pair_tolerance_abs_w: float = 20.0
    max_event_duration_s: float = 6 * 3600.0
    min_event_duration_s: float = 20.0
    power_bin_log10: float = 0.05
    power_radius_bins: int = 1
    duration_bin_log10: float = 0.15
    duration_radius_bins: int = 2
    min_events: int = 6
    min_days_seen: int = 2


@dataclass(frozen=True, slots=True)
class Edge:
    """A step change in the (filtered) power level."""

    timestamp: datetime
    delta_w: float
    level_before_w: float
    level_after_w: float


@dataclass(frozen=True, slots=True)
class PairedEvent:
    """An ON edge matched with its OFF edge: one run of one load."""

    start: datetime
    end: datetime
    power_w: float

    @property
    def duration_s(self) -> float:
        return (self.end - self.start).total_seconds()

    @property
    def energy_kwh(self) -> float:
        return self.power_w * self.duration_s / 3_600_000.0


@dataclass(frozen=True, slots=True)
class PatternCandidate:
    """A recurring load pattern found on a line, waiting for a human name."""

    candidate_id: str
    events: int
    days_seen: int
    events_per_day: float
    power_w: float
    power_p10_w: float
    power_p90_w: float
    duration_min: float
    duration_p10_min: float
    duration_p90_min: float
    energy_kwh_per_event: float
    energy_kwh_per_day: float
    period_min: float | None
    period_cv: float | None
    hours_profile: tuple[int, ...]
    hint: str
    first_seen: datetime
    last_seen: datetime
    examples: tuple[tuple[datetime, float, float], ...] = field(default_factory=tuple)

    @property
    def is_periodic(self) -> bool:
        return self.period_cv is not None and self.period_cv < 0.6


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def clean_series(
    samples: Iterable[tuple[datetime, float]], cfg: DiscoveryConfig
) -> list[list[tuple[datetime, float]]]:
    """Split into gap-free runs and median-filter each run."""
    runs: list[list[tuple[datetime, float]]] = []
    cur: list[tuple[datetime, float]] = []
    prev_ts: datetime | None = None
    for ts, w in samples:
        if w is None or math.isnan(w):
            continue
        # Event-driven meters stay silent while the level holds, so short silences
        # are not gaps. Only a silence longer than offline_gap_s cuts the run.
        if prev_ts is not None and (ts - prev_ts).total_seconds() > cfg.offline_gap_s:
            if cur:
                runs.append(cur)
            cur = []
        cur.append((ts, max(0.0, float(w))))
        prev_ts = ts
    if cur:
        runs.append(cur)

    half = max(1, cfg.median_window) // 2
    if half == 0:
        return runs
    out = []
    for run in runs:
        vals = [w for _, w in run]
        filt = []
        for i in range(len(run)):
            window = vals[max(0, i - half) : i + half + 1]
            filt.append((run[i][0], float(median(window))))
        out.append(filt)
    return out


def _steady_level(
    run: Sequence[tuple[datetime, float]], idx: int, window_s: float, forward: bool
) -> float:
    """Median level over ``window_s`` before (or after) ``run[idx]``, including it."""
    t0 = run[idx][0]
    vals = [run[idx][1]]
    step = 1 if forward else -1
    k = idx + step
    while 0 <= k < len(run) and abs((run[k][0] - t0).total_seconds()) <= window_s:
        vals.append(run[k][1])
        k += step
    return float(median(vals))


def detect_edges(run: Sequence[tuple[datetime, float]], cfg: DiscoveryConfig) -> list[Edge]:
    """Steady-state edge detection.

    A ramp of same-sign changes (within ``ramp_window_s``) is one candidate step.
    Its size is measured between the *steady* levels around it (median over
    ``settle_window_s`` on each side), not between two raw samples, so a load
    wobbling on top of the line cannot split a drop into partial steps. Same-sign
    steps that land within the settle window of each other are merged.
    """
    edges: list[Edge] = []
    if len(run) < 2:
        return edges
    raw: list[tuple[int, int]] = []
    i, n = 1, len(run)
    while i < n:
        d = run[i][1] - run[i - 1][1]
        if d == 0:
            i += 1
            continue
        sign = 1 if d > 0 else -1
        start_idx, j = i - 1, i
        while (
            j + 1 < n
            and (run[j + 1][1] - run[j][1]) * sign > 0
            and (run[j + 1][0] - run[start_idx][0]).total_seconds() <= cfg.ramp_window_s
        ):
            j += 1
        raw.append((start_idx, j))
        i = j + 1

    win = cfg.settle_window_s
    for start_idx, end_idx in raw:
        before = _steady_level(run, start_idx, win, forward=False)
        after = _steady_level(run, end_idx, win, forward=True)
        delta = after - before
        if abs(delta) < cfg.min_edge_w:
            continue
        ts = run[end_idx][0]
        if (
            edges
            and (edges[-1].delta_w > 0) == (delta > 0)
            and (ts - edges[-1].timestamp).total_seconds() <= win
        ):
            prev = edges.pop()
            edges.append(Edge(ts, after - prev.level_before_w, prev.level_before_w, after))
            continue
        if (
            edges
            and (edges[-1].delta_w > 0) != (delta > 0)
            and (ts - edges[-1].timestamp).total_seconds() <= win
            and abs(edges[-1].delta_w + delta) < cfg.min_edge_w
        ):
            # up-and-back-down within the settle window: a transient, not a load
            edges.pop()
            continue
        edges.append(Edge(ts, delta, before, after))
    return edges


def pair_edges(edges: Sequence[Edge], cfg: DiscoveryConfig) -> list[PairedEvent]:
    """Match falling edges to open rising edges of similar magnitude."""
    open_on: list[Edge] = []
    events: list[PairedEvent] = []
    for e in edges:
        if e.delta_w > 0:
            open_on.append(e)
            continue
        drop = -e.delta_w
        best_idx = -1
        best_err = float("inf")
        for idx in range(len(open_on) - 1, -1, -1):
            on = open_on[idx]
            age = (e.timestamp - on.timestamp).total_seconds()
            if age > cfg.max_event_duration_s:
                continue
            tol = max(cfg.pair_tolerance_abs_w, cfg.pair_tolerance_rel * on.delta_w)
            err = abs(on.delta_w - drop)
            if err <= tol and err < best_err:
                best_err, best_idx = err, idx
        if best_idx >= 0:
            on = open_on.pop(best_idx)
            dur = (e.timestamp - on.timestamp).total_seconds()
            if dur >= cfg.min_event_duration_s:
                events.append(PairedEvent(on.timestamp, e.timestamp, (on.delta_w + drop) / 2.0))
        # stale ONs never closed: forget them
        open_on = [
            o
            for o in open_on
            if (e.timestamp - o.timestamp).total_seconds() <= cfg.max_event_duration_s
        ]
    return events


def _density_clusters(
    values: list[tuple[float, int]], bin_log10: float, radius_bins: int, min_count: int
) -> list[list[int]]:
    """Mode-seeking clustering on a log10 histogram.

    Each local maximum of the smoothed histogram with enough mass becomes a
    cluster; values within ``radius_bins`` of a peak join it and the rest is
    treated as noise (overlapping loads, mis-paired edges). Unlike gap-based
    clustering this does not chain a continuum of powers into one group.
    """
    if not values:
        return []
    bins: dict[int, list[int]] = {}
    for v, idx in values:
        b = round(math.log10(max(v, 1e-6)) / bin_log10)
        bins.setdefault(b, []).append(idx)
    lo, hi = min(bins), max(bins)
    raw = {b: len(bins.get(b, ())) for b in range(lo - 1, hi + 2)}
    smooth = {b: raw.get(b - 1, 0) + 2 * raw[b] + raw.get(b + 1, 0) for b in range(lo, hi + 1)}
    peaks = []
    for b in range(lo, hi + 1):
        if smooth[b] == 0:
            continue
        if smooth[b] >= smooth.get(b - 1, 0) and smooth[b] > smooth.get(b + 1, 0):
            mass = sum(raw.get(b + k, 0) for k in range(-radius_bins, radius_bins + 1))
            if mass >= min_count:
                peaks.append(b)
    # keep the stronger of peaks that are too close to be different loads
    peaks.sort(key=lambda b: smooth[b], reverse=True)
    kept: list[int] = []
    for b in peaks:
        if all(abs(b - k) > radius_bins for k in kept):
            kept.append(b)
    clusters: dict[int, list[int]] = {k: [] for k in kept}
    for b, idxs in bins.items():
        near = [k for k in kept if abs(b - k) <= radius_bins]
        if near:
            best = min(near, key=lambda k: (abs(b - k), -smooth[k]))
            clusters[best].extend(idxs)
    return [c for c in clusters.values() if c]


def _hint(power_w: float, dur_min: float, per_day: float, period_cv: float | None) -> str:
    periodic = period_cv is not None and period_cv < 0.6
    if 40 <= power_w <= 200 and 4 <= dur_min <= 240 and per_day >= 6 and periodic:
        return "frío: compresor de nevera o congelador"
    if 150 <= power_w <= 450 and 8 <= dur_min <= 45 and 1 <= per_day <= 5:
        return "frío: resistencia de desescarche (No Frost)"
    if power_w >= 1500 and dur_min <= 6:
        return "calor rápido: hervidor, microondas o tostadora"
    if power_w >= 1500 and dur_min > 6:
        return "calor sostenido: horno, placa, termo o calefactor"
    if 600 <= power_w < 1500 and dur_min <= 10:
        return "pequeño electrodoméstico: microondas, cafetera o secador"
    if power_w < 40 and dur_min >= 30:
        return "carga pequeña y larga: iluminación, cargador o electrónica"
    return "sin pista clara"


def describe_cluster(
    candidate_id: str, events: Sequence[PairedEvent], cfg: DiscoveryConfig
) -> PatternCandidate | None:
    if len(events) < cfg.min_events:
        return None
    events = sorted(events, key=lambda e: e.start)
    days = {e.start.date() for e in events}
    if len(days) < cfg.min_days_seen:
        return None
    powers = sorted(e.power_w for e in events)
    durs = sorted(e.duration_s / 60.0 for e in events)
    span_days = max(1.0, (events[-1].end - events[0].start).total_seconds() / 86400.0)
    gaps = [
        (b.start - a.start).total_seconds() / 60.0 for a, b in zip(events, events[1:], strict=False)
    ]
    period = period_cv = None
    if len(gaps) >= 3:
        g = sorted(gaps)
        period = median(g)
        mad = median(abs(x - period) for x in g)
        period_cv = (1.4826 * mad / period) if period > 0 else None
    hours = [0] * 24
    for e in events:
        hours[e.start.hour] += 1
    energy = [e.energy_kwh for e in events]
    per_day = len(events) / span_days
    p_med = median(powers)
    d_med = median(durs)
    ex = tuple((e.start, round(e.power_w, 1), round(e.duration_s / 60.0, 1)) for e in events[-3:])
    return PatternCandidate(
        candidate_id=candidate_id,
        events=len(events),
        days_seen=len(days),
        events_per_day=round(per_day, 2),
        power_w=round(p_med, 1),
        power_p10_w=round(_percentile(powers, 0.1), 1),
        power_p90_w=round(_percentile(powers, 0.9), 1),
        duration_min=round(d_med, 1),
        duration_p10_min=round(_percentile(durs, 0.1), 1),
        duration_p90_min=round(_percentile(durs, 0.9), 1),
        energy_kwh_per_event=round(median(energy), 4),
        energy_kwh_per_day=round(median(energy) * per_day, 3),
        period_min=round(period, 1) if period is not None else None,
        period_cv=round(period_cv, 2) if period_cv is not None else None,
        hours_profile=tuple(hours),
        hint=_hint(p_med, d_med, per_day, period_cv),
        first_seen=events[0].start,
        last_seen=events[-1].start,
        examples=ex,
    )


def discover_patterns(
    samples: Iterable[tuple[datetime, float]],
    cfg: DiscoveryConfig | None = None,
) -> list[PatternCandidate]:
    """Full pipeline: samples -> recurring pattern candidates, biggest energy first."""
    cfg = cfg or DiscoveryConfig()
    events: list[PairedEvent] = []
    for run in clean_series(samples, cfg):
        events.extend(pair_edges(detect_edges(run, cfg), cfg))

    candidates: list[PatternCandidate] = []
    power_groups = _density_clusters(
        [(e.power_w, i) for i, e in enumerate(events)],
        cfg.power_bin_log10,
        cfg.power_radius_bins,
        cfg.min_events,
    )
    for pg in power_groups:
        dur_groups = _density_clusters(
            [(events[i].duration_s, i) for i in pg],
            cfg.duration_bin_log10,
            cfg.duration_radius_bins,
            cfg.min_events,
        )
        for dg in dur_groups:
            group = [events[i] for i in dg]
            p_med = round(median(e.power_w for e in group))
            d_med = round(median(e.duration_s for e in group) / 60)
            cand = describe_cluster(f"p{p_med}w_d{d_med}m", group, cfg)
            if cand is not None:
                candidates.append(cand)
    candidates.sort(key=lambda c: c.energy_kwh_per_day, reverse=True)
    return candidates


def to_dict(c: PatternCandidate) -> dict:
    """JSON-friendly view of a candidate (for storage, diagnostics and notifications)."""
    return {
        "candidate_id": c.candidate_id,
        "events": c.events,
        "days_seen": c.days_seen,
        "events_per_day": c.events_per_day,
        "power_w": c.power_w,
        "power_range_w": [c.power_p10_w, c.power_p90_w],
        "duration_min": c.duration_min,
        "duration_range_min": [c.duration_p10_min, c.duration_p90_min],
        "energy_kwh_per_event": c.energy_kwh_per_event,
        "energy_kwh_per_day": c.energy_kwh_per_day,
        "period_min": c.period_min,
        "period_cv": c.period_cv,
        "periodic": c.is_periodic,
        "hours_profile": list(c.hours_profile),
        "hint": c.hint,
        "first_seen": c.first_seen.isoformat(),
        "last_seen": c.last_seen.isoformat(),
        "examples": [
            {"start": s.isoformat(), "power_w": p, "duration_min": d} for s, p, d in c.examples
        ],
    }


def same_pattern(a: dict, b: dict, power_rel: float = 0.2, duration_factor: float = 2.0) -> bool:
    """Whether two stored candidate dicts describe the same physical load.

    Used to recognise a pattern across daily runs so the user is asked once.
    """
    pa, pb = float(a["power_w"]), float(b["power_w"])
    da, db = max(float(a["duration_min"]), 0.1), max(float(b["duration_min"]), 0.1)
    return abs(pa - pb) <= power_rel * max(pa, pb) and max(da, db) / min(da, db) <= duration_factor


def covered_by_profile(cand: dict, profiles: Iterable[dict], margin: float = 0.1) -> str | None:
    """Return the appliance_id of an existing detector that already explains the
    candidate (median power inside its band), so we never ask about it."""
    p = float(cand["power_w"])
    for prof in profiles:
        lo, hi = float(prof.get("min_power_w", 0)), float(prof.get("max_power_w", 0))
        # 10 % margin: a learned median just outside a hand-made band is the same load
        if lo * (1 - margin) <= p <= hi * (1 + margin):
            return str(prof.get("appliance_id") or prof.get("name"))
    return None


def profile_from_candidate(cand: dict, name: str) -> dict:
    """Derive a step-detector profile (options dict) from a discovered pattern.

    Bands come from the observed spread (p10-p90) with a margin, never from
    guesses: the user only supplies the name.
    """
    p = float(cand["power_w"])
    p10, p90 = (float(x) for x in cand.get("power_range_w") or (p, p))
    d10, d90 = (float(x) for x in cand.get("duration_range_min") or (cand["duration_min"],) * 2)
    energy = float(cand.get("energy_kwh_per_event") or 0.0)
    min_power = max(10.0, round(min(p10, p) * 0.85, 1))
    max_power = round(max(p90, p) * 1.15, 1)
    return {
        "name": name.strip(),
        "archetype": "step",
        "min_power_w": min_power,
        "max_power_w": max(max_power, min_power + 10.0),
        "edge_tolerance_w": round(max(15.0, 0.15 * p), 1),
        "min_on_s": float(min(300.0, max(30.0, d10 * 60.0 * 0.5))),
        "max_on_s": float(min(14400.0, max(600.0, d90 * 60.0 * 3.0))),
        "idle_threshold_w": 15.0,
        "end_idle_hold_s": 180.0,
        "max_pause_s": 900.0,
        "high_block_threshold_w": 1000.0,
        "high_block_min_s": 60.0,
        "min_cycle_energy_kwh": round(max(0.0005, energy * 0.3), 4),
        "min_high_blocks": 0,
        "priority": 100,
        "dedicated_meter": False,
        "phases_json": "",
        "programs_json": "",
        "learned_from": cand.get("candidate_id"),
    }


__all__ = [
    "DiscoveryConfig",
    "Edge",
    "PairedEvent",
    "PatternCandidate",
    "clean_series",
    "covered_by_profile",
    "detect_edges",
    "discover_patterns",
    "pair_edges",
    "profile_from_candidate",
    "same_pattern",
    "to_dict",
]
