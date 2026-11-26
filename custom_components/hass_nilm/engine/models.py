"""Data models for the pure-Python NILM disaggregation engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Archetype(StrEnum):
    """Supported deterministic appliance pattern archetypes."""

    STEP = "step"
    CYCLING = "cycling"
    MULTIPHASE = "multiphase"


UNKNOWN_PROGRAM = "unknown_program"


@dataclass(frozen=True, slots=True)
class SubmeterReading:
    """Single power reading of a known child sub-meter on a monitored line."""

    submeter_id: str
    power_w: float
    timestamp: datetime | None = None


@dataclass(frozen=True, slots=True)
class LineSample:
    """Point-in-time telemetry snapshot fed into a SourceLine engine."""

    timestamp: datetime
    parent_power_w: float
    submeters: tuple[SubmeterReading, ...] = ()
    current_a: float | None = None


@dataclass(frozen=True, slots=True)
class SourceLineConfig:
    """Configuration of a monitored electrical line and its residual filter."""

    line_id: str
    name: str
    skew_hold_s: float = 12.0
    skew_delta_threshold_w: float = 80.0
    median_window: int = 3
    baseline_floor_w: float = 0.0
    max_integration_gap_s: float = 900.0


@dataclass(frozen=True, slots=True)
class PhaseSpec:
    """Power and ordering rules for one phase of a multiphase appliance."""

    name: str
    min_power_w: float
    max_power_w: float
    min_duration_s: float = 0.0
    after_high_block: bool | None = None


@dataclass(frozen=True, slots=True)
class ProgramFingerprint:
    """Labeled cycle signature used to identify a specific appliance program."""

    name: str
    expected_energy_kwh: float
    energy_tolerance_kwh: float = 0.25
    expected_duration_min: float = 60.0
    duration_tolerance_min: float = 20.0
    expected_high_blocks: int | None = None


@dataclass(frozen=True, slots=True)
class ApplianceProfile:
    """Configuration and signature parameters of a target appliance."""

    appliance_id: str
    name: str
    archetype: Archetype
    min_power_w: float
    max_power_w: float
    priority: int = 100
    nominal_power_w: float | None = None
    edge_tolerance_w: float = 80.0
    min_on_s: float = 15.0
    max_on_s: float = 14400.0
    # >0: a run longer than this is NOT this appliance (e.g. a microwave never
    # runs 4 min, an air fryer always does). Energy is held until the run ends
    # and dropped if it overruns, so total_increasing counters never go back.
    max_duration_s: float = 0.0
    # >0: dips shorter than this (a thermostat cutting the heater while the fan
    # keeps running) do not end the run, neither while confirming nor once on.
    off_debounce_s: float = 0.0
    idle_threshold_w: float = 15.0
    max_pause_s: float = 900.0
    end_idle_hold_s: float = 180.0
    high_block_threshold_w: float = 1000.0
    high_block_min_s: float = 60.0
    min_cycle_energy_kwh: float = 0.02
    min_high_blocks: int = 0
    dedicated_meter: bool = False
    phases: tuple[PhaseSpec, ...] = ()
    programs: tuple[ProgramFingerprint, ...] = ()


@dataclass(frozen=True, slots=True)
class CompletedCycle:
    """Summary emitted when an appliance session or cycle finishes."""

    appliance_id: str
    appliance_name: str
    start_time: datetime
    end_time: datetime
    duration_min: float
    energy_kwh: float
    peak_power_w: float
    high_blocks: int
    phases_seen: tuple[str, ...]
    matched_program: str = UNKNOWN_PROGRAM
    confidence: float = 0.0


@dataclass(frozen=True, slots=True)
class ApplianceStatus:
    """Real-time state and energy counters for one appliance after a sample."""

    appliance_id: str
    name: str
    archetype: Archetype
    is_running: bool
    phase: str
    attributed_power_w: float
    total_energy_kwh: float
    current_cycle_energy_kwh: float
    current_cycle_duration_min: float
    high_blocks_in_cycle: int = 0
    last_cycle: CompletedCycle | None = None


@dataclass(frozen=True, slots=True)
class LineFrame:
    """Complete deterministic output of NilmLineEngine.process_sample()."""

    timestamp: datetime
    parent_power_w: float
    submeters_power_w: float
    raw_residual_w: float
    filtered_residual_w: float
    skew_guard_active: bool
    total_inferred_power_w: float
    unexplained_power_w: float
    appliances: dict[str, ApplianceStatus] = field(default_factory=dict)
    completed_cycles: tuple[CompletedCycle, ...] = ()
