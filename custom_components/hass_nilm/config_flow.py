"""Config flow and Options flow for NILM Appliance Disaggregator."""

from __future__ import annotations

import json
from typing import Any

import voluptuous as vol
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import slugify

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
    CONF_PARENT_POWER_ENTITY,
    CONF_PHASES_JSON,
    CONF_PRIORITY,
    CONF_PROGRAMS_JSON,
    CONF_SKEW_HOLD_S,
    CONF_SUBMETER_ENTITIES,
    DEFAULT_BASELINE_FLOOR_W,
    DEFAULT_MEDIAN_WINDOW,
    DEFAULT_SKEW_HOLD_S,
    DOMAIN,
)
from .engine import Archetype


def _line_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_LINE_NAME, default=defaults.get(CONF_LINE_NAME, "Home Mains")
            ): selector.TextSelector(),
            vol.Required(
                CONF_PARENT_POWER_ENTITY,
                default=defaults.get(CONF_PARENT_POWER_ENTITY, vol.UNDEFINED),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor",
                    device_class=SensorDeviceClass.POWER,
                )
            ),
            vol.Optional(
                CONF_SUBMETER_ENTITIES,
                default=list(defaults.get(CONF_SUBMETER_ENTITIES, [])),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor",
                    device_class=SensorDeviceClass.POWER,
                    multiple=True,
                )
            ),
            vol.Optional(
                CONF_SKEW_HOLD_S,
                default=float(defaults.get(CONF_SKEW_HOLD_S, DEFAULT_SKEW_HOLD_S)),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=120,
                    step=1,
                    unit_of_measurement="s",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_MEDIAN_WINDOW,
                default=int(defaults.get(CONF_MEDIAN_WINDOW, DEFAULT_MEDIAN_WINDOW)),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=15, step=2, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Optional(
                CONF_BASELINE_FLOOR_W,
                default=float(defaults.get(CONF_BASELINE_FLOOR_W, DEFAULT_BASELINE_FLOOR_W)),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=5000,
                    step=5,
                    unit_of_measurement="W",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }
    )


def _appliance_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_APPLIANCE_NAME, default=d.get(CONF_APPLIANCE_NAME, "")
            ): selector.TextSelector(),
            vol.Required(
                CONF_ARCHETYPE, default=d.get(CONF_ARCHETYPE, Archetype.STEP.value)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[a.value for a in Archetype],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key="archetype",
                )
            ),
            vol.Required(
                CONF_MIN_POWER_W, default=float(d.get(CONF_MIN_POWER_W, 100.0))
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=25000,
                    step=5,
                    unit_of_measurement="W",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_MAX_POWER_W, default=float(d.get(CONF_MAX_POWER_W, 2500.0))
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=5,
                    max=25000,
                    step=10,
                    unit_of_measurement="W",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_EDGE_TOLERANCE_W, default=float(d.get(CONF_EDGE_TOLERANCE_W, 80.0))
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=5,
                    max=2000,
                    step=5,
                    unit_of_measurement="W",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_MIN_ON_S, default=float(d.get(CONF_MIN_ON_S, 15.0))
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=3600,
                    step=5,
                    unit_of_measurement="s",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_MAX_ON_S, default=float(d.get(CONF_MAX_ON_S, 14400.0))
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=60,
                    max=86400,
                    step=60,
                    unit_of_measurement="s",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_IDLE_THRESHOLD_W, default=float(d.get(CONF_IDLE_THRESHOLD_W, 15.0))
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=1000,
                    step=1,
                    unit_of_measurement="W",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_END_IDLE_HOLD_S, default=float(d.get(CONF_END_IDLE_HOLD_S, 180.0))
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=10,
                    max=7200,
                    step=10,
                    unit_of_measurement="s",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_MAX_PAUSE_S, default=float(d.get(CONF_MAX_PAUSE_S, 900.0))
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=10,
                    max=14400,
                    step=10,
                    unit_of_measurement="s",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_HIGH_BLOCK_THRESHOLD_W,
                default=float(d.get(CONF_HIGH_BLOCK_THRESHOLD_W, 1000.0)),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=50,
                    max=25000,
                    step=25,
                    unit_of_measurement="W",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_HIGH_BLOCK_MIN_S, default=float(d.get(CONF_HIGH_BLOCK_MIN_S, 60.0))
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=5,
                    max=3600,
                    step=5,
                    unit_of_measurement="s",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_MIN_CYCLE_ENERGY_KWH,
                default=float(d.get(CONF_MIN_CYCLE_ENERGY_KWH, 0.02)),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.0,
                    max=50.0,
                    step=0.01,
                    unit_of_measurement="kWh",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_MIN_HIGH_BLOCKS, default=int(d.get(CONF_MIN_HIGH_BLOCKS, 0))
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=20, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Optional(
                CONF_PRIORITY, default=int(d.get(CONF_PRIORITY, 100))
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=1000, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Optional(
                CONF_DEDICATED_METER, default=bool(d.get(CONF_DEDICATED_METER, False))
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_PHASES_JSON, default=str(d.get(CONF_PHASES_JSON, ""))
            ): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
            vol.Optional(
                CONF_PROGRAMS_JSON, default=str(d.get(CONF_PROGRAMS_JSON, ""))
            ): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
        }
    )


def _validate_appliance_input(user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not str(user_input.get(CONF_APPLIANCE_NAME, "")).strip():
        errors[CONF_APPLIANCE_NAME] = "name_required"
    if float(user_input[CONF_MAX_POWER_W]) < float(user_input[CONF_MIN_POWER_W]):
        errors[CONF_MAX_POWER_W] = "max_below_min"
    for field in (CONF_PHASES_JSON, CONF_PROGRAMS_JSON):
        raw = str(user_input.get(field, "")).strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, list):
                    errors[field] = "invalid_json"
            except ValueError:
                errors[field] = "invalid_json"
    return errors


class HassNilmConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for NILM Appliance Disaggregator."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure a monitored SourceLine."""
        errors: dict[str, str] = {}
        if user_input is not None:
            parent = str(user_input[CONF_PARENT_POWER_ENTITY])
            subs = [s for s in user_input.get(CONF_SUBMETER_ENTITIES, []) if s != parent]
            user_input[CONF_SUBMETER_ENTITIES] = subs
            uid = f"{parent}_{slugify(user_input[CONF_LINE_NAME])}"
            await self.async_set_unique_id(uid)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=str(user_input[CONF_LINE_NAME]),
                data=user_input,
                options={CONF_APPLIANCES: []},
            )

        return self.async_show_form(
            step_id="user",
            data_schema=_line_schema({}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HassNilmOptionsFlow:
        """Create the options flow."""
        return HassNilmOptionsFlow()


class HassNilmOptionsFlow(OptionsFlow):
    """Manage SourceLine settings and appliance profiles."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Display options menu."""
        apps = list(self.config_entry.options.get(CONF_APPLIANCES, []))
        menu_options = ["line_settings", "add_appliance"]
        if apps:
            menu_options.append("remove_appliance")
        return self.async_show_menu(step_id="init", menu_options=menu_options)

    async def async_step_line_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update SourceLine parameters."""
        merged = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            parent = str(user_input[CONF_PARENT_POWER_ENTITY])
            user_input[CONF_SUBMETER_ENTITIES] = [
                s for s in user_input.get(CONF_SUBMETER_ENTITIES, []) if s != parent
            ]
            new_opts = {
                **self.config_entry.options,
                **user_input,
                CONF_APPLIANCES: list(self.config_entry.options.get(CONF_APPLIANCES, [])),
            }
            return self.async_create_entry(title="", data=new_opts)

        return self.async_show_form(
            step_id="line_settings",
            data_schema=_line_schema(merged),
        )

    async def async_step_add_appliance(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add or update an appliance detector profile."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_appliance_input(user_input)
            if not errors:
                apps = list(self.config_entry.options.get(CONF_APPLIANCES, []))
                aid = slugify(str(user_input[CONF_APPLIANCE_NAME]))
                record = {**user_input, CONF_APPLIANCE_ID: aid}
                apps = [a for a in apps if a.get(CONF_APPLIANCE_ID) != aid]
                apps.append(record)
                return self.async_create_entry(
                    title="",
                    data={**self.config_entry.options, CONF_APPLIANCES: apps},
                )

        return self.async_show_form(
            step_id="add_appliance",
            data_schema=_appliance_schema(user_input),
            errors=errors,
        )

    async def async_step_remove_appliance(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove an existing appliance profile."""
        apps = list(self.config_entry.options.get(CONF_APPLIANCES, []))
        if user_input is not None:
            target = str(user_input[CONF_APPLIANCE_ID])
            remaining = [a for a in apps if a.get(CONF_APPLIANCE_ID) != target]
            return self.async_create_entry(
                title="",
                data={**self.config_entry.options, CONF_APPLIANCES: remaining},
            )

        select_opts = [
            selector.SelectOptionDict(
                value=str(a[CONF_APPLIANCE_ID]),
                label=f"{a[CONF_APPLIANCE_NAME]} ({a.get(CONF_ARCHETYPE, 'step')})",
            )
            for a in apps
        ]
        return self.async_show_form(
            step_id="remove_appliance",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_APPLIANCE_ID): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=select_opts,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )
