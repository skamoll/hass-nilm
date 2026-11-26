"""Comprehensive unit tests for the pure-Python NILM disaggregation engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hass_nilm.engine import (
    UNKNOWN_PROGRAM,
    ApplianceProfile,
    Archetype,
    LineSample,
    NilmLineEngine,
    PhaseSpec,
    ProgramFingerprint,
    SourceLineConfig,
    SubmeterReading,
)

T0 = datetime(2026, 11, 26, 14, 0, 0, tzinfo=UTC)


def _ts(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def test_step_detector_blueprint_style_and_riemann_energy() -> None:
    """StepDetector pairs rising/falling edges and integrates exact kWh."""
    cfg = SourceLineConfig(line_id="mains", name="Mains", skew_hold_s=10.0, median_window=1)
    kettle = ApplianceProfile(
        appliance_id="kettle",
        name="Kettle",
        archetype=Archetype.STEP,
        min_power_w=1800.0,
        max_power_w=2400.0,
        edge_tolerance_w=150.0,
        min_on_s=10.0,
        min_cycle_energy_kwh=0.01,
    )
    engine = NilmLineEngine(cfg, [kettle])

    # Baseline 100 W
    engine.process_sample(LineSample(_ts(0), 100.0))
    engine.process_sample(LineSample(_ts(5), 100.0))

    # Kettle turns on (+2000 W -> 2100 W total) for 180 seconds (3 min = 0.05 h -> 0.10 kWh)
    f_on = engine.process_sample(LineSample(_ts(10), 2100.0))
    assert f_on.appliances["kettle"].is_running is False  # Pending confirmation (< 10 s)

    f_conf = engine.process_sample(LineSample(_ts(20), 2100.0))
    assert f_conf.appliances["kettle"].is_running is True
    assert f_conf.appliances["kettle"].attributed_power_w == pytest.approx(2000.0, rel=0.05)

    for sec in range(30, 190, 10):
        frame = engine.process_sample(LineSample(_ts(sec), 2100.0))
        assert frame.appliances["kettle"].is_running is True

    # Kettle turns off at t=190 (back to 100 W)
    f_off = engine.process_sample(LineSample(_ts(190), 100.0))
    st = f_off.appliances["kettle"]
    assert st.is_running is False
    assert len(f_off.completed_cycles) == 1
    cyc = f_off.completed_cycles[0]
    assert cyc.duration_min == pytest.approx(3.0, abs=0.05)
    assert cyc.energy_kwh == pytest.approx(0.1, abs=0.01)
    assert st.total_energy_kwh == pytest.approx(0.1, abs=0.01)


def test_parent_child_skew_both_directions_creates_zero_false_positives() -> None:
    """Skew guard blocks phantom triggers (parent-first) and false shutoffs (child-first)."""
    cfg = SourceLineConfig(
        line_id="mains",
        name="Mains",
        skew_hold_s=12.0,
        skew_delta_threshold_w=80.0,
        median_window=1,
    )
    heater_2kw = ApplianceProfile(
        appliance_id="space_heater",
        name="Space Heater",
        archetype=Archetype.STEP,
        min_power_w=1800.0,
        max_power_w=2200.0,
        min_on_s=10.0,
    )
    oven_1500w = ApplianceProfile(
        appliance_id="oven",
        name="Oven",
        archetype=Archetype.STEP,
        min_power_w=1350.0,
        max_power_w=1650.0,
        min_on_s=10.0,
    )
    engine = NilmLineEngine(cfg, [heater_2kw, oven_1500w])

    # Steady state at 100 W, known water_heater submeter at 0 W
    for s in (0, 5):
        engine.process_sample(LineSample(_ts(s), 100.0, (SubmeterReading("plug_termo", 0.0),)))

    # Direction 1: Parent reports +2000 W at t=10s, child plug_termo lags by 8s (reports at t=18s)
    f1 = engine.process_sample(LineSample(_ts(10), 2100.0, (SubmeterReading("plug_termo", 0.0),)))
    assert f1.appliances["space_heater"].is_running is False  # Still inside confirm/skew hold
    f2 = engine.process_sample(
        LineSample(_ts(18), 2100.0, (SubmeterReading("plug_termo", 2000.0),))
    )
    assert f2.appliances["space_heater"].is_running is False
    assert f2.appliances["space_heater"].total_energy_kwh == 0.0

    # Now actually turn on the unmonitored 1500 W oven at t=30..50s (total parent = 3600 W)
    engine.process_sample(LineSample(_ts(30), 3600.0, (SubmeterReading("plug_termo", 2000.0),)))
    f_oven = engine.process_sample(
        LineSample(_ts(45), 3600.0, (SubmeterReading("plug_termo", 2000.0),))
    )
    assert f_oven.appliances["oven"].is_running is True

    # Direction 2: ev_charger (+2000 W) reports FIRST at t=50s before parent updates.
    # Without skew guard, 3600 - 4000 -> 0 W would falsely turn off the running oven.
    f_skew = engine.process_sample(
        LineSample(
            _ts(50),
            3600.0,
            (
                SubmeterReading("plug_termo", 2000.0),
                SubmeterReading("ev_charger", 2000.0),
            ),
        )
    )
    assert f_skew.skew_guard_active is True
    assert f_skew.appliances["oven"].is_running is True

    # Parent catches up at t=56s (5600 W total -> residual still 1600 W)
    f_catchup = engine.process_sample(
        LineSample(
            _ts(56),
            5600.0,
            (
                SubmeterReading("plug_termo", 2000.0),
                SubmeterReading("ev_charger", 2000.0),
            ),
        )
    )
    assert f_catchup.skew_guard_active is False
    assert f_catchup.appliances["oven"].is_running is True


def test_conservation_ceiling_clamps_inferred_power() -> None:
    """Total inferred power across active detectors never exceeds residual power."""
    cfg = SourceLineConfig(line_id="kitchen", name="Kitchen", skew_hold_s=0.0, median_window=1)
    a1 = ApplianceProfile(
        appliance_id="load_a",
        name="Load A",
        archetype=Archetype.STEP,
        priority=10,
        min_power_w=900.0,
        max_power_w=1200.0,
        nominal_power_w=1000.0,
        min_on_s=0.0,
    )
    a2 = ApplianceProfile(
        appliance_id="load_b",
        name="Load B",
        archetype=Archetype.STEP,
        priority=20,
        min_power_w=900.0,
        max_power_w=1200.0,
        nominal_power_w=1000.0,
        min_on_s=0.0,
    )
    engine = NilmLineEngine(cfg, [a1, a2])
    engine.process_sample(LineSample(_ts(0), 0.0))
    engine.process_sample(LineSample(_ts(5), 1000.0))  # Load A latches
    f_both = engine.process_sample(LineSample(_ts(10), 2000.0))  # Load B latches
    assert f_both.appliances["load_a"].is_running is True
    assert f_both.appliances["load_b"].is_running is True

    # Residual dips slightly to 1750 W (still above turn-off delta, but < 2000 W nominal sum)
    f_dip = engine.process_sample(LineSample(_ts(15), 1750.0))
    assert f_dip.total_inferred_power_w <= f_dip.filtered_residual_w
    assert f_dip.appliances["load_a"].attributed_power_w == 1000.0
    assert f_dip.appliances["load_b"].attributed_power_w == 750.0


def test_cycling_detector_thermostatic_oven() -> None:
    """CyclingDetector holds session open across thermal pauses with 0 W attributed in pauses."""
    cfg = SourceLineConfig(line_id="p09", name="Cocina P09", skew_hold_s=0.0, median_window=1)
    oven = ApplianceProfile(
        appliance_id="oven",
        name="Bosch Oven",
        archetype=Archetype.CYCLING,
        min_power_w=1900.0,
        max_power_w=2500.0,
        nominal_power_w=2200.0,
        min_on_s=10.0,
        max_pause_s=300.0,
        min_cycle_energy_kwh=0.05,
    )
    engine = NilmLineEngine(cfg, [oven])
    engine.process_sample(LineSample(_ts(0), 20.0))

    # Pulse 1: 120 s at 2220 W
    for s in range(10, 140, 10):
        fr = engine.process_sample(LineSample(_ts(s), 2220.0))
    assert fr.appliances["oven"].is_running is True
    assert fr.appliances["oven"].phase == "running"

    # Thermal pause: 120 s at 20 W (< max_pause_s=300s) -> stays running in paused_thermal with 0 W!
    for s in range(140, 260, 20):
        fr = engine.process_sample(LineSample(_ts(s), 20.0))
        assert fr.appliances["oven"].is_running is True
        assert fr.appliances["oven"].phase == "paused_thermal"
        assert fr.appliances["oven"].attributed_power_w == 0.0

    # Pulse 2: 120 s at 2220 W
    for s in range(260, 390, 10):
        fr = engine.process_sample(LineSample(_ts(s), 2220.0))
    assert fr.appliances["oven"].high_blocks_in_cycle == 2

    # Final cool-down > 300 s closes the session at the end of Pulse 2!
    closed_frame = None
    for s in range(390, 720, 30):
        fr = engine.process_sample(LineSample(_ts(s), 20.0))
        if fr.completed_cycles:
            closed_frame = fr
            break

    assert closed_frame is not None
    cyc = closed_frame.completed_cycles[0]
    assert cyc.high_blocks == 2
    # Duration reflects active window up to end of pulse 2 (~6.33 min), not +5 min timeout
    assert cyc.duration_min == pytest.approx(6.33, abs=0.4)


def test_multiphase_washing_machine_phases_and_program_from_real_signature() -> None:
    """Replays the 2026-11-26 Samsung washing machine signature (darks 30C, 4 rinses, 1200 rpm)."""
    cfg = SourceLineConfig(
        line_id="plug_washer", name="Washer Plug", skew_hold_s=0.0, median_window=1
    )
    washer = ApplianceProfile(
        appliance_id="lavadora",
        name="Lavadora Galería",
        archetype=Archetype.MULTIPHASE,
        dedicated_meter=True,
        min_power_w=20.0,
        max_power_w=2500.0,
        min_on_s=15.0,
        idle_threshold_w=12.0,
        end_idle_hold_s=180.0,
        high_block_threshold_w=1500.0,
        high_block_min_s=60.0,
        min_cycle_energy_kwh=0.15,
        min_high_blocks=1,
        phases=(
            PhaseSpec("prewash", min_power_w=13.0, max_power_w=500.0, after_high_block=False),
            PhaseSpec("heating", min_power_w=1500.0, max_power_w=2500.0),
            PhaseSpec("wash_rinse", min_power_w=13.0, max_power_w=500.0, after_high_block=True),
            PhaseSpec(
                "spin",
                min_power_w=185.0,
                max_power_w=400.0,
                min_duration_s=180.0,
                after_high_block=True,
            ),
        ),
        programs=(
            ProgramFingerprint(
                name="ropa_oscura_30c",
                expected_energy_kwh=0.42,
                energy_tolerance_kwh=0.12,
                expected_duration_min=95.0,
                duration_tolerance_min=15.0,
                expected_high_blocks=1,
            ),
            ProgramFingerprint(
                name="algodon_60c",
                expected_energy_kwh=1.15,
                energy_tolerance_kwh=0.25,
                expected_duration_min=135.0,
                duration_tolerance_min=20.0,
                expected_high_blocks=1,
            ),
        ),
    )
    engine = NilmLineEngine(cfg, [washer])
    seen_phases: list[str] = []

    def feed_block(start_min: float, end_min: float, power_w: float, step_s: int = 30) -> list:
        frames = []
        cur_s = int(start_min * 60)
        end_s = int(end_min * 60)
        while cur_s < end_s:
            fr = engine.process_sample(LineSample(_ts(cur_s), power_w))
            ph = fr.appliances["lavadora"].phase
            if not seen_phases or seen_phases[-1] != ph:
                seen_phases.append(ph)
            frames.append(fr)
            cur_s += step_s
        return frames

    # 0..4 min: water fill & initial tumbles (~60 W) -> prewash
    feed_block(0, 4, 60.0)
    # 4..13 min (9 min): heating plateau (~2150 W -> ~0.322 kWh) -> heating
    feed_block(4, 13, 2150.0)
    # 13..24 min: wash tumbles (~90 W) with 8 W pauses (< end_idle_hold_s) -> no fragmentation
    feed_block(13, 18, 90.0)
    feed_block(18, 19.5, 8.0)  # 90 s pause at 8 W
    feed_block(19.5, 24, 95.0)
    # 4 short drain bursts (1 min at 294 W > final spin 240 W) -> stays wash_rinse, not spin
    for burst_min in (24, 42, 57, 70):
        feed_block(burst_min, burst_min + 1, 294.0)
        feed_block(burst_min + 1, burst_min + 12, 45.0)
    assert "spin" not in seen_phases

    # 88..95 min (7 min sustained at 240 W >= 180 s) -> spin!
    feed_block(88, 95, 240.0)
    assert "spin" in seen_phases

    # 95..99 min: drops to 4 W idle -> closes after 3 min end_idle_hold_s!
    closing_frames = feed_block(95, 99, 4.0)
    completed = [c for fr in closing_frames for c in fr.completed_cycles]
    assert len(completed) == 1
    cyc = completed[0]
    assert cyc.matched_program == "ropa_oscura_30c"
    assert cyc.high_blocks == 1
    assert cyc.phases_seen == ("prewash", "heating", "wash_rinse", "spin")
    assert cyc.duration_min == pytest.approx(94.5, abs=1.5)
    assert 0.38 <= cyc.energy_kwh <= 0.50


def test_multiphase_dishwasher_4_blocks_and_ambiguity_fallback() -> None:
    """Replays the 4-block P05 dishwasher pattern without fragmenting pauses and tests ambiguity."""
    cfg = SourceLineConfig(line_id="p05", name="P05 Lavavajillas", skew_hold_s=0.0, median_window=1)
    # Two overlapping programs -> should conservatively return UNKNOWN_PROGRAM!
    dishwasher = ApplianceProfile(
        appliance_id="lavavajillas",
        name="Lavavajillas",
        archetype=Archetype.MULTIPHASE,
        dedicated_meter=True,
        min_power_w=35.0,
        max_power_w=2500.0,
        min_on_s=30.0,
        idle_threshold_w=15.0,
        end_idle_hold_s=960.0,  # 16 min sustained idle closes cycle without multi-day merge
        max_pause_s=960.0,
        high_block_threshold_w=1200.0,
        high_block_min_s=120.0,
        min_cycle_energy_kwh=0.8,
        min_high_blocks=3,
        programs=(
            ProgramFingerprint("auto_a", 1.50, 0.25, 125.0, 20.0, expected_high_blocks=4),
            ProgramFingerprint("auto_b", 1.52, 0.25, 126.0, 20.0, expected_high_blocks=4),
        ),
    )
    engine = NilmLineEngine(cfg, [dishwasher])

    schedule = [
        (0, 20, 1600.0),  # Block 1: 20 min @ 1.6 kW (0.533 kWh)
        (20, 30, 5.0),  # 10 min pause (< 16 min end_idle_hold_s -> stays open!)
        (30, 42, 2200.0),  # Block 2: 12 min @ 2.2 kW (0.44 kWh)
        (42, 65, 55.0),  # Circulation @ 55 W
        (65, 73, 2200.0),  # Block 3: 8 min @ 2.2 kW (0.293 kWh)
        (73, 85, 5.0),  # 12 min drying pause (< 16 min)
        (85, 92, 2200.0),  # Block 4: 7 min @ 2.2 kW (0.256 kWh)
        (92, 125, 45.0),  # Final rinse/fan circulation
        (125, 145, 2.0),  # Sustained idle -> closes!
    ]
    completed = []
    for start_m, end_m, pw in schedule:
        for s in range(start_m * 60, end_m * 60, 60):
            fr = engine.process_sample(LineSample(_ts(s), pw))
            completed.extend(fr.completed_cycles)

    assert len(completed) == 1
    cyc = completed[0]
    assert cyc.high_blocks == 4
    assert 1.40 <= cyc.energy_kwh <= 1.65
    assert cyc.duration_min == pytest.approx(124.0, abs=2.0)
    assert cyc.matched_program == UNKNOWN_PROGRAM  # Ambiguous between auto_a and auto_b!


def test_microwave_and_air_fryer_split_by_duration_on_same_band() -> None:
    """P04 reality: microwave (coffee, ~2 min) and air fryer (>=3.5 min) draw the same
    ~1300 W. The microwave holds its energy and drops it if the run overruns 3.5 min;
    the fryer confirms after 3.5 min and back-dates its energy. No double counting."""
    cfg = SourceLineConfig(line_id="p04", name="P04", skew_hold_s=0.0, median_window=1)
    micro = ApplianceProfile(
        appliance_id="microondas",
        name="Microondas",
        archetype=Archetype.STEP,
        min_power_w=1100.0,
        max_power_w=1450.0,
        min_on_s=20.0,
        max_duration_s=210.0,
        min_cycle_energy_kwh=0.005,
    )
    fryer = ApplianceProfile(
        appliance_id="freidora",
        name="Freidora",
        archetype=Archetype.STEP,
        min_power_w=1150.0,
        max_power_w=1950.0,
        min_on_s=210.0,
        min_cycle_energy_kwh=0.05,
    )
    engine = NilmLineEngine(cfg, [micro, fryer])
    t = 0
    for t in range(0, 60, 10):
        engine.process_sample(LineSample(_ts(t), 10.0))

    def run(start: int, minutes: float, watts: float) -> None:
        end = start + int(minutes * 60)
        for s in range(start, end, 10):
            engine.process_sample(LineSample(_ts(s), 10.0 + watts))
        for s in range(end, end + 120, 10):
            engine.process_sample(LineSample(_ts(s), 10.0))

    run(100, 2.0, 1250.0)  # coffee in the microwave
    fr = engine.process_sample(LineSample(_ts(400), 10.0))
    micro_kwh = fr.appliances["microondas"].total_energy_kwh
    assert 0.035 <= micro_kwh <= 0.05  # ~1250 W x 2 min = 0.042 kWh
    assert fr.appliances["freidora"].total_energy_kwh == 0.0

    run(1000, 20.0, 1310.0)  # vegetables in the air fryer
    fr = engine.process_sample(LineSample(_ts(3000), 10.0))
    assert fr.appliances["microondas"].total_energy_kwh == micro_kwh  # nothing added
    fryer_kwh = fr.appliances["freidora"].total_energy_kwh
    assert 0.40 <= fryer_kwh <= 0.46  # ~1310 W x 20 min = 0.437 kWh, start back-dated
    assert fr.appliances["freidora"].last_cycle is not None
    assert fr.appliances["microondas"].last_cycle.duration_min < 3


# Real P04 readings, 26/11/2026 10:47-10:55: air fryer for ~6 min, right after the
# fridge defrost heater (~250 W on the line). The thermostat cuts the heater twice
# (10:51:42 and 10:52:28) for 11-33 s while the fan keeps ~340-590 W.
FRYER_2026_11_26 = [
    ("10:47:27", 280),
    ("10:47:32", 248),
    ("10:47:37", 280),
    ("10:47:43", 249),
    ("10:48:19", 1080),
    ("10:48:24", 2186),
    ("10:48:31", 2210),
    ("10:48:37", 2172),
    ("10:48:44", 2162),
    ("10:48:50", 2151),
    ("10:48:55", 2139),
    ("10:49:00", 2129),
    ("10:49:05", 2116),
    ("10:49:36", 2127),
    ("10:49:41", 2138),
    ("10:49:54", 2150),
    ("10:50:01", 2137),
    ("10:50:10", 2127),
    ("10:50:17", 2137),
    ("10:50:29", 2127),
    ("10:50:34", 2115),
    ("10:50:45", 2105),
    ("10:50:59", 2095),
    ("10:51:05", 2085),
    ("10:51:13", 2075),
    ("10:51:19", 2065),
    ("10:51:42", 1322),
    ("10:51:47", 586),
    ("10:52:15", 2015),
    ("10:52:20", 2091),
    ("10:52:28", 811),
    ("10:52:33", 587),
    ("10:52:39", 2062),
    ("10:52:44", 2106),
    ("10:52:49", 2102),
    ("10:52:54", 1901),
    ("10:52:59", 1874),
    ("10:53:38", 1672),
    ("10:53:43", 358),
    ("10:54:17", 344),
    ("10:54:22", 9),
    ("10:56:11", 21),
    ("10:56:16", 10),
]


def _replay(engine: NilmLineEngine, readings, until: str = "10:59:00"):
    def at(hms: str):
        h, m, s = (int(x) for x in hms.split(":"))
        return T0.replace(hour=h, minute=m, second=s)

    frames, last = [], None
    end = at(until)
    t = at(readings[0][0])
    i = 0
    while t <= end:  # 15 s heartbeat + every real report, like the coordinator
        while i < len(readings) and at(readings[i][0]) <= t:
            last = float(readings[i][1])
            frames.append(engine.process_sample(LineSample(at(readings[i][0]), last)))
            i += 1
        frames.append(engine.process_sample(LineSample(t, last)))
        t += timedelta(seconds=15)
    return frames


def test_air_fryer_survives_thermostat_cuts_real_session() -> None:
    cfg = SourceLineConfig(line_id="p04", name="P04", skew_hold_s=0.0, median_window=3)
    fryer = ApplianceProfile(
        appliance_id="freidora",
        name="Freidora",
        archetype=Archetype.STEP,
        min_power_w=1150.0,
        max_power_w=2300.0,
        edge_tolerance_w=150.0,
        min_on_s=210.0,
        min_cycle_energy_kwh=0.05,
        off_debounce_s=45.0,
    )
    engine = NilmLineEngine(cfg, [fryer])
    frames = _replay(engine, FRYER_2026_11_26)
    cycles = [c for f in frames for c in f.completed_cycles]
    assert len(cycles) == 1, cycles
    cyc = cycles[0]
    assert 5.0 <= cyc.duration_min <= 6.5  # 10:48:19 -> ~10:53:43
    assert 0.15 <= cyc.energy_kwh <= 0.22  # ~1.9 kW net for ~5.5 min
