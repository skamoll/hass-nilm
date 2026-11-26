"""Event-driven + periodic heartbeat coordinator for a monitored NILM SourceLine."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfPower
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_APPLIANCE_ID,
    CONF_APPLIANCE_NAME,
    CONF_APPLIANCES,
    CONF_ARCHETYPE,
    CONF_BASELINE_FLOOR_W,
    CONF_DEDICATED_METER,
    CONF_EDGE_TOLERANCE_W,
    CONF_END_IDLE_HOLD_S,
    CONF_HIGH_BLOCK_MIN_S,
    CONF_HIGH_BLOCK_THRESHOLD_W,
    CONF_IDLE_THRESHOLD_W,
    CONF_LINE_NAME,
    CONF_MAX_ON_S,
    CONF_MAX_PAUSE_S,
    CONF_MAX_POWER_W,
    CONF_MEDIAN_WINDOW,
    CONF_MIN_CYCLE_ENERGY_KWH,
    CONF_MIN_HIGH_BLOCKS,
    CONF_MIN_ON_S,
    CONF_MIN_POWER_W,
    CONF_NOMINAL_POWER_W,
    CONF_PARENT_POWER_ENTITY,
    CONF_PHASES,
    CONF_PHASES_JSON,
    CONF_PRIORITY,
    CONF_PROGRAMS,
    CONF_PROGRAMS_JSON,
    CONF_SKEW_DELTA_THRESHOLD_W,
    CONF_SKEW_HOLD_S,
    CONF_SUBMETER_ENTITIES,
    DEFAULT_BASELINE_FLOOR_W,
    DEFAULT_MEDIAN_WINDOW,
    DEFAULT_SKEW_DELTA_THRESHOLD_W,
    DEFAULT_SKEW_HOLD_S,
    DOMAIN,
    EVENT_CYCLE_FINISHED,
    HEARTBEAT_INTERVAL_SECONDS,
)
from .engine import (
    ApplianceProfile,
    Archetype,
    LineFrame,
    LineSample,
    NilmLineEngine,
    PhaseSpec,
    ProgramFingerprint,
    SourceLineConfig,
    SubmeterReading,
)

_LOGGER = logging.getLogger(__name__)


def state_to_watts(state: State | None) -> float | None:
    """Parse a Home Assistant state into Watts (supporting W and kW)."""
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
        return None
    try:
        raw = float(state.state)
    except (TypeError, ValueError):
        return None
    unit = state.attributes.get("unit_of_measurement")
    if unit == UnitOfPower.KILO_WATT or unit == "kW":
        return raw * 1000.0
    return raw


def _parse_json_or_list(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, str) and raw.strip():
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    return []


def dict_to_appliance_profile(data: dict[str, Any]) -> ApplianceProfile:
    """Deserialize a stored config/options dictionary into an ApplianceProfile."""
    raw_phases = data.get(CONF_PHASES) or _parse_json_or_list(data.get(CONF_PHASES_JSON))
    raw_programs = data.get(CONF_PROGRAMS) or _parse_json_or_list(data.get(CONF_PROGRAMS_JSON))

    phases = tuple(
        PhaseSpec(
            name=str(p["name"]),
            min_power_w=float(p["min_power_w"]),
            max_power_w=float(p["max_power_w"]),
            min_duration_s=float(p.get("min_duration_s", 0.0)),
            after_high_block=p.get("after_high_block"),
        )
        for p in raw_phases
    )
    programs = tuple(
        ProgramFingerprint(
            name=str(pr["name"]),
            expected_energy_kwh=float(pr["expected_energy_kwh"]),
            energy_tolerance_kwh=float(pr.get("energy_tolerance_kwh", 0.25)),
            expected_duration_min=float(pr.get("expected_duration_min", 60.0)),
            duration_tolerance_min=float(pr.get("duration_tolerance_min", 20.0)),
            expected_high_blocks=(
                int(pr["expected_high_blocks"])
                if pr.get("expected_high_blocks") is not None
                else None
            ),
        )
        for pr in raw_programs
    )
    nominal_raw = data.get(CONF_NOMINAL_POWER_W)
    nominal_w = float(nominal_raw) if nominal_raw not in (None, "", 0, 0.0) else None
    return ApplianceProfile(
        appliance_id=str(data[CONF_APPLIANCE_ID]),
        name=str(data[CONF_APPLIANCE_NAME]),
        archetype=Archetype(str(data.get(CONF_ARCHETYPE, Archetype.STEP.value))),
        priority=int(data.get(CONF_PRIORITY, 100)),
        min_power_w=float(data[CONF_MIN_POWER_W]),
        max_power_w=float(data[CONF_MAX_POWER_W]),
        nominal_power_w=nominal_w,
        edge_tolerance_w=float(data.get(CONF_EDGE_TOLERANCE_W, 80.0)),
        min_on_s=float(data.get(CONF_MIN_ON_S, 15.0)),
        max_on_s=float(data.get(CONF_MAX_ON_S, 14400.0)),
        idle_threshold_w=float(data.get(CONF_IDLE_THRESHOLD_W, 15.0)),
        max_pause_s=float(data.get(CONF_MAX_PAUSE_S, 900.0)),
        end_idle_hold_s=float(data.get(CONF_END_IDLE_HOLD_S, 180.0)),
        high_block_threshold_w=float(data.get(CONF_HIGH_BLOCK_THRESHOLD_W, 1000.0)),
        high_block_min_s=float(data.get(CONF_HIGH_BLOCK_MIN_S, 60.0)),
        min_cycle_energy_kwh=float(data.get(CONF_MIN_CYCLE_ENERGY_KWH, 0.02)),
        min_high_blocks=int(data.get(CONF_MIN_HIGH_BLOCKS, 0)),
        dedicated_meter=bool(data.get(CONF_DEDICATED_METER, False)),
        phases=phases,
        programs=programs,
    )


def build_line_config(entry: ConfigEntry) -> SourceLineConfig:
    """Build SourceLineConfig merging entry.data and entry.options."""
    merged = {**entry.data, **entry.options}
    return SourceLineConfig(
        line_id=entry.entry_id,
        name=str(merged.get(CONF_LINE_NAME, entry.title)),
        skew_hold_s=float(merged.get(CONF_SKEW_HOLD_S, DEFAULT_SKEW_HOLD_S)),
        skew_delta_threshold_w=float(
            merged.get(CONF_SKEW_DELTA_THRESHOLD_W, DEFAULT_SKEW_DELTA_THRESHOLD_W)
        ),
        median_window=int(merged.get(CONF_MEDIAN_WINDOW, DEFAULT_MEDIAN_WINDOW)),
        baseline_floor_w=float(merged.get(CONF_BASELINE_FLOOR_W, DEFAULT_BASELINE_FLOOR_W)),
    )


class NilmLineCoordinator(DataUpdateCoordinator[LineFrame | None]):
    """Coordinate live state-change listeners and heartbeat evaluations for one SourceLine."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        merged = {**entry.data, **entry.options}
        self.parent_entity_id: str = str(merged[CONF_PARENT_POWER_ENTITY])
        self.submeter_entity_ids: tuple[str, ...] = tuple(merged.get(CONF_SUBMETER_ENTITIES, ()))
        line_cfg = build_line_config(entry)
        raw_apps = merged.get(CONF_APPLIANCES, [])
        profiles = [dict_to_appliance_profile(item) for item in raw_apps]
        self.engine = NilmLineEngine(line_cfg, profiles)
        self._unsub_state_listener: CALLBACK_TYPE | None = None
        self.line_device_id: str | None = None
        self.patterns: Any = None  # PatternDiscovery, set in async_setup_entry

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=HEARTBEAT_INTERVAL_SECONDS),
        )

    def async_start_listeners(self) -> None:
        """Subscribe to instant state changes of parent and child power entities."""
        if self._unsub_state_listener is not None:
            return
        tracked = [self.parent_entity_id, *self.submeter_entity_ids]
        self._unsub_state_listener = async_track_state_change_event(
            self.hass,
            tracked,
            self._async_on_state_change,
        )

    def async_stop_listeners(self) -> None:
        """Unsubscribe from state change events."""
        if self._unsub_state_listener is not None:
            self._unsub_state_listener()
            self._unsub_state_listener = None

    @callback
    def _async_on_state_change(self, _event: Event) -> None:
        """Evaluate engine immediately when any watched power sensor updates."""
        frame = self._evaluate_now()
        if frame is not None:
            self.async_set_updated_data(frame)

    def _evaluate_now(self) -> LineFrame | None:
        """Read current states, step NilmLineEngine, and fire finished-cycle bus events."""
        parent_state = self.hass.states.get(self.parent_entity_id)
        parent_w = state_to_watts(parent_state)
        if parent_w is None:
            return self.data

        now = dt_util.utcnow()
        sub_readings: list[SubmeterReading] = []
        for sub_eid in self.submeter_entity_ids:
            st = self.hass.states.get(sub_eid)
            sub_w = state_to_watts(st)
            if sub_w is not None:
                sub_readings.append(
                    SubmeterReading(
                        submeter_id=sub_eid,
                        power_w=sub_w,
                        timestamp=st.last_updated if st is not None else now,
                    )
                )

        sample = LineSample(
            timestamp=now,
            parent_power_w=parent_w,
            submeters=tuple(sub_readings),
        )
        frame = self.engine.process_sample(sample)
        for cyc in frame.completed_cycles:
            self.hass.bus.async_fire(
                EVENT_CYCLE_FINISHED,
                {
                    "config_entry_id": self.entry.entry_id,
                    "line_name": self.engine.config.name,
                    "appliance_id": cyc.appliance_id,
                    "appliance_name": cyc.appliance_name,
                    "start_time": cyc.start_time.isoformat(),
                    "end_time": cyc.end_time.isoformat(),
                    "duration_min": cyc.duration_min,
                    "energy_kwh": cyc.energy_kwh,
                    "peak_power_w": cyc.peak_power_w,
                    "high_blocks": cyc.high_blocks,
                    "phases_seen": list(cyc.phases_seen),
                    "matched_program": cyc.matched_program,
                    "confidence": cyc.confidence,
                },
            )
        return frame

    async def _async_update_data(self) -> LineFrame | None:
        """Periodic heartbeat evaluation so pause/idle timers advance even on flat sensors."""
        return self._evaluate_now()
