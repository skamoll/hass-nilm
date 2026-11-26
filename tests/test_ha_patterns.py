"""Pattern learning end to end in a real Home Assistant core: Repairs ask, user names."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

if importlib.util.find_spec("homeassistant") is None:
    pytest.skip("homeassistant package not installed", allow_module_level=True)

from homeassistant import bootstrap, loader
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.hass_nilm.const import (
    CONF_APPLIANCE_ID,
    CONF_APPLIANCE_NAME,
    CONF_APPLIANCES,
    CONF_ARCHETYPE,
    CONF_LINE_NAME,
    CONF_MAX_POWER_W,
    CONF_MIN_POWER_W,
    CONF_PARENT_POWER_ENTITY,
    DOMAIN,
)
from custom_components.hass_nilm.discovery_manager import issue_id_for


def _cand(cid: str, power: float, dur: float, per_day: float, hint: str) -> dict:
    return {
        "candidate_id": cid,
        "events": int(per_day * 20),
        "days_seen": 20,
        "events_per_day": per_day,
        "power_w": power,
        "power_range_w": [power * 0.92, power * 1.08],
        "duration_min": dur,
        "duration_range_min": [dur * 0.9, dur * 1.1],
        "energy_kwh_per_event": power * dur / 60000,
        "energy_kwh_per_day": power * dur / 60000 * per_day,
        "period_min": 1440 / per_day,
        "period_cv": 0.05,
        "periodic": True,
        "hours_profile": [1] * 24,
        "hint": hint,
        "first_seen": "2026-11-01T00:00:00+00:00",
        "last_seen": "2026-11-26T00:00:00+00:00",
        "examples": [],
    }


@pytest.mark.asyncio
async def test_new_pattern_is_asked_named_and_becomes_a_detector(tmp_path: Path) -> None:
    (tmp_path / "custom_components").symlink_to(
        Path(__file__).resolve().parent.parent / "custom_components"
    )
    hass = HomeAssistant(str(tmp_path))
    hass.config.skip_pip = True
    loader.async_setup(hass)
    await bootstrap.async_from_config_dict({}, hass)
    await hass.async_start()
    assert await async_setup_component(hass, "repairs", {})

    try:
        hass.states.async_set("sensor.p03_power", "50", {"unit_of_measurement": "W"})
        entry = ConfigEntry(
            version=1,
            minor_version=1,
            domain=DOMAIN,
            title="P03 Potencia Bajo",
            data={
                CONF_LINE_NAME: "P03 Potencia Bajo",
                CONF_PARENT_POWER_ENTITY: "sensor.p03_power",
            },
            options={
                CONF_APPLIANCES: [
                    {
                        CONF_APPLIANCE_ID: "hervidor",
                        CONF_APPLIANCE_NAME: "Hervidor",
                        CONF_ARCHETYPE: "step",
                        CONF_MIN_POWER_W: 1700.0,
                        CONF_MAX_POWER_W: 2300.0,
                    }
                ]
            },
            source="user",
            unique_id="sensor.p03_power_p03",
            discovery_keys={},
            subentries_data=None,
        )
        await hass.config_entries.async_add(entry)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED

        manager = hass.data[DOMAIN][entry.entry_id].patterns
        freezer = _cand("p70w_d16m", 70.0, 16.0, 38.0, "frío: compresor de nevera o congelador")
        lamp = _cand("p34w_d84m", 34.0, 84.0, 2.2, "carga pequeña y larga")
        kettle = _cand("p2000w_d3m", 2000.0, 3.0, 2.0, "calor rápido")
        summary = manager.process_candidates([freezer, lamp, kettle])
        assert sorted(summary["new"]) == ["p34w_d84m", "p70w_d16m"]
        assert summary["known"] == ["p2000w_d3m"]  # already explained by "Hervidor"

        reg = ir.async_get(hass)
        freezer_issue = issue_id_for(entry.entry_id, "p70w_d16m")
        lamp_issue = issue_id_for(entry.entry_id, "p34w_d84m")
        assert reg.async_get_issue(DOMAIN, freezer_issue) is not None
        assert reg.async_get_issue(DOMAIN, lamp_issue) is not None
        assert reg.async_get_issue(DOMAIN, issue_id_for(entry.entry_id, "p2000w_d3m")) is None
        issue = reg.async_get_issue(DOMAIN, freezer_issue)
        assert issue.is_fixable and issue.translation_placeholders["power"] == "70"

        # Same load seen again next day with slightly different stats: not re-asked.
        again = manager.process_candidates([dict(freezer, power_w=72.0, duration_min=15.5)])
        assert again["new"] == [] and again["updated"] == ["p70w_d16m"]

        # The user answers in Repairs: "Congelador".
        flows = repairs_flow_manager(hass)
        res = await flows.async_init(DOMAIN, context={"issue_id": freezer_issue})
        assert res["type"] is FlowResultType.MENU
        res = await flows.async_configure(res["flow_id"], {"next_step_id": "name_it"})
        assert res["type"] is FlowResultType.FORM
        res = await flows.async_configure(res["flow_id"], {"name": "Congelador"})
        assert res["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

        assert reg.async_get_issue(DOMAIN, freezer_issue) is None
        apps = {a[CONF_APPLIANCE_ID]: a for a in entry.options[CONF_APPLIANCES]}
        assert "congelador" in apps
        assert apps["congelador"][CONF_MIN_POWER_W] < 70 < apps["congelador"][CONF_MAX_POWER_W]
        assert apps["congelador"]["learned_from"] == "p70w_d16m"
        coord = hass.data[DOMAIN][entry.entry_id]
        assert "congelador" in coord.engine.profiles  # entry reloaded with the new detector
        assert coord.patterns.data["patterns"]["p70w_d16m"]["status"] == "named"

        # And ignores the other one.
        res = await flows.async_init(DOMAIN, context={"issue_id": lamp_issue})
        res = await flows.async_configure(res["flow_id"], {"next_step_id": "ignore"})
        assert res["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        assert reg.async_get_issue(DOMAIN, lamp_issue) is None
        assert coord.patterns.data["patterns"]["p34w_d84m"]["status"] == "ignored"
        assert len(entry.options[CONF_APPLIANCES]) == 2  # ignoring creates nothing
    finally:
        await hass.async_stop(force=True)
