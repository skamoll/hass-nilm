"""Constants for the NILM Appliance Disaggregator integration."""

from __future__ import annotations

DOMAIN = "hass_nilm"
EVENT_CYCLE_FINISHED = "hass_nilm_cycle_finished"

HEARTBEAT_INTERVAL_SECONDS = 15

# SourceLine configuration keys
LINE_MANUFACTURER = "Hass-NILM"
LINE_MODEL = "Source Line Disaggregator"

CONF_LINE_NAME = "line_name"
CONF_PARENT_POWER_ENTITY = "parent_power_entity"
CONF_SUBMETER_ENTITIES = "submeter_entities"
CONF_SKEW_HOLD_S = "skew_hold_s"
CONF_SKEW_DELTA_THRESHOLD_W = "skew_delta_threshold_w"
CONF_MEDIAN_WINDOW = "median_window"
CONF_BASELINE_FLOOR_W = "baseline_floor_w"
CONF_APPLIANCES = "appliances"

# Appliance profile configuration keys
CONF_APPLIANCE_ID = "appliance_id"
CONF_APPLIANCE_NAME = "name"
CONF_ARCHETYPE = "archetype"
CONF_PRIORITY = "priority"
CONF_MIN_POWER_W = "min_power_w"
CONF_MAX_POWER_W = "max_power_w"
CONF_NOMINAL_POWER_W = "nominal_power_w"
CONF_EDGE_TOLERANCE_W = "edge_tolerance_w"
CONF_MIN_ON_S = "min_on_s"
CONF_MAX_ON_S = "max_on_s"
CONF_MAX_DURATION_S = "max_duration_s"
CONF_OFF_DEBOUNCE_S = "off_debounce_s"
CONF_IDLE_THRESHOLD_W = "idle_threshold_w"
CONF_MAX_PAUSE_S = "max_pause_s"
CONF_END_IDLE_HOLD_S = "end_idle_hold_s"
CONF_HIGH_BLOCK_THRESHOLD_W = "high_block_threshold_w"
CONF_HIGH_BLOCK_MIN_S = "high_block_min_s"
CONF_MIN_CYCLE_ENERGY_KWH = "min_cycle_energy_kwh"
CONF_MIN_HIGH_BLOCKS = "min_high_blocks"
CONF_DEDICATED_METER = "dedicated_meter"
CONF_PHASES = "phases"
CONF_PROGRAMS = "programs"
CONF_PHASES_JSON = "phases_json"
CONF_PROGRAMS_JSON = "programs_json"

DEFAULT_SKEW_HOLD_S = 12.0
DEFAULT_SKEW_DELTA_THRESHOLD_W = 80.0
DEFAULT_MEDIAN_WINDOW = 3
DEFAULT_BASELINE_FLOOR_W = 0.0
