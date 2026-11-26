"""Unsupervised pattern discovery on synthetic multi-load lines."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from custom_components.hass_nilm.engine.discovery import (
    DiscoveryConfig,
    detect_edges,
    discover_patterns,
    pair_edges,
    to_dict,
)

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)


def _render(loads: list[tuple[float, float, float]], days: float, base_w: float, seed: int = 1):
    """Event-driven series: one sample every time the summed level changes, plus
    small jitter samples and single-sample inrush spikes, like a Tongou."""
    rng = random.Random(seed)
    horizon = days * 86400.0
    changes: dict[float, float] = {}
    for start, dur, w in loads:
        if start >= horizon:
            continue
        changes[start] = changes.get(start, 0.0) + w
        changes[start + dur] = changes.get(start + dur, 0.0) - w
    t_points = sorted(set(changes) | {i * 300.0 for i in range(int(horizon // 300))})
    level = 0.0
    out = []
    for t in t_points:
        delta = changes.get(t, 0.0)
        if delta > 40:  # compressor inrush: one spiky sample just before the step
            out.append((T0 + timedelta(seconds=t), base_w + level + delta * 12))
            t += 5
        level += delta
        out.append((T0 + timedelta(seconds=t), base_w + level + rng.uniform(-6, 6)))
    return out


def _periodic(on_s: float, off_s: float, w: float, days: float, jitter: float = 0.08, seed=2):
    rng = random.Random(seed)
    t, loads = 300.0, []
    while t < days * 86400:
        on = on_s * rng.uniform(1 - jitter, 1 + jitter)
        loads.append((t, on, w))
        t += on + off_s * rng.uniform(1 - jitter, 1 + jitter)
    return loads


def test_edges_merge_ramps_and_pair_on_off() -> None:
    cfg = DiscoveryConfig(median_window=1)
    run = [
        (T0, 50.0),
        (T0 + timedelta(seconds=5), 90.0),
        (T0 + timedelta(seconds=10), 125.0),  # ramp 50 -> 125 within 20 s
        (T0 + timedelta(seconds=600), 125.0),
        (T0 + timedelta(seconds=605), 52.0),
    ]
    edges = detect_edges(run, cfg)
    assert [round(e.delta_w) for e in edges] == [75, -73]
    events = pair_edges(edges, cfg)
    assert len(events) == 1
    assert round(events[0].power_w) == 74
    assert round(events[0].duration_s) == 595


def test_discovers_freezer_on_noisy_shared_line() -> None:
    """P03-like: freezer 75 W, 16 min on / 21 min off, over a 50 W noisy base."""
    loads = _periodic(16 * 60, 21 * 60, 75.0, days=5)
    cands = discover_patterns(_render(loads, 5, base_w=50.0))
    freezer = [c for c in cands if 60 <= c.power_w <= 90]
    assert len(freezer) == 1
    c = freezer[0]
    assert 14 <= c.duration_min <= 18
    assert c.period_min is not None and 33 <= c.period_min <= 41
    assert c.is_periodic
    assert c.hint.startswith("frío: compresor")
    assert c.events_per_day > 30
    d = to_dict(c)
    assert d["periodic"] is True and d["power_range_w"][0] <= d["power_w"]


def test_separates_fridge_defrost_and_kettle_on_kitchen_line() -> None:
    """P04-like: fridge compressor, No Frost defrost heater and a kettle."""
    fridge = _periodic(55 * 60, 40 * 60, 78.0, days=6, jitter=0.25, seed=3)
    defrost = [(h * 3600.0 + 120, 18 * 60, 255.0) for h in range(3, 6 * 24, 9)]
    kettle = [(d * 86400.0 + 8 * 3600 + 30 * d, 180, 2000.0) for d in range(6)]
    kettle += [(d * 86400.0 + 20 * 3600 + 30 * d, 200, 2000.0) for d in range(6)]
    cands = discover_patterns(_render(fridge + defrost + kettle, 6, base_w=9.0, seed=4))
    hints = {c.hint for c in cands}
    by_power = sorted(cands, key=lambda c: c.power_w)

    compressor = [c for c in by_power if 60 <= c.power_w <= 100]
    heater = [c for c in by_power if 200 <= c.power_w <= 300]
    boil = [c for c in by_power if c.power_w >= 1500]
    assert compressor and heater and boil
    assert 14 <= heater[0].duration_min <= 22
    assert 2 <= heater[0].events_per_day <= 3.5
    assert "frío: resistencia de desescarche (No Frost)" in hints
    assert boil[0].hint.startswith("calor rápido")


def test_rare_one_off_loads_are_not_candidates() -> None:
    loads = [(3600.0, 600, 900.0), (90000.0, 300, 450.0)]
    assert discover_patterns(_render(loads, 3, base_w=20.0)) == []


def test_noisy_wobble_does_not_split_off_edges() -> None:
    """P03 reality: another load wobbles +-15 W every few seconds and the meter
    samples every 5 s, so a -73 W drop arrives as several partial steps. Cycles
    must still pair one-to-one instead of merging two runs into one long event."""
    rng = random.Random(7)
    loads = _periodic(16 * 60, 21 * 60, 73.0, days=3, jitter=0.03, seed=8)
    level_at = {}
    for start, dur, w in loads:
        level_at[start] = level_at.get(start, 0.0) + w
        level_at[start + dur] = level_at.get(start + dur, 0.0) - w
    marks = sorted(level_at)
    out, lvl, k = [], 0.0, 0
    for i in range(int(3 * 86400 / 5)):
        t = i * 5.0
        while k < len(marks) and marks[k] <= t:
            lvl += level_at[marks[k]]
            k += 1
        wobble = 15.0 if rng.random() < 0.3 else 0.0
        out.append((T0 + timedelta(seconds=t), 48.0 + lvl + wobble + rng.uniform(-3, 3)))
    cands = discover_patterns(out)
    freezer = [c for c in cands if 60 <= c.power_w <= 90]
    assert len(freezer) == 1, [(c.power_w, c.duration_min, c.events) for c in cands]
    assert 14 <= freezer[0].duration_min <= 18
    assert freezer[0].events >= 0.85 * len(loads)
    assert freezer[0].is_periodic


def test_profile_from_candidate_and_matching() -> None:
    from custom_components.hass_nilm.engine.discovery import (
        covered_by_profile,
        profile_from_candidate,
        same_pattern,
    )

    loads = _periodic(16 * 60, 21 * 60, 75.0, days=4)
    cand = to_dict(discover_patterns(_render(loads, 4, base_w=50.0))[0])
    prof = profile_from_candidate(cand, "  Congelador ")
    assert prof["name"] == "Congelador"
    assert prof["min_power_w"] < cand["power_w"] < prof["max_power_w"]
    assert 30 <= prof["min_on_s"] <= 600 < prof["max_on_s"]
    assert prof["learned_from"] == cand["candidate_id"]

    again = dict(cand, power_w=cand["power_w"] * 1.1, duration_min=cand["duration_min"] * 1.3)
    assert same_pattern(cand, again)
    assert not same_pattern(cand, dict(cand, power_w=cand["power_w"] * 3))

    assert covered_by_profile(cand, [{"appliance_id": "congelador", **prof}]) == "congelador"
    assert (
        covered_by_profile(cand, [{"appliance_id": "x", "min_power_w": 500, "max_power_w": 900}])
        is None
    )
    # P04 reality: defrost learned at 193 W, hand-made detector band 200-300 W.
    defrost = dict(cand, power_w=193.0)
    band = [{"appliance_id": "nevera_desescarche", "min_power_w": 200, "max_power_w": 300}]
    assert covered_by_profile(defrost, band) == "nevera_desescarche"
