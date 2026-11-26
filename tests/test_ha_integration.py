"""End-to-end Home Assistant Core integration tests for Hass-NILM."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

if importlib.util.find_spec("homeassistant") is None:
    pytest.skip("homeassistant package not installed", allow_module_level=True)

from homeassistant import bootstrap, loader
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from custom_components.hass_nilm.const import (
    CONF_APPLIANCE_ID,
    CONF_APPLIANCE_NAME,
    CONF_APPLIANCES,
    CONF_ARCHETYPE,
    CONF_LINE_NAME,
    CONF_MAX_POWER_W,
    CONF_MEDIAN_WINDOW,
    CONF_MIN_CYCLE_ENERGY_KWH,
    CONF_MIN_ON_S,
    CONF_MIN_POWER_W,
    CONF_PARENT_POWER_ENTITY,
    CONF_SKEW_HOLD_S,
    CONF_SUBMETER_ENTITIES,
    DOMAIN,
    EVENT_CYCLE_FINISHED,
)
from custom_components.hass_nilm.diagnostics import async_get_config_entry_diagnostics


@pytest.mark.asyncio
async def test_ha_end_to_end_disaggregation_services_and_unload(tmp_path: Path) -> None:
    """Verify live HA state disaggregation (kW + W), cycle event, services, and unload."""
    (tmp_path / "custom_components").symlink_to(
        Path(__file__).resolve().parent.parent / "custom_components"
    )
    hass = HomeAssistant(str(tmp_path))
    hass.config.skip_pip = True
    loader.async_setup(hass)
    await bootstrap.async_from_config_dict({}, hass)
    await hass.async_start()

    finished_events: list[dict] = []

    @callback
    def _on_cycle(ev: Event) -> None:
        finished_events.append(dict(ev.data))

    hass.bus.async_listen(EVENT_CYCLE_FINISHED, _on_cycle)

    try:
        # Parent sensor in kW (0.6 kW = 600 W), child known plug in W (500 W) -> 100 W residual
        hass.states.async_set("sensor.home_mains_power", "0.6", {"unit_of_measurement": "kW"})
        hass.states.async_set("sensor.plug_termo_power", "500", {"unit_of_measurement": "W"})

        entry = ConfigEntry(
            version=1,
            minor_version=1,
            domain=DOMAIN,
            title="Home Mains",
            data={
                CONF_LINE_NAME: "Home Mains",
                CONF_PARENT_POWER_ENTITY: "sensor.home_mains_power",
                CONF_SUBMETER_ENTITIES: ["sensor.plug_termo_power"],
                CONF_SKEW_HOLD_S: 0.0,
                CONF_MEDIAN_WINDOW: 1,
            },
            options={
                CONF_APPLIANCES: [
                    {
                        CONF_APPLIANCE_ID: "kettle",
                        CONF_APPLIANCE_NAME: "Kettle",
                        CONF_ARCHETYPE: "step",
                        CONF_MIN_POWER_W: 1800.0,
                        CONF_MAX_POWER_W: 2400.0,
                        CONF_MIN_ON_S: 0.0,
                        CONF_MIN_CYCLE_ENERGY_KWH: 0.0,
                    }
                ]
            },
            source="user",
            unique_id="sensor.home_mains_power_home_mains",
            discovery_keys={},
            subentries_data=None,
        )
        await hass.config_entries.async_add(entry)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        ent_reg = er.async_get(hass)
        entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        unique_suffixes = {e.unique_id.removeprefix(f"{entry.entry_id}_") for e in entities}
        assert {
            "line_residual_power",
            "line_submeters_power",
            "line_inferred_power",
            "line_unexplained_power",
            "line_skew_guard_active",
            "kettle_running",
            "kettle_power",
            "kettle_energy",
            "kettle_phase",
            "kettle_last_cycle",
        } <= unique_suffixes

        coord = hass.data[DOMAIN][entry.entry_id]
        assert coord.data is not None
        assert coord.data.filtered_residual_w == 100.0
        assert coord.data.appliances["kettle"].is_running is False

        # Kettle turns ON (+2.0 kW on mains -> 2.6 kW = 2600 W; residual = 2100 W)
        hass.states.async_set("sensor.home_mains_power", "2.6", {"unit_of_measurement": "kW"})
        await hass.async_block_till_done()
        assert coord.data.appliances["kettle"].is_running is True
        assert coord.data.appliances["kettle"].attributed_power_w >= 1800.0

        # Kettle turns OFF (back to 0.6 kW) -> fires hass_nilm_cycle_finished
        hass.states.async_set("sensor.home_mains_power", "0.6", {"unit_of_measurement": "kW"})
        await hass.async_block_till_done()
        assert coord.data.appliances["kettle"].is_running is False
        assert len(finished_events) == 1
        assert finished_events[0]["appliance_id"] == "kettle"

        # Diagnostics check
        diag = await async_get_config_entry_diagnostics(hass, entry)
        assert "kettle" in diag["profiles"]
        assert diag["entry"]["unique_id"] == "**REDACTED**"

        # Add a second appliance dynamically via admin service
        await hass.services.async_call(
            DOMAIN,
            "upsert_appliance",
            {
                "config_entry_id": entry.entry_id,
                "name": "Bosch Oven",
                "archetype": "cycling",
                "min_power_w": 1900,
                "max_power_w": 2500,
            },
            blocking=True,
        )
        await hass.async_block_till_done()
        coord = hass.data[DOMAIN][entry.entry_id]
        assert "bosch_oven" in coord.engine.profiles

        # Remove it via admin service
        await hass.services.async_call(
            DOMAIN,
            "remove_appliance",
            {
                "config_entry_id": entry.entry_id,
                "appliance_id": "bosch_oven",
            },
            blocking=True,
        )
        await hass.async_block_till_done()
        coord = hass.data[DOMAIN][entry.entry_id]
        assert "bosch_oven" not in coord.engine.profiles

        # Clean unload removes services when last entry unloads
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.NOT_LOADED
        assert not hass.services.has_service(DOMAIN, "scan_recorder")
    finally:
        await hass.async_stop()
